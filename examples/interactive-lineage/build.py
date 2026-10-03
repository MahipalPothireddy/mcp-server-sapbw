"""Render the interactive lineage example to a single self-contained HTML file.

    python examples/interactive-lineage/build.py

Writes ``lineage-report.html`` beside this file. CSS, JavaScript and the graph data are inlined,
so the page opens from the local filesystem with no network access and nothing to install.

Facts come from ``sample_landscape.py`` and are entirely synthetic. The layout is computed here in
Python so the page needs no layout library; the browser only draws and filters.
"""

from __future__ import annotations

import html
import json
import os
from typing import Any

from assets import CSS, JS
from sample_landscape import (
    CALC_VIEW_BASIS,
    CALC_VIEWS,
    EDGES,
    FINDINGS,
    GENERATED,
    LAYERS,
    LOOKUP_GROUP,
    OBJECTS,
    PROVIDER,
    QUERY,
    ROUTINE_BASIS,
    TRANSFORMATIONS,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "lineage-report.html")

E = html.escape

NODE_W, NODE_H = 170, 44
COL_W, ROW_PITCH = 248, 56
PAD_X, PAD_Y = 28, 34

KIND_NAME = {
    "query": "BEx query",
    "cp": "CompositeProvider",
    "adso": "Advanced DSO",
    "ds": "DataSource",
    "calc": "HANA calculation view",
    "src": "Source / logical system",
    "iobj": "InfoObject",
}
ORIGIN_NOTE = {
    "stored": "BW's own text - stored verbatim in the object's text table",
    "stored_augmented": "BW's text, extended - the short text is BW's; the long text was "
    "generated because the stored one added nothing",
    "generated": "generated - no usable text is stored in BW",
}
SEV_ORDER = {"High": 0, "Medium": 1, "Low": 2}
DOT = " \u00b7 "
#: Routine size above which the line count is flagged. Arbitrary, and the report says so rather
#: than implying a threshold anyone agreed on.
BIG_ROUTINE_LINES = 400


