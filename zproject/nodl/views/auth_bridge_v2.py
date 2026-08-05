"""Bridge v2: exchange a Supabase JWT for the user's full workspace list.

Where v1 picks ONE realm and returns one credential, v2 returns every
workspace the human has a realm profile in, each with its own credentials —
the mobile workspace switcher's data source.

The workspace list is served from a local join over NodlRealmUserExtension
(the human-to-realm-profile map maintained by nodl.extensions.mapping); the
Supabase workspace RPC is NOT consulted on the steady-state path.  Two
recovery paths keep cold starts working:

- self-heal: profiles matching the caller's email identities that have no
  mapping row yet are mapped on the fly (same identity rule as the v1
  auto-link and the backfill's email tier);
- cold-start reconcile: a caller with ZERO mappings after self-heal falls
  back to the v1 resolution (workspace RPC + ranking) and provisions
  profiles in their member realms, recording mappings so the next call is
  local.  This is a deliberate deviation from "RPC fully retired": without
  it, a phone-only invitee whose profiles were never synced would be told
  ``no_workspace`` forever.

Response contract (POST /nodl/auth/bridge/v2, Authorization: Bearer <jwt>):

    success: {
        "result": "success", "msg": "",
        "supabase_user_id": "...",
        "has_pin": bool,             # per-human under NODL_PIN_PER_HUMAN
        "is_new_device": bool,
        "default_workspace_id": "<uuid>",
        "workspaces": [
            {"workspace_id": "<uuid>", "name": ..., "realm_string_id": ...,
             "user_id": ..., "email": ..., "api_key": ...,
             "role": "owner|admin|moderator|member|guest",
             "unread_count": int, "last_message_id": int, "rank": int},
            ...ordered by rank...
        ],
    }
    no workspaces:   {"result": "no_workspace", "msg": "", "code": "NO_WORKSPACE"}   (HTTP 200)
    duplicate phone: {"result": "duplicate_phone", "msg": "", "code": "DUPLICATE_PHONE"} (HTTP 200)
        — unlike v1, NEVER a credential-less "success" (that shape hung the
          v1 client's registration spinner forever).
    workspace lookup unavailable (cold start only): HTTP 503.

Response bodies are never logged: one body carries N api_keys.
"""

import hashlib
import logging
import uuid

