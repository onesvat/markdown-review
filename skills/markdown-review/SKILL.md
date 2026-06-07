---
name: markdown-review
description: Review Markdown documents with the markdown-review CLI and sidecar annotation files. Use this skill whenever the user asks Codex to inspect reviewer comments, respond to annotations, mark items seen or unseen, add highlights, or propose source changes in a Markdown review workflow, especially when files named *.annotations.json are present.
---

# markdown-review

Use `markdown-review` as the source of truth for item-level Markdown review. It renders a Markdown document, assigns stable item IDs to rendered blocks, and stores review data in a sibling sidecar file named `<doc>.annotations.json`.

The user is reviewing the document, not asking you to edit the sidecar by hand unless there is a clear bulk operation. Prefer the CLI because it handles locking, ID lookup, and schema details.

## Workflow

1. Find the document under review and list unseen annotations:

   ```bash
   markdown-review new <doc.md> --json
   ```

2. Inspect the relevant source context before answering. Use previews only to locate the item.

   ```bash
   markdown-review items <doc.md> --json
   markdown-review items <doc.md> --section "Introduction"
   markdown-review items <doc.md> --from-line 35 --to-line 88
   ```

3. Respond as `agent` when a reply, clarification, verification, or recommendation is useful:

   ```bash
   markdown-review add \
     --path <doc.md> \
     --item-id <item_id> \
     --author agent \
     --type comment \
     --text "Reply in the user's language."
   ```

4. Mark user annotations seen only after addressing them:

   ```bash
   markdown-review mark-seen <annotation_id> --path <doc.md>
   ```

Leave new agent annotations unseen so the user can find them.

## Review Data Model

- Review items may be paragraphs, headings, list items, code blocks, tables, figures, equations, blockquotes, or other rendered blocks.
- The JSON/API field `paragraph_id` means review item ID for compatibility.
- IDs are content-derived. If source changes, the item ID can change.
- Authors are free-form reviewer names. Use `agent` for Codex; humans pick their own name. Each name gets its own rotating marker color in the UI.
- Annotation types are `comment`, `info`, `error`, and `task`.
- Annotations may carry a `selected_text` span; when present they also render inline as a marker over that text (this is what used to be a separate "highlight").
- Annotation state is only `seen: true|false`; do not create new status fields.
- Supported document formats are `.md`, `.markdown`, and `.qmd`.
- Missing items with review data become `orphans` after reconciliation.

## Highlights

A highlight is just an annotation that carries a `selected_text` span: it shows
as a comment card on the item and also renders inline as a marker over the span.
Use it for short selected rendered text, not whole-block comments.

```bash
markdown-review highlight \
  --path <doc.md> \
  --item-id <item_id> \
  --selected-text "exact visible text span" \
  --occurrence 0 \
  --type comment \
  --author agent \
  --text "Hover note"
```

`selected_text` must match visible rendered text. Markdown markers such as `**` are not visible, and source newlines may render as spaces. Use `--occurrence N` when the same visible text appears more than once in the item.

## Suggestions

Use suggestions when the user should review a source change before it is applied. Suggestions do not modify the Markdown until accepted.

```bash
markdown-review suggest \
  --path <doc.md> \
  --item-id <item_id> \
  --author agent \
  --action replace \
  --raw - \
  --note "Why this change helps"
```

Supported actions:

- `replace`: replace the item source.
- `delete`: delete the item source.
- `insert-before`: insert Markdown before the item.
- `insert-after`: insert Markdown after the item.
- `inline-replace`: replace one selected text occurrence inside the item.

Inline suggestion example:

```bash
markdown-review suggest \
  --path <doc.md> \
  --item-id <item_id> \
  --author agent \
  --action inline-replace \
  --selected-text "old phrase" \
  --occurrence 0 \
  --raw "new phrase" \
  --note "Optional reason"
```

Before proposing a change, check whether a direct answer, annotation, or highlight would be less disruptive. Prefer suggestions for wording edits, factual corrections, structural insertions, and deletions that the user should explicitly approve.

## Direct Sidecar Edits

Direct JSON edits are acceptable only for careful bulk work where the CLI cannot express the operation.

- Use uuid4 IDs.
- Anything written by Codex must use `author: agent`.
- Preserve existing annotations, suggestions, edits, and orphans.
- There is no separate `highlights` array. A highlight is an annotation with a `selected_text` (and optional `occurrence`) field; write it inside `annotations`. Legacy `highlights` arrays are migrated to annotations automatically on load.
- Do not delete user annotations. Reply, then mark them seen when addressed.
- Prefer suggestions over direct Markdown edits when the source change should be reviewed.

## Quick Reference

| Need | Command |
|---|---|
| Run UI | `markdown-review <doc.md>` |
| List items | `markdown-review items <doc.md> --json` |
| List unseen annotations | `markdown-review new <doc.md> --json` |
| Add annotation | `markdown-review add --path <doc.md> --item-id <id> --author agent --type info\|error\|task\|comment --text "..."` |
| Mark seen/unseen | `markdown-review mark-seen <id> --path <doc.md>` / `markdown-review mark-unseen <id> --path <doc.md>` |
| Highlight | `markdown-review highlight --path <doc.md> --item-id <id> --selected-text "..." --type info\|error\|task\|comment --text "..."` |
| Suggest source change | `markdown-review suggest --path <doc.md> --item-id <id> --author agent --action replace\|delete\|insert-before\|insert-after\|inline-replace ...` |
| List suggestions | `markdown-review suggestions <doc.md> --json` |
| Delete own note | `markdown-review delete <id> --path <doc.md>` |
