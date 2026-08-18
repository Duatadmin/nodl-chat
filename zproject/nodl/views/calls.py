import json
import logging
import threading
import uuid
from datetime import timedelta
from functools import wraps
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from zerver.models import UserProfile
from zproject.nodl.models import CallRecord
from zproject.nodl.serializers.call_serializers import (
    serialize_call_accept_response,
    serialize_call_initiate_response,
    serialize_call_record,
)
from zproject.nodl.services.call_push_service import (
    _ensure_firebase_initialized,
    _parse_firebase_json,
    dispatch_call_event_push_async,
    dispatch_call_push,
)
from zproject.nodl.views.webhooks_livekit import insert_call_event_message
from zproject.nodl.services.livekit_service import (
    CALL_ROOM_EMPTY_TIMEOUT,
    CALL_ROOM_MAX_PARTICIPANTS,
    LIVEKIT_URL,
    create_room_sync,
    generate_token,
)

logger = logging.getLogger(__name__)

# A ringing call older than this is treated as dead (client crashed or lost
# network before cancelling) — it must not keep either party "busy" forever.
# Slightly above the 30s client ring timeout + the 35s room empty_timeout.
STALE_RINGING_WINDOW = timedelta(seconds=45)

# Backstop for connected calls whose /end and LiveKit webhooks were BOTH
# lost (e.g. the room was never actually joined). Generous on purpose — the
# webhook path is the real cleanup; this only prevents a permanently-stuck
# "busy" state.
STALE_CONNECTED_WINDOW = timedelta(hours=24)


def _expire_stale_calls(profile_ids: list[int]) -> None:
    """Lazily expire dead call records involving any of the given users.

    Runs inside initiate_call before the busy check, so a stuck record from a
    crashed client can never permanently block new calls.
    """
    now = timezone.now()
    involved = Q(caller_id__in=profile_ids) | Q(callee_id__in=profile_ids)
    CallRecord.objects.filter(
        involved,
        status="ringing",
        initiated_at__lt=now - STALE_RINGING_WINDOW,
    ).update(status="missed", ended_at=now, end_reason="timeout")
    CallRecord.objects.filter(
        involved,
        status="connected",
        answered_at__lt=now - STALE_CONNECTED_WINDOW,
    ).update(status="ended", ended_at=now, end_reason="error")


def _run_call_setup(
    callee_id: int,
    call_id: str,
    room_name: str,
    caller_name: str,
    caller_avatar_url: str,
) -> None:
    """Background half of call initiation: provision the LiveKit room, then
    push-notify the callee's devices.

    Room creation is off the request's critical path (it costs a cross-region
    admin API round trip). The access tokens embed the same room config, so if
    the caller's join wins the race and auto-creates the room, the call
    semantics (empty_timeout, max_participants) still apply and this create
    just returns the existing room.
    """
    try:
        create_room_sync(
            room_name,
            max_participants=CALL_ROOM_MAX_PARTICIPANTS,
            empty_timeout=CALL_ROOM_EMPTY_TIMEOUT,
        )
    except Exception as e:
        # Non-fatal: token-embedded room config lets the first join
        # auto-create the room with the right shape.
        logger.error("LiveKit room creation failed for call %s: %s", call_id, e)

    dispatch_call_push(
        callee_id=callee_id,
        call_id=call_id,
        room_name=room_name,
        caller_name=caller_name,
        caller_avatar_url=caller_avatar_url,
    )


def _start_call_setup_async(
    callee_id: int,
    call_id: str,
    room_name: str,
    caller_name: str,
    caller_avatar_url: str,
) -> None:
    """Fire-and-forget wrapper: spawns _run_call_setup in a daemon thread."""
    thread = threading.Thread(
        target=_run_call_setup,
        args=(callee_id, call_id, room_name, caller_name, caller_avatar_url),
        daemon=True,
    )
    thread.start()
    logger.debug("Call setup thread started for call %s", call_id)


