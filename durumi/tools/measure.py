"""Report-only measurements of a durumi document. Nothing here blocks or raises on content.

Importing this module has no side effects.
"""
from __future__ import annotations

import difflib
import re

from document import (
    Bold,
    Checklist,
    Color,
    Highlight,
    ImageSlot,
    LinkBlock,
    LinkSlot,
    ListBlock,
    Paragraph,
    Quote,
    Subheading,
    Table,
    block_texts,
    inline_text,
)

# Same whitespace definition as doccheck: Python `\s` (includes U+00A0 and U+3000).
WHITESPACE = re.compile(r"\s")

# Sentence splitting rule (deliberately simple, documented for the report reader):
# 1. A line break always ends a sentence.
# 2. Inside a line, a run of '.', '?' or '!' followed by whitespace or the end of the line
#    ends a sentence. Closing quotes or brackets directly after the run stay with it.
#    "3.5" or "v1.2" is not split because no whitespace follows the dot.
# 3. A number and a dot at the start of a line ("1. 사진부터 찍기") is a step number, not a
#    sentence; it stays with the words after it.
# 4. Pieces without any non-whitespace character are dropped.
SENTENCE_END = re.compile(r"[.?!]+[\"'”’)\]」』]*(?=\s|$)")
STEP_NUMBER = re.compile(r"\d+\.")

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

VS16 = "️"


def chars(text: str) -> int:
    """Characters excluding whitespace."""
    return len(WHITESPACE.sub("", text))


def split_sentences(text: str) -> list[str]:
    """Split `text` into sentences by the rule above."""
    out = []
    for line in text.split("\n"):
        start = 0
        for m in SENTENCE_END.finditer(line):
            piece = line[start:m.end()].strip()
            if start == 0 and STEP_NUMBER.fullmatch(piece):
                continue
            if piece:
                out.append(piece)
            start = m.end()
        tail = line[start:].strip()
        if tail:
            out.append(tail)
    return out


def _inlines_of(block) -> list:
    """Every inline sequence inside a block."""
    if isinstance(block, (Paragraph, Subheading)):
        return [block.inlines]
    if isinstance(block, Quote):
        return list(block.lines)
    if isinstance(block, (ListBlock, Checklist)):
        return list(block.items)
    if isinstance(block, Table):
        seqs = list(block.header)
        for row in block.rows:
            seqs.extend(row)
        return seqs
    return []


def _prose_texts(block) -> list[str]:
    """Text units that are read as sentences: paragraphs, quotes, list and checklist items."""
    if isinstance(block, (Paragraph, Quote, ListBlock, Checklist)):
        return block_texts(block)
    return []


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _avg(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 2) if values else 0.0


def _emoji_counts(text: str, allowed) -> dict:
    """Occurrences of each allowed emoji, comparing with VS16 removed."""
    bare_text = text.replace(VS16, "")
    counts = {}
    for emoji in allowed:
        bare = emoji.replace(VS16, "")
        n = bare_text.count(bare)
        if n:
            counts[emoji] = n
    return counts


def measure(doc, rules) -> dict:
    """Numbers describing `doc`. Report only: no thresholds, no errors."""
    blocks = list(doc.blocks)
    body_text = "\n\n".join("\n".join(block_texts(b)) for b in blocks)
    body_chars = chars(body_text)

    paragraphs = [b for b in blocks if isinstance(b, Paragraph)]
    para_sentences = [len(split_sentences(inline_text(p.inlines))) for p in paragraphs]
    subheads = [chars(inline_text(b.inlines)) for b in blocks if isinstance(b, Subheading)]

    image_positions = [i for i, b in enumerate(blocks) if isinstance(b, ImageSlot)]
    gaps = [b - a - 1 for a, b in zip(image_positions, image_positions[1:])]
    edges = [len(blocks)] if not image_positions else [image_positions[0], len(blocks) - image_positions[-1] - 1]

    bold = color = highlight = inline_links = 0
    bold_chars = color_chars = highlight_chars = 0
    for block in blocks:
        for seq in _inlines_of(block):
            for node in seq:
                if isinstance(node, Bold):
                    bold += 1
                    bold_chars += chars(node.text)
                elif isinstance(node, Color):
                    color += 1
                    color_chars += chars(node.text)
                elif isinstance(node, Highlight):
                    highlight += 1
                    highlight_chars += chars(node.text)
                elif isinstance(node, LinkSlot):
                    inline_links += 1
    link_blocks = sum(isinstance(b, LinkBlock) for b in blocks)

    sentences = [s for b in blocks for t in _prose_texts(b) for s in split_sentences(t)]
    commas = sum(s.count(",") for s in sentences)

    allowed = []
    if rules is not None:
        allowed = list(rules.form.get("emoji", {}).get("allowed", []))

    return {
        "title_chars": chars(doc.title),
        "body_chars": body_chars,
        "total_chars": chars(doc.title) + body_chars,
        "blocks": len(blocks),
        "paragraphs": len(paragraphs),
        "sentences": len(sentences),
        "paragraph_sentences_max": max(para_sentences, default=0),
        "paragraph_sentences_avg": _avg(para_sentences),
        "subheadings": len(subheads),
        "subheading_chars": subheads,
        "images": len(image_positions),
        "max_blocks_between_images": max(gaps, default=0),
        "max_blocks_without_image": max(gaps + edges, default=0),
        "links": inline_links + link_blocks,
        "link_blocks": link_blocks,
        "inline_links": inline_links,
        "bold": bold,
        "color": color,
        "highlight": highlight,
        "bold_ratio": _ratio(bold_chars, body_chars),
        "color_ratio": _ratio(color_chars, body_chars),
        "highlight_ratio": _ratio(highlight_chars, body_chars),
        "commas_per_sentence": round(commas / len(sentences), 2) if sentences else 0.0,
        "exclamations": body_text.count("!") + doc.title.count("!"),
        "quotes": sum(isinstance(b, Quote) for b in blocks),
        "list_items": sum(len(b.items) for b in blocks if isinstance(b, ListBlock)),
        "checklist_items": sum(len(b.items) for b in blocks if isinstance(b, Checklist)),
        "tables": sum(isinstance(b, Table) for b in blocks),
        "emoji": _emoji_counts(doc.title + "\n" + body_text, allowed),
    }


def _body(doc) -> str:
    return "\n\n".join("\n".join(block_texts(b)) for b in doc.blocks)


def compare(draft_doc, final_doc) -> dict:
    """How much the revise node changed the draft. Report only."""
    a, b = _body(draft_doc), _body(final_doc)
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    nums_a, nums_b = set(NUMBER.findall(draft_doc.title + "\n" + a)), set(NUMBER.findall(final_doc.title + "\n" + b))
    return {
        "change_ratio": round(1 - ratio, 4),
        "similarity": round(ratio, 4),
        "draft_body_chars": chars(a),
        "final_body_chars": chars(b),
        "numbers_added": sorted(nums_b - nums_a),
        "numbers_removed": sorted(nums_a - nums_b),
        "title_changed": draft_doc.title != final_doc.title,
        "draft_title": draft_doc.title,
        "final_title": final_doc.title,
    }
