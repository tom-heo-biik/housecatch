"""Linters for durumi manuscripts.

lint_document judges the parsed document tree (codes L0xx).
lint_html is a safety net over the rendered HTML (codes H0xx).

Both only judge. They never rewrite a word of the manuscript.
Importing this module has no side effects.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

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
from issues import ERROR, WARN, Issue, excerpt

VS16 = "️"

# What counts as an emoji.
# - The supplementary pictograph planes (mahjong tiles to symbols and pictographs extended-A) are
#   emoji as written.
# - In the Basic Multilingual Plane only characters with the Unicode property Emoji_Presentation=Yes
#   (emoji-data.txt, Unicode 15.1) are emoji as written. Text symbols from the same blocks, such as
#   the star, check mark, heart or the floor and ceiling brackets used in mathematics, are not.
# - Any character followed by VS16 is an emoji (it asks for emoji presentation).
# - The allowed emoji of rules/form.toml count even without VS16, because the renderer adds it.
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # mahjong, cards, enclosed, pictographs, emoticons, transport, symbols
)
# Dingbat negative circled digits are numbering glyphs, not pictographs.
EMOJI_EXCLUDED = ((0x2776, 0x2793),)
BMP_EMOJI_PRESENTATION = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3), (0x25FD, 0x25FE),
    (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693), (0x26A1, 0x26A1),
    (0x26AA, 0x26AB), (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4),
    (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C), (0x274E, 0x274E),
    (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
)

# Defaults only. The rule source is rules/form.toml [manuscript]; these apply when a Rules object
# built in code has no such table. Sentences a model writes about the form instead of for the reader.
META_MARKERS = (
    "[형식 안내",
    "형식을 맞추기 위해",
    "형식에 맞추기 위해",
    "(빈 칸)",
    "(빈칸)",
    "[빈 칸]",
    "[빈칸]",
)

MIN_LIST_ITEMS = 3

# Markdown the manuscript syntax does not have. The parser leaves it as text, so it would be printed
# as raw characters. Judged only; the text is never changed.
MARKDOWN_AT_LINE_HEAD = (
    (re.compile(r"^#{2,}[^\s#]"), "띄어 쓰지 않은 제목 표시 ##"),
    (re.compile(r"^\* "), "* 목록 표시"),
    (re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$"), "구분선"),
)
MARKDOWN_ANYWHERE = (
    (re.compile(r"~~"), "취소선 ~~"),
    (re.compile(r"`"), "코드 표시(백틱)"),
    (re.compile(r"\[[^\[\]\n]+\]\([^()\s]*\)"), "마크다운 링크 [텍스트](주소)"),
    # *text* with no letter or digit glued to the outside, so 2*3*4 is not italic.
    (re.compile(r"(?<![0-9A-Za-z*])\*(?=[^\s*])[^*\n]*?(?<=[^\s*])\*(?![0-9A-Za-z*])"), "기울임 *텍스트*"),
)

# Attributes the renderer emits. Everything else is a stray attribute.
HTML_ALLOWED_ATTRS = {
    "html": {"lang"},
    "meta": {"charset"},
    "span": {"style"},
}
SPAN_STYLE_PROPS = ("color", "background-color", "font-size")
HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
VOID_TAGS = {"br", "meta", "img", "hr", "input", "link", "wbr", "col", "source", "area", "base", "embed", "param", "track"}
LIST_TAGS = ("ul", "ol")
# Standalone <br> counts come from rules/form.toml [spacing]; these are only the fallbacks the
# renderer also uses when a key is missing.
SPACING_DEFAULTS = {"after_title": 1, "before_subheading": 3, "after_subheading": 1, "between_blocks": 1}
SPACING_LABELS = {
    "after_title": "제목 뒤",
    "before_subheading": "소제목 앞",
    "after_subheading": "소제목 뒤",
    "between_blocks": "블록 사이",
}
URL_SCHEME_TAIL = "//"
SPACES = " \t\xa0"


# ---------------------------------------------------------------------------
# Emoji helpers
# ---------------------------------------------------------------------------


def is_emoji_char(ch: str, promoted=frozenset()) -> bool:
    """True for a character that shows as an emoji without VS16 (see EMOJI_RANGES).

    `promoted` holds extra base characters to count, such as the allowed emoji the renderer
    writes with VS16 (the checkbox and the warning sign).
    """
    if ch in promoted:
        return True
    cp = ord(ch)
    for lo, hi in EMOJI_EXCLUDED:
        if lo <= cp <= hi:
            return False
    if any(lo <= cp <= hi for lo, hi in EMOJI_RANGES):
        return True
    return any(lo <= cp <= hi for lo, hi in BMP_EMOJI_PRESENTATION)


def find_emojis(text: str, promoted=frozenset()) -> list[tuple[int, str]]:
    """Return (index, sequence) for each emoji. A trailing VS16 is part of the sequence."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == VS16:
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if is_emoji_char(ch, promoted) or nxt == VS16:
            seq = ch + (VS16 if nxt == VS16 else "")
            out.append((i, seq))
            i += len(seq)
        else:
            i += 1
    return out


