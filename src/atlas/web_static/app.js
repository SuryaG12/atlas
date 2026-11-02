"use strict";

// Atlas M3 client. Read-only: it only GETs the index and POSTs questions.

const state = {
  meta: null,
  files: [],
  activePath: null,
  mapLevel: "file",
  treeRows: [],
  treeDirs: [],
  filterTimer: 0,
  helpTrigger: null,
};

const $ = (id) => document.getElementById(id);

const HELP_KEY = "atlas.help.seen.v1";
const ANSWER_CACHE_KEY = "atlas.answers.v1";
const ANSWER_CACHE_LIMIT = 20;

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

// -- header ------------------------------------------------------------

function renderStats(meta) {
  const root = meta.root || "";
  const name = root.split(/[\\/]/).filter(Boolean).pop() || root;
  $("repo-name").textContent = name;
  $("repo-root").textContent = root;
  $("repo-root").title = root;
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
  renderTreeNode(root, 0, openAll, fragment, host);
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
}

function renderTreeNode(node, depth, openAll, parent, host) {
  const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const dir of dirs) {
    const details = document.createElement("details");
    details.open = openAll || depth < 1;
    const summary = document.createElement("summary");
    summary.style.paddingLeft = `${8 + depth * 12}px`;
    const name = document.createElement("span");
    name.className = "tree-folder-name mono";
    name.textContent = dir.name;
    summary.append(name);
    details.append(summary);
    renderTreeNode(dir, depth + 1, openAll, details, host);
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

function applyTreeFilter() {
  window.clearTimeout(state.filterTimer);
  const query = $("tree-filter").value.trim().toLowerCase();
  for (const row of state.treeRows) {
    row.el.hidden = query !== "" && !row.path.includes(query);
  }
  for (const dir of state.treeDirs) {
    const hasVisible = dir.rows.some((row) => !row.hidden);
    dir.el.hidden = !hasVisible;
    if (query && hasVisible) dir.el.open = true;
  }
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

function renderSource(data, start, end) {
  const host = $("source");
  host.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "code";
  const fragment = document.createDocumentFragment();
  const low = start ? Math.max(1, start) : 0;
  const high = end ? Math.max(end, low) : low;
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
    lc.textContent = data.lines[i] === "" ? " " : data.lines[i];
    row.append(ln, lc);
    fragment.append(row);
  }
  wrap.append(fragment);
  host.append(wrap);

  if (low) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const line = host.querySelector(`.code-line[data-line="${low}"]`);
      if (!line) return;
      const top = line.getBoundingClientRect().top
        - host.getBoundingClientRect().top
        + host.scrollTop;
      host.scrollTop = Math.max(0, top - host.clientHeight / 3);
    }));
  }
}

