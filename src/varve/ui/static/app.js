// varve read-only web UI. Vanilla JS, no build step, no network assets.
"use strict";

const POLL_MS = 1500;
const SVG_NS = "http://www.w3.org/2000/svg";
const EMPTY_KEY = "_";

// ---------------------------------------------------------------- DOM helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// Replace children; accepts nested arrays and skips null/false like h().
function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- formatting

const DASH = "—";

function fmtMs(ms) {
  if (ms === null || ms === undefined) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 2 : 1)} s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${String(Math.round(sec % 60)).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function fmtBytes(n) {
  if (n === null || n === undefined) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(n < 10 ? 1 : 0) : n} ${units[i]}`;
}

function fmtNum(n, digits = 0) {
  if (n === null || n === undefined) return DASH;
  if (typeof n !== "number") return String(n);
  if (!Number.isInteger(n) && digits === 0) digits = Math.abs(n) < 10 ? 2 : 1;
  return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function fmtRate(v) {
  if (v === null || v === undefined) return DASH;
  return v >= 100 ? fmtNum(Math.round(v)) : fmtNum(v, v >= 10 ? 1 : 2);
}

function fmtMb(v) { return v === null || v === undefined ? DASH : `${fmtNum(v, 0)} MB`; }

function parseTime(iso) {
  if (!iso) return null;
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(iso);
  const t = Date.parse(hasZone ? iso : `${iso}Z`);
  return Number.isNaN(t) ? null : t;
}

function fmtAgo(iso) {
  const t = parseTime(iso);
  if (t === null) return DASH;
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
  return `${Math.round(sec / 86400)} d ago`;
}

function fmtTime(iso) {
  const t = parseTime(iso);
  if (t === null) return DASH;
  const d = new Date(t);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function shortDate(iso) {
  const t = parseTime(iso);
  if (t === null) return "";
  const d = new Date(t);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function shortHash(x) { return x ? String(x).slice(0, 12) : DASH; }

function compact(value) {
  if (value === null || value === undefined) return DASH;
  if (typeof value === "string") return value;
  const text = JSON.stringify(value);
  return text.length > 400 ? `${text.slice(0, 400)}…` : text;
}

// ---------------------------------------------------------------- shared widgets

const STATUS_ORDER = ["ok", "failed", "running", "orphaned"];

function statusChips(counts) {
  const keys = [...STATUS_ORDER, ...Object.keys(counts || {}).filter((k) => !STATUS_ORDER.includes(k))];
  return keys.filter((k) => counts && counts[k]).map((k) =>
    h("span", { class: "chip", title: k }, h("i", { class: `dot-${k}` }), `${k} ${fmtNum(counts[k])}`));
}

function stateChip(state) {
  const text = { empty: "not run", partial: "partial" }[state] || state;
  return h("span", { class: "chip" }, h("i", { class: `dot-${state}` }), text);
}

function progressBar(ok, failed, running, total) {
  const pct = (n) => (total ? `${(100 * n) / total}%` : "0");
  return h("div", { class: "bar", title: `${ok} ok, ${failed} failed, ${running} running of ${total}` },
    h("span", { class: "b-ok", style: `width:${pct(ok)}` }),
    h("span", { class: "b-failed", style: `width:${pct(failed)}` }),
    h("span", { class: "b-running", style: `width:${pct(running)}` }));
}

function card(k, v, title) { return h("div", { class: "card", title: title || k }, h("div", { class: "k" }, k), h("div", { class: "v" }, v)); }

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter(Boolean).map(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
}

const tip = {
  show(evt, content) {
    const el = $("tip");
    fill(el, content);
    el.hidden = false;
    const pad = 14;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    const r = el.getBoundingClientRect();
    if (x + r.width > window.innerWidth) x = evt.clientX - r.width - pad;
    if (y + r.height > window.innerHeight) y = evt.clientY - r.height - pad;
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
  },
  hide() { $("tip").hidden = true; },
};

// ---------------------------------------------------------------- API and routing

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON error */ }
  if (!res.ok) throw new Error((body && body.error) || `${res.status} ${res.statusText}`);
  return body;
}

const enc = encodeURIComponent;
const branchApi = (p, b) => `/api/pipelines/${enc(p)}/branches/${enc(b)}`;
const unitToken = (key) => (key === "" ? EMPTY_KEY : key);

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "p" && parts.length >= 3) {
    const route = { view: "branch", pipeline: parts[1], branch: parts[2], stage: null, unit: null };
    if (parts[3] === "s" && parts[4] !== undefined) route.stage = parts[4];
    if (parts[5] === "u" && parts[6] !== undefined) route.unit = parts[6] === EMPTY_KEY ? "" : parts[6];
    return route;
  }
  return { view: "workspace" };
}

function href(pipeline, branch, stage, unit) {
  let out = `#/p/${enc(pipeline)}/${enc(branch)}`;
  if (stage !== undefined && stage !== null) out += `/s/${enc(stage)}`;
  if (unit !== undefined && unit !== null) out += `/u/${enc(unitToken(unit))}`;
  return out;
}

const state = {
  workspace: null,      // last /api/workspace payload
  routeKey: null,       // pipeline/branch currently rendered
  branch: null,         // last branch payload
  history: { stage: "", limit: 50 },
  units: { status: "", q: "", sort: "key", page: 1, page_size: 50, error_type: null, error_summary: null },
  unitsKey: null,       // stage whose unit filters are held in `units`
  openGroups: new Set(),  // expanded failure groups, kept across polling re-renders
  pollTimer: null,
  expandedRuns: new Set(),
};

function setLive(on) {
  $("live").hidden = !on;
}

function stamp() { $("stamp").textContent = `updated ${new Date().toLocaleTimeString()}`; }

function schedulePoll(fn, active) {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  setLive(active);
  if (active) state.pollTimer = setTimeout(fn, POLL_MS);
}

function crumbs(items) {
  const nav = $("crumbs");
  fill(nav);
  items.forEach((item, i) => {
    if (i) nav.append(h("span", { class: "sep" }, "/"));
    nav.append(item.href ? h("a", { href: item.href }, item.text) : h("span", null, item.text));
  });
}

function showError(target, err) {
  fill(target, h("p", { class: "error" }, String(err.message || err)));
}

// ---------------------------------------------------------------- workspace view

