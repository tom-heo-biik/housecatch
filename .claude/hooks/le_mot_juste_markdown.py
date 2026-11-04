"""Le-mot-juste-markdown format checker and the PreToolUse hook that runs it.

Checks the machine-decidable rules that Le-mot-juste-markdown took over from
.claude/harness/docs/how-to-write-CLAUDE.md (1st-round A1), together with the decisions recorded in
projects/Le-mot-juste-markdown (QnA rounds 1 to 6, memo/2026-10-04-헤더-표기법.md) and in chat:
- exactly one H1 (headings inside block quotes and list items count too), whose title is the document
  path (SKILL.md too, since 2026. 11. 1.), and the first line after any front matter and HTML comments
  is that H1 (markdownlint MD041)
- every leaf header and every H2 with H3s reads '작성자, 제목, 작성일, 글자 수, 기타', the cells parted by
  ', ' and none of them empty or '{}': author Claude(model), since the hook only sees what Claude
  writes (sections Tom, J or MGM wrote stay as they are until touched), date YYYY. M. D., and as the
  count the body's length without whitespace (0 for an empty body or one ending with
  '{추후 작성 예정}', A29), or for an H2 with H3s the sum of what its H3 bodies measure
- body text only under leaf headers other than the H1, exactly 200 characters without whitespace unless
  unfinished;
  '{추후 작성 예정}' named inside a sentence is ordinary text, a body that ends in a near miss of the
  mark is told so, and code-only bodies are not measured
- headers go no deeper than H3 and never skip a level (markdownlint MD001, headings inside block
  quotes and list items included); at most five H2s in a document and five H3s under one H2
- at most ten Markdown documents in one folder (hook only, it needs the file system)
- two blank lines above every H2 and H3, none between a leaf header and its body, braces instead of
  square brackets for placeholders, slashes instead of backslashes in paths outside code blocks and
  inline code (front matter included), LF line endings, no BOM, one trailing newline
- 존댓말 as far as code can tell, in sections Claude wrote in its own voice (author Claude, 기타 not
  Tom, J or MGM, so recorded answers keep the speaker's words): a section needs a Korean sentence
  ending in -ㅂ니다, -ㅂ니까, 요, 죠, 시오 or -ㅂ시다, and no sentence may end in 해라체 '-다'. Quoted
  words and block quotes are the original's and stay out; a spaced clause in parentheses or brackets
  is Claude's own and counts
맞춤법과 띄어쓰기 and courtesy beyond sentence endings need judgement and are left to the
probabilistic reviewers (QnA 1st round, 4th-round A19).

Every broken rule is reported at every place it breaks, so one change can bring many problems at
once. Each message opens with Tom's sentence for its rule (RULES) and then says what was found.

The block structure follows CommonMark as far as the rules need it. ATX headings (up to three leading
spaces, an optional closing run of '#', and an empty heading such as '### ' is still a heading) and
Setext headings part the document into sections; '---' or '===' under a GFM table (read as GitHub and
markdownlint read tables) or under link reference definitions alone underlines nothing. Fenced code
(also inside block quotes and list items), indented code and HTML blocks hold no headings, and code is
neither measured nor read for sentences, placeholders or paths.

Hook mode reads a PreToolUse payload for Write, Edit or MultiEdit, rebuilds the document the call
would leave behind the way Claude Code's tools do and exits 2 with the reasons on stderr when the call
adds a violation. Problems the document already had and the call did not touch do not block, so old
documents stay editable; set LE_MOT_JUSTE_MODE=strict to block on every problem of the resulting
document. Covered documents are the repository's Markdown (.md and .markdown) outside company/forum
and outside the top documents listed in .claude/hooks/claude-md-reminder.list, plus Markdown in
Claude Code scratchpad and memory folders, whose H1 path is relative to that folder. The memory index
MEMORY.md stays out (Tom, 2026. 11. 1.): Claude Code reads it as a list of one line per memory.
Markdown anywhere else is covered as well (Tom, 2026. 11. 5.), its H1 path relative to the git work
tree holding it, or to its own folder when none does.

Importing this module has no side effects.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TARGET = 200
MAX_LEVEL = 3
MAX_H2 = 5
MAX_H3 = 5  # under one H2
MAX_DOCS_PER_FOLDER = 10
UNKNOWN = "{}"  # once the mark of a cell nobody knew; now an empty value, which the rules forbid
CELLS = ("작성자", "제목", "작성일", "글자 수", "기타")
PEOPLE = ("Tom", "J", "MGM")
MARKDOWN = (".md", ".markdown")  # endings of the file names the rules cover, in lower case
CASE = re.IGNORECASE if os.name == "nt" else 0  # Windows paths ignore case

# Same whitespace definition as projects/durumi/tools/doccheck.py: Python's `\s`.
WHITESPACE = re.compile(r"\s")
TRAILING_BLANK = re.compile(r"\n[^\S\n]*\n\Z")  # the last line is empty or holds only whitespace
# Block structure, read from lines whose tabs are expanded to four columns (CommonMark 0.31.2).
ATX = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)(.*)$")
CLOSING_HASHES = re.compile(r"(?:^|(?<=[ \t]))#+[ \t]*$")
SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
QUOTE_MARK = re.compile(r"^ {0,3}>[ ]?")
_QUOTE_NEXT = re.compile(r" {0,3}>[ ]?")  # QUOTE_MARK for match(x, pos), which ^ would not let start at pos
LIST_MARK = re.compile(r"^ {0,3}(?:[-*+]|([0-9]{1,9})[.)])(?= |$)")
# A GFM table delimiter row such as '|---|:---:|'. With a pipe in it and as many cells in the paragraph line
# above, that line is the table's header row, and '---' or '===' below the table is no Setext underline.
TABLE_DELIMITER = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_PIPE = re.compile(r"(?<!\\)\|")
_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|"
    "div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h1|h2|h3|h4|h5|h6|head|header|hr|html|"
    "iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|"
    "table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
# Whitespace in a tag is spaces and tabs only (CommonMark 0.31.2). With \s, U+3000 or U+00A0 could both
# end an unquoted value and part two attributes, and a line of them backtracked in exponential time.
_ATTRIBUTE = r"""(?:[ \t]+[A-Za-z_:][A-Za-z0-9:._-]*(?:[ \t]*=[ \t]*(?:[^"'=<>`\x00-\x20]+|'[^']*'|"[^"]*"))?)"""
# What opens an HTML block, what ends it (None: the next blank line) and whether it may interrupt a paragraph.
HTML_BLOCKS = (
    (re.compile(r"^<(?:script|pre|style|textarea)(?=[ \t]|>|$)", re.IGNORECASE),
     re.compile(r"</(?:script|pre|style|textarea)>", re.IGNORECASE), True),
    (re.compile(r"^<!--"), re.compile(r"-->"), True),
    (re.compile(r"^<\?"), re.compile(r"\?>"), True),
    (re.compile(r"^<![A-Za-z]"), re.compile(r">"), True),
    (re.compile(r"^<!\[CDATA\["), re.compile(r"\]\]>"), True),
    (re.compile(rf"^</?(?:{_BLOCK_TAGS})(?=[ \t]|/?>|$)", re.IGNORECASE), None, True),
    (re.compile(rf"^(?:<[A-Za-z][A-Za-z0-9-]*{_ATTRIBUTE}*[ \t]*/?>|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*$"), None, False),
)
CODE = ("fence", "open", "code")  # line kinds that hold code: fenced (closed, or open to the end) and indented
TABLES = ("table", "quotetable")  # what _blocks calls an open GFM table
QUOTED = ("quote", "quotelist", "quotetable")  # what _blocks calls open inside a block quote

TODO = re.compile(r"\{추후 작성 예정\}\s*$")
# What D011 looks for; _bracketed finds the same in linear time, and the pattern stays for scripts that use it.
BRACKET_PLACEHOLDER = re.compile(r"\[[^\]]*(추후|작성 예정)[^\]]*\]")
# A body that ends almost like the mark: '{추후작성예정}', '{ 추후 작성 예정 }', '[추후 작성 예정]', the mark
# in quotes or followed by a period, or the words alone.
NEAR_TODO = re.compile(r"[\"“\[]?\{?\s*추후\s*작성\s*예정\s*\}?[\"”\]]?\.?\s*$")
# NEAR_TODO is tried on this many last characters of a body only: tried from every position of a long run
# of spaces it took quadratic time, and a near miss of the mark is some twenty characters.
NEAR_TODO_WINDOW = 200
IMPORT_HEADER = re.compile(r"(^|\s)@\S+")
HEADER_PLACEHOLDER = re.compile(r"\{\s*[^{}\s][^{}]*\}")  # '{브랜드}', but not the empty value '{}'
DYNAMIC_INJECTION = re.compile(r"^!`[^`\n]+`$")
FRONT_MATTER_NAME = re.compile(r"^name:(.*)$")
CLAUDE = re.compile(r"^Claude\((?=[^(){}]*[0-9A-Za-z가-힣])[^(){}]+\)$")  # a model name, not '{모델명}' or a blank
DATE = re.compile(r"^([0-9]{4})\. ([1-9]|1[0-2])\. ([1-9]|[12][0-9]|3[01])\.$")  # ASCII digits, not ２０２６
DIGITS = re.compile(r"^[0-9]+$")
# Claude Code scratchpad, <temp>/claude/<project>/<session id>/scratchpad/..., and auto memory,
# ~/.claude/projects/<project>/memory/...
SCRATCHPAD = re.compile(r"^(.*[\\/]claude[\\/][^\\/]+[\\/][0-9a-fA-F-]{36}[\\/]scratchpad)[\\/]", CASE)
MEMORY = re.compile(r"^(.*[\\/]\.claude[\\/]projects[\\/][^\\/]+[\\/]memory)[\\/]", CASE)
MEMORY_INDEX = "MEMORY.md"  # at the memory folder's root; not covered
# A Windows path written with backslashes: a drive (C:\...), a share (\\server\share), names joined by
# two backslashes or more, or by one that ends in a file name, a path that opens with ., .., ~ or
# %VARIABLE% and goes on with two names, a file name or a name and a closing backslash (.\run.ps1,
# ..\docs\a.md, ~\notes\), and a dot folder with one name under it (.claude\hooks). Markdown escapes
# such as 2026\. and 9\~10, regular expressions such as \d+, 1\.0\.19 and [0-9]+.\d+ and escapes such
# as \n and .\n are not paths, and neither are two bare names such as A\B.
_FIRST = r"\.{0,2}[0-9A-Za-z가-힣_][0-9A-Za-z가-힣_.$~-]*"
_NAME = r"(?!\.[0-9])\.{0,2}[0-9A-Za-z가-힣][0-9A-Za-z가-힣_.$~-]*"
_FILE = r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣_.$~-]*\.[0-9A-Za-z]{1,8}"
WINDOWS_PATH = re.compile(
    r"(?<![0-9A-Za-z가-힣_.\\$~%-])(?:"
    rf"[A-Za-z]:\\(?:{_NAME}(?:\\{_NAME})*\\?)?"
    rf"|\\\\{_NAME}(?:\\{_NAME})+\\?"
    rf"|{_FIRST}(?:\\{_NAME}){{2,}}\\?"
    rf"|{_FIRST}\\{_FILE}"
    rf"|(?:\.{{1,2}}|~|%[A-Za-z_]+%)(?:(?:\\{_NAME}){{2,}}\\?|\\{_NAME}\\|\\{_FILE})"
    rf"|\.[0-9A-Za-z_][0-9A-Za-z가-힣_.$~-]*\\{_NAME}"
    r")"
)
_PATH_TAIL = re.compile(r"(\.[0-9A-Za-z]{1,8})[가-힣.]+$")  # a particle or a period after the file name
# Sentence endings for the 존댓말 rule (D027). Code, comments, links, quotes, parentheses, brackets,
# braces and dates come out first, so a quoted 반말 or the date 2026. 11. 1. ends no sentence.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK = re.compile(r"!?\[([^\[\]]*)\]\([^)]*\)")
# Links as CommonMark reads them, whose brackets name no placeholder (D011): an inline link whose
# destination holds no space or sits in <...>, with an optional title; a reference link or a shortcut
# whose label a link reference definition of the document defines; and that definition. Without them
# '[추후 작성 예정](담당: Tom)', '[추후 작성 예정][1]' and '[추후 작성 예정]: 나중에 씁니다.' are text.
_INLINE_LINK = re.compile(
    r"!?\[[^\[\]]*\]\([ \t]*(?:<[^<>\n]*>|(?:[^\s()<]|\([^\s()]*\))(?:[^\s()]|\([^\s()]*\))*)?"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*\)")
_REFERENCE_LINK = re.compile(r"!?\[([^\[\]]*)\]\[([^\[\]]*)\]")
_SHORTCUT_LINK = re.compile(r"!?\[([^\[\]]+)\]")
_LINK_DEFINITION = re.compile(
    r"^ {0,3}\[(?=[^\]]*[^\s\]])((?:[^\[\]\\]|\\.)+)\]:[ \t]*(?:<[^<>\n]*>|[^\s<]\S*)"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*$")
_URL = re.compile(r"https?://\S+|www\.\S+")
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_QUOTED = re.compile(
    r'''"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<![A-Za-z])'[^'\n]*'(?![A-Za-z])|「[^」\n]*」|『[^』\n]*』|《[^》\n]*》|〈[^〉\n]*〉''')
_ASIDE = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}|（[^（）]*）")
# Asides come out innermost first, one pass a level; nesting deeper than this stays as it is. Each pass
# reads the whole line, so thousands of levels took seconds, and no writing nests a tenth as deep.
MAX_ASIDE_DEPTH = 64
_DATE_TEXT = re.compile(r"\d{4}\. ?\d{1,2}\. ?\d{1,2}\.")
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# A sentence ends at . ? ! ; or : (bold or other emphasis may close right after it) and at a dash. Before
# ; : and a dash it may also be a name or a headword ('- 레몬 소다: 5,000원', '- **적확하다** — 뜻'), so a
# 해라체 '-다' there counts only in a clause: a word before it ends in a particle, or 다 follows ㅆ (됐다).
_SENTENCE_BREAK = re.compile(r"(?<=[.?!;:])[*_~]*\s+|\s[—–]\s")
_LAST_WORD = re.compile(r"(?<![가-힣])([가-힣]+)[^가-힣0-9A-Za-z]*$")
_ENDS_SENTENCE = re.compile(r"[.?!][^가-힣0-9A-Za-z]*$")
_GLUED = re.compile(r"[0-9A-Za-z%)\]]")  # what '-다' may follow straight, as in CRLF다 and 77%다
_LABEL = re.compile(r"^[^:：]+[:：]\s")  # '이름: 값', however long the name
_HANGUL = re.compile(r"[가-힣]")
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_WORD_TAIL = re.compile(r"[^가-힣0-9A-Za-z]+$")
PARTICLES = ("은", "는", "이", "가", "을", "를", "에", "로", "서", "도", "만", "께", "게", "건", "걸", "란", "엔",
             "까지", "부터", "처럼", "만큼", "한테")
POLITE_ENDINGS = ("시오", "요", "죠")  # with -ㅂ니다, -ㅂ니까 and -ㅂ시다, which _ending reads from the syllable
NOUNS_ENDING_IN_YO = ("필요", "중요", "개요", "수요", "주요", "강요", "소요", "동요", "요요", "불요", "긴요", "적요")
NOUNS_ENDING_IN_DA = ("바다", "람다", "아젠다", "어젠다", "베란다", "버뮤다", "캐나다", "프로파간다")
NOMINAL_ENDINGS = ("함", "음", "임", "됨", "짐")  # 개조식 lines such as '확인함' end no sentence in 존댓말
# Nouns that end like a 개조식 line: '- 부가세와 출장비 포함' is a note, not a sentence.
NOUNS_ENDING_NOMINALLY = ("포함", "결함", "다음", "처음", "마음", "소음", "발음", "얼음", "책임", "모임", "담임",
                          "움직임", "다짐", "조짐")
_COUNTED = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열")

# The rule each problem code enforces, in Tom's words (2026. 11. 1.); every message opens with it.
# D017 (the old list of amounts) is retired because the count now follows from the body. D007
# depends on the header level; D014 and D018 on whether the header is an H2 with H3s ("parent") or
# a leaf.
PARENT_FORM = "상위 H2 헤더는 {작성자}, {제목}, {작성일}, {하위 H3 헤더의 공백 제외 본문 글자 수의 합}, {기타} 꼴로 빈 값 없이 작성해야 합니다."
LEAF_FORM = "최하위 헤더는 {작성자}, {제목}, {작성일}, {공백 제외 본문 글자 수}, {기타} 꼴로 빈 값 없이 작성해야 합니다."
FORM = {"parent": PARENT_FORM, "leaf": LEAF_FORM}
RULES = {
    "D001": "Markdown 문서의 줄 끝은 LF로 써야 합니다.",
    "D002": "Markdown 문서는 줄 바꿈 하나로 끝나야 합니다.",
    "D003": "Markdown 문서는 줄 바꿈 하나로 끝나야 합니다.",
    "D004": "본문은 H1 헤더가 아닌 최하위 헤더에만 작성해야 합니다.",
    "D005": "H1 헤더는 Markdown 문서당 정확히 1개 작성해야 합니다.",
    "D006": "H1 헤더는 Markdown 문서의 경로명으로 작성해야 합니다.",
    "D007": {2: "H2 헤더 위엔 빈 줄을 두 줄 둬야 합니다.", 3: "H3 헤더 위엔 빈 줄을 두 줄 둬야 합니다."},
    "D008": "본문은 H1 헤더가 아닌 최하위 헤더에만 작성해야 합니다.",
    "D009": "미완성 본문은 \"{추후 작성 예정}\"(큰따옴표 없이)으로 끝나야 합니다.",
    "D010": "본문은 최하위 헤더 바로 아래(빈 줄 없이)에 작성해야 합니다.",
    "D011": "플레이스홀더는 중괄호로 감싸 작성해야 합니다.",
    "D012": "본문은 실측 기준 공백 제외 정확히 200자로 작성해야 합니다.",
    "D013": "Markdown 문서는 BOM 없이 UTF-8로 작성해야 합니다.",
    "D014": FORM,
    "D015": "Claude가 쓴 본문의 헤더는 작성자를 Claude({모델명})으로 작성해야 합니다.",
    "D016": "작성일은 {YYYY. M. D.} 꼴로 작성해야 합니다.",
    "D018": FORM,
    "D019": "미완성 본문의 헤더는 공백 제외 본문 글자 수를 0으로 작성해야 합니다.",
    "D020": PARENT_FORM,
    "D021": "헤더는 H1, H2, H3만 사용해야 합니다.",
    "D022": "markdownlint의 MD001을 준수해야 합니다.",
    "D023": "H2 헤더는 Markdown 문서당 5개 이하로만 작성해야 합니다.",
    "D024": "Markdown 문서는 폴더당 10개 이하로만 작성해야 합니다.",
    "D025": "Markdown 문서 안의 경로(코드 블록 및 인라인 코드 예외)는 슬래시(/)로 표기해야 합니다.",
    "D026": "H3 헤더는 H2 헤더당 5개 이하로만 작성해야 합니다.",
    "D027": "예의를 갖추어 존댓말(인용한 원문 예외)로 작성해야 합니다.",
    "D028": LEAF_FORM,
    "D029": "markdownlint의 MD041(front matter 예외)을 준수해야 합니다.",
}

SKIP_DIRS = {".git", "runs", "__pycache__", "node_modules", "fixtures"}
FORUM = ("company", "forum")
TOP_LIST = ".claude/hooks/claude-md-reminder.list"
TOOLS = ("Write", "Edit", "MultiEdit")
QUOTES = str.maketrans("‘’“”", "''\"\"")  # Claude Code's Edit finds curly quotes from straight ones


@dataclass(frozen=True)
class Problem:
    code: str
    line: int  # 1-based line of the checked text, 0 for the whole document
    message: str
    key: tuple  # names the same problem in two versions of a document


@dataclass
class Report:
    problems: list = field(default_factory=list)
    h2_count: int = 0


@dataclass
class Header:
    index: int  # 0-based line index
    level: int
    text: str
    span: int = 1  # lines the heading takes: a Setext heading adds its underline to its text lines


@dataclass
class Notation:
    author: str
    title: str
    date: str
    amount: str
    etc: str


@dataclass
class Blocks:
    kinds: list  # per line: "front", "blank", "text", "html", or one of CODE
    headers: list = field(default_factory=list)  # top-level ATX and Setext headings: the sections
    nested: list = field(default_factory=list)  # headings inside block quotes and list items


def count_chars(text: str) -> int:
    """Characters of `text` excluding whitespace."""
    return len(WHITESPACE.sub("", text))


def lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_notation(title: str) -> Notation | None:
    """Split '작성자, 제목, 작성일, 분량, 기타'. Cells are read from both ends, so the title may hold commas."""
    parts = title.split(",")
    if len(parts) < 5:
        return None
    return Notation(
        author=parts[0].strip(),
        title=",".join(parts[1:-3]).strip(),
        date=parts[-3].strip(),
        amount=parts[-2].strip(),
        etc=parts[-1].strip(),
    )


def _parted(title: str) -> bool:
    """True when the cells of a notation are parted by ', ': no space before a comma and one after it.
    Commas inside the title cell are not looked at, and an empty cell is D018's to report."""
    parts = title.split(",")
    for b in sorted({0, len(parts) - 4, len(parts) - 3, len(parts) - 2}):
        left, right = parts[b], parts[b + 1]
        if not left.strip() or not right.strip():
            continue
        if left != left.rstrip() or not right.startswith(" ") or right[1:2].isspace():
            return False
    return True


def _visible(value: str) -> str:
    """`value` without whitespace and invisible format characters (Unicode Cf, such as U+200B)."""
    return "".join(c for c in value if not c.isspace() and unicodedata.category(c) != "Cf")


def _blank(value: str) -> bool:
    """True for a cell with no value: nothing visible, or only '{}'."""
    return _visible(value) in ("", UNKNOWN)


def value_of(notation: Notation | None) -> int | None:
    """The count written in the fourth cell, or None when it is not a number of nine digits at most."""
    if notation is None or not DIGITS.match(notation.amount) or len(notation.amount) > 9:
        return None
    return int(notation.amount)


def _filled(notation: Notation | None) -> str | None:
    """The fourth cell as written, or None when there is no notation or the cell is empty."""
    if notation is None or _blank(notation.amount):
        return None
    return notation.amount


def _valid_date(text: str) -> bool:
    m = DATE.match(text)
    if not m:
        return False
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return True


def _named(h: Header) -> str:
    """How a message calls a header: its title in quotes, or '빈 헤더' when it has none."""
    return f"'{h.text}' 헤더" if h.text else "빈 헤더"


def _lines_counted(n: int) -> str:
    return f"{_COUNTED[n]} 줄" if 0 < n < len(_COUNTED) else f"{n}줄"


def _front_matter(lines: list[str]) -> tuple[int, str | None]:
    """(lines taken by a leading front matter block, its `name` value)."""
    if not lines or lines[0].rstrip() != "---":
        return 0, None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            name = None
            for line in lines[1:i]:
                m = FRONT_MATTER_NAME.match(line)
                if m:
                    name = m.group(1).strip()
                    if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
                        name = name[1:-1]
                    else:
                        name = name.split(" #", 1)[0].rstrip()  # a YAML comment
            return i + 1, name
    return 0, None


def _opens_fence(line: str) -> str | None:
    m = FENCE_OPEN.match(line)
    if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return None
    return m.group(1)


def _closes_fence(line: str, fence: str) -> bool:
    m = FENCE_CLOSE.match(line)
    return bool(m) and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)


def _indent(x: str) -> int:
    return len(x) - len(x.lstrip(" "))


def _quotes(x: str, limit: int = 0) -> tuple[int, str]:
    """(block quote markers that open the line `x`, what follows them), at most `limit` markers if set. The
    markers are counted in place and the line is cut once, so a line of many markers takes linear time."""
    depth = at = 0
    while not limit or depth < limit:
        m = _QUOTE_NEXT.match(x, at)
        if not m:
            break
        at, depth = m.end(), depth + 1
    return depth, x[at:]


def _list_item(y: str):
    """(content column, rest of the line) when the line `y` opens a list item, else None."""
    if THEMATIC_BREAK.match(y):
        return None
    m = LIST_MARK.match(y)
    if not m:
        return None
    rest = y[m.end():]
    if not rest.strip():
        return m.end() + 1, ""
    spaces = _indent(rest)
    if spaces > 4:  # the item opens with indented code, which keeps all but one of the spaces
        return m.end() + 1, rest[1:]
    return m.end() + spaces, rest[spaces:]


def _interrupts(y: str, rest: str) -> bool:
    """True when the list item that the line `y` opens may interrupt a paragraph: it is not empty, and a
    numbered one starts at 1."""
    number = LIST_MARK.match(y).group(1)
    return bool(rest.strip()) and (number is None or int(number) == 1)


def _html_end(text: str, in_paragraph: bool):
    """What ends the HTML block that `text` (without its indent) opens: a pattern, or "" when the next blank
    line does. None when it opens none; inside a paragraph only the kinds that may interrupt it count."""
    for opening, closing, interrupts in HTML_BLOCKS:
        if (interrupts or not in_paragraph) and opening.search(text):
            return "" if closing is None else closing
    return None


def _starts_block(y: str) -> bool:
    """True when the line `y`, which list items above it do not take in, opens a block and so cannot continue
    their paragraph lazily. Any list item does, since the paragraph is not where the item would go."""
    if _indent(y) >= 4:
        return False
    return bool(ATX.match(y) or _opens_fence(y) or QUOTE_MARK.match(y) or THEMATIC_BREAK.match(y)
                or LIST_MARK.match(y) or _html_end(y.lstrip(" "), True) is not None)


def _interrupts_paragraph(z: str) -> bool:
    """True when the line `z`, read inside the container of an open paragraph, opens a block that ends the
    paragraph. A list item does only when it is not empty and, if numbered, starts at 1."""
    if not z.strip():
        return True
    if _indent(z) >= 4:
        return False  # indented code cannot interrupt a paragraph
    item = _list_item(z)
    if item:
        return _interrupts(z, item[1])
    return bool(ATX.match(z) or _opens_fence(z) or QUOTE_MARK.match(z) or THEMATIC_BREAK.match(z)
                or _html_end(z.lstrip(" "), True) is not None)


def _cells(row: str) -> int:
    """The cells of a GFM table row: parts between pipes that no backslash escapes, without the empty ends
    that a leading or a trailing pipe leaves."""
    parts = _PIPE.split(row.strip())
    if len(parts) > 1 and parts[0] == "":
        parts = parts[1:]
    if len(parts) > 1 and parts[-1] == "":
        parts = parts[:-1]
    return len(parts)


def _table_starts(row: str, above: str) -> bool:
    """True when the line `row` is a GFM table delimiter row with a pipe, and the paragraph line `above` it
    has as many cells: the two open a table (GitHub, markdownlint)."""
    return "|" in row and bool(TABLE_DELIMITER.match(row)) and _cells(_plain(above)) == _cells(row)


def _setext(blocks: Blocks, lines: list[str], first: int, i: int, underline: str, nested: bool, para):
    """Reads line i, a Setext underline, under the paragraph lines[first:i] and adds the heading. Link
    reference definitions that open the paragraph are no part of it (CommonMark 4.7), and with nothing else
    above, '---' is a thematic break and '===' or '--' opens a paragraph. Returns (open paragraph, its
    first line)."""
    top = first
    while top < i and _LINK_DEFINITION.match(_plain(lines[top])):
        top += 1
    if top < i:
        level = 1 if underline.strip()[0] == "=" else 2
        text = " ".join((_plain(line) if nested else line.strip()) for line in lines[top:i])
        (blocks.nested if nested else blocks.headers).append(Header(top, level, text, i - top + 1))
        return None, first
    if THEMATIC_BREAK.match(underline):
        return None, first
    return para, i


def _inside(x: str, depth: int, column: int) -> str | None:
    """The line `x` without the block quote markers and the list indent that hold a fenced block, or None
    when the line no longer belongs to them."""
    if depth:
        found, x = _quotes(x, depth)
        if found < depth:
            return None
    if column:
        if not x.strip():
            return ""
        if _indent(x) < column:
            return None
        x = x[column:]
    return x


def _plain(line: str) -> str:
    """A line of a paragraph inside a block quote or a list item, without the markers in front of it."""
    _, rest = _quotes(line.expandtabs(4))
    item = _list_item(rest)
    return (item[1] if item else rest).strip()


def _blocks(lines: list[str], start: int) -> Blocks:
    """The block structure of lines[start:] as CommonMark reads it, as far as the rules need it: which lines
    are code, HTML or blank, the top-level ATX and Setext headings that part the document into sections,
    and the headings inside block quotes and list items, which count only toward the heading rules."""
    blocks = Blocks(["front"] * start)
    kinds = blocks.kinds
    fence = None  # (marker, block quote depth, list column, first line) while in fenced code
    html_end = None  # while in an HTML block: the pattern that ends it, or "" when a blank line does
    lists = []  # content columns of the open list items, the innermost last
    # Where the open paragraph is: ("top", 0), ("list", column), ("listquote", column) for a block quote that
    # opens a list item, ("quote", depth), ("quotelist", depth, column) for a list item in a block quote, or
    # a GFM table, ("table", column) or ("quotetable", depth), which takes no Setext underline or lazy line.
    para = None
    first = 0  # the open paragraph's first line
    for i in range(start, len(lines)):
        x = lines[i].expandtabs(4)
        if fence is not None:
            inner = _inside(x, fence[1], fence[2])
            if inner is not None:
                kinds.append("fence")
                if _closes_fence(inner, fence[0]):
                    fence = None
                continue
            fence = None  # the block quote or list item around the code ended, and the code with it
        if html_end is not None:
            if html_end == "" and not x.strip():
                html_end = None  # the blank line that ends the block is read below
            else:
                kinds.append("html")
                if html_end != "" and html_end.search(x):
                    html_end = None
                continue
        if not x.strip():
            kinds.append("blank")
            para = None
            continue
        indent = _indent(x)
        keep = len(lists)
        while keep and indent < lists[keep - 1]:
            keep -= 1
        if keep < len(lists):
            if para is not None and para[0] not in TABLES and not _starts_block(x[lists[keep - 1] if keep else 0:]):
                kinds.append("text")  # a lazy continuation of the paragraph: the list items stay open
                continue
            del lists[keep:]
            para = None  # the line opens a block, which ends the paragraph of the closed list items
        base = lists[-1] if lists else 0
        here = ("list", base) if base else ("top", 0)
        if indent - base >= 4:
            if para is not None and para[0] not in TABLES:
                kinds.append("text")  # indented code cannot interrupt a paragraph
            else:
                kinds.append("code")  # but it ends a table
                para = None
            continue
        y = x[base:]
        if para is not None and para[0] == "quotetable" and not QUOTE_MARK.match(y):
            para = None  # a table takes no lazy line: the block quote around it ended
        if para == here and SETEXT.match(y):
            kinds.append("text")
            para, first = _setext(blocks, lines, first, i, y, bool(base), para)
            continue
        if para == here and _table_starts(y, lines[i - 1]):
            kinds.append("text")
            para = ("table", base)
            continue
        m = ATX.match(y if base else lines[i])
        if m:
            heading = Header(i, len(m.group(1)), CLOSING_HASHES.sub("", m.group(2)).strip())
            (blocks.nested if base else blocks.headers).append(heading)
            kinds.append("text")
            para = None
            continue
        mark = _opens_fence(y)
        if mark:
            fence = (mark, 0, base, i)
            kinds.append("fence")
            para = None
            continue
        if not base:
            end = _html_end(y.lstrip(" "), para is not None)
            if end is not None:
                kinds.append("html")
                para = None
                if end == "" or not end.search(y):
                    html_end = end
                continue
        depth, rest = _quotes(y)
        if depth:
            kinds.append("text")
            kind = para[0] if para is not None else None
            if kind in QUOTED and para[1] >= depth:
                # What is open in the block quote goes on unless this line opens a block. A line with fewer
                # markers, or outside the list item, goes on only lazily: never as an underline or a table row.
                same = para[1] == depth
                inside = same and kind == "quotelist" and _indent(rest) >= para[2]
                z = rest[para[2]:] if inside else rest
                if same and (kind == "quote" or inside) and SETEXT.match(z):
                    para, first = _setext(blocks, lines, first, i, z, True, para)
                    continue
                if same and kind == "quote" and _table_starts(rest, lines[i - 1]):
                    para = ("quotetable", depth)
                    continue
                if kind == "quotetable":
                    goes_on = same and _indent(rest) < 4 and not _starts_block(rest)
                elif same and (kind == "quote" or inside):
                    goes_on = not _interrupts_paragraph(z)
                else:
                    goes_on = not _starts_block(rest)
                if goes_on:
                    continue
            para = None  # a block quote ends a paragraph or a table outside it
            item = _list_item(rest)
            column, inner = item if item else (0, rest)
            m = ATX.match(inner)
            mark = _opens_fence(inner)
            if m:
                blocks.nested.append(Header(i, len(m.group(1)), CLOSING_HASHES.sub("", m.group(2)).strip()))
            elif mark:
                fence = (mark, depth, column, i)
                kinds[-1] = "fence"
            elif THEMATIC_BREAK.match(rest):
                pass
            elif not item and _indent(rest) >= 4:
                kinds[-1] = "code"
            elif inner.strip():
                para, first = (("quotelist", depth, column) if item else ("quote", depth)), i
            continue
        item = _list_item(y)
        if item and para == here and not _interrupts(y, item[1]):
            item = None  # a list item that may not interrupt the paragraph continues it
        if item:
            column, rest = item
            lists.append(base + column)
            para = None
            if _indent(rest) >= 4:
                kinds.append("code")
                continue
            kinds.append("text")
            m = ATX.match(rest)
            mark = _opens_fence(rest)
            if m:
                blocks.nested.append(Header(i, len(m.group(1)), CLOSING_HASHES.sub("", m.group(2)).strip()))
            elif mark:
                fence = (mark, 0, base + column, i)
                kinds[-1] = "fence"
            elif QUOTE_MARK.match(rest):
                para, first = ("listquote", base + column), i  # a block quote opens the item
            elif rest.strip():
                para, first = ("list", base + column), i
            continue
        kinds.append("text")
        if THEMATIC_BREAK.match(y):
            para = None
        elif para is None:
            para, first = here, i
    if fence is not None:  # a fence that never closes runs to the end, and its body is not code-only
        for j in range(fence[3], len(lines)):
            if kinds[j] == "fence":
                kinds[j] = "open"
    return blocks


def _headers(lines: list[str], start: int) -> list[Header]:
    """The top-level ATX and Setext headings, which part the document into sections."""
    return _blocks(lines, start).headers


def _outside_fences(lines: list[str], start: int, end: int | None = None):
    """(index, line) for every line of lines[start:end] outside fenced code. Kept for scripts that read the
    document the way the checker did before _blocks."""
    kinds = _blocks(lines, start).kinds
    for i in range(start, len(lines) if end is None else end):
        if kinds[i] not in ("fence", "open"):
            yield i, lines[i]


def _is_code_only(body: str, kinds: list[str] | None = None) -> bool:
    """True when the body is one dynamic injection line, or nothing but code: fenced blocks that close and
    indented blocks. `kinds` are the kinds of the body's lines in the document; without them the body is
    read on its own."""
    if DYNAMIC_INJECTION.match(body):
        return True
    if kinds is None:
        kinds = _blocks(body.split("\n"), 0).kinds
    seen = False
    for kind in kinds:
        if kind in ("fence", "code"):
            seen = True
        elif kind != "blank":
            return False
    return seen


def _bracketed(text: str) -> list[str]:
    """Each '[...]' that holds 추후 or 작성 예정, as the pattern \\[[^\\]]*(추후|작성 예정)[^\\]]*\\] finds
    them, in linear time: a bracket that opens before the first ']' after it and holds a mark."""
    found = []
    start = 0
    while True:
        close = text.find("]", start)
        if close < 0:
            return found
        open_ = text.find("[", start, close)
        if open_ >= 0:
            span = text[open_:close + 1]
            if "추후" in span or "작성 예정" in span:
                found.append(span)
        start = close + 1


def _without_tex(text: str) -> str:
    """`text` with TeX math \\( ... \\) and \\[ ... \\] blanked out, keeping every other character where it
    was."""
    for opening, closing in (("\\(", "\\)"), ("\\[", "\\]")):
        start = text.find(opening)
        if start < 0:
            continue
        out, last = [], 0
        while start >= 0:
            close = text.find(closing, start + 2)
            if close < 0:
                break
            out.append(text[last:start])
            out.append(" " * (close + 2 - start))
            last = close + 2
            start = text.find(opening, last)
        out.append(text[last:])
        text = "".join(out)
    return text


def _windows_paths(content: str):
    """The paths written with backslashes in the line `content`, outside inline code (Tom, 2026. 11. 1.:
    code blocks and inline code are exceptions)."""
    scan = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), content)
    for m in WINDOWS_PATH.finditer(_without_tex(scan)):
        yield m.group(0)