from django.conf import settings
from django.db.models import Count, Max
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from nodl.extensions.mapping import record_realm_user_mapping
from nodl.extensions.models import NodlRealmExtension, NodlRealmUserExtension
from zerver.models import UserActivity, UserMessage, UserProfile
from zproject.nodl.actions import (
    check_duplicate_phone,
    derive_email,
    find_email_identity,
    get_or_create_zulip_user,
    get_supabase_user_by_id,
    get_user_workspace_ids,
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

ROLE_NAMES = {
    UserProfile.ROLE_REALM_OWNER: "owner",
    UserProfile.ROLE_REALM_ADMINISTRATOR: "admin",
    UserProfile.ROLE_MODERATOR: "moderator",
    UserProfile.ROLE_MEMBER: "member",
    UserProfile.ROLE_GUEST: "guest",
}


@csrf_exempt
@require_POST
def auth_bridge_v2(request: HttpRequest) -> JsonResponse:
    try:
        return _auth_bridge_v2_inner(request)
    except Exception:
        logger.exception("NODL_DEBUG: Unhandled exception in auth_bridge_v2")
        return JsonResponse(
            {"result": "error", "msg": "Internal server error", "code": "INTERNAL_ERROR"},
            status=500,
        )


def _auth_bridge_v2_inner(request: HttpRequest) -> JsonResponse:
    rate_limit_response = check_rate_limit(request)
    if rate_limit_response is not None:
        return rate_limit_response

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        return JsonResponse(
            {"result": "error", "msg": "Invalid JWT token", "code": "UNAUTHORIZED"},
            status=401,
        )
    try:
        payload = validate_supabase_jwt(auth_header[7:])
    except JWTValidationError as e:
        logger.warning("NODL_DEBUG: v2 JWT validation failed: %s", e.message)
        return JsonResponse(
            {"result": "error", "msg": "Invalid JWT token", "code": "UNAUTHORIZED"},
            status=401,
        )

    supabase_user_id = payload.get("sub", "")

    phone = payload.get("phone", "")
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"
    if phone and not validate_e164_phone(phone):
        logger.warning("NODL_DEBUG: v2 phone failed E164 validation")
        return JsonResponse(
            {"result": "error", "msg": "Invalid phone number format", "code": "BAD_REQUEST"},
            status=400,
        )

    if phone and check_duplicate_phone(supabase_user_id, phone):
        return JsonResponse({"result": "duplicate_phone", "msg": "", "code": "DUPLICATE_PHONE"})

    _self_heal_mappings(payload, supabase_user_id)

    mappings = _load_mappings(supabase_user_id)
    provisioned_fresh = False
    if not mappings:
        reconcile = _reconcile_cold_start(payload, supabase_user_id)
        if isinstance(reconcile, JsonResponse):
            return reconcile
        provisioned_fresh = reconcile
        mappings = _load_mappings(supabase_user_id)

    entries = _build_workspace_entries(mappings)
    if not entries:
        logger.info(
            "NODL_DEBUG: v2 no workspaces for sub=%s (%d raw mappings)",
            supabase_user_id,
            len(mappings),
        )
        return JsonResponse({"result": "no_workspace", "msg": "", "code": "NO_WORKSPACE"})

    default_entry = entries[0]
    profile_ids = [entry["user_id"] for entry in entries]
    if getattr(settings, "NODL_PIN_PER_HUMAN", False):
        has_pin = NodlRegistrationPin.objects.filter(user_id__in=profile_ids).exists()
    else:
        has_pin = NodlRegistrationPin.objects.filter(user_id=default_entry["user_id"]).exists()

    if provisioned_fresh and phone:
        default_profile = UserProfile.objects.get(id=default_entry["user_id"])
        phone_hash = hashlib.sha256(phone.encode("utf-8")).hexdigest()
        mark_invite_registered(phone_hash, default_profile)

    logger.info(
        "NODL_DEBUG: v2 success for sub=%s: %d workspaces, default realm %s",
        supabase_user_id,
        len(entries),
        default_entry["realm_string_id"],
    )
    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "supabase_user_id": supabase_user_id,
            "has_pin": has_pin,
            "is_new_device": not provisioned_fresh,
            "default_workspace_id": default_entry["workspace_id"],
            "workspaces": entries,
        }
    )


@csrf_exempt
def auth_workspaces(request: HttpRequest) -> JsonResponse:
    """GET /nodl/auth/workspaces — the caller's workspace list, sans credentials.

    Authenticated by the standard middleware (api_key Basic auth or Supabase
    JWT); read-only, no provisioning side effects.  Same entry shape as
    bridge v2 minus ``api_key`` — the mobile app polls it for
    inactive-workspace badges (unread_count / last_message_id).
    """
    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "msg": "GET required", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )
    user_profile = getattr(request, "user_profile", None)
    if user_profile is None or not isinstance(user_profile, UserProfile):
        return JsonResponse(
            {"result": "error", "msg": "Authentication required", "code": "UNAUTHORIZED"},
            status=401,
        )

    mapping = NodlRealmUserExtension.objects.filter(zulip_user=user_profile).first()
    mappings = _load_mappings(str(mapping.supabase_user_id)) if mapping else []
    entries = _build_workspace_entries(mappings)
    if not entries:
        # Unmapped caller (pre-backfill data): degrade to their own workspace
        # so the client always sees at least the account it called with.
        own = _build_own_entry(user_profile)
        entries = [own] if own else []
    for entry in entries:
        entry.pop("api_key", None)

    if not entries:
        return JsonResponse({"result": "no_workspace", "msg": "", "code": "NO_WORKSPACE"})

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "default_workspace_id": entries[0]["workspace_id"],
            "workspaces": entries,
        }
    )


