from __future__ import annotations

import os
import difflib
import urllib.parse
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .bib import load_citations
from .patching import build_suggestion_hunk
from .parser import parse_blocks, preview_of
from .schemas import (
    Annotation,
    ApplySuggestionBody,
    ApplySuggestionResponse,
    CreateAnnotationBody,
    CreateSuggestionBody,
    DocResponse,
    EditItemBody,
    EditItemResponse,
    EditRevision,
    ParagraphRecord,
    Suggestion,
    SuggestionHunk,
    UpdateAnnotationBody,
    UpdateSuggestionBody,
    utcnow_iso,
)
from .storage import (
    PathLocks,
    doc_file_lock,
    find_annotation,
    find_suggestion,
    load_annotations,
    reconcile_with_blocks,
    save_annotations,
)


PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
REVIEW_APP_DIR = PACKAGE_ROOT / "review_app"

# Markdown-family documents the review tool will open.
DOC_SUFFIXES = {".md", ".markdown", ".qmd"}


def _review_root() -> Path:
    return Path(os.environ.get("MDR_ROOT") or os.getcwd()).resolve()


def _resolve_doc(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    p = p.resolve()
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {p}")
    if p.suffix.lower() not in DOC_SUFFIXES:
        raise HTTPException(status_code=400, detail="only markdown (.md/.markdown/.qmd) files supported")
    # Sandbox: only documents under the launch folder (and its subfolders) may be
    # opened, so the dropdown and ?path= navigation cannot escape that tree.
    root = _review_root()
    try:
        p.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="document is outside the review folder")
    return p


def _normalize_source(raw: str, *, allow_empty: bool = False) -> str:
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not allow_empty and not normalized.strip():
        raise HTTPException(status_code=400, detail="source cannot be empty")
    return normalized


def _load_reconciled_blocks(doc: Path, ann) -> tuple[str, list]:
    src = doc.read_text(encoding="utf-8")
    blocks = parse_blocks(doc)
    reconcile_with_blocks(ann, blocks, src)
    return src, blocks


def _find_block(blocks: list, item_id: str):
    return next((b for b in blocks if b.paragraph_id == item_id or b.legacy_id == item_id), None)


def _find_live_block(doc: Path, paragraph_id: str, ann=None, *, missing_status: int = 404):
    if ann is None:
        blocks = parse_blocks(doc)
    else:
        _src, blocks = _load_reconciled_blocks(doc, ann)
    target = _find_block(blocks, paragraph_id)
    if not target:
        raise HTTPException(status_code=missing_status, detail="item id not found in doc")
    if target.source_start_line is None or target.source_end_line is None:
        raise HTTPException(status_code=400, detail="item source range unavailable")
    return target


def _ensure_record_from_live(ann, doc: Path, paragraph_id: str) -> ParagraphRecord:
    _src, blocks = _load_reconciled_blocks(doc, ann)
    target = _find_block(blocks, paragraph_id)
    if not target:
        raise HTTPException(status_code=404, detail="item id not found in doc")
    return ann.paragraphs[target.paragraph_id]


def _write_source_lines(doc: Path, src_lines: list[str], src_had_trailing_newline: bool) -> None:
    src = "\n".join(src_lines)
    if src_lines and src_had_trailing_newline:
        src += "\n"
    doc.write_text(src, encoding="utf-8")


