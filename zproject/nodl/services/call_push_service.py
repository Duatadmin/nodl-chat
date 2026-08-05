import base64
import json
import logging
import os
import threading

import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin.exceptions import NotFoundError
from firebase_admin.messaging import UnregisteredError

logger = logging.getLogger(__name__)

# ---------- APNs config (iOS VoIP push via aioapns) ----------
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_AUTH_KEY_PATH = os.environ.get("APNS_AUTH_KEY_PATH", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "")
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "true").lower() == "true"

# ---------- FCM config (Android data message) ----------
# firebase-admin initializes from GOOGLE_APPLICATION_CREDENTIALS env var
# or from explicit credentials. We initialize lazily on first use.
_firebase_init_lock = threading.Lock()


def _parse_firebase_json(raw: str) -> dict:
    """Parse Firebase credentials JSON, handling Railway env var quirks.

    Railway may mangle the JSON in several ways:
    - Convert \\n escapes to real newlines (breaks json.loads)
    - Add surrounding quotes

    As a bulletproof fallback, also supports base64-encoded JSON.
    """
    # Strip surrounding quotes if Railway wrapped the value
    stripped = raw.strip().strip('"').strip("'")

    # Try 1: Standard JSON parse
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try 2: Base64-encoded JSON
    try:
        decoded = base64.b64decode(stripped).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        pass

    # Try 3: Re-escape newlines that Railway converted to real ones
    try:
        fixed = stripped.replace("\r", "").replace("\n", "\\n")
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        pass

    raise ValueError("Cannot parse FIREBASE_CREDENTIALS_JSON — tried raw, base64, and newline-fix")