def _label(text: str) -> str:
    """A link label as CommonMark matches it: case folded, its runs of whitespace one space."""
    return " ".join(text.split()).casefold()


def _labels(lines: list[str], kinds: list[str]) -> set:
    """The labels that the document's link reference definitions define."""
    found = set()
    for i, line in enumerate(lines):
        if kinds[i] == "text" and "]:" in line:
            m = _LINK_DEFINITION.match(_plain(line))
            if m:
                found.add(_label(m.group(1)))
    return found


def _without_links(text: str, labels: set) -> str:
    """The line `text` with its links and a link reference definition blanked out (see _INLINE_LINK)."""
    text = _INLINE_LINK.sub(" ", text)
    if labels:
        text = _REFERENCE_LINK.sub(lambda m: " " if _label(m.group(2) or m.group(1)) in labels else m.group(0), text)
        text = _SHORTCUT_LINK.sub(lambda m: " " if _label(m.group(1)) in labels else m.group(0), text)
    return _LINK_DEFINITION.sub(" ", text)


def _after_comments(lines: list[str], kinds: list[str], start: int) -> int:
    """The first line from `start` that is neither blank nor part of an HTML comment block, which
    markdownlint's MD041 passes over: an HTML block that opens with '<!--' and ends with '-->'. len(lines)
    when there is none."""
    i = start
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if kinds[i] == "html" and lines[i].lstrip(" ").startswith("<!--"):
            j = i
            while j + 1 < len(lines) and kinds[j + 1] == "html" and "-->" not in lines[j]:
                j += 1
            comment = "\n".join(lines[i:j + 1]).strip()
            inner = comment[4:-3]
            if (comment.endswith("-->") and len(comment) >= 7 and not inner.startswith((">", "->"))
                    and not inner.endswith("-")):
                i = j + 1
                continue
        return i
    return len(lines)


