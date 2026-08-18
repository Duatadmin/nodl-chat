"""Unit tests for nodl/message_preview.py (shared chat-list/push preview).

Plain-pytest job: zerver cannot be imported (Django app registry), so the
lazy `from zerver.lib.push_notifications import get_mobile_push_content`
inside message_preview_text is satisfied by injecting a stub module whose
flattener mirrors get_mobile_push_content's text extraction closely enough
for these fixtures (plain text, anchors, spans). Fixtures reuse the REAL
rendered shapes from test_preview_text.py.
"""

import sys
import types
import unittest

import lxml.html


def _fake_get_mobile_push_content(html: str) -> str:
    root = lxml.html.fragment_fromstring(html, create_parent="div")
    return root.text_content()


def _install_zerver_stub(flattener=_fake_get_mobile_push_content):
    zerver = types.ModuleType("zerver")
    zerver_lib = types.ModuleType("zerver.lib")
    push = types.ModuleType("zerver.lib.push_notifications")
    push.get_mobile_push_content = flattener
    zerver.lib = zerver_lib
    zerver_lib.push_notifications = push
    saved = {
        name: sys.modules.get(name)
        for name in ("zerver", "zerver.lib", "zerver.lib.push_notifications")
    }
    sys.modules["zerver"] = zerver
    sys.modules["zerver.lib"] = zerver_lib
    sys.modules["zerver.lib.push_notifications"] = push
    return saved


def _restore_modules(saved) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


DERIVED_VOICE_HTML = (
    '<p><a href="/user_uploads/2/ab/v/voice-1723600000.m4a">voice-1723600000.m4a</a></p>\n'
    "<!-- nodl-derived:v1:eyJ0cmFuc2NyaXB0IjoiaGkifQ== -->"
)

CARD_COMMENT_HTML = "<!-- nodl-card:v1:eyJjYXJkX3R5cGUiOiJhaSJ9 -->\n<p>Missed audio call</p>"

ESCAPED_CARD_HTML = (
    "<p>&lt;!-- nodl-card:v1:eyJjYXJkX3R5cGUiOiJjYWxsX2V2ZW50In0= --&gt;</p>\n"
    "<p><span>Missed audio call</span></p>"
)

PHOTO_HTML = (
    '<div class="message_inline_image">'
    '<a href="/user_uploads/2/ab/p/photo.jpg"><img src="/thumb/photo.jpg"></a></div>'
)


class MessagePreviewTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _install_zerver_stub()
        self.addCleanup(_restore_modules, self._saved)

    def _preview(self, rendered: str | None, fallback: str = "raw") -> str:
        from nodl.message_preview import message_preview_text

        return message_preview_text(rendered, fallback, 1)

    def test_plain_message_flattens_and_collapses_whitespace(self) -> None:
        self.assertEqual(self._preview("<p>Hello\n  world</p>"), "Hello world")

    def test_real_comment_marker_stripped_no_crash(self) -> None:
        # Real HtmlComment nodes crash stock get_mobile_push_content; the
        # marker strip must remove them before flattening.
        self.assertEqual(self._preview(CARD_COMMENT_HTML), "Missed audio call")

    def test_escaped_marker_text_stripped(self) -> None:
        self.assertEqual(self._preview(ESCAPED_CARD_HTML), "Missed audio call")

    def test_voice_note_labels_instead_of_filename(self) -> None:
        self.assertEqual(self._preview(DERIVED_VOICE_HTML), "🎤 Voice message")

    def test_photo_labels(self) -> None:
        self.assertEqual(self._preview(PHOTO_HTML), "📷 Photo")

    def test_photo_with_caption(self) -> None:
        html = PHOTO_HTML + "\n<p>the site today</p>"
        self.assertEqual(self._preview(html), "📷 the site today")

    def test_flattener_failure_falls_back_to_raw_content(self) -> None:
        def _boom(html: str) -> str:
            raise ValueError("Input object is not an XML element: HtmlComment")

        _restore_modules(self._saved)
        self._saved = _install_zerver_stub(flattener=_boom)
        self.assertEqual(
            self._preview("<p>whatever</p>", fallback="raw text <!-- file:{} -->"),
            "raw text",
        )

    def test_empty_rendered_uses_fallback(self) -> None:
        self.assertEqual(self._preview(None, fallback="typed text"), "typed text")


if __name__ == "__main__":
    unittest.main()
