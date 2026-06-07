from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import uvicorn

from .server.patching import build_suggestion_hunk
from .server.parser import parse_blocks
from .server.schemas import Suggestion, utcnow_iso
from .server.storage import (
    annotations_path_for,
    doc_file_lock,
    find_annotation,
    load_annotations,
    mutate_annotations,
    reconcile_with_blocks,
    save_annotations,
)


def _heading_level(raw: str) -> int:
    stripped = raw.lstrip()
    n = 0
    for ch in stripped:
        if ch == "#":
            n += 1
        else:
            break
    return n if 1 <= n <= 6 else 0


def _select_blocks(blocks, section=None, from_line=None, to_line=None):
    """Filter parsed blocks by heading section name and/or source line range."""
    selected = blocks
    if section is not None:
        needle = section.strip().lower()
        start_idx = None
        sect_level = 0
        for idx, b in enumerate(blocks):
            if b.type == "heading" and needle in b.raw.strip().lstrip("#").strip().lower():
                start_idx = idx
                sect_level = _heading_level(b.raw)
                break
        if start_idx is None:
            return []
        end_idx = len(blocks)
        for idx in range(start_idx + 1, len(blocks)):
            b = blocks[idx]
            if b.type == "heading" and 0 < _heading_level(b.raw) <= sect_level:
                end_idx = idx
                break
        selected = blocks[start_idx:end_idx]
    if from_line is not None or to_line is not None:
        lo = from_line if from_line is not None else 0
        hi = to_line if to_line is not None else 10**9
        selected = [
            b for b in selected
            if b.source_start_line is not None and lo <= (b.source_start_line + 1) <= hi
        ]
    return selected


def _reconcile_blocks(ann, doc: Path):
    src = doc.read_text(encoding="utf-8")
    blocks = parse_blocks(doc)
    reconcile_with_blocks(ann, blocks, src)
    return blocks


def _find_block(blocks, item_id: str):
    return next((b for b in blocks if b.paragraph_id == item_id or b.legacy_id == item_id), None)


def _open_browser_when_ready(url: str, delay: float = 0.6) -> None:
    def _open() -> None:
        time.sleep(delay)
        webbrowser.open(url)

    t = threading.Thread(target=_open, daemon=True)
    t.start()


