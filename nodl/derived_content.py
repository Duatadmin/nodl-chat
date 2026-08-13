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
import json
import re
from collections.abc import Iterable
from typing import Any

from django.utils.html import escape

DERIVED_PAYLOAD_VERSION = 1

# The voice-note discriminator: filenames minted by the mobile composer
# (`voice-<epoch-ms>.m4a`). path_ids look like "2/ab/xyz/voice-1723500000.m4a".
VOICE_NOTE_PATH_ID_RE = re.compile(r"(?:^|/)voice-\d+\.m4a$")

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
