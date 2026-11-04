"""Biik standard document format checker (successor of the scratchpad doclint prototype).

Checks the machine-decidable items of .claude/harness/docs/how-to-write-CLAUDE.md:
- exactly one H1, whose title is the document path relative to the repository root
  (SKILL.md also passes when the H1 equals the front matter `name`)
- body text only under leaf headers
- exactly two blank lines above every header except the H1
- no blank line between a leaf header and its body
- every leaf body is exactly TARGET characters, counted without whitespace
- placeholders use braces, not square brackets
- LF line endings and a single trailing newline

The checker never edits sentences. `fix_text` / `--fix` only repairs whitespace layout
(blank lines around headers, line endings, trailing newline) and asserts that the
non-whitespace character sequence is unchanged.

Importing this module has no side effects.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from issues import ERROR, INFO, Issue, excerpt

# Required body length of a leaf section.
TARGET = 200

# Whitespace definition for counting: Python's `\s` in a str pattern, i.e. every Unicode
# whitespace character (space, tab, newline, U+00A0 no-break space, U+3000 ideographic
# space, ...). A body's length is len(re.sub(r"\s", "", body)).
WHITESPACE = re.compile(r"\s")

HEADER = re.compile(r"^(#{1,6}) (.+)$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# Square-bracket placeholder such as "[추후 작성 예정]"; the standard asks for braces.
BRACKET_PLACEHOLDER = re.compile(r"\[[^\]]*(추후|작성 예정)[^\]]*\]")
# A body that is nothing but one brace placeholder, e.g. "{추후 작성 예정}".
BRACE_PLACEHOLDER = re.compile(r"^\{[^{}]+\}$")
# Import header, e.g. "## 비익 @BIIK.md": its content lives in the imported file.
IMPORT_HEADER = re.compile(r"(^|\s)@\S+")
# Placeholder inside a header title, e.g. "## 양(절대량, {...})".
HEADER_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
# Dynamic context injection line of a skill: "!" followed by a backtick command.
DYNAMIC_INJECTION = re.compile(r"^!`[^`\n]+`$")
FRONT_MATTER_NAME = re.compile(r"^name:\s*(.+?)\s*$")

SKIP_DIRS = {".git", "runs", "__pycache__", "node_modules"}


def count_chars(text: str) -> int:
    """Characters of `text` excluding whitespace as defined by WHITESPACE."""
    return len(WHITESPACE.sub("", text))


@dataclass
class DocReport:
    path: Path
    problems: list = field(default_factory=list)  # list[Issue], severity error
    notes: list = field(default_factory=list)  # list[Issue], severity info

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "path": self.path.as_posix(),
            "ok": self.ok,
            "problems": [p.to_dict() for p in self.problems],
            "notes": [n.to_dict() for n in self.notes],
        }


def _front_matter(lines: list[str]) -> tuple[int, dict]:
    """Return (number of lines taken by the front matter block, parsed simple keys)."""
    if not lines or lines[0] != "---":
        return 0, {}
    for i in range(1, len(lines)):
        if lines[i] == "---":
            keys = {}
            for line in lines[1:i]:
                m = FRONT_MATTER_NAME.match(line)
                if m:
                    value = m.group(1)
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    keys["name"] = value
            return i + 1, keys
    return 0, {}


def _headers(lines: list[str], start: int) -> list[tuple[int, int, str]]:
    """(line index, level, title) of every header outside fenced code blocks."""
    found = []
    fence = None
    for i in range(start, len(lines)):
        line = lines[i]
        m = FENCE.match(line)
        if fence is not None:
            if _closes(m, line, fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        if fence is None and (h := HEADER.match(line)):
            found.append((i, len(h.group(1)), h.group(2).strip()))
    return found


def _is_code_only(body: str) -> bool:
    """True when the body is a single dynamic injection line or only fenced code blocks."""
    if DYNAMIC_INJECTION.match(body):
        return True
    lines = body.split("\n")
    if not FENCE.match(lines[0]):
        return False
    fence = None
    for line in lines:
        m = FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
            elif line.strip():
                return False  # prose outside a code block
        elif _closes(m, line, fence):
            fence = None
    return fence is None


def _closes(match, line: str, fence: str) -> bool:
    """True when `line` closes a code block opened with `fence` (same char, not shorter, no info)."""
    if not match:
        return False
    marker = match.group(1)
    return marker[0] == fence[0] and len(marker) >= len(fence) and line.strip() == marker


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def check_text(text: str, rel: str, raw_has_cr: bool = False) -> tuple[list, list]:
    """Check decoded text of a document whose repository path is `rel`."""
    problems: list[Issue] = []
    notes: list[Issue] = []

    def problem(code, message, where="", ex=""):
        problems.append(Issue(code, ERROR, message, where, excerpt(ex)))

    def note(code, message, where="", ex=""):
        notes.append(Issue(code, INFO, message, where, excerpt(ex)))

    if raw_has_cr or "\r" in text:
        problem("D001", "CRLF 줄 끝이 있습니다. LF만 허용합니다.")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        problem("D002", "파일 끝 개행이 없습니다.")
    elif text.endswith("\n\n"):
        problem("D003", "파일 끝에 빈 줄이 있습니다. 끝 개행은 하나만 둡니다.")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    while lines and lines[-1].strip() == "" and len(lines) > 1:
        lines = lines[:-1]

    fm_len, fm = _front_matter(lines)
    if fm_len:
        note("D101", "프런트매터는 본문 계산에서 뺐습니다.", f"1~{fm_len}행")

    headers = _headers(lines, fm_len)
    first_header = headers[0][0] if headers else len(lines)
    stray = [ln for ln in lines[fm_len:first_header] if ln.strip()]
    if stray:
        problem("D004", "첫 헤더 앞에 본문이 있습니다.", f"{fm_len + 1}행", stray[0])

    h1 = [h for h in headers if h[1] == 1]
    is_skill = rel.rsplit("/", 1)[-1] == "SKILL.md"
    if len(h1) != 1:
        problem("D005", f"H1이 {len(h1)}개입니다. 하나만 허용합니다.")
    else:
        title = h1[0][2]
        where = f"{h1[0][0] + 1}행"
        if title == rel:
            pass
        elif is_skill and fm.get("name") and title == fm["name"]:
            note("D102", "SKILL.md의 H1이 프런트매터 name과 같아 통과했습니다.", where, title)
        elif is_skill and fm.get("name"):
            problem("D006", f"H1이 경로나 프런트매터 name과 다릅니다. H1 '{title}', 경로 '{rel}', name '{fm['name']}'입니다.", where, title)
        else:
            problem("D006", f"H1이 경로와 다릅니다. H1 '{title}', 경로 '{rel}'입니다.", where, title)

    for k, (i, level, title) in enumerate(headers):
        where = f"{i + 1}행"
        if level > 1:
            above = lines[max(fm_len, i - 3):i]
            if not (len(above) == 3 and above[1] == "" and above[2] == "" and above[0].strip() != ""):
                problem("D007", f"'{title}' 위 빈 줄이 정확히 두 줄이 아닙니다.", where, title)
        end = headers[k + 1][0] if k + 1 < len(headers) else len(lines)
        body_lines = lines[i + 1:end]
        body = "\n".join(body_lines).strip()
        deeper = k + 1 < len(headers) and headers[k + 1][1] > level
        if deeper:
            if body:
                problem("D008", f"'{title}'은 하위 헤더가 있는데 본문이 있습니다. 본문은 최하위 헤더에만 씁니다.", where, body)
            continue
        if level == 1:
            continue
        if not body:
            if IMPORT_HEADER.search(title):
                note("D103", f"'{title}'은 가져오기 헤더라 본문이 없습니다.", where, title)
            elif HEADER_PLACEHOLDER.search(title):
                note("D104", f"'{title}'은 헤더 플레이스홀더라 본문이 없습니다.", where, title)
            else:
                problem("D009", f"'{title}' 본문이 비어 있습니다.", where, title)
            continue
        if body_lines and body_lines[0].strip() == "":
            problem("D010", f"'{title}'와 본문 사이에 빈 줄이 있습니다.", where, title)
        if _is_code_only(body):
            note("D105", f"'{title}'은 코드 절이라 {TARGET}자 대상에서 뺐습니다.", where, title)
            continue
        if BRACKET_PLACEHOLDER.search(body):
            problem("D011", f"'{title}' 플레이스홀더가 대괄호입니다. 중괄호로 씁니다.", where, BRACKET_PLACEHOLDER.search(body).group(0))
        if BRACE_PLACEHOLDER.match(body):
            note("D106", f"'{title}'은 플레이스홀더만 있습니다({body}).", where, title)
            continue
        n = count_chars(body)
        if n != TARGET:
            problem("D012", f"'{title}' 본문이 {n}자입니다. 공백 제외 {TARGET}자여야 합니다({n - TARGET:+d}).", where, title)
        else:
            note("D107", f"'{title}' {n}자입니다.", where, title)
    return problems, notes


def check_doc(repo_root: Path, path: Path) -> DocReport:
    """Check one Markdown document. `repo_root` decides the path expected in the H1."""
    path = Path(path)
    report = DocReport(path=path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        report.problems.append(Issue("D000", ERROR, f"파일을 읽을 수 없습니다: {exc}", path.as_posix()))
        return report
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        report.problems.append(Issue("D000", ERROR, "UTF-8로 읽을 수 없습니다.", path.as_posix()))
        return report
    if text.startswith("﻿"):
        report.problems.append(Issue("D013", ERROR, "BOM이 있습니다. BOM 없는 UTF-8만 허용합니다."))
        text = text[1:]
    problems, notes = check_text(text, _relative(repo_root, path), b"\r" in raw)
    report.problems.extend(problems)
    report.notes.extend(notes)
    return report


def iter_markdown(paths) -> list[Path]:
    """Expand files and directories into a sorted, de-duplicated list of .md files."""
    out: list[Path] = []
    seen = set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found = [f for f in p.rglob("*.md") if not SKIP_DIRS.intersection(f.relative_to(p).parts[:-1])]
            candidates = sorted(found)
        else:
            candidates = [p]
        for f in candidates:
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def check_paths(repo_root: Path, paths) -> list[DocReport]:
    """Check every Markdown file in `paths` (files or directories)."""
    return [check_doc(repo_root, f) for f in iter_markdown(paths)]


# Whitespace-only repair


def fix_text(text: str) -> str:
    """Repair whitespace layout only. Raises AssertionError if any visible character would change.

    - CRLF and lone CR become LF, a leading BOM is removed
    - exactly two blank lines above every header except H1 (headers inside code fences untouched)
    - no blank lines between a header and what follows it
    - exactly one trailing newline
    Front matter and fenced code blocks are copied as they are.
    """
    src = text[1:] if text.startswith("﻿") else text
    src = src.replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    fm_len, _ = _front_matter(lines)
    head, rest = lines[:fm_len], lines[fm_len:]
    out: list[str] = []
    fence = None
    after_header = False
    for line in rest:
        m = FENCE.match(line)
        if fence is not None:
            out.append(line)
            if _closes(m, line, fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            after_header = False
            continue
        h = HEADER.match(line)
        if h:
            if len(h.group(1)) > 1:
                while out and out[-1].strip() == "":
                    out.pop()
                if out:
                    out.extend(["", ""])
            out.append(line)
            after_header = True
            continue
        if after_header and line.strip() == "":
            continue
        after_header = False
        out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    result = "\n".join(head + out) + "\n"
    if WHITESPACE.sub("", result) != WHITESPACE.sub("", src):
        raise AssertionError("fix_text would change visible characters")
    return result


def fix_doc(path: Path) -> bool:
    """Apply fix_text to a file in place. Returns True when the file changed."""
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    fixed = fix_text(text)
    data = fixed.encode("utf-8")
    if data == raw:
        return False
    path.write_bytes(data)
    return True


def format_report(report: DocReport) -> str:
    head = "통과" if report.ok else f"문제 {len(report.problems)}건"
    lines = [f"== {report.path.as_posix()}: {head}"]
    for p in report.problems:
        where = f"{p.where} " if p.where else ""
        lines.append(f"  ✗ [{p.code}] {where}{p.message}")
    for n in report.notes:
        where = f"{n.where} " if n.where else ""
        lines.append(f"  ✓ [{n.code}] {where}{n.message}")
    return "\n".join(lines)


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="비익 표준 양식 검사")
    parser.add_argument("paths", nargs="+", help="검사할 문서 또는 폴더")
    parser.add_argument("--root", help="저장소 루트(H1 경로 기준). 기본값은 현재 폴더에서 위로 찾은 .git 위치")
    parser.add_argument("--fix", action="store_true", help="공백 규칙만 고칩니다. 글자는 바꾸지 않습니다.")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else _find_repo_root(Path.cwd().resolve())
    files = iter_markdown(args.paths)
    if args.fix:
        for f in files:
            if fix_doc(f):
                print(f"공백을 고쳤습니다: {f.as_posix()}")
    failed = 0
    for f in files:
        report = check_doc(root, f)
        print(format_report(report))
        failed += not report.ok
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