@csrf_exempt
def calls_health(request: HttpRequest) -> HttpResponse:
    """Unauthenticated health check for call push infrastructure."""
    import os
    error_detail = None
    firebase_ok = False
    try:
        firebase_ok = _ensure_firebase_initialized()
    except Exception as e:
        error_detail = str(e)

    if not firebase_ok and not error_detail:
        # Try to get more detail about why it failed
        firebase_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
        if firebase_json:
            try:
                parsed = _parse_firebase_json(firebase_json)
                error_detail = f"JSON parsed OK, keys: {list(parsed.keys())}"
            except Exception as e:
                error_detail = f"Parse error: {e}"

    has_creds_file = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
    has_creds_json = bool(os.environ.get("FIREBASE_CREDENTIALS_JSON", ""))
    resp = {
        "result": "success",
        "firebase_initialized": firebase_ok,
        "has_credentials_file": has_creds_file,
        "has_credentials_json": has_creds_json,
    }
    if error_detail:
        resp["error_detail"] = error_detail
    return JsonResponse(resp)


def _require_jwt_auth(view_func):
    """Require JWT authentication via middleware (request.user_profile)."""
    @csrf_exempt
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = getattr(request, "user_profile", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return JsonResponse(
                {"result": "error", "code": "UNAUTHORIZED", "msg": "Authentication required"},
                status=401,
            )
        return view_func(request, user, *args, **kwargs)
    return wrapper


@_require_jwt_auth
def initiate_call(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    """Initiate a call to another user.

    POST /nodl/calls/initiate
    Body: {"callee_id": <int>}

    Creates a LiveKit room, generates caller token, inserts call_record(status=ringing).
    Dispatches push notifications to callee's devices (fire-and-forget).
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse(
            {"result": "error", "msg": "Invalid JSON", "code": "BAD_REQUEST"},
            status=400,
        )

    raw_callee_id = body.get("callee_id")
    if raw_callee_id is None:
        return JsonResponse(
            {"result": "error", "msg": "callee_id is required", "code": "BAD_REQUEST"},
            status=400,
        )

    # Strict type check: reject bool (subclass of int) and float; allow int and str only
    if isinstance(raw_callee_id, bool) or not isinstance(raw_callee_id, (int, str)):
        return JsonResponse(
            {"result": "error", "msg": "callee_id must be an integer", "code": "BAD_REQUEST"},
            status=400,
        )

    try:
        callee_id = int(raw_callee_id)
    except (ValueError, TypeError):
        return JsonResponse(
            {"result": "error", "msg": "callee_id must be an integer", "code": "BAD_REQUEST"},
            status=400,
        )

    # Validate callee exists — in the caller's own realm. Without the realm
    # scope any authenticated user could ring any user id on the server.
    try:
        callee = UserProfile.objects.get(
            id=callee_id, is_active=True, realm_id=user_profile.realm_id,
        )
    except UserProfile.DoesNotExist:
        return JsonResponse(
            {"result": "error", "msg": "Callee not found", "code": "BAD_REQUEST"},
            status=400,
        )

    # Prevent calling yourself
    if callee.id == user_profile.id:
        return JsonResponse(
            {"result": "error", "msg": "Cannot call yourself", "code": "BAD_REQUEST"},
            status=400,
        )

    # Busy handling: one active call per HUMAN (across sibling workspace
    # profiles — the device fan-out already treats them as one person).
    # Dead records must never block calls, so expire stale ones first.
    from nodl.extensions.mapping import resolve_human_profile_ids

    caller_profile_ids = resolve_human_profile_ids(user_profile.id)
    callee_profile_ids = resolve_human_profile_ids(callee.id)
    _expire_stale_calls(list({*caller_profile_ids, *callee_profile_ids}))

    def _has_active_call(profile_ids: list[int]) -> bool:
        return CallRecord.objects.filter(
            Q(caller_id__in=profile_ids) | Q(callee_id__in=profile_ids),
            status__in=("ringing", "connected"),
        ).exists()

    if _has_active_call(caller_profile_ids):
        return JsonResponse(
            {"result": "error", "msg": "You are already in a call", "code": "CALLER_BUSY"},
            status=409,
        )
    if _has_active_call(callee_profile_ids):
        return JsonResponse(
            {"result": "error", "msg": "User is on another call", "code": "CALLEE_BUSY"},
            status=409,
        )

    # Generate the caller's token now (local JWT signing — cheap); the LiveKit
    # room itself is provisioned in the background thread below, off the
    # response's critical path.
    room_name = f"call-{uuid.uuid4()}"
    caller_identity = str(user_profile.id)

    try:
        token = generate_token(caller_identity, room_name)
    except Exception as e:
        logger.error("LiveKit token generation failed: %s", e)
        return JsonResponse(
            {"result": "error", "msg": "Call service unavailable", "code": "SERVICE_ERROR"},
            status=503,
        )

    call = CallRecord.objects.create(
        room_name=room_name,
        caller=user_profile,
        callee=callee,
        status="ringing",
    )

    # Background: provision the LiveKit room, then push-notify the callee's
    # devices (Story 11.3).
    caller_name = user_profile.full_name or user_profile.delivery_email
    caller_avatar_url = ""
    _start_call_setup_async(
        callee_id=callee.id,
        call_id=str(call.id),
        room_name=room_name,
        caller_name=caller_name,
        caller_avatar_url=caller_avatar_url,
    )

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            **serialize_call_initiate_response(call, room_name, LIVEKIT_URL, token),
        }
    )


@_require_jwt_auth
def accept_call(
    request: HttpRequest, user_profile: UserProfile, call_id: str
) -> HttpResponse:
    """Accept an incoming call.

    POST /nodl/calls/<call_id>/accept

    Transitions status ringing → connected, sets answered_at, returns callee LiveKit token.
    First accept wins (multi-device); subsequent attempts get error.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        call_uuid = uuid.UUID(str(call_id))
    except ValueError:
        return JsonResponse(
            {"result": "error", "msg": "Invalid call_id", "code": "BAD_REQUEST"},
            status=400,
        )

    with transaction.atomic():
        try:
            call = CallRecord.objects.select_for_update().get(id=call_uuid)
        except CallRecord.DoesNotExist:
            return JsonResponse(
                {"result": "error", "msg": "Call not found", "code": "NOT_FOUND"},
                status=404,
            )

        # Only callee can accept
        if call.callee_id != user_profile.id:
            return JsonResponse(
                {"result": "error", "msg": "Not authorized", "code": "UNAUTHORIZED"},
                status=403,
            )

        # Must be in ringing state
        if call.status != "ringing":
            return JsonResponse(
                {
                    "result": "error",
                    "msg": f"Call cannot be accepted (status: {call.status})",
                    "code": "INVALID_STATE",
                },
                status=409,
            )

        # Generate token BEFORE persisting state change — if this fails,
        # the transaction rolls back and call stays in "ringing" state.
        callee_identity = str(user_profile.id)
        try:
            token = generate_token(callee_identity, call.room_name)
        except Exception as e:
            logger.error("LiveKit token generation failed: %s", e)
            return JsonResponse(
                {"result": "error", "msg": "Call service unavailable", "code": "SERVICE_ERROR"},
                status=503,
            )

        call.status = "connected"
        call.answered_at = timezone.now()
        call.save(update_fields=["status", "answered_at"])

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            **serialize_call_accept_response(call, call.room_name, LIVEKIT_URL, token),
        }
    )


