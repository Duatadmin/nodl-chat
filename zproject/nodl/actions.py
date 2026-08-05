import logging
import re
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError

from zerver.actions.create_user import do_create_user
from zerver.models import Realm, UserProfile

logger = logging.getLogger(__name__)

# Rate limit: max 5 link attempts per phone number per hour
LINK_RATE_LIMIT = 5
LINK_RATE_WINDOW = 3600  # seconds

# E.164 phone number format: + followed by 1-15 digits, first digit non-zero
E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")

# Cache lock TTL for phone linking TOCTOU protection (seconds)
PHONE_LINK_LOCK_TTL = 30


def validate_e164_phone(phone: str) -> bool:
    """Return True if *phone* matches the E.164 format."""
    return bool(E164_PATTERN.match(phone))


def mask_email(email: str) -> str:
    """Mask an email address for display, showing first char + domain.

    Examples: marcus@example.com -> m***@example.com, a@b.com -> a***@b.com
    """
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def get_supabase_admin_headers() -> dict[str, str]:
    """Return headers for Supabase Admin API requests."""
    service_role_key = getattr(settings, "NODL_SUPABASE_SERVICE_ROLE_KEY", "")
    return {
        "Authorization": f"Bearer {service_role_key}",
        "apikey": service_role_key,
        "Content-Type": "application/json",
    }


def get_supabase_user_by_id(supabase_user_id: str) -> dict[str, Any] | None:
    """Fetch a Supabase user by their UUID using the Admin API."""
    supabase_url = getattr(settings, "NODL_SUPABASE_URL", "")
    if not supabase_url:
        logger.error("NODL_SUPABASE_URL is not configured")
        return None

    url = f"{supabase_url.rstrip('/')}/auth/v1/admin/users/{supabase_user_id}"
    try:
        resp = requests.get(url, headers=get_supabase_admin_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "Supabase admin API returned %d for user %s",
            resp.status_code,
            supabase_user_id,
        )
        return None
    except requests.RequestException:
        logger.exception("Failed to fetch Supabase user %s", supabase_user_id)
        return None