def check_text(text: str, rel: str) -> Report:
    """Problems of `text` read as the document at path `rel` (relative to the repository or scratchpad).

    Every broken rule is reported at every place it breaks, sorted by line (0, the whole document, first).
    """
    report = Report()

    def add(code, line, detail, *key, variant=None):
        rule = RULES[code] if variant is None else RULES[code][variant]
        report.problems.append(Problem(code, line, f"{rule} {detail}", (code, *key)))

    if text.startswith("\ufeff"):
        add("D013", 1, "이 문서는 BOM으로 시작합니다.")
        text = text[1:]
    if "\r" in text:
        add("D001", 0, "이 문서에는 CRLF 줄 끝이 있습니다.")
        text = lf(text)
    if not text.endswith("\n"):
        add("D002", 0, "이 문서는 줄 바꿈으로 끝나지 않습니다.")
    elif TRAILING_BLANK.search(text):
        add("D003", 0, "이 문서는 끝에 빈 줄이 있습니다.")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    while len(lines) > 1 and lines[-1].strip() == "":
        lines = lines[:-1]

    fm_len, _ = _front_matter(lines)
    blocks = _blocks(lines, fm_len)
    headers, kinds = blocks.headers, blocks.kinds
    first = headers[0].index if headers else len(lines)
    # HTML comments above the first header are neither body nor the document's first line (MD041).
    opening = _after_comments(lines, kinds, fm_len)
    stray = next((i for i in range(opening, first) if lines[i].strip()), None)
    if stray is not None:
        add("D004", stray + 1, "첫 헤더 앞에 본문이 있습니다.", "\n".join(lines[opening:first]).strip())
    if opening < len(lines) and not (headers and headers[0].index == opening and headers[0].level == 1):
        shown = lines[opening].strip()
        shown = shown if len(shown) <= 40 else shown[:40] + "…"
        add("D029", opening + 1, f"이 문서는 H1 헤더가 아닌 줄로 시작합니다('{shown}').", lines[opening])

    # H1 count, depth and MD001 count every heading, those inside block quotes and list items too.
    headings = sorted(headers + blocks.nested, key=lambda h: h.index)
    h1 = [h for h in headings if h.level == 1]
    if len(h1) != 1:
        detail = f"이 문서에는 H1이 {len(h1)}개 있습니다." if h1 else "이 문서에는 H1이 없습니다."
        add("D005", h1[1].index + 1 if h1 else 1, detail, len(h1))
    top = [h for h in headers if h.level == 1]
    if top and top[0].text != rel:
        add("D006", top[0].index + 1, f"지금 H1 제목은 '{top[0].text}'이고, 이 문서의 경로는 '{rel}'입니다.", top[0].text)
    previous = None
    for h in headings:
        raw = lines[h.index]
        if h.level > MAX_LEVEL:
            add("D021", h.index + 1, f"{_named(h)}는 H{h.level}입니다.", raw)
        if previous is not None and h.level > previous + 1:
            add("D022", h.index + 1, f"{_named(h)}는 H{previous} 바로 다음의 H{h.level}입니다.", raw, previous)
        previous = h.level

    report.h2_count = sum(1 for h in headers if h.level == 2)
    if report.h2_count > MAX_H2:
        add("D023", 0, f"이 문서에는 H2가 {report.h2_count}개 있습니다.")

    # Placeholders and paths are checked on every line outside code, front matter and headers included. A
    # bracket inside inline code or a link only names the form, while a path in inline code is a path.
    labels = _labels(lines, kinds)
    for i, content in enumerate(lines):
        if kinds[i] in CODE:
            continue
        if "[" in content:
            scan = _without_links(_CODE_SPAN.sub(" ", content), labels)
            for found in _bracketed(scan):
                add("D011", i + 1, f"대괄호로 쓴 플레이스홀더가 있습니다({found}).", found)
        if "\\" in content:
            for path in _windows_paths(content):
                shown = _PATH_TAIL.sub(r"\1", path)
                add("D025", i + 1, f"역슬래시로 쓴 경로가 있습니다({shown}).", path)

    notations: dict[int, Notation | None] = {}
    counts: dict[int, int | None] = {}  # what each leaf measures, for the sum its H2 should carry
    for k, h in enumerate(headers):
        line = h.index + 1
        raw = lines[h.index]
        parent = k + 1 < len(headers) and headers[k + 1].level > h.level
        if h.level in (2, 3):
            _check_blank_lines(add, h, lines, fm_len, raw)
        # The header form covers H2s with H3s and every header that carries a body.
        role = "parent" if parent and h.level == 2 else "leaf" if not parent and h.level > 1 else None
        if role:
            notations[k] = parse_notation(h.text)
            _check_notation(add, h, notations[k], line, raw, role)

        begin = h.index + h.span
        end = headers[k + 1].index if k + 1 < len(headers) else len(lines)
        body_lines = lines[begin:end]
        body = "\n".join(body_lines).strip()
        code = _is_code_only(body, kinds[begin:end])
        if parent or h.level == 1:
            if body:
                # A body that should not be here still breaks the length rule when it is not 200.
                where = "H1" if h.level == 1 else "하위 헤더가 있는"
                add("D008", line, f"{where} {_named(h)} 아래에 본문이 있습니다.", body)
                _measure(add, h, body, line, code)
            continue

        notation = notations.get(k)
        if not body:
            counts[k] = 0
            if not _excused(h.text, notation):
                add("D009", line, f"{_named(h)} 아래 본문이 비어 있습니다.", raw)
            elif _filled(notation) not in (None, "0"):
                add("D028", line, f"{_named(h)}는 본문이 없는데 글자 수 칸이 {_filled(notation)}입니다.", raw, "")
            continue
        if body_lines and body_lines[0].strip() == "":
            add("D010", line, f"{_named(h)} 바로 아래에 빈 줄이 있습니다.", raw)
        near = None if TODO.search(body) else NEAR_TODO.search(body, max(0, len(body) - NEAR_TODO_WINDOW))
        if near:
            shown = " ".join(near.group(0).split())  # on one line, so the message stays one line
            add("D009", line, f"{_named(h)} 본문 끝의 미완성 표시가 '{shown}'입니다.", raw, "near")
        measured = _measure(add, h, body, line, code)
        counts[k] = measured if measured is not None else value_of(notation)
        written = _filled(notation)
        if measured is None or written is None or written == str(measured):
            continue
        if TODO.search(body):
            add("D019", line, f"{_named(h)}의 글자 수 칸은 {written}입니다.", raw, body)
        elif not DIGITS.match(written):
            add("D028", line, f"{_named(h)}의 글자 수 칸에 숫자가 아닌 글자가 있습니다('{written}'). "
                f"본문은 공백 제외 {measured}자입니다.", raw, body)
        else:
            add("D028", line, f"{_named(h)}의 글자 수 칸은 {written}인데 본문은 공백 제외 {measured}자입니다.",
                raw, body)

    for k, h in enumerate(headers):
        if h.level != 2:
            continue
        children = []
        for j in range(k + 1, len(headers)):
            if headers[j].level <= 2:
                break
            if headers[j].level == 3:
                children.append(j)
        if len(children) > MAX_H3:
            notation = parse_notation(h.text)
            add("D026", h.index + 1, f"{_named(h)} 아래에 H3 헤더가 {len(children)}개 있습니다.",
                notation.title if notation else h.text, len(children))
        written = _filled(notations.get(k)) if children else None
        values = [counts.get(j) for j in children]
        if written is None or any(v is None for v in values):
            continue
        total = sum(values)
        if written == str(total):
            continue
        if DIGITS.match(written):
            detail = f"{_named(h)}의 글자 수 칸은 {written}인데 하위 H3 헤더의 공백 제외 본문 글자 수의 합은 {total}입니다."
        else:
            detail = (f"{_named(h)}의 글자 수 칸에 숫자가 아닌 글자가 있습니다('{written}'). "
                      f"하위 H3 헤더의 공백 제외 본문 글자 수의 합은 {total}입니다.")
        add("D020", h.index + 1, detail, lines[h.index], total)

    _check_politeness(add, headers, lines, kinds)
    report.problems.sort(key=lambda p: p.line)
    return report


