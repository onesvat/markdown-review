from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


# Author is a free-form reviewer name. Each distinct name is assigned a rotating
# marker color in the UI; there is no fixed set of authors anymore.
Author = str
AnnotationType = Literal["info", "error", "task", "comment"]
SuggestionAction = Literal["replace", "delete", "insert_before", "insert_after", "inline_replace"]
SuggestionStatus = Literal["open", "accepted", "rejected"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Annotation(BaseModel):
    id: str
    author: Author
    type: AnnotationType = "comment"
    text: str
    # When set, the annotation also renders inline as a marker over this exact
    # visible text span (formerly a separate "highlight" entity).
    selected_text: str = ""
    occurrence: int = 0
    seen: bool = False
    ts: str = Field(default_factory=utcnow_iso)

    @model_validator(mode="before")
    @classmethod
    def migrate_status_to_seen(cls, data):
        if isinstance(data, dict) and "seen" not in data and "status" in data:
            data = dict(data)
            data["seen"] = data.get("status") in {"reviewed", "completed"}
        return data


def _migrate_highlights_to_annotations(data):
    """Fold legacy `highlights` records into `annotations` with selected_text.

    Highlights used to be a separate entity with a marker/underline `style`.
    They are now ordinary annotations that carry a `selected_text` span, so old
    sidecar files are upgraded transparently on load.
    """
    if not isinstance(data, dict) or "highlights" not in data:
        return data
    data = dict(data)
    legacy = data.pop("highlights") or []
    annotations = list(data.get("annotations") or [])
    for h in legacy:
        if not isinstance(h, dict):
            continue
        migrated = {
            "id": h.get("id"),
            "author": h.get("author", "user"),
            "type": "comment",
            "text": h.get("text", ""),
            "selected_text": h.get("selected_text", ""),
            "occurrence": h.get("occurrence", 0),
            "seen": False,
        }
        # Only carry ts when present; otherwise let the default factory stamp it
        # (passing ts=None would fail validation).
        if h.get("ts"):
            migrated["ts"] = h["ts"]
        annotations.append(migrated)
    data["annotations"] = annotations
    return data


class EditRevision(BaseModel):
    id: str
    author: Author
    old_id: str
    new_id: str
    before: str
    after: str
    ts: str = Field(default_factory=utcnow_iso)


class Suggestion(BaseModel):
    id: str
    author: Author
    action: SuggestionAction
    anchor_id: str
    raw: str = ""
    selected_text: str = ""
    occurrence: int = 0
    note: str = ""
    status: SuggestionStatus = "open"
    ts: str = Field(default_factory=utcnow_iso)
    applied_ts: Optional[str] = None


class ParagraphRecord(BaseModel):
    id: str
    preview: str
    annotations: list[Annotation] = Field(default_factory=list)
    edits: list[EditRevision] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_highlights(cls, data):
        return _migrate_highlights_to_annotations(data)


class OrphanRecord(BaseModel):
    paragraph_id: str
    preview: str
    annotations: list[Annotation] = Field(default_factory=list)
    edits: list[EditRevision] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    removed_at: str = Field(default_factory=utcnow_iso)

    @model_validator(mode="before")
    @classmethod
    def _migrate_highlights(cls, data):
        return _migrate_highlights_to_annotations(data)


class AnnotationsFile(BaseModel):
    schema_version: int = 1
    doc_path: str
    updated_at: str = Field(default_factory=utcnow_iso)
    paragraphs: dict[str, ParagraphRecord] = Field(default_factory=dict)
    orphans: list[OrphanRecord] = Field(default_factory=list)


class Block(BaseModel):
    type: str
    raw: str
    paragraph_id: Optional[str] = None
    preview: Optional[str] = None
    source_start_line: Optional[int] = None
    source_end_line: Optional[int] = None
    annotations: list[Annotation] = Field(default_factory=list)
    edits: list[EditRevision] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)


class DocResponse(BaseModel):
    doc_path: str
    blocks: list[Block]
    orphans: list[OrphanRecord]


class CreateAnnotationBody(BaseModel):
    path: str
    paragraph_id: str
    text: str
    author: Author
    type: AnnotationType = "comment"
    selected_text: str = ""
    occurrence: int = 0
    seen: bool = False


class UpdateAnnotationBody(BaseModel):
    path: str
    seen: Optional[bool] = None
    text: Optional[str] = None
    type: Optional[AnnotationType] = None


class EditItemBody(BaseModel):
    path: str
    paragraph_id: str
    raw: str
    author: Author


class EditItemResponse(BaseModel):
    ok: bool = True
    old_id: str
    new_id: str
    revision: EditRevision


class CreateSuggestionBody(BaseModel):
    path: str
    anchor_id: str
    action: SuggestionAction
    author: Author
    raw: str = ""
    selected_text: str = ""
    occurrence: int = 0
    note: str = ""


class UpdateSuggestionBody(BaseModel):
    path: str
    status: Optional[SuggestionStatus] = None
    note: Optional[str] = None


class ApplySuggestionBody(BaseModel):
    path: str


class ApplySuggestionResponse(BaseModel):
    ok: bool = True
    suggestion: Suggestion
    old_id: Optional[str] = None
    new_id: Optional[str] = None
    revision: Optional[EditRevision] = None