@_require_jwt_auth
def decline_call(
    request: HttpRequest, user_profile: UserProfile, call_id: str
) -> HttpResponse:
    """Decline an incoming call.

    POST /nodl/calls/<call_id>/decline

    Transitions status ringing → declined, sets ended_at + end_reason=callee_declined.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        call_uuid = uuid.UUID(str(call_id))
    except ValueError:
        return JsonResponse(
            {"result": "error", "msg": "Invalid call_id", "code": "BAD_REQUEST"},
            status=400,
        )

    with transaction.atomic():
        try:
            call = CallRecord.objects.select_for_update().get(id=call_uuid)
        except CallRecord.DoesNotExist:
            return JsonResponse(
                {"result": "error", "msg": "Call not found", "code": "NOT_FOUND"},
                status=404,
            )

        # Only callee can decline
        if call.callee_id != user_profile.id:
            return JsonResponse(
                {"result": "error", "msg": "Not authorized", "code": "UNAUTHORIZED"},
                status=403,
            )

        if call.status != "ringing":
            return JsonResponse(
                {
                    "result": "error",
                    "msg": f"Call cannot be declined (status: {call.status})",
                    "code": "INVALID_STATE",
                },
                status=409,
            )

        call.status = "declined"
        call.ended_at = timezone.now()
        call.end_reason = "callee_declined"
        call.save(update_fields=["status", "ended_at", "end_reason"])

    # Tell the caller's devices immediately — without this the caller rings
    # the full 30s timeout with no idea the callee declined.
    dispatch_call_event_push_async(call.caller_id, "call_declined", str(call.id))

    # Insert DM event message (best-effort — never break the success response)
    insert_call_event_message(call, "declined")

    return JsonResponse({"result": "success", "msg": ""})


@_require_jwt_auth
def cancel_call(
    request: HttpRequest, user_profile: UserProfile, call_id: str
) -> HttpResponse:
    """Cancel an outgoing call before it's answered.

    POST /nodl/calls/<call_id>/cancel

    Only the caller can cancel. Transitions ringing → cancelled.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        call_uuid = uuid.UUID(str(call_id))
    except ValueError:
        return JsonResponse(
            {"result": "error", "msg": "Invalid call_id", "code": "BAD_REQUEST"},
            status=400,
        )

    with transaction.atomic():
        try:
            call = CallRecord.objects.select_for_update().get(id=call_uuid)
        except CallRecord.DoesNotExist:
            return JsonResponse(
                {"result": "error", "msg": "Call not found", "code": "NOT_FOUND"},
                status=404,
            )

        # Only caller can cancel
        if call.caller_id != user_profile.id:
            return JsonResponse(
                {"result": "error", "msg": "Not authorized", "code": "UNAUTHORIZED"},
                status=403,
            )

        if call.status != "ringing":
            return JsonResponse(
                {
                    "result": "error",
                    "msg": f"Call cannot be cancelled (status: {call.status})",
                    "code": "INVALID_STATE",
                },
                status=409,
            )

        call.status = "cancelled"
        call.ended_at = timezone.now()
        call.end_reason = "caller_cancelled"
        call.save(update_fields=["status", "ended_at", "end_reason"])

    # Dismiss the callee's ringing UI immediately — without this their phone
    # keeps ringing for a call that no longer exists ("ghost ringing").
    dispatch_call_event_push_async(call.callee_id, "call_cancelled", str(call.id))

    # Insert DM event message (best-effort — never break the success response)
    insert_call_event_message(call, "cancelled")

    return JsonResponse({"result": "success", "msg": ""})


