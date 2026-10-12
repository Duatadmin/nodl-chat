"""Unit tests for nodl/derived_content.py (voice-messaging Phase V2).

Pure-stdlib + Django-utils tests for the wire-format helpers: the voice-note
path_id discriminator, the base64url payload round-trip, and idempotent
append/replace of the nodl-derived block in rendered_content. These run
without the Django app registry (django.utils.html.escape needs no setup).
"""

import base64
import json
import unittest
from typing import Any

from nodl.derived_content import (
    DERIVED_BLOCK_RE,
    apply_derived_block,
    build_derived_block,
    carry_voice_derived_block,
    content_voice_note_path_ids,
    decode_derived_payload,
    encode_derived_payload,
    translation_source_hash,
    translation_text,
    voice_note_path_ids,
)

RENDERED = '<p><a href="/user_uploads/2/ab/xyz/voice-1723500000.m4a">voice-1723500000.m4a</a></p>'


def build_card_message_content(card_type: str, payload: dict[str, Any], fallback: str) -> str:
    """The AD-12 card content shape (nodl/api/views/card_schemas.py), built
    here because importing nodl.api.views needs the Django app registry."""
    raw = json.dumps({"card_type": card_type, "payload": payload}, ensure_ascii=False)
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"<!-- nodl-card:v1:{encoded} -->\n\n{fallback}"


class VoiceNotePathIdTest(unittest.TestCase):
    def test_matches_voice_filenames_only(self) -> None:
        path_ids = [
            "2/ab/xyz/voice-1723500000.m4a",
            "2/ab/xyz/report.pdf",
            "2/ab/xyz/song.m4a",
            "2/ab/xyz/voice-123.mp3",
            "voice-42.m4a",  # bare (no directory prefix)
        ]
        self.assertEqual(
            voice_note_path_ids(path_ids),
            ["2/ab/xyz/voice-1723500000.m4a", "voice-42.m4a"],
        )

    def test_does_not_match_voice_substring_in_directory(self) -> None:
        # Only the FILENAME segment may carry the discriminator.
        self.assertEqual(voice_note_path_ids(["voice-1/other.m4a"]), [])


class TranslationTextTest(unittest.TestCase):
    """V3 scope: only PLAIN TEXT messages translate (voice has its own
    path); any /user_uploads reference = attachment message = skipped."""

    def test_plain_text_passes_through_stripped(self) -> None:
        self.assertEqual(
            translation_text("  Бетон привезли в 8 утра  "),
            "Бетон привезли в 8 утра",
        )

    def test_empty_content_is_none(self) -> None:
        self.assertIsNone(translation_text(""))
        self.assertIsNone(translation_text("   \n  "))

    def test_attachment_messages_are_skipped_captions_included(self) -> None:
        self.assertIsNone(translation_text("[photo.jpg](/user_uploads/2/ab/x/photo.jpg)"))
        self.assertIsNone(
            translation_text("смотри фото\n[photo.jpg](/user_uploads/2/ab/x/photo.jpg)")
        )
        self.assertIsNone(
            translation_text("вот файл https://chat.example/user_uploads/2/ab/x/report.pdf")
        )


class TranslationTextMarkerTest(unittest.TestCase):
    """W0 P5a: nodl machine markers are data the clients word themselves —
    their payload (base64 or JSON) must never reach the translator."""

    def test_call_event_card_is_skipped(self) -> None:
        # Sent with the human caller as sender, so the bot filter misses it.
        content = build_card_message_content(
            "call_event",
            {"call_id": "c-1", "status": "missed", "caller_id": 7, "callee_id": 9},
            "Missed call",
        )
        self.assertIsNone(translation_text(content))

    def test_assistant_card_is_skipped(self) -> None:
        content = build_card_message_content(
            "ask_ai_answer",
            {"task_id": "t-1", "mode": "qa", "question": "Когда бетон?", "answer": "Завтра."},
            "**Ask AI**: Завтра.",
        )
        self.assertIsNone(translation_text(content))

    def test_meeting_marker_is_skipped(self) -> None:
        # Human-sent from the web composer; no /user_uploads reference.
        content = (
            '<!-- meeting:{"room":"site-standup","title":"Standup"} -->\nJoin the meeting at 9'
        )
        self.assertIsNone(translation_text(content))

    def test_every_marker_prefix_is_skipped(self) -> None:
        for prefix in ("nodl-card:v1:", "meeting:", "nodl-derived:v1:", "file:"):
            with self.subTest(prefix=prefix):
                self.assertIsNone(translation_text(f"<!--{prefix}eyJ4IjoxfQ== -->\nhi"))

    def test_plain_text_still_translates(self) -> None:
        self.assertEqual(translation_text("Pour tomorrow at 7"), "Pour tomorrow at 7")

    def test_non_nodl_html_comment_still_translates(self) -> None:
        # A comment a user typed is not nodl's grammar; it stays visible text.
        content = "<!-- note to self --> Бетон привезли"
        self.assertEqual(translation_text(content), content)


