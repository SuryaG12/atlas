"use strict";

// Atlas M3 client. Read-only: it only GETs the index and POSTs questions.

const state = {
  meta: null,
  files: [],
  activePath: null,
  mapLevel: "file",
  mapView: "list",
  workTab: "editor",
  treeRows: [],
  treeDirs: [],
  filterTimer: 0,
  helpTrigger: null,
  sourceDoc: null,
  sourceRange: { start: 0, end: 0 },
  findOpen: false,
  findQuery: "",
  findHits: [],
  findIndex: -1,
};

const $ = (id) => document.getElementById(id);

const HELP_KEY = "atlas.help.seen.v1";
const ANSWER_CACHE_KEY = "atlas.answers.v1";
const ANSWER_CACHE_LIMIT = 20;
// Retrieval depth for Ask/Explain. Five keeps the recall@5 baseline; the
// stream endpoint also accepts a top_k override for comparison runs.
const ASK_TOP_K = 5;

function setButtonLabel(button, text, ariaText) {
  const label = button.querySelector(".tool-label");
  if (label) label.textContent = text;
  const name = ariaText || text;
  button.setAttribute("aria-label", name);
  button.title = name;
}

function formatDuration(ms) {
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function status(message, isError) {
  const el = $("status");
  el.textContent = message;
  el.classList.toggle("error", Boolean(isError));
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (_err) { /* keep status text */ }
    throw new Error(detail);
  }
  return response.json();
}

function parseSseEvent(raw) {
  let event = "message";
  const data = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return { event, data: null };
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch (_err) {
    return { event, data: null };
  }
}

// Ask and Explain share /api/ask/stream. The server sends a meta event (scope,
// retrieval, model), one delta per generated token, then a done payload that is
// identical to /api/ask. Returning the done payload keeps citations, caching,
// and rendering on the same path as the non-streaming fallback.
async function apiStream(path, options, handlers) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (_err) { /* keep status text */ }
    throw new Error(detail);
  }
  if (!response.body) throw new Error("This browser cannot stream responses.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done = null;

  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, "\n");
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const raw = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const parsed = parseSseEvent(raw);
      if (parsed.data) {
        if (parsed.event === "meta" && handlers.onMeta) handlers.onMeta(parsed.data);
        else if (parsed.event === "delta" && handlers.onDelta) {
          handlers.onDelta(parsed.data.text || "");
        } else if (parsed.event === "done") done = parsed.data;
      }
      cut = buffer.indexOf("\n\n");
    }
  }
  if (!done) throw new Error("The stream ended before the answer was complete.");
  return done;
}

// -- header ------------------------------------------------------------

function renderStats(meta) {
  const root = meta.root || "";
  const name = root.split(/[\\/]/).filter(Boolean).pop() || root;
  $("repo-name").textContent = name;
  $("repo-root").textContent = root;
  $("repo-root").title = root;
  $("repo-chip").title = root;
  $("stat-files").textContent = Number(meta.files || 0).toLocaleString("en-US");
  $("stat-symbols").textContent = Number(meta.symbols || 0).toLocaleString("en-US");
  $("stat-edges").textContent = Number(meta.edges || 0).toLocaleString("en-US");
  const pct = Number(meta.resolved_pct || 0);
  $("stat-resolved").textContent = `${pct.toFixed(pct % 1 ? 1 : 0)}%`;
  const resolved = $("stat-resolved");
  resolved.title = `${meta.resolved || 0} of ${meta.internal_edges || 0} internal edges resolved`;
  $("file-count").textContent = `${state.files.length} indexed`;
}

function renderModel(meta) {
  const el = $("model-status");
  if (meta.model) {
    el.textContent = `${meta.model.provider}/${meta.model.model}`;
    el.title = `local model at ${meta.model.endpoint}`;
    el.classList.add("online");
    el.classList.remove("offline");
  } else {
    el.textContent = "no local model";
    el.title = meta.no_model_message || "No local model configured.";
    el.classList.remove("online");
    el.classList.add("offline");
  }
}

// -- tree --------------------------------------------------------------

function buildTree(files) {
  const root = { dirs: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i += 1) {
      const name = parts[i];
      if (!node.dirs.has(name)) node.dirs.set(name, { name, dirs: new Map(), files: [] });
      node = node.dirs.get(name);
    }
    node.files.push(file);
  }
  return root;
}

function renderTree(files) {
  const host = $("tree");
  host.textContent = "";
  const root = buildTree(files);
  const openAll = files.length <= 220;
  const fragment = document.createDocumentFragment();
  renderTreeNode(root, 0, openAll, fragment);
  host.append(fragment);
  // Cache the row list and folder membership once per render. Filtering then
  // touches arrays instead of re-querying the DOM on every keystroke.
  state.treeRows = [...host.querySelectorAll(".tree-file")].map((el) => ({
    el,
    path: el.dataset.path.toLowerCase(),
  }));
  state.treeDirs = [...host.querySelectorAll("#tree details")].reverse().map((el) => ({
    el,
    rows: [...el.querySelectorAll(".tree-file")],
  }));
  syncTreeFilterState();
}

function renderTreeNode(node, depth, openAll, parent) {
  const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const dir of dirs) {
    const details = document.createElement("details");
    details.open = openAll || depth < 1;
    const summary = document.createElement("summary");
    summary.style.paddingLeft = `${7 + depth * 12}px`;
    const name = document.createElement("span");
    name.className = "tree-folder-name mono";
    name.textContent = dir.name;
    summary.append(name);
    details.append(summary);
    renderTreeNode(dir, depth + 1, openAll, details);
    parent.append(details);
  }
  const files = node.files.slice().sort((a, b) => a.path.localeCompare(b.path));
  for (const file of files) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "tree-file";
    row.dataset.path = file.path;
    row.title = file.path;
    row.style.paddingLeft = `${8 + depth * 12}px`;
    const name = document.createElement("span");
    name.className = "fname";
    name.textContent = file.path.split("/").pop();
    const lines = document.createElement("span");
    lines.className = "flines";
    lines.textContent = file.lines ? String(file.lines) : "";
    row.append(name, lines);
    row.addEventListener("click", () => openFile(file.path));
    parent.append(row);
  }
}

// Typing runs at most one filter pass per 60ms, so a large tree cannot stall
// input. The query is normalised once and compared on a cached lowercase path.
function scheduleTreeFilter() {
  window.clearTimeout(state.filterTimer);
  state.filterTimer = window.setTimeout(applyTreeFilter, 60);
}

// The count, clear button, and empty state all derive from the same pass, so
// the number in the header always matches the rows a reader can see.
function syncTreeFilterState(query, visible) {
  const total = state.treeRows.length;
  const active = typeof query === "string" ? query : $("tree-filter").value.trim();
  const shown = typeof visible === "number" ? visible : total;
  $("file-count").textContent = active ? `${shown} / ${total}` : `${total} indexed`;
  const clear = $("tree-filter-clear");
  if (clear) clear.hidden = active === "";
  const empty = $("tree-empty");
  if (!empty) return;
  if (!total) {
    empty.hidden = false;
    $("tree-empty-text").textContent =
      "No indexed files yet. Run the Atlas index for this folder, then reload.";
    return;
  }
  if (active && shown === 0) {
    empty.hidden = false;
    $("tree-empty-text").textContent =
      `Nothing in the tree matches "${active}". Try a shorter path or clear the filter.`;
    return;
  }
  empty.hidden = true;
}

function applyTreeFilter() {
  window.clearTimeout(state.filterTimer);
  const query = $("tree-filter").value.trim().toLowerCase();
  let visible = 0;
  for (const row of state.treeRows) {
    const show = query === "" || row.path.includes(query);
    row.el.hidden = !show;
    if (show) visible += 1;
  }
  for (const dir of state.treeDirs) {
    const hasVisible = dir.rows.some((row) => !row.hidden);
    dir.el.hidden = query !== "" && !hasVisible;
    if (query && hasVisible) dir.el.open = true;
  }
  syncTreeFilterState(query, visible);
}

