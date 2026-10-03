"""CSS and JavaScript for the interactive lineage report.

Held as plain strings rather than f-strings so braces need no escaping, and inlined into the
output file: the page has no network dependency and renders from a local filesystem.

This is the presentation layer only - it knows about node kinds, edge kinds and layers, and
nothing about any particular landscape. `build.py` supplies the graph.
"""

CSS = r"""
:root {
  --ink:#0f172a; --muted:#475569; --rule:#cbd5e1; --band:#f1f5f9;
  --paper:#ffffff; --accent:#1d4ed8; --ok:#15803d; --warn:#b45309;
  --bad:#be123c; --method:#7e22ce; --shadow:0 1px 2px rgba(15,23,42,.06);
  --k-query:#e9d5ff; --k-query-e:#7e22ce;
  --k-cp:#bbf7d0;    --k-cp-e:#15803d;
  --k-adso:#c7d2fe;  --k-adso-e:#4338ca;
  --k-ds:#fde68a;    --k-ds-e:#b45309;
  --k-calc:#fbcfe8;  --k-calc-e:#9d174d;
  --k-src:#fef3c7;   --k-src-e:#92400e;
  --k-iobj:#e2e8f0;  --k-iobj-e:#475569;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body {
  margin:0; background:#f8fafc; color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,
       Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.86em; }
a { color:var(--accent); }
h1,h2,h3,h4 { line-height:1.25; margin:0; }

/* ---------------------------------------------------------------- chrome */
header.top {
  background:var(--ink); color:#e2e8f0; padding:26px 32px 20px;
  border-bottom:3px solid var(--accent);
}
header.top h1 { font-size:30px; letter-spacing:-.4px; color:#fff; }
header.top .sub { color:#94a3b8; font-size:15px; margin-top:4px; }
.chips { display:flex; flex-wrap:wrap; gap:7px; margin-top:16px; }
.chip {
  background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.16);
  border-radius:999px; padding:3px 11px; font-size:12px; color:#cbd5e1;
}
.chip b { color:#fff; font-weight:600; }

nav.toc {
  position:sticky; top:0; z-index:40; background:rgba(255,255,255,.94);
  backdrop-filter:blur(8px); border-bottom:1px solid var(--rule);
  padding:0 22px; overflow-x:auto; white-space:nowrap; box-shadow:var(--shadow);
}
nav.toc a {
  display:inline-block; padding:11px 11px; font-size:13px; color:var(--muted);
  text-decoration:none; border-bottom:2px solid transparent;
}
nav.toc a:hover { color:var(--ink); background:var(--band); }
nav.toc a.on { color:var(--accent); border-bottom-color:var(--accent);
  font-weight:600; }

main { max-width:1280px; margin:0 auto; padding:0 22px 90px; }
section { scroll-margin-top:52px; padding:34px 0 8px; }
section + section { border-top:1px solid var(--rule); }
.secno { color:var(--accent); font-weight:700; font-size:13px;
  letter-spacing:.9px; text-transform:uppercase; }
section > h2 { font-size:25px; margin:5px 0 12px; letter-spacing:-.3px; }
h3 { font-size:17px; margin:26px 0 9px; }
h4 { font-size:14px; margin:18px 0 7px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.6px; }
p { margin:0 0 12px; max-width:95ch; }
p.lead { font-size:17px; line-height:1.55; }
.small { font-size:13px; color:var(--muted); }

/* ---------------------------------------------------------------- blocks */
.card {
  background:var(--paper); border:1px solid var(--rule); border-radius:10px;
  padding:18px 20px; margin:0 0 16px; box-shadow:var(--shadow);
}
.correction { border-left:5px solid var(--method); }
.correction h3 { margin-top:0; color:var(--method); }
.note { border-left:5px solid var(--accent); }
.caution { border-left:5px solid var(--warn); background:#fffbeb;
  border-color:#fde68a; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr));
  gap:16px; }

dl.kv { margin:0; display:grid; grid-template-columns:max-content 1fr;
  gap:3px 16px; }
dl.kv dt { font-weight:600; font-size:13px; color:var(--muted); }
dl.kv dd { margin:0; font-size:14px; }

table { border-collapse:collapse; width:100%; font-size:13.5px;
  background:var(--paper); }
.tw { overflow-x:auto; border:1px solid var(--rule); border-radius:10px;
  margin:0 0 16px; box-shadow:var(--shadow); }
thead th {
  background:var(--ink); color:#fff; text-align:left; font-weight:600;
  padding:9px 11px; font-size:12.5px; position:sticky; top:0;
}
tbody td { padding:8px 11px; border-top:1px solid #e8edf3; vertical-align:top; }
tbody tr:nth-child(even) { background:var(--band); }
tbody tr:hover { background:#eef2ff; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }

.sev { display:inline-block; padding:1px 8px; border-radius:5px;
  font-size:11.5px; font-weight:700; letter-spacing:.3px; white-space:nowrap; }
.sev.High   { background:#ffe4e6; color:var(--bad);    border:1px solid #fecdd3; }
.sev.Medium { background:#fef3c7; color:var(--warn);   border:1px solid #fde68a; }
.sev.Low    { background:var(--band); color:var(--muted); border:1px solid var(--rule); }
.sev.Method { background:#f3e8ff; color:var(--method); border:1px solid #e9d5ff; }
.tagok   { color:var(--ok);   font-weight:600; }
.tagwarn { color:var(--warn); font-weight:600; }
.tagbad  { color:var(--bad);  font-weight:600; }
.tagmut  { color:var(--muted); }

details { background:var(--paper); border:1px solid var(--rule);
  border-radius:10px; margin:0 0 10px; box-shadow:var(--shadow); }
details[open] { box-shadow:0 2px 10px rgba(15,23,42,.08); }
summary { cursor:pointer; padding:12px 16px; font-weight:600; font-size:14.5px;
  display:flex; align-items:center; gap:10px; }
summary::-webkit-details-marker { display:none; }
summary:before { content:"\25B8"; color:var(--accent); font-size:12px;
  transition:transform .14s; }
details[open] > summary:before { transform:rotate(90deg); }
summary:hover { background:var(--band); border-radius:9px; }
summary .meta { margin-left:auto; font-weight:400; font-size:12.5px;
  color:var(--muted); }
.dbody { padding:0 16px 14px; border-top:1px solid #e8edf3; }

ul.tight { margin:0 0 12px; padding-left:20px; }
ul.tight li { margin:3px 0; }

/* ---------------------------------------------------------------- diagram */
/* break the diagram out of the text column: nine layers need the width */
.bleed { margin-left:calc(50% - 50vw); margin-right:calc(50% - 50vw);
  padding:0 22px; }
.viz { background:var(--paper); border:1px solid var(--rule);
  border-radius:12px; box-shadow:var(--shadow); overflow:hidden; }
.vizbar { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center;
  padding:11px 14px; border-bottom:1px solid var(--rule);
  background:linear-gradient(#fff,#f8fafc); }
.vizbar .grp { display:flex; gap:6px; align-items:center; }
.vizbar label { font-size:12.5px; color:var(--muted); display:flex; gap:5px;
  align-items:center; cursor:pointer; user-select:none; }
.vizbar input[type=checkbox] { accent-color:var(--accent); }
.btn { font:inherit; font-size:12.5px; padding:4px 10px; cursor:pointer;
  background:#fff; border:1px solid var(--rule); border-radius:7px;
  color:var(--ink); }
.btn:hover { background:var(--band); border-color:#94a3b8; }
.btn:active { transform:translateY(1px); }
#q { font:inherit; font-size:13px; padding:5px 10px; border-radius:7px;
  border:1px solid var(--rule); width:190px; }
#q:focus { outline:2px solid var(--accent); outline-offset:1px; }
.hint { font-size:12px; color:var(--muted); margin-left:auto; }

.vizmain { display:grid; grid-template-columns:1fr 320px; min-height:580px; }
@media (max-width:960px){ .vizmain { grid-template-columns:1fr; } }
#stage { position:relative; background:#fff; cursor:grab; overflow:hidden; }
#stage.drag { cursor:grabbing; }
#svg { display:block; width:100%; height:100%; min-height:580px;
  touch-action:none; }
.lanelab { font:600 10.5px/1 -apple-system,"Segoe UI",sans-serif;
  fill:#94a3b8; letter-spacing:.7px; text-transform:uppercase; }
.lanebg { fill:#f8fafc; }
g.node { cursor:pointer; }
g.node rect { stroke-width:1.2; rx:6; }
g.node text { font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,
  monospace; fill:var(--ink); pointer-events:none; }
g.node text.sub { font:400 8.4px/1 -apple-system,"Segoe UI",Roboto,sans-serif;
  fill:#334155; letter-spacing:.1px; }
g.node:focus { outline:none; }
g.node:focus rect { stroke:var(--accent); stroke-width:3; }
g.node.sel rect { stroke-width:3; filter:drop-shadow(0 2px 5px rgba(0,0,0,.22)); }
g.node.dim { opacity:.14; }
path.edge { fill:none; }
path.edge.dim { opacity:.05; }
path.edge.hot { stroke-width:2.6; }
.k-query rect { fill:var(--k-query); stroke:var(--k-query-e); }
.k-cp    rect { fill:var(--k-cp);    stroke:var(--k-cp-e); }
.k-adso  rect { fill:var(--k-adso);  stroke:var(--k-adso-e); }
.k-ds    rect { fill:var(--k-ds);    stroke:var(--k-ds-e); }
.k-calc  rect { fill:var(--k-calc);  stroke:var(--k-calc-e); }
.k-src   rect { fill:var(--k-src);   stroke:var(--k-src-e); }
.k-iobj  rect { fill:var(--k-iobj);  stroke:var(--k-iobj-e); }

#panel { border-left:1px solid var(--rule); background:#fcfdfe; padding:14px 16px;
  overflow-y:auto; max-height:760px; }
#panel h4 { margin:0 0 2px; font-size:11px; }
#panel .pname { font:700 16px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,
  monospace; word-break:break-all; margin:0 0 2px; }
#panel .pdesc { font-size:14px; font-weight:600; line-height:1.35; margin:2px 0 5px; }
#panel .pkind { font-size:12px; color:var(--muted); margin-bottom:12px; }
.genmark { display:inline-block; font-size:9.5px; font-weight:600; padding:1px 5px;
  border-radius:4px; background:#f3e8ff; color:var(--method);
  border:1px solid #e9d5ff; vertical-align:2px; white-space:nowrap;
  letter-spacing:.2px; cursor:help; }
#panel dl.kv { grid-template-columns:1fr; gap:0; }
#panel dl.kv dt { margin-top:10px; font-size:10.5px; text-transform:uppercase;
  letter-spacing:.6px; }
#panel dl.kv dd { font-size:13px; line-height:1.45; }
#panel .empty { color:var(--muted); font-size:13px; }
.nb { margin:12px 0 0; border-top:1px solid var(--rule); padding-top:10px; }
.nb li { font-size:12.5px; margin:2px 0; list-style:none; }
.nb li b { font-weight:600; }
.nb .arrow { color:var(--muted); }
.swatch { display:inline-block; width:10px; height:10px; border-radius:3px;
  border:1px solid; vertical-align:-1px; margin-right:5px; }
.legend { display:flex; flex-wrap:wrap; gap:6px 16px; padding:11px 14px;
  border-top:1px solid var(--rule); font-size:12.5px; color:var(--muted);
  background:#fcfdfe; }
.legend .ln { display:inline-block; width:26px; height:0;
  border-top-width:2px; vertical-align:3px; margin-right:5px; }
.tip { position:absolute; pointer-events:none; z-index:9; max-width:300px;
  background:var(--ink); color:#fff; font-size:12px; line-height:1.45;
  padding:7px 10px; border-radius:7px; opacity:0; transition:opacity .1s;
  box-shadow:0 4px 14px rgba(0,0,0,.3); }
.tip.on { opacity:1; }
.tip b { color:#fff; }
.tip .t2 { color:#94a3b8; }

footer { max-width:1280px; margin:0 auto; padding:24px 22px 60px;
  color:var(--muted); font-size:12.5px; border-top:1px solid var(--rule); }

@media print {
  nav.toc, .vizbar, #panel, .hint { display:none !important; }
  body { background:#fff; }
  .viz, .card, .tw, details { break-inside:avoid; box-shadow:none; }
  details { border:1px solid var(--rule); }
  details:not([open]) > .dbody { display:block; }
  section { break-before:page; }
}
"""