def _check_blank_lines(add, h: Header, lines: list[str], start: int, raw: str) -> None:
    """Exactly two empty lines between a header and the text above it."""
    above = lines[max(start, h.index - 3):h.index]
    if len(above) == 3 and above[1] == "" and above[2] == "" and above[0].strip() != "":
        return
    blank, i = 0, h.index - 1
    while i >= start and lines[i].strip() == "":
        blank, i = blank + 1, i - 1
    if blank != 2:
        detail = f"{_named(h)} 위에 빈 줄이 {_lines_counted(blank)} 있습니다." if blank else f"{_named(h)} 위에 빈 줄이 없습니다."
    elif i < start:
        detail = f"{_named(h)} 위에는 빈 줄만 있고 앞선 내용이 없습니다."
    else:
        detail = f"{_named(h)} 위의 빈 줄에 공백 문자가 들어 있습니다."
    add("D007", h.index + 1, detail, raw, variant=h.level)


def _excused(text: str, notation: Notation | None) -> bool:
    """True for a header whose title holds an @import or a {placeholder}: its body may stay empty."""
    title = notation.title if notation is not None else text
    return bool(IMPORT_HEADER.search(title) or HEADER_PLACEHOLDER.search(title))


def _measure(add, h: Header, body: str, line: int, code: bool) -> int | None:
    """The count the header should carry: 0 for an unfinished body, else its length without whitespace,
    reported when it is not 200. None for code, which is not measured."""
    if code:
        return None
    if TODO.search(body):
        return 0
    n = count_chars(body)
    if n != TARGET:
        add("D012", line, f"{_named(h)}의 본문은 {n}자입니다({n - TARGET:+d}).", body)
    return n


