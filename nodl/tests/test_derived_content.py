"""Unit tests for nodl/derived_content.py (voice-messaging Phase V2).

Pure-stdlib + Django-utils tests for the wire-format helpers: the voice-note
path_id discriminator, the base64url payload round-trip, and idempotent
append/replace of the nodl-derived block in rendered_content. These run
without the Django app registry (django.utils.html.escape needs no setup).
"""

import unittest

from nodl.derived_content import (
    DERIVED_BLOCK_RE,
    apply_derived_block,
    build_derived_block,
    decode_derived_payload,
    encode_derived_payload,
    voice_note_path_ids,
)

RENDERED = '<p><a href="/user_uploads/2/ab/xyz/voice-1723500000.m4a">voice-1723500000.m4a</a></p>'


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


if __name__ == "__main__":
    unittest.main()