@_require_jwt_auth
def end_call(
    request: HttpRequest, user_profile: UserProfile, call_id: str
) -> HttpResponse:
    """End a connected call.

    POST /nodl/calls/<call_id>/end

    Transitions connected → ended, computes duration_seconds. Idempotent —
    second simultaneous /end returns 200 OK.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        call_uuid = uuid.UUID(str(call_id))
    except ValueError:
        return JsonResponse(
            {"result": "error", "msg": "Invalid call_id", "code": "BAD_REQUEST"},
            status=400,
        )

    with transaction.atomic():
        try:
            call = CallRecord.objects.select_for_update().get(id=call_uuid)
        except CallRecord.DoesNotExist:
            return JsonResponse(
                {"result": "error", "msg": "Call not found", "code": "NOT_FOUND"},
                status=404,
            )

        # Only caller or callee can end
        if call.caller_id != user_profile.id and call.callee_id != user_profile.id:
            return JsonResponse(
                {"result": "error", "msg": "Not authorized", "code": "UNAUTHORIZED"},
                status=403,
            )

        # Idempotent: if already ended, return success
        if call.status == "ended":
            return JsonResponse({"result": "success", "msg": ""})

        if call.status != "connected":
            return JsonResponse(
                {
                    "result": "error",
                    "msg": f"Call cannot be ended (status: {call.status})",
                    "code": "INVALID_STATE",
                },
                status=409,
            )

        now = timezone.now()
        duration = None
        if call.answered_at:
            duration = int((now - call.answered_at).total_seconds())

        # Determine who hung up
        if call.caller_id == user_profile.id:
            end_reason = "caller_hangup"
        else:
            end_reason = "callee_hangup"

        call.status = "ended"
        call.ended_at = now
        call.duration_seconds = duration
        call.end_reason = end_reason
        call.save(update_fields=["status", "ended_at", "duration_seconds", "end_reason"])

    # Backstop signal to the other party (LiveKit participant-left events are
    # the primary in-call path, but they miss e.g. never-joined edge cases).
    other_party_id = (
        call.callee_id if call.caller_id == user_profile.id else call.caller_id
    )
    dispatch_call_event_push_async(other_party_id, "call_ended", str(call.id))

    # Insert the ended-call DM entry (WhatsApp/Telegram parity: a completed
    # call shows in the thread with its duration). Only this request performed
    # the connected→ended transition (idempotent early-return above), so the
    # event is posted exactly once even with simultaneous /end + webhook.
    insert_call_event_message(call, "ended", duration_seconds=duration)

    return JsonResponse({"result": "success", "msg": ""})


@_require_jwt_auth
def call_history(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    """Get paginated call history for the authenticated user.

    GET /nodl/calls/history?limit=20&offset=0

    Returns calls where user is caller OR callee, newest first.
    """
    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        limit = int(request.GET.get("limit", "20"))
        offset = int(request.GET.get("offset", "0"))
    except (ValueError, TypeError):
        return JsonResponse(
            {"result": "error", "msg": "Invalid limit/offset", "code": "BAD_REQUEST"},
            status=400,
        )

    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)

    calls = (
        CallRecord.objects.filter(
            Q(caller=user_profile) | Q(callee=user_profile)
        )
        .select_related("caller", "callee")
        .order_by("-initiated_at")[offset : offset + limit]
    )

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "calls": [
                serialize_call_record(c, requesting_user_id=user_profile.id)
                for c in calls
            ],
        }
    )


@_require_jwt_auth
def call_detail(
    request: HttpRequest, user_profile: UserProfile, call_id: str
) -> HttpResponse:
    """Get a single call record.

    GET /nodl/calls/<call_id>

    Only returns the record if the authenticated user is caller or callee.
    """
    if request.method != "GET":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed", "code": "METHOD_NOT_ALLOWED"},
            status=405,
        )

    try:
        call_uuid = uuid.UUID(str(call_id))
    except ValueError:
        return JsonResponse(
            {"result": "error", "msg": "Invalid call_id", "code": "BAD_REQUEST"},
            status=400,
        )

    try:
        call = CallRecord.objects.select_related("caller", "callee").get(
            id=call_uuid,
        )
    except CallRecord.DoesNotExist:
        return JsonResponse(
            {"result": "error", "msg": "Call not found", "code": "NOT_FOUND"},
            status=404,
        )

    # Only caller or callee can view
    if call.caller_id != user_profile.id and call.callee_id != user_profile.id:
        return JsonResponse(
            {"result": "error", "msg": "Not authorized", "code": "UNAUTHORIZED"},
            status=403,
        )

    return JsonResponse(
        {
            "result": "success",
            "msg": "",
            "call": serialize_call_record(call, requesting_user_id=user_profile.id),
        }
    )
