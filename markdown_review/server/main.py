from __future__ import annotations

import os
import urllib.parse
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .bib import load_citations
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
    UpdateAnnotationBody,
    UpdateSuggestionBody,
    utcnow_iso,
)
from .storage import (
    PathLocks,
    annotations_path_for,
    doc_file_lock,
    find_annotation,
    find_suggestion,
    has_review_data,
    load_annotations,
    reconcile_with_live,
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


def _find_live_block(doc: Path, paragraph_id: str, *, missing_status: int = 404):
    blocks = parse_blocks(doc)
    target = next((b for b in blocks if b.paragraph_id == paragraph_id), None)
    if not target:
        raise HTTPException(status_code=missing_status, detail="item id not found in doc")
    if target.source_start_line is None or target.source_end_line is None:
        raise HTTPException(status_code=400, detail="item source range unavailable")
    return target


def _ensure_record_from_live(ann, doc: Path, paragraph_id: str) -> ParagraphRecord:
    if paragraph_id in ann.paragraphs:
        return ann.paragraphs[paragraph_id]

    target = _find_live_block(doc, paragraph_id)
    rec = ParagraphRecord(id=paragraph_id, preview=target.preview or preview_of(target.raw))
    ann.paragraphs[paragraph_id] = rec
    return rec


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
    target = _find_live_block(doc, paragraph_id, missing_status=missing_status)

    src = doc.read_text(encoding="utf-8")
    src_had_trailing_newline = src.endswith("\n")
    src_lines = src.splitlines()
    src_lines[target.source_start_line : target.source_end_line] = new_raw.split("\n")
    _write_source_lines(doc, src_lines, src_had_trailing_newline)

    new_blocks = parse_blocks(doc)
    new_target = next(
        (b for b in new_blocks if b.source_start_line == target.source_start_line),
        None,
    )
    if not new_target or not new_target.paragraph_id:
        raise HTTPException(status_code=500, detail="edited item could not be re-parsed")

    old_id = paragraph_id
    new_id = new_target.paragraph_id
    rec = ann.paragraphs.pop(
        old_id,
        ParagraphRecord(id=old_id, preview=target.preview or preview_of(target.raw)),
    )
    revision = EditRevision(
        id=str(uuid.uuid4()),
        author=author,
        old_id=old_id,
        new_id=new_id,
        before=target.raw,
        after=new_target.raw,
        ts=utcnow_iso(),
    )
    rec.id = new_id
    rec.preview = new_target.preview or preview_of(new_target.raw)
    rec.edits.append(revision)

    existing = ann.paragraphs.get(new_id)
    if existing and existing is not rec:
        existing.annotations.extend(rec.annotations)
        existing.edits.extend(rec.edits)
        existing.suggestions.extend(rec.suggestions)
        existing.preview = rec.preview
    else:
        ann.paragraphs[new_id] = rec

    return EditItemResponse(old_id=old_id, new_id=new_id, revision=revision)


def _occurrence_start(haystack: str, needle: str, occurrence: int) -> int:
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


def _inline_replace_item_source(doc: Path, ann, suggestion: Suggestion) -> EditItemResponse:
    if not suggestion.selected_text:
        raise HTTPException(status_code=400, detail="selected_text is required for inline suggestions")
    if suggestion.occurrence < 0:
        raise HTTPException(status_code=400, detail="occurrence must be >= 0")

    target = _find_live_block(doc, suggestion.anchor_id, missing_status=409)
    start = _occurrence_start(target.raw, suggestion.selected_text, suggestion.occurrence)
    if start == -1:
        raise HTTPException(status_code=409, detail="selected text no longer matches item source")

    end = start + len(suggestion.selected_text)
    new_raw = target.raw[:start] + suggestion.raw + target.raw[end:]
    if not new_raw.strip():
        raise HTTPException(status_code=400, detail="inline suggestion would make item source empty")
    return _replace_item_source(
        doc,
        ann,
        suggestion.anchor_id,
        new_raw,
        suggestion.author,
        missing_status=409,
    )


def _spaced_insert(src_lines: list[str], index: int, raw: str) -> None:
    new_lines = _normalize_source(raw).split("\n")
    chunk: list[str] = []
    if index > 0 and src_lines[index - 1].strip():
        chunk.append("")
    chunk.extend(new_lines)
    if index < len(src_lines) and src_lines[index].strip():
        chunk.append("")
    src_lines[index:index] = chunk


def _apply_open_suggestion(doc: Path, ann, suggestion_id: str) -> ApplySuggestionResponse:
    found = find_suggestion(ann, suggestion_id)
    if not found:
        raise HTTPException(status_code=404, detail="suggestion not found")

    _rec, suggestion = found
    if suggestion.status != "open":
        raise HTTPException(status_code=400, detail=f"suggestion is already {suggestion.status}")

    if suggestion.action == "replace":
        result = _replace_item_source(
            doc,
            ann,
            suggestion.anchor_id,
            suggestion.raw,
            suggestion.author,
            missing_status=409,
        )
        suggestion.status = "accepted"
        suggestion.applied_ts = utcnow_iso()
        return ApplySuggestionResponse(
            suggestion=suggestion,
            old_id=result.old_id,
            new_id=result.new_id,
            revision=result.revision,
        )

    if suggestion.action == "inline_replace":
        result = _inline_replace_item_source(doc, ann, suggestion)
        suggestion.status = "accepted"
        suggestion.applied_ts = utcnow_iso()
        return ApplySuggestionResponse(
            suggestion=suggestion,
            old_id=result.old_id,
            new_id=result.new_id,
            revision=result.revision,
        )

    target = _find_live_block(doc, suggestion.anchor_id, missing_status=409)
    src = doc.read_text(encoding="utf-8")
    src_had_trailing_newline = src.endswith("\n")
    src_lines = src.splitlines()

    if suggestion.action == "delete":
        src_lines[target.source_start_line : target.source_end_line] = []
        _write_source_lines(doc, src_lines, src_had_trailing_newline)
        suggestion.status = "accepted"
        suggestion.applied_ts = utcnow_iso()
        return ApplySuggestionResponse(suggestion=suggestion, old_id=suggestion.anchor_id)

    if suggestion.action == "insert_before":
        _spaced_insert(src_lines, target.source_start_line, suggestion.raw)
    elif suggestion.action == "insert_after":
        _spaced_insert(src_lines, target.source_end_line, suggestion.raw)
    else:  # pragma: no cover - pydantic rejects invalid actions before this branch
        raise HTTPException(status_code=400, detail="unsupported suggestion action")

    _write_source_lines(doc, src_lines, src_had_trailing_newline)
    suggestion.status = "accepted"
    suggestion.applied_ts = utcnow_iso()
    return ApplySuggestionResponse(suggestion=suggestion, old_id=suggestion.anchor_id)


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
                blocks = parse_blocks(doc)
                ann = load_annotations(doc)

                live_ids = {b.paragraph_id: (b.preview or preview_of(b.raw)) for b in blocks if b.paragraph_id}
                ann = reconcile_with_live(ann, live_ids)

                # Overlay review data onto blocks for the response. We do not
                # seed empty sidecar records just because a doc was viewed.
                for b in blocks:
                    if b.paragraph_id and b.paragraph_id in ann.paragraphs:
                        b.annotations = ann.paragraphs[b.paragraph_id].annotations
                        b.edits = ann.paragraphs[b.paragraph_id].edits
                        b.suggestions = ann.paragraphs[b.paragraph_id].suggestions

                if annotations_path_for(doc).exists() or has_review_data(ann):
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
                rec = _ensure_record_from_live(ann, doc, body.anchor_id)
                suggestion = Suggestion(
                    id=str(uuid.uuid4()),
                    author=body.author,
                    action=body.action,
                    anchor_id=body.anchor_id,
                    raw=raw,
                    selected_text=body.selected_text,
                    occurrence=body.occurrence,
                    note=body.note.strip(),
                    status="open",
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
                result = _apply_open_suggestion(doc, ann, suggestion_id)
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
        """List markdown files under the launch folder (and subfolders) for the dropdown."""
        root = _review_root()
        skip_dirs = {
            ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", ".pytest_cache",
            "_build", "build", "_site", ".quarto", "site",
        }
        files: list[dict[str, str]] = []
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.suffix.lower() not in DOC_SUFFIXES or not p.is_file():
                    continue
                if any(part in skip_dirs or part.startswith(".") for part in p.relative_to(root).parts[:-1]):
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