def promoted_emoji(form: dict) -> frozenset:
    """Base characters of the allowed emoji, so that a checkbox without VS16 is still judged."""
    return frozenset(emoji_base(e) for e in form.get("emoji", {}).get("allowed", []) if len(emoji_base(e)) == 1)


def emoji_base(seq: str) -> str:
    """Emoji identity with VS16 removed, so that ☑ and ☑️ compare equal."""
    return seq.replace(VS16, "")


def leading_emoji_positions(text: str, emojis) -> set[int]:
    """Indices of emojis that sit at the head of a line, allowing spaces between them."""
    pos = len(text) - len(text.lstrip(SPACES))
    ok = set()
    for index, seq in emojis:
        if index != pos:
            break
        ok.add(index)
        pos = index + len(seq)
        while pos < len(text) and text[pos] in SPACES:
            pos += 1
    return ok


# ---------------------------------------------------------------------------
# Text units of the document tree
# ---------------------------------------------------------------------------


@dataclass
class Unit:
    """One visible line of text with the context the rules need."""

    where: str
    text: str
    in_table: bool = False
    head_ok: bool = True  # False when the text is not at the head of a line (slot text)
    link_spans: list = field(default_factory=list)  # (start, end) of link target text


# Characters with no width. A line made only of these shows as an empty line.
ZERO_WIDTH = "​‌‍⁠﻿"


def _is_blank(text: str) -> bool:
    return not "".join(ch for ch in text if ch not in ZERO_WIDTH).replace("\xa0", " ").strip()


def _inline_lines(inlines) -> list[tuple[str, list]]:
    """Split inlines into lines at LineBreak. Each line is (text, link_spans)."""
    lines = [["", []]]
    for node in inlines:
        cur = lines[-1]
        if isinstance(node, Text):
            cur[0] += node.value
        elif isinstance(node, (Bold, Color, Highlight)):
            cur[0] += node.text
        elif isinstance(node, LinkSlot):
            start = len(cur[0])
            cur[0] += node.target
            cur[1].append((start, len(cur[0])))
        elif isinstance(node, LineBreak):
            lines.append(["", []])
    return [(text, spans) for text, spans in lines]


def _lines_units(prefix: str, inlines, label: str, in_table: bool = False) -> list[Unit]:
    lines = _inline_lines(inlines)
    units = []
    for n, (text, spans) in enumerate(lines, start=1):
        where = prefix if len(lines) == 1 else f"{prefix}, {label} {n}"
        units.append(Unit(where, text, in_table=in_table, link_spans=spans))
    return units


