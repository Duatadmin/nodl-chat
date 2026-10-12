"""Internal translation-candidates read (W0 multilingual, back-fill + sweep).

nodl-backend pulls message text from here; chat never pushes it. The text is
never persisted backend-side (it rides Celery task arguments), so this read
is how the backend gets it again.

POST /api/v1/internal/derived/candidates — service auth, read-only, two modes:

- Mode A, back-fill: a reader turned translation on (or changed language),
  so the backend wants the reader's recent messages. "The reader's chats"
  means streams they are subscribed to now plus DMs they received, then
  upstream's full access check (bulk_access_messages): on a stream whose
  history is not public to subscribers, only messages the reader actually
  received qualify. Never a realm-wide Message queryset — that was the
  list_messages read hole.
- Mode B, sweep: the backend holds message ids whose translation is stuck
  and needs their current text. Realm-scoped by id; there is no reader.
  Deleted messages are absent (Zulip archives them out of Message).

Each item is classified the way the send hook classifies a new message:
`voice_transcript` (links a voice note; the backend has the transcript, so
no text travels) or `text_translation` (translation_text() is not None, so
cards, meeting messages and other attachments are out).
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import BaseModel, Field, ValidationError

from nodl.api.views.internal import require_service_auth
from nodl.derived_content import (
    TEXT_TRANSLATION_KIND,
    VOICE_TRANSCRIPT_KIND,
    content_voice_note_path_ids,
    translation_text,
)
from nodl.extensions.models import (
    NodlRealmExtension,
    NodlRealmUserExtension,
    NodlUserExtension,
)
from zerver.lib.message import bulk_access_messages
from zerver.lib.stream_subscription import get_subscribed_stream_recipient_ids_for_user
from zerver.models import Message, UserMessage

logger = logging.getLogger(__name__)

MAX_BACKFILL_ITEMS = 300
MAX_SWEEP_MESSAGE_IDS = 200
# Total text per back-fill page, in UTF-8 bytes.
MAX_PAGE_TEXT_BYTES = 300_000
# Rows scanned per back-fill item wanted: access checks and classification
# drop some (photos, cards, protected history).
SCAN_FACTOR = 2


class CandidatesPayload(BaseModel):
    workspace_id: uuid.UUID
    # Mode A (back-fill)
    supabase_user_id: uuid.UUID | None = None
    since: datetime | None = None
    limit: int = MAX_BACKFILL_ITEMS
    before_message_id: int | None = None
    # Mode B (sweep)
    message_ids: list[int] | None = Field(default=None, max_length=MAX_SWEEP_MESSAGE_IDS)


def _error(code: str, msg: str, status: int) -> JsonResponse:
    return JsonResponse({"result": "error", "code": code, "msg": msg}, status=status)


def _classify(content: str) -> tuple[str | None, str | None]:
    """(kind, text) for a message; (None, None) when it doesn't translate."""
    if content_voice_note_path_ids(content):
        return VOICE_TRANSCRIPT_KIND, None
    text = translation_text(content)
    if text is not None:
        return TEXT_TRANSLATION_KIND, text
    return None, None


def _supabase_ids_by_sender(sender_ids: set[int]) -> dict[int, str]:
    """Realm-scoped mapping first, the legacy global one as fallback (the
    worker's _sender_nodl_user_id rule), in at most two queries."""
    found = {
        zulip_user_id: str(supabase_user_id)
        for zulip_user_id, supabase_user_id in NodlRealmUserExtension.objects.filter(
            zulip_user_id__in=sender_ids
        ).values_list("zulip_user_id", "supabase_user_id")
    }
    missing = sender_ids - found.keys()
    if missing:
        found.update(
            {
                zulip_user_id: str(supabase_user_id)
                for zulip_user_id, supabase_user_id in NodlUserExtension.objects.filter(
                    zulip_user_id__in=missing
                ).values_list("zulip_user_id", "supabase_user_id")
            }
        )
    return found


def _item(
    message: Message, kind: str | None, text: str | None, senders: dict[int, str]
) -> dict[str, Any]:
    return {
        "message_id": message.id,
        "kind": kind,
        "text": text,
        "sender_supabase_user_id": senders.get(message.sender_id),
        "date_sent": message.date_sent.isoformat(),
    }


