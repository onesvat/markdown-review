from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterator, TypeVar

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from .schemas import (
    Annotation,
    AnnotationsFile,
    Block,
    OrphanRecord,
    ParagraphRecord,
    Suggestion,
    utcnow_iso,
)


def annotations_path_for(doc_path: Path) -> Path:
    return doc_path.with_name(doc_path.stem + ".annotations.json")


def load_annotations(doc_path: Path) -> AnnotationsFile:
    ann_path = annotations_path_for(doc_path)
    if not ann_path.exists():
        return AnnotationsFile(doc_path=str(doc_path.resolve()))
    raw = ann_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if data.get("schema_version") != 2:
        raise ValueError(
            f"{ann_path} uses schema_version {data.get('schema_version', 1)}; "
            "run the v1 migration script before opening it"
        )
    return AnnotationsFile.model_validate_json(raw)


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".annotations.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_annotations(doc_path: Path, ann: AnnotationsFile) -> None:
    ann.updated_at = utcnow_iso()
    ann.doc_path = str(doc_path.resolve())
    payload = ann.model_dump(mode="json")
    _atomic_write(annotations_path_for(doc_path), json.dumps(payload, ensure_ascii=False, indent=2))


T = TypeVar("T")


def _lock_path_for(doc_path: Path) -> Path:
    return doc_path.with_name(doc_path.stem + ".annotations.lock")


@contextlib.contextmanager
def doc_file_lock(doc_path: Path) -> Iterator[None]:
    """Cross-process advisory lock around a doc's sidecar read-modify-write.

    The CLI writes the sidecar directly (not via the server), so concurrent CLI
    processes — e.g. parallel review subagents — would otherwise clobber each
    other. An flock on a sibling lockfile serializes them.
    """
    if fcntl is None:
        yield
        return
    lock_path = _lock_path_for(doc_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mutate_annotations(doc_path: Path, fn: "Callable[[AnnotationsFile], T]") -> T:
    """Load → mutate → save the sidecar atomically under a cross-process lock."""
    with doc_file_lock(doc_path):
        ann = load_annotations(doc_path)
        result = fn(ann)
        save_annotations(doc_path, ann)
        return result


def record_has_review_data(rec: ParagraphRecord | OrphanRecord) -> bool:
    return bool(rec.annotations or rec.edits or rec.suggestions)


def source_hash(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def doc_revision_id(src: str) -> str:
    return hashlib.sha1(src.encode("utf-8")).hexdigest()


def _line_start_offsets(src: str) -> list[int]:
    offsets = [0]
    for idx, ch in enumerate(src):
        if ch == "\n":
            offsets.append(idx + 1)
    return offsets


def _block_offsets(src: str, block: Block, line_offsets: list[int]) -> tuple[int | None, int | None]:
    if block.source_start_line is None or block.source_end_line is None:
        return None, None
    start_line = block.source_start_line
    if start_line >= len(line_offsets):
        return None, None

    line_start = line_offsets[start_line]
    found = src.find(block.raw, line_start)
    if found != -1:
        return found, found + len(block.raw)

    end_line = block.source_end_line
    end = line_offsets[end_line] if end_line < len(line_offsets) else len(src)
    while end > line_start and src[end - 1] in "\r\n":
        end -= 1
    return line_start, end


def attach_source_metadata(blocks: list[Block], src: str) -> None:
    line_offsets = _line_start_offsets(src)
    for block in blocks:
        block.legacy_id = block.paragraph_id
        block.source_start, block.source_end = _block_offsets(src, block, line_offsets)


def _review_record_id() -> str:
    return str(uuid.uuid4())


def _candidate_by_range(
    records: list[ParagraphRecord],
    used: set[str],
    block: Block,
) -> ParagraphRecord | None:
    for rec in records:
        if rec.id in used:
            continue
        if rec.source_start_line != block.source_start_line:
            continue
        if rec.block_type and rec.block_type != block.type:
            continue
        return rec
    return None


def _candidate_by_legacy_id(
    records: list[ParagraphRecord],
    used: set[str],
    legacy_id: str | None,
) -> ParagraphRecord | None:
    if not legacy_id:
        return None
    for rec in records:
        if rec.id in used:
            continue
        if rec.legacy_id == legacy_id or rec.content_hash == legacy_id or rec.id == legacy_id:
            return rec
    return None


def _record_for_block(records: list[ParagraphRecord], used: set[str], block: Block) -> ParagraphRecord:
    record = _candidate_by_range(records, used, block)
    if record is None:
        record = _candidate_by_legacy_id(records, used, block.legacy_id)
    if record is not None:
        used.add(record.id)
        return record
    record = ParagraphRecord(id=_review_record_id(), preview=block.preview or "")
    used.add(record.id)
    return record


def _update_record_from_block(rec: ParagraphRecord, block: Block) -> None:
    rec.preview = block.preview or ""
    rec.legacy_id = block.legacy_id or rec.legacy_id or rec.id
    rec.block_type = block.type
    rec.content_hash = source_hash(block.raw)
    rec.source_start_line = block.source_start_line
    rec.source_end_line = block.source_end_line
    rec.source_start = block.source_start
    rec.source_end = block.source_end
    rec.active = True


def reconcile_with_blocks(ann: AnnotationsFile, blocks: list[Block], src: str) -> AnnotationsFile:
    """Assign stable review item IDs to live parser blocks and update sidecar state.

    Parser IDs are content-derived and become `legacy_id`. The public item ID in
    the API remains `paragraph_id` for compatibility, but after reconciliation it
    is the stable review record ID.
    """
    attach_source_metadata(blocks, src)
    ann.schema_version = 2
    ann.doc_revision_id = doc_revision_id(src)

    old_records = list(ann.paragraphs.values())
    for rec in old_records:
        rec.legacy_id = rec.legacy_id or rec.content_hash or rec.id
        rec.content_hash = rec.content_hash or rec.legacy_id

    used: set[str] = set()
    next_records: dict[str, ParagraphRecord] = {}
    for block in blocks:
        if not block.paragraph_id:
            continue
        rec = _record_for_block(old_records, used, block)
        rec.id = rec.id or _review_record_id()
        _update_record_from_block(rec, block)
        block.paragraph_id = rec.id
        next_records[rec.id] = rec

    for rec in old_records:
        if rec.id in next_records:
            continue
        if record_has_review_data(rec):
            ann.orphans.append(
                OrphanRecord(
                    paragraph_id=rec.id,
                    preview=rec.preview,
                    annotations=rec.annotations,
                    edits=rec.edits,
                    suggestions=rec.suggestions,
                )
            )

    ann.paragraphs = next_records
    return ann


class PathLocks:
    """Per-path asyncio locks so concurrent mutations on the same doc serialize."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, doc_path: Path) -> asyncio.Lock:
        return self._locks[str(doc_path.resolve())]


def find_annotation(ann: AnnotationsFile, annotation_id: str) -> tuple[ParagraphRecord, Annotation] | None:
    for para in ann.paragraphs.values():
        for a in para.annotations:
            if a.id == annotation_id:
                return para, a
    return None


def find_suggestion(ann: AnnotationsFile, suggestion_id: str) -> tuple[ParagraphRecord, Suggestion] | None:
    for para in ann.paragraphs.values():
        for s in para.suggestions:
            if s.id == suggestion_id:
                return para, s
    return None
