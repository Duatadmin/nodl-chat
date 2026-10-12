"""Shared helpers for chat-message derived content (voice-messaging Phase V2).

Wire format appended to a message's rendered_content (V2.1 spike verdict,
docs/voice-messaging/ARCHITECTURE.md §6 in the nodl_beta monorepo):

    {existing rendered_content}
    <!-- nodl-derived:v1:<base64url JSON> -->
    <p class="nodl-transcript">{transcript text}</p>

Design constraints the shape encodes:
- The payload comment MUST stay a top-level sibling (mobile's parser silently
  drops top-level comments but renders nested ones as error text).
- The visible transcript is a plain <p> so every client — including ones that
  predate this feature — renders it as ordinary text.
- base64url payload (nodl-card:v1 convention): survives every HTML-escaping
  layer; clients extract it by regex on the raw rendered_content string.
- The block is written directly into rendered_content via
  do_update_embedded_data's plain-string branch — NEVER through markdown,
  which HTML-escapes literal comments/tags.
"""

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from django.utils.html import escape

from nodl.preview_text import contains_nodl_marker

DERIVED_PAYLOAD_VERSION = 1

# Derived-content kinds, as nodl-backend's ChatDerivedKind names them.
VOICE_TRANSCRIPT_KIND = "voice_transcript"
TEXT_TRANSLATION_KIND = "text_translation"

# The voice-note discriminator: filenames minted by the mobile composer
# (`voice-<epoch-ms>.m4a`). path_ids look like "2/ab/xyz/voice-1723500000.m4a".
VOICE_NOTE_PATH_ID_RE = re.compile(r"(?:^|/)voice-\d+\.m4a$")

# An upload path_id referenced from raw message content: a relative
# `/user_uploads/<path_id>` link target or the same path on an absolute URL.
_CONTENT_UPLOAD_PATH_RE = re.compile(r"/user_uploads/([^\s()\[\]<>\"'?#]+)")

# The payload comment of a derived block (the block regex below includes it).
_DERIVED_PAYLOAD_RE = re.compile(r"<!--\s*nodl-derived:v1:([A-Za-z0-9_=-]+)\s*-->")

# One appended block, for idempotent replace on re-attach. Matches the comment
# plus its companion transcript paragraph (either may be re-generated).
#
# The transcript <p> is deliberately CLASSLESS: the mobile parser only
# treats `<p>` with an empty class as an ordinary paragraph — any class
# would render as raw-HTML error text on clients that predate V2.9.
# Clients identify the transcript paragraph positionally (it immediately
# follows the nodl-derived comment, which this writer guarantees).
DERIVED_BLOCK_RE = re.compile(
    r"\n?<!--\s*nodl-derived:v1:[A-Za-z0-9_=-]+\s*-->(?:\n?<p>.*?</p>)?",
    re.DOTALL,
)


def voice_note_path_ids(path_ids: Iterable[str]) -> list[str]:
    """Filter attachment path_ids down to voice-note uploads."""
    return [p for p in path_ids if VOICE_NOTE_PATH_ID_RE.search(p)]


def content_voice_note_path_ids(content: str) -> list[str]:
    """The voice-note uploads a message's raw content links to."""
    return voice_note_path_ids(_CONTENT_UPLOAD_PATH_RE.findall(content))


def translation_text(content: str) -> str | None:
    """The translatable text of a message, or None when it shouldn't translate.

    V3 scope (founder 2026-08-13): only voice notes and PLAIN TEXT messages
    are translated. Any /user_uploads reference marks the message as an
    attachment message (image, document, pasted upload link) — those are
    skipped entirely, captions included. So is any message carrying a nodl
    machine marker (cards such as call_event, meeting messages): those are
    data the clients word themselves, and their payload must never reach
    the translator.
    """
    text = content.strip()
    if not text or "/user_uploads/" in text or contains_nodl_marker(text):
        return None
    return text


def translation_source_hash(text: str) -> str:
    """Hash of a translation source, as nodl-backend stores it in
    chat_message_derived.source_hash: hex SHA-256 of the stripped UTF-8 text.

    The text is what translation_text() returned when the message was sent
    to the backend, so both sides hash the same string.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def carry_voice_derived_block(
    old_rendered: str,
    new_rendered: str,
    old_paths: Iterable[str],
    new_paths: Iterable[str],
) -> str:
    """Keep a voice note's derived block across an edit of its message.

    An edit re-renders rendered_content from markdown, which drops the
    appended block. When the edited message still links a voice note the
    old one linked, the audio is unchanged, so its transcript and
    translations stay valid: the old block is appended to the new render
    (no re-transcription, no re-bill, and the edit event already carries
    it). A text-translation block is never carried: the edit changed the
    text it translated.
    """
    if not set(voice_note_path_ids(old_paths)) & set(voice_note_path_ids(new_paths)):
        return new_rendered
    block = DERIVED_BLOCK_RE.search(old_rendered)
    if block is None:
        return new_rendered
    payload_comment = _DERIVED_PAYLOAD_RE.search(block.group(0))
    payload = decode_derived_payload(payload_comment.group(1)) if payload_comment else None
    if payload is None or payload.get("kind") != VOICE_TRANSCRIPT_KIND:
        return new_rendered
    return DERIVED_BLOCK_RE.sub("", new_rendered) + "\n" + block.group(0).lstrip("\n")


def encode_derived_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_derived_payload(encoded: str) -> dict[str, Any] | None:
    """Best-effort decode (used by tests and future server-side readers)."""
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def build_derived_block(payload: dict[str, Any], transcript: str | None) -> str:
    """Render the appendable HTML block for one derived-content payload.

    Voice blocks carry a visible transcript paragraph; text-translation
    blocks (V3) are comment-only — the original text is already visible and
    translations live in the payload for capable clients. Old clients drop
    an unknown top-level comment silently, so both degrade cleanly.
    """
    encoded = encode_derived_payload(payload)
    block = f"\n<!-- nodl-derived:v1:{encoded} -->"
    if transcript:
        block += f"\n<p>{escape(transcript)}</p>"
    return block


def apply_derived_block(
    rendered_content: str, payload: dict[str, Any], transcript: str | None
) -> str:
    """Append (or idempotently replace) the derived block in rendered HTML."""
    base = DERIVED_BLOCK_RE.sub("", rendered_content)
    return base + build_derived_block(payload, transcript)