function clearTreeFilter() {
  $("tree-filter").value = "";
  applyTreeFilter();
  $("tree-filter").focus();
}

function setActiveTreeRow(path) {
  for (const row of document.querySelectorAll(".tree-file.active")) {
    row.classList.remove("active");
  }
  for (const row of document.querySelectorAll('.tree-file[aria-current="true"]')) {
    row.removeAttribute("aria-current");
  }
  const row = state.treeRows.map((item) => item.el)
    .find((item) => item.dataset.path === path);
  if (row) {
    row.classList.add("active");
    row.setAttribute("aria-current", "true");
    row.scrollIntoView({ block: "nearest" });
  }
}

// -- source ------------------------------------------------------------

function scrollSourceToLine(lineNumber) {
  const host = $("source");
  const line = host.querySelector(`.code-line[data-line="${lineNumber}"]`);
  if (!line) return;
  const top = line.getBoundingClientRect().top
    - host.getBoundingClientRect().top
    + host.scrollTop;
  host.scrollTop = Math.max(0, top - host.clientHeight / 3);
}

function appendFindHits(span, text, query, marks) {
  if (!query) {
    span.textContent = text === "" ? " " : text;
    return;
  }
  const hay = text.toLowerCase();
  const needle = query.toLowerCase();
  let cursor = 0;
  let found = false;
  while (cursor <= hay.length) {
    const at = hay.indexOf(needle, cursor);
    if (at === -1) break;
    if (at > cursor) span.append(text.slice(cursor, at));
    const mark = document.createElement("mark");
    mark.className = "find-hit";
    mark.textContent = text.slice(at, at + needle.length);
    span.append(mark);
    marks.push(mark);
    cursor = at + needle.length;
    found = true;
    if (marks.length >= 500) break;
  }
  if (cursor < text.length) span.append(text.slice(cursor));
  if (!found) span.textContent = text === "" ? " " : text;
}

function updateFindCount() {
  const el = $("editor-find-count");
  if (!el) return;
  const total = state.findHits.length;
  const query = state.findQuery.trim();
  el.classList.remove("has-hits", "no-hits");
  if (!query) {
    el.textContent = "0 / 0";
    return;
  }
  if (total === 0) {
    el.textContent = "no matches";
    el.classList.add("no-hits");
    return;
  }
  const index = Math.max(0, state.findIndex);
  el.textContent = `${index + 1} / ${total}`;
  el.classList.add("has-hits");
}

function setCurrentFindHit(index, scroll) {
  if (!state.findHits.length) {
    state.findIndex = -1;
    updateFindCount();
    return;
  }
  const next = ((index % state.findHits.length) + state.findHits.length)
    % state.findHits.length;
  state.findIndex = next;
  for (const mark of state.findHits) mark.classList.remove("current");
  const mark = state.findHits[next];
  mark.classList.add("current");
  updateFindCount();
  if (scroll) {
    const row = mark.closest(".code-line");
    if (row) scrollSourceToLine(Number(row.dataset.line));
  }
}

// Rendering and finding share one pass: the source is rebuilt with the current
// query marked, then the current hit is scrolled into view. That keeps the
// count, the highlights, and the visible line in sync at every step.
function paintSource(options) {
  const host = $("source");
  const data = state.sourceDoc;
  if (!data) return;
  const opts = options || {};
  const { start, end } = state.sourceRange;
  const low = start ? Math.max(1, start) : 0;
  const high = end ? Math.max(end, low) : low;
  const query = state.findQuery;
  const marks = [];

  host.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "code";
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < data.lines.length; i += 1) {
    const number = i + 1;
    const row = document.createElement("div");
    row.className = "code-line";
    row.dataset.line = String(number);
    if (low && number >= low && number <= high) row.classList.add("hl");
    const ln = document.createElement("span");
    ln.className = "ln";
    ln.textContent = String(number);
    const lc = document.createElement("span");
    lc.className = "lc";
    appendFindHits(lc, data.lines[i], query.trim(), marks);
    row.append(ln, lc);
    fragment.append(row);
  }
  wrap.append(fragment);
  host.append(wrap);

  state.findHits = marks;
  if (query.trim() && marks.length) {
    if (state.findIndex < 0 || state.findIndex >= marks.length) state.findIndex = 0;
    setCurrentFindHit(state.findIndex, opts.scrollToCurrent !== false);
  } else {
    state.findIndex = -1;
    updateFindCount();
  }

  if (!query.trim() && low) {
    requestAnimationFrame(() => requestAnimationFrame(() => scrollSourceToLine(low)));
  }
}

function renderSource(data, start, end) {
  state.sourceDoc = data;
  state.sourceRange = { start: start || 0, end: end || 0 };
  if (!state.findOpen) {
    state.findQuery = "";
    state.findHits = [];
    state.findIndex = -1;
  }
  paintSource({ scrollToCurrent: true });
}

function setEditorFindOpen(open) {
  const bar = $("editor-find");
  const toggle = $("editor-find-toggle");
  if (!bar || !toggle) return;
  if (open && !state.activePath) {
    status("open a file first, then use find", true);
    return;
  }
  state.findOpen = open;
  bar.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  if (open) {
    const input = $("editor-find-input");
    input.value = state.findQuery;
    input.focus();
    input.select();
    if (state.findQuery) {
      state.findIndex = 0;
      paintSource({ scrollToCurrent: true });
    }
  } else {
    const input = $("editor-find-input");
    input.value = "";
    state.findQuery = "";
    state.findHits = [];
    state.findIndex = -1;
    updateFindCount();
    paintSource({ scrollToCurrent: false });
    $("source").focus({ preventScroll: true });
  }
}

function applyEditorFind(raw) {
  state.findQuery = String(raw || "");
  state.findIndex = 0;
  paintSource({ scrollToCurrent: true });
}

function stepEditorFind(step) {
  if (!state.findHits.length) {
    if (state.findQuery.trim()) {
      status(`no matches for "${state.findQuery.trim()}" in this file`, true);
    }
    return;
  }
  setCurrentFindHit(state.findIndex + step, true);
}

async function openFile(path, start, end) {
  if (!path) return;
  status(`loading ${path}`);
  try {
    const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
    state.activePath = path;
    setActiveTreeRow(path);
    setWorkTab("editor");
    const explain = $("explain-file");
    explain.hidden = false;
    explain.title = `Ask about ${path} only, using its own code`;
    $("editor-find-toggle").disabled = false;
    $("source-title").textContent = path;
    $("source-title").title = path;
    $("source-meta").textContent = [
      data.language,
      `${data.total_lines} lines`,
      "live from disk",
      data.truncated ? "truncated" : "",
    ].filter(Boolean).join(" / ");
    $("source-meta").title =
      "Read from disk now. The file tree and the top stats come from the last index run.";
    const fileState = $("file-state");
    if (fileState) {
      fileState.textContent = "ready";
      fileState.classList.add("ready");
      fileState.title = "File loaded from disk; Atlas never writes to it";
    }
    renderSource(data, start, end);
    if (start) {
      status(`${path}:${start}${end && end !== start ? `-${end}` : ""}`);
    } else {
      status(path);
    }
  } catch (err) {
    status(err.message, true);
  }
}

// -- markdown ----------------------------------------------------------

function citationMarkup(label) {
  const match = String(label).match(/^(.+):(\d+)(?:-(\d+))?$/);
  if (!match) return null;
  const [, path, start, end] = match;
  return `<a class="cite" href="#" data-path="${escapeHtml(path)}"`
    + ` data-start="${start}" data-end="${end || start}">${escapeHtml(label)}</a>`;
}

