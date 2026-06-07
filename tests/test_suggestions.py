from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from markdown_review import cli
from markdown_review.server.main import _apply_open_suggestion
from markdown_review.server.patching import build_suggestion_hunk
from markdown_review.server.parser import parse_blocks
from markdown_review.server.schemas import Annotation, AnnotationsFile, Suggestion
from markdown_review.server.storage import load_annotations, reconcile_with_blocks


def reconciled(doc: Path) -> tuple[AnnotationsFile, list]:
    ann = AnnotationsFile(doc_path=str(doc))
    src = doc.read_text(encoding="utf-8")
    blocks = parse_blocks(doc)
    reconcile_with_blocks(ann, blocks, src)
    return ann, blocks


def paragraph_with(blocks, text: str):
    for block in blocks:
        if block.type == "paragraph" and text in block.raw:
            return block
    raise AssertionError(f"paragraph not found: {text}")


def suggestion_for(ann, target, suggestion_id: str, selected: str, occurrence: int, replacement: str) -> Suggestion:
    hunk = build_suggestion_hunk(ann, target, "inline_replace", replacement, selected, occurrence)
    return Suggestion(
        id=suggestion_id,
        author="agent",
        action="inline_replace",
        anchor_id=target.paragraph_id,
        raw=replacement,
        selected_text=selected,
        occurrence=occurrence,
        hunks=[hunk],
    )


class SuggestionTests(unittest.TestCase):
    def test_schema_v2_records_default_to_empty_review_data(self) -> None:
        ann = AnnotationsFile(doc_path="/tmp/doc.md")

        self.assertEqual(ann.schema_version, 2)
        self.assertEqual(ann.paragraphs, {})
        self.assertEqual(ann.orphans, [])

    def test_annotation_seen_is_not_migrated_from_legacy_status(self) -> None:
        ann = Annotation.model_validate({"id": "a1", "author": "user", "type": "comment", "text": "x"})

        self.assertFalse(ann.seen)

    def test_accept_two_inline_suggestions_in_same_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("Alpha beta alpha beta.\n", encoding="utf-8")
            ann, blocks = reconciled(doc)
            target = paragraph_with(blocks, "Alpha")
            rec = ann.paragraphs[target.paragraph_id]
            rec.suggestions.extend(
                [
                    suggestion_for(ann, target, "s1", "beta", 0, "gamma"),
                    suggestion_for(ann, target, "s2", "beta", 1, "delta"),
                ]
            )

            first = _apply_open_suggestion(doc, ann, "s1")
            second = _apply_open_suggestion(doc, ann, "s2")

            self.assertEqual(doc.read_text(encoding="utf-8"), "Alpha gamma alpha delta.\n")
            self.assertEqual(first.suggestion.status, "accepted")
            self.assertEqual(second.suggestion.status, "accepted")
            self.assertEqual(first.old_id, first.new_id)
            self.assertEqual(second.old_id, second.new_id)

    def test_later_hunk_rebases_after_prior_length_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("One two three four.\n", encoding="utf-8")
            ann, blocks = reconciled(doc)
            target = paragraph_with(blocks, "One")
            rec = ann.paragraphs[target.paragraph_id]
            rec.suggestions.extend(
                [
                    suggestion_for(ann, target, "s1", "two", 0, "very long two"),
                    suggestion_for(ann, target, "s2", "four", 0, "five"),
                ]
            )

            _apply_open_suggestion(doc, ann, "s1")
            _apply_open_suggestion(doc, ann, "s2")

            self.assertEqual(doc.read_text(encoding="utf-8"), "One very long two three five.\n")

    def test_changed_old_text_returns_conflict_and_leaves_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("A target B target C.\n", encoding="utf-8")
            ann, blocks = reconciled(doc)
            target = paragraph_with(blocks, "target")
            rec = ann.paragraphs[target.paragraph_id]
            rec.suggestions.append(suggestion_for(ann, target, "s1", "target", 0, "replacement"))
            doc.write_text("A changed B target C.\n", encoding="utf-8")

            with self.assertRaises(HTTPException) as err:
                _apply_open_suggestion(doc, ann, "s1")

            self.assertEqual(err.exception.status_code, 409)
            self.assertEqual(doc.read_text(encoding="utf-8"), "A changed B target C.\n")
            self.assertEqual(rec.suggestions[0].status, "needs_resolution")

    def test_insert_after_uses_patch_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("# Title\n\nFirst paragraph.\n\nSecond paragraph.\n", encoding="utf-8")
            ann, blocks = reconciled(doc)
            target = paragraph_with(blocks, "First")
            hunk = build_suggestion_hunk(ann, target, "insert_after", "Inserted paragraph.", "", 0)
            suggestion = Suggestion(
                id="s1",
                author="agent",
                action="insert_after",
                anchor_id=target.paragraph_id,
                raw="Inserted paragraph.",
                hunks=[hunk],
            )
            ann.paragraphs[target.paragraph_id].suggestions.append(suggestion)

            _apply_open_suggestion(doc, ann, "s1")

            self.assertEqual(
                doc.read_text(encoding="utf-8"),
                "# Title\n\nFirst paragraph.\n\nInserted paragraph.\n\nSecond paragraph.\n",
            )
            self.assertEqual(suggestion.status, "accepted")

    def test_load_annotations_rejects_v1_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("First paragraph.\n", encoding="utf-8")
            doc.with_name("doc.annotations.json").write_text(
                json.dumps({"schema_version": 1, "doc_path": str(doc), "paragraphs": {}}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_annotations(doc)

    def test_cli_inline_suggest_stores_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("First paragraph.\n", encoding="utf-8")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["items", str(doc), "--json"])
            self.assertEqual(code, 0)
            target_id = json.loads(out.getvalue())[0]["item_id"]

            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(
                    [
                        "suggest",
                        "--path",
                        str(doc),
                        "--item-id",
                        target_id,
                        "--action",
                        "inline-replace",
                        "--selected-text",
                        "First",
                        "--raw",
                        "Updated",
                    ]
                )
            self.assertEqual(code, 0)

            ann = load_annotations(doc)
            suggestion = next(iter(ann.paragraphs.values())).suggestions[0]
            self.assertEqual(suggestion.action, "inline_replace")
            self.assertEqual(suggestion.hunks[0].old_text, "First")
            self.assertEqual(suggestion.hunks[0].new_text, "Updated")


if __name__ == "__main__":
    unittest.main()