JS = r"""
(function () {
  "use strict";
  var G = window.__LINEAGE_GRAPH__;
  var NW = G.node_w, NH = G.node_h;
  var PADX = G.pad_x, PADY = G.pad_y, COLW = G.col_w, PITCH = G.row_pitch;
  var curW = G.width, curH = G.height;
  var svg = document.getElementById("svg");
  var stage = document.getElementById("stage");
  var panel = document.getElementById("panel");
  var tip = document.getElementById("tip");
  var qbox = document.getElementById("q");

  var byId = {};
  G.nodes.forEach(function (n) { byId[n.id] = n; });

  var EDGE = {
    declared: { c: "#64748b", w: 1.3, d: "" },
    full:     { c: "#be123c", w: 2.0, d: "" },
    part:     { c: "#15803d", w: 1.5, d: "" },
    calcbase: { c: "#0e7490", w: 1.7, d: "" },
    expose:   { c: "#0e7490", w: 1.7, d: "6 3" },
    read:     { c: "#7e22ce", w: 2.2, d: "" },
    lookup:   { c: "#2563eb", w: 1.1, d: "5 4" },
    routine:  { c: "#b45309", w: 1.1, d: "2 3" },
    self:     { c: "#475569", w: 1.1, d: "3 2" }
  };
  var KINDNAME = {
    query: "BEx query", cp: "CompositeProvider", adso: "Advanced DSO",
    ds: "DataSource", calc: "HANA calculation view",
    src: "Source / logical system", iobj: "InfoObject"
  };
  var EDGENAME = {
    declared: "declared load", full: "declared FULL load",
    part: "CompositeProvider part", calcbase: "calc-view base",
    expose: "exposed to BW", read: "read by the query",
    lookup: "declared lookup", routine: "routine-derived read (advisory)",
    self: "self re-processing"
  };

  /* ------------------------------------------------------------ build svg */
  var NS = "http://www.w3.org/2000/svg";
  function el(t, a) {
    var e = document.createElementNS(NS, t);
    for (var k in a) { if (a[k] !== null) e.setAttribute(k, a[k]); }
    return e;
  }
  var defs = el("defs", {});
  Object.keys(EDGE).forEach(function (k) {
    var m = el("marker", { id: "ar-" + k, viewBox: "0 0 10 10", refX: 9,
      refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
    m.appendChild(el("path", { d: "M0,1 L10,5 L0,9 z", fill: EDGE[k].c }));
    defs.appendChild(m);
  });
  svg.appendChild(defs);

  var root = el("g", { id: "root" });
  var gLane = el("g", {}), gEdge = el("g", {}), gNode = el("g", {});
  root.appendChild(gLane); root.appendChild(gEdge); root.appendChild(gNode);
  svg.appendChild(root);

  var laneBg = [], laneLab = [];
  G.layers.forEach(function (L, i) {
    var x = PADX + i * COLW;
    if (i % 2 === 1) {
      var r = el("rect", { class: "lanebg", x: x - 14, y: 0,
        width: NW + 28, height: curH + 26 });
      laneBg.push(r);
      gLane.appendChild(r);
    }
    var t = el("text", { class: "lanelab", x: x, y: curH + 18 });
    t.textContent = L.name;
    laneLab.push(t);
    gLane.appendChild(t);
  });

  function split(s, max) {
    if (s.length <= max) return [s];
    var cut = -1, i;
    for (i = s.length - 1; i > 3; i--) {
      if ((s[i] === "_" || s[i] === "/") && i <= max) { cut = i + 1; break; }
    }
    if (cut < 0) cut = max;
    return [s.slice(0, cut), s.slice(cut)];
  }

  /* The description line. Wrapped to two lines at most and ellipsised beyond that - the
     full text is in the panel, so truncating here costs nothing. */
  function wrapSub(s) {
    if (!s) return [];
    var words = s.split(/\s+/), lines = [""], max = 26;
    words.forEach(function (w) {
      var line = lines[lines.length - 1];
      if (!line) { lines[lines.length - 1] = w; return; }
      if ((line + " " + w).length <= max) lines[lines.length - 1] = line + " " + w;
      else lines.push(w);
    });
    if (lines.length > 2) {
      lines = lines.slice(0, 2);
      lines[1] = lines[1].slice(0, max - 1) + "\u2026";
    }
    return lines.filter(Boolean);
  }

  var nodeEls = {};
  G.nodes.forEach(function (n) {
    var aria = n.id + ", " + KINDNAME[n.kind] + (n.sub ? ", " + n.sub : "");
    var g = el("g", { class: "node k-" + n.kind, tabindex: 0,
      role: "button", transform: "translate(" + n.x + "," + n.y + ")",
      "aria-label": aria });
    g.appendChild(el("rect", { x: 0, y: 0, width: NW, height: NH }));
    var name = split(n.label, 22);
    var sub = wrapSub(n.sub);
    // Lay the block out from the middle so a one-line name with a two-line description and a
    // two-line name with none both sit centred.
    var nameH = name.length * 11.5, subH = sub.length * 9.5;
    var top = (NH - (nameH + (sub.length ? subH + 2 : 0))) / 2 + 9;
    name.forEach(function (ln, k) {
      var t = el("text", { x: NW / 2, y: top + k * 11.5, "text-anchor": "middle" });
      if (name.length > 1) t.setAttribute("style", "font-size:9.5px");
      t.textContent = ln;
      g.appendChild(t);
    });
    sub.forEach(function (ln, k) {
      var t = el("text", { class: "sub", x: NW / 2,
        y: top + nameH + 1 + k * 9.5, "text-anchor": "middle" });
      t.textContent = ln;
      g.appendChild(t);
    });
    g.__n = n;
    nodeEls[n.id] = g;
    gNode.appendChild(g);
  });

  /* Re-run the layered layout over whatever is currently visible, so hiding
     the lookup column shrinks the canvas instead of leaving dead space. */
  function layout() {
    var cols = {};
    G.nodes.forEach(function (n) {
      if (!shown(n)) return;
      (cols[n.layer] = cols[n.layer] || []).push(n);
    });
    var tallest = 1;
    Object.keys(cols).forEach(function (k) {
      cols[k].sort(function (a, b) { return a.order - b.order; });
      if (cols[k].length > tallest) tallest = cols[k].length;
    });
    curH = PADY * 2 + tallest * PITCH;
    curW = PADX * 2 + (G.layers.length - 1) * COLW + NW;
    Object.keys(cols).forEach(function (k) {
      var col = cols[k];
      var top = PADY + (tallest - col.length) * PITCH / 2;
      col.forEach(function (n, i) {
        n.cx = PADX + n.layer * COLW;
        n.cy = top + i * PITCH;
      });
    });
    laneBg.forEach(function (r) { r.setAttribute("height", curH + 26); });
    laneLab.forEach(function (t) { t.setAttribute("y", curH + 18); });
  }

  function geom(e) {
    var a = byId[e.src], b = byId[e.dst];
    if (a.cx === undefined || b.cx === undefined) return "M0,0";
    if (e.src === e.dst) {
      var x = a.cx, y = a.cy;
      return "M" + (x + NW * .3) + "," + y + " C" + (x + NW * .1) + "," +
        (y - 30) + " " + (x + NW * .9) + "," + (y - 30) + " " +
        (x + NW * .7) + "," + y;
    }
    if (a.layer === b.layer) {
      var sx = a.cx + NW, sy = a.cy + NH / 2,
          ex = b.cx + NW, ey = b.cy + NH / 2;
      return "M" + sx + "," + sy + " C" + (sx + 78) + "," + sy + " " +
        (ex + 78) + "," + ey + " " + ex + "," + ey;
    }
    if (e.back) {
      var bx = a.cx, by = a.cy + NH / 2, cx = b.cx + NW, cy = b.cy + NH / 2;
      var dip = 54 + Math.abs(a.layer - b.layer) * 12;
      return "M" + bx + "," + by + " C" + (bx - 60) + "," + (by + dip) + " " +
        (cx + 60) + "," + (cy + dip) + " " + cx + "," + cy;
    }
    var fx = a.cx + NW, fy = a.cy + NH / 2, tx = b.cx, ty = b.cy + NH / 2;
    var m = (fx + tx) / 2;
    return "M" + fx + "," + fy + " C" + m + "," + fy + " " + m + "," + ty +
      " " + tx + "," + ty;
  }

  var edgeEls = [];
  G.edges.forEach(function (e) {
    var s = EDGE[e.kind];
    var p = el("path", { class: "edge e-" + e.kind, d: geom(e), stroke: s.c,
      "stroke-width": s.w, "stroke-dasharray": s.d || null,
      "marker-end": "url(#ar-" + e.kind + ")",
      opacity: (e.kind === "lookup" ? .4 : e.kind === "routine" ? .5 : .78) });
    p.__e = e;
    edgeEls.push(p);
    gEdge.appendChild(p);
  });

  /* ------------------------------------------------------- adjacency index */
  var adj = {};
  G.nodes.forEach(function (n) { adj[n.id] = []; });
  G.edges.forEach(function (e) {
    adj[e.src].push({ o: e.dst, dir: "out", e: e });
    if (e.src !== e.dst) adj[e.dst].push({ o: e.src, dir: "in", e: e });
  });

  /* ----------------------------------------------------------- view state */
  var view = { x: 0, y: 0, k: 1 };
  function apply() {
    root.setAttribute("transform", "translate(" + view.x + "," + view.y +
      ") scale(" + view.k + ")");
  }
  /* Fit the visible extent, but never below a scale where the labels stop
     being readable - below that, anchor the source end and let the reader
     pan. An unreadable overview is not an overview. */
  var MIN_FIT = 0.5;
  function fit() {
    var r = stage.getBoundingClientRect();
    var k = Math.min((r.width - 20) / curW, (r.height - 40) / (curH + 34));
    var clamped = k < MIN_FIT;
    view.k = Math.min(Math.max(k, MIN_FIT), 1.1);
    view.x = clamped ? 8 : (r.width - curW * view.k) / 2;
    view.y = Math.max(8, (r.height - (curH + 34) * view.k) / 2);
    apply();
  }
  var panning = false, px = 0, py = 0;
  stage.addEventListener("pointerdown", function (ev) {
    if (ev.target.closest("g.node")) return;
    panning = true; px = ev.clientX; py = ev.clientY;
    stage.classList.add("drag"); stage.setPointerCapture(ev.pointerId);
  });
  stage.addEventListener("pointermove", function (ev) {
    if (!panning) return;
    view.x += ev.clientX - px; view.y += ev.clientY - py;
    px = ev.clientX; py = ev.clientY; apply();
  });
  function endPan() { panning = false; stage.classList.remove("drag"); }
  stage.addEventListener("pointerup", endPan);
  stage.addEventListener("pointercancel", endPan);
  stage.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var r = stage.getBoundingClientRect();
    var mx = ev.clientX - r.left, my = ev.clientY - r.top;
    var f = Math.exp(-ev.deltaY * 0.0016);
    var nk = Math.max(.15, Math.min(2.6, view.k * f));
    view.x = mx - (mx - view.x) * (nk / view.k);
    view.y = my - (my - view.y) * (nk / view.k);
    view.k = nk; apply();
  }, { passive: false });
  function zoom(f) {
    var r = stage.getBoundingClientRect(), mx = r.width / 2, my = r.height / 2;
    var nk = Math.max(.15, Math.min(2.6, view.k * f));
    view.x = mx - (mx - view.x) * (nk / view.k);
    view.y = my - (my - view.y) * (nk / view.k);
    view.k = nk; apply();
  }

  /* -------------------------------------------------------- show / hide */
  function shown(n) {
    if (n.group === "lookup" && !document.getElementById("t-look").checked)
      return false;
    return true;
  }
  function edgeOn(e) {
    if (!shown(byId[e.src]) || !shown(byId[e.dst])) return false;
    if (e.kind === "routine" && !document.getElementById("t-adv").checked)
      return false;
    if (e.kind === "lookup" && !document.getElementById("t-look-e").checked)
      return false;
    if (e.kind === "self" && !document.getElementById("t-self").checked)
      return false;
    return true;
  }

  var sel = null;
  function matches(n) {
    var q = qbox.value.trim().toUpperCase();
    if (!q) return null;
    return n.id.toUpperCase().indexOf(q) >= 0;
  }

  function render() {
    layout();
    var hot = {}, hotE = {};
    if (sel) {
      hot[sel] = 1;
      adj[sel].forEach(function (a) { if (edgeOn(a.e)) hot[a.o] = 1; });
      G.edges.forEach(function (e, i) {
        if (e.src === sel || e.dst === sel) hotE[i] = 1;
      });
    }
    G.nodes.forEach(function (n) {
      var g = nodeEls[n.id];
      var vis = shown(n);
      g.style.display = vis ? "" : "none";
      if (vis) g.setAttribute("transform",
        "translate(" + n.cx + "," + n.cy + ")");
      var m = matches(n);
      var dim = (sel && !hot[n.id]) || (m === false);
      g.classList.toggle("dim", !!dim);
      g.classList.toggle("sel", n.id === sel || m === true);
    });
    edgeEls.forEach(function (p, i) {
      var e = p.__e, on = edgeOn(e);
      p.style.display = on ? "" : "none";
      if (on) p.setAttribute("d", geom(e));
      p.classList.toggle("dim", !!(sel && !hotE[i]));
      p.classList.toggle("hot", !!hotE[i]);
    });
    document.getElementById("vcount").textContent =
      G.nodes.filter(shown).length + " objects \u00b7 " +
      G.edges.filter(edgeOn).length + " dependencies";
  }

  /* -------------------------------------------------------------- panel */
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  var LABEL = {
    description_long: "In full", description_origin: "Description origin",
    description_quality: "Stored-text quality",
    what: "What it is", contribution: "Contribution to this path",
    system: "Logical system", appended_fields: "Appended fields (BW metadata)",
    fill_logic: "Fill logic found in the source system", enhancement_verdict: "Verdict",
    exit_program: "Exit code", exit_tables: "Tables the exit reads",
    unguarded_for_all_entries: "Unguarded FOR ALL ENTRIES",
    role: "Role on this path", upstream_traced: "Upstream traced here",
    description: "Description", info_area: "InfoArea", parts: "Parts",
    reached_via: "Reached via", full_name: "Runtime name",
    purpose: "What it does", last_activated: "Last activated",
    direct_bases: "Direct bases", nodes: "Nodes", final_node: "Final node",
    calculated_columns: "Calculated columns",
    analytic_privilege: "Analytic privilege", feeds: "Feeds back into BW as",
    loads: "Loads", model_slot: "Model slot",
    query_reads: "What the query reads from it",
    declared_lookups: "Declared lookups", routine_lookups:
      "Routine-derived reads (advisory)", compuid: "COMPUID", owner: "Owner",
    last_executed: "Last executed", origin: "Origin",
    description_origin: "Description origin", kind_basis: "Object kind read from",
    provenance: "Source tables"
  };
  function show(id) {
    var n = byId[id];
    if (!n) {
      panel.innerHTML = '<h4>Selected object</h4><p class="empty">Click any ' +
        'box in the diagram to see what it is, where its facts came from, and ' +
        'everything it connects to. Click the background to clear.</p>';
      return;
    }
    var lane = G.layers[n.layer].name;
    var m = n.meta || {};
    var h = '<h4>Selected object</h4><div class="pname">' + esc(n.id) +
      '</div>';
    // What it is, before anything else. Flagged when the text is not purely BW's own.
    if (m.description) {
      var flag = m.description_origin && m.description_origin.indexOf("stored verbatim") < 0
        ? ' <span class="genmark" title="' + esc(m.description_origin) +
          '">not stored verbatim</span>' : '';
      h += '<div class="pdesc">' + esc(m.description) + flag + '</div>';
    }
    h += '<div class="pkind"><span class="swatch" style="background:var(--k-' +
      n.kind + ');border-color:var(--k-' + n.kind + '-e)"></span>' +
      KINDNAME[n.kind] +
      (lane === KINDNAME[n.kind] ? '' : ' \u00b7 ' + esc(lane)) + '</div>';
    h += '<dl class="kv">';
    Object.keys(LABEL).forEach(function (k) {
      if (!(k in n.meta) || k === "description") return;
      var v = n.meta[k];
      if (Array.isArray(v)) v = v.join(", ");
      var cls = "";
      if (k === "enhancement_verdict")
        cls = v === "resolved" ? "tagok" : v === "n/a" ? "tagmut" : "tagbad";
      if (k === "unguarded_for_all_entries")
        cls = v >= 10 ? "tagbad" : v > 0 ? "tagwarn" : "tagok";
      h += '<dt>' + LABEL[k] + '</dt><dd' + (cls ? ' class="' + cls + '"' : '') +
        (k === "provenance" || k === "full_name" ? ' class="mono"' : '') + '>' +
        esc(v) + '</dd>';
    });
    h += '</dl>';
    var ins = [], outs = [];
    adj[id].forEach(function (a) {
      if (!edgeOn(a.e)) return;
      var row = '<li><span class="arrow">' + (a.dir === "in" ? "\u2190" :
        "\u2192") + '</span> <b>' + esc(a.o === id ? "itself" : a.o) +
        '</b> <span class="t2">' + EDGENAME[a.e.kind] + '</span></li>';
      (a.dir === "in" ? ins : outs).push(row);
    });
    if (ins.length) h += '<ul class="nb"><li><b>Feeds in (' + ins.length +
      ')</b></li>' + ins.join("") + '</ul>';
    if (outs.length) h += '<ul class="nb"><li><b>Feeds out (' + outs.length +
      ')</b></li>' + outs.join("") + '</ul>';
    panel.innerHTML = h;
  }

  /* ------------------------------------------------------------ handlers */
  function pick(id) { sel = (sel === id ? null : id); render(); show(sel); }
  G.nodes.forEach(function (n) {
    var g = nodeEls[n.id];
    g.addEventListener("click", function (ev) { ev.stopPropagation();
      pick(n.id); });
    g.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault();
        pick(n.id); }
    });
    g.addEventListener("pointerenter", function (ev) {
      var d = n.degree, m = n.meta || {};
      var body = m.description ? "<br>" + esc(m.description) : "";
      var long = m.description_long && m.description_long !== m.description
        ? "<br><span class='t2'>" + esc(m.description_long) + "</span>" : "";
      tipShow(ev, "<b>" + esc(n.id) + "</b>" + body + long +
        "<br><span class='t2'>" + KINDNAME[n.kind] + " \u00b7 " + d["in"] +
        " in / " + d.out + " out" +
        (m.description_origin ? " \u00b7 " + esc(m.description_origin.split(" - ")[0])
          : "") + "</span>");
    });
    g.addEventListener("pointerleave", tipHide);
  });
  edgeEls.forEach(function (p) {
    p.addEventListener("pointerenter", function (ev) {
      var e = p.__e;
      tipShow(ev, "<b>" + esc(e.src) + " \u2192 " + esc(e.dst) +
        "</b><br><span class='t2'>" + EDGENAME[e.kind] + " \u00b7 " + e.basis +
        (e.note ? "<br>" + esc(e.note) : "") + "</span>");
    });
    p.addEventListener("pointerleave", tipHide);
  });
  function tipShow(ev, html) {
    var r = stage.getBoundingClientRect();
    tip.innerHTML = html;
    tip.style.left = Math.min(ev.clientX - r.left + 14, r.width - 310) + "px";
    tip.style.top = (ev.clientY - r.top + 14) + "px";
    tip.classList.add("on");
  }
  function tipHide() { tip.classList.remove("on"); }

  stage.addEventListener("click", function (ev) {
    if (!ev.target.closest("g.node")) { sel = null; render(); show(null); }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { sel = null; qbox.value = ""; render();
      show(null); }
  });
  ["t-look", "t-look-e", "t-adv", "t-self"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", function () {
      if (sel && !shown(byId[sel])) sel = null;
      render(); show(sel); fit();
    });
  });
  qbox.addEventListener("input", render);
  document.getElementById("b-in").addEventListener("click", function () {
    zoom(1.25); });
  document.getElementById("b-out").addEventListener("click", function () {
    zoom(.8); });
  document.getElementById("b-fit").addEventListener("click", fit);
  document.getElementById("b-clear").addEventListener("click", function () {
    sel = null; qbox.value = ""; render(); show(null);
  });
  document.getElementById("b-loop").addEventListener("click", function () {
    qbox.value = ""; sel = "AOS.OTC.SD/SD_C07"; render(); show(sel);
  });
  window.addEventListener("resize", fit);

  render(); show(null); fit();

  /* ------------------------------------------------- toc scroll spy */
  var secs = [].slice.call(document.querySelectorAll("main section[id]"));
  var links = {};
  [].slice.call(document.querySelectorAll("nav.toc a")).forEach(function (a) {
    links[a.getAttribute("href").slice(1)] = a;
  });
  var io = new IntersectionObserver(function (ents) {
    ents.forEach(function (en) {
      if (!en.isIntersecting) return;
      Object.keys(links).forEach(function (k) {
        links[k].classList.toggle("on", k === en.target.id);
      });
    });
  }, { rootMargin: "-48px 0px -72% 0px" });
  secs.forEach(function (s) { io.observe(s); });
})();
"""