function linkCitations(html, citations) {
  if (!citations || citations.length === 0) return html;
  const labels = citations.slice().sort((a, b) => b.length - a.length).map(escapeRegExp);
  const pattern = new RegExp(
    `(?<![A-Za-z0-9_./:-])(${labels.join("|")})`
    + `(?![A-Za-z0-9_/:-])(?!\\.[A-Za-z0-9])`,
    "g",
  );
  return html.replace(pattern, (label) => citationMarkup(label) || escapeHtml(label));
}

function citationLinkFromLabel(label) {
  const match = String(label).match(/^(.+):(\d+)(?:-(\d+))?$/);
  if (!match) return null;
  const [, path, start, end] = match;
  const link = document.createElement("a");
  link.className = "cite";
  link.href = "#";
  link.dataset.path = path;
  link.dataset.start = start;
  link.dataset.end = end || start;
  link.textContent = label;
  return link;
}

// Final pass over the rendered answer. The string-level pass above covers
// paragraphs and inline code, but a model that quotes the cited function in a
// fenced block left the citation as plain text: nothing in a <pre> was ever
// linkified, so a validated citation showed up unclickable. Walking text nodes
// catches both, skips anything already a link, and lets the caller report the
// number of citations a reader can actually click.
function linkifyAnswerCitations(root, labels) {
  if (!labels || labels.length === 0) return;
  const ordered = labels.slice().sort((a, b) => b.length - a.length).map(escapeRegExp);
  const finder = new RegExp(
    `(?<![A-Za-z0-9_./:-])(${ordered.join("|")})(?![A-Za-z0-9_/:-])(?!\\.[A-Za-z0-9])`,
    "g",
  );
  const tester = new RegExp(finder.source);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!tester.test(node.nodeValue || "")) return NodeFilter.FILTER_REJECT;
      let el = node.parentElement;
      while (el && el !== root) {
        if (el.tagName === "A") return NodeFilter.FILTER_REJECT;
        el = el.parentElement;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.nodeValue;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of text.matchAll(finder)) {
      const label = match[1];
      if (match.index > cursor) fragment.append(text.slice(cursor, match.index));
      const link = citationLinkFromLabel(label);
      if (link) fragment.append(link);
      else fragment.append(label);
      cursor = match.index + label.length;
    }
    if (cursor < text.length) fragment.append(text.slice(cursor));
    node.replaceWith(fragment);
  }
}

function inlineMarkup(text, citations) {
  const codes = [];
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, (_match, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>',
  );
  html = linkCitations(html, citations);
  return html.replace(/\u0000(\d+)\u0000/g, (_match, index) => {
    const code = codes[Number(index)];
    return `<code>${linkCitations(code, citations)}</code>`;
  });
}

