"""Canonical plain-text preview of a message.

Single source of truth for every surface that flattens a message to one line
of text: the DM-conversations chat-list preview (nodl/api/views/messages.py)
and mobile push notification bodies (zerver/lib/push_notifications.py).

The pipeline (extracted verbatim from _dm_preview_text, which shipped first):
nodl machine markers are stripped at the HTML level (real comment nodes lose
their delimiters during flattening and a bisected marker is unstrippable
afterwards — see nodl/preview_text.py), inline images are split out, then
Zulip's push-notification HTML->text converter flattens the cleaned HTML.
Voice notes label as ``🎤 <transcript>``, photos as ``📷 <caption>``,
mirroring the mobile client's typed classifier. No truncation here — each
caller applies its own budget (100 chars for chat-list rows, Zulip's
truncate_content for push bodies).

zerver imports are deliberately lazy (call-time): zerver.lib.push_notifications
itself calls into this module, and the chat-list view shouldn't pay the
push-notifications import at module load.
"""

import logging
import re

logger = logging.getLogger(__name__)

# The voice-note filename discriminator (`voice-<epoch>.m4a`) as it appears
# in preview text extracted from rendered_content.
_VOICE_FILENAME_RE = re.compile(r"voice-\d+\.m4a")


def message_preview_text(
    rendered_content: str | None, fallback_content: str, message_id: int
) -> str:
    """Flatten a message to preview text, matching how clients render it."""
    # Local imports: see module docstring.
    from nodl.preview_text import (
        split_inline_images,
        strip_marker_text,
        strip_nodl_markers,
    )
    from zerver.lib.push_notifications import get_mobile_push_content

    text = fallback_content
    image_count = 0
    if rendered_content:
        try:
            cleaned = strip_nodl_markers(rendered_content)
            # Photos flatten to nothing useful (empty img alt) or worse — the
            # raw upload filename from the link paragraph. Split them out and
            # label below, mirroring the clients' typed classifier.
            cleaned, image_count = split_inline_images(cleaned)
            text = get_mobile_push_content(cleaned)
        except Exception:
            # An lxml parse failure must not break the caller (500 the inbox
            # / drop the push); raw content is an acceptable degraded preview.
            logger.exception("Failed to render preview for message %d", message_id)
    # Belt-and-braces for whatever reached the text layer (e.g. the raw-
    # content fallback above, whose markdown source carries escaped markers).
    text = strip_marker_text(text)
    # Voice notes (V2.11): never leak the raw `voice-….m4a` filename into the
    # preview — show `🎤 <transcript>` once the derived transcript is attached
    # to rendered_content, or a bare `🎤 Voice message` before it. Mirrors
    # the mobile client's messagePreviewText so polled, live-derived, and
    # push previews render identically.
    if _VOICE_FILENAME_RE.search(text):
        text = _VOICE_FILENAME_RE.sub("", text)
        text = " ".join(text.split())
        text = f"🎤 {text}" if text else "🎤 Voice message"
    elif image_count:
        # Photo messages: `📷 <caption>` / `📷 Photo` — same shape as the
        # voice label above and the clients' own classifier.
        text = " ".join(text.split())
        text = f"📷 {text}" if text else "📷 Photo"
    text = " ".join(text.split())
    return text
