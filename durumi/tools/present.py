"""The final text as Markdown, for showing a post in a Claude Code session. Deterministic.

A session outside the monorepo does not save the post into brands/; the run prints this instead,
and the model relays it. Marks become what reads best in a chat: bold and text color both show as
bold (the renderer makes colored text bold too), a highlight shows as its plain words, and image
and link slots stay as the brackets a person fills. The HTML for pasting is still in the run folder.

The post's own text is escaped so that no renderer reads it as Markdown: CommonMark and
GitHub-flavored renderers (the VS Code extension) and marked (the Claude Code terminal) disagree at
the edges, so the rules below take the safe side of each.

Importing this module has no side effects.
"""
from __future__ import annotations

import re
import string
import unicodedata

from document import (
    Bold,
    Checklist,
    Color,
    Highlight,
    ImageSlot,
    LineBreak,
    LinkBlock,
    LinkSlot,
    ListBlock,
    Paragraph,
    Quote,
    Subheading,
    Table,
    Text,
)

# The post's own characters that Markdown would read as marks inside a line: a single ~ strikes
# through in GitHub-flavored renderers ('월~금 2시~6시'), < opens HTML, & an entity, | a table.
INLINE_MARK = re.compile(r"([\\`*_~<&|])")
# Addresses stay as written, since autolinks do not undo backslashes. An http(s) address goes in
# <...> so that it ends where it ends, not at the next space ('...example_link]에서' would all link).
URL = re.compile(r"(https?://[!-;=?-~]+|www\.[!-;=?-~]+)")  # printable ASCII but < and >; Korean ends it
URL_TAIL = ".,:;!?)]}'\""
# At the start of a line: a setext underline or a thematic break, an ordered list, a heading, a
# quote, a bullet. '2026. 11. 3.' would become a list, '====' would turn the line above into a title.
UNDERLINE = re.compile(r"(?m)^([=\-]+[ \t]*)$")
ORDERED_START = re.compile(r"(?m)^(\d{1,9})([.)])(?=\s|$)")
BLOCK_START = re.compile(r"(?m)^(#{1,6}(?=\s|$)|>|[-+](?=\s|$))")
LEADING_SPACE = re.compile(r"(?m)^[ \t]+")  # four would make a code block; HTML folds them anyway
CLOSING_HASHES = re.compile(r"(^|\s)(#+)[ \t]*$")  # a heading's trailing ' #' would be read as its end
MASKED = re.compile(r"<[^<>]*>")


def _escape(text: str) -> str:
    parts = URL.split(text)  # addresses at the odd places
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(INLINE_MARK.sub(r"\\\1", part))
            continue
        url = part.rstrip(URL_TAIL)
        tail = part[len(url):]
        out.append(f"<{url}>" if url.startswith(("http://", "https://")) else url)
        out.append(INLINE_MARK.sub(r"\\\1", tail))
    return "".join(out)


def _line_starts(text: str) -> str:
    """Escape whatever would open a block at the start of each line of text."""
    text = UNDERLINE.sub(r"\\\1", LEADING_SPACE.sub("", text))
    return BLOCK_START.sub(r"\\\1", ORDERED_START.sub(r"\1\\\2", text))


def _heading(text: str) -> str:
    """Heading text: a trailing run of # stays text instead of closing the heading."""
    return CLOSING_HASHES.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", text)


def _punct(ch: str) -> bool:
    """Punctuation or symbol, as CommonMark 0.31 counts them for emphasis."""
    return unicodedata.category(ch)[0] in "PS"


def _word(ch: str) -> bool:
    """A character beside a ** that every renderer treats like a letter; '' (a line's edge) is not one.

    CommonMark 0.31 counts every punctuation and symbol, GitHub's 0.29 only ASCII punctuation, and
    marked sees an emoji outside the BMP as half a surrogate pair, so only spaces and ASCII
    punctuation are trusted as non-letters.
    """
    return bool(ch) and not ch.isspace() and ch not in string.punctuation