class PayloadCodecTest(unittest.TestCase):
    def test_round_trip_preserves_unicode(self) -> None:
        payload = {"v": 1, "transcript": "Привет, стройка!", "source_lang": "ru"}
        self.assertEqual(decode_derived_payload(encode_derived_payload(payload)), payload)

    def test_encoded_alphabet_is_escape_proof(self) -> None:
        # base64url only — no chars any HTML-escaping layer touches.
        encoded = encode_derived_payload({"t": "<&\"'>"})
        self.assertRegex(encoded, r"^[A-Za-z0-9_=-]+$")

    def test_decode_garbage_returns_none(self) -> None:
        self.assertIsNone(decode_derived_payload("!!!not-base64!!!"))
        self.assertIsNone(decode_derived_payload(encode_derived_payload_list()))


def encode_derived_payload_list() -> str:
    # A valid base64url string that decodes to a non-dict JSON value.
    import base64

    return base64.urlsafe_b64encode(b"[1,2,3]").decode("ascii")


class DerivedBlockTest(unittest.TestCase):
    PAYLOAD = {"v": 1, "kind": "voice_transcript", "transcript": "привет"}

    def test_block_shape(self) -> None:
        block = build_derived_block(self.PAYLOAD, "привет <мир>")
        # Top-level comment sibling + CLASSLESS transcript paragraph, in
        # order. Classless is load-bearing: the mobile parser renders a
        # classed <p> as raw-HTML error text on pre-V2.9 clients; clients
        # bind the paragraph to the comment positionally instead.
        self.assertRegex(
            block,
            r"^\n<!-- nodl-derived:v1:[A-Za-z0-9_=-]+ -->"
            r"\n<p>привет &lt;мир&gt;</p>$",
        )

    def test_apply_appends_after_existing_content(self) -> None:
        result = apply_derived_block(RENDERED, self.PAYLOAD, "привет")
        self.assertTrue(result.startswith(RENDERED))
        self.assertEqual(len(DERIVED_BLOCK_RE.findall(result)), 1)

    def test_apply_is_idempotent_replace(self) -> None:
        first = apply_derived_block(RENDERED, self.PAYLOAD, "первая версия")
        second = apply_derived_block(
            first, {**self.PAYLOAD, "transcript": "вторая"}, "вторая версия"
        )
        self.assertEqual(len(DERIVED_BLOCK_RE.findall(second)), 1)
        self.assertIn("вторая версия", second)
        self.assertNotIn("первая версия", second)
        self.assertTrue(second.startswith(RENDERED))

    def test_payload_extractable_by_client_regex(self) -> None:
        # The client-side extraction contract: regex over the raw string.
        import re

        result = apply_derived_block(RENDERED, self.PAYLOAD, "привет")
        match = re.search(r"<!--\s*nodl-derived:v1:([A-Za-z0-9_=-]+)\s*-->", result)
        assert match is not None
        decoded = decode_derived_payload(match.group(1))
        assert decoded is not None
        self.assertEqual(decoded["kind"], "voice_transcript")

    def test_text_translation_block_is_comment_only(self) -> None:
        # V3: text-translation blocks have NO visible paragraph — the
        # original message is already on screen; translations live only in
        # the payload. Old clients drop the top-level comment silently.
        payload = {
            "v": 1,
            "kind": "text_translation",
            "translations": {"en": "Concrete was delivered"},
        }
        block = build_derived_block(payload, None)
        self.assertRegex(block, r"^\n<!-- nodl-derived:v1:[A-Za-z0-9_=-]+ -->$")
        self.assertNotIn("<p>", block)

    def test_comment_only_block_replaced_idempotently(self) -> None:
        text_rendered = "<p>Бетон привезли в 8 утра</p>"
        payload = {"v": 1, "kind": "text_translation", "translations": {"en": "a"}}
        first = apply_derived_block(text_rendered, payload, None)
        second = apply_derived_block(
            first,
            {**payload, "translations": {"en": "b", "ru": "в"}},
            None,
        )
        self.assertEqual(len(DERIVED_BLOCK_RE.findall(second)), 1)
        self.assertTrue(second.startswith(text_rendered))
        # The message's own <p> must never be eaten by the replace regex.
        self.assertIn("Бетон привезли в 8 утра", second)

    def test_voice_block_upgraded_with_translations(self) -> None:
        # A voice message first gets its transcript block, then the
        # translations re-attach replaces it wholesale.
        first = apply_derived_block(RENDERED, self.PAYLOAD, "привет")
        upgraded_payload = {**self.PAYLOAD, "translations": {"en": "hi"}}
        second = apply_derived_block(first, upgraded_payload, "привет")
        self.assertEqual(len(DERIVED_BLOCK_RE.findall(second)), 1)
        import re

        match = re.search(r"<!--\s*nodl-derived:v1:([A-Za-z0-9_=-]+)\s*-->", second)
        assert match is not None
        decoded = decode_derived_payload(match.group(1))
        assert decoded is not None
        self.assertEqual(decoded["translations"], {"en": "hi"})
        self.assertIn("<p>привет</p>", second)