def _replace_item_source(
    doc: Path,
    ann,
    paragraph_id: str,
    raw: str,
    author: str,
    *,
    missing_status: int = 404,
) -> EditItemResponse:
    new_raw = _normalize_source(raw)
    src, _blocks = _load_reconciled_blocks(doc, ann)
    target = _find_live_block(doc, paragraph_id, ann, missing_status=missing_status)
    item_id = target.paragraph_id
    before_revision_id = ann.doc_revision_id

    src_had_trailing_newline = src.endswith("\n")
    src_lines = src.splitlines()
    src_lines[target.source_start_line : target.source_end_line] = new_raw.split("\n")
    _write_source_lines(doc, src_lines, src_had_trailing_newline)

    _new_src, new_blocks = _load_reconciled_blocks(doc, ann)
    new_target = _find_block(new_blocks, item_id)
    if not new_target or not new_target.paragraph_id:
        raise HTTPException(status_code=500, detail="edited item could not be re-parsed")

    old_id = item_id
    new_id = item_id
    rec = ann.paragraphs.get(
        item_id,
        ParagraphRecord(id=item_id, preview=target.preview or preview_of(target.raw)),
    )
    revision = EditRevision(
        id=str(uuid.uuid4()),
        author=author,
        old_id=old_id,
        new_id=new_id,
        before=target.raw,
        after=new_target.raw,
        item_id=item_id,
        before_revision_id=before_revision_id,
        after_revision_id=ann.doc_revision_id,
        start=target.source_start or 0,
        end=target.source_end or 0,
        ts=utcnow_iso(),
    )
    rec.preview = new_target.preview or preview_of(new_target.raw)
    rec.edits.append(revision)
    ann.paragraphs[item_id] = rec

    return EditItemResponse(old_id=old_id, new_id=new_id, revision=revision)


