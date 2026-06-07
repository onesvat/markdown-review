"""Dependency-free, best-effort BibTeX reader for citation tooltips.

This is not a full BibTeX parser; it extracts the handful of fields the review
UI needs (author, year, title, container) so `{cite:...}` markers can render an
author-year label with a hover tooltip.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_FIELD_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*", re.ASCII)


def _strip_braces(value: str) -> str:
    v = value.strip().strip(",").strip()
    if v and v[0] in "{\"" and v[-1] in "}\"":
        v = v[1:-1]
    # Collapse leftover braces and whitespace runs.
    v = v.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", v).strip()


def _read_value(text: str, i: int) -> tuple[str, int]:
    """Read a field value starting at index i; return (value, next_index)."""
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i >= n:
        return "", i
    ch = text[i]
    if ch == "{":
        depth = 0
        start = i
        while i < n:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1], i + 1
            i += 1
        return text[start:], i
    if ch == '"':
        start = i
        i += 1
        while i < n and text[i] != '"':
            i += 1
        return text[start : i + 1], i + 1
    # bareword / number until comma or closing brace
    start = i
    while i < n and text[i] not in ",}":
        i += 1
    return text[start:i], i


def _parse_entry_body(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(body):
        name = m.group(1).lower()
        if name in fields:
            continue
        raw, _ = _read_value(body, m.end())
        fields[name] = _strip_braces(raw)
    return fields


def parse_bibtex(text: str) -> dict[str, dict[str, str]]:
    """Return {citekey: {author, year, title, container}}."""
    entries: dict[str, dict[str, str]] = {}
    n = len(text)
    i = 0
    while True:
        at = text.find("@", i)
        if at == -1:
            break
        brace = text.find("{", at)
        if brace == -1:
            break
        entry_type = text[at + 1 : brace].strip().lower()
        # find matching close brace for the whole entry
        depth = 0
        j = brace
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = text[brace + 1 : j]
        i = j + 1
        if entry_type in {"comment", "string", "preamble"}:
            continue
        comma = inner.find(",")
        if comma == -1:
            continue
        key = inner[:comma].strip()
        if not key:
            continue
        f = _parse_entry_body(inner[comma + 1 :])
        entries[key] = {
            "author": f.get("author", ""),
            "year": f.get("year", ""),
            "title": f.get("title", ""),
            "container": f.get("journal") or f.get("booktitle") or f.get("publisher") or "",
        }
    return entries


def _bib_paths_for(doc_path: Path) -> list[Path]:
    """Resolve .bib files: myst.yml `bibliography:` entries, then sibling *.bib."""
    doc_dir = doc_path.parent
    found: list[Path] = []
    myst = doc_dir / "myst.yml"
    if myst.exists():
        try:
            text = myst.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for rel in re.findall(r"[^\s'\"]+\.bib", text):
            p = (doc_dir / rel).resolve()
            if p.exists() and p not in found:
                found.append(p)
    for p in sorted(doc_dir.glob("*.bib")):
        if p.resolve() not in found:
            found.append(p.resolve())
    return found


def load_citations(doc_path: Path) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    bib_paths = list(_bib_paths_for(doc_path))
    extra = os.environ.get("MDR_BIB", "")
    if extra:
        for p in extra.split(os.pathsep):
            bp = Path(p)
            if bp.exists() and bp not in bib_paths:
                bib_paths.append(bp)
    for bib in bib_paths:
        try:
            merged.update(parse_bibtex(bib.read_text(encoding="utf-8")))
        except OSError:
            continue
    return merged
