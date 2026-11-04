"""System prompt assembly for durumi nodes. Importing this module has no side effects.

A node's system prompt is its `include` list joined in order. Human documents are copied as they are:
the only removals are YAML front matter, the author, date, amount and extra fields of a heading written
in the monorepo's header notation ('## 작성자, 제목, 작성일, 분량, 기타', so a node reads the title only),
and sections whose body is nothing but a brace placeholder such as '{추후 작성 예정}' (plus parent
headings left with no children). No sentence is rewritten.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from config import ConfigError
from node import SLUG_MAX, SLUG_MIN

GENERATED_PREFIX = "generated:"
DOC_SEPARATOR = "\n\n\n"  # two blank lines between documents
# Matched with fullmatch: with re.match, `$` also matches before a trailing newline.
BRAND_ID = re.compile(r"[A-Za-z0-9_-]+")

HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
# Body made only of one or more brace placeholders, e.g. '{추후 작성 예정}'.
PLACEHOLDER_BODY = re.compile(r"^\s*(?:\{[^{}\n]*[^{}\s][^{}\n]*\}\s*)+$")
PATH_SEP = " > "
# Header notation (Le-mot-juste-markdown 6차 A28): '## 작성자, 제목, 작성일, 분량, 기타'. The title may hold
# commas, so the four fixed fields are read from both ends and whatever lies between is the title.
NOTATION = re.compile(
    r"^(#{2,6})[ \t]+(?:Tom|J|Max|MGM|Claude\([^()\n]*\)|\{\}), (.+), "
    r"(?:\d{4}\. \d{1,2}\. \d{1,2}\.|\{\}), (?:\d+|\{\}), [^,\n]+[ \t]*$")


@dataclass
class Assembly:
    text: str
    parts: list = field(default_factory=list)  # dict(path, sha256, omitted_sections, frontmatter_removed)


# Markdown section pruning


@dataclass
class _Section:
    level: int
    title: str
    lines: list  # heading line first, then own body lines (children excluded)
    children: list = field(default_factory=list)
    omitted: bool = False
    body_omitted: bool = False

    @property
    def body(self) -> str:
        return "\n".join(self.lines[1:])


def _parse_sections(lines: list[str]) -> tuple[list[str], list[_Section]]:
    preamble: list[str] = []
    roots: list[_Section] = []
    stack: list[_Section] = []
    fence = None
    for line in lines:
        fence_match = FENCE.match(line)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
            (stack[-1].lines if stack else preamble).append(line)
            continue
        if fence_match:
            fence = fence_match.group(1)
            (stack[-1].lines if stack else preamble).append(line)
            continue
        heading = HEADING.match(line)
        if heading:
            section = _Section(level=len(heading.group(1)), title=heading.group(2), lines=[line])
            while stack and stack[-1].level >= section.level:
                stack.pop()
            (stack[-1].children if stack else roots).append(section)
            stack.append(section)
            continue
        (stack[-1].lines if stack else preamble).append(line)
    return preamble, roots


def _is_placeholder(body: str) -> bool:
    return bool(PLACEHOLDER_BODY.match(body))


def _mark(section: _Section) -> None:
    for child in section.children:
        _mark(child)
    body = section.body
    if not section.children:
        section.omitted = _is_placeholder(body)
        return
    if all(child.omitted for child in section.children):
        if not body.strip() or _is_placeholder(body):
            section.omitted = True
            return
    if _is_placeholder(body):
        section.body_omitted = True


def _emit(section: _Section, trail: tuple, out: list[str], omitted: list[str]) -> None:
    path = PATH_SEP.join(trail + (section.title,))
    if section.omitted:
        omitted.append(path)
        _collect_omitted(section.children, trail + (section.title,), omitted)
        return
    out.append(section.lines[0])
    if section.body_omitted:
        omitted.append(f"{path} (본문)")
        out.extend(["", ""])  # keep the two blank lines above the next heading
    else:
        out.extend(section.lines[1:])
    for child in section.children:
        _emit(child, trail + (section.title,), out, omitted)


def _collect_omitted(children: list, trail: tuple, omitted: list[str]) -> None:
    for child in children:
        omitted.append(PATH_SEP.join(trail + (child.title,)))
        _collect_omitted(child.children, trail + (child.title,), omitted)


def strip_frontmatter(text: str) -> tuple[str, bool]:
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() in ("---", "..."):
                return "\n".join(lines[index + 1:]), True
    return text, False


def prune_placeholders(text: str) -> tuple[str, list[str]]:
    """Drop placeholder-only sections and emptied parent headings. Returns (text, omitted paths)."""
    preamble, roots = _parse_sections(text.split("\n"))
    for section in roots:
        _mark(section)
    out = list(preamble)
    omitted: list[str] = []
    for section in roots:
        _emit(section, (), out, omitted)
    return "\n".join(out), omitted


def normalize_text(raw: bytes, label: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"UTF-8로 읽을 수 없는 문서입니다: {label}") from exc
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_notation(text: str) -> str:
    """Reduce headings written in the header notation to '## 제목'. Fenced code is left as it is."""
    out: list[str] = []
    fence = None
    for line in text.split("\n"):
        fence_match = FENCE.match(line)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        if fence_match:
            fence = fence_match.group(1)
            out.append(line)
            continue
        notation = NOTATION.match(line)
        out.append(f"{notation.group(1)} {notation.group(2)}" if notation else line)
    return "\n".join(out)


def prepare_document(raw: bytes, label: str) -> tuple[str, list[str], bool]:
    text = normalize_text(raw, label)
    text, had_frontmatter = strip_frontmatter(text)
    text = strip_notation(text)
    text, omitted = prune_placeholders(text)
    return text.strip("\n"), omitted, had_frontmatter


# Include resolution


def _brand_doc(root: Path, brand) -> Path:
    if not BRAND_ID.fullmatch(str(brand.id)):
        raise ConfigError(f"브랜드 id에 쓸 수 없는 문자가 있습니다: {brand.id}")
    expected = (root / "brands" / brand.id / "brand.md").resolve()
    actual = Path(brand.doc_path).resolve()
    if actual != expected:
        raise ConfigError(f"브랜드 문서가 루트의 brands/{brand.id}/brand.md가 아닙니다: {brand.doc_path}")
    return actual


def resolve_include(root: Path, item, brand) -> Path:
    """Map an include entry to a file under root. Only context/ and this brand's brand.md are allowed."""
    if not isinstance(item, str) or not item.strip():
        raise ConfigError(f"include 항목이 비어 있거나 문자열이 아닙니다: {item!r}")
    if item == "brand":
        return _brand_doc(root, brand)
    normalized = item.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if normalized.startswith("/") or ":" in normalized or pure.is_absolute():
        raise ConfigError(f"include에 절대 경로는 쓸 수 없습니다: {item}")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ConfigError(f"include 경로에 빈 조각, '.', '..'는 쓸 수 없습니다: {item}")
    if parts[0] == "brands":
        if len(parts) == 3 and parts[1] == brand.id and parts[2] == "brand.md":
            return _brand_doc(root, brand)
        raise ConfigError(f"다른 브랜드 폴더나 브랜드 문서 밖의 파일은 include할 수 없습니다: {item}")
    if parts[0] != "context":
        raise ConfigError(f"include는 context/ 아래 문서와 브랜드 문서만 허용합니다: {item}")
    if not normalized.endswith(".md"):
        raise ConfigError(f"include 문서는 .md 파일이어야 합니다: {item}")
    base = (root / "context").resolve()
    target = (root / Path(*parts)).resolve()
    if not target.is_relative_to(base):
        raise ConfigError(f"include 경로가 context/ 밖을 가리킵니다: {item}")
    return target