const BLOCK_START = /^(```|#{1,6}\s|\s*[-*]\s|\s*\d+\.\s|>)/;

function markdown(source, citations) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === "") { index += 1; continue; }

    if (line.startsWith("```")) {
      const buffer = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      out.push(`<pre><code>${escapeHtml(buffer.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inlineMarkup(heading[2], citations)}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*]\s+/, ""));
        index += 1;
      }
      out.push(`<ul>${items.map((item) => `<li>${inlineMarkup(item, citations)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+\.\s+/, ""));
        index += 1;
      }
      out.push(`<ol>${items.map((item) => `<li>${inlineMarkup(item, citations)}</li>`).join("")}</ol>`);
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quoted = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quoted.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      out.push(`<blockquote>${inlineMarkup(quoted.join(" "), citations)}</blockquote>`);
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() !== "" && !BLOCK_START.test(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    out.push(`<p>${inlineMarkup(paragraph.join(" "), citations)}</p>`);
  }
  return out.join("\n");
}

// -- retrieval ---------------------------------------------------------

function ranksText(ranks) {
  if (!ranks) return "";
  return Object.keys(ranks).sort()
    .map((key) => `${key[0]}#${ranks[key]}`)
    .join(" ");
}

function chunkLocation(chunk) {
  return `${chunk.path}:${chunk.start}-${chunk.end}`;
}

function retrievalDetails(retrieval) {
  const wrap = document.createElement("div");
  wrap.className = "chunk-wrap";
  const chunks = (retrieval && retrieval.chunks) || [];
  const sources = (retrieval && retrieval.sources) || {};
  const sourceCounts = ["symbol", "fts", "graph"]
    .map((key) => `${key} ${(sources[key] || []).length}`)
    .join(" / ");
  const summary = document.createElement("div");
  summary.className = "chunk-summary";
  const label = document.createElement("span");
  label.textContent = retrieval ? `${chunks.length} retrieved` : "no retrieval";
  const modes = document.createElement("span");
  modes.textContent = retrieval ? sourceCounts : "ask to populate";
  summary.append(label, modes);
  wrap.append(summary);

  if (chunks.length === 0) {
    const empty = document.createElement("p");
    empty.className = "state-body state-body-dim";
    empty.textContent = retrieval
      ? "No chunks passed the retrieval threshold."
      : "Citations appear here after an answer.";
    wrap.append(empty);
    return wrap;
  }

  const list = document.createElement("ol");
  list.className = "chunk-list";
  chunks.forEach((chunk, position) => {
    const row = document.createElement("li");
    row.className = "chunk-row";
    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = String(position + 1).padStart(2, "0");
    const where = document.createElement("button");
    where.type = "button";
    where.className = "where cite";
    where.dataset.path = chunk.path;
    where.dataset.start = String(chunk.start);
    where.dataset.end = String(chunk.end);
    where.textContent = chunkLocation(chunk);
    where.title = `Open ${chunkLocation(chunk)}`;
    const sym = document.createElement("span");
    sym.className = "sym";
    sym.textContent = chunk.name;
    const ranks = document.createElement("span");
    ranks.className = "ranks";
    ranks.textContent = ranksText(chunk.ranks);
    row.append(idx, where, sym, ranks);
    list.append(row);
  });
  wrap.append(list);
  return wrap;
}

// Citation chunks live in the right detail pane, not under the answer, so the
// answer stays readable and every retrieved path:line is still one click away.
function resetChunks() {
  const host = $("chunks");
  if (!host) return null;
  host.textContent = "";
  const count = $("chunk-count");
  if (count) count.textContent = "0";
  return host;
}

function renderChunks(retrieval) {
  const host = resetChunks();
  if (!host) return;
  const count = $("chunk-count");
  if (count) count.textContent = String(((retrieval && retrieval.chunks) || []).length);
  host.append(retrievalDetails(retrieval));
}

// -- state components --------------------------------------------------

// One block shape for idle, loading, cached, refusal, warn, no-model and error.
// The answer renderers build these instead of bare paragraphs, so no state can
// fall back to the old layout.
function stateBlock(tone, label, body, options) {
  const opts = options || {};
  const el = document.createElement("div");
  el.className = `state state--${tone}`;
  el.setAttribute("role", opts.role || "status");
  const icon = document.createElement("span");
  icon.className = "state-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = opts.icon || "";
  const copy = document.createElement("div");
  copy.className = "state-copy";
  const heading = document.createElement("p");
  heading.className = "state-label";
  heading.textContent = label;
  const text = document.createElement("p");
  text.className = "state-body";
  text.textContent = body;
  copy.append(heading, text);
  el.append(icon, copy);
  return { el, copy, body: text };
}

// -- answers -----------------------------------------------------------

function resetAnswer() {
  const host = $("answer");
  host.textContent = "";
  host.scrollTop = 0;
  host.removeAttribute("aria-busy");
  return host;
}

function scopeChip(payload) {
  const chip = document.createElement("span");
  chip.className = payload.scope ? "mono scope-chip file" : "mono scope-chip repo";
  chip.textContent = payload.scope ? `${payload.scope} only` : "repo · all files";
  chip.title = payload.scope
    ? "This answer used only chunks from the open file"
    : "This answer used chunks from the whole repository";
  return chip;
}

function renderAnswer(payload, timing) {
  const host = resetAnswer();

  if (payload.mode === "answer") {
    const body = document.createElement("div");
    body.className = "answer-body";
    const citations = (payload.citations && payload.citations.valid) || [];
    body.innerHTML = markdown(payload.answer, citations);
    host.append(body);
    linkifyAnswerCitations(body, citations);
    // Report what a reader can click, so the meta count and the visible links
    // cannot drift apart.
    const linkCount = body.querySelectorAll("a.cite").length;

    const invalid = (payload.citations && payload.citations.invalid) || [];
    if (linkCount === 0 || invalid.length) {
      const tone = linkCount === 0 ? "warn" : "warn";
      const block = stateBlock(
        tone,
        "citation check",
        linkCount === 0
          ? "No validated citations in this answer. Treat it as ungrounded until you find the code yourself."
          : "Some citations were removed because they did not match the index.",
        { icon: "!" },
      );
      if (invalid.length) {
        const list = document.createElement("ul");
        list.className = "state-list";
        for (const issue of invalid) {
          const item = document.createElement("li");
          item.textContent = `removed ${issue.citation}: ${issue.reason}`;
          list.append(item);
        }
        block.copy.append(list);
      }
      host.append(block.el);
    }

    const meta = document.createElement("div");
    meta.className = "answer-meta";
    meta.append(scopeChip(payload));
    const model = payload.model ? `${payload.model.provider}/${payload.model.model}` : "local model";
    for (const text of [
      `${(payload.retrieval && payload.retrieval.chunks || []).length} chunks`,
      `${linkCount} citation${linkCount === 1 ? "" : "s"}`,
      model,
    ]) {
      const span = document.createElement("span");
      span.className = "mono";
      span.textContent = text;
      meta.append(span);
    }
    if (timing) {
      const span = document.createElement("span");
      span.className = timing.cached ? "mono cached-chip" : "mono";
      if (timing.cached) {
        span.textContent = "cached";
        span.title = "Reused from this browser session; ask a different question to call the model";
      } else if (timing.ttftMs != null) {
        span.textContent = `${formatDuration(timing.ms)} · first ${formatDuration(timing.ttftMs)}`;
        span.title = "Total time from Ask to rendered answer, then time to the first generated token";
      } else {
        span.textContent = formatDuration(timing.ms);
        span.title = "Time from Ask to rendered answer";
      }
      meta.append(span);
    }
    host.append(meta);
    renderChunks(payload.retrieval);
    return;
  }

  if (payload.mode === "refused") {
    const block = stateBlock(
      "refusal",
      "no grounded answer",
      payload.answer || "I couldn't find that in this repo.",
      { icon: "!" },
    );
    if (payload.reason) {
      const reason = document.createElement("p");
      reason.className = "state-body state-body-dim";
      reason.textContent = payload.reason;
      block.copy.append(reason);
    }
    host.append(block.el);
    if (payload.retrieval) renderChunks(payload.retrieval);
    return;
  }

  if (payload.mode === "no-model") {
    const block = stateBlock(
      "no-model",
      "local model offline",
      payload.message,
      { icon: "-" },
    );
    const hint = document.createElement("p");
    hint.className = "state-body state-body-dim";
    hint.textContent = "Instant search and the file tree still work. Start a local model for written answers.";
    block.copy.append(hint);
    host.append(block.el);
    if (payload.retrieval) renderChunks(payload.retrieval);
    return;
  }

  const block = stateBlock(
    "error",
    "request failed",
    payload.error || payload.message || "Something went wrong.",
    { icon: "!" },
  );
  host.append(block.el);
  if (payload.retrieval) renderChunks(payload.retrieval);
}

// Answers are cached per question for this browser session. A file-scoped
// answer is cached separately for each file, so opening another file cannot
// reuse the wrong context. Asking the same thing twice is instant.
function answerCacheKey(question, scope) {
  const model = state.meta && state.meta.model
    ? `${state.meta.model.provider}/${state.meta.model.model}`
    : "no-model";
  return `${model}::${scope || "repo"}::${question.trim().toLowerCase()}`;
}

function readAnswerCache(key) {
  try {
    const raw = window.sessionStorage.getItem(ANSWER_CACHE_KEY);
    if (!raw) return null;
    const store = JSON.parse(raw);
    return store && store[key] ? store[key] : null;
  } catch (_err) {
    return null;
  }
}

function writeAnswerCache(key, payload) {
  try {
    const raw = window.sessionStorage.getItem(ANSWER_CACHE_KEY);
    const store = raw ? JSON.parse(raw) : {};
    store[key] = payload;
    const keys = Object.keys(store);
    if (keys.length > ANSWER_CACHE_LIMIT) delete store[keys[0]];
    window.sessionStorage.setItem(ANSWER_CACHE_KEY, JSON.stringify(store));
  } catch (_err) {
    // Private mode or quota: caching is optional, never fatal.
  }
}

async function ask(question, scope) {
  const cacheKey = answerCacheKey(question, scope);
  const cached = readAnswerCache(cacheKey);
  if (cached) {
    renderAnswer(cached, { cached: true, ms: 0 });
    status(scope
      ? `cached answer for ${scope} - same file and question, no model call`
      : "cached answer - same question, no model call");
    return;
  }

  const button = scope ? $("explain-file") : $("ask-button");
  button.disabled = true;
  const host = resetAnswer();
  host.setAttribute("aria-busy", "true");
  resetChunks();
  const block = stateBlock(
    "pending",
    scope ? "explaining this file" : "asking the repo",
    scope
      ? `Reading ${scope}, then asking the local model about that file only.`
      : "Searching the index, then asking the local model across files.",
    { icon: "" },
  );
  const note = document.createElement("p");
  note.className = "state-body state-body-dim";
  note.textContent = "0.0s · local model · nothing leaves this machine";
  block.copy.append(note);
  host.append(block.el);
  status(scope ? `explaining ${scope}...` : "asking...");
  const started = performance.now();
  const timer = window.setInterval(() => {
    const elapsed = formatDuration(performance.now() - started);
    note.textContent = `${elapsed} · still local · a short question can still take a minute`;
  }, 1000);
  let live = null;
  let firstTokenMs = null;
  const requestBody = scope
    ? { question, path: scope, top_k: ASK_TOP_K }
    : { question, top_k: ASK_TOP_K };
  try {
    const payload = await apiStream("/api/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    }, {
      onMeta(meta) {
        if (meta.retrieval) renderChunks(meta.retrieval);
        const chunks = (meta.retrieval && meta.retrieval.chunks || []).length;
        note.textContent = `${formatDuration(performance.now() - started)} · ${chunks} chunks · generating locally`;
      },
      onDelta(text) {
        if (!live) {
          if (block.el.parentNode) block.el.remove();
          live = document.createElement("div");
          live.className = "answer-body streaming";
          host.append(live);
        }
        if (firstTokenMs === null) firstTokenMs = performance.now() - started;
        live.textContent += text;
        host.scrollTop = host.scrollHeight;
      },
    });
    writeAnswerCache(cacheKey, payload);
    const ms = performance.now() - started;
    const ttft = firstTokenMs === null ? payload.ttft_ms : firstTokenMs;
    renderAnswer(payload, { cached: false, ms, ttftMs: ttft });
    const first = ttft == null ? "" : ` · first token in ${formatDuration(ttft)}`;
    status(payload.mode === "refused"
      ? `no answer found in ${formatDuration(ms)}`
      : `answer ready in ${formatDuration(ms)}${first}`);
  } catch (err) {
    const failed = stateBlock("error", "request failed", err.message, { icon: "!" });
    resetAnswer().append(failed.el);
    resetChunks();
    status(err.message, true);
  } finally {
    window.clearInterval(timer);
    host.setAttribute("aria-busy", "false");
    button.disabled = false;
  }
}

// "Explain this file" is the scoped counterpart to repo-wide Ask: the same
// answer pipeline and the same validated citations, but retrieval may only use
// chunks from the open file.
const EXPLAIN_FILE_QUESTION = "Explain this file.";

async function explainFile() {
  const scope = state.activePath;
  if (!scope) {
    status("open a file first, then choose Explain this file", true);
    return;
  }
  // The answer renders in the Ask tab, so bring that panel forward.
  setWorkTab("ask");
  await ask(EXPLAIN_FILE_QUESTION, scope);
}

// -- instant find ------------------------------------------------------
// Retrieval only: /api/search never calls the model, so this is the "where is
// X?" path and returns in milliseconds. Rows reuse openFile(), exactly like a
// validated citation, so every result lands on real lines.

let findTimer = 0;
let findSeq = 0;

function setFindExpanded(expanded) {
  const input = $("find-input");
  if (input) input.setAttribute("aria-expanded", String(expanded));
}

function hideFindResults() {
  window.clearTimeout(findTimer);
  const host = $("find-results");
  host.hidden = true;
  host.textContent = "";
  setFindExpanded(false);
}

function findRow(item, position) {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "find-row";
  row.dataset.path = item.path;
  row.dataset.start = String(item.start);
  row.dataset.end = String(item.end);
  row.title = `Open ${item.path}:${item.start}-${item.end}`;
  const num = document.createElement("span");
  num.className = "find-num";
  num.textContent = String(position + 1).padStart(2, "0");
  const where = document.createElement("span");
  where.className = "find-where mono";
  where.textContent = `${item.path}:${item.start}-${item.end}`;
  const kind = document.createElement("span");
  kind.className = "find-kind mono";
  kind.textContent = item.kind || "match";
  const name = document.createElement("span");
  name.className = "find-name";
  name.textContent = item.name;
  const snippet = document.createElement("span");
  snippet.className = "find-snippet mono";
  snippet.textContent = item.snippet;
  row.append(num, where, kind, name, snippet);
  return row;
}

function renderFindResults(data) {
  const host = $("find-results");
  host.hidden = false;
  host.textContent = "";
  setFindExpanded(true);
  const head = document.createElement("div");
  head.className = "find-head";
  const count = document.createElement("span");
  count.textContent = data.items.length
    ? `${data.items.length} match${data.items.length === 1 ? "" : "es"}`
    : "no matches";
  const mode = document.createElement("span");
  mode.className = "find-head-mode";
  mode.textContent = `${data.elapsed_ms} ms / index only / no model`;
  head.append(count, mode);
  host.append(head);

  if (!data.items.length) {
    const hint = document.createElement("p");
    hint.className = "find-empty";
    hint.textContent = data.reason || "Try a symbol name or a distinctive word.";
    host.append(hint);
    return;
  }

  data.items.forEach((item, index) => host.append(findRow(item, index)));

  const query = $("find-input").value.trim();
  const ask = document.createElement("button");
  ask.type = "button";
  ask.className = "find-ask";
  ask.dataset.ask = "1";
  const mark = document.createElement("span");
  mark.className = "find-ask-mark";
  mark.textContent = "?";
  const copy = document.createElement("span");
  copy.className = "find-ask-copy";
  const title = document.createElement("span");
  title.className = "find-ask-title";
  title.textContent = `Ask the local model about "${query}"`;
  const sub = document.createElement("span");
  sub.className = "find-ask-sub";
  sub.textContent = "slower path / can take a minute / citations still checked";
  copy.append(title, sub);
  const cta = document.createElement("span");
  cta.className = "find-ask-cta";
  cta.textContent = "ask \u2192";
  ask.append(mark, copy, cta);
  host.append(ask);
}

async function runFind(raw) {
  const query = String(raw || "").trim();
  if (!query) {
    hideFindResults();
    return;
  }
  const seq = ++findSeq;
  const host = $("find-results");
  host.hidden = false;
  host.textContent = "";
  setFindExpanded(true);
  const pending = document.createElement("p");
  pending.className = "find-empty";
  pending.textContent = "searching the index / no model...";
  host.append(pending);
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    if (seq !== findSeq) return;
    renderFindResults(data);
    status(data.items.length
      ? `${data.items.length} matches in ${data.elapsed_ms} ms - retrieval only`
      : `no matches for "${query}"`);
  } catch (err) {
    if (seq !== findSeq) return;
    host.textContent = "";
    const failed = document.createElement("p");
    failed.className = "find-empty error";
    failed.textContent = err.message;
    host.append(failed);
    status(err.message, true);
  }
}

function askFromSearch() {
  const raw = $("find-input").value.trim();
  if (!raw) {
    $("find-input").focus();
    status("type what you want to ask about first");
    return;
  }
  const question = raw.endsWith("?") || /\s/.test(raw)
    ? raw
    : `Where is ${raw}?`;
  hideFindResults();
  setWorkTab("ask");
  const input = $("ask-input");
  input.value = question;
  input.focus();
  input.select();
  status("moved to Ask - local model can take a minute; press ask to run");
}

function focusFindRow(step) {
  const rows = [...$("find-results").querySelectorAll(".find-row, .find-ask")];
  if (!rows.length) return;
  const current = rows.indexOf(document.activeElement);
  let next;
  if (current === -1) next = step > 0 ? 0 : rows.length - 1;
  else next = (current + step + rows.length) % rows.length;
  rows[next].focus();
}

// -- module graph ------------------------------------------------------

// The map is drawn here rather than by a rendering library. Layout comes from
// /api/map alone: no third-party runtime, no CDN, one JSON call.

const MAP_NODE_H = 42;
const MAP_GAP_X = 56;
const MAP_GAP_Y = 16;
const MAP_PAD = 18;

function mapAdjacency(nodes, edges) {
  const index = new Map(nodes.map((node, i) => [node.key, i]));
  const parents = nodes.map(() => []);
  for (const edge of edges || []) {
    const from = index.get(edge.src);
    const to = index.get(edge.dst);
    if (from === undefined || to === undefined || from === to) continue;
    parents[to].push(from);
  }
  return parents;
}

// Longest path from the roots, so dependencies sit in the left-hand columns.
// A cycle would recurse forever, so an in-progress node scores zero.
function mapDepths(nodes, parents) {
  const depth = nodes.map(() => 0);
  const seen = nodes.map(() => 0);
  const visit = (i) => {
    if (seen[i] === 2) return depth[i];
    if (seen[i] === 1) return 0;
    seen[i] = 1;
    let best = 0;
    for (const parent of parents[i]) best = Math.max(best, visit(parent) + 1);
    seen[i] = 2;
    depth[i] = best;
    return best;
  };
  for (let i = 0; i < nodes.length; i += 1) visit(i);
  return depth;
}

// Order each column by the mean row of its parents (a barycenter pass). Two
// sweeps are enough to pull most edges into short, non-crossing runs.
function mapColumns(nodes, parents, depth) {
  const columns = [];
  nodes.forEach((node, i) => {
    if (!columns[depth[i]]) columns[depth[i]] = [];
    columns[depth[i]].push(i);
  });
  columns.forEach((members) => members.sort((a, b) => nodes[a].key.localeCompare(nodes[b].key)));
  const rowOf = new Map();
  const assign = () => columns.forEach((members) => {
    members.forEach((i, row) => rowOf.set(i, row));
  });
  assign();
  for (let pass = 0; pass < 3; pass += 1) {
    for (let c = 1; c < columns.length; c += 1) {
      columns[c].sort((a, b) => {
        const mean = (i) => {
          const list = parents[i];
          if (!list || !list.length) return rowOf.get(i) || 0;
          return list.reduce((sum, p) => sum + (rowOf.get(p) || 0), 0) / list.length;
        };
        return mean(a) - mean(b) || nodes[a].key.localeCompare(nodes[b].key);
      });
      columns[c].forEach((i, row) => rowOf.set(i, row));
    }
  }
  return columns;
}

function mapNodeWidth(node) {
  return Math.min(320, Math.max(150, 26 + node.key.length * 6.6));
}

function shorten(text, max) {
  if (max < 4 || text.length <= max) return text;
  return `${text.slice(0, max - 1)}\u2026`;
}

function svgNode(name, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const key of Object.keys(attrs)) el.setAttribute(key, String(attrs[key]));
  return el;
}

