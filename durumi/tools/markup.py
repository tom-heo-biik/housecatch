"""Manuscript parser: node envelope (slug, title, palettes, body) to a Document tree.

The parser never rewrites words. It only splits the body into blocks and inline marks
and reports problems. If any error is found the Document is None.

Issue codes
  Envelope (E0xx)
    E001 envelope is not an object
    E002 slug missing or not a string
    E003 slug does not match ^[a-z0-9]+(-[a-z0-9]+)*$
    E004 slug length outside 3..60
    E005 title missing, not a string or blank
    E006 title spans more than one line
    E007 palettes missing or not a list (brand without fixed palettes)
    E008 palette count outside 1..max_palettes
    E009 unknown palette key
    E010 duplicate palette key
    E011 body missing, not a string or blank
    E012 palettes given although the brand fixes them (info, envelope value ignored)
    E013 unknown envelope key (warn)
    E014 brand.toml palettes invalid
  Parsing (P0xx)
    P001 first block is not a paragraph (rules.form['title_first_block'])
    P002 indented list line (nesting is not supported)
    P003 unmatched **
    P004 unmatched { or }
    P005 nested inline marks
    P006 unknown role such as c3 or h0
    P007 c2 or h2 used with a single palette
    P008 table row cell count differs from the header
    P009 empty image description
    P010 malformed table (no separator line, no body rows)
    P011 block mixes line kinds (lists, quotes, tables need their own block)
    P012 malformed quote line
    P013 subheading is not a single non-empty line
    P014 image slot not on its own line and block
    P015 malformed or empty link slot
    P016 empty inline mark
    P017 heading marker other than ##
    P018 empty list, checklist or quote item
    P019 ordered list numbers not 1, 2, 3, ... or an item that reads like a date (9. 30.)
    P020 line breaks written as a backslash and n (a doubly escaped JSON string). Error when the
         body has no real line break or the two characters stand where a block would start (a
         blank line, ##, -, >, |, a slot, a number and a period); the blocks are then not parsed.
         Warning otherwise, since a post may show the two characters on purpose.
"""
from __future__ import annotations

import re

