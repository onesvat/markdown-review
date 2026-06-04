# markdown-review

Block-based Markdown review tool. It renders any Markdown file in a local web UI and stores review data next to the document as `<doc>.annotations.json`.

The tool is item-based, not paragraph-only. Review items include paragraphs, headings, list items, code blocks, tables, figures, equations, and other rendered blocks. Top-level bullet/ordered lists are split into separate list-item review items.

## Install

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
```

`uv sync` also works if you prefer uv.

## Use

```bash
# Open a doc in the browser
markdown-review /abs/path/to/doc.md

# Or run the server without opening a browser
markdown-review serve --port 8765 --no-open

# List reviewable items with ids, line ranges, types, and previews
markdown-review items /abs/path/to/doc.md --json

# List unseen annotations
markdown-review new /abs/path/to/doc.md --json

# Mark an annotation as seen or unseen
markdown-review mark-seen <annotation_id> --path /abs/path/to/doc.md
markdown-review mark-unseen <annotation_id> --path /abs/path/to/doc.md

# Add an agent annotation
markdown-review add \
  --path /abs/path/to/doc.md \
  --item-id <item_id> \
  --author agent \
  --type comment \
  --text "Reply or note"

# Highlight selected rendered text
markdown-review highlight \
  --path /abs/path/to/doc.md \
  --item-id <item_id> \
  --selected-text "exact visible text" \
  --style underline \
  --text "Gloss or note"

# Suggest a source change without editing the Markdown file
markdown-review suggest \
  --path /abs/path/to/doc.md \
  --item-id <item_id> \
  --action replace \
  --raw - \
  --note "Why this change helps"

# Suggest replacing only one selected text occurrence
markdown-review suggest \
  --path /abs/path/to/doc.md \
  --item-id <item_id> \
  --action inline-replace \
  --selected-text "old phrase" \
  --occurrence 0 \
  --raw "new phrase"

# List open suggestions
markdown-review suggestions /abs/path/to/doc.md --json
```

## Review Data

The sidecar file is JSON:

```jsonc
{
  "schema_version": 1,
  "doc_path": "/abs/path/to/doc.md",
  "updated_at": "2026-06-04T12:00:00Z",
  "paragraphs": {
    "<item_id>": {
      "id": "<item_id>",
      "preview": "first ~120 chars",
      "annotations": [
        {
          "id": "<uuid>",
          "author": "user|agent",
          "type": "info|error|task|comment",
          "text": "...",
          "seen": false,
          "ts": "..."
        }
      ],
      "highlights": [
        {
          "id": "<uuid>",
          "author": "user|agent",
          "style": "marker|underline",
          "selected_text": "...",
          "occurrence": 0,
          "text": "hover comment",
          "ts": "..."
        }
      ],
      "suggestions": [
        {
          "id": "<uuid>",
          "author": "user|agent",
          "action": "replace|delete|insert_before|insert_after|inline_replace",
          "anchor_id": "<item_id>",
          "raw": "suggested markdown",
          "selected_text": "selected text for inline_replace",
          "occurrence": 0,
          "note": "...",
          "status": "open|accepted|rejected",
          "ts": "...",
          "applied_ts": null
        }
      ],
      "edits": [
        {
          "id": "<uuid>",
          "author": "user|agent",
          "old_id": "<previous item id>",
          "new_id": "<new item id>",
          "before": "old source",
          "after": "new source",
          "ts": "..."
        }
      ]
    }
  },
  "orphans": []
}
```

`paragraph_id` is kept as an API/JSON field name for compatibility, but it now means review item ID.

## Suggestions

Suggestions are proposed source changes. They do not edit the Markdown file until accepted.

- `replace`: replace the current review item source.
- `delete`: delete the current review item source.
- `insert_before`: insert Markdown before the current item.
- `insert_after`: insert Markdown after the current item.
- `inline_replace`: replace only the selected text occurrence inside the current item.

Accepting a suggestion applies it to the file and marks it `accepted`. Rejecting marks it `rejected`. There is no conflict resolver: if the anchor item no longer exists, apply fails and the file is left unchanged.

Inline suggestions are shown directly on the rendered text, similar to highlights. Click the inline old/new marker to accept or reject it. They are intentionally simple: the selected rendered text must still be found in the item's Markdown source at apply time. If Markdown syntax makes that ambiguous or the text changed outside the tool, apply fails with a stale suggestion error.

## API

| Method | Path | Body |
|---|---|---|
| `GET` | `/api/doc?path=<abs>` | live parse + review overlay |
| `GET` | `/api/annotations?path=<abs>` | raw sidecar JSON |
| `POST` | `/api/annotations` | `{path, paragraph_id, text, author, type?, seen?}` |
| `PATCH` | `/api/annotations/{id}` | `{path, seen?, text?, type?}` |
| `DELETE` | `/api/annotations/{id}?path=<abs>` | - |
| `POST` | `/api/highlights` | `{path, paragraph_id, selected_text, occurrence, text, author, style}` |
| `DELETE` | `/api/highlights/{id}?path=<abs>` | - |
| `POST` | `/api/suggestions` | `{path, anchor_id, action, author, raw?, note?}` |
| `PATCH` | `/api/suggestions/{id}` | `{path, status?, note?}` |
| `POST` | `/api/suggestions/{id}/apply` | `{path}` |
| `PATCH` | `/api/items/source` | legacy direct source edit endpoint |
| `GET` | `/figures?src=<rel>&doc=<abs>` | serve figure relative to doc dir |

## Notes

- Item IDs are content-derived. Replacing source creates a new item ID; attached review data is migrated to the new ID.
- Annotations only track `seen: true|false`; their type remains `info|error|task|comment`.
- Old annotation `status` values are read once and migrated to `seen` when the sidecar is saved.
- If Markdown is edited outside this tool and an item disappears, its review data moves to `orphans`.
- The UI comment form always creates `author=user`. Highlights and source suggestions use the current mode toggle.
