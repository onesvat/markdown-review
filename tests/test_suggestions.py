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
from markdown_review.server.parser import parse_blocks
from markdown_review.server.schemas import Annotation, AnnotationsFile, ParagraphRecord, Suggestion
from markdown_review.server.storage import load_annotations, reconcile_with_live


def paragraph_with(doc: Path, text: str):
    for block in parse_blocks(doc):
        if block.type == "paragraph" and text in block.raw:
            return block
    raise AssertionError(f"paragraph not found: {text}")


class SuggestionTests(unittest.TestCase):
    def test_old_sidecar_records_default_to_empty_suggestions(self) -> None:
        rec = ParagraphRecord.model_validate({"id": "abc", "preview": "old"})

        self.assertEqual(rec.suggestions, [])

    def test_legacy_annotation_status_migrates_to_seen(self) -> None:
        unseen = Annotation.model_validate(
            {"id": "a1", "author": "user", "type": "comment", "text": "x", "status": "new"}
        )
        seen = Annotation.model_validate(
            {"id": "a2", "author": "user", "type": "comment", "text": "x", "status": "reviewed"}
        )

        self.assertFalse(unseen.seen)
        self.assertTrue(seen.seen)

    def test_apply_replace_migrates_record_and_marks_suggestion_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("# Title\n\nFirst paragraph.\n\nSecond paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")
            ann = AnnotationsFile(doc_path=str(doc))
            suggestion = Suggestion(
                id="s1",
                author="agent",
                action="replace",
                anchor_id=target.paragraph_id,
                raw="Updated paragraph.",
                note="",
            )
            ann.paragraphs[target.paragraph_id] = ParagraphRecord(
                id=target.paragraph_id,
                preview=target.preview or "",
                suggestions=[suggestion],
            )

            result = _apply_open_suggestion(doc, ann, "s1")

            self.assertIn("Updated paragraph.", doc.read_text(encoding="utf-8"))
            self.assertEqual(result.suggestion.status, "accepted")
            self.assertIsNotNone(result.new_id)
            self.assertIn(result.new_id, ann.paragraphs)
            self.assertEqual(ann.paragraphs[result.new_id].suggestions[0].status, "accepted")
            self.assertEqual(len(ann.paragraphs[result.new_id].edits), 1)

    def test_apply_insert_after_adds_block_with_spacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("# Title\n\nFirst paragraph.\n\nSecond paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")
            ann = AnnotationsFile(doc_path=str(doc))
            suggestion = Suggestion(
                id="s1",
                author="agent",
                action="insert_after",
                anchor_id=target.paragraph_id,
                raw="Inserted paragraph.",
                note="",
            )
            ann.paragraphs[target.paragraph_id] = ParagraphRecord(
                id=target.paragraph_id,
                preview=target.preview or "",
                suggestions=[suggestion],
            )

            _apply_open_suggestion(doc, ann, "s1")

            self.assertEqual(
                doc.read_text(encoding="utf-8"),
                "# Title\n\nFirst paragraph.\n\nInserted paragraph.\n\nSecond paragraph.\n",
            )
            self.assertEqual(suggestion.status, "accepted")

    def test_apply_inline_replace_changes_only_selected_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("Alpha beta alpha beta.\n", encoding="utf-8")
            target = paragraph_with(doc, "Alpha")
            ann = AnnotationsFile(doc_path=str(doc))
            suggestion = Suggestion(
                id="s1",
                author="agent",
                action="inline_replace",
                anchor_id=target.paragraph_id,
                selected_text="beta",
                occurrence=1,
                raw="gamma",
                note="",
            )
            ann.paragraphs[target.paragraph_id] = ParagraphRecord(
                id=target.paragraph_id,
                preview=target.preview or "",
                suggestions=[suggestion],
            )

            result = _apply_open_suggestion(doc, ann, "s1")

            self.assertEqual(doc.read_text(encoding="utf-8"), "Alpha beta alpha gamma.\n")
            self.assertEqual(result.suggestion.status, "accepted")
            self.assertIn(result.new_id, ann.paragraphs)
            self.assertEqual(ann.paragraphs[result.new_id].suggestions[0].selected_text, "beta")

    def test_inline_replace_stale_selected_text_returns_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("Alpha beta.\n", encoding="utf-8")
            target = paragraph_with(doc, "Alpha")
            ann = AnnotationsFile(doc_path=str(doc))
            ann.paragraphs[target.paragraph_id] = ParagraphRecord(
                id=target.paragraph_id,
                preview=target.preview or "",
                suggestions=[
                    Suggestion(
                        id="s1",
                        author="agent",
                        action="inline_replace",
                        anchor_id=target.paragraph_id,
                        selected_text="missing",
                        raw="gamma",
                    )
                ],
            )

            with self.assertRaises(HTTPException) as err:
                _apply_open_suggestion(doc, ann, "s1")

            self.assertEqual(err.exception.status_code, 409)

    def test_apply_delete_can_orphan_suggestion_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("# Title\n\nFirst paragraph.\n\nSecond paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")
            ann = AnnotationsFile(doc_path=str(doc))
            suggestion = Suggestion(
                id="s1",
                author="agent",
                action="delete",
                anchor_id=target.paragraph_id,
                raw="",
                note="",
            )
            ann.paragraphs[target.paragraph_id] = ParagraphRecord(
                id=target.paragraph_id,
                preview=target.preview or "",
                suggestions=[suggestion],
            )

            _apply_open_suggestion(doc, ann, "s1")
            live_ids = {b.paragraph_id: b.preview or "" for b in parse_blocks(doc) if b.paragraph_id}
            reconcile_with_live(ann, live_ids)

            self.assertNotIn("First paragraph.", doc.read_text(encoding="utf-8"))
            self.assertEqual(len(ann.orphans), 1)
            self.assertEqual(ann.orphans[0].suggestions[0].status, "accepted")

    def test_stale_suggestion_returns_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("First paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")
            ann = AnnotationsFile(doc_path=str(doc))
            ann.paragraphs[target.paragraph_id] = ParagraphRecord(
                id=target.paragraph_id,
                preview=target.preview or "",
                suggestions=[
                    Suggestion(
                        id="s1",
                        author="agent",
                        action="replace",
                        anchor_id=target.paragraph_id,
                        raw="Updated paragraph.",
                    )
                ],
            )
            doc.write_text("Changed outside review.\n", encoding="utf-8")

            with self.assertRaises(HTTPException) as err:
                _apply_open_suggestion(doc, ann, "s1")

            self.assertEqual(err.exception.status_code, 409)

    def test_cli_suggest_and_list_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("First paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "suggest",
                        "--path",
                        str(doc),
                        "--item-id",
                        target.paragraph_id,
                        "--action",
                        "insert-after",
                        "--raw",
                        "Inserted paragraph.",
                        "--note",
                        "add bridge",
                    ]
                )
            self.assertEqual(code, 0)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["suggestions", str(doc), "--json"])
            self.assertEqual(code, 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(rows[0]["action"], "insert_after")
            self.assertEqual(rows[0]["note"], "add bridge")

            ann = load_annotations(doc)
            self.assertEqual(len(ann.paragraphs[target.paragraph_id].suggestions), 1)

    def test_cli_inline_suggest_and_list_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("First paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "suggest",
                        "--path",
                        str(doc),
                        "--item-id",
                        target.paragraph_id,
                        "--action",
                        "inline-replace",
                        "--selected-text",
                        "First",
                        "--raw",
                        "Updated",
                    ]
                )
            self.assertEqual(code, 0)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["suggestions", str(doc), "--json"])
            self.assertEqual(code, 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(rows[0]["action"], "inline_replace")
            self.assertEqual(rows[0]["selected_text"], "First")
            self.assertEqual(rows[0]["raw"], "Updated")

    def test_cli_mark_seen_and_unseen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "doc.md"
            doc.write_text("First paragraph.\n", encoding="utf-8")
            target = paragraph_with(doc, "First")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "add",
                        "--path",
                        str(doc),
                        "--item-id",
                        target.paragraph_id,
                        "--author",
                        "agent",
                        "--text",
                        "note",
                    ]
                )
            self.assertEqual(code, 0)
            annotation_id = out.getvalue().strip()

            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["mark-seen", annotation_id, "--path", str(doc)])
            self.assertEqual(code, 0)
            ann = load_annotations(doc)
            self.assertTrue(ann.paragraphs[target.paragraph_id].annotations[0].seen)

            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["mark-unseen", annotation_id, "--path", str(doc)])
            self.assertEqual(code, 0)
            ann = load_annotations(doc)
            self.assertFalse(ann.paragraphs[target.paragraph_id].annotations[0].seen)


if __name__ == "__main__":
    unittest.main()
