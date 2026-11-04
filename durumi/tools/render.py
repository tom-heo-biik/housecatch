"""Renderer: Document tree to SmartEditor ONE paste-ready HTML.

Rendering turns marks into tags and escapes text. It never rewrites words; the only text
normalizations are the image description suffix tidy-up and VS16 on emoji the rules list
with VS16 (for example the checkbox and warning sign), both required by the spec.
Same input always gives the same bytes.
"""
from __future__ import annotations

import html
import re

from document import (
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

VS16 = "️"
CHECKBOX = "☑" + VS16
IMAGE_SUFFIX = "이미지"


class RenderError(ValueError):
    """Raised when a Document cannot be rendered with the given rules."""


def _vs16_pattern(rules):
    """Regex that finds emoji bases (from rules) missing their VS16."""
    bases = set()
    for emoji in rules.form.get("emoji", {}).get("allowed", []):
        if emoji.endswith(VS16) and len(emoji) == 2:
            bases.add(emoji[0])
    bases.add(CHECKBOX[0])
    return re.compile("([" + "".join(re.escape(b) for b in sorted(bases)) + "])(?!" + VS16 + ")")


class _Renderer:
    def __init__(self, doc: Document, rules):
        self.doc = doc
        self.rules = rules
        self.form = rules.form
        self.vs16 = _vs16_pattern(rules)
        try:
            self.palettes = [rules.palettes[key] for key in doc.palettes]
        except KeyError as exc:
            raise RenderError(f"규칙에 없는 팔레트입니다: {exc.args[0]}") from exc

    # Text

    def text(self, value: str) -> str:
        return html.escape(self.vs16.sub(lambda m: m.group(1) + VS16, value), quote=True)

    def palette(self, role: int):
        if not 1 <= role <= len(self.palettes):
            raise RenderError(f"팔레트 {role}번이 문서에 없습니다(팔레트 {len(self.palettes)}개).")
        return self.palettes[role - 1]

    def link_slot(self, target: str) -> str:
        return self.form["placeholder"]["link"].format(color=self.rules.placeholder, target=self.text(target))

    def image_slot(self, desc: str) -> str:
        core = desc.strip()
        if core.endswith(IMAGE_SUFFIX):
            core = core[: -len(IMAGE_SUFFIX)].rstrip()
        return self.form["placeholder"]["image"].format(color=self.rules.placeholder, desc=self.text(core))

    def inlines(self, nodes) -> str:
        parts = []
        for node in nodes:
            if isinstance(node, Text):
                parts.append(self.text(node.value))
            elif isinstance(node, Bold):
                parts.append(f"<strong>{self.text(node.text)}</strong>")
            elif isinstance(node, Color):
                hex_ = self.palette(node.role).text
                parts.append(f'<strong><span style="color:{hex_};">{self.text(node.text)}</span></strong>')
            elif isinstance(node, Highlight):
                hex_ = self.palette(node.role).highlight
                parts.append(f'<span style="background-color:{hex_};">{self.text(node.text)}</span>')
            elif isinstance(node, LinkSlot):
                parts.append(self.link_slot(node.target))
            elif isinstance(node, LineBreak):
                parts.append("<br>")
            else:
                raise RenderError(f"알 수 없는 인라인 노드입니다: {type(node).__name__}")
        return "".join(parts)

    # Blocks

    def block(self, block) -> str:
        if isinstance(block, Paragraph):
            return f"<p>{self.inlines(block.inlines)}</p>"
        if isinstance(block, Subheading):
            px = int(self.form.get("subheading_font_px", 24))
            return f'<p><strong><span style="font-size:{px}px;">{self.inlines(block.inlines)}</span></strong></p>'
        if isinstance(block, Quote):
            return "<blockquote>" + "<br>".join(self.inlines(line) for line in block.lines) + "</blockquote>"
        if isinstance(block, ListBlock):
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{self.inlines(item)}</li>" for item in block.items)
            return f"<{tag}>{items}</{tag}>"
        if isinstance(block, Checklist):
            return "<p>" + "<br>".join(f"{CHECKBOX} {self.inlines(item)}" for item in block.items) + "</p>"
        if isinstance(block, Table):
            head = "".join(f"<th>{self.inlines(cell)}</th>" for cell in block.header)
            body = "".join(
                "<tr>" + "".join(f"<td>{self.inlines(cell)}</td>" for cell in row) + "</tr>" for row in block.rows
            )
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        if isinstance(block, ImageSlot):
            return f"<p>{self.image_slot(block.desc)}</p>"
        if isinstance(block, LinkBlock):
            return f"<p>{self.link_slot(block.target)}</p>"
        raise RenderError(f"알 수 없는 블록입니다: {type(block).__name__}")

    def gap(self, index: int) -> int:
        """Number of standalone <br> lines before block `index`."""
        spacing = self.form.get("spacing", {})
        block = self.doc.blocks[index]
        if isinstance(block, Subheading):
            return int(spacing.get("before_subheading", 3))
        if index == 0:
            return int(spacing.get("after_title", 1))
        if isinstance(self.doc.blocks[index - 1], Subheading):
            return int(spacing.get("after_subheading", 1))
        return int(spacing.get("between_blocks", 1))

    def document(self) -> str:
        title = self.text(self.doc.title)
        lines = [
            "<!doctype html>",
            '<html lang="ko">',
            "<head>",
            '<meta charset="utf-8">',
            f"<title>{title}</title>",
            f"<style>{self.form['html']['style']}</style>",
            "</head>",
            "<body>",
            f"<h1>{title}</h1>",
        ]
        for index, block in enumerate(self.doc.blocks):
            lines.extend(["<br>"] * self.gap(index))
            lines.append(self.block(block))
        lines.extend(["</body>", "</html>"])
        return "\n".join(lines) + "\n"


def render(doc: Document, rules) -> str:
    """Render a parsed Document to a complete HTML document string (LF, trailing newline)."""
    return _Renderer(doc, rules).document()