def _check_notation(add, h: Header, n: Notation | None, line: int, raw: str, role: str) -> None:
    if n is None:
        detail = f"{_named(h)}는 쉼표로 나눈 칸이 {len(h.text.split(','))}개뿐입니다." if h.text else "빈 헤더입니다."
        add("D014", line, detail, raw, variant=role)
        return
    if not _parted(h.text):
        add("D014", line, f"{_named(h)}의 칸 사이가 쉼표와 공백 한 칸(', ')이 아닙니다.", raw, "separator",
            variant=role)
    for name, value in zip(CELLS, (n.author, n.title, n.date, n.amount, n.etc)):
        if _blank(value):
            shown = f"'{value}'로 비어 있습니다" if _visible(value) else "비어 있습니다"
            add("D018", line, f"{_named(h)}의 {name} 칸이 {shown}.", raw, name, variant=role)
    # A person (Tom, J, MGM) is a valid author of what that person wrote; the hook, which only sees
    # Claude writing, stops Claude from putting a person's name on a header it adds or changes.
    if not _blank(n.author) and not CLAUDE.match(n.author) and n.author not in PEOPLE:
        add("D015", line, f"{_named(h)}의 작성자는 '{n.author}'입니다.", raw)
    if not _blank(n.date) and not _valid_date(n.date):
        add("D016", line, f"{_named(h)}의 작성일은 '{n.date}'입니다.", raw)


