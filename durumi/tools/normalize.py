"""Deterministic repairs of how a node encoded its envelope, never of what it wrote.

A node sometimes escapes the JSON string twice, so the body arrives with the two characters
backslash and n where line breaks should be. A blog body always has paragraphs, so a body with no
line break inside it (one at either end does not count) but with literal backslash-n pairs, a
paragraph break (two in a row) among them, can only be this encoding slip. Undoing it here saves a repair round (a max effort node call takes
several minutes), and the original stays in the node's raw output file. It runs before
check_envelope, which still only judges: a body that mixes real line breaks with literal ones is
left alone for P020 to report, and so is one that would decode into a lone surrogate.

Importing this module has no side effects.
"""
from __future__ import annotations

import re

LITERAL_NEWLINE = "\\n"
MIN_LITERAL_NEWLINES = 2
# JSON's escapes, one level, and \' which a node escaping by hand writes too. \b and \f are left
# alone: in a blog body they are far likelier to be a word after a stray backslash than a backspace.
ESCAPE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/nrt\'])')
SIMPLE = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "r": "\r", "t": "\t", "'": "'"}
REPLACEMENT = "�"  # what a lone surrogate half becomes; markup's P021 then asks for a repair


def _unescape(text: str) -> str | None:
    """text with one level of JSON escaping undone, any other backslash left as it is.

    None when an escaped surrogate has no partner: that text cannot be written as UTF-8, and a
    repair round is cheaper than a run that stops while saving it.
    """
    decoded = ESCAPE.sub(lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u" else SIMPLE[m.group(1)], text)
    try:
        decoded = decoded.encode("utf-16", "surrogatepass").decode("utf-16")  # joins escaped surrogate pairs
    except UnicodeDecodeError:
        return None
    return decoded.replace("\r\n", "\n")


def normalize_envelope(envelope):
    """Return (envelope, notes). The given envelope is not modified.

    notes are Korean sentences for the report, one per repair. When nothing is repaired the same
    object comes back with an empty list. A title escaped the same way is undone with the body.
    """
    if not isinstance(envelope, dict):
        return envelope, []
    envelope, notes = _mend_surrogates(envelope)
    body = envelope.get("body")
    if not isinstance(body, str) or "\n" in body.strip():
        return envelope, notes
    count = body.count(LITERAL_NEWLINE)
    # A post escaped twice has literal paragraph breaks too; two lone ones (C:\new\notes) are just text.
    if count < MIN_LITERAL_NEWLINES or LITERAL_NEWLINE * 2 not in body:
        return envelope, notes
    fixed = _unescape(body)
    # Escaped three times, one level still leaves no line break: then nothing is undone, P020 asks.
    if fixed is None or fixed == body or "\n" not in fixed.strip():
        return envelope, notes
    repaired = dict(envelope, body=fixed)
    note = (f"본문에 줄바꿈이 하나도 없고 역슬래시와 n 두 글자가 {count}번 있어서 "
            "봉투를 두 번 이스케이프한 것으로 보고 줄바꿈으로 풀었습니다.")
    title = envelope.get("title")
    if isinstance(title, str) and "\\" in title:
        plain = _unescape(title)
        if plain is not None and plain != title:
            repaired["title"] = plain
            note += " 제목도 같은 방식으로 풀었습니다."
    return repaired, notes + [note]


def _mend_surrogates(envelope: dict) -> tuple[dict, list]:
    """Replace lone surrogate halves in the title and body, which no file can hold in UTF-8.

    A node that escapes an emoji by one half (a single \\ud83d) makes the run stop at its first
    write. As U+FFFD the text can be saved, and markup's P021 sends it back for a repair.
    """
    mended, fields = dict(envelope), []
    for key in ("title", "body"):
        value = envelope.get(key)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                mended[key] = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
                fields.append({"title": "제목", "body": "본문"}[key])
    if not fields:
        return envelope, []
    return mended, [f"{'과 '.join(fields)}에 짝 없는 서로게이트 글자가 있어 U+FFFD로 바꿨습니다. 수리 요청으로 다시 쓰게 합니다."]