async function renderWorkspace() {
  state.routeKey = null;
  closeDrawer(false);
  crumbs([{ text: "workspace" }]);
  const view = $("view");
  let data;
  try {
    data = await api("/api/workspace");
  } catch (err) { showError(view, err); return; }
  if (parseRoute().view !== "workspace") return;
  state.workspace = data;
  stamp();

  const rows = [];
  let active = false;
  for (const p of data.pipelines) {
    const branches = p.branches.length ? p.branches : [null];
    branches.forEach((b, i) => {
      const cells = [];
      if (i === 0) {
        cells.push(h("td", { class: "pipe", rowspan: branches.length },
          h("div", null, p.name), h("div", { class: "muted mono", title: p.db_path }, p.module && p.qualname ? `${p.module}:${p.qualname}` : p.root),
          p.error ? h("div", { class: "error" }, p.error) : null));
      }
      if (!b) {
        cells.push(h("td", { colspan: 7, class: "muted" }, p.error ? "unreadable" : "no branches yet"));
        rows.push(h("tr", null, cells));
        return;
      }
      if (b.active) active = true;
      const c = b.counts || {};
      const last = b.last_run;
      cells.push(
        h("td", null, h("a", { href: href(p.id, b.name) }, b.name),
          b.is_temp ? h("span", { class: "chip label" }, "temp") : null,
          b.manual ? h("span", { class: "chip label" }, "manual") : null),
        h("td", null, statusChips(c)),
        h("td", null, progressBar(c.ok || 0, c.failed || 0, c.running || 0, b.total_units),
          h("div", { class: "muted num", style: "text-align:left" }, `${fmtNum(c.ok || 0)} / ${fmtNum(b.total_units)}`)),
        h("td", null, last ? [fmtAgo(last.started_at), " ", h("span", { class: `st-${last.status === "ok" ? "ok" : last.status === "running" ? "running" : "failed"}` }, last.status)] : DASH),
        h("td", { class: "num" }, fmtMs(b.last_duration_ms)),
        h("td", { class: "num" }, fmtBytes(b.artifact_bytes)),
        h("td", null, b.active
          ? h("span", { class: "st-running" }, `running · ${fmtNum(b.active.done)} done, ${fmtNum(b.active.running)} active`)
          : h("span", { class: "muted" }, "idle")),
      );
      rows.push(h("tr", null, cells));
    });
  }

  const parts = [];
  if (data.edges.length) parts.push(lineagePanel(data));
  parts.push(h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", null, "Pipelines"), h("span", { class: "grow" }),
      h("span", { class: "muted" }, `${data.pipelines.length} state databases`)),
    data.pipelines.length
      ? h("table", null,
        h("thead", null, h("tr", null, ["Pipeline", "Branch", "Units", "Progress", "Last run", "Last duration", "Artifacts", "Activity"].map((t, i) => h("th", { class: i >= 5 && i <= 6 ? "num" : null }, t)))),
        h("tbody", null, rows))
      : h("p", { class: "muted" }, "No .varve/state.db found. Run a pipeline first.")));
  fill(view, ...parts);
  schedulePoll(() => { if (parseRoute().view === "workspace") renderWorkspace(); }, active);
}

function lineagePanel(data) {
  const names = Object.fromEntries(data.pipelines.map((p) => [p.id, p.name]));
  const nodes = new Map();
  const edges = [];
  const add = (id, label) => { if (!nodes.has(id)) nodes.set(id, { name: id, label, deps: [] }); return nodes.get(id); };
  for (const e of data.edges) {
    const src = e.source ? add(e.source, names[e.source]) : add(`?${e.source_name}`, `${e.source_name} (not found)`);
    const dst = add(e.target, names[e.target]);
    dst.deps.push({ stage: src.name, kind: "data" });
    edges.push({ from: src.name, to: dst.name, kind: "data", label: `${e.source_stage || ""}${e.branches && e.branches.length ? ` @${e.branches.join(",")}` : ""}` });
  }
  const list = [...nodes.values()];
  const svg = drawDag(list.map((n) => ({ ...n, display: n.label })), edges, {
    nodeH: 34, render: (g, n, w) => {
      g.append(s("text", { x: 10, y: 21 }, truncate(n.display || n.name, 30)));
    },
    onClick: null,
  });
  const det = h("details", { open: true }, h("summary", null, h("h2", { style: "display:inline" }, "Cross-pipeline lineage")),
    h("div", { class: "dag-wrap" }, svg), h("p", { class: "note" }, "Edges follow Ref declarations recorded by each pipeline (upstream → downstream)."));
  return h("section", { class: "panel" }, det);
}

// ---------------------------------------------------------------- DAG drawing

function truncate(text, n) { return text.length > n ? `${text.slice(0, n - 1)}…` : text; }

function layoutLayers(nodes, edges) {
  const byName = new Map(nodes.map((n, i) => [n.name, { ...n, index: i, layer: 0, parents: [] }]));
  for (const e of edges) if (byName.has(e.to) && byName.has(e.from)) byName.get(e.to).parents.push(e.from);
  // Longest-path layering; nodes are expected in topological order, iterate to fixpoint for safety.
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const n of byName.values()) {
      for (const p of n.parents) {
        const want = byName.get(p).layer + 1;
        if (want > n.layer && want < nodes.length) { n.layer = want; changed = true; }
      }
    }
    if (!changed) break;
  }
  const layers = [];
  for (const n of byName.values()) (layers[n.layer] = layers[n.layer] || []).push(n);
  // One barycenter pass: order each layer by the mean position of its parents.
  layers.forEach((layer, li) => {
    if (li === 0) { layer.sort((a, b) => a.index - b.index); }
    else {
      const pos = new Map(layers[li - 1].map((n, i) => [n.name, i]));
      const key = (n) => {
        const ps = n.parents.filter((p) => pos.has(p));
        return ps.length ? ps.reduce((a, p) => a + pos.get(p), 0) / ps.length : n.index;
      };
      layer.sort((a, b) => key(a) - key(b) || a.index - b.index);
    }
    layer.forEach((n, i) => { n.row = i; });
  });
  return { byName, layers };
}

