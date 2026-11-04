"""Sentences a person should check against the brand documents. Report only.

The machine never judges whether a sentence is true. It picks, by fixed rules, the sentences
where a wrong fact costs the most:

- sentences in the final text that name the brand (the same matching rules as resolve.py)
- sentences in the final text that carry a number
- sentences whose hedge the revise node dropped (the claim got stronger with no new evidence)
- sentences the revise node wrote with no counterpart in the draft

It also lists quotes and image slots that the revise node removed or added, verbatim, so the
reader does not have to diff draft.json and final.json by hand.

Importing this module has no side effects.
"""
from __future__ import annotations

import difflib

from document import Checklist, ImageSlot, ListBlock, Paragraph, Quote, Table, inline_text
from measure import NUMBER, WHITESPACE, split_sentences
from resolve import _matches

# Words that soften a claim, in two tiers. A possibility hedge says it may be so; a frequency
# hedge says it is often so. A claim got stronger when the revise node dropped every hedge, or
# turned a possibility into something else ("젖어 있을 수 있어요" into "젖어 있는 경우가 많아요").
# Swapping one frequency word for another ("대개" for "대부분") is not flagged. The lists are
# deliberately short and literal; they only decide which pairs are shown to a person.
POSSIBILITY = ("수 있", "수도 있", "아마", "것 같", "듯합", "듯해", "지도 모릅", "일지도")
FREQUENCY = (
    "보통", "대부분", "대개", "대체로", "주로", "흔히", "종종", "가끔", "때로", "경우가", "경우도",
    "편이", "편입", "때가 많", "때도 많", "때가 있", "때도 있", "분도 많", "분이 많",
)

# Two sentences are the same sentence rewritten when their similarity reaches this value.
PAIR_RATIO = 0.5

# Pieces shorter than this (characters without whitespace) carry no checkable fact, such as
# "됩니다." split off a longer thought.
MIN_CHARS = 8


def _long_enough(sentence: str) -> bool:
    return len(WHITESPACE.sub("", sentence)) >= MIN_CHARS


def sentences(doc) -> list[str]:
    """Readable sentences of a document in order: paragraphs, quotes, list and checklist items,
    and table rows (cells joined with ' | '). Pieces shorter than MIN_CHARS are left out."""
    out = []
    for block in doc.blocks:
        if isinstance(block, Paragraph):
            out += split_sentences(inline_text(block.inlines))
        elif isinstance(block, Quote):
            for line in block.lines:
                out += split_sentences(inline_text(line))
        elif isinstance(block, (ListBlock, Checklist)):
            for item in block.items:
                out += split_sentences(inline_text(item))
        elif isinstance(block, Table):
            for row in block.rows:
                out.append(" | ".join(inline_text(cell) for cell in row))
    return [s for s in out if _long_enough(s)]


def brand_sentences(doc, brand) -> list[str]:
    return [s for s in sentences(doc) if _matches(s, [brand])]


def number_sentences(doc) -> list[str]:
    return [s for s in sentences(doc) if NUMBER.search(s)]


def _has(sentence: str, words) -> bool:
    return any(word in sentence for word in words)


def _stronger(before: str, after: str) -> bool:
    """True when the rewritten sentence lost its hedge (see POSSIBILITY and FREQUENCY)."""
    if before == after:
        return False
    had_possibility, has_possibility = _has(before, POSSIBILITY), _has(after, POSSIBILITY)
    had_any = had_possibility or _has(before, FREQUENCY)
    has_any = has_possibility or _has(after, FREQUENCY)
    return (had_any and not has_any) or (had_possibility and not has_possibility)


def _dropped(before: str, after: str) -> list[str]:
    return [w for w in POSSIBILITY + FREQUENCY if w in before and w not in after]


def _best(pool: list[str], item: str) -> tuple[str | None, float]:
    best, best_ratio = None, 0.0
    for candidate in pool:
        ratio = difflib.SequenceMatcher(None, candidate, item, autojunk=False).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best, best_ratio


def _pairs(before: list[str], after: list[str]):
    """Yield (draft item or None, final item) for every final item.

    Equal runs pair one to one. Inside a replaced run each final item takes the most similar
    draft item of that run when the similarity reaches PAIR_RATIO.
    """
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                yield before[i1 + k], after[j1 + k]
            continue
        for item in after[j1:j2]:
            best, ratio = _best(before[i1:i2], item)
            yield (best if ratio >= PAIR_RATIO else None), item


