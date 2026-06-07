from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


# Author is a free-form reviewer name. Each distinct name is assigned a rotating
# marker color in the UI; there is no fixed set of authors anymore.
Author = str
AnnotationType = Literal["info", "error", "task", "comment"]
SuggestionAction = Literal["replace", "delete", "insert_before", "insert_after", "inline_replace"]
SuggestionStatus = Literal["open", "accepted", "rejected", "needs_resolution"]
SuggestionHunkKind = Literal["replace", "delete", "insert"]
SuggestionHunkPlacement = Literal["inside", "before", "after"]


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


class EditRevision(BaseModel):
    id: str
    author: Author
    old_id: str
    new_id: str
    before: str
    after: str
    item_id: str = ""
    before_revision_id: str = ""
    after_revision_id: str = ""
    start: int = 0
    end: int = 0
    ts: str = Field(default_factory=utcnow_iso)


class SuggestionHunk(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    item_id: str = ""
    base_revision_id: str = ""
    base_item_hash: str = ""
    base_item_text: str = ""
    start: int = 0
    end: int = 0
    old_text: str = ""
    new_text: str = ""
    prefix_context: str = ""
    suffix_context: str = ""
    kind: SuggestionHunkKind = "replace"
    placement: SuggestionHunkPlacement = "inside"


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
    hunks: list[SuggestionHunk] = Field(default_factory=list)
    conflict: str = ""
    ts: str = Field(default_factory=utcnow_iso)
    applied_ts: Optional[str] = None


class ParagraphRecord(BaseModel):
    id: str
    preview: str
    legacy_id: str = ""
    block_type: str = ""
    content_hash: str = ""
    source_start_line: Optional[int] = None
    source_end_line: Optional[int] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    active: bool = True
    annotations: list[Annotation] = Field(default_factory=list)
    edits: list[EditRevision] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)


class OrphanRecord(BaseModel):
    paragraph_id: str
    preview: str
    annotations: list[Annotation] = Field(default_factory=list)
    edits: list[EditRevision] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    removed_at: str = Field(default_factory=utcnow_iso)


class AnnotationsFile(BaseModel):
    schema_version: int = 2
    doc_path: str
    updated_at: str = Field(default_factory=utcnow_iso)
    doc_revision_id: str = ""
    paragraphs: dict[str, ParagraphRecord] = Field(default_factory=dict)
    orphans: list[OrphanRecord] = Field(default_factory=list)


class Block(BaseModel):
    type: str
    raw: str
    paragraph_id: Optional[str] = None
    legacy_id: Optional[str] = None
    preview: Optional[str] = None
    source_start_line: Optional[int] = None
    source_end_line: Optional[int] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
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