function drawDag(nodes, edges, opts) {
  const nodeW = opts.nodeW || 200, nodeH = opts.nodeH || 66, gapX = 56, gapY = 14, pad = 8;
  const usable = edges.filter((e) => nodes.some((n) => n.name === e.from) && nodes.some((n) => n.name === e.to));
  const pos = new Map();
  let width, height;
  if (usable.length) {
    const { byName, layers } = layoutLayers(nodes, usable);
    const maxRows = Math.max(...layers.map((l) => (l ? l.length : 0)));
    for (const n of byName.values()) {
      const rows = layers[n.layer].length;
      const offset = ((maxRows - rows) * (nodeH + gapY)) / 2;
      pos.set(n.name, { x: pad + n.layer * (nodeW + gapX), y: pad + offset + n.row * (nodeH + gapY) });
    }
    width = pad * 2 + layers.length * nodeW + (layers.length - 1) * gapX;
    height = pad * 2 + maxRows * nodeH + (maxRows - 1) * gapY;
  } else {
    // No edges recorded: wrap stages in execution order.
    const avail = Math.max(nodeW, ($("view").clientWidth || 1000) - 60);
    const cols = Math.max(1, Math.min(nodes.length, Math.floor((avail + gapX) / (nodeW + gapX))));
    nodes.forEach((n, i) => pos.set(n.name, { x: pad + (i % cols) * (nodeW + gapX), y: pad + Math.floor(i / cols) * (nodeH + gapY) }));
    const rows = Math.ceil(nodes.length / cols);
    width = pad * 2 + cols * nodeW + (cols - 1) * gapX;
    height = pad * 2 + rows * nodeH + (rows - 1) * gapY;
  }
  const svg = s("svg", { class: "dag", width, height, viewBox: `0 0 ${width} ${height}` },
    s("defs", null, s("marker", { id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto" },
      s("path", { d: "M0,0 L8,4 L0,8 z" }))));
  for (const e of usable) {
    const a = pos.get(e.from), b = pos.get(e.to);
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2, x2 = b.x - 2, y2 = b.y + nodeH / 2;
    const mx = (x1 + x2) / 2;
    const path = s("path", { class: `edge ${e.kind || "data"}`, d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, "marker-end": "url(#arrow)" });
    path.append(s("title", null, `${e.from} → ${e.to}${e.kind && e.kind !== "data" ? ` (${e.kind})` : ""}${e.label ? ` ${e.label}` : ""}`));
    svg.append(path);
  }
  for (const n of nodes) {
    const p = pos.get(n.name);
    const g = s("g", { class: `node${opts.selected === n.name ? " sel" : ""}`, transform: `translate(${p.x},${p.y})`,
      onclick: opts.onClick ? () => opts.onClick(n) : null });
    g.append(s("rect", { class: "box", width: nodeW, height: nodeH, rx: 5 }));
    opts.render(g, n, nodeW, nodeH);
    svg.append(g);
  }
  return svg;
}

const STATE_COLOR = { ok: "var(--ok)", failed: "var(--failed)", running: "var(--running)", partial: "var(--partial)", empty: "var(--muted)" };

function renderStageNode(g, st, w) {
  g.append(s("rect", { x: 0, y: 0, width: 4, height: 66, rx: 2, fill: STATE_COLOR[st.state] || "var(--muted)" }));
  g.append(s("text", { x: 12, y: 18 }, truncate(st.name, 26)));
  // A stage_runs row in status "running" may announce the full unit count before rows exist.
  const live = st.last && st.last.status === "running" ? st.last.total_units || 0 : 0;
  const denom = Math.max(st.total, live);
  const ratio = denom ? `${fmtNum(st.ok)} / ${fmtNum(denom)}` : "no units";
  g.append(s("text", { class: "small", x: 12, y: 34 }, ratio));
  const tail = st.state === "running" ? `running ${fmtNum(st.running)}`
    : st.state === "failed" ? `${fmtNum(st.failed)} failed`
      : st.state === "empty" ? (st.declared === false ? "removed" : "not run") : "";
  g.append(s("text", { class: "small", x: w - 8, y: 34, "text-anchor": "end", fill: st.state === "failed" ? "var(--failed-text)" : null }, tail));
  const barW = w - 20;
  g.append(s("rect", { class: "track", x: 12, y: 42, width: barW, height: 4, rx: 2 }));
  if (denom) {
    let x = 12;
    for (const [n, color] of [[st.ok, "var(--ok)"], [st.failed, "var(--failed)"], [st.running, "var(--running)"]]) {
      if (!n) continue;
      const bw = (barW * n) / denom;
      g.append(s("rect", { x, y: 42, width: bw, height: 4, fill: color }));
      x += bw;
    }
  }
  const last = st.last;
  const info = last ? `${fmtMs(last.wall_ms)} · ${last.executed_units}/${last.total_units} run` : "";
  g.append(s("text", { class: "small", x: 12, y: 59 }, info));
  if (st.version) g.append(s("text", { class: "small", x: w - 8, y: 59, "text-anchor": "end" }, `v=${truncate(String(st.version), 10)}`));
}

// ---------------------------------------------------------------- branch view

async function renderBranch(route, { poll = false } = {}) {
  const key = `${route.pipeline}\u0000${route.branch}`;
  const fresh = state.routeKey !== key;
  if (fresh) {
    state.routeKey = key;
    state.branch = null;
    state.history = { stage: "", limit: 50 };
    state.expandedRuns = new Set();
    $("view").replaceChildren(h("p", { class: "muted" }, "Loading…"));
  }
  let data;
  try {
    data = await api(branchApi(route.pipeline, route.branch));
  } catch (err) { showError($("view"), err); schedulePoll(null, false); return; }
  if (state.routeKey !== key) return;
  const isLive = (b) => !!b && (b.running_units > 0 || !!b.active_run);
  const wasRunning = isLive(state.branch);
  state.branch = data;
  stamp();
  crumbs([{ text: "workspace", href: "#/" }, { text: data.pipeline.name }, { text: route.branch }]);

  if (fresh) buildBranchPage(route, data);
  else updateBranchPage(route, data);
  if (fresh || (wasRunning && !isLive(data))) loadHistory(route);

  const current = parseRoute();
  if (current.stage !== null && current.stage !== undefined) {
    if (current.unit !== null && current.unit !== undefined) { if (!poll || fresh) openUnit(current); }
    else openStage(current, { quiet: poll && !fresh });
  } else closeDrawer(false);

  schedulePoll(() => {
    const r = parseRoute();
    if (r.view === "branch" && `${r.pipeline}\u0000${r.branch}` === key) renderBranch(r, { poll: true });
  }, isLive(data));
}

function buildBranchPage(route, data) {
  const view = $("view");
  fill(view,
    h("div", { class: "pagehead", id: "bhead" }),
    h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", null, "Stages"), h("span", { class: "grow" }), h("span", { id: "dagmeta", class: "muted" })),
      h("div", { class: "dag-wrap", id: "dag" }),
      h("p", { class: "note", id: "dagnote" })),
    h("section", { class: "panel", id: "history" }, h("p", { class: "muted" }, "Loading history…")),
  );
  updateBranchPage(route, data);
  ensureWorkspace().then(() => updateHead(route, state.branch));
}

