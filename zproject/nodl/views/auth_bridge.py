import hashlib
import json
import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from nodl.extensions.mapping import record_realm_user_mapping
from zerver.models import Realm, UserProfile
from zproject.nodl.actions import (
    acquire_phone_link_lock,
    check_duplicate_phone,
    check_link_rate_limit,
    derive_email,
    find_email_identity,
    find_existing_zulip_user_by_email,
    get_or_create_zulip_user,
    get_supabase_user_by_email,
    get_supabase_user_by_id,
    get_user_workspace_ids,
    link_phone_to_existing_user,
    mask_email,
    release_phone_link_lock,
    validate_e164_phone,
)
from zproject.nodl.auth import JWTValidationError, validate_supabase_jwt
from zproject.nodl.models import NodlRegistrationPin
from zproject.nodl.throttle import check_rate_limit
from zproject.nodl.views.invites import mark_invite_registered
from zproject.nodl.workspace_resolution import (
    rank_realms_for_user,
    resolve_candidate_realms,
)

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def auth_bridge(request: HttpRequest) -> JsonResponse:
    """Exchange a Supabase JWT for a Zulip API key, with account linking support.

    POST /nodl/auth/bridge
    Authorization: Bearer <supabase-jwt>

    Optional body (JSON):
        link_action: "link" | "create_new"  -- confirm linking decision

    Returns:
        Normal: {"result": "success", "msg": "", "api_key": "...", "user_id": 123, "email": "..."}
        Linking available: {"result": "success", "msg": "", "linking_available": true,
                           "existing_email_masked": "m***@example.com", "existing_user_id": 123}
        Duplicate phone: {"result": "success", "msg": "", "duplicate_phone": true}
        No workspace: {"result": "no_workspace", "msg": "", "code": "NO_WORKSPACE"}
            (HTTP 200 — a valid state of a valid account; the client shows a
            waiting-for-invite state)
        Workspace lookup unavailable: HTTP 503 {"result": "error",
            "code": "SERVICE_UNAVAILABLE"}
    """
    try:
        return _auth_bridge_inner(request)
    except Exception:
        logger.exception("NODL_DEBUG: Unhandled exception in auth_bridge")
        return JsonResponse(
            {
                "result": "error",
                "msg": "Internal server error",
                "code": "INTERNAL_ERROR",
            },
            status=500,
        )


