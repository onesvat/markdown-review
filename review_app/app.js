/* markdown-review frontend — vanilla JS */
(() => {
  const state = {
    docPath: null,
    name: localStorage.getItem("mdr.name") || "user",
    pendingHighlight: null,
    data: null,
    bib: {},
  };

  const $ = (sel) => document.querySelector(sel);

  function getQueryParam(name) {
    const u = new URL(window.location.href);
    return u.searchParams.get(name);
  }

  function setName(name) {
    state.name = (name || "").trim() || "user";
    localStorage.setItem("mdr.name", state.name);
  }

  // Distinct hues rotated across reviewer names so each name gets its own marker
  // color. The hue is picked deterministically from the name, so the same name
  // always keeps the same color across sessions and documents.
  const AUTHOR_HUES = [38, 222, 162, 286, 0, 130, 196, 320, 50, 256, 100, 12];

  function hashString(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i += 1) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function colorForName(name) {
    const hue = AUTHOR_HUES[hashString(String(name || "").trim().toLowerCase()) % AUTHOR_HUES.length];
    return {
      marker: `hsl(${hue} 90% 55% / 0.32)`,
      strong: `hsl(${hue} 75% 38%)`,
      bg: `hsl(${hue} 85% 97%)`,
      border: `hsl(${hue} 70% 60%)`,
      fg: `hsl(${hue} 65% 28%)`,
    };
  }

  function applyAuthorColors(el, name) {
    const c = colorForName(name);
    el.style.setProperty("--author-bg", c.bg);
    el.style.setProperty("--author-border", c.border);
    el.style.setProperty("--author-fg", c.fg);
    el.style.setProperty("--author-strong", c.strong);
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  // Render raw markdown text → HTML using marked, with KaTeX auto-rendering after.
  // Handles common MyST directives via a pre-pass before code fences are protected.
  function preprocessColonFence(raw, docPath) {
    // Detect MyST figure directives in both forms:
    //   :::{figure} <src> ... :::
    //   ```{figure} <src> ... ```
    const renderFigure = (src, body) => {
      const captionMatch = body.match(/:caption:\s*(.+?)(?:\n|$)/);
      const caption = captionMatch ? captionMatch[1].trim() : "";
      const widthMatch = body.match(/:width:\s*(.+?)(?:\n|$)/);
      const width = widthMatch ? widthMatch[1].trim() : "";
      const url =
        "/figures?src=" +
        encodeURIComponent(src) +
        "&doc=" +
        encodeURIComponent(docPath);
      const widthAttr = width ? ` style="max-width:${escapeHtml(width)}"` : "";
      return (
        `<figure class="figure">` +
        `<img class="figure-img" src="${url}" alt="${escapeHtml(caption)}"${widthAttr} />` +
        (caption ? `<figcaption class="figure-caption">${escapeHtml(caption)}</figcaption>` : "") +
        `</figure>`
      );
    };
    const parseListTableRows = (body) => {
      const lines = body.split(/\r?\n/);
      const options = {};
      const content = [];
      let seenRows = false;

      for (const line of lines) {
        const option = line.match(/^:([^:]+):\s*(.*)$/);
        if (!seenRows && option) {
          options[option[1].trim()] = option[2].trim();
          continue;
        }
        if (line.trim().startsWith("* -")) seenRows = true;
        if (seenRows || line.trim()) content.push(line);
      }

      const rows = [];
      let row = null;
      let cellIndex = -1;

      for (const line of content) {
        const rowMatch = line.match(/^\s*\*\s+-\s*(.*)$/);
        if (rowMatch) {
          row = [rowMatch[1].trim()];
          rows.push(row);
          cellIndex = 0;
          continue;
        }

        const cellMatch = line.match(/^\s*-\s+(.*)$/);
        if (cellMatch && row) {
          row.push(cellMatch[1].trim());
          cellIndex = row.length - 1;
          continue;
        }

        if (row && cellIndex >= 0 && line.trim()) {
          row[cellIndex] += `${row[cellIndex] ? "\n" : ""}${line.trim()}`;
        }
      }

      return { options, rows };
    };
    const tableCell = (value) =>
      String(value || "")
        .replace(/\r?\n/g, "<br>")
        .replace(/\|/g, "\\|")
        .trim();
    const renderListTable = (title, body) => {
      const { options, rows } = parseListTableRows(body);
      if (!rows.length) return `<pre>${escapeHtml(body)}</pre>`;

      const headerRows = Math.max(0, parseInt(options["header-rows"] || "0", 10) || 0);
      const columnCount = Math.max(...rows.map((row) => row.length));
      const normalizedRows = rows.map((row) => {
        const out = row.slice();
        while (out.length < columnCount) out.push("");
        return out;
      });
      const header =
        headerRows > 0
          ? normalizedRows[0]
          : Array.from({ length: columnCount }, (_v, idx) => `Column ${idx + 1}`);
      const bodyRows = headerRows > 0 ? normalizedRows.slice(headerRows) : normalizedRows;
      const table = [
        `| ${header.map(tableCell).join(" | ")} |`,
        `| ${header.map(() => "---").join(" | ")} |`,
        ...bodyRows.map((row) => `| ${row.map(tableCell).join(" | ")} |`),
      ].join("\n");
      const safeTitle = title.trim() ? `**${title.trim().replace(/\|/g, "\\|")}**\n\n` : "";
      return `${safeTitle}${table}`;
    };
    let out = raw.replace(
      /^:::\{figure\}\s+(\S+)\s*\n([\s\S]*?)\n:::$/gm,
      (_m, src, body) => renderFigure(src, body),
    );
    out = out.replace(
      /^```\{figure\}\s+(\S+)\s*\n([\s\S]*?)\n```$/gm,
      (_m, src, body) => renderFigure(src, body),
    );
    out = out.replace(
      /^:::\{list-table\}\s*([^\n]*)\n([\s\S]*?)\n:::$/gm,
      (_m, title, body) => renderListTable(title, body),
    );
    out = out.replace(
      /^```\{list-table\}\s*([^\n]*)\n([\s\S]*?)\n```$/gm,
      (_m, title, body) => renderListTable(title, body),
    );
    return out;
  }

  function citeSurnames(author) {
    if (!author) return "";
    const authors = author.split(/\s+and\s+/);
    const surname = (a) =>
      a.includes(",") ? a.split(",")[0].trim() : a.trim().split(/\s+/).pop();
    if (authors.length === 1) return surname(authors[0]);
    if (authors.length === 2) return `${surname(authors[0])} & ${surname(authors[1])}`;
    return `${surname(authors[0])} et al.`;
  }

  function citeLabel(key, kind) {
    const e = state.bib && state.bib[key];
    if (!e) return key;
    const names = citeSurnames(e.author) || key;
    const yr = e.year || "";
    if (!yr) return names;
    return kind === "t" ? `${names} (${yr})` : `${names}, ${yr}`;
  }

  function citeTooltip(key) {
    const e = state.bib && state.bib[key];
    if (!e) return key;
    const parts = [];
    const head = [e.author, e.year ? `(${e.year})` : ""].filter(Boolean).join(" ");
    if (head) parts.push(head + ".");
    if (e.title) parts.push(e.title + ".");
    if (e.container) parts.push(e.container + ".");
    return parts.join(" ") || key;
  }

  function preprocessCitations(raw) {
    // Render `{cite:p}`key,...`` and `{cite:t}`key`` as author-year spans with a
    // hover tooltip resolved from the bibliography (state.bib).
    return raw.replace(/\{cite:([pt])\}`([^`]+)`/g, (_m, kind, keys) => {
      const parts = keys.split(/[,;\s]+/).filter(Boolean);
      const spans = parts.map((k) => {
        const label = citeLabel(k, kind);
        const tip = citeTooltip(k);
        return (
          `<span class="cite" tabindex="0" data-tooltip="${escapeHtml(tip)}" ` +
          `title="${escapeHtml(tip)}">${escapeHtml(label)}</span>`
        );
      });
      return kind === "p" ? `(${spans.join("; ")})` : spans.join("; ");
    });
  }

  let markedConfigured = false;
  function configureMarked() {
    if (markedConfigured || !window.marked) return;
    // Disable GFM strikethrough: in technical prose, "~126 h" means "approximately",
    // not a strikethrough opener. We keep tables, autolinks etc. from gfm.
    window.marked.use({
      tokenizer: {
        del() {
          return false;
        },
      },
    });
    markedConfigured = true;
  }

  function renderKatex(expr, display) {
    if (!window.katex) {
      return escapeHtml(display ? `$$${expr}$$` : `$${expr}$`);
    }
    try {
      return window.katex.renderToString(normalizeKatexExpr(expr), {
        displayMode: display,
        throwOnError: false,
        output: "html",
      });
    } catch (e) {
      return (
        `<span class="katex-error" title="${escapeHtml(e.message)}">` +
        escapeHtml(display ? `$$${expr}$$` : `$${expr}$`) +
        `</span>`
      );
    }
  }

  function normalizeKatexExpr(expr) {
    return expr.replace(/\\text\{_\}/g, "\\text{\\_}");
  }

  function escapeSingleTildes(raw) {
    return raw.replace(/(^|[^~])~(?!~)/g, "$1&#126;");
  }

  function rerenderMath(root) {
    if (!window.renderMathInElement) return;
    window.renderMathInElement(root, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      throwOnError: false,
    });
  }

  function fitMath(root) {
    const formulas = root.querySelectorAll(".block-body .katex");
    for (const formula of formulas) {
      formula.style.fontSize = "";
      formula.dataset.fitScale = "1";

      const container =
        formula.closest(".katex-display") ||
        formula.parentElement?.closest("td, th, li, p, .block-body") ||
        formula.closest(".block-body");
      if (!container) continue;

      const available = container.clientWidth - 4;
      if (available <= 0) continue;

      const naturalWidth = formula.scrollWidth || formula.getBoundingClientRect().width;
      if (naturalWidth <= available) continue;

      const scale = Math.max(0.35, Math.min(1, available / naturalWidth));
      formula.style.fontSize = `${scale}em`;
      formula.dataset.fitScale = scale.toFixed(3);
    }
  }

  function renderMarkdown(raw, docPath) {
    configureMarked();
    let s = preprocessColonFence(raw, docPath);
    s = preprocessCitations(s);

    // Pull math regions out BEFORE marked, otherwise underscores, backslashes
    // and braces inside formulas get mangled by markdown syntax. We also need
    // to skip math that lives inside code spans/fences, so protect those first.
    const codeRegions = [];
    const mathRegions = [];

    s = s.replace(/```[\s\S]*?```/g, (m) => {
      codeRegions.push(m);
      return `@@MDRCODE${codeRegions.length - 1}@@`;
    });
    s = s.replace(/`[^`\n]+`/g, (m) => {
      codeRegions.push(m);
      return `@@MDRCODE${codeRegions.length - 1}@@`;
    });

    // Display math: $$ ... $$ (may span lines)
    s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_m, expr) => {
      mathRegions.push({ expr: expr.trim(), display: true });
      return `@@MDRMATH${mathRegions.length - 1}@@`;
    });

    // Inline math: $ ... $ with no whitespace adjacent to the delimiters; not
    // preceded by a backslash, and not followed by a digit (avoid "$5 and $10").
    s = s.replace(
      /(^|[^\\$])\$([^\s$][^\n$]*?[^\s$]|[^\s$])\$(?!\d)/g,
      (_m, pre, expr) => {
        mathRegions.push({ expr, display: false });
        return `${pre}@@MDRMATH${mathRegions.length - 1}@@`;
      },
    );

    // Marked's GFM mode treats paired single tildes as <del>. In this corpus,
    // single tildes usually mean approximation (~126 h), so escape them outside
    // code and math before Markdown tokenization.
    s = escapeSingleTildes(s);

    // Restore code spans/fences so marked formats them normally.
    s = s.replace(
      /@@MDRCODE(\d+)@@/g,
      (_m, idx) => codeRegions[parseInt(idx, 10)],
    );

    let html;
    if (window.marked && typeof window.marked.parse === "function") {
      html = window.marked.parse(s, { gfm: true, breaks: false });
    } else {
      html = `<pre>${escapeHtml(s)}</pre>`;
    }

    // Swap math placeholders with KaTeX-rendered HTML.
    html = html.replace(/@@MDRMATH(\d+)@@/g, (_m, idx) => {
      const region = mathRegions[parseInt(idx, 10)];
      return renderKatex(region.expr, region.display);
    });

    return html;
  }

  function blockTypeLabel(t) {
    return t.replace(/_/g, " ");
  }

  // ISO timestamp → friendly local string, e.g. "7 Haz 2026, 09:57".
  function formatTs(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function countOccurrences(haystack, needle) {
    if (!needle) return 0;
    let count = 0;
    let pos = 0;
    while (true) {
      const idx = haystack.indexOf(needle, pos);
      if (idx === -1) return count;
      count += 1;
      pos = idx + needle.length;
    }
  }

  function findOccurrenceStart(haystack, needle, occurrence) {
    let pos = 0;
    for (let i = 0; i <= occurrence; i += 1) {
      const idx = haystack.indexOf(needle, pos);
      if (idx === -1) return -1;
      if (i === occurrence) return idx;
      pos = idx + needle.length;
    }
    return -1;
  }

  function highlightTooltip(ann) {
    return `${ann.author}\n${ann.text || ""}`;
  }

  function makeHighlightSpan(ann) {
    const mark = document.createElement("mark");
    mark.className = "mdr-highlight marker";
    mark.dataset.annId = ann.id;
    mark.dataset.tooltip = highlightTooltip(ann);
    mark.title = ann.text || "";
    mark.tabIndex = 0;
    const c = colorForName(ann.author);
    mark.style.setProperty("--highlight-color", c.marker);
    mark.style.setProperty("--highlight-strong", c.strong);
    return mark;
  }

  function textNodesForHighlight(root) {
    const nodes = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (parent.closest("script, style")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  }

  function applyHighlight(root, annotation) {
    const selectedText = annotation.selected_text || "";
    if (!selectedText) return;

    const nodes = textNodesForHighlight(root);
    const spans = [];
    let fullText = "";
    for (const node of nodes) {
      const start = fullText.length;
      fullText += node.nodeValue;
      spans.push({ node, start, end: fullText.length });
    }

    const targetStart = findOccurrenceStart(fullText, selectedText, annotation.occurrence || 0);
    if (targetStart === -1) return;
    const targetEnd = targetStart + selectedText.length;

    const intersecting = spans.filter((s) => targetStart < s.end && targetEnd > s.start);
    if (!intersecting.length) return;
    const first = intersecting[0];
    const last = intersecting[intersecting.length - 1];

    // Wrap the whole range in a single <mark>, even when it straddles inline
    // elements (e.g. *italic* → <em>). One element = one continuous band and
    // one hover outline, instead of one <mark> per text node.
    const range = document.createRange();
    range.setStart(first.node, targetStart - first.start);
    range.setEnd(last.node, targetEnd - last.start);
    const mark = makeHighlightSpan(annotation);
    try {
      range.surroundContents(mark);
    } catch (_e) {
      // surroundContents throws if the range partially selects a non-Text node;
      // extractContents splits the boundaries cleanly and always succeeds.
      mark.appendChild(range.extractContents());
      range.insertNode(mark);
    }
  }

  // Annotations that carry a selected_text span also render inline as a marker
  // over that span (the former "highlight" behavior).
  function applyHighlights(root, annotations) {
    for (const ann of annotations || []) {
      if (ann.selected_text) applyHighlight(root, ann);
    }
  }

  function makeInlineSuggestionSpan(suggestion, paragraphId) {
    const wrap = document.createElement("span");
    wrap.className = `mdr-inline-suggestion ${suggestion.author || ""}`;
    wrap.dataset.suggestionId = suggestion.id;
    wrap.dataset.paragraphId = paragraphId;
    wrap.dataset.author = suggestion.author || "";
    wrap.dataset.replacement = suggestion.raw || "";
    wrap.dataset.note = suggestion.note || "";
    wrap.dataset.selectedText = suggestion.selected_text || "";
    wrap.tabIndex = 0;

    const oldText = document.createElement("span");
    oldText.className = "inline-suggestion-old";
    const arrow = document.createElement("span");
    arrow.className = "inline-suggestion-arrow";
    arrow.textContent = " -> ";
    const replacement = document.createElement("span");
    replacement.className = "inline-suggestion-new";
    replacement.textContent = suggestion.raw || "";

    wrap.appendChild(oldText);
    wrap.appendChild(arrow);
    wrap.appendChild(replacement);
    wrap.addEventListener("click", (e) => {
      e.stopPropagation();
      showInlineSuggestionPopover(wrap);
    });
    wrap.addEventListener("keydown", (e) => {
      if (!["Enter", " "].includes(e.key)) return;
      e.preventDefault();
      showInlineSuggestionPopover(wrap);
    });
    return { wrap, oldText };
  }

  function applyInlineSuggestion(root, suggestion, paragraphId) {
    if (suggestion.action !== "inline_replace" || suggestion.status !== "open") return false;
    const selectedText = suggestion.selected_text || "";
    if (!selectedText) return false;

    const nodes = textNodesForHighlight(root);
    const spans = [];
    let fullText = "";
    for (const node of nodes) {
      const start = fullText.length;
      fullText += node.nodeValue;
      spans.push({ node, start, end: fullText.length });
    }

    const targetStart = findOccurrenceStart(fullText, selectedText, suggestion.occurrence || 0);
    if (targetStart === -1) return false;
    const targetEnd = targetStart + selectedText.length;
    const intersecting = spans.filter((s) => targetStart < s.end && targetEnd > s.start);
    if (!intersecting.length) return false;

    const first = intersecting[0];
    const last = intersecting[intersecting.length - 1];
    const range = document.createRange();
    range.setStart(first.node, targetStart - first.start);
    range.setEnd(last.node, targetEnd - last.start);
    const { wrap, oldText } = makeInlineSuggestionSpan(suggestion, paragraphId);
    oldText.appendChild(range.extractContents());
    range.insertNode(wrap);
    return true;
  }

  function applyInlineSuggestions(root, suggestions, paragraphId) {
    for (const suggestion of suggestions || []) {
      applyInlineSuggestion(root, suggestion, paragraphId);
    }
  }

  // Annotation/highlight comment text supports lightweight inline markdown
  // (**bold**, *italic*, `code`, [link](url)) plus literal line breaks. Block
  // constructs are intentionally not supported — comments are short prose.
  function renderAnnotationText(raw) {
    const s = String(raw == null ? "" : raw);
    if (window.marked && typeof window.marked.parseInline === "function") {
      try {
        return window.marked.parseInline(s, { gfm: true, breaks: true });
      } catch (e) {
        /* fall through to escaped text */
      }
    }
    return escapeHtml(s).replace(/\n/g, "<br>");
  }

  const ANNOTATION_TYPES = ["info", "error", "task", "comment"];

  function renderAnnotation(ann, paragraphId) {
    const annType = ANNOTATION_TYPES.includes(ann.type) ? ann.type : "comment";
    const seen = Boolean(ann.seen);
    const wrap = document.createElement("div");
    wrap.className = `annotation ${annType} ${seen ? "seen" : "unseen"}`;
    wrap.dataset.annId = ann.id;
    applyAuthorColors(wrap, ann.author);

    const meta = document.createElement("div");
    meta.className = "annotation-meta";

    const typeSelect = document.createElement("select");
    typeSelect.className = `annotation-type type-${annType}`;
    typeSelect.lang = "en";
    for (const t of ANNOTATION_TYPES) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      if (t === annType) opt.selected = true;
      typeSelect.appendChild(opt);
    }
    typeSelect.addEventListener("change", () => changeType(ann.id, paragraphId, typeSelect.value));
    meta.appendChild(typeSelect);

    const seenBtn = document.createElement("button");
    seenBtn.className = `seen-toggle ${seen ? "seen" : "unseen"}`;
    seenBtn.textContent = seen ? "mark unseen" : "mark seen";
    seenBtn.addEventListener("click", () => setSeen(ann.id, paragraphId, !seen));
    meta.appendChild(seenBtn);

    const rest = document.createElement("span");
    rest.innerHTML =
      `<span class="annotation-author">${escapeHtml(ann.author)}</span>` +
      `<span class="annotation-ts">${escapeHtml(formatTs(ann.ts))}</span>`;
    meta.appendChild(rest);
    wrap.appendChild(meta);

    if (ann.selected_text) {
      const anchor = document.createElement("div");
      anchor.className = "annotation-anchor";
      anchor.textContent = ann.selected_text;
      wrap.appendChild(anchor);
    }

    const text = document.createElement("div");
    text.className = "annotation-text";
    text.innerHTML = renderAnnotationText(ann.text);
    wrap.appendChild(text);

    const actions = document.createElement("div");
    actions.className = "annotation-actions";

    const delBtn = document.createElement("button");
    delBtn.className = "ann-btn";
    delBtn.textContent = "delete";
    delBtn.addEventListener("click", () => deleteAnnotation(ann.id, paragraphId));
    actions.appendChild(delBtn);

    wrap.appendChild(actions);
    return wrap;
  }

  function renderAddAnnotationForm(paragraphId) {
    const wrap = document.createElement("div");
    wrap.className = "add-annotation hidden";

    const ta = document.createElement("textarea");
    ta.placeholder = "Write a comment…";
    wrap.appendChild(ta);

    const row = document.createElement("div");
    row.className = "add-row";

    const typeSelect = document.createElement("select");
    typeSelect.className = "annotation-type";
    typeSelect.lang = "en";
    for (const t of ANNOTATION_TYPES) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      if (t === "comment") opt.selected = true;
      typeSelect.appendChild(opt);
    }
    row.appendChild(typeSelect);

    const cancel = document.createElement("button");
    cancel.textContent = "cancel";
    cancel.addEventListener("click", () => {
      ta.value = "";
      wrap.classList.add("hidden");
      toggleBtn.classList.remove("hidden");
    });
    row.appendChild(cancel);

    const submit = document.createElement("button");
    submit.className = "primary";
    submit.textContent = "add comment";
    const submitAnnotation = async () => {
      if (submit.disabled) return;
      const text = ta.value.trim();
      if (!text) return;
      submit.disabled = true;
      try {
        const created = await createAnnotation(paragraphId, text, state.name, typeSelect.value);
        if (!created) return;
        ta.value = "";
        wrap.classList.add("hidden");
        toggleBtn.classList.remove("hidden");
        await loadDoc({ preserveScroll: true, anchorId: paragraphId });
      } finally {
        submit.disabled = false;
      }
    };
    submit.addEventListener("click", submitAnnotation);
    ta.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      e.preventDefault();
      submitAnnotation();
    });
    row.appendChild(submit);

    wrap.appendChild(row);

    const toggleBtn = document.createElement("button");
    toggleBtn.className = "toggle-add";
    toggleBtn.textContent = "+ comment";
    toggleBtn.addEventListener("click", () => {
      wrap.classList.remove("hidden");
      toggleBtn.classList.add("hidden");
      ta.focus();
    });

    const container = document.createElement("div");
    container.appendChild(toggleBtn);
    container.appendChild(wrap);
    return container;
  }

  function renderEditRevision(revision, index) {
    const details = document.createElement("details");
    details.className = "edit-revision";

    const summary = document.createElement("summary");
    summary.textContent = `${index + 1}. ${revision.author} · ${formatTs(revision.ts)} · ${revision.old_id} → ${revision.new_id}`;
    details.appendChild(summary);

    const grid = document.createElement("div");
    grid.className = "edit-revision-grid";

    const before = document.createElement("div");
    before.innerHTML = `<div class="edit-revision-label">before</div>`;
    const beforePre = document.createElement("pre");
    beforePre.textContent = revision.before || "";
    before.appendChild(beforePre);

    const after = document.createElement("div");
    after.innerHTML = `<div class="edit-revision-label">after</div>`;
    const afterPre = document.createElement("pre");
    afterPre.textContent = revision.after || "";
    after.appendChild(afterPre);

    grid.appendChild(before);
    grid.appendChild(after);
    details.appendChild(grid);
    return details;
  }

  function suggestionActionLabel(action) {
    return String(action || "").replace(/_/g, " ");
  }

  function diffTokens(text) {
    return String(text || "").match(/\s+|[^\s]+/g) || [];
  }

  function renderDiffParts(parts, className) {
    return parts
      .map((part) => {
        if (part.type === "equal" || !part.text.trim()) return escapeHtml(part.text);
        return `<span class="${className}">${escapeHtml(part.text)}</span>`;
      })
      .join("");
  }

  function inlineDiffHtml(beforeText, afterText) {
    const before = diffTokens(beforeText);
    const after = diffTokens(afterText);
    const dp = Array.from({ length: before.length + 1 }, () =>
      Array(after.length + 1).fill(0),
    );

    for (let i = before.length - 1; i >= 0; i -= 1) {
      for (let j = after.length - 1; j >= 0; j -= 1) {
        dp[i][j] =
          before[i] === after[j]
            ? dp[i + 1][j + 1] + 1
            : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }

    const beforeParts = [];
    const afterParts = [];
    let i = 0;
    let j = 0;
    while (i < before.length || j < after.length) {
      if (i < before.length && j < after.length && before[i] === after[j]) {
        beforeParts.push({ type: "equal", text: before[i] });
        afterParts.push({ type: "equal", text: after[j] });
        i += 1;
        j += 1;
      } else if (j >= after.length || (i < before.length && dp[i + 1][j] >= dp[i][j + 1])) {
        beforeParts.push({ type: "delete", text: before[i] });
        i += 1;
      } else {
        afterParts.push({ type: "insert", text: after[j] });
        j += 1;
      }
    }

    return {
      before: renderDiffParts(beforeParts, "diff-delete"),
      after: renderDiffParts(afterParts, "diff-insert"),
    };
  }

  function replaceTextOccurrence(text, selectedText, occurrence, replacement) {
    const start = findOccurrenceStart(text, selectedText, occurrence || 0);
    if (start === -1) return null;
    return text.slice(0, start) + replacement + text.slice(start + selectedText.length);
  }

  function renderSuggestion(suggestion, block) {
    const status = suggestion.status || "open";
    const wrap = document.createElement("div");
    wrap.className = `suggestion ${suggestion.author} ${status}`;
    wrap.dataset.suggestionId = suggestion.id;

    const meta = document.createElement("div");
    meta.className = "suggestion-meta";
    meta.innerHTML =
      `<span class="suggestion-action" lang="en">${escapeHtml(suggestionActionLabel(suggestion.action))}</span>` +
      `<span class="suggestion-status ${escapeHtml(status)}" lang="en">${escapeHtml(status)}</span>` +
      `<span class="annotation-author">${escapeHtml(suggestion.author)}</span>` +
      `<span class="annotation-ts">${escapeHtml(formatTs(suggestion.ts))}</span>`;
    wrap.appendChild(meta);

    if (suggestion.note) {
      const note = document.createElement("div");
      note.className = "suggestion-note";
      note.innerHTML = renderAnnotationText(suggestion.note);
      wrap.appendChild(note);
    }

    if (suggestion.action === "replace" || suggestion.action === "inline_replace") {
      const suggestedRaw =
        suggestion.action === "inline_replace"
          ? replaceTextOccurrence(
              block.raw || "",
              suggestion.selected_text || "",
              suggestion.occurrence || 0,
              suggestion.raw || "",
            )
          : suggestion.raw || "";
      const diff = inlineDiffHtml(block.raw || "", suggestedRaw == null ? suggestion.raw || "" : suggestedRaw);
      const grid = document.createElement("div");
      grid.className = "suggestion-grid";

      if (suggestion.action === "inline_replace") {
        const inlineMeta = document.createElement("div");
        inlineMeta.className = "suggestion-inline-meta";
        inlineMeta.innerHTML =
          `<span>selected:</span> <code>${escapeHtml(suggestion.selected_text || "")}</code>` +
          `<span>occurrence:</span> <code>${escapeHtml(suggestion.occurrence || 0)}</code>`;
        grid.appendChild(inlineMeta);
        if (suggestedRaw == null) {
          const stale = document.createElement("div");
          stale.className = "suggestion-stale";
          stale.textContent = "Selected text is not present in the current source preview.";
          grid.appendChild(stale);
        }
      }

      const before = document.createElement("div");
      before.innerHTML = `<div class="edit-revision-label">current</div>`;
      const beforePre = document.createElement("pre");
      beforePre.className = "suggestion-diff-source";
      beforePre.innerHTML = diff.before;
      before.appendChild(beforePre);

      const after = document.createElement("div");
      after.innerHTML = `<div class="edit-revision-label">suggested</div>`;
      const afterPre = document.createElement("pre");
      afterPre.className = "suggestion-diff-source";
      afterPre.innerHTML = diff.after;
      after.appendChild(afterPre);

      grid.appendChild(before);
      grid.appendChild(after);
      wrap.appendChild(grid);
    } else if (suggestion.action === "delete") {
      const deleted = document.createElement("pre");
      deleted.className = "suggestion-raw";
      deleted.textContent = block.raw || "";
      wrap.appendChild(deleted);
    } else {
      const raw = document.createElement("pre");
      raw.className = "suggestion-raw";
      raw.textContent = suggestion.raw || "";
      wrap.appendChild(raw);
    }

    if (status === "open") {
      const actions = document.createElement("div");
      actions.className = "suggestion-actions";

      const accept = document.createElement("button");
      accept.className = "suggestion-btn primary";
      accept.textContent = "accept";
      accept.addEventListener("click", () => applySuggestion(suggestion.id, block.paragraph_id));
      actions.appendChild(accept);

      const reject = document.createElement("button");
      reject.className = "suggestion-btn";
      reject.textContent = "reject";
      reject.addEventListener("click", () => rejectSuggestion(suggestion.id, block.paragraph_id));
      actions.appendChild(reject);

      wrap.appendChild(actions);
    }

    return wrap;
  }

  function renderSuggestionsPanel(block) {
    const panel = document.createElement("div");
    panel.className = "suggestions-panel";

    const suggestions = (block.suggestions || []).filter((s) => s.action !== "inline_replace");
    if (!suggestions.length) return null;

    const title = document.createElement("div");
    title.className = "suggestions-title";
    title.lang = "en";
    title.textContent = `suggestions (${suggestions.length})`;
    panel.appendChild(title);

    for (const suggestion of suggestions) {
      panel.appendChild(renderSuggestion(suggestion, block));
    }

    return panel;
  }

  function renderSourcePanel(block) {
    const panel = document.createElement("div");
    panel.className = "source-panel";

    const tools = document.createElement("div");
    tools.className = "source-tools";

    const replaceBtn = document.createElement("button");
    replaceBtn.className = "source-btn";
    replaceBtn.textContent = "suggest replace";
    tools.appendChild(replaceBtn);

    const beforeBtn = document.createElement("button");
    beforeBtn.className = "source-btn";
    beforeBtn.textContent = "insert before";
    tools.appendChild(beforeBtn);

    const afterBtn = document.createElement("button");
    afterBtn.className = "source-btn";
    afterBtn.textContent = "insert after";
    tools.appendChild(afterBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "source-btn danger";
    deleteBtn.textContent = "suggest delete";
    tools.appendChild(deleteBtn);

    const versionsBtn = document.createElement("button");
    versionsBtn.className = "source-btn";
    versionsBtn.textContent = `versions (${(block.edits || []).length})`;
    tools.appendChild(versionsBtn);
    panel.appendChild(tools);

    const editor = document.createElement("div");
    editor.className = "source-editor hidden";
    const editorTitle = document.createElement("div");
    editorTitle.className = "source-editor-title";
    editor.appendChild(editorTitle);
    const ta = document.createElement("textarea");
    ta.value = block.raw;
    editor.appendChild(ta);
    const noteTa = document.createElement("textarea");
    noteTa.className = "suggestion-note-input";
    noteTa.placeholder = "Note";
    editor.appendChild(noteTa);

    const editActions = document.createElement("div");
    editActions.className = "source-actions";
    const cancel = document.createElement("button");
    cancel.textContent = "cancel";
    const save = document.createElement("button");
    save.className = "primary";
    save.textContent = `suggest as ${state.name}`;
    editActions.appendChild(cancel);
    editActions.appendChild(save);
    editor.appendChild(editActions);
    panel.appendChild(editor);

    const versions = document.createElement("div");
    versions.className = "source-versions hidden";
    if (!block.edits || block.edits.length === 0) {
      const empty = document.createElement("div");
      empty.className = "source-empty";
      empty.textContent = "no source edits";
      versions.appendChild(empty);
    } else {
      block.edits.forEach((revision, index) => {
        versions.appendChild(renderEditRevision(revision, index));
      });
    }
    panel.appendChild(versions);

    const suggestionsPanel = renderSuggestionsPanel(block);
    if (suggestionsPanel) panel.appendChild(suggestionsPanel);

    let activeAction = "replace";
    const openEditor = (action) => {
      activeAction = action;
      editor.classList.remove("hidden");
      editorTitle.textContent = `suggest ${suggestionActionLabel(action)} as ${state.name}`;
      save.textContent = `suggest as ${state.name}`;
      ta.value = action === "replace" ? block.raw : "";
      noteTa.value = "";
      ta.focus();
    };

    replaceBtn.addEventListener("click", () => openEditor("replace"));
    beforeBtn.addEventListener("click", () => openEditor("insert_before"));
    afterBtn.addEventListener("click", () => openEditor("insert_after"));
    deleteBtn.addEventListener("click", async () => {
      if (deleteBtn.disabled) return;
      if (!confirm("Suggest deleting this item?")) return;
      deleteBtn.disabled = true;
      try {
        const created = await createSuggestion({
          anchorId: block.paragraph_id,
          action: "delete",
          raw: "",
          note: "",
          author: state.name,
        });
        if (!created) return;
        await loadDoc({ preserveScroll: true, anchorId: block.paragraph_id });
      } finally {
        deleteBtn.disabled = false;
      }
    });
    versionsBtn.addEventListener("click", () => {
      versions.classList.toggle("hidden");
    });
    cancel.addEventListener("click", () => {
      ta.value = block.raw;
      noteTa.value = "";
      editor.classList.add("hidden");
    });
    save.addEventListener("click", async () => {
      if (save.disabled) return;
      const raw = ta.value;
      if (!raw.trim()) return;
      save.disabled = true;
      try {
        const created = await createSuggestion({
          anchorId: block.paragraph_id,
          action: activeAction,
          raw,
          note: noteTa.value.trim(),
          author: state.name,
        });
        if (!created) return;
        await loadDoc({
          preserveScroll: true,
          anchorId: block.paragraph_id,
        });
      } finally {
        save.disabled = false;
      }
    });

    return panel;
  }

  function renderAnnotationsPanel(block) {
    const panel = document.createElement("div");
    panel.className = "annotations";

    if (!block.annotations || block.annotations.length === 0) {
      const empty = document.createElement("div");
      empty.className = "annotations-empty";
      empty.textContent = "no annotations";
      panel.appendChild(empty);
    } else {
      for (const ann of block.annotations) {
        panel.appendChild(renderAnnotation(ann, block.paragraph_id));
      }
    }

    panel.appendChild(renderAddAnnotationForm(block.paragraph_id));
    return panel;
  }

  function renderBlock(block, docPath) {
    const card = document.createElement("article");
    card.className = `block ${block.type}`;
    if (block.paragraph_id) card.dataset.paragraphId = block.paragraph_id;

    const header = document.createElement("div");
    header.className = "block-header";
    header.innerHTML =
      `<span class="block-type" lang="en">${escapeHtml(blockTypeLabel(block.type))}</span>` +
      (block.paragraph_id
        ? `<span class="block-id">${escapeHtml(block.paragraph_id)}</span>`
        : "");
    card.appendChild(header);

    const body = document.createElement("div");
    body.className = "block-body";
    if (block.type === "frontmatter") {
      body.innerHTML = `<pre>${escapeHtml(block.raw)}</pre>`;
    } else {
      body.innerHTML = renderMarkdown(block.raw, docPath);
    }
    applyHighlights(body, block.annotations);
    applyInlineSuggestions(body, block.suggestions, block.paragraph_id);
    card.appendChild(body);

    if (block.paragraph_id) {
      card.appendChild(renderSourcePanel(block));
      card.appendChild(renderAnnotationsPanel(block));
    }
    return card;
  }

  function renderOrphans(orphans) {
    const wrap = $("#orphans");
    const list = $("#orphan-list");
    list.innerHTML = "";
    if (!orphans || orphans.length === 0) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    for (const o of orphans) {
      const div = document.createElement("div");
      div.className = "orphan-item";
      div.innerHTML =
        `<div><strong>${escapeHtml(o.paragraph_id)}</strong> — ` +
        `<span>${escapeHtml(o.preview || "")}</span></div>` +
        `<div style="margin-top:6px;font-size:12px;color:#7f1d1d;">` +
        `${(o.annotations || []).length} annotation(s), ` +
        `${(o.highlights || []).length} highlight(s), ` +
        `${(o.suggestions || []).length} suggestion(s), removed at ${escapeHtml(o.removed_at || "")}` +
        `</div>`;
      list.appendChild(div);
    }
  }

  function renderError(message) {
    const content = $("#content");
    content.innerHTML = `<div class="error-box">${escapeHtml(message)}</div>`;
  }

  function closestElement(node) {
    return node && node.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
  }

  function selectionContext() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;

    const range = selection.getRangeAt(0);
    const startBody = closestElement(range.startContainer)?.closest(".block-body");
    const endBody = closestElement(range.endContainer)?.closest(".block-body");
    if (!startBody || startBody !== endBody) return null;

    const block = startBody.closest(".block");
    const paragraphId = block?.dataset.paragraphId;
    const selectedText = selection.toString().trim();
    if (!paragraphId || !selectedText) return null;

    const beforeRange = document.createRange();
    beforeRange.setStart(startBody, 0);
    beforeRange.setEnd(range.startContainer, range.startOffset);
    const beforeText = beforeRange.toString();
    beforeRange.detach();

    return {
      paragraphId,
      selectedText,
      occurrence: countOccurrences(beforeText, selectedText),
      rect: range.getBoundingClientRect(),
    };
  }

  function hideHighlightPopover() {
    const popover = $("#highlight-popover");
    if (popover) popover.classList.add("hidden");
    state.pendingHighlight = null;
  }

  function highlightPopover() {
    let popover = $("#highlight-popover");
    if (popover) return popover;

    popover = document.createElement("div");
    popover.id = "highlight-popover";
    popover.className = "highlight-popover hidden";
    const typeOptions = ANNOTATION_TYPES.map(
      (t) => `<option value="${t}"${t === "comment" ? " selected" : ""}>${t}</option>`,
    ).join("");
    popover.innerHTML =
      `<div class="highlight-popover-meta">` +
      `<span class="highlight-popover-mode"></span>` +
      `<button class="highlight-popover-close" type="button" aria-label="Close">×</button>` +
      `</div>` +
      `<div class="highlight-popover-selection"></div>` +
      `<div class="hp-tabs">` +
      `<button class="hp-tab active" type="button" data-tab="highlight">highlight</button>` +
      `<button class="hp-tab" type="button" data-tab="suggest">suggest change</button>` +
      `</div>` +
      `<div class="hp-panel" data-panel="highlight">` +
      `<select class="highlight-type annotation-type" lang="en">${typeOptions}</select>` +
      `<textarea class="highlight-note" placeholder="Highlight comment"></textarea>` +
      `<div class="highlight-popover-actions">` +
      `<button class="highlight-cancel" type="button">cancel</button>` +
      `<button class="highlight-save" type="button">add highlight</button>` +
      `</div>` +
      `</div>` +
      `<div class="hp-panel hidden" data-panel="suggest">` +
      `<textarea class="inline-replacement" placeholder="Replacement text for selected text"></textarea>` +
      `<textarea class="suggest-note" placeholder="Note (optional)"></textarea>` +
      `<div class="highlight-popover-actions">` +
      `<button class="highlight-cancel" type="button">cancel</button>` +
      `<button class="inline-suggest-save" type="button">suggest change</button>` +
      `</div>` +
      `</div>`;
    document.body.appendChild(popover);

    const showTab = (tab) => {
      popover.querySelectorAll(".hp-tab").forEach((b) => {
        b.classList.toggle("active", b.dataset.tab === tab);
      });
      popover.querySelectorAll(".hp-panel").forEach((p) => {
        p.classList.toggle("hidden", p.dataset.panel !== tab);
      });
    };
    popover.querySelectorAll(".hp-tab").forEach((b) => {
      b.addEventListener("click", () => showTab(b.dataset.tab));
    });

    popover.querySelectorAll(".highlight-popover-close, .highlight-cancel").forEach((b) => {
      b.addEventListener("click", hideHighlightPopover);
    });
    popover.querySelector(".highlight-save").addEventListener("click", () => submitHighlight());
    popover.querySelector(".inline-suggest-save").addEventListener("click", () => submitInlineSuggestion());
    popover.querySelector(".highlight-note").addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      e.preventDefault();
      submitHighlight();
    });
    popover.querySelector(".inline-replacement").addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !e.ctrlKey) return;
      e.preventDefault();
      submitInlineSuggestion();
    });
    return popover;
  }

  function positionHighlightPopover(popover, rect) {
    const margin = 10;
    const width = popover.offsetWidth || 320;
    const height = popover.offsetHeight || 160;
    let left = rect.left + rect.width / 2 - width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - width - margin));
    let top = rect.bottom + margin;
    if (top + height > window.innerHeight - margin) {
      top = Math.max(margin, rect.top - height - margin);
    }
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
  }

  function hideInlineSuggestionPopover() {
    const popover = $("#inline-suggestion-popover");
    if (popover) popover.classList.add("hidden");
  }

  function inlineSuggestionPopover() {
    let popover = $("#inline-suggestion-popover");
    if (popover) return popover;

    popover = document.createElement("div");
    popover.id = "inline-suggestion-popover";
    popover.className = "inline-suggestion-popover hidden";
    popover.innerHTML =
      `<div class="highlight-popover-meta">` +
      `<span class="inline-suggestion-popover-mode"></span>` +
      `<button class="highlight-popover-close" type="button" aria-label="Close">×</button>` +
      `</div>` +
      `<div class="inline-suggestion-popover-row">` +
      `<span>current</span><code class="inline-popover-current"></code>` +
      `</div>` +
      `<div class="inline-suggestion-popover-row">` +
      `<span>suggested</span><code class="inline-popover-replacement"></code>` +
      `</div>` +
      `<div class="inline-popover-note"></div>` +
      `<div class="highlight-popover-actions">` +
      `<button class="inline-suggestion-reject" type="button">reject</button>` +
      `<button class="inline-suggestion-accept" type="button">accept</button>` +
      `</div>`;
    document.body.appendChild(popover);
    popover.querySelector(".highlight-popover-close").addEventListener("click", hideInlineSuggestionPopover);
    popover.querySelector(".inline-suggestion-accept").addEventListener("click", () => {
      const suggestionId = popover.dataset.suggestionId;
      const paragraphId = popover.dataset.paragraphId;
      if (suggestionId && paragraphId) applySuggestion(suggestionId, paragraphId);
    });
    popover.querySelector(".inline-suggestion-reject").addEventListener("click", () => {
      const suggestionId = popover.dataset.suggestionId;
      const paragraphId = popover.dataset.paragraphId;
      if (suggestionId && paragraphId) rejectSuggestion(suggestionId, paragraphId);
    });
    return popover;
  }

  function showInlineSuggestionPopover(target) {
    hideHighlightPopover();
    const popover = inlineSuggestionPopover();
    popover.classList.remove("hidden");
    popover.dataset.suggestionId = target.dataset.suggestionId || "";
    popover.dataset.paragraphId = target.dataset.paragraphId || "";
    popover.querySelector(".inline-suggestion-popover-mode").textContent =
      `${target.dataset.author || "agent"} · inline replace`;
    popover.querySelector(".inline-popover-current").textContent = target.dataset.selectedText || "";
    popover.querySelector(".inline-popover-replacement").textContent = target.dataset.replacement || "";
    const note = popover.querySelector(".inline-popover-note");
    note.textContent = target.dataset.note || "";
    note.classList.toggle("hidden", !target.dataset.note);
    positionHighlightPopover(popover, target.getBoundingClientRect());
  }

  function showHighlightPopoverFromSelection() {
    const ctx = selectionContext();
    if (!ctx) return;

    state.pendingHighlight = ctx;
    const popover = highlightPopover();
    popover.classList.remove("hidden");
    popover.dataset.author = state.name;
    applyAuthorColors(popover, state.name);
    popover.querySelector(".highlight-popover-mode").textContent = state.name;
    popover.querySelector(".highlight-popover-selection").textContent = ctx.selectedText;
    // Reset to the highlight tab each time the selection popover opens.
    popover.querySelectorAll(".hp-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === "highlight"));
    popover.querySelectorAll(".hp-panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== "highlight"));
    popover.querySelector(".highlight-type").value = "comment";
    popover.querySelector(".highlight-note").value = "";
    popover.querySelector(".suggest-note").value = "";
    popover.querySelector(".inline-replacement").value = ctx.selectedText;
    positionHighlightPopover(popover, ctx.rect);
  }

  async function submitHighlight() {
    const popover = highlightPopover();
    const pending = state.pendingHighlight;
    if (!pending) return;
    const text = popover.querySelector(".highlight-note").value.trim();
    if (!text) return;
    const type = popover.querySelector(".highlight-type").value;

    const save = popover.querySelector(".highlight-save");
    save.disabled = true;
    try {
      const created = await createAnnotation(
        pending.paragraphId,
        text,
        state.name,
        type,
        pending.selectedText,
        pending.occurrence,
      );
      if (!created) return;
      window.getSelection()?.removeAllRanges();
      hideHighlightPopover();
      await loadDoc({ preserveScroll: true, anchorId: pending.paragraphId });
    } finally {
      save.disabled = false;
    }
  }

  async function submitInlineSuggestion() {
    const popover = highlightPopover();
    const pending = state.pendingHighlight;
    if (!pending) return;
    const replacement = popover
      .querySelector(".inline-replacement")
      .value.replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
    if (!replacement && !confirm("Suggest deleting the selected text?")) return;

    const save = popover.querySelector(".inline-suggest-save");
    save.disabled = true;
    try {
      const created = await createSuggestion({
        anchorId: pending.paragraphId,
        action: "inline_replace",
        raw: replacement,
        note: popover.querySelector(".suggest-note").value.trim(),
        author: state.name,
        selectedText: pending.selectedText,
        occurrence: pending.occurrence,
      });
      if (!created) return;
      window.getSelection()?.removeAllRanges();
      hideHighlightPopover();
      await loadDoc({ preserveScroll: true, anchorId: pending.paragraphId });
    } finally {
      save.disabled = false;
    }
  }

  function captureScroll(anchorId) {
    const anchor = anchorId
      ? document.querySelector(`[data-paragraph-id="${anchorId}"]`)
      : null;
    return {
      top: window.scrollY,
      anchorId,
      anchorViewportTop: anchor ? anchor.getBoundingClientRect().top : null,
    };
  }

  function restoreScroll(snapshot) {
    if (!snapshot) return;
    requestAnimationFrame(() => {
      if (snapshot.anchorId && snapshot.anchorViewportTop !== null) {
        const anchor =
          document.querySelector(`[data-paragraph-id="${snapshot.anchorId}"]`) ||
          (snapshot.fallbackAnchorId
            ? document.querySelector(`[data-paragraph-id="${snapshot.fallbackAnchorId}"]`)
            : null);
        if (anchor) {
          window.scrollBy(0, anchor.getBoundingClientRect().top - snapshot.anchorViewportTop);
          return;
        }
      }
      window.scrollTo(0, snapshot.top);
    });
  }

  async function loadBib() {
    try {
      const resp = await fetch(`/api/bib?path=${encodeURIComponent(state.docPath)}`);
      state.bib = resp.ok ? await resp.json() : {};
    } catch (_e) {
      state.bib = {};
    }
  }

  async function loadDoc(options = {}) {
    const { preserveScroll = false, anchorId = null, fallbackAnchorId = null } = options;
    const content = $("#content");
    const scrollSnapshot = preserveScroll ? captureScroll(anchorId) : null;
    if (scrollSnapshot) scrollSnapshot.fallbackAnchorId = fallbackAnchorId;
    if (!state.docPath) {
      content.innerHTML =
        `<div class="error-box">No <code>?path=</code> query parameter. ` +
        `Usage: open <code>/?path=/abs/path/to/doc.md</code></div>`;
      return;
    }
    const fileSelect = $("#file-select");
    if (fileSelect && state.docPath) fileSelect.value = state.docPath;

    if (!preserveScroll) {
      content.innerHTML = `<div class="loading">loading…</div>`;
    }
    let resp;
    try {
      resp = await fetch(
        `/api/doc?path=${encodeURIComponent(state.docPath)}`,
      );
    } catch (e) {
      renderError(`Network error: ${e.message}`);
      return;
    }
    if (!resp.ok) {
      const txt = await resp.text();
      renderError(`API error ${resp.status}: ${txt}`);
      return;
    }
    const data = await resp.json();
    state.data = data;
    await loadBib();

    content.replaceChildren();
    for (const block of data.blocks) {
      content.appendChild(renderBlock(block, state.docPath));
    }
    rerenderMath(content);
    fitMath(content);
    renderOrphans(data.orphans);
    restoreScroll(scrollSnapshot);
  }

  async function createAnnotation(paragraphId, text, author, type, selectedText = "", occurrence = 0) {
    const resp = await fetch("/api/annotations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: state.docPath,
        paragraph_id: paragraphId,
        text,
        author,
        type: type || "comment",
        selected_text: selectedText,
        occurrence,
        seen: false,
      }),
    });
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to create annotation: ${txt}`);
      return false;
    }
    return true;
  }

  async function createSuggestion({ anchorId, action, raw, note, author, selectedText = "", occurrence = 0 }) {
    const resp = await fetch("/api/suggestions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: state.docPath,
        anchor_id: anchorId,
        action,
        raw,
        selected_text: selectedText,
        occurrence,
        note,
        author,
      }),
    });
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to create suggestion: ${txt}`);
      return false;
    }
    return true;
  }

  async function applySuggestion(suggestionId, paragraphId) {
    const resp = await fetch(`/api/suggestions/${suggestionId}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.docPath }),
    });
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to apply suggestion: ${txt}`);
      return;
    }
    const result = await resp.json();
    await loadDoc({
      preserveScroll: true,
      anchorId: paragraphId,
      fallbackAnchorId: result.new_id || null,
    });
  }

  async function rejectSuggestion(suggestionId, paragraphId) {
    const resp = await fetch(`/api/suggestions/${suggestionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.docPath, status: "rejected" }),
    });
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to reject suggestion: ${txt}`);
      return;
    }
    await loadDoc({ preserveScroll: true, anchorId: paragraphId });
  }

  async function changeType(annotationId, _paragraphId, type) {
    const resp = await fetch(`/api/annotations/${annotationId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.docPath, type }),
    });
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to update type: ${txt}`);
      return;
    }
    await loadDoc({ preserveScroll: true, anchorId: _paragraphId });
  }

  async function setSeen(annotationId, _paragraphId, seen) {
    const resp = await fetch(`/api/annotations/${annotationId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.docPath, seen }),
    });
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to update: ${txt}`);
      return;
    }
    await loadDoc({ preserveScroll: true, anchorId: _paragraphId });
  }

  async function deleteAnnotation(annotationId, _paragraphId) {
    if (!confirm("Delete this annotation?")) return;
    const resp = await fetch(
      `/api/annotations/${annotationId}?path=${encodeURIComponent(state.docPath)}`,
      { method: "DELETE" },
    );
    if (!resp.ok) {
      const txt = await resp.text();
      alert(`Failed to delete: ${txt}`);
      return;
    }
    await loadDoc({ preserveScroll: true, anchorId: _paragraphId });
  }

  async function loadFiles() {
    const select = $("#file-select");
    if (!select) return;
    let files = [];
    let root = "";
    try {
      const resp = await fetch("/api/files");
      if (resp.ok) {
        const data = await resp.json();
        files = data.files || [];
        root = data.root || "";
      }
    } catch (_e) {
      /* leave the dropdown empty on failure */
    }
    select.innerHTML = "";
    // Ensure the current doc is always selectable even if it lives outside root.
    if (state.docPath && !files.some((f) => f.path === state.docPath)) {
      files = [{ path: state.docPath, name: state.docPath }, ...files];
    }
    if (!files.length) {
      const opt = document.createElement("option");
      opt.textContent = "no markdown files";
      opt.disabled = true;
      select.appendChild(opt);
      return;
    }
    for (const f of files) {
      const opt = document.createElement("option");
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    }
    if (state.docPath) select.value = state.docPath;
    select.title = root ? `Documents under ${root}` : "Pick a document";
  }

  function setNameInput(name) {
    setName(name);
    const input = $("#name-input");
    if (input && input.value !== state.name) input.value = state.name;
  }

  function init() {
    state.docPath = getQueryParam("path");
    setName(state.name);

    const nameInput = $("#name-input");
    if (nameInput) {
      nameInput.value = state.name;
      nameInput.addEventListener("input", () => setName(nameInput.value));
      nameInput.addEventListener("blur", () => setNameInput(nameInput.value));
    }

    const fileSelect = $("#file-select");
    if (fileSelect) {
      fileSelect.addEventListener("change", () => {
        if (!fileSelect.value || fileSelect.value === state.docPath) return;
        window.location.href = `/?path=${encodeURIComponent(fileSelect.value)}`;
      });
    }
    loadFiles();

    document.addEventListener("mouseup", (e) => {
      if (e.target.closest?.("#highlight-popover")) return;
      if (e.target.closest?.("#inline-suggestion-popover")) return;
      if (e.target.closest?.(".mdr-inline-suggestion")) return;
      setTimeout(showHighlightPopoverFromSelection, 0);
    });
    document.addEventListener("keyup", (e) => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
      showHighlightPopoverFromSelection();
    });
    document.addEventListener("mousedown", (e) => {
      if (e.target.closest?.("#highlight-popover")) return;
      if (e.target.closest?.("#inline-suggestion-popover")) return;
      if (e.target.closest?.(".mdr-highlight")) return;
      if (e.target.closest?.(".mdr-inline-suggestion")) return;
      hideHighlightPopover();
      hideInlineSuggestionPopover();
    });
    window.addEventListener("resize", () => fitMath($("#content")));
    $("#reload-btn").addEventListener("click", () => loadDoc());

    loadDoc();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