def _build_own_entry(user_profile: UserProfile) -> dict | None:
    """Entry for the caller's own realm when no mappings exist yet."""
    extension = NodlRealmExtension.objects.filter(zulip_realm_id=user_profile.realm_id).first()
    if extension is None or user_profile.realm.deactivated:
        return None
    max_message = (
        UserMessage.objects.filter(user_profile=user_profile).aggregate(max_id=Max("message_id"))[
            "max_id"
        ]
        or 0
    )
    unread = (
        UserMessage.objects.filter(user_profile=user_profile)
        .extra(where=[UserMessage.where_unread()])  # noqa: S610
        .count()
    )
    return {
        "workspace_id": str(extension.nodl_workspace_id),
        "name": user_profile.realm.name,
        "realm_string_id": user_profile.realm.string_id,
        "user_id": user_profile.id,
        "email": user_profile.delivery_email,
        "role": ROLE_NAMES.get(user_profile.role, "member"),
        "unread_count": unread,
        "last_message_id": max_message,
        "rank": 0,
    }


def _load_mappings(supabase_user_id: str) -> list[NodlRealmUserExtension]:
    """Load usable mapping rows: active profile in an active nodl realm."""
    try:
        supabase_uuid = uuid.UUID(str(supabase_user_id))
    except (AttributeError, TypeError, ValueError):
        return []
    return list(
        NodlRealmUserExtension.objects.select_related("zulip_user", "zulip_realm")
        .filter(
            supabase_user_id=supabase_uuid,
            zulip_user__is_active=True,
            zulip_realm__deactivated=False,
        )
        .exclude(zulip_realm__string_id="zulipinternal")
    )


def _self_heal_mappings(payload: dict, supabase_user_id: str) -> None:
    """Map unmapped profiles matching the caller's identities (auto-link).

    Identity rule (same as v1 auto-link and the backfill email tier): a
    profile whose delivery email equals one of this Supabase account's
    identities belongs to this human.  record_realm_user_mapping refuses to
    touch profiles claimed by another Supabase user, so a false match can
    never steal a mapped profile.
    """
    try:
        uuid.UUID(str(supabase_user_id))
    except (AttributeError, TypeError, ValueError):
        return

    emails = {derive_email(payload).lower()}
    jwt_email = (payload.get("email") or "").lower()
    if jwt_email:
        emails.add(jwt_email)
    supabase_user = get_supabase_user_by_id(supabase_user_id)
    if supabase_user is not None:
        email_identity = find_email_identity(supabase_user)
        if email_identity:
            emails.add(email_identity.lower())

    nodl_realm_ids = set(
        NodlRealmExtension.objects.exclude(zulip_realm=None).values_list(
            "zulip_realm_id", flat=True
        )
    )
    for email in emails:
        unmapped = UserProfile.objects.select_related("realm").filter(
            delivery_email__iexact=email,
            is_active=True,
            is_bot=False,
            realm__deactivated=False,
            realm_id__in=nodl_realm_ids,
            nodl_realm_user_extension__isnull=True,
        )
        for profile in unmapped:
            record_realm_user_mapping(profile.realm, profile, supabase_user_id)