def _block_units(n: int, block) -> list[Unit]:
    prefix = f"블록 {n}"
    if isinstance(block, (Paragraph, Subheading)):
        return _lines_units(prefix, block.inlines, "줄")
    if isinstance(block, Quote):
        units = []
        for k, line in enumerate(block.lines, start=1):
            units.extend(_lines_units(f"{prefix}, 줄 {k}", line, "줄"))
        return units
    if isinstance(block, (ListBlock, Checklist)):
        units = []
        for k, item in enumerate(block.items, start=1):
            units.extend(_lines_units(f"{prefix}, 항목 {k}", item, "줄"))
        return units
    if isinstance(block, Table):
        units = []
        for k, cell in enumerate(block.header, start=1):
            units.extend(_lines_units(f"{prefix}, 머리글 칸 {k}", cell, "줄", in_table=True))
        for r, row in enumerate(block.rows, start=1):
            for k, cell in enumerate(row, start=1):
                units.extend(_lines_units(f"{prefix}, 행 {r} 칸 {k}", cell, "줄", in_table=True))
        return units
    if isinstance(block, ImageSlot):
        return [Unit(prefix, block.desc, head_ok=False)]
    if isinstance(block, LinkBlock):
        return [Unit(prefix, block.target, head_ok=False, link_spans=[(0, len(block.target))])]
    return []


def _char_name(name: str, ch: str) -> str:
    """Name a character for a message without printing the character itself."""
    return name if "U+" in name else f"{name}(U+{ord(ch):04X})"


def _around(text: str, index: int, width: int = 8) -> str:
    return excerpt(text[max(0, index - width): index + width])


# ---------------------------------------------------------------------------
# Document lint
# ---------------------------------------------------------------------------


def _check_punctuation(unit: Unit, form: dict, out: list) -> None:
    punct = form.get("punctuation", {})
    banned = punct.get("banned", {})
    watch = punct.get("watch", {})
    for i, ch in enumerate(unit.text):
        if ch in banned:
            out.append(Issue("L001", ERROR, f"{_char_name(banned[ch], ch)} 문자는 쓰지 않습니다.", unit.where, _around(unit.text, i)))
        elif ch in watch:
            out.append(Issue("L002", WARN, f"{_char_name(watch[ch], ch)} 문자가 있습니다. 의도한 것인지 확인하세요.", unit.where, _around(unit.text, i)))
    allowed_re = re.compile(punct.get("colon_allowed", r"(?<=\d):(?=\d)"))
    allowed = set()
    for m in allowed_re.finditer(unit.text):
        allowed.update(m.start() + k for k, ch in enumerate(m.group(0)) if ch == ":")
    for i, ch in enumerate(unit.text):
        if ch != ":" or i in allowed:
            continue
        in_link = any(start <= i < end for start, end in unit.link_spans)
        if in_link and unit.text.startswith(URL_SCHEME_TAIL, i + 1):
            continue  # URL scheme inside a link slot target
        out.append(Issue("L003", ERROR, "콜론은 숫자 사이(시각, 비율)에만 쓸 수 있습니다.", unit.where, _around(unit.text, i)))


def _check_emoji(unit: Unit, form: dict, out: list) -> None:
    emojis = find_emojis(unit.text, promoted_emoji(form))
    if not emojis:
        return
    spec = form.get("emoji", {})
    allowed = {emoji_base(e) for e in spec.get("allowed", [])}
    table_only = {emoji_base(e) for e in spec.get("table_only", [])}
    outside_only = {emoji_base(e) for e in spec.get("outside_table_only", [])}
    head = leading_emoji_positions(unit.text, emojis) if unit.head_ok else set()
    for index, seq in emojis:
        base = emoji_base(seq)
        ctx = _around(unit.text, index)
        if base not in allowed:
            out.append(Issue("L010", ERROR, f"허용 목록 밖의 이모지({seq})입니다.", unit.where, ctx))
            continue
        if index not in head:
            out.append(Issue("L011", ERROR, f"이모지({seq})는 줄, 항목, 칸, 소제목의 첫머리에만 쓸 수 있습니다.", unit.where, ctx))
        if base in table_only and not unit.in_table:
            out.append(Issue("L012", ERROR, f"이모지({seq})는 표 안에서만 씁니다.", unit.where, ctx))
        if base in outside_only and unit.in_table:
            out.append(Issue("L013", ERROR, f"이모지({seq})는 표 밖에서만 씁니다.", unit.where, ctx))