function setGraphHelp(text, detail) {
  const help = $("graph-help");
  if (!help) return;
  const [lead, dim] = help.querySelectorAll("span");
  if (lead) lead.textContent = text;
  if (dim) dim.textContent = detail || "";
}

// A package node has no file of its own, so open the first indexed file that
// lives under it. Returns null when nothing matches, and the node stays inert.
function resolveMapTarget(key) {
  const paths = state.files.map((file) => file.path);
  if (paths.includes(key)) return key;
  const prefix = key.endsWith("/") ? key : `${key}/`;
  return paths.find((path) => path.startsWith(prefix)) || null;
}

function attachMapActivation(element, path) {
  const activate = () => openFile(path);
  element.addEventListener("click", activate);
  element.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    activate();
  });
}

// Edges are orthogonal and rounded, not free-form curves: a dependency leaves
// the right edge of its box, runs down a gutter, and enters the next column.
// Deduplicating pairs removes the parallel strands that made the old map read
// as a web. Hovering or focusing a node dims everything unrelated.
function mapElbow(x1, y1, x2, y2) {
  if (Math.abs(y2 - y1) < 1) return `M${x1},${y1} H${x2}`;
  const mid = x1 + Math.max(18, (x2 - x1) / 2);
  const dir = y2 > y1 ? 1 : -1;
  return `M${x1},${y1} H${mid - 5} Q${mid},${y1} ${mid},${y1 + 5 * dir}`
    + ` V${y2 - 5 * dir} Q${mid},${y2} ${mid + 5},${y2} H${x2}`;
}