def _reconcile_cold_start(payload: dict, supabase_user_id: str) -> JsonResponse | bool:
    """Zero mappings: fall back to v1 resolution and provision member realms.

    Returns a JsonResponse to short-circuit with (503 on RPC failure), or a
    bool: True when at least one profile was freshly provisioned.
    """
    workspace_ids = get_user_workspace_ids(supabase_user_id)
    if workspace_ids is None:
        logger.error(
            "NODL_DEBUG: v2 cold-start workspace lookup unavailable for sub=%s",
            supabase_user_id,
        )
        return JsonResponse(
            {
                "result": "error",
                "msg": "Workspace lookup unavailable",
                "code": "SERVICE_UNAVAILABLE",
            },
            status=503,
        )

    email = derive_email(payload)
    ranked = rank_realms_for_user(resolve_candidate_realms(workspace_ids), email)
    if not ranked:
        return False

    provisioned_fresh = False
    for realm in ranked:
        existed = UserProfile.objects.filter(
            delivery_email__iexact=email, realm=realm, is_active=True
        ).exists()
        # Provisions the profile AND records the mapping row.
        get_or_create_zulip_user(payload, realm)
        if not existed:
            provisioned_fresh = True

    logger.info(
        "NODL_DEBUG: v2 cold-start reconcile for sub=%s: provisioned into %d realms",
        supabase_user_id,
        len(ranked),
    )
    return provisioned_fresh


def _build_workspace_entries(mappings: list[NodlRealmUserExtension]) -> list[dict]:
    """Build ranked workspace entries from mapping rows.

    Rank keys mirror workspace_resolution.rank_realms_for_user: message
    recency (globally monotonic ids) -> UserActivity -> date_joined ->
    Realm.id.
    """
    if not mappings:
        return []

    workspace_by_realm = {
        ext.zulip_realm_id: str(ext.nodl_workspace_id)
        for ext in NodlRealmExtension.objects.filter(
            zulip_realm_id__in=[m.zulip_realm_id for m in mappings]
        )
    }

    profile_ids = [m.zulip_user_id for m in mappings]
    max_message_by_profile: dict[int, int] = {
        row["user_profile_id"]: row["max_id"]
        for row in UserMessage.objects.filter(user_profile_id__in=profile_ids)
        .values("user_profile_id")
        .annotate(max_id=Max("message_id"))
    }
    last_visit_by_profile: dict[int, object] = {
        row["user_profile_id"]: row["last"]
        for row in UserActivity.objects.filter(user_profile_id__in=profile_ids)
        .values("user_profile_id")
        .annotate(last=Max("last_visit"))
    }
    unread_by_profile: dict[int, int] = {
        row["user_profile_id"]: row["total"]
        for row in UserMessage.objects.filter(user_profile_id__in=profile_ids)
        .extra(where=[UserMessage.where_unread()])  # noqa: S610
        .values("user_profile_id")
        .annotate(total=Count("id"))
    }

    def sort_key(mapping: NodlRealmUserExtension) -> tuple:
        profile = mapping.zulip_user
        last_visit = last_visit_by_profile.get(profile.id)
        return (
            -max_message_by_profile.get(profile.id, 0),
            -(last_visit.timestamp() if last_visit is not None else 0.0),
            -(profile.date_joined.timestamp() if profile.date_joined else 0.0),
            mapping.zulip_realm_id,
        )

    entries = []
    for rank, mapping in enumerate(sorted(mappings, key=sort_key)):
        realm = mapping.zulip_realm
        profile = mapping.zulip_user
        workspace_id = workspace_by_realm.get(realm.id)
        if workspace_id is None:
            # Mapping in a realm that lost its workspace extension — not a
            # nodl workspace; never expose it to the switcher.
            logger.warning(
                "NODL_DEBUG: v2 mapping %d skipped: realm %d has no workspace extension",
                mapping.id,
                realm.id,
            )
            continue
        entries.append(
            {
                "workspace_id": workspace_id,
                "name": realm.name,
                "realm_string_id": realm.string_id,
                "user_id": profile.id,
                "email": profile.delivery_email,
                "api_key": profile.api_key,
                "role": ROLE_NAMES.get(profile.role, "member"),
                "unread_count": unread_by_profile.get(profile.id, 0),
                "last_message_id": max_message_by_profile.get(profile.id, 0),
                "rank": rank,
            }
        )
    # Re-number ranks in case a skip left a hole.
    for index, entry in enumerate(entries):
        entry["rank"] = index
    return entries