def _check_phrases(unit: Unit, brand, out: list) -> None:
    if brand is None:
        return
    for phrase in brand.banned_phrases:
        if phrase and phrase in unit.text:
            out.append(Issue("L030", ERROR, f"브랜드 금지 문자열 '{phrase}'에 걸렸습니다.", unit.where, excerpt(phrase)))
    for phrase in brand.watch_phrases:
        if phrase and phrase in unit.text:
            out.append(Issue("L031", WARN, f"브랜드 주의 문자열 '{phrase}'에 걸렸습니다.", unit.where, excerpt(phrase)))


def _check_markdown(unit: Unit, out: list) -> None:
    found = []
    if unit.head_ok:
        stripped = unit.text.strip(SPACES)
        for pattern, name in MARKDOWN_AT_LINE_HEAD:
            if pattern.search(stripped):
                found.append((unit.text.find(stripped[:1]) if stripped else 0, name))
    for pattern, name in MARKDOWN_ANYWHERE:
        match = pattern.search(unit.text)
        if match:
            found.append((match.start(), name))
    for index, name in found:
        out.append(Issue("L070", ERROR, f"원고 문법에 없는 마크다운 표시({name})가 남았습니다. 이런 표시는 글자 그대로 찍힙니다.",
                         unit.where, _around(unit.text, index, 10)))


def _manuscript(rules) -> dict:
    return (getattr(rules, "form", None) or {}).get("manuscript", {})


def _check_meta(unit: Unit, out: list, markers=META_MARKERS) -> None:
    for marker in markers:
        index = unit.text.find(marker)
        if index >= 0:
            out.append(Issue("L050", ERROR, f"독자가 아니라 형식을 향한 문장('{marker}')이 있습니다.", unit.where, _around(unit.text, index, 12)))


def _check_palettes(doc, rules, brand, out: list) -> None:
    max_palettes = int(rules.form.get("max_palettes", 2))
    keys = tuple(doc.palettes)
    if not 1 <= len(keys) <= max_palettes:
        out.append(Issue("L020", ERROR, f"팔레트는 1개에서 {max_palettes}개까지 고릅니다(지금 {len(keys)}개).", "팔레트", excerpt(", ".join(keys))))
    for key in keys:
        if key not in rules.palettes:
            out.append(Issue("L021", ERROR, f"rules/palettes.toml에 없는 팔레트 이름입니다({key}).", "팔레트", excerpt(key)))
    if len(set(keys)) != len(keys):
        out.append(Issue("L021", ERROR, "같은 팔레트를 두 번 골랐습니다.", "팔레트", excerpt(", ".join(keys))))
    if brand is not None and brand.palettes and keys != tuple(brand.palettes):
        out.append(Issue("L022", ERROR, f"브랜드 고정 팔레트({', '.join(brand.palettes)})와 다릅니다.", "팔레트", excerpt(", ".join(keys))))


def _inline_groups(block):
    """(where suffix, inlines) for every inline sequence in a block."""
    if isinstance(block, (Paragraph, Subheading)):
        yield "", block.inlines
    elif isinstance(block, Quote):
        for k, line in enumerate(block.lines, start=1):
            yield f", 줄 {k}", line
    elif isinstance(block, (ListBlock, Checklist)):
        for k, item in enumerate(block.items, start=1):
            yield f", 항목 {k}", item
    elif isinstance(block, Table):
        for k, cell in enumerate(block.header, start=1):
            yield f", 머리글 칸 {k}", cell
        for r, row in enumerate(block.rows, start=1):
            for k, cell in enumerate(row, start=1):
                yield f", 행 {r} 칸 {k}", cell