def _auth_bridge_inner(request: HttpRequest) -> JsonResponse:
    """Inner auth bridge logic, wrapped by auth_bridge() for error handling."""
    # Rate limit by IP
    rate_limit_response = check_rate_limit(request)
    if rate_limit_response is not None:
        return rate_limit_response

    # Extract Bearer token
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        return JsonResponse(
            {"result": "error", "msg": "Invalid JWT token", "code": "UNAUTHORIZED"},
            status=401,
        )
    token = auth_header[7:]

    # Validate JWT
    try:
        payload = validate_supabase_jwt(token)
    except JWTValidationError as e:
        logger.warning("NODL_DEBUG: JWT validation failed: %s", e.message)
        return JsonResponse(
            {"result": "error", "msg": "Invalid JWT token", "code": "UNAUTHORIZED"},
            status=401,
        )

    supabase_user_id = payload.get("sub", "")
    logger.info("NODL_DEBUG: JWT validated, sub=%s phone=%s", payload.get("sub"), payload.get("phone"))

    # Find realm based on user's workspace membership.  There is deliberately
    # NO fallback realm: placing (and provisioning!) a user in a realm they
    # have no membership in is a cross-tenant leak.
    workspace_ids = get_user_workspace_ids(supabase_user_id)
    if workspace_ids is None:
        # The RPC could not be consulted — an operational failure, not a
        # statement about the user's membership.  Fail loud.
        logger.error(
            "NODL_DEBUG: workspace lookup unavailable for sub=%s", supabase_user_id
        )
        return JsonResponse(
            {
                "result": "error",
                "msg": "Workspace lookup unavailable",
                "code": "SERVICE_UNAVAILABLE",
            },
            status=503,
        )

    candidate_realms = resolve_candidate_realms(workspace_ids)
    ranked_realms = rank_realms_for_user(candidate_realms, derive_email(payload))
    if not ranked_realms:
        logger.info(
            "NODL_DEBUG: no resolvable workspace for sub=%s (%d workspace ids)",
            supabase_user_id,
            len(workspace_ids),
        )
        return JsonResponse(
            {"result": "no_workspace", "msg": "", "code": "NO_WORKSPACE"}
        )

    realm = ranked_realms[0]
    logger.info(
        "NODL_DEBUG: realm resolved for sub=%s: id=%d string_id=%r (of %d candidates)",
        supabase_user_id,
        realm.id,
        realm.string_id,
        len(ranked_realms),
    )

    # Parse optional link_action from request body
    link_action = None
    if request.content_type == "application/json" and request.body:
        try:
            body = json.loads(request.body)
            link_action = body.get("link_action")
        except (json.JSONDecodeError, ValueError):
            pass
    phone = payload.get("phone", "")

    # Normalize phone: Supabase may store without '+' prefix
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"

    # Validate phone format if present (H2: E.164 validation)
    if phone and not validate_e164_phone(phone):
        logger.warning("NODL_DEBUG: phone failed E164 validation: %s", phone)
        return JsonResponse(
            {
                "result": "error",
                "msg": "Invalid phone number format",
                "code": "BAD_REQUEST",
            },
            status=400,
        )

    # Handle link confirmation (Task 2)
    if link_action is not None:
        return _handle_link_action(link_action, payload, phone, supabase_user_id, realm)

    # Account detection logic (Task 1)
    # Check for duplicate phone first
    logger.info("NODL_DEBUG: checking duplicate phone, phone=%s", phone)
    if phone and check_duplicate_phone(supabase_user_id, phone):
        return JsonResponse({"result": "success", "msg": "", "duplicate_phone": True})

    # If this Supabase user also carries an email identity and a Zulip user
    # with that email already exists in the resolved realm, log straight into
    # that profile (auto-link). Supabase has already authenticated this human
    # and asserts both identities belong to the same account, so a separate
    # confirmation step adds no security. (The old interactive linking
    # handshake was broken end-to-end anyway: the confirm endpoint was never
    # registered in urls.py and shipped clients parse different JSON keys, so
    # returning `linking_available` without an api_key just hung the client's
    # registration spinner.)
    logger.info("NODL_DEBUG: checking supabase user, id=%s", supabase_user_id)
    if supabase_user_id:
        supabase_user = get_supabase_user_by_id(supabase_user_id)
        if supabase_user is not None:
            existing_email = find_email_identity(supabase_user)
            if existing_email:
                existing_zulip_user = find_existing_zulip_user_by_email(existing_email, realm)
                if existing_zulip_user is not None:
                    logger.info(
                        "NODL_DEBUG: auto-linking to existing user %d (%s) in realm %d",
                        existing_zulip_user.id,
                        mask_email(existing_email),
                        realm.id,
                    )
                    record_realm_user_mapping(realm, existing_zulip_user, supabase_user_id)
                    return JsonResponse(
                        {
                            "result": "success",
                            "msg": "",
                            "api_key": existing_zulip_user.api_key,
                            "user_id": existing_zulip_user.id,
                            "email": existing_zulip_user.delivery_email,
                            "has_pin": NodlRegistrationPin.objects.filter(
                                user=existing_zulip_user
                            ).exists(),
                            "is_new_device": True,
                            "linked_email_masked": mask_email(existing_email),
                        }
                    )

    # Default: get or create user (same as Story 1.2 flow)
    # Check if user already exists (for is_new_device detection)
    email = derive_email(payload)
    logger.info("NODL_DEBUG: derived email=%s", email)
    # is_new_device: true if user already had an account before this call.
    # MVP decision: this means every sign-in after sign-out triggers PIN
    # verification, even on the same physical device. This deviates from
    # AC6 ("existing device = no PIN") but provides stronger security --
    # a global sign-out invalidates all sessions, so re-auth on any device
    # (including the original) should require PIN. Accepted as intentional
    # for MVP per team review.
    user_existed_before = UserProfile.objects.filter(
        delivery_email__iexact=email,
        realm=realm,
        is_active=True,
    ).exists()

    logger.info("NODL_DEBUG: user_existed_before=%s, calling get_or_create_zulip_user", user_existed_before)
    try:
        user_profile = get_or_create_zulip_user(payload, realm)
    except Exception:
        logger.exception("Failed to get or create user for Supabase sub=%s", payload.get("sub"))
        return JsonResponse(
            {
                "result": "error",
                "msg": "User provisioning failed",
                "code": "INTERNAL_ERROR",
            },
            status=500,
        )

    logger.info("NODL_DEBUG: user_profile id=%d email=%s", user_profile.id, user_profile.delivery_email)

    # Mark any pending invites for this phone as registered
    if phone and not user_existed_before:
        phone_hash = hashlib.sha256(phone.encode("utf-8")).hexdigest()
        mark_invite_registered(phone_hash, user_profile)

    # has_pin: true if user has a Registration Lock PIN set
    has_pin = NodlRegistrationPin.objects.filter(user=user_profile).exists()
    is_new_device = user_existed_before

    logger.info("NODL_DEBUG: success, returning api_key for user %d", user_profile.id)
    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "api_key": user_profile.api_key,
            "user_id": user_profile.id,
            "email": user_profile.delivery_email,
            "has_pin": has_pin,
            "is_new_device": is_new_device,
        }
    )


