"""Strip nodl machine markers from rendered message HTML (preview hygiene).

Chat-list previews (the DM-conversations endpoint) flatten a message's
rendered_content to a short plain-text line via zerver's
get_mobile_push_content, then truncate it. nodl messages carry machine
markers that must be removed BEFORE flattening — a marker bisected by the
truncation is unstrippable afterwards, and the two marker shapes flatten
differently:

- Entity-escaped markers rendered from markdown source (file cards, legacy
  meeting cards): the marker sits as literal TEXT inside an element, e.g.
  ``<div class="file_card_container">&lt;!-- file:{json} --&gt;<a>…</a></div>``
  (zerver/lib/markdown handle_file_inlining writes it via ``div.text``).
  lxml exposes it un-escaped through ``.text``/``.tail``.
- Real top-level HTML comments injected post-render, bypassing markdown
  (``nodl-derived:v1`` voice transcripts — see nodl/derived_content.py — and
  ``nodl-card:v1`` assistant/call cards): lxml yields comment NODES whose
  delimiters never appear in extracted text, so only node removal works.

Only nodl's own marker grammar is matched for the text form: a user literally
typing an HTML comment (or a code block containing one) renders as visible
text in the message bubble, and the preview must keep showing it.

This module is intentionally free of Django/zerver imports so its tests run
under the plain-pytest CI job (see nodl/tests/test_preview_text.py).
"""

import re

import lxml.etree
import lxml.html

# The nodl marker grammar: every machine-marker comment nodl writes into
# message content starts with one of these prefixes. Extend here when a new
# marker kind is introduced — never match arbitrary comments (see module doc).
_MARKER_PREFIXES = r"(?:file:|nodl-card:v1:|nodl-derived:v1:|meeting:)"

# A complete marker appearing as literal text (the entity-escaped form,
# un-escaped by the HTML parser before we see it).
_MARKER_TEXT_RE = re.compile(rf"<!--\s*{_MARKER_PREFIXES}[\s\S]*?-->")

# A marker whose closing delimiter is gone (e.g. a previously-truncated
# preview persisted by an older build, re-cleaned by a client). Anchored to
# the same grammar; consumes to end of string.
_MARKER_UNTERMINATED_RE = re.compile(rf"<!--\s*{_MARKER_PREFIXES}[\s\S]*$")

# Real comment nodes: same grammar, applied to the node's inner text.
_MARKER_COMMENT_BODY_RE = re.compile(rf"^\s*{_MARKER_PREFIXES}")


def strip_marker_text(text: str) -> str:
    """Remove nodl marker text (complete or truncated-tail) from plain text."""
    return _MARKER_UNTERMINATED_RE.sub("", _MARKER_TEXT_RE.sub("", text))


def _remove_preserving_tail(node: lxml.etree._Element) -> None:
    """Drop a node, splicing its tail text into the surviving tree."""
    parent = node.getparent()
    if parent is None:
        return
    tail = node.tail or ""
    previous = node.getprevious()
    if previous is not None:
        previous.tail = (previous.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail
    parent.remove(node)


def _sole_anchor_href(element: lxml.html.HtmlElement) -> str | None:
    """The href of ``element``'s only child anchor, if the element contains
    exactly one ``<a>`` and no other non-whitespace text."""
    children = list(element)
    if len(children) != 1 or children[0].tag != "a":
        return None
    anchor = children[0]
    stray = (element.text or "") + (anchor.tail or "")
    if stray.strip():
        return None
    return anchor.get("href")


def strip_nodl_markers(html: str) -> str:
    """Return ``html`` with nodl machine markers removed, structure intact.

    Also drops the bare upload-link paragraph the renderer leaves beside a
    file card (same href, anchor-only ``<p>``) — otherwise the flattened
    preview reads the filename twice, once from the paragraph and once from
    the card's fallback anchor.

    On any parse failure the input is returned unchanged; callers keep their
    own degradation path.
    """
    if not html or not html.strip():
        return html
    try:
        root = lxml.html.fragment_fromstring(html, create_parent="div")

        for comment in list(root.iter(lxml.etree.Comment)):
            if _MARKER_COMMENT_BODY_RE.match(comment.text or ""):
                _remove_preserving_tail(comment)

        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if element.text:
                element.text = strip_marker_text(element.text)
            if element.tail:
                element.tail = strip_marker_text(element.tail)

        card_hrefs = {
            href
            for card in root.find_class("file_card_container")
            for href in [_sole_anchor_href(card)]
            if href
        }
        if card_hrefs:
            for paragraph in list(root.iter("p")):
                href = _sole_anchor_href(paragraph)
                if href in card_hrefs:
                    _remove_preserving_tail(paragraph)

        return (root.text or "") + "".join(
            lxml.html.tostring(child, encoding="unicode") for child in root
        )
    except Exception:
        return html