async function ensureWorkspace() {
  if (state.workspace) return;
  try { state.workspace = await api("/api/workspace"); } catch (_) { /* branch switcher is optional */ }
}

function updateHead(route, data) {
  const head = $("bhead");
  if (!head || !data) return;
  const p = data.pipeline, b = data.branch;
  const ws = state.workspace && state.workspace.pipelines.find((x) => x.id === route.pipeline);
  const branchNames = ws ? ws.branches.map((x) => x.name) : [b.name];
  const select = h("select", { onchange: (e) => { location.hash = href(route.pipeline, e.target.value); } },
    branchNames.map((n) => { const o = h("option", { value: n }, n); if (n === b.name) o.selected = true; return o; }));
  const totals = { ok: 0, failed: 0, running: 0, orphaned: 0 };
  let total = 0;
  for (const st of data.stages) { for (const k of Object.keys(totals)) totals[k] += st[k] || 0; total += st.total; }
  const last = data.last_run;
  fill(head,
    h("h1", null, p.name),
    h("span", null, "branch ", select),
    b.is_temp ? h("span", { class: "chip label" }, "temp") : null,
    b.manual ? h("span", { class: "chip label" }, "manual") : null,
    h("span", { class: "sub mono", title: p.db_path }, p.root),
    h("span", { class: "grow", style: "flex:1" }),
    statusChips(totals),
    h("span", { class: "muted" }, `${fmtNum(total)} units`),
    data.active_run
      ? h("span", { class: "st-running" }, `run ${data.active_run.run_id.slice(0, 8)} · ${fmtNum(data.active_run.done)} done, ${fmtNum(data.running_units)} running`)
      : last ? h("span", { class: "muted" }, `last run ${fmtAgo(last.started_at)} · ${last.status} · ${fmtMs(last.total_wall_ms)}`) : h("span", { class: "muted" }, "never run"),
  );
}

function updateBranchPage(route, data) {
  updateHead(route, data);
  const selected = parseRoute().stage;
  const svg = drawDag(data.stages, data.edges, {
    render: renderStageNode,
    selected,
    onClick: (st) => { location.hash = href(route.pipeline, route.branch, st.name); },
  });
  $("dag").replaceChildren(svg);
  $("dagmeta").textContent = `${data.stages.length} stages`;
  $("dagnote").textContent =
    "Solid: data dependency · dashed: needs= ordering · dotted: dynamic-axis resolver. Click a stage for units, failures and metrics.";
}

// ---------------------------------------------------------------- history panel

async function loadHistory(route) {
  const panel = $("history");
  if (!panel) return;
  const q = new URLSearchParams({ limit: state.history.limit });
  if (state.history.stage) q.set("stage", state.history.stage);
  let data;
  try {
    data = await api(`${branchApi(route.pipeline, route.branch)}/history?${q}`);
  } catch (err) { showError(panel, err); return; }
  if (!$("history")) return;
  renderHistory(route, data);
}

function renderHistory(route, data) {
  const panel = $("history");
  const stageSel = h("select", { onchange: (e) => { state.history.stage = e.target.value; loadHistory(route); } },
    h("option", { value: "" }, "all stages"),
    data.stages.map((n) => { const o = h("option", { value: n }, n); if (n === state.history.stage) o.selected = true; return o; }));
  const limitSel = h("select", { onchange: (e) => { state.history.limit = Number(e.target.value); loadHistory(route); } },
    [20, 50, 100, 200].map((n) => { const o = h("option", { value: n }, `last ${n}`); if (n === state.history.limit) o.selected = true; return o; }));
  const runs = data.runs.slice().reverse(); // chronological for charts
  const unit = data.throughput_unit;
  const rate = (r) => (unit === "items/s" ? r.items_per_s : r.units_per_s);

  const charts = h("div", { class: "charts" },
    chartBox("Wall time per run", wallChart(runs),
      h("div", { class: "legend" }, h("span", null, h("i", { class: "solid" }), "full run (all units executed)"), h("span", null, h("i", { class: "hollow" }), "partial run, executed / total"))),
    chartBox(`Per-unit time${data.stage ? ` · ${data.stage}` : ""}`, data.stage ? latencyChart(runs) : emptyChart("Select a stage to see unit p50 / p95"),
      data.stage ? h("div", { class: "legend" }, h("span", null, h("i", { class: "solid" }), "p50"), h("span", null, h("i", { class: "l2" }), "p95")) : h("div", { class: "legend" }, h("span", null, " "))),
    chartBox(`Throughput (${unit})`, seriesChart(runs, rate, fmtRate, unit), h("div", { class: "legend" }, h("span", null, unit === "items/s" ? "from ctx.metric(items=…)" : "no items metric reported; executed units per second"))),
  );

  const rows = [];
  for (const r of data.runs) {
    const expanded = state.expandedRuns.has(r.run_id);
    const toggle = () => {
      if (state.expandedRuns.has(r.run_id)) state.expandedRuns.delete(r.run_id); else state.expandedRuns.add(r.run_id);
      renderHistory(route, data);
    };
    rows.push(h("tr", { class: "click", onclick: toggle },
      h("td", null, expanded ? "▾ " : "▸ ", fmtTime(r.started_at)),
      h("td", { class: "mono" }, r.git_commit ? `${r.git_commit.slice(0, 8)}${r.git_dirty ? "*" : ""}` : DASH),
      h("td", null, h("span", { class: `st-${r.status === "ok" ? "ok" : r.status === "running" ? "running" : "failed"}` }, r.status)),
      h("td", { class: "num" }, fmtNum(r.workers)),
      h("td", { class: "num" }, fmtNum(r.executed)),
      h("td", { class: "num" }, fmtNum(r.hit)),
      h("td", { class: "num" }, r.failed ? h("span", { class: "st-failed" }, fmtNum(r.failed)) : fmtNum(r.failed)),
      h("td", null, r.kind === "full" ? "full" : r.kind === "partial" ? `partial ${fmtNum(r.executed)}/${fmtNum(r.total)}` : DASH),
      h("td", { class: "num" }, fmtMs(r.wall_ms)),
      h("td", { class: "num" }, rate(r) === null || rate(r) === undefined ? DASH : `${fmtRate(rate(r))}`),
      h("td", { class: "num" }, fmtMb(r.peak_rss_mb)),
    ));
    if (expanded) {
      const cell = h("td", { colspan: 11 }, h("span", { class: "muted" }, "Loading…"));
      rows.push(h("tr", { class: "sub" }, cell));
      api(`/api/runs/${enc(r.run_id)}?pipeline=${enc(route.pipeline)}`).then((d) => fill(cell, runBreakdown(d))).catch((err) => showError(cell, err));
    }
  }
  fill(panel,
    h("div", { class: "panel-head" }, h("h2", null, "Run history"), h("span", { class: "grow" }), stageSel, limitSel),
    runs.length ? charts : null,
    runs.length
      ? h("div", { style: "max-height:420px;overflow:auto" }, h("table", null,
        h("thead", null, h("tr", null, ["Started", "Commit", "Status", "Workers", "Executed", "Hit", "Failed", "Coverage", "Wall time", unit, "Peak RSS"].map((t, i) => h("th", { class: [3, 4, 5, 6, 8, 9, 10].includes(i) ? "num" : null }, t)))),
        h("tbody", null, rows)))
      : h("p", { class: "muted" }, "No runs recorded yet."),
    h("p", { class: "note" }, "* after a commit: the working tree was dirty. Wall time of partial runs is not connected so cache hits do not distort the trend."),
  );
}

