"""Unit tests for nodl/preview_text.py (chat-list preview marker hygiene).

Pure lxml/stdlib tests — no Django app registry, no zerver imports — so they
run under the plain-pytest CI job. Fixtures mirror the REAL rendered shapes:

- file cards: zerver/lib/markdown handle_file_inlining writes the marker as
  ``div.text`` (serialized entity-escaped) plus a fallback anchor, sometimes
  beside a bare upload-link paragraph;
- nodl-derived/nodl-card blocks: REAL top-level HTML comments injected into
  rendered_content post-render (nodl/derived_content.py wire format).
"""

import unittest

from nodl.preview_text import strip_marker_text, strip_nodl_markers

FILE_CARD_HTML = (
    '<p><a href="/user_uploads/2/ab/T/test-letter.docx">test-letter.docx</a></p>\n'
    '<div class="file_card_container">'
    "&lt;!-- file:{&quot;path_id&quot;:&quot;2/ab/T/test-letter.docx&quot;,"
    "&quot;file_name&quot;:&quot;test-letter.docx&quot;,&quot;size&quot;:123456,"
    "&quot;content_type&quot;:&quot;application/pdf&quot;} --&gt;"
    '<a href="/user_uploads/2/ab/T/test-letter.docx">test-letter.docx</a></div>'
)

DERIVED_VOICE_HTML = (
    '<p><a href="/user_uploads/2/ab/v/voice-1723600000.m4a">voice-1723600000.m4a</a></p>\n'
    "<!-- nodl-derived:v1:eyJ0cmFuc2NyaXB0IjoiaGkifQ== -->\n"
    "<p>hi</p>"
)

CARD_COMMENT_HTML = "<!-- nodl-card:v1:eyJjYXJkX3R5cGUiOiJhaSJ9 -->\n<p>fallback text</p>"


def _text_of(html: str) -> str:
    """Flatten with lxml the way get_mobile_push_content extracts text."""
    import lxml.html

    root = lxml.html.fragment_fromstring(html, create_parent="div")
    return " ".join(root.text_content().split())


class StripNodlMarkersTest(unittest.TestCase):
    def test_escaped_file_marker_removed(self) -> None:
        cleaned = strip_nodl_markers(FILE_CARD_HTML)
        self.assertNotIn("file:{", cleaned)
        self.assertNotIn("path_id", cleaned)

    def test_redundant_upload_paragraph_dropped_once(self) -> None:
        # The filename must survive exactly once (the card's fallback anchor);
        # the bare upload-link paragraph beside the card is dropped.
        flattened = _text_of(strip_nodl_markers(FILE_CARD_HTML))
        self.assertEqual(flattened, "test-letter.docx")

    def test_real_derived_comment_removed_transcript_kept(self) -> None:
        cleaned = strip_nodl_markers(DERIVED_VOICE_HTML)
        self.assertNotIn("nodl-derived", cleaned)
        self.assertIn("<p>hi</p>", cleaned)
        # The voice anchor stays — the endpoint's 🎤 special case keys on it.
        self.assertIn("voice-1723600000.m4a", cleaned)

    def test_real_card_comment_removed_fallback_kept(self) -> None:
        cleaned = strip_nodl_markers(CARD_COMMENT_HTML)
        self.assertNotIn("nodl-card", cleaned)
        self.assertIn("fallback text", cleaned)

    def test_user_typed_comment_text_survives(self) -> None:
        # A user literally typing an HTML comment renders as visible escaped
        # text; it is NOT our marker grammar and must stay in the preview.
        html = "<p>see &lt;!-- important note --&gt; above</p>"
        self.assertIn("important note", strip_nodl_markers(html))

    def test_code_block_comment_survives(self) -> None:
        html = '<div class="codehilite"><pre>&lt;!-- html comment in code --&gt;</pre></div>'
        self.assertIn("html comment in code", strip_nodl_markers(html))

    def test_plain_text_untouched(self) -> None:
        html = "<p>Hello <strong>world</strong>!</p>"
        self.assertEqual(_text_of(strip_nodl_markers(html)), "Hello world!")

    def test_empty_and_garbage_inputs_returned_unchanged(self) -> None:
        self.assertEqual(strip_nodl_markers(""), "")
        self.assertEqual(strip_nodl_markers("   "), "   ")


class StripMarkerTextTest(unittest.TestCase):
    def test_complete_marker_stripped(self) -> None:
        text = 'name <!-- file:{"path_id":"x","file_name":"name"} --> tail'
        self.assertEqual(strip_marker_text(text).strip(), "name  tail".strip())

    def test_truncated_marker_tail_stripped(self) -> None:
        # The exact production junk: a marker bisected by the 100-char cut.
        text = (
            "test-letter.docx "
            '<!-- file:{"path_id":"2/ab/T/test-letter.docx","file_name":"test-lett…'
        )
        self.assertEqual(strip_marker_text(text).strip(), "test-letter.docx")

    def test_truncated_card_marker_stripped(self) -> None:
        text = "<!-- nodl-card:v1:eyJjYXJkX3R5cGUiOiAiY2FsbF9ldmVu…"
        self.assertEqual(strip_marker_text(text).strip(), "")

    def test_non_marker_comment_text_survives(self) -> None:
        text = "before <!-- just a note --> after"
        self.assertEqual(strip_marker_text(text), text)


if __name__ == "__main__":
    unittest.main()
