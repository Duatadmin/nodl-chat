# NODL fork worker — voice-messaging Phase V2 (derived content).
#
# This file lives under zerver/worker/ (not nodl/) because the worker
# registry is hardcoded to this package: get_worker() imports
# f"zerver.worker.{queue_name}" and get_active_worker_queues() scans only
# zerver.worker.__path__. One small isolated file here is the cheaper fork
# trade vs. patching the registry. All non-trivial logic stays in
# nodl/derived_content.py.
#
# Flow: do_send_messages hook (message_send.py, NODL block) queues
# {message_id, message_realm_id, path_ids} for every voice-note attachment →
# this worker mints presigned R2 GET URLs (AssemblyAI pulls the audio
# directly; the client-visible /user_uploads/ path is auth-proxied and not
# backend-fetchable) → POSTs to nodl-backend's internal derive endpoint,
# which owns transcription + attach-back.
import logging
from collections.abc import Mapping
from typing import Any

import requests
from django.conf import settings
from typing_extensions import override

from nodl.derived_content import translation_text, voice_note_path_ids
from nodl.extensions.models import NodlRealmExtension
from zerver.lib.queue import retry_event
from zerver.models import Message
from zerver.worker.base import QueueProcessingWorker, assign_queue

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15


def _presign_attachment(path_id: str) -> str:
    """Mint a presigned GET URL for an R2/S3-stored attachment."""
    from zerver.lib.upload.s3 import get_boto_client

    return get_boto_client().generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": settings.S3_AUTH_UPLOADS_BUCKET,
            "Key": path_id,
        },
        ExpiresIn=settings.NODL_DERIVED_PRESIGN_TTL,
        HttpMethod="GET",
    )


@assign_queue("nodl_derived_content")
class NodlDerivedContentWorker(QueueProcessingWorker):
    # Network-bound consume (presign is local crypto, but the backend POST
    # is a real request) — update stats after every event, like embed_links.
    CONSUME_ITERATIONS_BEFORE_UPDATE_STATS_NUM = 1

    @override
    def consume(self, event: Mapping[str, Any]) -> None:
        message_id = event["message_id"]

        if not settings.NODL_BACKEND_URL or not settings.BACKEND_SERVICE_KEY:
            logger.warning(
                "nodl_derived_content: NODL_BACKEND_URL/BACKEND_SERVICE_KEY "
                "unconfigured; dropping event for message %s",
                message_id,
            )
            return

        message = Message.objects.filter(id=message_id).first()
        if message is None:
            return  # Deleted before we got to it.

        extension = NodlRealmExtension.objects.filter(
            zulip_realm_id=event["message_realm_id"]
        ).first()
        if extension is None:
            logger.warning(
                "nodl_derived_content: realm %s has no NodlRealmExtension; "
                "dropping event for message %s",
                event["message_realm_id"],
                message_id,
            )
            return

        # Re-filter defensively (the hook already filtered).
        path_ids = voice_note_path_ids(event["path_ids"])
        if path_ids:
            if settings.LOCAL_UPLOADS_DIR is not None:
                # Dev/local-uploads deployments can't presign — nothing to do.
                logger.warning(
                    "nodl_derived_content: local uploads backend; dropping "
                    "voice event for message %s",
                    message_id,
                )
                return
            attachments = [
                {
                    "path_id": path_id,
                    "presigned_url": _presign_attachment(path_id),
                    "filename": path_id.rsplit("/", 1)[-1],
                    "mime": "audio/mp4",
                }
                for path_id in path_ids
            ]
            payload = {
                "workspace_id": str(extension.nodl_workspace_id),
                "message_id": message_id,
                "kind": "voice_transcript",
                "attachments": attachments,
            }
        else:
            # V3.6: text-translation candidate. The backend gates on the
            # workspace's live translation_enabled setting (202-skips when
            # off), so we forward every eligible message rather than
            # mirroring that flag here. Only PLAIN TEXT translates —
            # attachment messages (any /user_uploads reference) are out of
            # scope, captions included (founder 2026-08-13).
            if message.sender.is_bot:
                return
            text = translation_text(message.content)
            if text is None:
                return
            payload = {
                "workspace_id": str(extension.nodl_workspace_id),
                "message_id": message_id,
                "kind": "text_translation",
                "text": text,
            }

        try:
            response = requests.post(
                f"{settings.NODL_BACKEND_URL}/api/v1/internal/chat/messages/derived",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.BACKEND_SERVICE_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            logger.warning(
                "nodl_derived_content: backend unreachable for message %s: %s",
                message_id,
                e,
            )
            self._retry(dict(event))
            return

        if response.status_code >= 500:
            logger.warning(
                "nodl_derived_content: backend %s for message %s: %s",
                response.status_code,
                message_id,
                response.text[:200],
            )
            self._retry(dict(event))
            return

        if response.status_code not in (200, 202):
            # 4xx — permanent (bad payload / unknown workspace); don't retry.
            logger.error(
                "nodl_derived_content: backend rejected message %s with %s: %s",
                message_id,
                response.status_code,
                response.text[:200],
            )
            return

        logger.info(
            "nodl_derived_content: queued %s derive for message %s",
            payload["kind"],
            message_id,
        )

    def _retry(self, event: dict[str, Any]) -> None:
        def failure_processor(failed_event: dict[str, Any]) -> None:
            logger.error(
                "nodl_derived_content: gave up on message %s after %s tries",
                failed_event["message_id"],
                failed_event.get("failed_tries"),
            )

        retry_event(self.queue_name, event, failure_processor)
