from __future__ import annotations

from .schemas import SuggestionHunk
from .storage import source_hash


def occurrence_start(haystack: str, needle: str, occurrence: int) -> int:
    if not needle or occurrence < 0:
        return -1
    pos = 0
    for idx in range(occurrence + 1):
        found = haystack.find(needle, pos)
        if found == -1:
            return -1
        if idx == occurrence:
            return found
        pos = found + len(needle)
    return -1


def _hunk_context(raw: str, start: int, end: int, size: int = 32) -> tuple[str, str]:
    return raw[max(0, start - size) : start], raw[end : end + size]


def _make_hunk(
    ann,
    target,
    *,
    start: int,
    end: int,
    old_text: str,
    new_text: str,
    kind: str,
    placement: str = "inside",
) -> SuggestionHunk:
    prefix, suffix = _hunk_context(target.raw, start, end)
    return SuggestionHunk(
        item_id=target.paragraph_id,
        base_revision_id=ann.doc_revision_id,
        base_item_hash=source_hash(target.raw),
        base_item_text=target.raw,
        start=start,
        end=end,
        old_text=old_text,
        new_text=new_text,
        prefix_context=prefix,
        suffix_context=suffix,
        kind=kind,
        placement=placement,
    )


def build_suggestion_hunk(
    ann,
    target,
    action: str,
    raw: str,
    selected_text: str = "",
    occurrence: int = 0,
) -> SuggestionHunk:
    if action == "replace":
        return _make_hunk(
            ann,
            target,
            start=0,
            end=len(target.raw),
            old_text=target.raw,
            new_text=raw,
            kind="replace",
        )
    if action == "delete":
        return _make_hunk(
            ann,
            target,
            start=0,
            end=len(target.raw),
            old_text=target.raw,
            new_text="",
            kind="delete",
        )
    if action == "insert_before":
        return _make_hunk(
            ann,
            target,
            start=0,
            end=0,
            old_text="",
            new_text=raw,
            kind="insert",
            placement="before",
        )
    if action == "insert_after":
        end = len(target.raw)
        return _make_hunk(
            ann,
            target,
            start=end,
            end=end,
            old_text="",
            new_text=raw,
            kind="insert",
            placement="after",
        )
    if action == "inline_replace":
        if not selected_text.strip():
            raise ValueError("selected_text is required for inline suggestions")
        if occurrence < 0:
            raise ValueError("occurrence must be >= 0")
        start = occurrence_start(target.raw, selected_text, occurrence)
        if start == -1:
            raise ValueError("selected text does not match item source")
        end = start + len(selected_text)
        return _make_hunk(
            ann,
            target,
            start=start,
            end=end,
            old_text=selected_text,
            new_text=raw,
            kind="replace",
        )
    raise ValueError("unsupported suggestion action")