async function openFile(path, start, end) {
  if (!path) return;
  status(`loading ${path}`);
  try {
    const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
    state.activePath = path;
    setActiveTreeRow(path);
    renderSource(data, start, end);
    $("source-title").textContent = path;
    $("source-meta").textContent = [
      data.language,
      `${data.total_lines} lines`,
      "live",
      data.truncated ? "truncated" : "",
    ].filter(Boolean).join(" / ");
    $("source-meta").title =
      "Read from disk now. The file tree and the top stats come from the last index run.";
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
  const details = document.createElement("details");
  details.className = "chunks";
  const summary = document.createElement("summary");
  const chunks = (retrieval && retrieval.chunks) || [];
  const sources = (retrieval && retrieval.sources) || {};
  const sourceCounts = ["symbol", "fts", "graph"]
    .map((key) => `${key} ${(sources[key] || []).length}`)
    .join(" / ");
  summary.textContent = `retrieved chunks (${chunks.length}) - ${sourceCounts}`;
  details.append(summary);

  if (chunks.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "No chunks passed the retrieval threshold.";
    details.append(empty);
    return details;
  }

  const list = document.createElement("ol");
  list.className = "chunk-list";
  chunks.forEach((chunk, position) => {
    const row = document.createElement("li");
    row.className = "chunk-row";
    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = String(position + 1);
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
  details.append(list);
  return details;
}

// -- answers -----------------------------------------------------------

function resetAnswer() {
  const host = $("answer");
  host.textContent = "";
  host.scrollTop = 0;
  return host;
}

function renderAnswer(payload, timing) {
  const host = resetAnswer();
  if (payload.mode === "answer") {
    const body = document.createElement("div");
    const citations = (payload.citations && payload.citations.valid) || [];
    body.innerHTML = markdown(payload.answer, citations);
    host.append(body);
    linkifyAnswerCitations(body, citations);
    // Report what a reader can click, so the meta count and the visible links
    // cannot drift apart.
    const linkCount = body.querySelectorAll("a.cite").length;

    if (linkCount === 0) {
      const warn = document.createElement("div");
      warn.className = "warn";
      warn.textContent = "No validated citations in this answer.";
      host.append(warn);
    }

    const invalid = (payload.citations && payload.citations.invalid) || [];
    if (invalid.length) {
      const list = document.createElement("ul");
      list.className = "invalid-list";
      for (const issue of invalid) {
        const item = document.createElement("li");
        item.textContent = `removed ${issue.citation}: ${issue.reason}`;
        list.append(item);
      }
      host.append(list);
    }

    const meta = document.createElement("div");
    meta.className = "answer-meta";
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
      span.className = "mono";
      span.textContent = timing.cached ? "cached" : formatDuration(timing.ms);
      span.title = timing.cached
        ? "Reused from this browser session; ask a different question to call the model"
        : "Time from Ask to rendered answer";
      meta.append(span);
    }
    host.append(meta);
    host.append(retrievalDetails(payload.retrieval));
    return;
  }

  if (payload.mode === "refused") {
    const refusal = document.createElement("p");
    refusal.className = "refusal";
    refusal.textContent = payload.answer || "I couldn't find that in this repo.";
    host.append(refusal);
    if (payload.reason) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = payload.reason;
      host.append(hint);
    }
    host.append(retrievalDetails(payload.retrieval));
    return;
  }

  if (payload.mode === "no-model") {
    const notice = document.createElement("p");
    notice.className = "notice";
    notice.textContent = payload.message;
    host.append(notice);
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "Retrieval still works. Start a local model for written answers.";
    host.append(hint);
    host.append(retrievalDetails(payload.retrieval));
    return;
  }

  const error = document.createElement("p");
  error.className = "error";
  error.textContent = payload.error || payload.message || "Something went wrong.";
  host.append(error);
  if (payload.retrieval) host.append(retrievalDetails(payload.retrieval));
}

// Answers are cached per question for this browser session. Asking the same
// thing twice is instant; a new question still calls the local model.
function answerCacheKey(question) {
  const model = state.meta && state.meta.model
    ? `${state.meta.model.provider}/${state.meta.model.model}`
    : "no-model";
  return `${model}::${question.trim().toLowerCase()}`;
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

async function ask(question) {
  const cacheKey = answerCacheKey(question);
  const cached = readAnswerCache(cacheKey);
  if (cached) {
    renderAnswer(cached, { cached: true, ms: 0 });
    status("cached answer - same question, no model call");
    return;
  }

  const button = $("ask-button");
  button.disabled = true;
  const host = resetAnswer();
  host.setAttribute("aria-busy", "true");
  const pending = document.createElement("p");
  pending.className = "hint";
  pending.textContent = "searching the index, then asking the local model...";
  host.append(pending);
  status("asking...");
  const started = performance.now();
  const timer = window.setInterval(() => {
    const elapsed = formatDuration(performance.now() - started);
    pending.textContent = "searching the index, then asking the local model... "
      + `${elapsed}. A short question can still take a minute.`;
  }, 1000);
  try {
    const payload = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    writeAnswerCache(cacheKey, payload);
    const ms = performance.now() - started;
    renderAnswer(payload, { cached: false, ms });
    status(payload.mode === "refused"
      ? `no answer found in ${formatDuration(ms)}`
      : `answer ready in ${formatDuration(ms)}`);
  } catch (err) {
    resetAnswer().innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
    status(err.message, true);
  } finally {
    window.clearInterval(timer);
    host.setAttribute("aria-busy", "false");
    button.disabled = false;
  }
}

// -- module graph ------------------------------------------------------

// The map is drawn here rather than by a rendering library. The previous
// build shipped a 2.5 MB vendored Mermaid bundle and recovered its output by
// parsing generated element ids; when that single script failed to arrive the
// pane only said "Mermaid failed to load" and the whole map was lost. Layout
// now comes from /api/map alone: no third-party runtime, no CDN, one JSON call.

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

function setGraphHelp(text) {
  const help = $("graph-help");
  const lead = help && help.firstElementChild;
  if (lead) lead.textContent = text;
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

function renderMapSvg(host, data) {
  const parents = mapAdjacency(data.nodes, data.edges);
  const depth = mapDepths(data.nodes, parents);
  const columns = [];
  data.nodes.forEach((node, i) => {
    if (!columns[depth[i]]) columns[depth[i]] = [];
    columns[depth[i]].push(i);
  });

  const width = Math.max(150, ...data.nodes.map(mapNodeWidth));
  const placed = new Array(data.nodes.length);
  let height = 0;
  columns.forEach((members, column) => {
    members.sort((a, b) => data.nodes[a].key.localeCompare(data.nodes[b].key));
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
  for (const edge of data.edges || []) {
    const from = index.get(edge.src);
    const to = index.get(edge.dst);
    if (from === undefined || to === undefined || from === to) continue;
    const x1 = placed[from].x + width;
    const y1 = placed[from].y + MAP_NODE_H / 2;
    const x2 = placed[to].x;
    const y2 = placed[to].y + MAP_NODE_H / 2;
    const mid = (x1 + x2) / 2;
    edgeLayer.append(svgNode("path", {
      d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`,
      class: "map-edge",
    }));
  }
  svg.append(edgeLayer);

  const nodeLayer = svgNode("g", { class: "map-nodes" });
  let linked = 0;
  data.nodes.forEach((node, i) => {
    const spot = placed[i];
    const target = resolveMapTarget(node.key);
    const group = svgNode("g", {
      class: target ? "map-node" : "map-node static",
      transform: `translate(${spot.x}, ${spot.y})`,
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
      rx: 8,
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
  return linked;
}

// Last resort: if the SVG pass throws for any reason the pane still shows
// node/edge counts and every entry is still a working link to its file.
function renderMapList(host, data) {
  host.textContent = "";
  const list = document.createElement("ul");
  list.className = "map-list";
  for (const node of data.nodes) {
    const item = document.createElement("li");
    const target = resolveMapTarget(node.key);
    const files = `${node.files} file${node.files === 1 ? "" : "s"}`;
    const label = node.symbols ? `${files}, ${node.symbols} sym` : files;
    if (target) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "map-list-item";
      button.dataset.path = target;
      button.textContent = `${node.key} \u2014 ${label}`;
      attachMapActivation(button, target);
      item.append(button);
    } else {
      const plain = document.createElement("span");
      plain.className = "map-list-item static";
      plain.textContent = `${node.key} \u2014 ${label}`;
      item.append(plain);
    }
    list.append(item);
  }
  host.append(list);
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
  host.innerHTML = '<div class="empty">Loading module graph...</div>';
  $("graph-meta").textContent = "";
  try {
    const limit = level === "file" ? 40 : 24;
    const data = await api(`/api/map?level=${encodeURIComponent(level)}&limit=${limit}`);
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    if (nodes.length === 0) {
      host.innerHTML = '<div class="empty">No internal imports in this view.</div>';
      $("graph-meta").textContent = "0 nodes / 0 edges";
      setGraphHelp("Nothing to draw. Switch view or index a repository with imports.");
      return;
    }
    let shown;
    try {
      shown = renderMapSvg(host, data);
      setGraphHelp("Click a box to open its file. Tab to a box, then press Enter.");
    } catch (renderError) {
      shown = renderMapList(host, data);
      setGraphHelp("List view. Activate an entry to open its file.");
    }
    window.requestAnimationFrame(scrollGraphToStart);
    $("graph-meta").textContent = [
      `${shown} nodes`,
      `${edges.length} edges`,
      data.omitted ? `${data.omitted} omitted` : "",
    ].filter(Boolean).join(" / ");
  } catch (err) {
    host.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
    $("graph-meta").textContent = "graph unavailable";
    status(`graph: ${err.message}`, true);
  } finally {
    host.setAttribute("aria-busy", "false");
  }
}

function setGraphExpanded(expanded) {
  const pane = $("graph-pane");
  pane.classList.toggle("maximized", expanded);
  const collapse = $("graph-collapse");
  if (expanded) {
    pane.classList.remove("minimized");
    collapse.setAttribute("aria-expanded", "true");
    setButtonLabel(collapse, "Hide", "Hide module map");
  }
  const button = $("graph-expand");
  button.setAttribute("aria-pressed", String(expanded));
  setButtonLabel(
    button,
    expanded ? "Restore" : "Expand",
    expanded ? "Restore module map" : "Expand module map",
  );
  window.requestAnimationFrame(scrollGraphToStart);
}

function setGraphMinimized(minimized) {
  const pane = $("graph-pane");
  if (minimized) setGraphExpanded(false);
  pane.classList.toggle("minimized", minimized);
  const button = $("graph-collapse");
  button.setAttribute("aria-expanded", String(!minimized));
  setButtonLabel(
    button,
    minimized ? "Show" : "Hide",
    minimized ? "Show module map" : "Hide module map",
  );
  if (!minimized) window.requestAnimationFrame(scrollGraphToStart);
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
    if (event.key !== "Escape" || event.defaultPrevented) return;
    const dialog = $("help-dialog");
    if (dialog && dialog.open) return;
    const pane = $("graph-pane");
    if (pane && pane.classList.contains("maximized")) {
      setGraphExpanded(false);
      status("module map restored");
    }
  });
}

// -- init --------------------------------------------------------------

async function init() {
  initHelp();
  initExamples();
  initShortcuts();
  setButtonLabel($("graph-expand"), "Expand", "Expand module map");
  setButtonLabel($("graph-collapse"), "Hide", "Hide module map");
  $("graph-expand").addEventListener("click", () => {
    const expanded = !$("graph-pane").classList.contains("maximized");
    setGraphExpanded(expanded);
    status(expanded ? "module map expanded - Esc restores it" : "module map restored");
  });
  $("graph-collapse").addEventListener("click", () => {
    const minimized = !$("graph-pane").classList.contains("minimized");
    setGraphMinimized(minimized);
    status(minimized ? "module map hidden" : "module map shown");
  });
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("ask-input").value.trim();
    if (question) ask(question);
  });
  $("tree-filter").addEventListener("input", scheduleTreeFilter);
  $("tree-filter").addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !$("tree-filter").value) return;
    event.preventDefault();
    $("tree-filter").value = "";
    applyTreeFilter();
    status("filter cleared");
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
  $("answer").addEventListener("click", (event) => {
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
    $("tree").innerHTML = `<div class="empty error">${escapeHtml(err.message)}</div>`;
    $("graph").innerHTML = `<div class="empty error">${escapeHtml(err.message)}</div>`;
  }
}

init();