def _check_inlines(n: int, block, palette_count: int, max_palettes: int, out: list) -> None:
    for suffix, inlines in _inline_groups(block):
        where = f"블록 {n}{suffix}"
        for node in inlines:
            if isinstance(node, (Color, Highlight)):
                mark = ("c" if isinstance(node, Color) else "h") + str(node.role)
                if node.role < 1 or node.role > max_palettes:
                    out.append(Issue("L023", ERROR, f"{{{mark}:...}} 표시는 없는 역할 번호입니다.", where, excerpt(node.text)))
                elif node.role > palette_count:
                    out.append(Issue("L023", ERROR, f"{{{mark}:...}} 표시는 팔레트를 {node.role}개 골랐을 때만 씁니다(지금 {palette_count}개).", where, excerpt(node.text)))
            if isinstance(node, (Bold, Color, Highlight)) and _is_blank(node.text):
                out.append(Issue("L042", ERROR, "강조 표시 안이 비었습니다.", where, ""))
            if isinstance(node, LinkSlot) and _is_blank(node.target):
                out.append(Issue("L042", ERROR, "링크 자리의 대상이 비었습니다.", where, ""))


def _check_empty(n: int, block, out: list) -> None:
    where = f"블록 {n}"

    def empty_block(what: str) -> None:
        out.append(Issue("L040", ERROR, f"빈 {what}입니다.", where, ""))

    def empty_part(suffix: str, what: str) -> None:
        out.append(Issue("L041", ERROR, f"빈 {what}입니다.", where + suffix, ""))

    if isinstance(block, (Paragraph, Subheading)):
        lines = _inline_lines(block.inlines)
        if all(_is_blank(text) for text, _ in lines):
            empty_block("문단" if isinstance(block, Paragraph) else "소제목")
        else:
            for k, (text, _) in enumerate(lines, start=1):
                if _is_blank(text):
                    empty_part(f", 줄 {k}", "줄")
    elif isinstance(block, Quote):
        texts = ["".join(t for t, _ in _inline_lines(line)) for line in block.lines]
        if not texts or all(_is_blank(t) for t in texts):
            empty_block("인용")
        else:
            for k, t in enumerate(texts, start=1):
                if _is_blank(t):
                    empty_part(f", 줄 {k}", "줄")
    elif isinstance(block, (ListBlock, Checklist)):
        what = "목록" if isinstance(block, ListBlock) else "체크리스트"
        texts = ["".join(t for t, _ in _inline_lines(item)) for item in block.items]
        if not texts or all(_is_blank(t) for t in texts):
            empty_block(what)
        else:
            for k, t in enumerate(texts, start=1):
                if _is_blank(t):
                    empty_part(f", 항목 {k}", "항목")
    elif isinstance(block, Table):
        if not block.header or not block.rows:
            empty_block("표")
        for suffix, cell in _inline_groups(block):
            if _is_blank("".join(t for t, _ in _inline_lines(cell))):
                empty_part(suffix, "칸")
    elif isinstance(block, ImageSlot):
        if _is_blank(block.desc):
            empty_block("이미지 자리 설명")
    elif isinstance(block, LinkBlock):
        if _is_blank(block.target):
            empty_block("링크 자리 대상")


def lint_document(doc, rules, brand) -> list[Issue]:
    """Judge a parsed document against rules/form.toml, palettes and the brand's phrases."""
    manuscript = _manuscript(rules)
    markers = tuple(manuscript.get("meta_markers", META_MARKERS))
    min_items = int(manuscript.get("list_min_items", MIN_LIST_ITEMS))
    form = rules.form
    out: list[Issue] = []
    _check_palettes(doc, rules, brand, out)

    if _is_blank(doc.title):
        out.append(Issue("L040", ERROR, "제목이 비었습니다.", "제목", ""))
    if not doc.blocks:
        out.append(Issue("L040", ERROR, "본문이 비었습니다.", "본문", ""))

    units = [Unit("제목", doc.title)]
    for n, block in enumerate(doc.blocks, start=1):
        _check_empty(n, block, out)
        _check_inlines(n, block, len(doc.palettes), int(form.get("max_palettes", 2)), out)
        if isinstance(block, ListBlock) and 0 < len(block.items) < min_items:
            out.append(Issue("L060", WARN, f"목록 항목이 {len(block.items)}개입니다. 셋 이상이 나란히 설 때 목록으로 씁니다.", f"블록 {n}", ""))
        units.extend(_block_units(n, block))

    for unit in units:
        _check_punctuation(unit, form, out)
        _check_emoji(unit, form, out)
        _check_phrases(unit, brand, out)
        _check_meta(unit, out, markers)
        _check_markdown(unit, out)
    return out