def get_supabase_user_by_email(email: str) -> dict[str, Any] | None:
    """Look up a Supabase user by email via the Admin list-users API.

    Returns the first user whose email matches, or None.
    """
    supabase_url = getattr(settings, "NODL_SUPABASE_URL", "")
    if not supabase_url:
        logger.error("NODL_SUPABASE_URL is not configured")
        return None

    url = f"{supabase_url.rstrip('/')}/auth/v1/admin/users"
    try:
        resp = requests.get(
            url,
            headers=get_supabase_admin_headers(),
            params={"email": email, "per_page": "5"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "Supabase admin API returned %d looking up email %s",
                resp.status_code,
                email,
            )
            return None
        data = resp.json()
        users = data.get("users", data) if isinstance(data, dict) else data
        if not isinstance(users, list):
            return None
        for user in users:
            if isinstance(user, dict) and user.get("email", "").lower() == email.lower():
                return user
        return None
    except requests.RequestException:
        logger.exception("Failed to look up Supabase user by email %s", email)
        return None


def get_user_workspace_ids(supabase_user_id: str) -> list[str] | None:
    """Query workspace IDs via Supabase RPC function (SECURITY DEFINER).

    The RPC's EXECUTE was revoked from anon/authenticated/public by
    nodl-backend migration 20260505_security_advisor_fixes.sql, so this only
    works with the service_role key (NODL_SUPABASE_SERVICE_ROLE_KEY).

    Returns:
        A list of workspace UUID strings — possibly empty, meaning the user
        genuinely has no workspace membership — or None when the RPC could
        not be consulted (missing config, non-200, network failure).  Callers
        must treat None as "unknown", never as "no workspaces".
    """
    supabase_url = getattr(settings, "NODL_SUPABASE_URL", "")
    if not supabase_url:
        logger.error("NODL_SUPABASE_URL is not configured; cannot resolve workspaces")
        return None
    url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/get_user_workspace_ids"
    try:
        resp = requests.post(
            url,
            headers=get_supabase_admin_headers(),
            json={"uid": supabase_user_id},
            timeout=5,
        )
        if resp.status_code == 200:
            # RETURNS SETOF uuid → flat array of UUID strings
            data = resp.json()
            if isinstance(data, list):
                return [str(ws_id) for ws_id in data]
            logger.error(
                "Supabase RPC get_user_workspace_ids returned unexpected shape %s for user %s",
                type(data).__name__,
                supabase_user_id,
            )
            return None
        logger.error(
            "Supabase RPC get_user_workspace_ids returned %d for user %s"
            " (401 here almost always means SUPABASE_KEY is not the service_role key)",
            resp.status_code,
            supabase_user_id,
        )
        return None
    except requests.RequestException:
        logger.exception("Failed to query workspace_ids for user %s", supabase_user_id)
        return None


def find_email_identity(supabase_user: dict[str, Any]) -> str | None:
    """Check if a Supabase user has an email identity and return the email."""
    identities = supabase_user.get("identities", [])
    if not isinstance(identities, list):
        return None
    for identity in identities:
        if isinstance(identity, dict) and identity.get("provider") == "email":
            identity_data = identity.get("identity_data", {})
            if isinstance(identity_data, dict):
                email = identity_data.get("email", "")
                if email:
                    return email
    return None


def find_existing_zulip_user_by_email(email: str, realm: Realm) -> UserProfile | None:
    """Look up an active Zulip user by email."""
    try:
        return UserProfile.objects.get(
            delivery_email__iexact=email,
            realm=realm,
            is_active=True,
        )
    except UserProfile.DoesNotExist:
        return None


def check_duplicate_phone(supabase_user_id: str, phone: str) -> bool:
    """Check if a phone number is already linked to a different Supabase user.

    GoTrue enforces phone uniqueness (an OTP to an existing phone logs into
    that user rather than creating a new one), so a genuine cross-user
    duplicate is structurally impossible; this remains as a belt-and-braces
    guard only.

    The admin list-users endpoint ignores a ``phone`` query param and this
    project's GoTrue version doesn't match phones via ``filter`` either, so
    the results MUST be compared by actual phone value — treating "any other
    user came back" as a duplicate flags every registration once the admin
    key works (it silently 401'd on the anon key for months).
    """
    supabase_url = getattr(settings, "NODL_SUPABASE_URL", "")
    if not supabase_url:
        return False

    target = re.sub(r"\D", "", phone)
    if not target:
        return False

    url = f"{supabase_url.rstrip('/')}/auth/v1/admin/users"
    try:
        resp = requests.get(
            url,
            headers=get_supabase_admin_headers(),
            params={"filter": phone, "per_page": "10"},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        users = data.get("users", data) if isinstance(data, dict) else data
        if not isinstance(users, list):
            return False
        for user in users:
            if (
                isinstance(user, dict)
                and user.get("id") != supabase_user_id
                and re.sub(r"\D", "", user.get("phone") or "") == target
            ):
                logger.warning(
                    "Duplicate phone across Supabase users: %s also on user %s",
                    phone,
                    user.get("id"),
                )
                return True
        return False
    except requests.RequestException:
        logger.exception("Failed to check duplicate phone %s", phone)
        return False


def check_link_rate_limit(phone: str) -> bool:
    """Check if link attempts for a phone number exceed the rate limit.

    Returns True if rate limited.
    """
    cache_key = f"nodl_link_attempt:{phone}"
    count = cache.get(cache_key, 0)
    if count >= LINK_RATE_LIMIT:
        return True
    if count == 0:
        cache.set(cache_key, 1, timeout=LINK_RATE_WINDOW)
    else:
        try:
            cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, count + 1, timeout=LINK_RATE_WINDOW)
    return False


def acquire_phone_link_lock(phone: str) -> bool:
    """Acquire a cache-based lock for phone linking to prevent TOCTOU races.

    Returns True if the lock was acquired, False if already held.
    Uses cache.add() which is atomic -- only succeeds if the key does not exist.
    """
    cache_key = f"nodl_link_lock:{phone}"
    return cache.add(cache_key, "1", timeout=PHONE_LINK_LOCK_TTL)


def release_phone_link_lock(phone: str) -> None:
    """Release the cache-based phone linking lock."""
    cache_key = f"nodl_link_lock:{phone}"
    cache.delete(cache_key)


def link_phone_to_existing_user(existing_supabase_user_id: str, phone: str) -> bool:
    """Link a phone number to an existing Supabase user via Admin API.

    Uses updateUserById to add the phone identity to the existing email user.
    Returns True on success.
    """
    supabase_url = getattr(settings, "NODL_SUPABASE_URL", "")
    if not supabase_url:
        logger.error("NODL_SUPABASE_URL is not configured for linking")
        return False

    url = f"{supabase_url.rstrip('/')}/auth/v1/admin/users/{existing_supabase_user_id}"
    try:
        resp = requests.put(
            url,
            headers=get_supabase_admin_headers(),
            json={"phone": phone, "phone_confirm": True},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(
                "Linked phone %s to Supabase user %s",
                phone,
                existing_supabase_user_id,
            )
            return True
        logger.warning(
            "Supabase admin API link returned %d for user %s",
            resp.status_code,
            existing_supabase_user_id,
        )
        return False
    except requests.RequestException:
        logger.exception(
            "Failed to link phone %s to Supabase user %s",
            phone,
            existing_supabase_user_id,
        )
        return False


def derive_email(supabase_payload: dict) -> str:
    """Derive a Zulip email from Supabase JWT claims.

    If the JWT has an email claim, use it directly.
    For phone-only users, derive email as {phone}@nodl.local.
    """
    email = supabase_payload.get("email", "")
    if email:
        return email
    phone = supabase_payload.get("phone", "")
    if phone:
        return f"{phone}@nodl.local"
    # Fallback to sub UUID
    return f"{supabase_payload['sub']}@nodl.local"


def derive_full_name(supabase_payload: dict) -> str:
    """Derive a display name from Supabase JWT claims."""
    user_metadata = supabase_payload.get("user_metadata", {})
    if isinstance(user_metadata, dict):
        name = user_metadata.get("full_name", "") or user_metadata.get("name", "")
        if name:
            return name
    phone = supabase_payload.get("phone", "")
    if phone:
        return phone
    return "nodl user"


def get_or_create_zulip_user(supabase_payload: dict, realm: Realm) -> UserProfile:
    """Look up or provision a Zulip user from Supabase JWT claims.

    Args:
        supabase_payload: Decoded Supabase JWT payload.
        realm: The Zulip realm to create the user in.

    Returns:
        The existing or newly created UserProfile.
    """
    email = derive_email(supabase_payload)

    # Try to find existing user
    try:
        return UserProfile.objects.get(
            delivery_email__iexact=email,
            realm=realm,
            is_active=True,
        )
    except UserProfile.DoesNotExist:
        pass

    # User does not exist - create one
    full_name = derive_full_name(supabase_payload)

    try:
        user_profile = do_create_user(
            email=email,
            password=None,
            realm=realm,
            full_name=full_name,
            acting_user=None,
        )
        logger.info(
            "Created Zulip user %s (id=%d) for Supabase user %s",
            email,
            user_profile.id,
            supabase_payload.get("sub", "unknown"),
        )
        return user_profile
    except IntegrityError:
        # Race condition: another request created the user concurrently
        logger.info("Race condition on user creation for %s, fetching existing", email)
        return UserProfile.objects.get(
            delivery_email__iexact=email,
            realm=realm,
        )