def _has_bieup(syllable: str) -> bool:
    """True for a Hangul syllable whose final consonant is ㅂ, as in 합, 습 and 입."""
    return (ord(syllable) - 0xAC00) % 28 == 17


def _ending(word: str) -> str | None:
    """'polite' for -ㅂ니다, -ㅂ니까, 요, 죠, 시오 and -ㅂ시다, 'haera' for 해라체 '-다' (아니다 and 다니다
    included), None for the rest."""
    if word.endswith(NOUNS_ENDING_IN_YO):
        return None
    if word.endswith(("니다", "니까")):
        if len(word) >= 3 and _has_bieup(word[-3]):
            return "polite"
        return "haera" if word.endswith("다") else None
    if word.endswith(POLITE_ENDINGS):
        return "polite"
    if word.endswith("시다") and not word.endswith(("입시다", "집시다")) and len(word) >= 3 and _has_bieup(word[-3]):
        return "polite"  # 합시다, 갑시다
    if word.endswith("다") and word != "다" and not word.endswith(NOUNS_ENDING_IN_DA):
        return "haera"
    return None


def _clause(before: str, word: str) -> bool:
    """True when a 해라체 `word` before ; : or a dash ends a clause, not a name or a headword such as
    '레몬 소다:' or '**적확하다** —': a word of `before` ends in a particle ('원인은', 'Windows는',
    '가지로'), or 다 follows a syllable with ㅆ (됐다, 했다)."""
    if len(word) >= 2 and (ord(word[-2]) - 0xAC00) % 28 == 20:
        return True
    for token in before.split():
        token = _WORD_TAIL.sub("", token)
        if len(token) >= 2 and token.endswith(PARTICLES):
            return True
    return False


def _aside_clause(inner: str) -> str | None:
    """`inner`, what a parenthesis or a bracket holds (quotes and code already blanked), when it is a spaced
    clause that ends in 해라체 '-다': '(원인은 아직 모른다)', '[다음 세션에서 다시 본다.]'. Tom (2026. 11. 1.)
    exempts only quoted words, so such an aside is Claude's own sentence. A name or a headword, such as
    '(레몬 소다)' or '(버릇없다)', is none, by the same test as before ; : and a dash (_clause)."""
    said = inner.strip()
    m = _LAST_WORD.search(said)
    if not m or " " not in said:
        return None
    word = m.group(1)
    if _ending(word) != "haera":
        return None
    if _clause(said[:m.start()], word) or _ENDS_SENTENCE.search(said):
        return said
    return None


