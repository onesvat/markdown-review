#!/usr/bin/env python3
"""One-shot migration for schema v1 markdown-review sidecars.

This script is intentionally separate from runtime loading: the app expects
schema v2 sidecars only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

from markdown_review.server.patching import build_suggestion_hunk
from markdown_review.server.parser import parse_blocks
from markdown_review.server.schemas import Annotation, AnnotationsFile, OrphanRecord, Suggestion
from markdown_review.server.storage import annotations_path_for, reconcile_with_blocks, save_annotations


def _annotation_rows(rec: dict) -> list[Annotation]:
    rows = []
    for raw in rec.get("annotations") or []:
        data = dict(raw)
        data.pop("status", None)
        rows.append(Annotation.model_validate(data))
    for raw in rec.get("highlights") or []:
        data = {
            "id": raw.get("id") or str(uuid.uuid4()),
            "author": raw.get("author", "user"),
            "type": "comment",
            "text": raw.get("text", ""),
            "selected_text": raw.get("selected_text", ""),
            "occurrence": raw.get("occurrence", 0),
            "seen": False,
        }
        if raw.get("ts"):
            data["ts"] = raw["ts"]
        rows.append(Annotation(**data))
    return rows


def _suggestion_row(ann: AnnotationsFile, block, raw: dict) -> Suggestion:
    action = str(raw.get("action") or "replace").replace("-", "_")
    data = {
        "id": raw.get("id") or str(uuid.uuid4()),
        "author": raw.get("author", "agent"),
        "action": action,
        "anchor_id": block.paragraph_id if block else raw.get("anchor_id", ""),
        "raw": raw.get("raw", ""),
        "selected_text": raw.get("selected_text", ""),
        "occurrence": raw.get("occurrence", 0),
        "note": raw.get("note", ""),
        "status": raw.get("status", "open"),
        "applied_ts": raw.get("applied_ts"),
    }
    if raw.get("ts"):
        data["ts"] = raw["ts"]
    suggestion = Suggestion(**data)
    if not block:
        suggestion.status = "needs_resolution"
        suggestion.conflict = "legacy anchor item was not found during migration"
        return suggestion
    try:
        suggestion.hunks = [
            build_suggestion_hunk(
                ann,
                block,
                action,
                suggestion.raw,
                suggestion.selected_text,
                suggestion.occurrence,
            )
        ]
    except Exception as exc:  # noqa: BLE001 - migration should preserve data and mark unresolved.
        suggestion.status = "needs_resolution"
        suggestion.conflict = f"could not migrate legacy hunk: {exc}"
    return suggestion


def migrate(doc: Path) -> Path:
    sidecar = annotations_path_for(doc)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    if data.get("schema_version") == 2:
        return sidecar
    if data.get("schema_version", 1) != 1:
        raise SystemExit(f"unsupported schema_version: {data.get('schema_version')}")

    ann = AnnotationsFile(doc_path=str(doc.resolve()))
    src = doc.read_text(encoding="utf-8")
    blocks = parse_blocks(doc)
    reconcile_with_blocks(ann, blocks, src)
    by_legacy = {b.legacy_id: b for b in blocks}

    for legacy_id, raw_rec in (data.get("paragraphs") or {}).items():
        block = by_legacy.get(legacy_id)
        if not block:
            ann.orphans.append(
                OrphanRecord(
                    paragraph_id=legacy_id,
                    preview=raw_rec.get("preview", ""),
                    annotations=_annotation_rows(raw_rec),
                    suggestions=[],
                )
            )
            continue
        rec = ann.paragraphs[block.paragraph_id]
        rec.annotations.extend(_annotation_rows(raw_rec))
        rec.suggestions.extend(_suggestion_row(ann, block, s) for s in raw_rec.get("suggestions") or [])

    backup = sidecar.with_name(f"{doc.stem}.annotations.v1.json")
    if not backup.exists():
        shutil.copy2(sidecar, backup)
    save_annotations(doc, ann)
    return sidecar


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate a markdown-review v1 sidecar to schema v2")
    parser.add_argument("doc", type=Path, help="Markdown document path")
    args = parser.parse_args()
    migrated = migrate(args.doc.expanduser().resolve())
    print(migrated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