def _strong(text: str, before: str, after: str) -> str:
    """text as **bold** that renderers open and close, given the characters around it.

    A ** with a space just inside never counts, so edge spaces go outside. Punctuation just inside
    counts only when the character outside is a space, ASCII punctuation or a line's edge, so edge
    punctuation goes outside only when anything else sits beyond it: '**확인하세요.**다음' would
    show its asterisks, while '**- 항목**' at a line's start must stay whole or it becomes a list.
    An emoji keeps its variation selector (U+FE0F) and joiners with it.
    """
    start, end = 0, len(text)
    while True:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        outside = text[start - 1] if start else before
        if start < end and _punct(text[start]) and _word(outside):
            start += 1
            while start < end and (_punct(text[start]) or unicodedata.category(text[start]) in ("Mn", "Me", "Cf")):
                start += 1
            continue
        outside = text[end] if end < len(text) else after
        if start < end and _punct(text[end - 1]) and _word(outside):
            end -= 1
            while end > start and _punct(text[end - 1]):
                end -= 1
            continue
        break
    if start == end:
        return _escape(text)
    return f"{_escape(text[:start])}**{_escape(text[start:end])}**{_escape(text[end:])}"


def _runs(inlines) -> list[tuple[str, str]]:
    """(kind, text) runs in order; bold and colored runs next to each other become one, or '****' appears."""
    runs: list[tuple[str, str]] = []
    for node in inlines:
        if isinstance(node, (Bold, Color)):
            if runs and runs[-1][0] == "bold":
                runs[-1] = ("bold", runs[-1][1] + node.text)
            else:
                runs.append(("bold", node.text))
        elif isinstance(node, (Text, Highlight)):
            runs.append(("text", node.value if isinstance(node, Text) else node.text))
        elif isinstance(node, LinkSlot):
            runs.append(("slot", f"[링크: {_escape(node.target)}]"))
        elif isinstance(node, LineBreak):
            runs.append(("break", "  \n"))
    return runs


def _inline(inlines) -> str:
    runs = _runs(inlines)
    # marked in the Claude Code terminal hides any <...> span before it reads marks, backslashes or
    # not, so bold that such a span would cut shows its asterisks: that text goes out plain.
    plain = any(kind == "bold" for kind, _ in runs) and MASKED.search("".join(text for _, text in runs))
    parts: list[str] = []
    for i, (kind, text) in enumerate(runs):
        if kind == "bold" and plain:
            parts.append(_escape(text))
        elif kind == "bold":
            before = parts[-1][-1:] if parts else ""
            after = next((t[:1] for _, t in runs[i + 1:] if t), "")
            parts.append(_strong(text, before, after))
        else:
            parts.append(_escape(text) if kind == "text" else text)
    # A slot followed by '(' would read as a link: [링크: 상담 페이지](...).
    return _line_starts("".join(parts)).replace("](", "]\\(")


def _cell(inlines) -> str:
    return _inline(inlines).replace("  \n", " ")  # the text's own | is escaped already


def _block(block, marker: str | None = None) -> str:
    if isinstance(block, Paragraph):
        return _inline(block.inlines)
    if isinstance(block, Subheading):
        return f"## {_heading(_inline(block.inlines))}"
    if isinstance(block, Quote):
        return "\n".join(f"> {_inline(line)}" for line in block.lines)
    if isinstance(block, ListBlock):
        return "\n".join(f"{n}{marker} {_inline(item)}" if block.ordered else f"{marker} {_inline(item)}"
                         for n, item in enumerate(block.items, start=1))
    if isinstance(block, Checklist):
        return "\n".join(f"{marker} ☑️ {_inline(item)}" for item in block.items)
    if isinstance(block, Table):
        head = "| " + " | ".join(_cell(c) for c in block.header) + " |"
        rule = "|" + " --- |" * len(block.header)
        rows = ["| " + " | ".join(_cell(c) for c in row) + " |" for row in block.rows]
        return "\n".join([head, rule] + rows)
    if isinstance(block, ImageSlot):
        return f"[이미지: {_escape(block.desc)}]"
    if isinstance(block, LinkBlock):
        return f"[링크: {_escape(block.target)}]"
    return ""


def _marker(block, last: str | None) -> str | None:
    """A list's marker. Two lists in a row with one marker would merge into one (and keep counting),
    so the marker alternates: - and * for bullets and checklists, . and ) for numbers."""
    if isinstance(block, ListBlock) and block.ordered:
        choices = (".", ")")
    elif isinstance(block, (ListBlock, Checklist)):
        choices = ("-", "*")
    else:
        return None
    return choices[1] if last == choices[0] else choices[0]


def as_markdown(doc) -> str:
    """Title as a level 1 heading, then every block, separated by blank lines."""
    title = _heading(_escape(doc.title).replace("](", "]\\("))
    chunks = [f"# {title}"]
    last = None
    for block in doc.blocks:
        last = _marker(block, last)
        chunk = _block(block, last)
        if chunk:
            chunks.append(chunk)
    return "\n\n".join(chunks) + "\n"