# ---------------------------------------------------------------------------
# HTML lint
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    tag: str  # "#text" for text nodes
    attrs: list = field(default_factory=list)
    children: list = field(default_factory=list)
    parent: "_Node | None" = None
    text: str = ""
    line: int = 0
    raw: str = ""

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def text_content(self) -> str:
        if self.tag == "#text":
            return self.text
        return "".join(child.text_content() for child in self.children)


class _TreeBuilder(HTMLParser):
    """Minimal tree. Comments and declarations are dropped; <style> content stays out of the tree."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.cur = self.root
        self._raw_text = False

    def _add(self, tag, attrs) -> _Node:
        node = _Node(tag, list(attrs), parent=self.cur, line=self.getpos()[0], raw=self.get_starttag_text() or f"<{tag}>")
        self.cur.children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._add(tag, attrs)
        if tag in ("style", "script"):
            self._raw_text = True
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self._add(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._raw_text = False
        if tag in VOID_TAGS:
            return
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        if self._raw_text:
            self.cur.text += data  # CSS or script body is kept as text of its element, not as markup
            return
        self.cur.children.append(_Node("#text", parent=self.cur, text=data, line=self.getpos()[0]))


def _parse_style(value: str) -> list[tuple[str, str, bool]]:
    """(property, value, well_formed) for each declaration."""
    out = []
    for part in (value or "").split(";"):
        if not part.strip():
            continue
        if ":" not in part:
            out.append((part.strip().lower(), "", False))
            continue
        prop, val = part.split(":", 1)
        out.append((prop.strip().lower(), val.strip(), True))
    return out


def _is_blank_node(node: _Node) -> bool:
    return node.tag == "#text" and _is_blank(node.text)


def _element_children(node: _Node) -> list:
    return [child for child in node.children if not _is_blank_node(child)]


def _only_child(parent: _Node | None, child: _Node) -> bool:
    return parent is not None and _element_children(parent) == [child]


def _subheading_span(span: _Node) -> bool:
    """The renderer's subheading shape: <p><strong><span style="font-size:..">...</span></strong></p>."""
    strong = span.parent
    if strong is None or strong.tag != "strong" or not _only_child(strong, span):
        return False
    p = strong.parent
    return p is not None and p.tag == "p" and _only_child(p, strong)


def _is_subheading_block(node: _Node, font_size: str) -> bool:
    if node.tag != "p":
        return False
    kids = _element_children(node)
    if len(kids) != 1 or kids[0].tag != "strong":
        return False
    inner = _element_children(kids[0])
    if len(inner) != 1 or inner[0].tag != "span":
        return False
    style = {prop: val for prop, val, _ in _parse_style(dict(inner[0].attrs).get("style") or "")}
    return style.get("font-size", "").replace(" ", "").lower() == font_size


def _placeholder_prefixes(form: dict) -> tuple:
    """Fixed words that open each placeholder template, such as '이미지를 첨부하세요: '."""
    out = []
    for template in form.get("placeholder", {}).values():
        if not isinstance(template, str):
            continue
        match = re.search(r"\{color\}[^>]*>([^{<]*)", template)
        if match and match.group(1).strip():
            out.append(html_lib.unescape(match.group(1)))
    return tuple(out)


def _style_body_font_size(style: str) -> str | None:
    match = re.search(r"(?:^|[};\s])body\s*\{([^}]*)\}", style or "")
    if not match:
        return None
    for prop, val, ok in _parse_style(match.group(1)):
        if ok and prop == "font-size":
            return val.replace(" ", "").lower()
    return None


