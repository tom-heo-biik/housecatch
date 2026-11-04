"""Document tree for a durumi manuscript. Shared by markup, render, lint and measure.

The tree is immutable. Inline lists are tuples of inline nodes; blocks hold tuples of inlines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


# Inline nodes


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Bold:
    text: str


@dataclass(frozen=True)
class Color:
    role: int  # 1 or 2, index into the document palettes
    text: str


@dataclass(frozen=True)
class Highlight:
    role: int  # 1 or 2
    text: str


@dataclass(frozen=True)
class LinkSlot:
    target: str


@dataclass(frozen=True)
class LineBreak:
    pass


Inline = Union[Text, Bold, Color, Highlight, LinkSlot, LineBreak]
Inlines = tuple  # tuple[Inline, ...]


# Block nodes


@dataclass(frozen=True)
class Paragraph:
    inlines: Inlines


@dataclass(frozen=True)
class Subheading:
    inlines: Inlines


@dataclass(frozen=True)
class Quote:
    lines: tuple  # tuple[Inlines, ...]


@dataclass(frozen=True)
class ListBlock:
    ordered: bool
    items: tuple  # tuple[Inlines, ...]


@dataclass(frozen=True)
class Checklist:
    items: tuple  # tuple[Inlines, ...], each item text starts after the leading checkbox emoji


@dataclass(frozen=True)
class Table:
    header: tuple  # tuple[Inlines, ...]
    rows: tuple  # tuple[tuple[Inlines, ...], ...]


@dataclass(frozen=True)
class ImageSlot:
    desc: str


@dataclass(frozen=True)
class LinkBlock:
    target: str


Block = Union[Paragraph, Subheading, Quote, ListBlock, Checklist, Table, ImageSlot, LinkBlock]


@dataclass(frozen=True)
class Document:
    slug: str
    title: str
    palettes: tuple  # palette keys, 1 or 2
    blocks: tuple  # tuple[Block, ...]


def inline_text(inlines) -> str:
    """Plain text of an inline sequence, line breaks as newlines, slots as their visible text."""
    parts = []
    for node in inlines:
        if isinstance(node, Text):
            parts.append(node.value)
        elif isinstance(node, (Bold, Color, Highlight)):
            parts.append(node.text)
        elif isinstance(node, LinkSlot):
            parts.append(node.target)
        elif isinstance(node, LineBreak):
            parts.append("\n")
    return "".join(parts)


def block_texts(block) -> list[str]:
    """Visible text units of a block: one string per line, item or cell."""
    if isinstance(block, (Paragraph, Subheading)):
        return [inline_text(block.inlines)]
    if isinstance(block, Quote):
        return [inline_text(line) for line in block.lines]
    if isinstance(block, (ListBlock, Checklist)):
        return [inline_text(item) for item in block.items]
    if isinstance(block, Table):
        cells = [inline_text(c) for c in block.header]
        for row in block.rows:
            cells.extend(inline_text(c) for c in row)
        return cells
    if isinstance(block, ImageSlot):
        return [block.desc]
    if isinstance(block, LinkBlock):
        return [block.target]
    return []


def document_text(doc: Document) -> str:
    """Whole visible text: title then blocks, separated by blank lines."""
    chunks = [doc.title]
    for block in doc.blocks:
        chunks.append("\n".join(block_texts(block)))
    return "\n\n".join(chunks)
