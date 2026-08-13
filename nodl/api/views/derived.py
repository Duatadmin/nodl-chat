"""Internal derived-content attach endpoint (voice-messaging Phase V2).

nodl-backend POSTs transcription results here after AssemblyAI completes;
we append the nodl-derived block (payload comment + visible transcript
paragraph, see nodl/derived_content.py) to the message's rendered_content
via do_update_embedded_data's plain-string branch — the embed_links
mechanism. That makes the update rendering-only: no "(edited)" flag, no
edit-history entry, and the standard update_message event (rendering_only:
true) re-renders every client live.

Known limitation (same class as embed_links previews): a later real user
edit re-renders from raw markdown and drops the appended block. Voice
messages are effectively un-editable in practice; revisit for V3 text
translations.
"""

import json
import logging

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import BaseModel, ValidationError

from nodl.api.views.internal import require_service_auth
from nodl.derived_content import DERIVED_PAYLOAD_VERSION, apply_derived_block
from zerver.actions.message_edit import do_update_embedded_data
from zerver.models import Message

logger = logging.getLogger(__name__)


class DerivedAttachPayload(BaseModel):
    kind: str
    # Voice rows always carry a transcript; text-translation rows (V3) don't
    # — their visible original is the message itself.
    transcript: str | None = None
    source_lang: str | None = None
    duration_seconds: float | None = None
    confidence: float | None = None
    # V3: faithful per-language translations {lang: text} for opted-in
    # readers. Canonical copies never travel here (AI-side only).
    translations: dict[str, str] | None = None


@csrf_exempt  # type: ignore[untyped-decorator]
@require_service_auth  # type: ignore[untyped-decorator]
def attach_derived_content(request: HttpRequest, message_id: int) -> HttpResponse:
    """Append derived content to a sent message, rendering-only.

    POST /api/v1/internal/messages/<message_id>/derived
    Body: {kind, transcript, source_lang?, duration_seconds?, confidence?}

    Idempotent: a repeat POST replaces the previously appended block.
    """
    if request.method != "POST":
        return JsonResponse(
            {"result": "error", "code": "METHOD_NOT_ALLOWED", "msg": "POST required"},
            status=405,
        )

    try:
        payload = DerivedAttachPayload(**json.loads(request.body))
    except json.JSONDecodeError:
        return JsonResponse(
            {"result": "error", "code": "INVALID_JSON", "msg": "Invalid JSON body"},
            status=400,
        )
    except ValidationError as exc:
        return JsonResponse(
            {"result": "error", "code": "VALIDATION_ERROR", "msg": str(exc)},
            status=400,
        )

    transcript = (payload.transcript or "").strip() or None
    translations = {
        lang: text.strip()
        for lang, text in (payload.translations or {}).items()
        if text and text.strip()
    }
    if transcript is None and not translations:
        return JsonResponse(
            {
                "result": "error",
                "code": "EMPTY_PAYLOAD",
                "msg": "Neither transcript nor translations present",
            },
            status=400,
        )

    with transaction.atomic():
        message = (
            Message.objects.select_for_update()
            .filter(id=message_id)
            .select_related("sender")
            .first()
        )
        if message is None:
            return JsonResponse(
                {"result": "error", "code": "MESSAGE_NOT_FOUND", "msg": "Message not found"},
                status=404,
            )

        derived_payload = {
            "v": DERIVED_PAYLOAD_VERSION,
            "kind": payload.kind,
            "transcript": transcript,
            "source_lang": payload.source_lang,
            "duration_s": payload.duration_seconds,
            "confidence": payload.confidence,
        }
        if translations:
            derived_payload["translations"] = translations
        new_rendered_content = apply_derived_block(
            message.rendered_content or "", derived_payload, transcript
        )
        do_update_embedded_data(message.sender, message, new_rendered_content)

    logger.info(
        "nodl_derived_attach: message %s kind=%s lang=%s",
        message_id,
        payload.kind,
        payload.source_lang,
    )
    return JsonResponse({"result": "success", "message_id": message_id})