def _backfill_candidates(realm_id: int, payload: CandidatesPayload) -> JsonResponse:
    """Mode A: the reader's recent translatable messages, newest first."""
    mapping = (
        NodlRealmUserExtension.objects.select_related("zulip_user")
        .filter(zulip_realm_id=realm_id, supabase_user_id=payload.supabase_user_id)
        .first()
    )
    reader = mapping.zulip_user if mapping is not None else None
    if reader is None or not reader.is_active or reader.realm_id != realm_id:
        return _error("USER_NOT_FOUND", "User not found in this workspace", 404)

    assert payload.since is not None
    since = payload.since if payload.since.tzinfo else payload.since.replace(tzinfo=UTC)
    limit = min(max(1, payload.limit), MAX_BACKFILL_ITEMS)

    received_dm_ids = UserMessage.objects.filter(
        user_profile=reader,
        flags__andnz=UserMessage.flags.is_private.mask,
        message__date_sent__gte=since,
    ).values("message_id")
    candidates = (
        Message.objects.filter(realm_id=realm_id, date_sent__gte=since)
        .filter(
            Q(recipient_id__in=get_subscribed_stream_recipient_ids_for_user(reader))
            | Q(id__in=received_dm_ids)
        )
        .exclude(sender_id=reader.id)
        .exclude(sender__is_bot=True)
    )
    if payload.before_message_id is not None:
        candidates = candidates.filter(id__lt=payload.before_message_id)
    window = limit * SCAN_FACTOR
    scanned = list(candidates.select_related("recipient").order_by("-id")[:window])
    accessible_ids = {
        message.id for message in bulk_access_messages(reader, scanned, is_modifying_message=False)
    }

    picked: list[tuple[Message, str, str | None]] = []
    text_bytes = 0
    # The last row fully handled (picked or skipped): the next page starts below it.
    cursor: int | None = None
    more = len(scanned) == window
    for message in scanned:
        if message.id in accessible_ids:
            kind, text = _classify(message.content)
            if kind is not None:
                size = len(text.encode("utf-8")) if text else 0
                if picked and text_bytes + size > MAX_PAGE_TEXT_BYTES:
                    more = True
                    break
                picked.append((message, kind, text))
                text_bytes += size
        cursor = message.id
        if len(picked) >= limit:
            more = True
            break

    senders = _supabase_ids_by_sender({message.sender_id for message, _kind, _text in picked})
    return JsonResponse(
        {
            "result": "success",
            "items": [_item(message, kind, text, senders) for message, kind, text in picked],
            "next_before_message_id": cursor if more else None,
        }
    )


def _sweep_candidates(realm_id: int, message_ids: list[int]) -> JsonResponse:
    """Mode B: current kind and text for known ids; deleted ids are absent.

    `kind` is null for a message that no longer translates (edited into a
    photo, a card or a meeting message).
    """
    messages = list(
        Message.objects.filter(realm_id=realm_id, id__in=set(message_ids))
        .only("id", "content", "sender_id", "date_sent")
        .order_by("id")
    )
    senders = _supabase_ids_by_sender({message.sender_id for message in messages})
    return JsonResponse(
        {
            "result": "success",
            "items": [_item(message, *_classify(message.content), senders) for message in messages],
        }
    )


@csrf_exempt  # type: ignore[untyped-decorator]
@require_service_auth  # type: ignore[untyped-decorator]
def list_translation_candidates(request: HttpRequest) -> HttpResponse:
    """Translation candidates for the backend's back-fill and sweep.

    POST /api/v1/internal/derived/candidates
    Mode A: {workspace_id, supabase_user_id, since, limit?<=300, before_message_id?}
        -> {items, next_before_message_id}
    Mode B: {workspace_id, message_ids<=200} -> {items}
    Items: {message_id, kind, text, sender_supabase_user_id, date_sent}.

    An unknown workspace, or a user who isn't an active member of it, is 404.
    """
    if request.method != "POST":
        return _error("METHOD_NOT_ALLOWED", "POST required", 405)

    try:
        payload = CandidatesPayload(**json.loads(request.body))
    except json.JSONDecodeError:
        return _error("INVALID_JSON", "Invalid JSON body", 400)
    except ValidationError as exc:
        return _error("VALIDATION_ERROR", str(exc), 400)

    sweep = payload.message_ids is not None
    if sweep == (payload.supabase_user_id is not None):
        return _error(
            "VALIDATION_ERROR", "Send either message_ids or supabase_user_id, not both", 400
        )
    if not sweep and payload.since is None:
        return _error("VALIDATION_ERROR", "since is required with supabase_user_id", 400)

    extension = (
        NodlRealmExtension.objects.select_related("zulip_realm")
        .filter(nodl_workspace_id=payload.workspace_id)
        .first()
    )
    if extension is None or extension.zulip_realm.deactivated:
        return _error("WORKSPACE_NOT_FOUND", "Workspace not found", 404)

    if sweep:
        assert payload.message_ids is not None
        return _sweep_candidates(extension.zulip_realm_id, payload.message_ids)
    return _backfill_candidates(extension.zulip_realm_id, payload)