function runBreakdown(d) {
  const r = d.run;
  return h("div", null,
    kv([["run", h("span", { class: "mono" }, r.run_id)], ["host", r.host], ["argv", h("span", { class: "mono" }, (r.argv || []).join(" "))],
      ["commit", r.git_commit ? `${r.git_commit}${r.git_dirty ? " (dirty)" : ""}` : DASH], ["ended", fmtTime(r.ended_at)]]),
    h("table", null,
      h("thead", null, h("tr", null, ["Stage", "Status", "Exec", "Hit", "Failed", "Total", "Workers", "Wall", "p50", "p95", "max", "Peak RSS", "Throughput"].map((t, i) => h("th", { class: i >= 2 ? "num" : null }, t)))),
      h("tbody", null, d.stages.map((st) => h("tr", null,
        h("td", null, st.stage_name), h("td", null, h("span", { class: st.status === "ok" ? "st-ok" : "st-failed" }, st.status)),
        h("td", { class: "num" }, fmtNum(st.executed_units)), h("td", { class: "num" }, fmtNum(st.hit_units)),
        h("td", { class: "num" }, fmtNum(st.failed_units)), h("td", { class: "num" }, fmtNum(st.total_units)),
        h("td", { class: "num" }, fmtNum(st.workers)), h("td", { class: "num" }, fmtMs(st.wall_ms)),
        h("td", { class: "num" }, fmtMs(st.unit_p50_ms)), h("td", { class: "num" }, fmtMs(st.unit_p95_ms)), h("td", { class: "num" }, fmtMs(st.unit_max_ms)),
        h("td", { class: "num" }, fmtMb(st.peak_rss_mb)),
        h("td", { class: "num" }, st.items_per_s !== null && st.items_per_s !== undefined ? `${fmtRate(st.items_per_s)} items/s` : st.units_per_s !== null && st.units_per_s !== undefined ? `${fmtRate(st.units_per_s)} units/s` : DASH),
      )))));
}

// ---------------------------------------------------------------- charts

const CH = { w: 420, h: 170, l: 48, r: 10, t: 14, b: 22 };

function chartBox(title, svg, legend) { return h("div", { class: "chart" }, h("h3", null, title), legend, svg); }

function emptyChart(text) {
  return s("svg", { viewBox: `0 0 ${CH.w} ${CH.h}` },
    s("text", { class: "empty", x: CH.w / 2, y: CH.h / 2, "text-anchor": "middle" }, text));
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

// Frame with y grid, x date ticks, and a hover layer that snaps to the nearest run.
function chartFrame(runs, maxY, yFmt, tipFor) {
  const top = niceMax(maxY);
  const iw = CH.w - CH.l - CH.r, ih = CH.h - CH.t - CH.b;
  const x = (i) => CH.l + (runs.length <= 1 ? iw / 2 : (iw * i) / (runs.length - 1));
  const y = (v) => CH.t + ih - (ih * v) / top;
  const svg = s("svg", { viewBox: `0 0 ${CH.w} ${CH.h}` });
  const grid = s("g", { class: "grid" });
  for (let k = 0; k <= 4; k++) {
    const v = (top * k) / 4;
    grid.append(s("line", { x1: CH.l, x2: CH.w - CH.r, y1: y(v), y2: y(v) }));
    grid.append(s("text", { x: CH.l - 6, y: y(v) + 3, "text-anchor": "end" }, yFmt(v)));
  }
  svg.append(grid);
  svg.append(s("g", { class: "axis" }, s("line", { x1: CH.l, x2: CH.w - CH.r, y1: CH.t + ih, y2: CH.t + ih })));
  const step = Math.max(1, Math.ceil(runs.length / 6));
  runs.forEach((r, i) => {
    if (i % step === 0 || i === runs.length - 1) svg.append(s("text", { x: x(i), y: CH.h - 6, "text-anchor": "middle" }, shortDate(r.started_at)));
  });
  const marks = s("g");
  svg.append(marks);
  const cross = s("line", { class: "cross", y1: CH.t, y2: CH.t + ih, visibility: "hidden" });
  svg.append(cross);
  const hit = s("rect", { x: CH.l - 6, y: 0, width: iw + 12, height: CH.h, fill: "transparent" });
  hit.addEventListener("mousemove", (evt) => {
    const box = svg.getBoundingClientRect();
    const px = ((evt.clientX - box.left) / box.width) * CH.w;
    let best = 0;
    runs.forEach((_, i) => { if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i; });
    cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); cross.setAttribute("visibility", "visible");
    tip.show(evt, tipFor(runs[best]));
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hide(); });
  svg.append(hit);
  return { svg, marks, x, y };
}