def _resolve_doc_arg(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        print(f"error: file not found: {p}", file=sys.stderr)
        sys.exit(2)
    if p.suffix.lower() not in {".md", ".markdown", ".qmd"}:
        print(f"error: only markdown (.md/.markdown/.qmd) files supported: {p}", file=sys.stderr)
        sys.exit(2)
    return p


def cmd_serve(args: argparse.Namespace) -> int:
    host = args.host
    port = args.port

    if args.path:
        p = Path(args.path).expanduser().resolve()
        if not p.exists():
            print(f"error: file not found: {p}", file=sys.stderr)
            sys.exit(2)
        if p.is_dir():
            doc_path = None
            root = p
        else:
            doc_path = _resolve_doc_arg(args.path)
            root = Path.cwd().resolve()
            try:
                doc_path.relative_to(root)
            except ValueError:
                root = doc_path.parent
    else:
        doc_path = None
        root = Path.cwd().resolve()

    os.environ["MDR_ROOT"] = str(root)

    if doc_path is not None:
        os.environ["MDR_DEFAULT_DOC"] = str(doc_path)

    if args.bib:
        os.environ["MDR_BIB"] = os.pathsep.join(str(Path(b).expanduser().resolve()) for b in args.bib)

    if not args.no_open:
        browser_host = "127.0.0.1" if host == "0.0.0.0" else host
        if doc_path is not None:
            qs = urllib.parse.urlencode({"path": str(doc_path)})
            _open_browser_when_ready(f"http://{browser_host}:{port}/?{qs}")
        else:
            _open_browser_when_ready(f"http://{browser_host}:{port}/")

    uvicorn.run(
        "markdown_review.server.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    doc = _resolve_doc_arg(args.path)
    ann = load_annotations(doc)
    rows: list[tuple[str, str, str, str]] = []  # (item_id, annotation_id, author, text)
    for pid, para in ann.paragraphs.items():
        for a in para.annotations:
            if not a.seen:
                rows.append((pid, a.id, a.author, a.text))

    ann_path = annotations_path_for(doc)
    if args.json:
        out = [
            {
                "paragraph_id": pid,
                "annotation_id": aid,
                "author": author,
                "text": text,
                "preview": ann.paragraphs[pid].preview if pid in ann.paragraphs else "",
            }
            for (pid, aid, author, text) in rows
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print(f"no unseen annotations in {ann_path}")
        return 0

    print(f"{len(rows)} unseen annotation(s) in {ann_path}")
    print()
    for pid, aid, author, text in rows:
        preview = ann.paragraphs[pid].preview if pid in ann.paragraphs else ""
        print(f"[{author}] item {pid}  annotation {aid}")
        if preview:
            print(f"  item: {preview}")
        for line in text.splitlines() or [""]:
            print(f"  > {line}")
        print()
    return 0


def _set_annotation_seen(args: argparse.Namespace, seen: bool) -> int:
    doc = _resolve_doc_arg(args.path)

    outcome = {"status": "missing"}

    def _do(ann):
        found = find_annotation(ann, args.annotation_id)
        if not found:
            outcome["status"] = "missing"
            return
        _, target = found
        if target.seen == seen:
            outcome["status"] = "already"
            return
        target.seen = seen
        target.ts = utcnow_iso()
        outcome["status"] = "ok"

    mutate_annotations(doc, _do)
    if outcome["status"] == "missing":
        print(f"error: annotation {args.annotation_id} not found in {doc}", file=sys.stderr)
        return 1
    if outcome["status"] == "already":
        print(f"annotation {args.annotation_id} already {'seen' if seen else 'unseen'}")
        return 0
    print(f"marked {args.annotation_id} as {'seen' if seen else 'unseen'}")
    return 0


def cmd_mark_seen(args: argparse.Namespace) -> int:
    return _set_annotation_seen(args, True)


def cmd_mark_unseen(args: argparse.Namespace) -> int:
    return _set_annotation_seen(args, False)


def cmd_items(args: argparse.Namespace) -> int:
    """List reviewable blocks with their item-ids, types, line ranges, and previews."""
    doc = _resolve_doc_arg(args.path)
    with doc_file_lock(doc):
        ann = load_annotations(doc)
        blocks = _select_blocks(
            _reconcile_blocks(ann, doc),
            section=args.section,
            from_line=args.from_line,
            to_line=args.to_line,
        )
        save_annotations(doc, ann)

    if args.json:
        out = [
            {
                "item_id": b.paragraph_id,
                "type": b.type,
                "start_line": (b.source_start_line + 1) if b.source_start_line is not None else None,
                "end_line": b.source_end_line,
                "preview": b.preview or "",
            }
            for b in blocks
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not blocks:
        print("no matching items")
        return 0
    for b in blocks:
        start = (b.source_start_line + 1) if b.source_start_line is not None else "?"
        end = b.source_end_line if b.source_end_line is not None else "?"
        print(f"{b.paragraph_id}  {str(start):>4}-{str(end):<4} {b.type:<18} {b.preview or ''}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Convenience: add an annotation from the CLI (mainly for agent automation)."""
    import uuid

    doc = _resolve_doc_arg(args.path)
    text = args.text
    if text == "-":
        text = sys.stdin.read().rstrip("\n")
    from .server.schemas import Annotation

    new_ann = Annotation(
        id=str(uuid.uuid4()),
        author=args.author,
        type=args.type,
        text=text,
        seen=args.seen,
        ts=utcnow_iso(),
    )

    seeded = {"ok": False}

    def _do(ann):
        blocks = _reconcile_blocks(ann, doc)
        block = _find_block(blocks, args.paragraph_id)
        if not block:
            return
        seeded["ok"] = True
        ann.paragraphs[block.paragraph_id].annotations.append(new_ann)

    mutate_annotations(doc, _do)
    if not seeded["ok"]:
        print(
            f"error: item id {args.paragraph_id} not found in {doc}; run `items` to list valid ids",
            file=sys.stderr,
        )
        return 1
    print(new_ann.id)
    return 0


def cmd_highlight(args: argparse.Namespace) -> int:
    """Mark a selected text span. Highlights are annotations carrying a selected_text."""
    import uuid

    doc = _resolve_doc_arg(args.path)
    note = args.text
    if note == "-":
        note = sys.stdin.read().rstrip("\n")
    from .server.schemas import Annotation

    hl = Annotation(
        id=str(uuid.uuid4()),
        author=args.author,
        type=args.type,
        text=note,
        selected_text=args.selected_text,
        occurrence=args.occurrence,
        seen=False,
        ts=utcnow_iso(),
    )

    seeded = {"ok": False}

    def _do(ann):
        blocks = _reconcile_blocks(ann, doc)
        block = _find_block(blocks, args.paragraph_id)
        if not block:
            return
        seeded["ok"] = True
        ann.paragraphs[block.paragraph_id].annotations.append(hl)

    mutate_annotations(doc, _do)
    if not seeded["ok"]:
        print(
            f"error: item id {args.paragraph_id} not found in {doc}; run `items` to list valid ids",
            file=sys.stderr,
        )
        return 1
    print(hl.id)
    return 0


def _normalize_suggestion_action(action: str) -> str:
    return action.replace("-", "_")


def cmd_suggest(args: argparse.Namespace) -> int:
    """Create a source-change suggestion without modifying the markdown file."""
    import uuid

    doc = _resolve_doc_arg(args.path)
    action = _normalize_suggestion_action(args.action)
    raw = args.raw or ""
    if raw == "-":
        raw = sys.stdin.read().rstrip("\n")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if action not in {"delete", "inline_replace"} and not raw.strip():
        print("error: --raw is required for replace/insert suggestions; pass '-' to read stdin", file=sys.stderr)
        return 2
    if action == "delete":
        raw = ""
    if action == "inline_replace":
        if not args.selected_text.strip():
            print("error: --selected-text is required for inline-replace suggestions", file=sys.stderr)
            return 2
        if args.occurrence < 0:
            print("error: --occurrence must be >= 0", file=sys.stderr)
            return 2

    seeded = {"ok": False}
    created: dict[str, Suggestion | None] = {"suggestion": None}
    hunk_missing = {"bad": False}

    def _do(ann):
        blocks = _reconcile_blocks(ann, doc)
        block = _find_block(blocks, args.paragraph_id)
        if not block:
            return
        try:
            hunk = build_suggestion_hunk(ann, block, action, raw, args.selected_text, args.occurrence)
        except ValueError:
            hunk_missing["bad"] = True
            return
        suggestion = Suggestion(
            id=str(uuid.uuid4()),
            author=args.author,
            action=action,
            anchor_id=block.paragraph_id,
            raw=raw,
            selected_text=args.selected_text,
            occurrence=args.occurrence,
            note=args.note,
            status="open",
            hunks=[hunk],
            ts=utcnow_iso(),
        )
        seeded["ok"] = True
        created["suggestion"] = suggestion
        ann.paragraphs[block.paragraph_id].suggestions.append(suggestion)

    mutate_annotations(doc, _do)
    if hunk_missing["bad"]:
        print("error: selected text not found in item source", file=sys.stderr)
        return 1
    if not seeded["ok"]:
        print(
            f"error: item id {args.paragraph_id} not found in {doc}; run `items` to list valid ids",
            file=sys.stderr,
        )
        return 1
    print(created["suggestion"].id)
    return 0


def cmd_suggestions(args: argparse.Namespace) -> int:
    """List source-change suggestions in a doc."""
    doc = _resolve_doc_arg(args.path)
    ann = load_annotations(doc)
    rows = []
    for pid, para in ann.paragraphs.items():
        for suggestion in para.suggestions:
            if args.all or suggestion.status == "open":
                rows.append((pid, para.preview, suggestion))

    if args.json:
        out = [
            {
                "paragraph_id": pid,
                "suggestion_id": suggestion.id,
                "author": suggestion.author,
                "action": suggestion.action,
                "status": suggestion.status,
                "note": suggestion.note,
                "raw": suggestion.raw,
                "selected_text": suggestion.selected_text,
                "occurrence": suggestion.occurrence,
                "hunks": [h.model_dump(mode="json") for h in suggestion.hunks],
                "conflict": suggestion.conflict,
                "preview": preview,
            }
            for (pid, preview, suggestion) in rows
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        label = "suggestions" if args.all else "open suggestions"
        print(f"no {label} in {annotations_path_for(doc)}")
        return 0

    print(f"{len(rows)} suggestion(s) in {annotations_path_for(doc)}")
    print()
    for pid, preview, suggestion in rows:
        print(f"[{suggestion.author}] item {pid}  suggestion {suggestion.id}")
        print(f"  action: {suggestion.action}  status: {suggestion.status}")
        if preview:
            print(f"  item: {preview}")
        if suggestion.note:
            print(f"  note: {suggestion.note}")
        if suggestion.conflict:
            print(f"  conflict: {suggestion.conflict}")
        if suggestion.selected_text:
            print(f"  selected: {suggestion.selected_text}  occurrence: {suggestion.occurrence}")
        if suggestion.raw:
            for line in suggestion.raw.splitlines():
                print(f"  + {line}")
        print()
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete an annotation by id."""
    doc = _resolve_doc_arg(args.path)
    outcome: dict[str, str | None] = {"kind": None}

    def _do(ann):
        for para in ann.paragraphs.values():
            for i, a in enumerate(para.annotations):
                if a.id == args.id:
                    para.annotations.pop(i)
                    outcome["kind"] = "annotation"
                    return

    mutate_annotations(doc, _do)
    if outcome["kind"]:
        print(f"deleted {outcome['kind']} {args.id}")
        return 0
    print(f"error: id {args.id} not found in {doc}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="markdown-review", description="Block-based markdown review")
    sub = p.add_subparsers(dest="cmd")

    # default `serve` when first positional looks like a path; we expose both forms.
    p_serve = sub.add_parser("serve", help="Run the review server")
    p_serve.add_argument("path", nargs="?", help="Markdown file to open (optional)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--bib", action="append", help="Additional .bib file for citation tooltips")
    p_serve.add_argument("--no-open", action="store_true", help="Do not open browser")
    p_serve.set_defaults(func=cmd_serve)

    p_new = sub.add_parser("new", help="List unseen annotations in a doc")
    p_new.add_argument("path", help="Markdown file path")
    p_new.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    p_new.set_defaults(func=cmd_new)

    p_seen = sub.add_parser("mark-seen", help="Mark an annotation as seen")
    p_seen.add_argument("annotation_id")
    p_seen.add_argument("--path", required=True, help="Markdown file path")
    p_seen.set_defaults(func=cmd_mark_seen)

    p_unseen = sub.add_parser("mark-unseen", help="Mark an annotation as unseen")
    p_unseen.add_argument("annotation_id")
    p_unseen.add_argument("--path", required=True, help="Markdown file path")
    p_unseen.set_defaults(func=cmd_mark_unseen)

    p_items = sub.add_parser("items", help="List reviewable blocks with their item-ids")
    p_items.add_argument("path", help="Markdown file path")
    p_items.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    p_items.add_argument("--section", help="Only blocks under the heading matching this text")
    p_items.add_argument("--from-line", type=int, dest="from_line", help="Only blocks starting at/after this 1-based line")
    p_items.add_argument("--to-line", type=int, dest="to_line", help="Only blocks starting at/before this 1-based line")
    p_items.set_defaults(func=cmd_items)

    p_add = sub.add_parser("add", help="Add an annotation (for agent automation)")
    p_add.add_argument("--path", required=True)
    p_add.add_argument("--item-id", "--paragraph-id", dest="paragraph_id", metavar="ITEM_ID", required=True)
    p_add.add_argument("--author", default="agent", help="Reviewer name (free-form; e.g. agent, user, alice)")
    p_add.add_argument(
        "--type", choices=["info", "error", "task", "comment"], default="comment",
        help="Annotation category set by the author (agents pick info/error/task)",
    )
    p_add.add_argument("--seen", action="store_true", help="Create the annotation already marked seen")
    p_add.add_argument("--text", required=True, help="Annotation text, or '-' to read from stdin")
    p_add.set_defaults(func=cmd_add)

    p_hl = sub.add_parser("highlight", help="Mark a selected text span (an annotation with selected_text)")
    p_hl.add_argument("--path", required=True)
    p_hl.add_argument("--item-id", "--paragraph-id", dest="paragraph_id", metavar="ITEM_ID", required=True)
    p_hl.add_argument("--selected-text", dest="selected_text", required=True, help="Exact visible text span to mark")
    p_hl.add_argument(
        "--type", choices=["info", "error", "task", "comment"], default="comment",
        help="Annotation category for the highlight",
    )
    p_hl.add_argument("--occurrence", type=int, default=0, help="0-based occurrence within the block")
    p_hl.add_argument("--author", default="agent", help="Reviewer name (free-form; e.g. agent, user, alice)")
    p_hl.add_argument("--text", default="", help="Note attached to the highlight, or '-' for stdin")
    p_hl.set_defaults(func=cmd_highlight)

    p_suggest = sub.add_parser("suggest", help="Create a source-change suggestion")
    p_suggest.add_argument("--path", required=True)
    p_suggest.add_argument("--item-id", "--paragraph-id", dest="paragraph_id", metavar="ITEM_ID", required=True)
    p_suggest.add_argument(
        "--action",
        choices=["replace", "delete", "insert-before", "insert-after", "inline-replace"],
        required=True,
    )
    p_suggest.add_argument("--author", default="agent", help="Reviewer name (free-form; e.g. agent, user, alice)")
    p_suggest.add_argument("--raw", default="", help="Suggested markdown source, or '-' to read from stdin")
    p_suggest.add_argument("--selected-text", default="", help="Selected source text for inline-replace")
    p_suggest.add_argument("--occurrence", type=int, default=0, help="0-based selected text occurrence")
    p_suggest.add_argument("--note", default="", help="Optional note explaining the suggestion")
    p_suggest.set_defaults(func=cmd_suggest)

    p_suggestions = sub.add_parser("suggestions", help="List source-change suggestions")
    p_suggestions.add_argument("path", help="Markdown file path")
    p_suggestions.add_argument("--all", action="store_true", help="Include accepted/rejected suggestions")
    p_suggestions.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    p_suggestions.set_defaults(func=cmd_suggestions)

    p_del = sub.add_parser("delete", help="Delete an annotation or highlight by id")
    p_del.add_argument("id")
    p_del.add_argument("--path", required=True, help="Markdown file path")
    p_del.set_defaults(func=cmd_delete)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Bare-positional shorthand: `markdown-review /path/to/doc.md` → serve that doc.
    commands = {
        "serve",
        "new",
        "mark-seen",
        "mark-unseen",
        "add",
        "items",
        "highlight",
        "suggest",
        "suggestions",
        "delete",
    }
    if argv and not argv[0].startswith("-") and argv[0] not in commands:
        argv = ["serve", *argv]
    elif not argv:
        argv = ["serve"]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