function clearMapFocus() {
  const graph = $("graph");
  if (!graph) return;
  graph.classList.remove("has-focus");
  graph.querySelectorAll(".map-node.is-lit").forEach((node) => node.classList.remove("is-lit"));
  graph.querySelectorAll(".map-node.is-dim").forEach((node) => node.classList.remove("is-dim"));
  graph.querySelectorAll(".map-edge.is-lit").forEach((edge) => edge.classList.remove("is-lit"));
}

function wireMapFocus(host, key, neighborKeys) {
  const graph = $("graph");
  const nodeGroups = [...host.querySelectorAll(".map-node")];
  const edgePaths = [...host.querySelectorAll(".map-edge")];
  const enter = () => {
    graph.classList.add("has-focus");
    for (const group of nodeGroups) {
      group.classList.toggle("is-lit", group.dataset.key === key);
      group.classList.toggle("is-dim", !neighborKeys.has(group.dataset.key));
    }
    for (const edge of edgePaths) {
      edge.classList.toggle("is-lit", edge.dataset.src === key || edge.dataset.dst === key);
    }
  };
  const leave = () => {
    graph.classList.remove("has-focus");
    for (const group of nodeGroups) group.classList.remove("is-lit", "is-dim");
    for (const edge of edgePaths) edge.classList.remove("is-lit");
  };
  const group = host.querySelector(`.map-node[data-key="${CSS.escape(key)}"]`);
  if (!group) return;
  group.addEventListener("pointerenter", enter);
  group.addEventListener("pointerleave", leave);
  group.addEventListener("focus", enter);
  group.addEventListener("blur", leave);
}

function renderMapSvg(host, data) {
  const parents = mapAdjacency(data.nodes, data.edges);
  const depth = mapDepths(data.nodes, parents);
  const columns = mapColumns(data.nodes, parents, depth);

  const width = Math.max(150, ...data.nodes.map(mapNodeWidth));
  const placed = new Array(data.nodes.length);
  let height = 0;
  columns.forEach((members, column) => {
    members.forEach((i, row) => {
      placed[i] = {
        x: MAP_PAD + column * (width + MAP_GAP_X),
        y: MAP_PAD + row * (MAP_NODE_H + MAP_GAP_Y),
      };
      height = Math.max(height, placed[i].y + MAP_NODE_H);
    });
  });

  const totalWidth = MAP_PAD + columns.length * (width + MAP_GAP_X);
  const svg = svgNode("svg", {
    viewBox: `0 0 ${totalWidth} ${height + MAP_PAD}`,
    width: totalWidth,
    height: height + MAP_PAD,
    role: "img",
    "aria-label": `${data.nodes.length} nodes and ${(data.edges || []).length} import edges`,
  });
  svg.style.maxWidth = "none";

  const index = new Map(data.nodes.map((node, i) => [node.key, i]));
  const edgeLayer = svgNode("g", { class: "map-edges" });
  const seen = new Set();
  for (const edge of data.edges || []) {
    const from = index.get(edge.src);
    const to = index.get(edge.dst);
    if (from === undefined || to === undefined || from === to) continue;
    const pair = `${edge.src}\u0000${edge.dst}`;
    if (seen.has(pair)) continue;
    seen.add(pair);
    const x1 = placed[from].x + width;
    const y1 = placed[from].y + MAP_NODE_H / 2;
    const x2 = placed[to].x;
    const y2 = placed[to].y + MAP_NODE_H / 2;
    edgeLayer.append(svgNode("path", {
      d: mapElbow(x1, y1, x2, y2),
      class: "map-edge",
      "data-src": edge.src,
      "data-dst": edge.dst,
    }));
  }
  svg.append(edgeLayer);

  const neighbors = new Map();
  const link = (a, b) => {
    if (!neighbors.has(a)) neighbors.set(a, new Set([a]));
    neighbors.get(a).add(b);
  };
  for (const edge of data.edges || []) {
    link(edge.src, edge.dst);
    link(edge.dst, edge.src);
  }

  const nodeLayer = svgNode("g", { class: "map-nodes" });
  let linked = 0;
  data.nodes.forEach((node, i) => {
    const spot = placed[i];
    const target = resolveMapTarget(node.key);
    const group = svgNode("g", {
      class: target ? "map-node" : "map-node static",
      transform: `translate(${spot.x}, ${spot.y})`,
      "data-key": node.key,
    });
    const caption = svgNode("title", {});
    caption.textContent = target ? `${node.key} - open file` : node.key;
    group.append(caption);
    if (target) {
      group.setAttribute("role", "button");
      group.setAttribute("tabindex", "0");
      group.setAttribute("data-path", target);
      group.setAttribute("aria-label", `${node.key}, open file`);
      attachMapActivation(group, target);
      linked += 1;
    }
    group.append(svgNode("rect", {
      width: width,
      height: MAP_NODE_H,
      rx: 6,
      class: "map-node-box",
    }));
    const name = svgNode("text", { class: "map-node-name", x: 12, y: 18 });
    name.textContent = shorten(node.key, Math.floor((width - 24) / 6.4));
    group.append(name);
    const files = `${node.files} file${node.files === 1 ? "" : "s"}`;
    const sub = svgNode("text", { class: "map-node-sub", x: 12, y: 33 });
    sub.textContent = node.symbols ? `${files} \u00b7 ${node.symbols} sym` : files;
    group.append(sub);
    nodeLayer.append(group);
  });
  svg.append(nodeLayer);

  host.textContent = "";
  host.append(svg);
  host.scrollTop = 0;
  host.scrollLeft = 0;

  // Wire focus after the nodes exist in the document.
  for (const node of data.nodes) {
    wireMapFocus(host, node.key, neighbors.get(node.key) || new Set([node.key]));
  }
  return linked;
}

