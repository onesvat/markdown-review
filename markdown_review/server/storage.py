from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
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
    Highlight,
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
    return bool(rec.annotations or rec.highlights or rec.edits or rec.suggestions)


def has_review_data(ann: AnnotationsFile) -> bool:
    return bool(ann.orphans or any(record_has_review_data(rec) for rec in ann.paragraphs.values()))


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


def find_highlight(ann: AnnotationsFile, highlight_id: str) -> tuple[ParagraphRecord, Highlight] | None:
    for para in ann.paragraphs.values():
        for h in para.highlights:
            if h.id == highlight_id:
                return para, h
    return None


def find_suggestion(ann: AnnotationsFile, suggestion_id: str) -> tuple[ParagraphRecord, Suggestion] | None:
    for para in ann.paragraphs.values():
        for s in para.suggestions:
            if s.id == suggestion_id:
                return para, s
    return None


def reconcile_with_live(ann: AnnotationsFile, live_paragraph_ids: dict[str, str]) -> AnnotationsFile:
    """Move paragraph records whose ID is no longer present in the live doc into orphans.

    live_paragraph_ids: {paragraph_id: preview} for paragraphs currently in the doc.
    """
    live_ids = set(live_paragraph_ids.keys())
    stored_ids = set(ann.paragraphs.keys())

    missing = stored_ids - live_ids
    for pid in missing:
        rec = ann.paragraphs.pop(pid)
        # only orphan if it actually has review data worth keeping
        if record_has_review_data(rec):
            ann.orphans.append(
                OrphanRecord(
                    paragraph_id=pid,
                    preview=rec.preview,
                    annotations=rec.annotations,
                    highlights=rec.highlights,
                    edits=rec.edits,
                    suggestions=rec.suggestions,
                )
            )

    # Refresh previews for live paragraphs that already had records.
    for pid, preview in live_paragraph_ids.items():
        if pid in ann.paragraphs:
            ann.paragraphs[pid].preview = preview

    return ann