def _nominal(word: str) -> bool:
    """True for a 개조식 ending such as 확인함 or 없음, but not for a noun that ends alike, such as 포함."""
    return word.endswith(NOMINAL_ENDINGS) and not word.endswith(NOUNS_ENDING_NOMINALLY)


def _msub(pattern, text: str, spans: list, put: str = " ", keep: int | None = None):
    """pattern.sub over `text` that carries `spans`, the columns of the source line each character came
    from, along. A match becomes its group `keep` when given, whose first and last characters then stand
    from the start and up to the end of the match, else `put`, whose characters all stand for the whole
    match."""
    parts, where, last = [], [], 0
    for m in pattern.finditer(text):
        a, b = m.span()
        parts.append(text[last:a])
        where.extend(spans[last:a])
        if keep is not None:
            ka, kb = m.span(keep)
            if kb > ka:
                kept = spans[ka:kb]
                kept[0] = (spans[a][0], kept[0][1])
                kept[-1] = (kept[-1][0], spans[b - 1][1])
                parts.append(text[ka:kb])
                where.extend(kept)
        elif put and b > a:
            parts.append(put)
            where.extend([(spans[a][0], spans[b - 1][1])] * len(put))
        last = b
    if not parts:
        return text, spans
    parts.append(text[last:])
    where.extend(spans[last:])
    return "".join(parts), where


def _uncomment(text: str, spans: list):
    """`text` without its HTML comments, and True when the last one stays open past the end of the line."""
    parts, where, last = [], [], 0
    while True:
        opening = text.find("<!--", last)
        if opening < 0:
            break
        closing = text.find("-->", opening + 4)
        parts.append(text[last:opening])
        where.extend(spans[last:opening])
        if closing < 0:
            return "".join(parts), where, True
        parts.append(" ")
        where.append((spans[opening][0], spans[closing + 2][1]))
        last = closing + 3
    if not parts:
        return text, spans, False
    parts.append(text[last:])
    where.extend(spans[last:])
    return "".join(parts), where, False


def _korean_sentences(lines: list[str], start: int, end: int, kinds: list[str] | None = None):
    """(line index, sentence, ending) for the Korean sentences of lines[start:end]; see _sentences."""
    if kinds is None:
        kinds = _blocks(lines, _front_matter(lines)[0]).kinds
    for i, sentence, ending, _ in _sentences(lines, kinds, start, end):
        yield i, sentence, ending


def _sentences(lines: list[str], kinds: list[str], start: int, end: int):
    """(line index, sentence, ending, the sentence as the line writes it) for the Korean sentences of
    lines[start:end], outside code, HTML comments, tables and block quotes. A line without a sentence mark
    counts when it ends in 존댓말, 해라체 or, unless it is a '이름: 값' label, a nominal ending such as
    확인함; and a sentence that is English but for the Korean word it ends on stays out."""
    in_comment = False
    for i in range(start, end):
        if kinds[i] in CODE:
            continue
        line = lines[i]
        text, spans = line, [(c, c + 1) for c in range(len(line))]
        if in_comment:
            closing = line.find("-->")
            if closing < 0:
                continue
            in_comment = False
            text, spans = text[closing + 3:], spans[closing + 3:]
        lead = len(text) - len(text.lstrip())
        text, spans = text[lead:].rstrip(), spans[lead:]
        spans = spans[:len(text)]
        if not text or text.startswith(("|", ">")):
            continue
        text, spans = _msub(_LIST_MARK, text, spans, put="")
        text, spans = _msub(_CODE_SPAN, text, spans)
        text, spans, in_comment = _uncomment(text, spans)
        text, spans = _msub(_LINK, text, spans, keep=1)
        for pattern in (_URL, _TAG, _QUOTED):
            text, spans = _msub(pattern, text, spans)
        asides = []  # (clause, as the line writes it) for the spaced 해라체 clauses in parentheses or brackets
        for _ in range(MAX_ASIDE_DEPTH):
            for m in _ASIDE.finditer(text):
                said = _aside_clause(m.group(0)[1:-1]) if m.group(0)[0] in "(（[" else None
                if said:
                    asides.append((said, line[spans[m.start()][0]:spans[m.end() - 1][1]]))
            previous = text
            text, spans = _msub(_ASIDE, text, spans, put="")
            if text == previous:
                break
        for said, written in asides:
            yield i, said, "haera", written
        text, spans = _msub(_DATE_TEXT, text, spans, put="날짜")
        label = bool(_LABEL.match(text))
        offset = cut = 0  # where the piece starts, and where the break before it starts
        for a, b in [m.span() for m in _SENTENCE_BREAK.finditer(text)] + [(len(text), len(text))]:
            piece, begin, opening = text[offset:a], cut, offset
            lo = offset + len(piece) - len(piece.lstrip())
            hi = offset + len(piece.rstrip())
            soft = a < len(text) and ((a > 0 and text[a - 1] in ";:") or text[a:b].strip() in ("—", "–"))
            offset, cut = b, a
            if lo >= hi:
                continue
            sentence = text[lo:hi]
            m = _LAST_WORD.search(sentence)
            if not m:
                continue
            before = sentence[:m.start()]
            if not _HANGUL.search(before) and _LATIN_WORD.search(before):
                english = sum(1 for t in sentence.split() if _LATIN_WORD.search(t) and not _HANGUL.search(t))
                if len(_HANGUL.findall(sentence)) < english:
                    continue  # an English sentence that ends on a Korean word
            word = m.group(1)
            ending = _ending(word)
            if word == "다" and m.start(1) and _GLUED.match(sentence[m.start(1) - 1]):
                ending = "haera"  # CRLF다, 77%다
            if ending == "haera" and soft and not _clause(before, word):
                continue  # a name or a headword before ; : or a dash
            if ending or _ENDS_SENTENCE.search(sentence) or (not label and _nominal(word)):
                # As the line has it, from where the sentence starts (a quote, code or a link that opens it
                # included; the break before it, spaces and an HTML comment not) up to the last word and what
                # closes it; a long aside after the word is '…'.
                j = begin
                while j < lo:
                    source = line[spans[j][0]:spans[j][1]]
                    if not (source.isspace() or source.startswith("<!--")
                            or (j < opening and source in ("*", "_", "~", "—", "–"))):
                        break
                    j += 1
                start_at, word_end, end_at = spans[j][0], spans[lo + m.end(1) - 1][1], spans[hi - 1][1]
                tail = line[word_end:end_at]
                yield i, sentence, ending, line[start_at:word_end] + (tail if len(tail) <= 3 else "…")


def _check_politeness(add, headers: list[Header], lines: list[str], kinds: list[str]) -> None:
    """존댓말 in the sections Claude wrote in its own voice (D027). A section with no 존댓말 ending at all
    is one problem; otherwise each sentence that ends in 해라체 '-다' is one."""
    for k, h in enumerate(headers):
        n = parse_notation(h.text) if h.level > 1 else None
        if n is None or not CLAUDE.match(n.author) or n.etc in PEOPLE:
            continue
        end = headers[k + 1].index if k + 1 < len(headers) else len(lines)
        sentences = list(_sentences(lines, kinds, h.index + h.span, end))
        if not sentences:
            continue
        if not any(ending == "polite" for _, _, ending, _ in sentences):
            add("D027", h.index + 1, f"{_named(h)} 아래 본문에는 존댓말로 끝나는 문장이 하나도 없습니다.",
                "section", tuple(sentence for _, sentence, _, _ in sentences))
            continue
        for i, sentence, ending, written in sentences:
            if ending == "haera":
                shown = written if len(written) <= 40 else "…" + written[-40:]
                add("D027", i + 1, f"'{shown}' 문장이 해라체로 끝납니다.", sentence)


# Documents the rules cover


def _top_patterns(root: str) -> list:
    try:
        text = Path(root, TOP_LIST).read_text(encoding="utf-8")
    except OSError:
        return []
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # {user} and {USER} name the same account, the second in capitals: team/tom/TOM.md, not team/j/TOM.md.
        # Whichever comes first on the line captures the name, in either order; the same spelling later
        # repeats it, and the other spelling repeats it in any case.
        pattern, captured = "", None
        for k, piece in enumerate(re.split(r"(\{user\}|\{USER\})", line)):
            if k % 2 == 0:
                pattern += re.escape(piece)
            elif captured is None:
                pattern, captured = pattern + "(?P<user>[^/]+)", piece
            else:
                pattern += "(?P=user)" if piece == captured else "(?i:(?P=user))"
        patterns.append(re.compile(f"^{pattern}$", CASE))
    return patterns


def _fold(name: str) -> str:
    return name.casefold() if CASE else name