// The list is the default module view: 32 file nodes and 53 import edges make
// a crossing-heavy drawing, while a package-grouped list shows every file with
// no lines to trace. The SVG is still one click away under Map.
function renderMapList(host, data, level) {
  host.textContent = "";
  const groups = new Map();
  for (const node of data.nodes) {
    const key = node.key;
    let group = ".";
    if (level === "package") {
      const cut = key.indexOf("/");
      group = cut === -1 ? key : key.slice(0, cut);
    } else {
      const cut = key.lastIndexOf("/");
      group = cut === -1 ? "." : key.slice(0, cut);
    }
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(node);
  }

  for (const group of [...groups.keys()].sort()) {
    const members = groups.get(group).sort((a, b) => a.key.localeCompare(b.key));
    const section = document.createElement("section");
    section.className = "map-group";
    const heading = document.createElement("h3");
    heading.className = "map-group-title mono";
    const label = document.createElement("span");
    label.textContent = group;
    const count = document.createElement("span");
    count.className = "map-group-count mono";
    count.textContent = `${members.length}`;
    heading.append(label, count);
    const list = document.createElement("ul");
    list.className = "map-list";
    for (const node of members) {
      const item = document.createElement("li");
      const target = resolveMapTarget(node.key);
      const files = `${node.files} file${node.files === 1 ? "" : "s"}`;
      const labelText = node.symbols ? `${files}, ${node.symbols} sym` : files;
      if (target) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "map-list-item";
        button.dataset.path = target;
        const name = document.createElement("span");
        name.className = "map-list-name mono";
        name.textContent = node.key;
        const sub = document.createElement("span");
        sub.className = "map-list-sub mono";
        sub.textContent = labelText;
        button.append(name, sub);
        attachMapActivation(button, target);
        item.append(button);
      } else {
        const plain = document.createElement("span");
        plain.className = "map-list-item static";
        plain.textContent = `${node.key} \u2014 ${labelText}`;
        item.append(plain);
      }
      list.append(item);
    }
    section.append(heading, list);
    host.append(section);
  }
  return data.nodes.length;
}

function scrollGraphToStart() {
  const host = $("graph");
  const firstNode = host.querySelector(".map-node, g.node");
  if (!firstNode) return;
  const nodeBox = firstNode.getBoundingClientRect();
  const hostBox = host.getBoundingClientRect();
  host.scrollLeft += nodeBox.left - hostBox.left - 10;
  host.scrollTop += nodeBox.top - hostBox.top - 10;
}

async function loadGraph(level) {
  state.mapLevel = level;
  const host = $("graph");
  host.setAttribute("aria-busy", "true");
  host.textContent = "";
  const loading = stateBlock("loading", "loading modules", "Building the import view from the index...");
  host.append(loading.el);
  $("graph-meta").textContent = "";
  clearMapFocus();
  try {
    const limit = level === "file" ? 40 : 24;
    const data = await api(`/api/map?level=${encodeURIComponent(level)}&limit=${limit}`);
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const moduleCount = $("module-count");
    if (moduleCount) moduleCount.textContent = String(nodes.length);
    if (nodes.length === 0) {
      host.textContent = "";
      const empty = stateBlock(
        "empty",
        "no imports in this view",
        "Switch between File and Package, or index a repository that imports its own files.",
        { icon: "0" },
      );
      host.append(empty.el);
      $("graph-meta").textContent = "0 nodes / 0 edges";
      setGraphHelp("Nothing to draw here.", "Switch view or index imports.");
      return;
    }
    let shown;
    try {
      if (state.mapView === "map") {
        shown = renderMapSvg(host, data);
        setGraphHelp(
          "Hover or focus a box to isolate its links. Enter opens the file.",
          "Expand for the full map \u00b7 Esc restores it",
        );
      } else {
        shown = renderMapList(host, data, level);
        setGraphHelp("List grouped by package. Click a file to open it.", "Switch to Map for imports");
      }
    } catch (renderError) {
      shown = renderMapList(host, data, level);
      setGraphHelp("List view. Activate an entry to open its file.", "Switch to Map for imports");
    }
    window.requestAnimationFrame(scrollGraphToStart);
    $("graph-meta").textContent = [
      `${shown} nodes`,
      `${edges.length} edges`,
      data.omitted ? `${data.omitted} omitted` : "",
    ].filter(Boolean).join(" / ");
  } catch (err) {
    host.textContent = "";
    const failed = stateBlock("error", "graph unavailable", err.message, { icon: "!" });
    host.append(failed.el);
    $("graph-meta").textContent = "graph unavailable";
    status(`graph: ${err.message}`, true);
  } finally {
    host.setAttribute("aria-busy", "false");
  }
}

function setGraphExpanded(expanded) {
  const pane = $("graph-pane");
  pane.classList.toggle("maximized", expanded);
  const button = $("graph-expand");
  button.setAttribute("aria-pressed", String(expanded));
  setButtonLabel(
    button,
    expanded ? "Restore" : "Expand",
    expanded ? "Restore module map" : "Expand module map",
  );
  const backdrop = $("map-backdrop");
  if (backdrop) backdrop.hidden = !expanded;
  if (!expanded) clearMapFocus();
  window.requestAnimationFrame(scrollGraphToStart);
}

function setMapView(view) {
  state.mapView = view === "map" ? "map" : "list";
  const tabs = [...document.querySelectorAll("#map-view button")];
  const selected = Math.max(0, tabs.findIndex((button) => button.dataset.view === state.mapView));
  tabs.forEach((button, index) => {
    const active = index === selected;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $("graph").setAttribute("aria-labelledby", tabs[selected].id);
  loadGraph(state.mapLevel);
  // The dock is narrower than an import graph. Map opens the focus layer, so
  // the 32-node drawing is readable instead of one clipped column.
  if (state.mapView === "map" && $("graph").clientWidth < 520) {
    setGraphExpanded(true);
    status("module map expanded - Esc restores it");
  }
}

function setMapLevel(level) {
  const tabs = [...document.querySelectorAll("#map-level button")];
  const selected = Math.max(0, tabs.findIndex((button) => button.dataset.level === level));
  tabs.forEach((button, index) => {
    const active = index === selected;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $("graph").setAttribute("aria-labelledby", tabs[selected].id);
  loadGraph(tabs[selected].dataset.level);
}

// -- work tabs ---------------------------------------------------------
// Editor and Ask share the middle pane; Modules, citations, and model state
// stay visible on the right. A question never hides the code it cites.

function setWorkTab(name, options) {
  const tab = name === "ask" ? "ask" : "editor";
  const editorTab = $("work-tab-editor");
  const askTab = $("work-tab-ask");
  const editorPanel = $("work-editor");
  const askPanel = $("work-ask");
  if (!editorTab || !askTab || !editorPanel || !askPanel) return;
  state.workTab = tab;
  const entries = [
    { button: editorTab, panel: editorPanel, active: tab === "editor" },
    { button: askTab, panel: askPanel, active: tab === "ask" },
  ];
  for (const entry of entries) {
    entry.button.classList.toggle("active", entry.active);
    entry.button.setAttribute("aria-selected", String(entry.active));
    entry.button.tabIndex = entry.active ? 0 : -1;
    entry.panel.hidden = !entry.active;
  }
  if (tab === "ask" && options && options.focus) $("ask-input").focus();
}

function initWorkTabs() {
  const tabs = [$("work-tab-editor"), $("work-tab-ask")];
  if (tabs.some((tab) => !tab)) return;
  tabs.forEach((button, index) => {
    button.addEventListener("click", () => setWorkTab(index === 0 ? "editor" : "ask"));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let next;
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else next = (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      setWorkTab(next === 0 ? "editor" : "ask");
      tabs[next].focus();
    });
  });
}

// -- guide -------------------------------------------------------------

function openHelp(trigger) {
  const dialog = $("help-dialog");
  if (!dialog || dialog.open) return;
  state.helpTrigger = trigger || document.activeElement;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  try {
    window.localStorage.setItem(HELP_KEY, "1");
  } catch (_err) {
    // Storage is optional; the guide still opens.
  }
}

function closeHelp() {
  const dialog = $("help-dialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function initHelp() {
  const dialog = $("help-dialog");
  if (!dialog) return;
  document.querySelectorAll("[data-help]").forEach((button) => {
    button.addEventListener("click", () => openHelp(button));
  });
  const close = $("help-close");
  if (close) close.addEventListener("click", closeHelp);
  const gotit = $("help-gotit");
  if (gotit) gotit.addEventListener("click", closeHelp);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeHelp();
  });
  dialog.addEventListener("close", () => {
    const back = state.helpTrigger;
    state.helpTrigger = null;
    if (back && back.isConnected && typeof back.focus === "function") {
      // Defer past the browser's own focus restoration for the dialog.
      window.requestAnimationFrame(() => back.focus());
    }
  });
  let seen = null;
  try {
    seen = window.localStorage.getItem(HELP_KEY);
  } catch (_err) {
    seen = "1";
  }
  if (!seen) {
    // First visit: open the guide once, after layout has settled.
    window.requestAnimationFrame(() => openHelp($("ask-input")));
  }
}

function initExamples() {
  document.querySelectorAll(".example").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $("ask-input");
      input.value = button.dataset.question || button.textContent.trim();
      input.focus();
      status("example loaded - press Ask to run it");
    });
  });
}