function runTip(r, lines) {
  return [h("div", null, h("b", null, fmtTime(r.started_at)), " ", r.git_commit ? h("span", { class: "mono" }, `${r.git_commit.slice(0, 8)}${r.git_dirty ? "*" : ""}`) : null),
    ...lines.map((l) => h("div", null, l)),
    h("div", { class: "muted" }, `${r.kind === "full" ? "full run" : r.kind === "partial" ? "partial run" : "no stage data"} · executed ${fmtNum(r.executed)} / ${fmtNum(r.total)} · workers ${fmtNum(r.workers)}`)];
}

function linePath(points) {
  return points.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
}

function wallChart(runs) {
  const withData = runs.filter((r) => r.wall_ms !== null && r.wall_ms !== undefined && r.kind !== "none");
  if (!withData.length) return emptyChart("No finished runs");
  const max = Math.max(...withData.map((r) => r.wall_ms));
  const f = chartFrame(runs, max, (v) => fmtMs(v), (r) => runTip(r, [`wall time ${fmtMs(r.wall_ms)}`]));
  const full = [];
  runs.forEach((r, i) => { if (r.kind === "full" && r.wall_ms !== null) full.push([f.x(i), f.y(r.wall_ms)]); });
  if (full.length > 1) f.marks.append(s("path", { class: "line s1", d: linePath(full) }));
  runs.forEach((r, i) => {
    if (r.kind === "none" || r.wall_ms === null || r.wall_ms === undefined) return;
    const cx = f.x(i), cy = f.y(r.wall_ms);
    f.marks.append(s("circle", { class: r.kind === "full" ? "full" : "partial", cx, cy, r: 4 }));
    if (r.kind === "partial") f.marks.append(s("text", { class: "pt-label", x: cx, y: cy - 8, "text-anchor": "middle" }, `${fmtNum(r.executed)}/${fmtNum(r.total)}`));
  });
  return f.svg;
}

function latencyChart(runs) {
  const vals = runs.flatMap((r) => [r.unit_p50_ms, r.unit_p95_ms]).filter((v) => v !== null && v !== undefined);
  if (!vals.length) return emptyChart("No per-unit timings recorded");
  const f = chartFrame(runs, Math.max(...vals), (v) => fmtMs(v), (r) => runTip(r, [`p50 ${fmtMs(r.unit_p50_ms)} · p95 ${fmtMs(r.unit_p95_ms)} · max ${fmtMs(r.unit_max_ms)}`]));
  for (const [field, cls, dot] of [["unit_p95_ms", "s2", "f2"], ["unit_p50_ms", "s1", "f1"]]) {
    const pts = [];
    runs.forEach((r, i) => { if (r[field] !== null && r[field] !== undefined) pts.push([f.x(i), f.y(r[field])]); });
    if (pts.length > 1) f.marks.append(s("path", { class: `line ${cls}`, d: linePath(pts) }));
    for (const p of pts) f.marks.append(s("circle", { class: dot, cx: p[0], cy: p[1], r: 3.5 }));
  }
  return f.svg;
}

function seriesChart(runs, get, fmt, unit) {
  const vals = runs.map(get).filter((v) => v !== null && v !== undefined);
  if (!vals.length) return emptyChart("No executed units");
  const f = chartFrame(runs, Math.max(...vals), fmt, (r) => runTip(r, [`${fmt(get(r))} ${unit}`]));
  const pts = [];
  runs.forEach((r, i) => { const v = get(r); if (v !== null && v !== undefined) pts.push([f.x(i), f.y(v)]); });
  if (pts.length > 1) f.marks.append(s("path", { class: "line s1", d: linePath(pts) }));
  for (const p of pts) f.marks.append(s("circle", { class: "f1", cx: p[0], cy: p[1], r: 3.5 }));
  return f.svg;
}

// ---------------------------------------------------------------- drawer: stage

function closeDrawer(updateHash = true) {
  const d = $("drawer");
  if (!d.hidden) d.hidden = true;
  d.dataset.key = "";
  if (updateHash) {
    const r = parseRoute();
    if (r.view === "branch") location.hash = href(r.pipeline, r.branch);
  }
}

function drawerHead(title, sub, back) {
  return h("div", { class: "drawer-head" },
    back ? h("button", { class: "link", onclick: back, title: "back to stage" }, "←") : null,
    h("h1", null, title), sub,
    h("button", { onclick: () => closeDrawer(true), title: "close (Esc)" }, "✕"));
}