def _handle_link_action(
    link_action: str,
    payload: dict,
    phone: str,
    supabase_user_id: str,
    realm: Realm,
) -> JsonResponse:
    """Handle link confirmation or create_new actions."""
    if link_action not in ("link", "create_new"):
        return JsonResponse(
            {"result": "error", "msg": "Invalid link_action", "code": "BAD_REQUEST"},
            status=400,
        )

    # Rate limit link attempts per phone
    if phone and check_link_rate_limit(phone):
        response = JsonResponse(
            {
                "result": "error",
                "msg": "Too many link attempts",
                "code": "RATE_LIMIT_HIT",
            },
            status=429,
        )
        response["Retry-After"] = "3600"
        return response

    if link_action == "link":
        return _handle_link(payload, phone, supabase_user_id, realm)
    else:  # create_new
        return _handle_create_new(payload, realm)


def _handle_link(
    payload: dict,
    phone: str,
    supabase_user_id: str,
    realm: Realm,
) -> JsonResponse:
    """Link the phone identity to an existing account."""
    # Re-verify: fetch the Supabase user and find the email identity
    supabase_user = get_supabase_user_by_id(supabase_user_id)
    if supabase_user is None:
        return JsonResponse(
            {
                "result": "error",
                "msg": "Failed to verify account",
                "code": "INTERNAL_ERROR",
            },
            status=500,
        )

    existing_email = find_email_identity(supabase_user)
    if not existing_email:
        return JsonResponse(
            {
                "result": "error",
                "msg": "No email identity found",
                "code": "BAD_REQUEST",
            },
            status=400,
        )

    existing_zulip_user = find_existing_zulip_user_by_email(existing_email, realm)
    if existing_zulip_user is None:
        return JsonResponse(
            {
                "result": "error",
                "msg": "Existing account not found",
                "code": "BAD_REQUEST",
            },
            status=400,
        )

    # H1 fix: Look up the *email* user's Supabase UUID (not the phone user's).
    # The phone user's Supabase ID (supabase_user_id) is NOT the target for linking.
    # We need the Supabase user who owns the email identity.
    email_supabase_user = get_supabase_user_by_email(existing_email)
    if email_supabase_user is None:
        logger.warning("Could not find Supabase user for email %s during link", existing_email)
        return JsonResponse(
            {
                "result": "error",
                "msg": "Failed to resolve target account",
                "code": "INTERNAL_ERROR",
            },
            status=500,
        )
    existing_supabase_id = email_supabase_user["id"]

    # H3 fix: Acquire cache lock to prevent TOCTOU race between duplicate-check
    # and the actual link_phone_to_existing_user call.
    if phone:
        if not acquire_phone_link_lock(phone):
            return JsonResponse(
                {
                    "result": "error",
                    "msg": "Link operation in progress, please retry",
                    "code": "CONFLICT",
                },
                status=409,
            )
        try:
            success = link_phone_to_existing_user(existing_supabase_id, phone)
        finally:
            release_phone_link_lock(phone)

        if not success:
            return JsonResponse(
                {
                    "result": "error",
                    "msg": "Account linking failed",
                    "code": "INTERNAL_ERROR",
                },
                status=500,
            )

    # The phone identity now lives on the email user's Supabase account, so
    # that account is the one this profile realizes.
    record_realm_user_mapping(realm, existing_zulip_user, existing_supabase_id)

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "api_key": existing_zulip_user.api_key,
            "user_id": existing_zulip_user.id,
            "email": existing_zulip_user.delivery_email,
        }
    )


def _handle_create_new(payload: dict, realm: Realm) -> JsonResponse:
    """Provision a new Zulip account for the phone-only Supabase user."""
    email = derive_email(payload)
    user_existed_before = UserProfile.objects.filter(
        delivery_email__iexact=email,
        realm=realm,
        is_active=True,
    ).exists()

    try:
        user_profile = get_or_create_zulip_user(payload, realm)
    except Exception:
        logger.exception("Failed to create new user for Supabase sub=%s", payload.get("sub"))
        return JsonResponse(
            {
                "result": "error",
                "msg": "User provisioning failed",
                "code": "INTERNAL_ERROR",
            },
            status=500,
        )

    has_pin = NodlRegistrationPin.objects.filter(user=user_profile).exists()
    is_new_device = user_existed_before

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "api_key": user_profile.api_key,
            "user_id": user_profile.id,
            "email": user_profile.delivery_email,
            "has_pin": has_pin,
            "is_new_device": is_new_device,
        }
    )