def _ensure_firebase_initialized() -> bool:
    """Lazily initialize the Firebase Admin SDK (thread-safe). Returns True if initialized."""
    # Fast path: check if already initialized without acquiring lock
    try:
        firebase_admin.get_app()
        return True
    except ValueError:
        pass

    with _firebase_init_lock:
        # Double-check after acquiring lock
        try:
            firebase_admin.get_app()
            return True
        except ValueError:
            pass

        # Option 1: Standard file path via GOOGLE_APPLICATION_CREDENTIALS
        google_creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if google_creds_path:
            try:
                cred = credentials.Certificate(google_creds_path)
                firebase_admin.initialize_app(cred)
                logger.info("Firebase initialized from GOOGLE_APPLICATION_CREDENTIALS")
                return True
            except Exception as e:
                logger.error("Firebase init from file failed: %s", e)
                return False

        # Option 2: Inline JSON via FIREBASE_CREDENTIALS_JSON (for Railway/containers)
        # Supports raw JSON, newline-mangled JSON, or base64-encoded JSON.
        firebase_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
        if firebase_json:
            try:
                cred_dict = _parse_firebase_json(firebase_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                logger.info("Firebase initialized from FIREBASE_CREDENTIALS_JSON")
                return True
            except Exception as e:
                logger.error("Firebase init from JSON env var failed: %s", e)
                return False

        logger.warning("No Firebase credentials configured — FCM push disabled")
        return False


def send_voip_push_ios(
    voip_token: str,
    call_id: str,
    room_name: str,
    caller_name: str,
    caller_avatar_url: str,
    *,
    recipient_user_id: str = "",
    recipient_realm_url: str = "",
    recipient_workspace_id: str = "",
) -> bool:
    """Send an APNs VoIP push notification to an iOS device.

    Uses aioapns for HTTP/2 APNs transport. The push topic is
    <bundle_id>.voip as required by Apple PushKit.

    Returns True on success, False on failure. Never raises.
    """
    if not all([APNS_KEY_ID, APNS_TEAM_ID, APNS_AUTH_KEY_PATH, APNS_BUNDLE_ID]):
        logger.warning("APNs credentials not configured — skipping iOS VoIP push")
        return False

    try:
        from aioapns import APNs, NotificationRequest
        from asgiref.sync import async_to_sync

        with open(APNS_AUTH_KEY_PATH) as f:
            apns_key_content = f.read()

        client = APNs(
            key=apns_key_content,
            key_id=APNS_KEY_ID,
            team_id=APNS_TEAM_ID,
            topic=f"{APNS_BUNDLE_ID}.voip",
            use_sandbox=APNS_USE_SANDBOX,
        )

        payload = {
            "call_id": call_id,
            "room_name": room_name,
            "caller_name": caller_name,
            "caller_avatar_url": caller_avatar_url,
            # Which local account should take this call — the token may have
            # been registered under a sibling profile of the same human.
            "recipient_user_id": recipient_user_id,
            "recipient_realm_url": recipient_realm_url,
            "recipient_workspace_id": recipient_workspace_id,
        }

        request = NotificationRequest(
            device_token=voip_token,
            message=payload,
        )

        result = async_to_sync(client.send_notification)(request)

        if not result.is_successful:
            logger.warning(
                "APNs VoIP push failed for token %s...: %s",
                voip_token[:16],
                result.description,
            )
            return False

        logger.info("APNs VoIP push sent for call %s", call_id)
        return True

    except Exception as e:
        logger.error("APNs VoIP push error: %s", e)
        return False


def send_fcm_call_data(
    fcm_token: str,
    call_id: str,
    room_name: str,
    caller_name: str,
    caller_avatar_url: str,
    *,
    recipient_user_id: str = "",
    recipient_realm_url: str = "",
    recipient_workspace_id: str = "",
) -> str:
    """Send an FCM high-priority DATA message to an Android device.

    Uses firebase-admin SDK. This is a DATA message (not notification)
    so the client app controls display even when backgrounded.

    Returns:
        "sent" on success,
        "unregistered" if the token is stale/invalid (caller should clean up),
        "error" on other failures.
    Never raises.
    """
    if not _ensure_firebase_initialized():
        logger.warning("Firebase not initialized — skipping FCM push")
        return "error"

    try:
        message = messaging.Message(
            data={
                "type": "incoming_call",
                "call_id": call_id,
                "room_name": room_name,
                "caller_name": caller_name,
                "caller_avatar_url": caller_avatar_url,
                "recipient_user_id": recipient_user_id,
                "recipient_realm_url": recipient_realm_url,
                "recipient_workspace_id": recipient_workspace_id,
            },
            token=fcm_token,
            android=messaging.AndroidConfig(
                priority="high",
            ),
        )

        response = messaging.send(message)
        logger.info("FCM data message sent for call %s: %s", call_id, response)
        return "sent"

    except (UnregisteredError, NotFoundError) as e:
        # Firebase best practice: token is stale — caller should deactivate it.
        logger.warning("FCM token %s... is unregistered/not-found: %s", fcm_token[:16], e)
        return "unregistered"

    except Exception as e:
        logger.error("FCM push error for token %s...: %s", fcm_token[:16], e)
        return "error"


def dispatch_call_push(
    callee_id: int,
    call_id: str,
    room_name: str,
    caller_name: str,
    caller_avatar_url: str,
) -> None:
    """Dispatch incoming call push notifications to ALL devices of the callee HUMAN.

    One human holds one Zulip profile per workspace realm, and a device may
    have registered its push tokens under any of those sibling profiles —
    so token lookup fans out over resolve_human_profile_ids(callee_id) and
    dedupes by token value so each physical device rings exactly once.
    The payload carries the callee profile's identity (recipient_user_id /
    realm URL / workspace id) so the app knows which local account must
    accept the call.

    Queries DeviceVoipToken for active tokens, sends platform-appropriate
    push to each. For Android, if all DeviceVoipToken FCM sends fail,
    falls back to Zulip's PushDeviceToken (always kept current by the
    GCM registration flow).

    Stale tokens that return UNREGISTERED/NOT_FOUND are deactivated
    immediately per Firebase best practices.

    Errors are logged but never raised — this is designed to run in a
    fire-and-forget thread.
    """
    # Import here to avoid circular imports at module level
    from django.conf import settings

    from nodl.extensions.mapping import resolve_human_profile_ids
    from nodl.extensions.models import NodlRealmExtension
    from zerver.models import PushDeviceToken, UserProfile
    from zproject.nodl.models import DeviceVoipToken

    try:
        # Recipient identity for the payload: the app's accounts all live on
        # the bare root host (per-realm subdomain URLs are never used by
        # mobile), and Zulip user ids are server-global — so ROOT_DOMAIN_URI
        # plus the callee profile id identifies the target account.
        recipient_workspace_id = ""
        callee_realm_id = (
            UserProfile.objects.filter(id=callee_id)
            .values_list("realm_id", flat=True)
            .first()
        )
        if callee_realm_id is not None:
            realm_ext = (
                NodlRealmExtension.objects.filter(zulip_realm_id=callee_realm_id)
                .only("nodl_workspace_id")
                .first()
            )
            if realm_ext is not None:
                recipient_workspace_id = str(realm_ext.nodl_workspace_id)

        recipient = {
            "recipient_user_id": str(callee_id),
            "recipient_realm_url": settings.ROOT_DOMAIN_URI,
            "recipient_workspace_id": recipient_workspace_id,
        }

        profile_ids = resolve_human_profile_ids(callee_id)

        tokens = list(
            DeviceVoipToken.objects.filter(
                user_id__in=profile_ids,
                is_active=True,
            ).values("platform", "voip_token", "fcm_token", "device_id")
        )

        if not tokens:
            logger.info(
                "No active device tokens for callee %s (%d sibling profiles) — push skipped",
                callee_id, len(profile_ids),
            )
            # Even with no DeviceVoipToken, try the PushDeviceToken fallback below.

        any_android_success = False
        tried_voip_tokens: set[str] = set()
        tried_fcm_tokens: set[str] = set()

        for token_record in tokens:
            platform = token_record["platform"]
            device_id = token_record["device_id"]

            if platform == "ios":
                voip_token = token_record.get("voip_token")
                if not voip_token:
                    logger.warning("iOS device %s has no voip_token — skipping", device_id)
                    continue
                if voip_token in tried_voip_tokens:
                    continue  # same device registered under a sibling profile
                tried_voip_tokens.add(voip_token)
                send_voip_push_ios(
                    voip_token, call_id, room_name, caller_name, caller_avatar_url,
                    **recipient,
                )

            elif platform == "android":
                fcm_token = token_record.get("fcm_token")
                if not fcm_token:
                    logger.warning("Android device %s has no fcm_token — skipping", device_id)
                    continue
                if fcm_token in tried_fcm_tokens:
                    continue  # same device registered under a sibling profile
                tried_fcm_tokens.add(fcm_token)
                result = send_fcm_call_data(
                    fcm_token, call_id, room_name, caller_name, caller_avatar_url,
                    **recipient,
                )
                if result == "sent":
                    any_android_success = True
                elif result == "unregistered":
                    # Firebase best practice: deactivate stale token immediately —
                    # every row carrying it, whichever sibling profile owns it.
                    DeviceVoipToken.objects.filter(
                        user_id__in=profile_ids, fcm_token=fcm_token,
                    ).update(is_active=False)
                    logger.info("Deactivated stale DeviceVoipToken for device %s", device_id)

            else:
                logger.warning(
                    "Unknown platform '%s' for device %s — skipping", platform, device_id,
                )

        # Fallback: if no DeviceVoipToken succeeded for Android, try Zulip's
        # PushDeviceToken which is always current from the GCM registration flow.
        if not any_android_success:
            zulip_fcm_tokens = list(
                PushDeviceToken.objects.filter(
                    user_id__in=profile_ids,
                    kind=PushDeviceToken.FCM,
                ).values_list("token", flat=True)
            )
            if zulip_fcm_tokens:
                logger.info(
                    "No Android DeviceVoipToken succeeded for user %s — "
                    "trying %d PushDeviceToken(s) as fallback",
                    callee_id, len(zulip_fcm_tokens),
                )
            for fallback_token in zulip_fcm_tokens:
                if fallback_token in tried_fcm_tokens:
                    continue  # already tried (and failed) via DeviceVoipToken
                tried_fcm_tokens.add(fallback_token)
                result = send_fcm_call_data(
                    fallback_token, call_id, room_name, caller_name, caller_avatar_url,
                    **recipient,
                )
                if result == "sent":
                    logger.info("FCM sent via PushDeviceToken fallback for user %s", callee_id)
                    break

    except Exception as e:
        logger.error("Push dispatch failed for callee %s: %s", callee_id, e)


def dispatch_call_push_async(
    callee_id: int,
    call_id: str,
    room_name: str,
    caller_name: str,
    caller_avatar_url: str,
) -> None:
    """Fire-and-forget wrapper: spawns dispatch_call_push in a daemon thread.

    This is called from initiate_call view so the endpoint returns
    immediately without waiting for push delivery.
    """
    thread = threading.Thread(
        target=dispatch_call_push,
        args=(callee_id, call_id, room_name, caller_name, caller_avatar_url),
        daemon=True,
    )
    thread.start()
    logger.debug("Push dispatch thread started for call %s", call_id)
