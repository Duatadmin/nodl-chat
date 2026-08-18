import logging
import os

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from livekit.api import TokenVerifier, WebhookReceiver

from nodl.api.views.card_schemas import build_card_message_content
from zerver.actions.message_flags import do_update_message_flags
from zerver.actions.message_send import internal_send_private_message
from zerver.models import UserProfile
from zproject.nodl.models import CallRecord

logger = logging.getLogger(__name__)

# Human-readable fallback per call-event status — what legacy clients, web,
# push notifications, and DM-list previews show (the card marker precedes it).
CALL_EVENT_FALLBACK = {
    "missed": "📞 Missed voice call",
    "declined": "📞 Voice call declined",
    "cancelled": "📞 Missed voice call",
    "ended": "📞 Voice call",
}

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")


def _get_webhook_receiver() -> WebhookReceiver | None:
    """Create a WebhookReceiver with current credentials. Returns None if unconfigured."""
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        logger.error("LiveKit webhook credentials not configured")
        return None
    return WebhookReceiver(TokenVerifier(LIVEKIT_API_KEY, LIVEKIT_API_SECRET))


def insert_call_event_message(
    call: CallRecord,
    status: str,
    *,
    duration_seconds: int | None = None,
) -> None:
    """Insert a call-event message into the caller↔callee 1:1 DM thread.

    Sender is the CALLER (the human whose action the event describes) — a bot
    sender is structurally impossible here: Zulip always adds the sender to
    the recipient set, so bot + caller + callee would become a separate
    3-party group DM instead of the existing 1:1 (the pre-2026-08-18 bug that
    put every call in the chats list as its own conversation).

    Content is a `nodl-card:v1` call_event card plus a human-readable
    fallback; clients that know the card render a WhatsApp-style call bubble,
    everything else (web, push text, DM previews) shows the fallback.

    For events the recipient (callee) already experienced first-hand —
    declining a call, or a normal ended call — the message is marked read for
    them immediately: no unread badge, no notification. Missed/cancelled
    calls stay unread for the callee, which is exactly the missed-call badge.
    """
    try:
        caller = UserProfile.objects.get(id=call.caller_id)
        callee = UserProfile.objects.get(id=call.callee_id)
        payload: dict[str, object] = {
            "call_id": str(call.id),
            "status": status,
            "caller_id": caller.id,
            "callee_id": callee.id,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        content = build_card_message_content(
            "call_event", payload, CALL_EVENT_FALLBACK[status],
        )
        quiet_for_callee = status in ("declined", "ended")
        message_id = internal_send_private_message(
            caller,
            callee,
            content,
            disable_external_notifications=quiet_for_callee,
        )
        if message_id is not None and quiet_for_callee:
            do_update_message_flags(callee, "add", "read", [message_id])
    except Exception as e:
        logger.error(
            "Failed to insert call event message for call %s: %s", call.id, e,
        )


@csrf_exempt
def livekit_webhook(request: HttpRequest) -> HttpResponse:
    """Handle LiveKit webhook events.

    POST /nodl/webhooks/livekit

    Exempt from Zulip API key auth. Validates LiveKit JWT signature in
    the Authorization header. Returns 200 for all valid webhooks.

    Handles:
    - room_finished: marks ringing calls as missed (timeout mechanism)
    - participant_joined: confirms callee presence for CDR accuracy
    - participant_left: updates ended_at/duration if both left
    - room_started: logged only, no action
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "msg": "Method not allowed"},
            status=405,
        )

    receiver = _get_webhook_receiver()
    if receiver is None:
        return JsonResponse(
            {"result": "error", "msg": "Webhook handler not configured"},
            status=503,
        )

    # Validate JWT signature
    auth_header = request.headers.get("Authorization", "")
    body = request.body.decode("utf-8")

    try:
        event = receiver.receive(body, auth_header)
    except Exception as e:
        error_msg = str(e)
        if "hash mismatch" in error_msg or "sha256" in error_msg:
            logger.warning("LiveKit webhook auth failed: %s", e)
        else:
            logger.error("LiveKit webhook unexpected error: %s", e)
        return JsonResponse(
            {"result": "error", "msg": "Invalid webhook signature"},
            status=401,
        )

    event_type = event.event
    room_name = event.room.name if event.room else ""

    logger.info("LiveKit webhook: event=%s room=%s", event_type, room_name)

    if event_type == "room_finished":
        _handle_room_finished(room_name)
    elif event_type == "participant_joined":
        _handle_participant_joined(room_name, event)
    elif event_type == "participant_left":
        _handle_participant_left(room_name, event)
    elif event_type == "room_started":
        logger.debug("Room started: %s", room_name)
    else:
        logger.debug("Unhandled webhook event type: %s", event_type)

    return JsonResponse({"result": "success", "msg": ""})


def _handle_room_finished(room_name: str) -> None:
    """Handle room_finished webhook: mark ringing calls as missed.

    When LiveKit auto-closes a room via empty_timeout (35s), if the call
    is still ringing (callee never joined), it means the call was missed.
    """
    if not room_name:
        return

    with transaction.atomic():
        try:
            call = CallRecord.objects.select_for_update().get(room_name=room_name)
        except CallRecord.DoesNotExist:
            logger.warning("room_finished: no call_record for room %s", room_name)
            return

        # Only transition ringing → missed (idempotent: skip if already terminal)
        if call.status != "ringing":
            logger.debug(
                "room_finished: call %s already in status %s — no action",
                call.id, call.status,
            )
            return

        call.status = "missed"
        call.ended_at = timezone.now()
        call.end_reason = "timeout"
        call.save(update_fields=["status", "ended_at", "end_reason"])

    # Dismiss any still-ringing UI on the callee's devices (best-effort).
    from zproject.nodl.services.call_push_service import dispatch_call_event_push_async

    dispatch_call_event_push_async(call.callee_id, "call_timeout", str(call.id))

    # Insert DM message outside transaction (best-effort)
    insert_call_event_message(call, "missed")


def _handle_participant_joined(room_name: str, event: object) -> None:
    """Handle participant_joined: confirm callee presence for CDR accuracy.

    Supplements the REST /accept endpoint — if callee joins the LiveKit room,
    we confirm their presence on the call record.
    """
    if not room_name:
        return

    participant = getattr(event, "participant", None)
    if not participant:
        return

    identity = participant.identity
    if not identity:
        return

    try:
        call = CallRecord.objects.get(room_name=room_name)
    except CallRecord.DoesNotExist:
        logger.warning("participant_joined: no call_record for room %s", room_name)
        return

    # Only interested in callee joining
    if str(call.callee_id) != identity:
        return

    # If call is still ringing (race: webhook arrives before /accept), update to connected
    if call.status == "ringing":
        with transaction.atomic():
            call_locked = CallRecord.objects.select_for_update().get(id=call.id)
            if call_locked.status == "ringing":
                call_locked.status = "connected"
                call_locked.answered_at = timezone.now()
                call_locked.save(update_fields=["status", "answered_at"])

    logger.info("Callee %s joined room %s", identity, room_name)


def _handle_participant_left(room_name: str, event: object) -> None:
    """Handle participant_left: update CDR if both participants have left.

    If both caller and callee have left the room and the call hasn't been
    ended via REST /end, update the call record.
    """
    if not room_name:
        return

    room = getattr(event, "room", None)
    if not room:
        return

    # If room still has participants, nothing to do
    if room.num_participants > 0:
        return

    with transaction.atomic():
        try:
            call = CallRecord.objects.select_for_update().get(room_name=room_name)
        except CallRecord.DoesNotExist:
            logger.warning("participant_left: no call_record for room %s", room_name)
            return

        # Already ended — idempotent
        if call.status in ("ended", "missed", "declined", "cancelled"):
            return

        # Only end connected calls (ringing calls will be handled by room_finished)
        if call.status != "connected":
            return

        now = timezone.now()
        duration = None
        if call.answered_at:
            duration = int((now - call.answered_at).total_seconds())

        call.status = "ended"
        call.ended_at = now
        call.duration_seconds = duration
        call.end_reason = "room_empty"
        call.save(update_fields=["status", "ended_at", "duration_seconds", "end_reason"])

    # Tell both parties' devices the call is over (best-effort). Clients
    # that already tore down are idle and no-op on this — but a client
    # stranded in a dead call (e.g. it accepted just as the caller
    # cancelled, so the caller never joined) has no other signal: this
    # webhook is the only place that notices the room genuinely emptied.
    from zproject.nodl.services.call_push_service import dispatch_call_event_push_async

    dispatch_call_event_push_async(call.caller_id, "call_ended", str(call.id))
    dispatch_call_event_push_async(call.callee_id, "call_ended", str(call.id))

    # Exactly one path performs the connected→ended transition (the row lock +
    # status guards above make /end and this webhook mutually exclusive), so
    # the ended event is posted once per call.
    insert_call_event_message(call, "ended", duration_seconds=duration)

    logger.info("Both participants left room %s — call ended", room_name)