function initShortcuts() {
  document.addEventListener("keydown", (event) => {
    const active = document.activeElement;
    const tag = active && active.tagName;
    const typing = tag === "INPUT" || tag === "TEXTAREA" || (active && active.isContentEditable);
    const dialog = $("help-dialog");

    if (event.key === "/" && !event.defaultPrevented) {
      if (typing || (dialog && dialog.open)) return;
      event.preventDefault();
      const search = $("find-input");
      search.focus();
      search.select();
      return;
    }

    if (event.key === "?" && !event.defaultPrevented) {
      if (typing || (dialog && dialog.open)) return;
      event.preventDefault();
      openHelp(active);
      return;
    }

    if (event.key === "f" && (event.ctrlKey || event.metaKey) && state.activePath) {
      event.preventDefault();
      setEditorFindOpen(true);
      return;
    }

    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (dialog && dialog.open) return;
    if (state.findOpen) {
      setEditorFindOpen(false);
      status("find closed");
      return;
    }
    const pane = $("graph-pane");
    if (pane && pane.classList.contains("maximized")) {
      setGraphExpanded(false);
      status("module map restored");
      return;
    }
    if (!$("find-results").hidden) {
      hideFindResults();
      $("find-input").focus();
    }
  });
}

// -- init --------------------------------------------------------------

function initEditorControls() {
  const toggle = $("editor-find-toggle");
  toggle.addEventListener("click", () => setEditorFindOpen(!state.findOpen));

  const input = $("editor-find-input");
  input.addEventListener("input", () => applyEditorFind(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      stepEditorFind(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setEditorFindOpen(false);
      status("find closed");
    }
  });
  $("editor-find-next").addEventListener("click", () => stepEditorFind(1));
  $("editor-find-prev").addEventListener("click", () => stepEditorFind(-1));
  $("editor-find-close").addEventListener("click", () => {
    setEditorFindOpen(false);
    status("find closed");
  });
}

function initSearch() {
  $("find-form").addEventListener("submit", (event) => {
    event.preventDefault();
    window.clearTimeout(findTimer);
    runFind($("find-input").value);
  });
  $("find-input").addEventListener("input", () => {
    window.clearTimeout(findTimer);
    findTimer = window.setTimeout(() => runFind($("find-input").value), 220);
  });
  $("find-input").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" && !$("find-results").hidden) {
      event.preventDefault();
      focusFindRow(1);
      return;
    }
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    if ($("find-input").value) {
      $("find-input").value = "";
      runFind("");
      status("search cleared");
    } else {
      hideFindResults();
      $("find-input").blur();
    }
  });
  $("find-results").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      focusFindRow(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      hideFindResults();
      $("find-input").focus();
    }
  });
  $("find-results").addEventListener("click", (event) => {
    const ask = event.target.closest("[data-ask]");
    if (ask) {
      askFromSearch();
      return;
    }
    const row = event.target.closest("[data-path]");
    if (!row) return;
    const start = Number(row.dataset.start || 0) || null;
    const end = Number(row.dataset.end || 0) || null;
    hideFindResults();
    openFile(row.dataset.path, start, end);
  });
  $("ask-from-search").addEventListener("click", askFromSearch);
  $("empty-focus-search").addEventListener("click", () => {
    const search = $("find-input");
    search.focus();
    search.select();
  });
}

function initTreeFilter() {
  $("tree-filter").addEventListener("input", scheduleTreeFilter);
  $("tree-filter").addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !$("tree-filter").value) return;
    event.preventDefault();
    event.stopPropagation();
    clearTreeFilter();
    status("filter cleared");
  });
  $("tree-filter-clear").addEventListener("click", () => {
    clearTreeFilter();
    status("filter cleared");
  });
}

function initGraphControls() {
  setButtonLabel($("graph-expand"), "Expand", "Expand module map");
  $("graph-expand").addEventListener("click", () => {
    const expanded = !$("graph-pane").classList.contains("maximized");
    setGraphExpanded(expanded);
    status(expanded ? "module map expanded - Esc restores it" : "module map restored");
  });
  $("map-backdrop").addEventListener("click", () => {
    setGraphExpanded(false);
    status("module map restored");
  });

  const levelTabs = [...document.querySelectorAll("#map-level button")];
  levelTabs.forEach((button, index) => {
    button.addEventListener("click", () => setMapLevel(button.dataset.level));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = (index + step + levelTabs.length) % levelTabs.length;
      setMapLevel(levelTabs[next].dataset.level);
      levelTabs[next].focus();
    });
  });
  const viewTabs = [...document.querySelectorAll("#map-view button")];
  viewTabs.forEach((button, index) => {
    button.addEventListener("click", () => setMapView(button.dataset.view));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = (index + step + viewTabs.length) % viewTabs.length;
      setMapView(viewTabs[next].dataset.view);
      viewTabs[next].focus();
    });
  });
}

async function init() {
  initHelp();
  initExamples();
  initShortcuts();
  initWorkTabs();
  initEditorControls();
  initSearch();
  initTreeFilter();
  initGraphControls();

  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("ask-input").value.trim();
    if (question) ask(question);
  });
  $("explain-file").addEventListener("click", () => explainFile());
  $("answer").addEventListener("click", (event) => {
    const target = event.target.closest("[data-path]");
    if (!target) return;
    event.preventDefault();
    const start = Number(target.dataset.start || 0) || null;
    const end = Number(target.dataset.end || 0) || null;
    openFile(target.dataset.path, start, end);
  });
  $("chunks").addEventListener("click", (event) => {
    const target = event.target.closest("[data-path]");
    if (!target) return;
    event.preventDefault();
    const start = Number(target.dataset.start || 0) || null;
    const end = Number(target.dataset.end || 0) || null;
    openFile(target.dataset.path, start, end);
  });

  try {
    const meta = await api("/api/meta");
    state.meta = meta;
    state.files = meta.tree || [];
    renderStats(meta);
    renderModel(meta);
    renderTree(state.files);
    status(`${state.files.length} indexed files`);
    setMapLevel(meta.mapLevel || "file");
  } catch (err) {
    status(err.message, true);
    const treeError = stateBlock("error", "index unavailable", err.message, { icon: "!" });
    $("tree").textContent = "";
    $("tree").append(treeError.el);
    const graphError = stateBlock("error", "index unavailable", err.message, { icon: "!" });
    $("graph").textContent = "";
    $("graph").append(graphError.el);
  }
}

init();