# ------------------------------------------------------------------ helpers
def tw(head: list[str], rows: list[list[str]], num: set[int] | None = None) -> str:
    num = num or set()
    th = "".join(f"<th{' class=num' if i in num else ''}>{h}</th>" for i, h in enumerate(head))
    body = "".join(
        "<tr>"
        + "".join(f"<td{' class=num' if i in num else ''}>{c}</td>" for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def kv(pairs: list[tuple[str, str]]) -> str:
    return '<dl class="kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"


def card(inner: str, cls: str = "") -> str:
    return f'<div class="card {cls}">{inner}</div>'


def sec(sid: str, no: str, title: str, inner: str) -> str:
    return f'<section id="{sid}"><div class="secno">{no}</div><h2>{title}</h2>{inner}</section>'


def mono(s: str) -> str:
    return f"<code>{E(s)}</code>"


# ------------------------------------------------------------------ graph
def build_graph() -> dict[str, Any]:
    """Nodes and edges with a layered layout, ready for the page to draw."""
    layer_of: dict[str, int] = {}
    order_of: dict[str, int] = {}
    for layer, (_name, members) in enumerate(LAYERS):
        for order, nid in enumerate(members):
            layer_of[nid] = layer
            order_of[nid] = order

    missing = [nid for nid, _ in OBJECTS.items() if nid not in layer_of]
    if missing:
        raise KeyError(f"objects with no layer assignment: {missing}")

    nodes: list[dict[str, Any]] = []
    for nid, (kind, description, origin) in OBJECTS.items():
        meta: dict[str, Any] = {"description": description}
        if origin:
            meta["description_origin"] = ORIGIN_NOTE[origin]
        if kind == "ds":
            meta["role"] = "the extraction boundary"
        nodes.append(
            {
                "id": nid,
                "label": nid.split("/")[-1],
                "kind": kind,
                "layer": layer_of[nid],
                "order": order_of[nid],
                "group": "lookup" if nid in LOOKUP_GROUP else "spine",
                "sub": description,
                "meta": meta,
            }
        )

    by_id = {n["id"]: n for n in nodes}
    edges: list[dict[str, Any]] = []
    for src, dst, kind, label, basis, note in EDGES:
        if src not in by_id or dst not in by_id:
            raise KeyError(f"edge {src} -> {dst} references an undrawn node")
        edges.append(
            {
                "src": src,
                "dst": dst,
                "kind": kind,
                "label": label,
                "basis": basis,
                "note": note,
                "back": by_id[dst]["layer"] <= by_id[src]["layer"] and src != dst,
            }
        )

    degree = {n["id"]: {"in": 0, "out": 0} for n in nodes}
    for e in edges:
        degree[e["dst"]]["in"] += 1
        degree[e["src"]]["out"] += 1
    for n in nodes:
        n["degree"] = degree[n["id"]]

    counts: dict[int, int] = {}
    for n in nodes:
        counts[n["layer"]] = counts.get(n["layer"], 0) + 1
    tallest = max(counts.values())
    for n in nodes:
        top = PAD_Y + (tallest - counts[n["layer"]]) * ROW_PITCH / 2
        n["x"] = PAD_X + n["layer"] * COL_W
        n["y"] = top + n["order"] * ROW_PITCH

    return {
        "layers": [{"name": name, "note": ""} for name, _ in LAYERS],
        "nodes": nodes,
        "edges": edges,
        "width": PAD_X * 2 + (len(LAYERS) - 1) * COL_W + NODE_W,
        "height": PAD_Y * 2 + tallest * ROW_PITCH,
        "node_w": NODE_W,
        "node_h": NODE_H,
        "pad_x": PAD_X,
        "pad_y": PAD_Y,
        "col_w": COL_W,
        "row_pitch": ROW_PITCH,
    }


# ------------------------------------------------------------------ sections
def header() -> str:
    chips = [
        ("Provider", f"{PROVIDER['name']} (CompositeProvider)"),
        ("Parts", f"{PROVIDER['part_count']} Advanced DSOs"),
        ("Objects drawn", str(len(OBJECTS))),
        ("Data", "synthetic"),
        ("Rendered", GENERATED),
    ]
    chip_html = "".join(f'<span class="chip">{k} <b>{E(v)}</b></span>' for k, v in chips)
    nav = [
        ("intro", "About"),
        ("s1", "1 Findings"),
        ("s2", "2 Diagram"),
        ("s3", "3 Transformations"),
        ("s4", "4 HANA views"),
    ]
    links = "".join(f'<a href="#{i}">{t}</a>' for i, t in nav)
    return f"""<header class="top">
  <div style="max-width:1280px;margin:0 auto">
    <div style="color:#94a3b8;font-size:13px;letter-spacing:.8px;
      text-transform:uppercase">Example output &middot; synthetic data</div>
    <h1>{E(QUERY["name"])}</h1>
    <div class="sub">{E(QUERY["description"])}</div>
    <div class="chips">{chip_html}</div>
  </div>
</header>
<nav class="toc"><div style="max-width:1280px;margin:0 auto">{links}</div></nav>"""


def intro() -> str:
    body = """<h3>What this is</h3>
<p>An example of what <code>mcp-server-sapbw</code> produces for one BEx query: the objects behind
it, how they connect, what the transformations do, and what the HANA calculation views compute.
It is a static file &mdash; CSS, JavaScript and data inlined, no network access, nothing to
install.</p>
<p><b>Every name in it is invented.</b> The repository contains no customer metadata and this
example is not an exception: the landscape below is synthetic, and the repository's CI fails the
build if a real BW object name appears anywhere in the tree or its history. What is realistic is
the <i>shape</i> &mdash; a report on a CompositeProvider that unions five stores, two of them
full-loaded out of HANA calculation views that read other BW objects and hand the result back.
That shape is what makes the output worth reading, and a tidy three-box example would not
demonstrate it.</p>
<h3>What to try</h3>
<ul class="tight">
  <li><b>Click a box.</b> The panel shows what the object is, where that description came from,
      and every dependency with the mechanism that produced it. The rest of the graph dims.</li>
  <li><b>Switch on master-data lookups.</b> The diagram re-lays itself out over what is left, so
      hiding a column does not leave a gap.</li>
  <li><b>Hover an edge.</b> It says whether the relationship is declared in metadata or was
      parsed out of ABAP &mdash; the distinction the rest of this page keeps making.</li>
  <li><b>Search</b>, drag to pan, scroll to zoom, <kbd>Esc</kbd> to reset.</li>
</ul>
<h3>The one thing to take from it</h3>
<p>Every fact carries how it was established. <span class="tagok">Declared</span> means a metadata
row states it. <span class="tagwarn">Advisory</span> means it was parsed out of ABAP or resolved
from a generated name, so the set is a <b>lower bound</b> &mdash; dynamic SQL, function-module
calls and class methods are not followed. An advisory edge is real; the absence of one proves
nothing. Descriptions work the same way: anything not stored verbatim in BW is marked, because a
synthesized description must never be indistinguishable from one BW holds.</p>"""
    return f'<section id="intro">{card(body, "note")}</section>'


def s1_findings() -> str:
    rows = []
    for code, sev, headline, detail, evidence in sorted(FINDINGS, key=lambda f: SEV_ORDER[f[1]]):
        rows.append(
            [
                f"<b>{code}</b>",
                f'<span class="sev {sev}">{sev}</span>',
                f"<b>{headline}</b><p style='margin:5px 0 0'>{detail}</p>"
                f"<p class='small' style='margin:6px 0 0'>Evidence: "
                f"<code>{E(DOT.join(evidence))}</code></p>",
            ]
        )
    return sec(
        "s1",
        "Section 1",
        "Findings",
        "<p>Ordered by severity. Each one names the metadata it rests on, so a disputed "
        "finding can be traced back to the rows behind it rather than argued about.</p>"
        + tw(["#", "Severity", "Finding"], rows),
    )


def s2_diagram(graph: dict[str, Any]) -> str:
    swatches = "".join(
        f'<span><span class="swatch" style="background:var(--k-{k});'
        f'border-color:var(--k-{k}-e)"></span>{t}</span>'
        for k, t in [
            ("src", "Source system"),
            ("ds", "DataSource"),
            ("adso", "Advanced DSO"),
            ("iobj", "InfoObject"),
            ("cp", "CompositeProvider"),
            ("calc", "HANA calc view"),
            ("query", "BEx query"),
        ]
    )
    lines = "".join(
        f'<span><span class="ln" style="border-top:2px '
        f'{"dashed" if d else "solid"} {c}"></span>{t}</span>'
        for c, d, t in [
            ("#be123c", "", "declared FULL load"),
            ("#64748b", "", "declared delta load"),
            ("#15803d", "", "CompositeProvider part"),
            ("#0e7490", "", "read by a HANA calc view"),
            ("#0e7490", "6 3", "exposed back to BW"),
            ("#7e22ce", "", "read by the query"),
            ("#2563eb", "5 4", "declared lookup"),
            ("#b45309", "2 3", "routine-derived (advisory)"),
        ]
    )
    back = sum(1 for e in graph["edges"] if e["back"])
    intro_text = f"""<p>Laid out left to right by dependency depth. Every box is an object, every
line a dependency with the mechanism that produced it, and every box's second line is the object's
description.</p>
<p class="small">{len(graph["nodes"])} objects and {len(graph["edges"])} dependencies in total,
{back} of the dependencies running backwards &mdash; those are the loops. A box with a dashed
self-loop has a transformation whose source and target are the same object. The counter on the
right says how many the current filters leave visible.</p>"""
    viz = f"""<div class="bleed"><div class="viz">
  <div class="vizbar">
    <div class="grp">
      <button class="btn" id="b-fit" type="button">Fit</button>
      <button class="btn" id="b-in" type="button" aria-label="Zoom in">+</button>
      <button class="btn" id="b-out" type="button" aria-label="Zoom out">&minus;</button>
      <button class="btn" id="b-loop" type="button">Show the HANA loop</button>
      <button class="btn" id="b-clear" type="button">Clear</button>
    </div>
    <div class="grp">
      <label><input type="checkbox" id="t-look"> master-data &amp; lookup targets</label>
      <label><input type="checkbox" id="t-look-e" checked> declared lookups</label>
      <label><input type="checkbox" id="t-adv" checked> advisory edges</label>
      <label><input type="checkbox" id="t-self" checked> self re-processing</label>
    </div>
    <input id="q" type="search" placeholder="Search object name&hellip;"
      aria-label="Search object name">
    <span class="hint" id="vcount"></span>
  </div>
  <noscript><div class="card caution" style="margin:14px">The diagram is drawn by script in the
  page, so it needs JavaScript enabled. Nothing is fetched from the network. The same
  relationships are written out as text in sections 3 and 4.</div></noscript>
  <div class="vizmain">
    <div id="stage">
      <svg id="svg" role="img" aria-label="Dependency graph of the example query."></svg>
      <div class="tip" id="tip" role="status"></div>
    </div>
    <aside id="panel" aria-live="polite"></aside>
  </div>
  <div class="legend">{swatches}</div>
  <div class="legend">{lines}</div>
</div></div>"""
    tail = """<p class="small" style="margin-top:14px">Master-data and lookup targets are hidden
by default. On a real landscape far more is hidden: the full upstream graph for one query routinely
runs to a couple of hundred objects, most of them the master-data supply chains behind customer,
material and plant.</p>"""
    return sec("s2", "Section 2", "The shape of the flow", intro_text + viz + tail)


def s3_transformations() -> str:
    rows = []
    for t in TRANSFORMATIONS:
        mix = ", ".join(
            f"{n}&nbsp;{k}" for k, n in sorted(t["rules"].items(), key=lambda kv: (-kv[1], kv[0]))
        )
        anti = ", ".join(t["anti"])
        lines = (
            f'<span class="tagbad">{t["lines"]:,}</span>'
            if t["lines"] >= BIG_ROUTINE_LINES
            else f"{t['lines']:,}"
            if t["lines"]
            else "&mdash;"
        )
        rows.append(
            [
                f"<b>{E(t['target'])}</b>",
                mono(t["source"]) + (' <span class="tagbad">self</span>' if t["self"] else ""),
                str(t["fields"]),
                mix,
                E(t["routines"]),
                lines,
                ", ".join(t["reads"]) or '<span class="tagmut">none resolved</span>',
                f'<span class="tagwarn">{anti}</span>'
                if anti
                else '<span class="tagok">none detected</span>',
                f'<span class="tagmut">{E(", ".join(t["unresolved"]))}</span>'
                if t["unresolved"]
                else "&mdash;",
            ]
        )
    table = tw(
        [
            "Target",
            "Loaded from",
            "Fields",
            "How the fields are derived",
            "Routines",
            "ABAP lines",
            "BW objects read",
            "Anti-patterns",
            "Calls not followed",
        ],
        rows,
        num={2, 5},
    )
    return sec(
        "s3",
        "Section 3",
        "What the transformations do",
        "<p>One row per transformation into the five parts. Read the "
        "<b>Fields</b> and <b>ABAP lines</b> columns together: the rows with the most "
        "fields are rarely the rows with the code. Here the three self-referencing "
        "transformations hold every line of it, and the DataSource feeds hold none "
        "&mdash; which is the opposite of where a change review usually looks.</p>"
        + table
        + f'<p class="small">{ROUTINE_BASIS}</p>',
    )


def s4_hana() -> str:
    blocks = []
    for name, v in CALC_VIEWS.items():
        node_rows = [
            [
                f"<b>{E(n)}</b>",
                E(typ),
                mono(src),
                mono(filt) if filt != "none" else '<span class="tagmut">none</span>',
            ]
            for n, typ, src, filt in v["nodes"]
        ]
        calc = ""
        if v["calculated"]:
            calc = "<h4>Calculated columns &mdash; logic that lives in HANA, not in BW</h4>" + tw(
                ["Column", "Node", "Formula"],
                [[f"<b>{E(c)}</b>", mono(n), mono(f)] for c, n, f in v["calculated"]],
            )
        blocks.append(
            card(
                f'<h4 style="margin-top:0">{E(name)}</h4>'
                f'<p class="lead" style="margin-bottom:8px"><b>{E(v["produces"])}</b></p>'
                + kv(
                    [
                        ("How BW names it", E(v["named_by"])),
                        ("How it is built", v["how"]),
                        ("What that means", E(v["reading"])),
                        ("Worth knowing", E(v["watch"])),
                    ]
                ),
                "note",
            )
            + tw(["Node", "Type", "Reads", "Filter"], node_rows)
            + calc
        )
    return sec(
        "s4",
        "Section 4",
        "What the HANA calculation views compute",
        "<p>Two of the five parts are not loaded from a DataSource at all. They are "
        "full-loaded from CompositeProviders whose only content is a HANA calculation "
        "view, and those views read other BW objects directly. The data leaves BW, is "
        "reshaped in HANA, and comes back as a load &mdash; a closed loop that BW raises "
        "no where-used warning about.</p>"
        + "".join(blocks)
        + f'<p class="small">{CALC_VIEW_BASIS}</p>',
    )


def footer() -> str:
    return f"""<footer>
<p>Rendered {E(GENERATED)} by <code>examples/interactive-lineage/build.py</code> from
<code>sample_landscape.py</code>. Regenerate with
<code>python examples/interactive-lineage/build.py</code>.</p>
<p><b>Synthetic data throughout.</b> Against a real system the same renderer is driven by
<code>bw_get_lineage</code>, <code>bw_describe_object</code>, <code>bw_get_transformation</code>,
<code>bw_analyze_routine</code> and <code>bw_get_calc_view_logic</code>, and the output belongs
outside the repository &mdash; it is customer intellectual property.</p>
</footer>"""


# ------------------------------------------------------------------ assemble
def render() -> str:
    graph = build_graph()
    payload = (
        json.dumps(graph, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    body = (
        header()
        + "<main>"
        + intro()
        + s1_findings()
        + s2_diagram(graph)
        + s3_transformations()
        + s4_hana()
        + "</main>"
        + footer()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(QUERY["name"])} \u2014 interactive lineage example</title>
<meta name="generator" content="mcp-server-sapbw">
<style>{CSS}</style>
</head>
<body>
{body}
<script>window.__LINEAGE_GRAPH__ = {payload};</script>
<script>{JS}</script>
</body>
</html>
"""


def build() -> str:
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render())
    return OUT


if __name__ == "__main__":
    print(build())
