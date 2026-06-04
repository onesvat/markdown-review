# markdown-review demo

This file demonstrates the core features of markdown-review in one place.

## 1. Block-level review

Every paragraph, heading, list item, code block, and table gets its own review item identity. This paragraph has both a user comment and agent highlights.

- The first list item can be annotated as a separate review item.
- The second list item also carries its own review item identity.

## 2. Highlights

This paragraph specifically has **selected words** and `inline code` with highlight notes attached.

## 3. Suggestion flow

This paragraph is a bit weakly written. During review it can be improved with a replace suggestion.

This paragraph has an insert-after suggestion behind it.

This new paragraph was inserted by accepting the suggestion above.

## 4. Code and tables

```python
def normalize(text: str) -> str:
    return " ".join(text.split())
```

| Feature | Status |
|---|---|
| Annotation | Yes |
| Highlight | Yes |
| Suggestion | Yes |

## 5. Math

A simple inline formula: $a^2 + b^2 = c^2$. This line was updated as an accepted suggestion and version history example.

This paragraph was edited externally so its old comment moved to the orphan panel.