def revise_changes(draft_doc, final_doc) -> dict:
    """Claims the revise node made stronger, and sentences it wrote without a draft counterpart."""
    stronger, new = [], []
    for before, after in _pairs(sentences(draft_doc), sentences(final_doc)):
        if before is None:
            new.append(after)
        elif _stronger(before, after):
            stronger.append({"draft": before, "final": after, "dropped": _dropped(before, after)})
    return {"stronger": stronger, "new": new}


def _quotes(doc) -> list[str]:
    return [" / ".join(inline_text(line) for line in b.lines) for b in doc.blocks if isinstance(b, Quote)]


def _images(doc) -> list[str]:
    return [b.desc for b in doc.blocks if isinstance(b, ImageSlot)]


def _unmatched(a: list[str], b: list[str]) -> list[str]:
    """Items of a with no item in b at PAIR_RATIO or above (a reworded item is not unmatched)."""
    return [item for item in a if _best(b, item)[1] < PAIR_RATIO]


def factcheck(draft_doc, final_doc, brand) -> dict:
    """Everything the report's fact check section shows. final_doc may be None (no final text)."""
    if final_doc is None:
        return {}
    out = {
        "brand_sentences": brand_sentences(final_doc, brand) if brand is not None else [],
        "number_sentences": number_sentences(final_doc),
    }
    if draft_doc is not None:
        out.update(revise_changes(draft_doc, final_doc))
        quotes_a, quotes_b = _quotes(draft_doc), _quotes(final_doc)
        images_a, images_b = _images(draft_doc), _images(final_doc)
        out["quotes_removed"], out["quotes_added"] = _unmatched(quotes_a, quotes_b), _unmatched(quotes_b, quotes_a)
        out["images_removed"], out["images_added"] = _unmatched(images_a, images_b), _unmatched(images_b, images_a)
    return out


def markdown_lines(check: dict) -> list[str]:
    """The report's fact check section. An empty dict means there was no final text."""
    lines = ["## 사실 확인", "",
             "기계는 문장이 사실인지 판정하지 않습니다. 틀리면 손해가 큰 문장을 정해진 규칙으로 골라 두었으니 "
             "브랜드 문서와 대조해 주세요."]
    if not check:
        return lines + ["", "최종 원고가 없습니다."]

    def listed(title, items, fmt=lambda s: f"- {s}"):
        out = ["", f"### {title}", ""]
        return out + ([fmt(item) for item in items] if items else ["없습니다."])

    lines += listed("브랜드가 나오는 문장", check["brand_sentences"])
    lines += listed("숫자가 나오는 문장", check["number_sentences"])
    if "stronger" in check:
        def pair(p):
            dropped = ", ".join(f"'{w}'" for w in p["dropped"])
            return f"- 초안 {p['draft']}\n  최종 {p['final']} (빠진 말 {dropped})"
        lines += listed("퇴고에서 단정이 세진 문장", check["stronger"], pair)
        lines += listed("퇴고가 새로 쓴 문장", check["new"])
        blocks = ([f"- 사라진 인용 {q}" for q in check["quotes_removed"]]
                  + [f"- 새로 생긴 인용 {q}" for q in check["quotes_added"]]
                  + [f"- 사라진 이미지 자리 {d}" for d in check["images_removed"]]
                  + [f"- 새로 생긴 이미지 자리 {d}" for d in check["images_added"]])
        lines += ["", "### 퇴고에서 빠지거나 새로 생긴 인용과 이미지 자리", ""]
        lines += blocks if blocks else ["없습니다."]
    return lines


def summary_line(check: dict) -> str | None:
    """One line for the run summary, or None when there was no final text."""
    if not check:
        return None
    parts = [f"브랜드 {len(check['brand_sentences'])}", f"숫자 {len(check['number_sentences'])}"]
    if "stronger" in check:
        parts += [f"단정이 세진 문장 {len(check['stronger'])}", f"퇴고가 새로 쓴 문장 {len(check['new'])}"]
    return "사실 확인할 문장 " + ", ".join(parts) + " (보고서의 사실 확인 절)"