from document import (  # noqa: F401  re-exported so callers can use markup.Text etc.
    Block,
    Bold,
    Checklist,
    Color,
    Document,
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
from issues import ERROR, INFO, WARN, Issue, excerpt

# Matched with fullmatch: with re.match, `$` also matches before a trailing newline.
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SLUG_MIN = 3
SLUG_MAX = 60
ENVELOPE_KEYS = ("slug", "title", "palettes", "body")

VS16 = "️"
CHECK_BASE = "☑"  # ballot box with check, rendered with VS16

# Line kinds
K_TEXT = "text"
K_SUB = "subheading"
K_HEADING_OTHER = "heading_other"
K_QUOTE = "quote"
K_QUOTE_BAD = "quote_bad"
K_UL = "ul"
K_OL = "ol"
K_CHECK = "check"
K_TABLE = "table"
K_IMAGE = "image"
K_LINK = "link"
K_INDENTED_LIST = "indented_list"

SUB_RE = re.compile(r"^##(?: (.*))?$")
HEADING_OTHER_RE = re.compile(r"^(?:#|#{3,6})(?: |$)")
QUOTE_RE = re.compile(r"^>(?: (.*))?$")
UL_RE = re.compile(r"^-(?: (.*))?$")
# One or two digits only, so dates such as "2026. 9. 30." stay paragraphs. A month-first date
# ("9. 30.부터") still looks like a list line; P019 stops it instead of dropping the "9.".
OL_RE = re.compile(r"^(\d{1,2})\.(?: (.*))?$")
# An ordered item whose text starts with a number and a period reads like the day of a date.
OL_DATE_TAIL_RE = re.compile(r"^\d{1,2}\.(?!\d)")
CHECK_RE = re.compile(r"^☑️?(?: (.*))?$")
IMAGE_RE = re.compile(r"^\[이미지:(.*)\]$")
LINK_RE = re.compile(r"^\[링크:(.*)\]$")
INDENTED_LIST_RE = re.compile(r"^[ \t　]+(?:-|\d{1,3}\.|\*|☑️?)(?: |$)")
TABLE_SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")
ROLE_RE = re.compile(r"^([ch])([0-9]+):(.*)$", re.DOTALL)
# A backslash and n as two characters: what is left of a line break escaped twice in the JSON.
ESCAPED_NEWLINE = "\\n"
# The two characters where a new block would begin: another one (a blank line) or a block marker.
ESCAPED_BLOCK_START_RE = re.compile(r"\\n(?:\\n|## |- |> |\||\[이미지:|\[링크:|\d{1,2}\. |☑)")

KIND_NAMES = {
    K_TEXT: "문단",
    K_SUB: "소제목",
    K_QUOTE: "인용",
    K_UL: "순서 없는 목록",
    K_OL: "순서 있는 목록",
    K_CHECK: "체크리스트",
    K_TABLE: "표",
    K_IMAGE: "이미지 자리",
    K_LINK: "링크 자리",
}


class _Collector:
    """Accumulates issues with a shared shape."""

    def __init__(self):
        self.issues: list[Issue] = []

    def add(self, code, message, where="", text="", severity=ERROR):
        self.issues.append(Issue(code=code, severity=severity, message=message, where=where, excerpt=excerpt(text)))


def _where(block_no: int, line_no: int | None = None) -> str:
    return f"블록 {block_no}" if line_no is None else f"블록 {block_no}, 줄 {line_no}"


# Envelope


def _check_envelope(envelope, rules, brand, out: _Collector):
    """Return (slug, title, palettes, body) with None for invalid parts."""
    if not isinstance(envelope, dict):
        out.add("E001", "봉투는 JSON 객체여야 합니다.", "봉투", repr(envelope))
        return None, None, None, None

    for key in sorted(k for k in envelope if k not in ENVELOPE_KEYS):
        out.add("E013", f"봉투에 알 수 없는 항목 '{key}'가 있어 무시했습니다.", "봉투", str(key), severity=WARN)

    slug = envelope.get("slug")
    if not isinstance(slug, str):
        out.add("E002", "slug가 없거나 문자열이 아닙니다.", "봉투 slug", repr(slug))
        slug = None
    else:
        ok = True
        if not SLUG_RE.fullmatch(slug):
            out.add("E003", "slug는 영문 소문자와 숫자를 하이픈 하나로 이은 형태여야 합니다(예 winter-break-math).",
                    "봉투 slug", slug)
            ok = False
        if not SLUG_MIN <= len(slug) <= SLUG_MAX:
            out.add("E004", f"slug 길이는 {SLUG_MIN}자 이상 {SLUG_MAX}자 이하여야 합니다(지금 {len(slug)}자).",
                    "봉투 slug", slug)
            ok = False
        if not ok:
            slug = None

    title = envelope.get("title")
    if not isinstance(title, str) or not title.strip():
        out.add("E005", "title이 없거나 비어 있습니다.", "봉투 title", repr(title))
        title = None
    elif len(title.strip().splitlines()) > 1:
        out.add("E006", "title은 한 줄이어야 합니다.", "봉투 title", title)
        title = None
    else:
        title = title.strip()

    palettes = _check_palettes(envelope, rules, brand, out)

    body = envelope.get("body")
    if not isinstance(body, str) or not body.strip():
        out.add("E011", "body가 없거나 비어 있습니다.", "봉투 body", repr(body))
        body = None

    # normalize turns a lone surrogate half into U+FFFD so the run can save it; the text itself is
    # broken, and only the node knows which character it meant.
    for key, value in (("title", title), ("body", body)):
        if value is not None and "�" in value:
            at = value.index("�")
            out.add("E015", f"{key}에 읽을 수 없는 글자(U+FFFD)가 있습니다. 이모지 같은 글자를 \\u 이스케이프 반쪽으로 "
                            "쓰지 말고 글자 그대로 쓰십시오.", f"봉투 {key}", value[max(0, at - 20):at + 20])

    return slug, title, palettes, body


def _check_palettes(envelope, rules, brand, out: _Collector):
    max_palettes = int(rules.form.get("max_palettes", 2))
    fixed = getattr(brand, "palettes", None) if brand is not None else None
    if fixed:
        if "palettes" in envelope:
            out.add("E012", "브랜드에 고정 팔레트가 있어 봉투의 palettes를 무시했습니다.", "봉투 palettes",
                    repr(envelope.get("palettes")), severity=INFO)
        fixed = tuple(fixed)
        bad = [key for key in fixed if key not in rules.palettes]
        if bad or not 1 <= len(fixed) <= max_palettes or len(set(fixed)) != len(fixed):
            out.add("E014", f"brand.toml의 palettes가 올바르지 않습니다. palettes.toml의 키로 1개 이상 "
                            f"{max_palettes}개 이하, 중복 없이 적어야 합니다.", "brand.toml palettes", repr(list(fixed)))
            return None
        return fixed

    value = envelope.get("palettes")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        out.add("E007", "palettes가 없거나 문자열 목록이 아닙니다.", "봉투 palettes", repr(value))
        return None
    ok = True
    if not 1 <= len(value) <= max_palettes:
        out.add("E008", f"palettes는 1개 이상 {max_palettes}개 이하여야 합니다(지금 {len(value)}개).",
                "봉투 palettes", repr(value))
        ok = False
    for key in dict.fromkeys(value):
        if key not in rules.palettes:
            names = ", ".join(sorted(rules.palettes))
            out.add("E009", f"알 수 없는 팔레트 '{key}'입니다. 쓸 수 있는 팔레트는 {names}입니다.",
                    "봉투 palettes", key)
            ok = False
    if len(set(value)) != len(value):
        out.add("E010", "palettes에 같은 팔레트가 두 번 들어 있습니다.", "봉투 palettes", repr(value))
        ok = False
    return tuple(value) if ok else None


# Body: blocks


def _split_blocks(body: str):
    """Split the body into blank-line separated blocks of right-stripped lines."""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    blocks, current = [], []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _line_kind(line: str) -> str:
    if INDENTED_LIST_RE.match(line):
        return K_INDENTED_LIST
    if SUB_RE.match(line):
        return K_SUB
    if HEADING_OTHER_RE.match(line):
        return K_HEADING_OTHER
    if line.startswith(">"):
        return K_QUOTE if QUOTE_RE.match(line) else K_QUOTE_BAD
    if UL_RE.match(line):
        return K_UL
    if OL_RE.match(line):
        return K_OL
    if CHECK_RE.match(line):
        return K_CHECK
    if line.startswith("|"):
        return K_TABLE
    if IMAGE_RE.match(line):
        return K_IMAGE
    if LINK_RE.match(line):
        return K_LINK
    return K_TEXT


class _Parser:
    def __init__(self, palette_count: int | None, out: _Collector):
        # palette_count None means palettes were invalid; role checks against 2 are then skipped
        self.palette_count = palette_count
        self.out = out

    # Inline

    def inlines(self, text: str, where: str) -> tuple:
        nodes: list = []
        buf: list[str] = []
        i, n = 0, len(text)

        def flush():
            if buf:
                nodes.append(Text("".join(buf)))
                buf.clear()

        while i < n:
            if text.startswith("**", i):
                end = text.find("**", i + 2)
                if end < 0:
                    self.out.add("P003", "짝이 맞지 않는 **가 있습니다.", where, text[i:])
                    buf.append(text[i:])
                    break
                content = text[i + 2:end]
                if not content:
                    self.out.add("P016", "** 사이가 비어 있습니다.", where, text[i:end + 2])
                elif self._has_mark(content, bold=True):
                    self.out.add("P005", "굵게 표시 안에 다른 표시를 넣을 수 없습니다.", where, text[i:end + 2])
                else:
                    flush()
                    nodes.append(Bold(content))
                i = end + 2
                continue
            ch = text[i]
            if ch == "{":
                end = text.find("}", i + 1)
                if end < 0:
                    self.out.add("P004", "짝이 맞지 않는 {가 있습니다.", where, text[i:])
                    buf.append(text[i:])
                    break
                inner = text[i + 1:end]
                span = text[i:end + 1]
                if "{" in inner:
                    self.out.add("P005", "색 표시 안에 다른 표시를 넣을 수 없습니다.", where, span)
                    # skip to the matching outer brace if any, so one mistake is reported once
                    close = text.find("}", end + 1)
                    i = (close + 1) if close >= 0 else end + 1
                    continue
                node = self._role(inner, where, span)
                if node is not None:
                    flush()
                    nodes.append(node)
                i = end + 1
                continue
            if ch == "}":
                self.out.add("P004", "짝이 맞지 않는 }가 있습니다.", where, text[max(0, i - 10):i + 1])
                i += 1
                continue
            if text.startswith("[이미지:", i):
                self.out.add("P014", "이미지 자리는 문장 안에 둘 수 없습니다. 한 줄에 [이미지: 설명]만 적고 앞뒤를 빈 줄로 "
                                     "나누세요.", where, text[i:])
                end = text.find("]", i)
                i = (end + 1) if end >= 0 else n
                continue
            if text.startswith("[링크:", i):
                end = text.find("]", i)
                if end < 0:
                    self.out.add("P015", "링크 자리의 ]가 없습니다.", where, text[i:])
                    buf.append(text[i:])
                    break
                target = text[i + len("[링크:"):end].strip()
                if not target:
                    self.out.add("P015", "링크 자리의 대상이 비어 있습니다.", where, text[i:end + 1])
                elif self._has_mark(target):
                    self.out.add("P005", "링크 자리 안에 다른 표시를 넣을 수 없습니다.", where, text[i:end + 1])
                else:
                    flush()
                    nodes.append(LinkSlot(target))
                i = end + 1
                continue
            buf.append(ch)
            i += 1
        flush()
        return tuple(nodes)

    @staticmethod
    def _has_mark(content: str, bold: bool = False) -> bool:
        marks = ["{", "}", "[링크:", "[이미지:"]
        if not bold:
            marks.append("**")
        return any(m in content for m in marks)

    def _role(self, inner: str, where: str, span: str):
        m = ROLE_RE.match(inner)
        if not m or m.group(2) not in ("1", "2"):
            self.out.add("P006", "알 수 없는 표시입니다. 쓸 수 있는 표시는 {c1:텍스트}, {c2:텍스트}, {h1:텍스트}, "
                                 "{h2:텍스트}입니다.", where, span)
            return None
        kind, role, content = m.group(1), int(m.group(2)), m.group(3)
        if not content:
            self.out.add("P016", "색 표시 안이 비어 있습니다.", where, span)
            return None
        if "**" in content or "[링크:" in content or "[이미지:" in content:
            self.out.add("P005", "색 표시 안에 다른 표시를 넣을 수 없습니다.", where, span)
            return None
        if role == 2 and self.palette_count == 1:
            self.out.add("P007", f"팔레트가 하나뿐이라 {kind}2를 쓸 수 없습니다.", where, span)
            return None
        return Color(role, content) if kind == "c" else Highlight(role, content)

    # Blocks

    def block(self, block_no: int, lines: list[str]):
        kinds = [_line_kind(line) for line in lines]
        out = self.out

        bad = False
        for no, (line, kind) in enumerate(zip(lines, kinds), start=1):
            if kind == K_INDENTED_LIST:
                out.add("P002", "들여쓴 목록 줄은 쓸 수 없습니다. 목록은 한 단계로만 적습니다.", _where(block_no, no), line)
                bad = True
            elif kind == K_HEADING_OTHER:
                out.add("P017", "본문의 제목 표시는 ## 소제목만 쓸 수 있습니다. 글 제목은 봉투의 title에 적습니다.",
                        _where(block_no, no), line)
                bad = True
            elif kind == K_QUOTE_BAD:
                out.add("P012", "인용 줄은 '> '로 시작해야 합니다.", _where(block_no, no), line)
                bad = True
        if bad:
            return None

        first = kinds[0]
        if first == K_SUB:
            if len(lines) > 1:
                out.add("P013", "소제목은 한 줄이어야 하고 뒤에 빈 줄을 두어야 합니다.", _where(block_no, 2), lines[1])
                return None
            text = (SUB_RE.match(lines[0]).group(1) or "").strip()
            if not text:
                out.add("P013", "소제목이 비어 있습니다.", _where(block_no, 1), lines[0])
                return None
            return Subheading(self.inlines(text, _where(block_no, 1)))

        if first == K_IMAGE:
            if len(lines) > 1:
                out.add("P014", "이미지 자리는 앞뒤를 빈 줄로 나눈 한 줄이어야 합니다.", _where(block_no, 2), lines[1])
                return None
            desc = IMAGE_RE.match(lines[0]).group(1).strip()
            core = desc[:-len("이미지")].strip() if desc.endswith("이미지") else desc
            if not core:
                out.add("P009", "이미지 설명이 비어 있습니다.", _where(block_no, 1), lines[0])
                return None
            if self._has_mark(desc):
                out.add("P005", "이미지 자리 안에 다른 표시를 넣을 수 없습니다.", _where(block_no, 1), lines[0])
                return None
            return ImageSlot(desc)

        if first == K_LINK and len(lines) == 1:
            target = LINK_RE.match(lines[0]).group(1).strip()
            if not target:
                out.add("P015", "링크 자리의 대상이 비어 있습니다.", _where(block_no, 1), lines[0])
                return None
            if self._has_mark(target):
                out.add("P005", "링크 자리 안에 다른 표시를 넣을 수 없습니다.", _where(block_no, 1), lines[0])
                return None
            return LinkBlock(target)

        if first in (K_QUOTE, K_UL, K_OL, K_TABLE):
            for no, kind in enumerate(kinds[1:], start=2):
                if kind != first:
                    out.add("P011", f"{KIND_NAMES[first]} 블록의 모든 줄은 같은 표시로 시작해야 합니다. 다른 블록은 빈 줄로 "
                                    f"나누세요.", _where(block_no, no), lines[no - 1])
                    return None
            if first == K_TABLE:
                return self._table(block_no, lines)
            pattern = {K_QUOTE: QUOTE_RE, K_UL: UL_RE, K_OL: OL_RE}[first]
            items = []
            for no, line in enumerate(lines, start=1):
                match = pattern.match(line)
                content = (match.group(2 if first == K_OL else 1) or "").strip()
                if first == K_OL and not self._ordered_number_ok(block_no, no, line, int(match.group(1)), content):
                    items.append(())
                    continue
                if not content:
                    out.add("P018", f"{KIND_NAMES[first]}의 줄이 비어 있습니다.", _where(block_no, no), line)
                    items.append(())
                    continue
                items.append(self.inlines(content, _where(block_no, no)))
            if first == K_QUOTE:
                return Quote(tuple(items))
            return ListBlock(first == K_OL, tuple(items))

        if all(kind == K_CHECK for kind in kinds):
            items = []
            for no, line in enumerate(lines, start=1):
                content = (CHECK_RE.match(line).group(1) or "").strip()
                if not content:
                    out.add("P018", "체크리스트 항목이 비어 있습니다.", _where(block_no, no), line)
                    items.append(())
                    continue
                items.append(self.inlines(content, _where(block_no, no)))
            return Checklist(tuple(items))

        # Paragraph: text, checklist-looking and whole-line link lines may mix; structural lines may not.
        inlines: list = []
        for no, (line, kind) in enumerate(zip(lines, kinds), start=1):
            if kind == K_IMAGE:
                out.add("P014", "이미지 자리는 앞뒤를 빈 줄로 나눈 한 줄이어야 합니다.", _where(block_no, no), line)
                return None
            if kind in (K_SUB, K_QUOTE, K_UL, K_OL, K_TABLE):
                out.add("P011", f"문단 안에 {KIND_NAMES[kind]} 줄이 섞였습니다. 빈 줄로 블록을 나누세요.",
                        _where(block_no, no), line)
                return None
            if no > 1:
                inlines.append(LineBreak())
            inlines.extend(self.inlines(line, _where(block_no, no)))
        return Paragraph(_merge_text(inlines))

    def _ordered_number_ok(self, block_no: int, no: int, line: str, number: int, content: str) -> bool:
        """The renderer numbers <ol> items 1, 2, 3, ... and drops the written numbers.

        Dropping is only safe when the written numbers are exactly those, so anything else is an
        error sent back to the node instead of a silent change: '3. 5. 7.' would lose its numbers and
        a month-first date line such as '9. 30.부터 개강합니다.' would lose its month.
        """
        if number != no:
            self.out.add("P019", f"순서 있는 목록의 번호는 1부터 차례로 적습니다(블록의 {no}번째 줄이 {number}번입니다). "
                                 f"번호는 렌더러가 1부터 매기므로 다른 번호는 사라집니다. 날짜처럼 숫자와 마침표로 "
                                 f"시작하는 문장이라면 줄 첫머리를 달리 쓰십시오.", _where(block_no, no), line)
            return False
        if OL_DATE_TAIL_RE.match(content):
            self.out.add("P019", "숫자와 마침표가 두 번 이어져 날짜처럼 읽히는 줄이 순서 있는 목록으로 읽혔습니다. "
                                 "날짜라면 줄 첫머리를 달리 쓰고, 목록이라면 항목 첫머리의 숫자를 빼십시오.",
                         _where(block_no, no), line)
            return False
        return True

    def _table(self, block_no: int, lines: list[str]):
        out = self.out
        if len(lines) < 3:
            out.add("P010", "표는 머리글 줄, 구분선 줄(|---|---|), 본문 행 한 줄 이상으로 이루어져야 합니다.",
                    _where(block_no), lines[0])
            return None
        header_cells = _split_row(lines[0])
        sep_cells = _split_row(lines[1])
        if not all(TABLE_SEP_CELL_RE.match(c) for c in sep_cells):
            out.add("P010", "표의 둘째 줄은 구분선(|---|---|)이어야 합니다.", _where(block_no, 2), lines[1])
            return None
        width = len(header_cells)
        ok = True
        if len(sep_cells) != width:
            out.add("P008", f"구분선의 칸 수({len(sep_cells)})가 머리글 칸 수({width})와 다릅니다.",
                    _where(block_no, 2), lines[1])
            ok = False
        rows_raw = []
        for no, line in enumerate(lines[2:], start=3):
            cells = _split_row(line)
            if len(cells) != width:
                out.add("P008", f"표 행의 칸 수({len(cells)})가 머리글 칸 수({width})와 다릅니다.",
                        _where(block_no, no), line)
                ok = False
            rows_raw.append((no, cells))
        header = tuple(self.inlines(c, _where(block_no, 1)) for c in header_cells)
        rows = tuple(tuple(self.inlines(c, _where(block_no, no)) for c in cells) for no, cells in rows_raw)
        return Table(header, rows) if ok else None


def _split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _merge_text(nodes) -> tuple:
    merged: list = []
    for node in nodes:
        if isinstance(node, Text) and merged and isinstance(merged[-1], Text):
            merged[-1] = Text(merged[-1].value + node.value)
        else:
            merged.append(node)
    return tuple(merged)


def _check_escaped_newlines(body: str, out: _Collector) -> bool:
    """P020. True when the line breaks arrived as a backslash and n, so the blocks are not parsed.

    Parsing them would only report symptoms (an image slot inside a sentence, say) and send the
    node after the wrong cause. Without real line breaks the whole body is one paragraph that
    renders the two characters, ## and - as they are, and nothing else would flag it.
    """
    count = body.count(ESCAPED_NEWLINE)
    if not count:
        return False
    where, around = _where(1), ""
    for block_no, lines in enumerate(_split_blocks(body), start=1):
        hit = next(((no, line) for no, line in enumerate(lines, start=1) if ESCAPED_NEWLINE in line), None)
        if hit is not None:
            index = hit[1].find(ESCAPED_NEWLINE)
            where, around = _where(block_no, hit[0]), hit[1][max(0, index - 8):index + 12]
            break
    if "\n" not in body.strip() or ESCAPED_BLOCK_START_RE.search(body):
        out.add("P020", f"본문에 역슬래시와 n 두 글자가 {count}번 있습니다. 줄바꿈을 글자로 적어 블록이 나뉘지 않았습니다. "
                        "JSON 문자열의 줄바꿈 이스케이프는 \\n 하나뿐이니 역슬래시를 겹쳐 쓰지 말고, body에 실제 줄바꿈이 "
                        "들어가게 다시 쓰십시오.", where, around)
        return True
    out.add("P020", f"본문에 역슬래시와 n 두 글자가 {count}번 있습니다. 줄바꿈을 글자로 적은 것이라면 실제 줄바꿈으로 "
                    "고치십시오. 글에서 그 두 글자를 보여 주려는 것이라면 그대로 두어도 됩니다.", where, around, severity=WARN)
    return False


def parse(envelope: dict, rules, brand) -> tuple:
    """Validate the envelope and parse its body. Returns (Document or None, issues)."""
    out = _Collector()
    slug, title, palettes, body = _check_envelope(envelope, rules, brand, out)

    blocks: list = []
    if body is not None:
        parser = _Parser(len(palettes) if palettes else None, out)
        raw_blocks = [] if _check_escaped_newlines(body, out) else _split_blocks(body)
        if raw_blocks and rules.form.get("title_first_block") == "paragraph" \
                and not _looks_like_paragraph(raw_blocks[0]):
            out.add("P001", "본문의 첫 블록은 문단이어야 합니다. 제목 바로 뒤에는 도입 문단을 둡니다.",
                    _where(1), raw_blocks[0][0])
        for block_no, lines in enumerate(raw_blocks, start=1):
            block = parser.block(block_no, lines)
            if block is not None:
                blocks.append(block)

    if any(issue.severity == ERROR for issue in out.issues):
        return None, out.issues
    return Document(slug=slug, title=title, palettes=palettes, blocks=tuple(blocks)), out.issues


def _looks_like_paragraph(lines: list[str]) -> bool:
    """Block kind decided from the first line, the same way _Parser.block classifies it."""
    kinds = [_line_kind(line) for line in lines]
    if kinds[0] == K_TEXT:
        return True
    if kinds[0] == K_CHECK:
        return not all(kind == K_CHECK for kind in kinds)
    if kinds[0] == K_LINK:
        return len(lines) > 1
    return False