def _map_base_point(base: str, current: str, point: int) -> int | None:
    matcher = difflib.SequenceMatcher(a=base, b=current, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if point < a0:
            return b0
        if point <= a1:
            if tag == "equal":
                return b0 + (point - a0)
            if point == a0:
                return b0
            if point == a1:
                return b1
            return None
    return len(current)


def _resolve_by_diff(current: str, hunk: SuggestionHunk) -> tuple[int, int] | None:
    if not hunk.base_item_text:
        return None
    start = _map_base_point(hunk.base_item_text, current, hunk.start)
    end = _map_base_point(hunk.base_item_text, current, hunk.end)
    if start is None or end is None or start > end:
        return None
    if current[start:end] == hunk.old_text:
        return start, end
    return None


def _context_matches(current: str, start: int, end: int, hunk: SuggestionHunk) -> bool:
    if hunk.prefix_context and not current[:start].endswith(hunk.prefix_context):
        return False
    if hunk.suffix_context and not current[end:].startswith(hunk.suffix_context):
        return False
    return True


def _resolve_by_context(current: str, hunk: SuggestionHunk) -> tuple[int, int] | None:
    if not hunk.old_text:
        return None
    matches: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = current.find(hunk.old_text, pos)
        if start == -1:
            break
        end = start + len(hunk.old_text)
        if _context_matches(current, start, end, hunk):
            matches.append((start, end))
        pos = end
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_hunk_in_block(current: str, hunk: SuggestionHunk) -> tuple[int, int]:
    if hunk.kind == "insert":
        if hunk.placement == "before":
            return 0, 0
        if hunk.placement == "after":
            return len(current), len(current)

    if current[hunk.start : hunk.end] == hunk.old_text:
        return hunk.start, hunk.end

    resolved = _resolve_by_diff(current, hunk)
    if resolved is not None:
        return resolved

    resolved = _resolve_by_context(current, hunk)
    if resolved is not None:
        return resolved

    raise HTTPException(status_code=409, detail="suggestion hunk no longer matches item source")


def _spaced_insert_text(src: str, index: int, raw: str) -> str:
    chunk = _normalize_source(raw)
    prefix = ""
    suffix = ""
    if index > 0 and not src[:index].endswith("\n\n"):
        prefix = "\n\n"
    if index < len(src) and not src[index:].startswith("\n\n"):
        suffix = "\n\n"
    return prefix + chunk + suffix


def _conflict(suggestion: Suggestion, detail: str) -> None:
    suggestion.status = "needs_resolution"
    suggestion.conflict = detail
    raise HTTPException(status_code=409, detail=detail)


def _replacement_ops_for_suggestion(src: str, blocks: list, suggestion: Suggestion):
    ops = []
    for hunk in suggestion.hunks:
        target = _find_block(blocks, hunk.item_id)
        if not target or target.source_start is None or target.source_end is None:
            _conflict(suggestion, "suggestion anchor item is no longer present")

        if hunk.kind == "insert":
            if hunk.placement == "before":
                global_start = target.source_start
            elif hunk.placement == "after":
                global_start = target.source_end
            else:
                item_start, _item_end = _resolve_hunk_in_block(target.raw, hunk)
                global_start = target.source_start + item_start
            replacement = _spaced_insert_text(src, global_start, hunk.new_text)
            ops.append((global_start, global_start, replacement, target, hunk))
            continue

        try:
            item_start, item_end = _resolve_hunk_in_block(target.raw, hunk)
        except HTTPException as err:
            _conflict(suggestion, str(err.detail))
        global_start = target.source_start + item_start
        global_end = target.source_start + item_end
        ops.append((global_start, global_end, hunk.new_text, target, hunk))

    sorted_ops = sorted(ops, key=lambda op: (op[0], op[1]))
    previous_end = -1
    for start, end, _replacement, _target, _hunk in sorted_ops:
        if start < previous_end:
            _conflict(suggestion, "suggestion hunks overlap")
        previous_end = max(previous_end, end)
    return sorted(ops, key=lambda op: op[0], reverse=True)


def _append_revision(ann, item_id: str, revision: EditRevision) -> None:
    if item_id in ann.paragraphs:
        ann.paragraphs[item_id].edits.append(revision)
        return
    for orphan in reversed(ann.orphans):
        if orphan.paragraph_id == item_id:
            orphan.edits.append(revision)
            return


def _apply_open_suggestion(doc: Path, ann, suggestion_id: str) -> ApplySuggestionResponse:
    src, blocks = _load_reconciled_blocks(doc, ann)
    found = find_suggestion(ann, suggestion_id)
    if not found:
        raise HTTPException(status_code=404, detail="suggestion not found")

    _rec, suggestion = found
    if suggestion.status != "open":
        raise HTTPException(status_code=400, detail=f"suggestion is already {suggestion.status}")
    if not suggestion.hunks:
        _conflict(suggestion, "suggestion has no patch hunks")

    ops = _replacement_ops_for_suggestion(src, blocks, suggestion)
    primary_hunk = suggestion.hunks[0]
    primary_target = _find_block(blocks, primary_hunk.item_id)
    before_text = primary_target.raw if primary_target else ""
    before_revision_id = ann.doc_revision_id

    next_src = src
    for start, end, replacement, _target, _hunk in ops:
        next_src = next_src[:start] + replacement + next_src[end:]
    if not next_src.strip():
        _conflict(suggestion, "suggestion would make the document empty")

    doc.write_text(next_src, encoding="utf-8")
    suggestion.status = "accepted"
    suggestion.conflict = ""
    suggestion.applied_ts = utcnow_iso()

    _new_src, new_blocks = _load_reconciled_blocks(doc, ann)
    new_target = _find_block(new_blocks, primary_hunk.item_id)
    after_text = new_target.raw if new_target else ""
    revision = EditRevision(
        id=str(uuid.uuid4()),
        author=suggestion.author,
        old_id=primary_hunk.item_id,
        new_id=primary_hunk.item_id if new_target else "",
        before=before_text,
        after=after_text,
        item_id=primary_hunk.item_id,
        before_revision_id=before_revision_id,
        after_revision_id=ann.doc_revision_id,
        start=ops[-1][0] if ops else 0,
        end=ops[-1][1] if ops else 0,
        ts=utcnow_iso(),
    )
    _append_revision(ann, primary_hunk.item_id, revision)
    return ApplySuggestionResponse(
        suggestion=suggestion,
        old_id=primary_hunk.item_id,
        new_id=primary_hunk.item_id if new_target else None,
        revision=revision,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="markdown-review", version="0.1.0")
    locks = PathLocks()

    # ---------------- static UI ----------------

    @app.get("/")
    async def index(request: Request) -> Response:
        # If the server was launched with a default doc and the client did not
        # supply ?path=, redirect to the default doc so the UI loads it directly.
        default_doc = os.environ.get("MDR_DEFAULT_DOC")
        if default_doc and "path" not in request.query_params:
            qs = urllib.parse.urlencode({"path": default_doc})
            return RedirectResponse(url=f"/?{qs}", status_code=302)
        return FileResponse(REVIEW_APP_DIR / "index.html")

    @app.get("/api/default-doc")
    async def get_default_doc() -> dict[str, str | None]:
        return {"path": os.environ.get("MDR_DEFAULT_DOC")}

    app.mount("/static", StaticFiles(directory=str(REVIEW_APP_DIR)), name="static")

    # ---------------- doc + annotations ----------------

    @app.get("/api/doc", response_model=DocResponse)
    async def get_doc(path: str = Query(...)) -> DocResponse:
        doc = _resolve_doc(path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                _src, blocks = _load_reconciled_blocks(doc, ann)

                # Overlay review data onto blocks after assigning stable item IDs.
                for b in blocks:
                    if b.paragraph_id and b.paragraph_id in ann.paragraphs:
                        b.annotations = ann.paragraphs[b.paragraph_id].annotations
                        b.edits = ann.paragraphs[b.paragraph_id].edits
                        b.suggestions = ann.paragraphs[b.paragraph_id].suggestions

                save_annotations(doc, ann)

            return DocResponse(doc_path=str(doc), blocks=blocks, orphans=ann.orphans)

    @app.get("/api/annotations")
    async def get_raw_annotations(path: str = Query(...)) -> JSONResponse:
        doc = _resolve_doc(path)
        ann = load_annotations(doc)
        return JSONResponse(ann.model_dump(mode="json"))

    @app.get("/api/bib")
    async def get_bib(path: str = Query(...)) -> JSONResponse:
        doc = _resolve_doc(path)
        return JSONResponse(load_citations(doc))

    @app.post("/api/annotations", response_model=Annotation)
    async def create_annotation(body: CreateAnnotationBody) -> Annotation:
        doc = _resolve_doc(body.path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                rec = _ensure_record_from_live(ann, doc, body.paragraph_id)
                new_ann = Annotation(
                    id=str(uuid.uuid4()),
                    author=body.author,
                    type=body.type,
                    text=body.text,
                    selected_text=body.selected_text,
                    occurrence=body.occurrence,
                    seen=body.seen,
                    ts=utcnow_iso(),
                )
                rec.annotations.append(new_ann)
                save_annotations(doc, ann)
                return new_ann

    @app.patch("/api/items/source", response_model=EditItemResponse)
    async def edit_item_source(body: EditItemBody) -> EditItemResponse:
        doc = _resolve_doc(body.path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                result = _replace_item_source(doc, ann, body.paragraph_id, body.raw, body.author)
                save_annotations(doc, ann)
                return result

    @app.post("/api/suggestions", response_model=Suggestion)
    async def create_suggestion(body: CreateSuggestionBody) -> Suggestion:
        doc = _resolve_doc(body.path)
        if body.action == "delete":
            raw = ""
        elif body.action == "inline_replace":
            raw = _normalize_source(body.raw, allow_empty=True)
            if not body.selected_text.strip():
                raise HTTPException(status_code=400, detail="selected_text is required for inline suggestions")
            if body.occurrence < 0:
                raise HTTPException(status_code=400, detail="occurrence must be >= 0")
        else:
            raw = _normalize_source(body.raw)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                _src, blocks = _load_reconciled_blocks(doc, ann)
                target = _find_block(blocks, body.anchor_id)
                if not target:
                    raise HTTPException(status_code=404, detail="item id not found in doc")
                rec = ann.paragraphs[target.paragraph_id]
                try:
                    hunk = build_suggestion_hunk(
                        ann,
                        target,
                        body.action,
                        raw,
                        body.selected_text,
                        body.occurrence,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                suggestion = Suggestion(
                    id=str(uuid.uuid4()),
                    author=body.author,
                    action=body.action,
                    anchor_id=target.paragraph_id,
                    raw=raw,
                    selected_text=body.selected_text,
                    occurrence=body.occurrence,
                    note=body.note.strip(),
                    status="open",
                    hunks=[hunk],
                    ts=utcnow_iso(),
                )
                rec.suggestions.append(suggestion)
                save_annotations(doc, ann)
                return suggestion

    @app.patch("/api/suggestions/{suggestion_id}", response_model=Suggestion)
    async def update_suggestion(suggestion_id: str, body: UpdateSuggestionBody) -> Suggestion:
        doc = _resolve_doc(body.path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                found = find_suggestion(ann, suggestion_id)
                if not found:
                    raise HTTPException(status_code=404, detail="suggestion not found")
                _, suggestion = found
                if body.status == "accepted":
                    raise HTTPException(status_code=400, detail="use apply endpoint to accept suggestions")
                if suggestion.status == "accepted" and body.status is not None:
                    raise HTTPException(status_code=400, detail="accepted suggestions cannot change status")
                if body.status is not None:
                    suggestion.status = body.status
                if body.note is not None:
                    suggestion.note = body.note
                save_annotations(doc, ann)
                return suggestion

    @app.post("/api/suggestions/{suggestion_id}/apply", response_model=ApplySuggestionResponse)
    async def apply_suggestion(suggestion_id: str, body: ApplySuggestionBody) -> ApplySuggestionResponse:
        doc = _resolve_doc(body.path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                try:
                    result = _apply_open_suggestion(doc, ann, suggestion_id)
                except HTTPException:
                    save_annotations(doc, ann)
                    raise
                else:
                    save_annotations(doc, ann)
                    return result

    @app.patch("/api/annotations/{annotation_id}", response_model=Annotation)
    async def update_annotation(annotation_id: str, body: UpdateAnnotationBody) -> Annotation:
        doc = _resolve_doc(body.path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                found = find_annotation(ann, annotation_id)
                if not found:
                    raise HTTPException(status_code=404, detail="annotation not found")
                _, target = found
                if body.seen is not None:
                    target.seen = body.seen
                if body.text is not None:
                    target.text = body.text
                if body.type is not None:
                    target.type = body.type
                save_annotations(doc, ann)
                return target

    @app.delete("/api/annotations/{annotation_id}")
    async def delete_annotation(annotation_id: str, path: str = Query(...)) -> dict[str, bool]:
        doc = _resolve_doc(path)
        async with locks.lock(doc):
            with doc_file_lock(doc):
                ann = load_annotations(doc)
                found = find_annotation(ann, annotation_id)
                if not found:
                    raise HTTPException(status_code=404, detail="annotation not found")
                para, _target = found
                para.annotations = [a for a in para.annotations if a.id != annotation_id]
                save_annotations(doc, ann)
                return {"ok": True}

    # ---------------- file listing (for the document dropdown) ----------------

    @app.get("/api/files")
    async def list_files() -> JSONResponse:
        """List markdown files under the launch folder for the dropdown (non-recursive)."""
        root = _review_root()
        files: list[dict[str, str]] = []
        if root.is_dir():
            for p in sorted(root.glob("*")):
                if p.suffix.lower() not in DOC_SUFFIXES or not p.is_file():
                    continue
                files.append({"path": str(p), "name": str(p.relative_to(root))})
        return JSONResponse({"root": str(root), "files": files})

    # ---------------- figures (relative to doc dir) ----------------
    #
    # `src` is a path as written in the markdown source — typically relative to the
    # doc (e.g. "../figures/foo.png"). It is taken as a query parameter rather than
    # a URL path so that browsers do not collapse "../" segments before sending.

    @app.get("/figures")
    async def serve_figure(src: str = Query(...), doc: str = Query(...)) -> FileResponse:
        doc_p = _resolve_doc(doc)
        candidate = (doc_p.parent / src).resolve()
        # Sandbox: must live under the doc's grandparent (project root). Allows
        # ../figures/... but rejects absolute-anywhere reads.
        sandbox = doc_p.parent.parent.resolve()
        try:
            candidate.relative_to(sandbox)
        except ValueError:
            raise HTTPException(status_code=403, detail="figure path outside sandbox")
        if not candidate.exists() or not candidate.is_file():
            raise HTTPException(status_code=404, detail=f"figure not found: {src}")
        return FileResponse(candidate)

    return app


app = create_app()
