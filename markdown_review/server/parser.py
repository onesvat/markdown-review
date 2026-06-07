from __future__ import annotations

import hashlib
import re
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.colon_fence import colon_fence_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin

from .schemas import Block


_WS_RE = re.compile(r"\s+")


def normalize(content: str) -> str:
    return _WS_RE.sub(" ", content.strip())


def paragraph_id(content: str) -> str:
    return hashlib.sha1(normalize(content).encode("utf-8")).hexdigest()[:12]


def review_item_id(block_type: str, content: str) -> str:
    if block_type == "paragraph":
        return paragraph_id(content)
    return hashlib.sha1(f"{block_type}\n{normalize(content)}".encode("utf-8")).hexdigest()[:12]


def preview_of(content: str, n: int = 120) -> str:
    flat = normalize(content)
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _build_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": False})
    md.enable("table")
    md.use(front_matter_plugin)
    md.use(dollarmath_plugin, allow_labels=True, double_inline=False)
    md.use(colon_fence_plugin)
    return md


_MD = _build_md()


# Top-level opening tokens we treat as block boundaries → resulting block type.
# For container blocks the parser emits *_open / *_close pairs around children; for atomic
# blocks (code, math, fence, hr, html) a single token carries everything.
_OPEN_TO_CLOSE = {
    "heading_open": ("heading_close", "heading"),
    "paragraph_open": ("paragraph_close", "paragraph"),
    "bullet_list_open": ("bullet_list_close", "bullet_list"),
    "ordered_list_open": ("ordered_list_close", "ordered_list"),
    "blockquote_open": ("blockquote_close", "blockquote"),
    "table_open": ("table_close", "table"),
}

_ATOMIC_TYPES = {
    "fence": "code",
    "code_block": "code",
    "math_block": "math_block",
    "math_block_label": "math_block",
    "hr": "hr",
    "html_block": "html_block",
    "front_matter": "frontmatter",
    "colon_fence": "colon_fence",
}


def _slice_lines(src_lines: list[str], start: int, end: int) -> str:
    return "\n".join(src_lines[start:end])


def _make_block(block_type: str, raw: str, start_line: int | None = None, end_line: int | None = None) -> Block:
    block = Block(type=block_type, raw=raw)
    block.paragraph_id = review_item_id(block_type, raw)
    block.preview = preview_of(raw)
    block.source_start_line = start_line
    block.source_end_line = end_line
    return block


def _find_matching_close(tokens, start: int, open_type: str, close_type: str, level: int) -> int:
    depth = 1
    j = start + 1
    while j < len(tokens):
        t = tokens[j]
        if t.type == open_type and t.level == level:
            depth += 1
        elif t.type == close_type and t.level == level:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return start


def _append_list_item_blocks(blocks: list[Block], tokens, list_start: int, src_lines: list[str]) -> int:
    list_open = tokens[list_start]
    list_close = "ordered_list_close" if list_open.type == "ordered_list_open" else "bullet_list_close"
    block_type = "ordered_list_item" if list_open.type == "ordered_list_open" else "bullet_list_item"
    list_end = _find_matching_close(tokens, list_start, list_open.type, list_close, list_open.level)
    item_level = list_open.level + 1

    j = list_start + 1
    while j < list_end:
        item = tokens[j]
        if item.type == "list_item_open" and item.level == item_level and item.map:
            start_line, end_line = item.map
            raw = _slice_lines(src_lines, start_line, end_line)
            blocks.append(_make_block(block_type, raw, start_line, end_line))
            j = _find_matching_close(tokens, j, "list_item_open", "list_item_close", item.level) + 1
            continue
        j += 1

    return list_end + 1


def parse_blocks(doc_path: Path) -> list[Block]:
    src = doc_path.read_text(encoding="utf-8")
    return parse_blocks_from_text(src)


def parse_blocks_from_text(src: str) -> list[Block]:
    src_lines = src.splitlines()
    tokens = _MD.parse(src)

    blocks: list[Block] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.level != 0:
            i += 1
            continue

        if tok.type in {"bullet_list_open", "ordered_list_open"}:
            i = _append_list_item_blocks(blocks, tokens, i, src_lines)
            continue

        if tok.type in _OPEN_TO_CLOSE:
            close_type, block_type = _OPEN_TO_CLOSE[tok.type]
            start_line = tok.map[0] if tok.map else 0
            # find matching close at level 0
            j = _find_matching_close(tokens, i, tok.type, close_type, tok.level)
            end_tok = tokens[j] if j < len(tokens) else tok
            end_line = end_tok.map[1] if end_tok.map else (tok.map[1] if tok.map else start_line + 1)
            raw = _slice_lines(src_lines, start_line, end_line)
            blocks.append(_make_block(block_type, raw, start_line, end_line))
            i = j + 1
            continue

        if tok.type in _ATOMIC_TYPES:
            block_type = _ATOMIC_TYPES[tok.type]
            # MyST directives in a backtick fence (```{figure} ..., ```{list-table} ...)
            if tok.type == "fence" and (tok.info or "").lstrip().startswith("{"):
                info = (tok.info or "").lstrip()
                if info.startswith("{figure}"):
                    block_type = "figure"
                else:
                    block_type = "colon_fence"
            if tok.map:
                start_line, end_line = tok.map
                raw = _slice_lines(src_lines, start_line, end_line)
            else:
                start_line = None
                end_line = None
                raw = tok.content or ""
            blocks.append(_make_block(block_type, raw, start_line, end_line))
            i += 1
            continue

        # Unknown top-level token: skip but advance.
        i += 1

    # Post-process: detect standalone image paragraphs (![alt](src) on its own line).
    _IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
    for block in blocks:
        if block.type == "paragraph" and _IMAGE_RE.match(block.raw):
            block.type = "image"

    return blocks