async function openStage(route, { quiet = false } = {}) {
  const d = $("drawer");
  const key = `${route.pipeline}\u0000${route.branch}\u0000${route.stage}`;
  if (state.unitsKey !== key) {
    // Filters survive a round trip to a unit and back, but not a stage switch.
    state.units = { status: "", q: "", sort: "key", page: 1, page_size: 50, error_type: null, error_summary: null };
    state.unitsKey = key;
    state.openGroups = new Set();
  }
  if (d.dataset.key !== key) {
    d.dataset.key = key;
    fill(d, drawerHead(route.stage, null), h("p", { class: "muted" }, "Loading…"));
  }
  d.hidden = false;
  const u = state.units;
  const q = new URLSearchParams({ page: u.page, page_size: u.page_size, sort: u.sort });
  if (u.status) q.set("status", u.status);
  if (u.q) q.set("q", u.q);
  if (u.error_type !== null) { q.set("error_type", u.error_type); q.set("error_summary", u.error_summary); }
  let data;
  try {
    data = await api(`${branchApi(route.pipeline, route.branch)}/stages/${enc(route.stage)}/units?${q}`);
  } catch (err) { if (!quiet) showError(d, err); return; }
  if (d.dataset.key !== key) return;
  const scroll = d.scrollTop;
  const focusedSearch = document.activeElement && document.activeElement.id === "unit-search";
  renderStageDrawer(route, data);
  d.scrollTop = scroll;
  if (focusedSearch) { const el = $("unit-search"); el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
}

const groupKey = (g) => `${g.error_type}\u0000${g.error_summary}`;

function renderStageDrawer(route, data) {
  const d = $("drawer");
  const st = (state.branch && state.branch.stages.find((x) => x.name === route.stage)) || { name: route.stage, state: "empty" };
  const last = st.last;
  const reload = () => openStage(parseRoute());
  const labels = [];
  if (st.version !== undefined && st.version !== null) labels.push(h("span", { class: "chip label" }, `version=${st.version}`));
  if (st.reused_units) labels.push(h("span", { class: "chip label" }, `reused ×${fmtNum(st.reused_units)}`));
  for (const b of st.linked_from || []) labels.push(h("span", { class: "chip label" }, `linked@${b}`));
  if (st.storage) labels.push(h("span", { class: "chip label" }, `storage ${st.storage}`));
  if (st.axes && st.axes.length) labels.push(h("span", { class: "chip label" }, `foreach ${st.axes.map((a) => (typeof a === "string" ? a : a.name)).join(" × ")}`));

  const sm = data.summary || {};
  const cards = [
    card("units", fmtNum(st.total ?? Object.values(data.counts).reduce((a, b) => a + b, 0))),
    card("ok", fmtNum(data.counts.ok || 0)),
    card("failed", fmtNum(data.counts.failed || 0)),
    data.counts.running ? card("running", fmtNum(data.counts.running)) : null,
    data.counts.orphaned ? card("orphaned", fmtNum(data.counts.orphaned)) : null,
    card("mean unit", fmtMs(sm.mean_ms)), card("max unit", fmtMs(sm.max_ms)),
    card("artifacts", fmtBytes(sm.artifact_bytes), `${fmtNum(sm.artifact_files)} files`),
    card("peak RSS (unit)", fmtMb(sm.peak_rss_mb)),
  ];
  const metricCards = data.metrics.map((m) => card(m.name, fmtNum(m.sum), `ctx.metric ${m.name}: sum over ${m.units} units; min ${fmtNum(m.min)}, max ${fmtNum(m.max)}, mean ${fmtNum(m.mean)}`));

  const lastRun = last ? h("div", null, h("h3", null, "Last stage run"),
    h("div", { class: "cards" },
      card("wall", fmtMs(last.wall_ms)), card("executed", `${fmtNum(last.executed_units)} / ${fmtNum(last.total_units)}`),
      card("hit", fmtNum(last.hit_units)), card("workers", fmtNum(last.workers)),
      card("p50", fmtMs(last.unit_p50_ms)), card("p95", fmtMs(last.unit_p95_ms)), card("peak RSS", fmtMb(last.peak_rss_mb)),
      card("finished", fmtAgo(last.finished_at), fmtTime(last.finished_at)))) : null;

  const groups = data.error_groups.length ? h("div", null, h("h3", null, `Failures by error (${fmtNum(data.counts.failed || 0)})`),
    data.error_groups.map((g) => h("details", {
      open: state.openGroups.has(groupKey(g)),
      ontoggle: (e) => { if (e.target.open) state.openGroups.add(groupKey(g)); else state.openGroups.delete(groupKey(g)); },
    },
      h("summary", null, h("span", { class: "group-head" }, h("span", { class: "cnt" }, `×${fmtNum(g.count)}`), h("b", null, g.error_type || "error"), h("span", null, g.error_summary || ""))),
      h("div", { style: "margin-left:16px" },
        h("div", { class: "controls" },
          h("span", { class: "muted" }, "sample "), h("a", { href: href(route.pipeline, route.branch, route.stage, g.sample_unit_key) }, g.sample_unit_key || "(no axes)"),
          h("button", { class: "link", onclick: () => { Object.assign(state.units, { error_type: g.error_type || "", error_summary: g.error_summary || "", status: "", page: 1 }); reload(); } }, "show these units")),
        g.sample_traceback ? h("pre", null, g.sample_traceback) : h("p", { class: "muted" }, "no traceback recorded"))))) : null;

  const u = state.units;
  const statusSel = h("select", { onchange: (e) => { u.status = e.target.value; u.page = 1; reload(); } },
    ["", "ok", "failed", "running", "orphaned"].map((v) => { const o = h("option", { value: v }, v ? `${v} (${fmtNum(data.counts[v] || 0)})` : "all statuses"); if (v === u.status) o.selected = true; return o; }));
  const sortSel = h("select", { onchange: (e) => { u.sort = e.target.value; u.page = 1; reload(); } },
    [["key", "expansion order"], ["duration", "slowest first"], ["updated", "recently updated"]].map(([v, t]) => { const o = h("option", { value: v }, t); if (v === u.sort) o.selected = true; return o; }));
  let searchTimer = null;
  const search = h("input", { id: "unit-search", type: "search", placeholder: "filter unit key", value: u.q,
    oninput: (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { u.q = e.target.value; u.page = 1; reload(); }, 250); } });
  const groupFilter = u.error_type !== null ? h("span", { class: "chip label" }, `${u.error_type || "error"}: ${truncate(u.error_summary || "", 40)} `,
    h("button", { class: "link", onclick: () => { u.error_type = null; u.error_summary = null; u.page = 1; reload(); } }, "✕")) : null;

  const table = h("table", null,
    h("thead", null, h("tr", null, ["Unit", "Status", "Duration", "RSS", "Artifacts", "Error"].map((t, i) => h("th", { class: i >= 2 && i <= 4 ? "num" : null }, t)))),
    h("tbody", null, data.units.map((x) => h("tr", { class: "click", onclick: () => { location.hash = href(route.pipeline, route.branch, route.stage, x.unit_key); } },
      h("td", { class: "mono wrap" }, x.unit_key || "(no axes)", x.reused ? h("span", { class: "chip label" }, "reused") : null, x.linked_from ? h("span", { class: "chip label" }, `linked@${x.linked_from}`) : null),
      h("td", null, h("span", { class: `st-${x.status}` }, x.status)),
      h("td", { class: "num" }, fmtMs(x.duration_ms)),
      h("td", { class: "num" }, fmtMb(x.peak_rss_mb)),
      h("td", { class: "num" }, x.artifact_bytes === null ? DASH : `${fmtBytes(x.artifact_bytes)}`),
      h("td", null, x.error_type ? h("span", { class: "st-failed", title: x.error_summary || "" }, `${x.error_type}: ${truncate(x.error_summary || "", 60)}`) : null)))));
  const pager = h("div", { class: "pager" },
    h("button", { disabled: data.page <= 1, onclick: () => { u.page = data.page - 1; reload(); } }, "‹ prev"),
    h("span", null, `page ${data.page} / ${data.pages} · ${fmtNum(data.total)} units`),
    h("button", { disabled: data.page >= data.pages, onclick: () => { u.page = data.page + 1; reload(); } }, "next ›"),
    h("select", { onchange: (e) => { u.page_size = Number(e.target.value); u.page = 1; reload(); } },
      [25, 50, 100, 200].map((n) => { const o = h("option", { value: n }, `${n} / page`); if (n === data.page_size) o.selected = true; return o; })));

  fill(d,
    drawerHead(route.stage, stateChip(st.state)),
    labels.length ? h("div", null, labels) : null,
    h("div", { class: "cards" }, cards),
    metricCards.length ? [h("h3", null, "Metrics (sum over units)"), h("div", { class: "cards" }, metricCards)] : null,
    lastRun, groups,
    h("h3", null, "Units"),
    h("div", { class: "controls" }, statusSel, search, sortSel, groupFilter),
    table, pager,
  );
}

