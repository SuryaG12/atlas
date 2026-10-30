"use strict";

// Atlas M3 client. Read-only: it only GETs the index and POSTs questions.

const state = {
  meta: null,
  files: [],
  activePath: null,
  mapLevel: "file",
  graphSeq: 0,
  mermaidReady: false,
};

const $ = (id) => document.getElementById(id);

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
  $("file-count").textContent = `${state.files.length}`;
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

function applyTreeFilter() {
  const query = $("tree-filter").value.trim().toLowerCase();
  const rows = [...document.querySelectorAll(".tree-file")];
  for (const row of rows) {
    row.hidden = query !== "" && !row.dataset.path.toLowerCase().includes(query);
  }
  const details = [...document.querySelectorAll("#tree details")].reverse();
  for (const item of details) {
    const hasVisible = Boolean(item.querySelector(".tree-file:not([hidden])"));
    item.hidden = !hasVisible;
    if (query && hasVisible) item.open = true;
  }
}

function setActiveTreeRow(path) {
  for (const row of document.querySelectorAll(".tree-file.active")) {
    row.classList.remove("active");
  }
  const row = [...document.querySelectorAll(".tree-file")]
    .find((item) => item.dataset.path === path);
  if (row) {
    row.classList.add("active");
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
      data.truncated ? "truncated" : "",
    ].filter(Boolean).join(" / ");
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
    `(?<![A-Za-z0-9_./:-])(${labels.join("|")})(?![A-Za-z0-9_./:-])`,
    "g",
  );
  return html.replace(pattern, (label) => citationMarkup(label) || escapeHtml(label));
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
  html = html.replace(/\u0000(\d+)\u0000/g, (_match, index) => `<code>${codes[Number(index)]}</code>`);
  return linkCitations(html, citations);
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

function renderAnswer(payload) {
  const host = resetAnswer();
  if (payload.mode === "answer") {
    const body = document.createElement("div");
    const citations = (payload.citations && payload.citations.valid) || [];
    body.innerHTML = markdown(payload.answer, citations);
    host.append(body);

    if (!payload.trusted) {
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
      `${citations.length} citations`,
      model,
    ]) {
      const span = document.createElement("span");
      span.className = "mono";
      span.textContent = text;
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

async function ask(question) {
  const button = $("ask-button");
  button.disabled = true;
  const host = resetAnswer();
  const pending = document.createElement("p");
  pending.className = "hint";
  pending.textContent = "retrieving and asking the local model...";
  host.append(pending);
  status("asking...");
  try {
    const payload = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    renderAnswer(payload);
    status(payload.mode === "refused" ? "no answer found" : `answer ready (${payload.mode})`);
  } catch (err) {
    resetAnswer().innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
    status(err.message, true);
  } finally {
    button.disabled = false;
  }
}

// -- module graph ------------------------------------------------------

function ensureMermaid() {
  if (state.mermaidReady) return true;
  if (!window.mermaid || typeof window.mermaid.render !== "function") return false;
  window.mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    themeVariables: {
      fontFamily: "JetBrains Mono, Consolas, monospace",
      fontSize: "11px",
      background: "#0E1524",
      primaryColor: "#121A2B",
      primaryTextColor: "#E6EDF3",
      primaryBorderColor: "#2A3B55",
      secondaryColor: "#121A2B",
      tertiaryColor: "#0E1524",
      lineColor: "#8B98AD",
      textColor: "#E6EDF3",
      mainBkg: "#121A2B",
      nodeBorder: "#2A3B55",
      clusterBkg: "#0E1524",
      clusterBorder: "#1E2A40",
      edgeLabelBackground: "#0A0F1A",
      titleColor: "#E6EDF3",
    },
    flowchart: {
      htmlLabels: true,
      curve: "basis",
      useMaxWidth: false,
      nodeSpacing: 22,
      rankSpacing: 34,
      padding: 6,
    },
  });
  state.mermaidReady = true;
  return true;
}

async function loadGraph(level) {
  state.mapLevel = level;
  const host = $("graph");
  host.innerHTML = '<div class="empty">Loading module graph...</div>';
  $("graph-meta").textContent = "";
  try {
    const limit = level === "file" ? 40 : 24;
    const data = await api(`/api/map?level=${encodeURIComponent(level)}&limit=${limit}`);
    if (!ensureMermaid()) throw new Error("Mermaid failed to load");
    // The backend map is left-to-right; a narrow pane reads better top-down.
    const source = String(data.mermaid || "")
      .replace(/^graph\s+LR\b/, "graph TD")
      .replace(/<br\/>/g, " / ");
    const { svg } = await window.mermaid.render(`atlas-graph-${state.graphSeq += 1}`, source);
    host.innerHTML = svg;
    const element = host.querySelector("svg");
    if (element) {
      element.style.maxWidth = "none";
      element.style.height = "auto";
    }
    host.scrollTop = 0;
    host.scrollLeft = 0;
    const firstNode = host.querySelector("g.node");
    if (firstNode) {
      const nodeBox = firstNode.getBoundingClientRect();
      const hostBox = host.getBoundingClientRect();
      host.scrollLeft += nodeBox.left - hostBox.left - 8;
      host.scrollTop += nodeBox.top - hostBox.top - 8;
    }
    $("graph-meta").textContent = [
      `${data.nodes.length} nodes`,
      `${data.edges.length} edges`,
      data.omitted ? `${data.omitted} omitted` : "",
    ].filter(Boolean).join(" / ");
  } catch (err) {
    host.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
    $("graph-meta").textContent = "graph unavailable";
    status(`graph: ${err.message}`, true);
  }
}

function setMapLevel(level) {
  for (const button of document.querySelectorAll("#map-level button")) {
    const active = button.dataset.level === level;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  loadGraph(level);
}

// -- cursor ------------------------------------------------------------

function initCursor() {
  if (window.matchMedia("(pointer: coarse)").matches) return;
  document.body.classList.add("custom-cursor");
  const cursor = $("cursor");
  const allowed = (target) => {
    if (!target || !target.closest) return false;
    if (target.closest(".source, .answer, .ask-form, .tree-filter")) return false;
    return Boolean(target.closest(".shell, .pane-head, .tree-pane, .graph-pane, .seg, .statusbar"));
  };
  document.addEventListener("pointermove", (event) => {
    cursor.style.transform = `translate(${event.clientX}px, ${event.clientY}px) translate(-50%, -50%)`;
    cursor.classList.toggle("on", allowed(event.target));
    cursor.classList.toggle(
      "hot",
      Boolean(event.target.closest && event.target.closest("button, a, summary, .tree-file, .seg")),
    );
  });
  document.addEventListener("pointerdown", () => cursor.classList.add("down"));
  document.addEventListener("pointerup", () => cursor.classList.remove("down"));
  document.documentElement.addEventListener("pointerleave", () => cursor.classList.remove("on"));
}

// -- init --------------------------------------------------------------

async function init() {
  initCursor();
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("ask-input").value.trim();
    if (question) ask(question);
  });
  $("tree-filter").addEventListener("input", applyTreeFilter);
  document.querySelectorAll("#map-level button").forEach((button) => {
    button.addEventListener("click", () => setMapLevel(button.dataset.level));
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
    loadGraph(meta.mapLevel || "file");
  } catch (err) {
    status(err.message, true);
    $("tree").innerHTML = `<div class="empty error">${escapeHtml(err.message)}</div>`;
    $("graph").innerHTML = `<div class="empty error">${escapeHtml(err.message)}</div>`;
  }
}

init();