def _check_style(elements: list, form: dict, out: list) -> None:
    """The <style> element must be the rules' style, and the rules must agree with themselves."""
    expected = form.get("html", {}).get("style")
    if expected is None:
        return
    styles = [n for n in elements if n.tag == "style"]
    if len(styles) != 1 or styles[0].text != expected:
        where = f"줄 {styles[0].line}" if styles else "문서"
        got = styles[0].text if styles else ""
        out.append(Issue("H014", ERROR, "문서의 <style>이 규칙(rules/form.toml [html] style)과 다릅니다.", where, excerpt(got)))
    body_px = form.get("body_font_px")
    if body_px is not None:
        size = _style_body_font_size(expected)
        if size != f"{body_px}px":
            out.append(Issue("H015", ERROR, f"규칙끼리 어긋납니다. body_font_px는 {body_px}px인데 [html] style의 본문 글자 크기는 "
                                            f"{size or '없음'}입니다.", "rules/form.toml", excerpt(size or "")))


def lint_html(html: str, rules) -> list[Issue]:
    """Safety net over rendered HTML: tags, attributes, span styles, colors and spacing."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    root = builder.root
    elements = [n for n in root.iter() if n.tag not in ("#text", "#root")]
    out: list[Issue] = []

    allowed_tags = set(rules.form.get("html", {}).get("allowed_tags", []))
    font_size = f"{rules.form.get('subheading_font_px', 24)}px"
    text_colors = {p.text.upper(): key for key, p in rules.palettes.items()}
    highlight_colors = {p.highlight.upper(): key for key, p in rules.palettes.items()}
    placeholder = rules.placeholder.upper()
    max_palettes = int(rules.form.get("max_palettes", 2))
    prefixes = _placeholder_prefixes(rules.form)
    used_palettes: dict[str, None] = {}

    for node in elements:
        where = f"줄 {node.line}"
        raw = excerpt(node.raw)
        if node.tag not in allowed_tags:
            out.append(Issue("H001", ERROR, f"허용하지 않는 태그 <{node.tag}>입니다.", where, raw))
        for name, value in node.attrs:
            if name == "start":
                out.append(Issue("H009", ERROR, "start 속성은 보존되지 않습니다.", where, raw))
            elif name not in HTML_ALLOWED_ATTRS.get(node.tag, set()):
                out.append(Issue("H002", ERROR, f"<{node.tag}>에 허용하지 않는 속성이 있습니다({name}).", where, raw))
            elif node.tag == "span" and name == "style":
                for prop, val, ok in _parse_style(value or ""):
                    if not ok or prop not in SPAN_STYLE_PROPS:
                        out.append(Issue("H003", ERROR, f"span의 style에는 {', '.join(SPAN_STYLE_PROPS)}만 씁니다.", where, excerpt(f"{prop}:{val}")))
                    elif prop == "font-size":
                        if val.replace(" ", "").lower() != font_size:
                            out.append(Issue("H004", ERROR, f"글자 크기는 {font_size}만 지정합니다.", where, excerpt(val)))
                        elif not _subheading_span(node):
                            out.append(Issue("H018", ERROR, f"글자 크기 {font_size}는 소제목 모양(<p><strong><span>)에서만 씁니다.",
                                             where, excerpt(node.text_content())))
                    elif not HEX6.match(val):
                        out.append(Issue("H005", ERROR, "색은 6자리 hex(#RRGGBB)로 씁니다.", where, excerpt(val)))
                    else:
                        color = val.upper()
                        if prop == "color" and color in text_colors:
                            used_palettes.setdefault(text_colors[color])
                            if node.parent is None or node.parent.tag != "strong":
                                out.append(Issue("H016", ERROR, "글자 색은 굵게(<strong>) 바로 안에서만 씁니다.",
                                                 where, excerpt(node.text_content())))
                        elif prop == "color" and color == placeholder:
                            if prefixes and not node.text_content().startswith(prefixes):
                                out.append(Issue("H017", ERROR, "플레이스홀더 색은 이미지와 링크 자리에만 씁니다.",
                                                 where, excerpt(node.text_content())))
                        elif prop == "background-color" and color in highlight_colors:
                            used_palettes.setdefault(highlight_colors[color])
                        else:
                            role = "글자 색" if prop == "color" else "형광펜 색"
                            out.append(Issue("H006", ERROR, f"팔레트의 {role}이 아닌 색입니다.", where, excerpt(f"{prop}:{val}")))

    if len(used_palettes) > max_palettes:
        out.append(Issue("H013", ERROR, f"팔레트를 {len(used_palettes)}개 썼습니다. {max_palettes}개까지 씁니다.", "문서", excerpt(", ".join(used_palettes))))

    h1s = [n for n in elements if n.tag == "h1"]
    if len(h1s) != 1:
        out.append(Issue("H007", ERROR, f"h1은 정확히 하나여야 합니다(지금 {len(h1s)}개).", "문서", ""))

    for node in elements:
        if node.tag == "p" and _is_blank(node.text_content()):
            out.append(Issue("H008", ERROR, "빈 문단 <p>가 있습니다. 간격은 <br>로 만듭니다.", f"줄 {node.line}", excerpt(node.raw)))
        if node.tag in LIST_TAGS and any(a.tag in LIST_TAGS for a in node.ancestors()):
            out.append(Issue("H010", ERROR, f"목록 안에 목록이 중첩되었습니다(<{node.tag}>).", f"줄 {node.line}", excerpt(node.raw)))

    _check_style(elements, rules.form, out)

    body = next((n for n in elements if n.tag == "body"), root)
    spacing = {**SPACING_DEFAULTS, **{k: int(v) for k, v in rules.form.get("spacing", {}).items()}}
    _check_br_runs(body, spacing, lambda node: _is_subheading_block(node, font_size), out)
    return out


def _expected_gap(blocks: list, k: int, is_sub) -> str:
    """Spacing key before block k, mirroring render._Renderer.gap (blocks[0] is the h1)."""
    if is_sub(blocks[k]):
        return "before_subheading"
    if k == 1:
        return "after_title"
    if is_sub(blocks[k - 1]):
        return "after_subheading"
    return "between_blocks"


def _check_br_runs(body: _Node, spacing: dict, is_sub, out: list) -> None:
    """Standalone <br> runs directly under body, counted against rules/form.toml [spacing].

    With the h1 first, each gap must equal the spacing value for its position (after the title,
    before a subheading, after a subheading, between other blocks). Without a leading h1 the
    positions are unknown and any spacing value is accepted. No runs at either end.
    """
    seq = [child for child in body.children if not _is_blank_node(child)]
    index = [i for i, node in enumerate(seq) if node.tag != "br"]
    if not index:
        return
    first, last = index[0], index[-1]
    if first > 0:
        out.append(Issue("H012", ERROR, f"본문 앞에 줄바꿈 {first}개가 남았습니다.", f"줄 {seq[0].line}", ""))
    if last < len(seq) - 1:
        out.append(Issue("H012", ERROR, f"본문 끝에 줄바꿈 {len(seq) - 1 - last}개가 남았습니다.", f"줄 {seq[last + 1].line}", ""))
    blocks = [seq[i] for i in index]
    positional = blocks[0].tag == "h1"
    allowed = sorted(set(spacing.values()))
    for k, (a, b) in enumerate(zip(index, index[1:]), start=1):
        count = b - a - 1
        where = f"줄 {seq[a + 1].line if count else seq[b].line}"
        ctx = excerpt(seq[b].raw or seq[b].text)
        if positional:
            key = _expected_gap(blocks, k, is_sub)
            if count != spacing[key]:
                out.append(Issue("H011", ERROR, f"{SPACING_LABELS[key]} 줄바꿈이 {count}개입니다. 이 자리에는 {spacing[key]}개를 씁니다.",
                                 where, ctx))
        elif count not in allowed:
            names = ", ".join(str(n) for n in allowed)
            out.append(Issue("H011", ERROR, f"블록 사이 줄바꿈이 {count}개입니다. {names}개 가운데 하나만 씁니다.", where, ctx))