def _work_tree(folder: str) -> str | None:
    """The nearest folder at or above `folder` that holds a .git entry, or None."""
    current = folder
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _outside(absolute: str) -> tuple[str, str] | None:
    """Markdown outside the repository is covered too (Tom, 2026. 11. 5.: wherever it is). Its H1 path is
    relative to the work tree holding it, or to its own folder when no work tree does; company/forum and
    the skipped folders stay out there as they do here."""
    folder = os.path.dirname(absolute)
    base = _work_tree(folder) or folder
    rel = os.path.relpath(absolute, base).replace("\\", "/")
    parts = [_fold(p) for p in rel.split("/")]
    if tuple(parts[:2]) == FORUM or SKIP_DIRS.intersection(parts[:-1]):
        return None
    return base, rel


def locate(path: str, root: str) -> tuple[str, str] | None:
    """(folder the H1 path is relative to, document path) when the rules cover `path`, else None."""
    absolute = os.path.abspath(path)
    if not absolute.lower().endswith(MARKDOWN):
        return None
    m = SCRATCHPAD.match(absolute) or MEMORY.match(absolute)
    if m:
        rel = os.path.relpath(absolute, m.group(1)).replace("\\", "/")
        if m.re is MEMORY and _fold(rel) == _fold(MEMORY_INDEX):
            return None
        return m.group(1), rel
    base = os.path.abspath(root)
    try:
        rel = os.path.relpath(absolute, base)
    except ValueError:  # another drive
        return _outside(absolute)
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return _outside(absolute)
    rel = rel.replace("\\", "/")
    parts = [_fold(p) for p in rel.split("/")]
    if tuple(parts[:2]) == FORUM or SKIP_DIRS.intersection(parts[:-1]):
        return None
    if any(p.match(rel) for p in _top_patterns(base)):
        return None
    return base, rel


def folder_count(folder: str) -> int:
    """Markdown documents directly inside `folder`."""
    try:
        return sum(1 for e in os.scandir(folder) if e.is_file() and e.name.lower().endswith(MARKDOWN))
    except OSError:
        return 0


# Hook


def _read(path: str) -> tuple[str | None, bool]:
    """(text, exists). Text is None when the file is missing or not UTF-8."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return None, True


def rebuild(tool: str, tool_input: dict, before: str | None) -> str | None:
    """The document after the call, or None when the call would fail or cannot be rebuilt here. Edits
    follow Claude Code's tools: an empty old_string fills a missing, empty or blank file; straight quotes
    in old_string find curly ones; and deleting text that a line break follows deletes the break too."""
    if tool == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    if not isinstance(edits, list):
        return None
    text = before
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if not old:
            if text is not None and text.strip():
                return None  # the tool will not create a file that already has content
            text = new
            continue
        if text is None:
            return None
        old, new, base = lf(old), lf(new), lf(text)
        if old not in base:
            at = base.translate(QUOTES).find(old.translate(QUOTES))
            if at < 0:
                return None
            old = base[at:at + len(old)]
        replace_all = bool(edit.get("replace_all"))
        found = base.count(old)
        if found > 1 and not replace_all:
            return None
        if not new and not old.endswith("\n") and old + "\n" in base:
            old += "\n"
        text = base.replace(old, new) if replace_all else base.replace(old, new, 1)
    return text


def added(old: Report, new: Report) -> list[Problem]:
    """Problems of `new` that `old` did not have. The H2 count of a document and the H3 count under an
    H2 block only when they grow."""
    seen = Counter(p.key for p in old.problems)
    h3_before = {p.key[1]: p.key[2] for p in old.problems if p.code == "D026"}
    out = []
    for p in new.problems:
        if p.code == "D023":
            if new.h2_count > max(old.h2_count, MAX_H2):
                out.append(p)
            continue
        if p.code == "D026":
            if p.key[2] > h3_before.get(p.key[1], MAX_H3):
                out.append(p)
            continue
        if seen[p.key] > 0:
            seen[p.key] -= 1
        else:
            out.append(p)
    return out


def _people_as_authors(after: str, before: str) -> list[Problem]:
    """D015 for each header line this change adds or alters that names Tom, J or MGM as its author.
    Whatever the hook sees is Claude writing, and Claude's headers name Claude."""
    old_lines = set(before.split("\n"))
    lines = after.split("\n")
    fm_len, _ = _front_matter(lines)
    found = []
    for h in _headers(lines, fm_len):
        raw = lines[h.index]
        n = parse_notation(h.text) if h.level > 1 else None
        if n is not None and n.author in PEOPLE and raw not in old_lines:
            found.append(Problem("D015", h.index + 1, f"{RULES['D015']} {_named(h)}의 작성자가 '{n.author}'인데, "
                                 "이 헤더는 지금 Claude가 쓰고 있습니다.", ("D015", raw, "writer")))
    return found


def run_hook(payload: dict, strict: bool = False) -> tuple[int, str]:
    """(exit code, stderr text) for one PreToolUse payload."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if tool not in TOOLS or not isinstance(path, str) or not path:
        return 0, ""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or str(Path(__file__).resolve().parents[2])
    place = locate(path, root)
    if place is None:
        return 0, ""
    _, rel = place
    before, exists = _read(path)
    if exists and before is None and tool != "Write":  # an Edit of a file that is not UTF-8 is left to the tool
        return 0, ""
    after = rebuild(tool, tool_input, before)
    if after is None:
        return 0, ""

    old = check_text(lf(before), rel) if before is not None else Report()
    new = check_text(lf(after), rel)
    fresh = list(new.problems) if strict else added(old, new)
    fresh = [p for p in fresh if p.code != "D001"]
    kept = len([p for p in new.problems if p.code != "D001"]) - len(fresh)
    count = folder_count(os.path.dirname(os.path.abspath(path)))
    if (not exists and count + 1 > MAX_DOCS_PER_FOLDER) or (strict and count > MAX_DOCS_PER_FOLDER):
        fresh.insert(0, Problem("D024", 0, f"{RULES['D024']} 이 폴더에는 Markdown 문서가 이미 {count}개 있습니다.", ("D024",)))
    fresh += _people_as_authors(lf(after), lf(before) if before is not None else "")
    fresh.sort(key=lambda p: p.line)
    if not fresh:
        return 0, ""
    out = [f"Le-mot-juste 형식 검사가 이 변경을 막았습니다. 새 위반 {len(fresh)}건, 문서 {rel}"]
    for p in fresh:
        where = f"{p.line}행 " if p.line else ""
        out.append(f"  ✗ {where}[{p.code}] {p.message}")
    if kept > 0:
        out.append(f"이 문서에는 이번 변경과 상관없는 기존 위반 {kept}건이 더 있습니다(막지 않음).")
    out.append("규칙에 맞게 고쳐서 다시 쓰세요.")
    return 2, "\n".join(out)


# Command line


def _iter_documents(paths, root: str):
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for folder, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if _fold(d) not in SKIP_DIRS)
                for name in sorted(files):
                    if name.lower().endswith(MARKDOWN):
                        yield os.path.join(folder, name)
        else:
            yield p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if argv[:1] == ["hook"]:
        strict = os.environ.get("LE_MOT_JUSTE_MODE", "").strip().lower() == "strict"
        try:
            payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 0
        try:
            code, message = run_hook(payload, strict)
        except Exception as exc:  # a checker bug must not block work, only show up
            print(f"Le-mot-juste 형식 검사가 오류로 멈춰 이번 변경은 검사하지 않았습니다: {exc!r}", file=sys.stderr)
            return 1
        if message:
            print(message, file=sys.stderr)
        return code
    if argv[:1] == ["check"] and len(argv) > 1:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path(__file__).resolve().parents[2])
        documents = failed = total = 0
        folders = Counter()
        for path in _iter_documents(argv[1:], root):
            place = locate(path, root)
            if place is None:
                continue
            folders[os.path.dirname(path)] += 1
            try:
                raw = Path(path).read_bytes()
            except OSError:
                continue
            documents += 1
            try:
                problems = check_text(raw.decode("utf-8"), place[1]).problems
            except UnicodeDecodeError:
                problems = [Problem("D013", 0, f"{RULES['D013']} 이 문서는 UTF-8로 읽히지 않습니다.", ("D013",))]
            if problems:
                failed += 1
                total += len(problems)
                print(f"== {place[1]}: 문제 {len(problems)}건")
                for p in problems:
                    where = f"{p.line}행 " if p.line else ""
                    print(f"  ✗ {where}[{p.code}] {p.message}")
        crowded = {f: n for f, n in folders.items() if folder_count(f) > MAX_DOCS_PER_FOLDER}
        for folder in sorted(crowded):
            where = os.path.relpath(folder, root).replace("\\", "/")
            print(f"== {where}: [D024] {RULES['D024']} 이 폴더에는 Markdown 문서가 {folder_count(folder)}개 있습니다.")
        print(f"문서 {documents}편, 문제 있는 문서 {failed}편, 문제 {total}건, 열 편 넘는 폴더 {len(crowded)}곳")
        return 1 if failed or crowded else 0
    print("쓰는 법: le_mot_juste_markdown.py check <문서나 폴더>... | le_mot_juste_markdown.py hook < 훅 입력", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