// ---------------------------------------------------------------- drawer: unit

async function openUnit(route) {
  const d = $("drawer");
  const key = `${route.pipeline}\u0000${route.branch}\u0000${route.stage}\u0000u:${route.unit}`;
  d.dataset.key = key;
  d.hidden = false;
  fill(d, drawerHead(route.unit || "(no axes)", null), h("p", { class: "muted" }, "Loading…"));
  let data;
  try {
    data = await api(`${branchApi(route.pipeline, route.branch)}/stages/${enc(route.stage)}/units/${enc(unitToken(route.unit))}`);
  } catch (err) { showError(d, err); return; }
  if (d.dataset.key !== key) return;
  const x = data.unit;
  const back = () => { location.hash = href(route.pipeline, route.branch, route.stage); };
  const sd = x.source_detail || {};
  const symbols = Object.entries(sd.symbols || {});
  const external = Object.entries(sd.external || {});
  const manifest = Array.isArray(x.artifact_manifest) ? x.artifact_manifest : [];
  const metrics = Object.entries(x.metrics || {});

  fill(d,
    drawerHead(x.unit_key || "(no axes)", h("span", { class: `chip` }, h("i", { class: `dot-${x.status}` }), x.status), back),
    h("div", { class: "muted" }, `stage ${x.stage_name}`),
    data.labels.length ? h("div", null, data.labels.map((l) => h("span", { class: "chip label" }, l.text))) : null,
    h("div", { class: "cards" },
      card("duration", fmtMs(x.duration_ms)), card("peak RSS", fmtMb(x.peak_rss_mb)),
      card("artifacts", fmtBytes(x.artifact_bytes), `${fmtNum(x.artifact_files)} files`),
      card("updated", fmtAgo(x.updated_at), fmtTime(x.updated_at))),
    metrics.length ? [h("h3", null, "Metrics"), h("div", { class: "cards" }, metrics.map(([k, v]) => card(k, typeof v === "number" ? fmtNum(v) : compact(v))))] : null,
    x.error_traceback || x.error_type ? [h("h3", { class: "st-failed" }, `${x.error_type || "error"}: ${x.error_summary || ""}`), x.error_traceback ? h("pre", null, x.error_traceback) : null] : null,
    h("h3", null, "Key components"),
    h("table", { class: "seg" }, h("tbody", null, data.segments.map((sg) => h("tr", null,
      h("td", null, sg.name), h("td", { class: "mono", title: sg.hash || "" }, shortHash(sg.hash)), h("td", { class: "mono wrap" }, compact(sg.summary)))))),
    Object.keys(data.file_sets).length ? h("p", { class: "note" }, Object.entries(data.file_sets).map(([fh, fs]) => `file set ${fh.slice(0, 12)}: ${fmtNum(fs.file_count)} files, ${fmtBytes(fs.total_bytes)}`).join(" · ")) : null,
    kv([["input_key", h("span", { class: "mono" }, x.input_key)],
      ["config access", x.config_access === null ? "whole Config (not yet projected)" : (x.config_access || []).join(", ") || "none"]]),
    h("h3", null, "Source"),
    kv([["file", sd.file || DASH], ["mode", sd.mode || DASH], sd.fallback_reason ? ["fallback", sd.fallback_reason] : null,
      ["source_fp", h("span", { class: "mono" }, shortHash(x.source_fp))],
      x.reused_source_fp ? ["reused fp", h("span", { class: "mono" }, shortHash(x.reused_source_fp))] : null]),
    symbols.length ? h("details", null, h("summary", null, `${symbols.length} symbols in closure`),
      h("table", null, h("tbody", null, symbols.map(([n, hsh]) => h("tr", null, h("td", { class: "mono" }, n), h("td", { class: "mono muted" }, shortHash(hsh))))))) : null,
    external.length ? h("details", null, h("summary", null, `${external.length} external sources`),
      h("table", null, h("tbody", null, external.map(([n, hsh]) => h("tr", null, h("td", { class: "mono wrap" }, n), h("td", { class: "mono muted" }, shortHash(hsh))))))) : null,
    h("h3", null, "Artifact"),
    kv([["path", data.artifact_path ? h("span", { class: "mono" }, data.artifact_path) : DASH], ["storage", x.storage_name],
      ["artifact_fp", h("span", { class: "mono" }, shortHash(x.artifact_fp))]]),
    manifest.length ? h("details", null, h("summary", null, `${manifest.length} files`),
      h("table", null, h("tbody", null, manifest.slice(0, 200).map((m) => h("tr", null, h("td", { class: "mono wrap" }, m[0]), h("td", { class: "num" }, fmtBytes(m[1]))))))) : null,
    h("h3", null, "Run"),
    data.run ? kv([["run", h("span", { class: "mono" }, data.run.run_id)], ["started", fmtTime(data.run.started_at)], ["status", data.run.status],
      ["commit", data.run.git_commit ? `${data.run.git_commit}${data.run.git_dirty ? " (dirty)" : ""}` : DASH]]) : h("p", { class: "muted" }, "no run recorded"),
    h("h3", null, "Captured log"),
    data.log && data.log.available
      ? [data.log.truncated ? h("p", { class: "note" }, `showing the last 64 KB of ${fmtBytes(data.log.size)}`) : null, h("pre", null, data.log.text || "(empty)"), h("p", { class: "note mono" }, data.log.path)]
      : h("p", { class: "muted" }, data.log ? "no log captured for this unit (output went to the terminal)" : "no run recorded"),
  );
}

// ---------------------------------------------------------------- boot

function route() {
  const r = parseRoute();
  tip.hide();
  if (r.view === "branch") renderBranch(r);
  else renderWorkspace();
}

window.addEventListener("hashchange", route);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || $("drawer").hidden) return;
  const r = parseRoute();
  if (r.view === "branch" && r.unit !== null && r.unit !== undefined) location.hash = href(r.pipeline, r.branch, r.stage);
  else closeDrawer(true);
});
route();