VOICE_PATH = "2/ab/xyz/voice-1723500000.m4a"


class ContentVoiceNotePathIdsTest(unittest.TestCase):
    def test_finds_voice_links_in_raw_content(self) -> None:
        content = (
            f"[voice-1723500000.m4a](/user_uploads/{VOICE_PATH}) fixed the caption\n"
            "[plan.pdf](/user_uploads/2/ab/xyz/plan.pdf)\n"
            "https://chat.example/user_uploads/2/cd/q/voice-42.m4a?x=1"
        )
        self.assertEqual(content_voice_note_path_ids(content), [VOICE_PATH, "2/cd/q/voice-42.m4a"])

    def test_text_and_other_uploads_have_none(self) -> None:
        self.assertEqual(content_voice_note_path_ids("pour at 7, voice-1.m4a later"), [])
        self.assertEqual(
            content_voice_note_path_ids("[song.m4a](/user_uploads/2/ab/x/song.m4a)"), []
        )


class TranslationSourceHashTest(unittest.TestCase):
    def test_is_hex_sha256_of_the_stripped_utf8_text(self) -> None:
        import hashlib

        expected = hashlib.sha256("Бетон в 8".encode()).hexdigest()
        self.assertEqual(translation_source_hash("Бетон в 8"), expected)
        self.assertEqual(translation_source_hash("  Бетон в 8 \n"), expected)
        self.assertRegex(expected, r"^[0-9a-f]{64}$")


class CarryVoiceDerivedBlockTest(unittest.TestCase):
    """W0 P1: an edit re-renders from markdown and drops the appended block;
    a voice note the edit keeps keeps its transcript and translations."""

    VOICE_PAYLOAD = {
        "v": 1,
        "kind": "voice_transcript",
        "transcript": "привет",
        "translations": {"en": "hi"},
    }
    NEW_RENDER = f'<p><a href="/user_uploads/{VOICE_PATH}">voice-1723500000.m4a</a> fixed</p>'

    def test_carries_the_block_when_the_voice_note_is_kept(self) -> None:
        old = apply_derived_block(RENDERED, self.VOICE_PAYLOAD, "привет")
        carried = carry_voice_derived_block(old, self.NEW_RENDER, [VOICE_PATH], [VOICE_PATH])

        self.assertTrue(carried.startswith(self.NEW_RENDER))
        self.assertEqual(DERIVED_BLOCK_RE.findall(carried), DERIVED_BLOCK_RE.findall(old))
        self.assertIn("<p>привет</p>", carried)

    def test_drops_the_block_when_the_voice_note_is_removed(self) -> None:
        old = apply_derived_block(RENDERED, self.VOICE_PAYLOAD, "привет")
        new_render = "<p>never mind</p>"

        self.assertEqual(carry_voice_derived_block(old, new_render, [VOICE_PATH], []), new_render)
        other_voice = "2/ab/xyz/voice-99.m4a"
        self.assertEqual(
            carry_voice_derived_block(old, new_render, [VOICE_PATH], [other_voice]), new_render
        )

    def test_never_carries_a_text_translation_block(self) -> None:
        # Even beside a kept voice path: a text block translated the old text.
        old = apply_derived_block(
            RENDERED, {"v": 1, "kind": "text_translation", "translations": {"en": "a"}}, None
        )
        self.assertEqual(
            carry_voice_derived_block(old, self.NEW_RENDER, [VOICE_PATH], [VOICE_PATH]),
            self.NEW_RENDER,
        )

    def test_nothing_to_carry_without_a_block(self) -> None:
        self.assertEqual(
            carry_voice_derived_block(RENDERED, self.NEW_RENDER, [VOICE_PATH], [VOICE_PATH]),
            self.NEW_RENDER,
        )

    def test_non_voice_attachments_are_not_voice_notes(self) -> None:
        old = apply_derived_block(RENDERED, self.VOICE_PAYLOAD, "привет")
        pdf = "2/ab/xyz/plan.pdf"
        self.assertEqual(
            carry_voice_derived_block(old, self.NEW_RENDER, [pdf], [pdf]), self.NEW_RENDER
        )


if __name__ == "__main__":
    unittest.main()