def _relative_label(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assemble(root, node_cfg, brand, rules) -> Assembly:
    """Join the node's include list in order into one system prompt text."""
    root = Path(root)
    generators = {"syntax": syntax_text, "palettes": palettes_text}
    chunks: list[str] = []
    parts: list[dict] = []
    for item in node_cfg.include:
        if isinstance(item, str) and item.startswith(GENERATED_PREFIX):
            name = item[len(GENERATED_PREFIX):]
            if name not in generators:
                raise ConfigError(f"알 수 없는 생성 항목입니다: {item}")
            text = generators[name](rules, brand).strip("\n")
            parts.append({"path": item, "sha256": _sha256(text.encode("utf-8")), "omitted_sections": [],
                          "frontmatter_removed": False})
            chunks.append(text)
            continue
        path = resolve_include(root, item, brand)
        label = _relative_label(root, path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(f"include 문서가 없습니다: {label}") from exc
        except OSError as exc:
            raise ConfigError(f"include 문서를 읽지 못했습니다: {label} ({exc})") from exc
        text, omitted, had_frontmatter = prepare_document(raw, label)
        parts.append({"path": label, "sha256": _sha256(raw), "omitted_sections": omitted,
                      "frontmatter_removed": had_frontmatter})
        if text.strip():
            chunks.append(text)
    body = DOC_SEPARATOR.join(chunks)
    return Assembly(text=body + "\n" if body else "", parts=parts)


# Generated guides. Rules: Korean formal register, no em dash, no middle dot, colons only in examples.


def _codepoint_name(char: str, name: str) -> str:
    if "U+" in name:
        return name
    codes = " ".join(f"U+{ord(c):04X}" for c in char)
    return f"{name}({codes})"


def _palette_roles(rules, brand) -> int:
    """Number of palette roles the node may use: fixed brand count, else the maximum it may pick."""
    if brand.palettes:
        return len(brand.palettes)
    return int(rules.form.get("max_palettes", 2))


def _fixed_palettes(rules, brand) -> list:
    missing = [key for key in brand.palettes if key not in rules.palettes]
    if missing:
        raise ConfigError(f"brand.toml의 팔레트가 rules/palettes.toml에 없습니다: {', '.join(missing)}")
    return [rules.palettes[key] for key in brand.palettes]


def _code_span(text: str) -> str:
    fence = "``" if "`" in text else "`"
    pad = " " if fence == "``" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _brand_phrase_lines(brand) -> list[str]:
    """The brand's banned (L030, error) and watched (L031, warning) strings, when brand.toml has any.

    Without them the node would learn a banned string only from a repair request, one costly call later.
    """
    banned = [phrase for phrase in getattr(brand, "banned_phrases", ()) if phrase]
    watch = [phrase for phrase in getattr(brand, "watch_phrases", ()) if phrase]
    if not banned and not watch:
        return []
    lines = ["", "", "## 브랜드 문자열", ""]
    if banned:
        lines.append(f"- 이 브랜드의 원고에 쓰면 오류가 되는 문자열은 {', '.join(_code_span(p) for p in banned)}입니다.")
    if watch:
        lines.append(f"- 이 브랜드에서 보고서에 적히는 문자열은 {', '.join(_code_span(p) for p in watch)}입니다. 되도록 쓰지 않습니다.")
    return lines


def syntax_text(rules, brand) -> str:
    """Manuscript syntax and envelope guide printed from rules and brand.toml (not a human document)."""
    form = rules.form
    fixed = bool(brand.palettes)
    roles = _palette_roles(rules, brand)
    emoji = form.get("emoji", {})
    meaning = emoji.get("meaning", {})
    allowed = emoji.get("allowed", [])
    table_only = emoji.get("table_only", [])
    outside_only = emoji.get("outside_table_only", [])
    punctuation = form.get("punctuation", {})
    banned = punctuation.get("banned", {})
    watch = punctuation.get("watch", {})

    if fixed:
        example = '{"slug": "winter-break-math", "title": "글 제목", "body": "원고 문법으로 쓴 본문"}'
    else:
        first = next(iter(rules.palettes))
        example = f'{{"slug": "winter-break-math", "title": "글 제목", "palettes": ["{first}"], "body": "원고 문법으로 쓴 본문"}}'

    lines = [
        "# 원고 문법과 봉투 형식",
        "",
        "이 안내는 서식 규칙 파일에서 기계가 찍어 낸 것입니다. 글은 아래 원고 문법으로 쓰고, 결과는 JSON 봉투 하나로만 냅니다. HTML은 쓰지 않습니다. HTML은 렌더러가 원고에서 만듭니다.",
        "",
        "",
        "## 봉투",
        "",
        "결과는 아래 모양의 JSON 객체 하나뿐입니다. 봉투 앞뒤에 설명, 인사, 코드 블록 표시를 붙이지 않습니다.",
        "",
        "```json",
        example,
        "```",
        "",
        f"- slug는 영어 소문자와 숫자를 하이픈으로 이은 {SLUG_MIN}자 이상 {SLUG_MAX}자 이하의 이름입니다. 하이픈으로 시작하거나 끝나지 않고, 하이픈을 겹쳐 쓰지 않습니다. 최종 파일 이름에 들어갑니다.",
        "- title은 글 제목 한 줄입니다. 줄바꿈을 넣지 않습니다. 제목은 body에 다시 쓰지 않습니다.",
    ]
    if fixed:
        lines.append("- 이 브랜드는 팔레트가 정해져 있으므로 봉투에 palettes를 넣지 않습니다.")
    else:
        lines.append(f"- palettes는 고른 팔레트 키의 배열입니다. 1개 이상 {roles}개 이하로 적고, 첫 번째 키가 팔레트 1, 두 번째 키가 팔레트 2가 됩니다.")
    lines += [
        # A node once doubled the escape (\\n in the JSON), which left a backslash and n in the body
        # and no line break at all; markup P020 now stops that, and this sentence says it first.
        "- body는 아래 원고 문법으로 쓴 본문 문자열입니다. 봉투를 JSON으로 읽었을 때 body 안에 실제 줄바꿈이 들어 있어야 합니다. JSON 문자열의 줄바꿈 이스케이프는 `\\n` 하나이며, 역슬래시를 겹쳐 써서 역슬래시와 n 두 글자가 본문에 남으면 오류입니다.",
        "- 봉투에는 이 네 가지 밖의 항목을 넣지 않습니다." if not fixed else "- 봉투에는 이 세 가지 밖의 항목을 넣지 않습니다.",
        "",
        "",
        "## 블록",
        "",
        "블록은 빈 줄로 나눕니다. 블록마다 첫 줄의 모양으로 종류가 정해집니다.",
        "",
        "- 문단은 아래 어느 모양에도 해당하지 않는 줄들입니다. 문단 안에서 줄을 바꾸면 그 줄바꿈이 그대로 살아납니다.",
        "- 소제목은 `## 소제목` 모양의 한 줄입니다. 소제목은 한 가지 단계뿐입니다.",
        "- 인용은 줄마다 `> `로 시작하는 줄들입니다.",
        "- 순서 없는 목록은 줄마다 `- `로 시작하는 줄들입니다.",
        "- 순서 있는 목록은 `1. `, `2. `처럼 숫자와 마침표로 시작하는 줄들입니다. 번호는 1부터 차례로 적습니다. 렌더러가 번호를 1부터 다시 매기므로, 다른 번호로 시작하거나 번호를 건너뛰면 오류입니다.",
        "- 날짜처럼 숫자와 마침표로 시작하는 줄(예 `9. 30.부터 개강합니다.`)은 순서 있는 목록으로 읽혀 오류가 됩니다. 이런 문장은 줄 첫머리를 달리 씁니다.",
        "- 목록 안에 들여쓴 줄(중첩 목록)은 쓸 수 없습니다.",
        "- 체크리스트는 모든 줄이 `☑️ `로 시작하는 블록입니다.",
        "- 표는 `|`로 시작하는 줄들입니다. 첫 줄은 머리글, 둘째 줄은 `|---|---|` 모양의 구분선, 나머지는 본문 행입니다. 모든 행의 칸 수는 머리글과 같아야 합니다.",
        "- 이미지 자리는 한 줄 전체가 `[이미지: 설명]`인 블록입니다. 설명 끝에 '이미지'를 붙이지 않습니다. 렌더러가 붙입니다.",
        "- 링크 자리는 한 줄 전체가 `[링크: 대상]`인 블록입니다. 문단 안에 `[링크: 대상]`을 쓰면 문장 속 링크 자리가 됩니다.",
    ]
    if form.get("title_first_block", "paragraph") == "paragraph":
        lines.append("- 제목 바로 뒤, 곧 body의 첫 블록은 도입 문단이어야 합니다. 소제목이나 목록, 표, 이미지 자리로 시작하면 오류입니다.")
    lines += [
        "",
        "",
        "## 문장 속 표시",
        "",
        "- `**텍스트**`는 굵게입니다.",
        "- `{c1:텍스트}`는 팔레트 1의 글자 색입니다. 글자 색은 언제나 굵게와 함께 나오므로 `**`로 다시 감싸지 않습니다.",
        "- `{h1:텍스트}`는 팔레트 1의 형광펜입니다. 굵게 없이 검은 글자에 배경색만 입힙니다.",
    ]
    if roles >= 2:
        lines.append("- `{c2:텍스트}`와 `{h2:텍스트}`는 팔레트 2의 글자 색과 형광펜입니다. 팔레트가 둘일 때만 씁니다.")
    else:
        lines.append("- 이 브랜드는 팔레트가 하나이므로 `{c2:텍스트}`와 `{h2:텍스트}`는 쓰지 않습니다.")
    lines += [
        "- 표시끼리 겹치거나 한 표시 안에 다른 표시를 넣지 않습니다. `**`, `{`, `}`는 반드시 짝을 맞춥니다. 표시가 아닌 곳에는 중괄호를 쓰지 않습니다.",
        "- HTML 태그와 마크다운 표시는 쓰지 않습니다. 마크다운 링크 `[텍스트](주소)`, 기울임 `*텍스트*`, 취소선 `~~`, 코드 표시(백틱), 구분선 `---`, `* `로 시작하는 목록, 띄어 쓰지 않은 제목 표시 `##소제목`이 남아 있으면 오류입니다.",
        "",
        "",
        "## 문장 부호",
        "",
    ]
    if banned:
        names = ", ".join(_codepoint_name(char, name) for char, name in banned.items())
        lines.append(f"- 쓰면 오류가 되는 문자는 {names}입니다. 문장을 이런 부호로 잇지 말고 문장으로 풀어 씁니다.")
    if watch:
        names = ", ".join(_codepoint_name(char, name) for char, name in watch.items())
        lines.append(f"- 보고서에 적히는 문자는 {names}입니다. 되도록 쓰지 않습니다.")
    lines += [
        "- 콜론은 `10:30`이나 `2:1`처럼 숫자와 숫자 사이에만 씁니다. 그 밖의 콜론은 오류입니다. 설명을 콜론으로 붙이지 말고 문장으로 풀어 씁니다.",
        # The linter (L003) judges the text inside slots and marks like any other text; only a
        # scheme's :// inside a link target is let through (lint.URL_SCHEME_TAIL).
        "- 위 표시 문법에 들어 있는 콜론(`{c1:텍스트}`, `[이미지: 설명]`, `[링크: 대상]`)은 문법의 일부이므로 괜찮습니다. 설명, 대상, 텍스트 자리에 적는 글에는 위 규칙이 그대로 적용되므로 숫자 사이가 아닌 콜론은 오류입니다. 링크 대상에 주소를 적을 때는 `https://`처럼 주소 앞머리에 붙는 콜론만 괜찮습니다.",
    ]
    lines += _brand_phrase_lines(brand)
    # rules.form [manuscript] meta_markers (L050) are deliberately not printed: whether the node should
    # see them is Tom's decision (review of 2026-09-30). Until then only a repair request names one.
    lines += [
        "",
        "",
        "## 이모지",
        "",
        f"쓸 수 있는 이모지는 아래 {len(allowed)}가지뿐입니다.",
        "",
    ]
    for item in allowed:
        lines.append(f"- {item} {meaning.get(item, '')}".rstrip())
    table_list = ", ".join(table_only)
    outside_list = ", ".join(outside_only)
    lines += [
        "",
        "- 이모지는 첫머리에만 씁니다. 첫머리란 블록의 첫머리, 목록 항목의 첫머리, 표 칸의 첫머리, 소제목의 첫머리, 체크리스트 줄의 첫머리입니다. 문장 중간이나 끝에는 쓰지 않습니다.",
    ]
    if table_list:
        lines.append(f"- {table_list}는 표 안에서만 씁니다.")
    if outside_list:
        lines.append(f"- {outside_list}는 표 밖에서만 씁니다.")
    lines.append("- 목록에 없는 이모지는 오류입니다. 그림 문자로 나오는 기호(예 ⭐, ❌, ❗, ✨)와 이모지 선택자(U+FE0F)를 붙인 기호도 이모지로 셉니다. ★, ✓, ♥처럼 글자로 쓰는 기호는 이모지 선택자 없이 쓰면 이모지로 세지 않습니다.")
    return "\n".join(lines) + "\n"


def palettes_text(rules, brand) -> str:
    """Palette guide: the fixed palette roles, or the selectable keys and the maximum count."""
    lines = ["# 팔레트", ""]
    if brand.palettes:
        palettes = _fixed_palettes(rules, brand)
        lines.append("이 브랜드는 팔레트가 정해져 있습니다. 봉투에 palettes를 넣지 않습니다.")
        lines.append("")
        for index, palette in enumerate(palettes, start=1):
            particle = "는" if index == 2 else "은"
            lines.append(f"- 팔레트 {index}{particle} {palette.label}({palette.key})입니다. `{{c{index}:텍스트}}`는 이 팔레트의 글자 색, `{{h{index}:텍스트}}`는 이 팔레트의 형광펜입니다.")
        if len(palettes) == 1:
            lines.append("- 팔레트가 하나이므로 `{c2:텍스트}`와 `{h2:텍스트}`는 쓰지 않습니다.")
        return "\n".join(lines) + "\n"
    max_palettes = int(rules.form.get("max_palettes", 2))
    lines.append(f"글에 어울리는 팔레트를 1개 이상 {max_palettes}개 이하로 고릅니다. 고를 수 있는 팔레트는 아래와 같습니다.")
    lines.append("")
    for key, palette in rules.palettes.items():
        lines.append(f"- {key} ({palette.label})")
    lines += [
        "",
        f"봉투의 palettes에 고른 키를 적습니다. 첫 번째 키가 팔레트 1로 `{{c1:텍스트}}`와 `{{h1:텍스트}}`에, 두 번째 키가 팔레트 2로 `{{c2:텍스트}}`와 `{{h2:텍스트}}`에 쓰입니다. 팔레트를 하나만 고르면 c2와 h2는 쓰지 않습니다.",
    ]
    return "\n".join(lines) + "\n"
