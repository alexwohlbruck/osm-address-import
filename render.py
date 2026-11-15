#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.8"]
# ///
"""
Before/after render of a conflate.py run.

  ./render.py --bbox=-80.826,35.208,-80.817,35.214 --out out/elizabeth

Reads the cached Overpass response (run conflate.py with --cache) plus
<out>.osm and <out>_review.geojson; writes <out>_before.png, <out>_after.png
and <out>_compare.png.
"""

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

BG, STREET, STREET_LABEL = "#f6f4ef", "#ffffff", "#8a8578"
BLD, BLD_EDGE = "#d9d4c7", "#b5ae9c"
HAD = "#7fa7d6"       # already had an address
NEW = "#4caf6e"       # tagged by this run
NODE = "#e8902a"      # new address node
REVIEW = "#d64545"


def load(args):
    out = Path(args.out)
    tag = args.bbox.replace(",", "_")
    cache = out.parent / ".cache" / f"osm_v2_{tag}.json"
    if not cache.exists():
        cache = out.parent / ".cache" / f"osm_{tag}.json"
    els = {}
    for e in json.loads(cache.read_text())["elements"]:
        els.setdefault((e["type"], e["id"]), {}).update(e)
    nodes = {i: (e["lon"], e["lat"]) for (t, i), e in els.items() if t == "node" and "lat" in e}
    buildings = {i: e for (t, i), e in els.items() if t == "way" and "nodes" in e and "building" in e.get("tags", {})}
    streets = [e for (t, i), e in els.items() if t == "way" and "geometry" in e and "highway" in e.get("tags", {})]
    addr_nodes = [(e["lon"], e["lat"], e["tags"]) for (t, i), e in els.items()
                  if t == "node" and "addr:housenumber" in e.get("tags", {})]

    root = ET.parse(out.with_suffix(".osm")).getroot()
    modified = {int(w.get("id")): {t.get("k"): t.get("v") for t in w.findall("tag")}
                for w in root.findall("way") if w.get("action") == "modify"}
    new_nodes = [(float(n.get("lon")), float(n.get("lat")), {t.get("k"): t.get("v") for t in n.findall("tag")})
                 for n in root.findall("node") if n.get("id").startswith("-")]
    review = json.loads(Path(f"{out}_review.geojson").read_text())["features"]
    return nodes, buildings, streets, addr_nodes, modified, new_nodes, review


def setup(ax, bbox, title):
    w, s, e, n = bbox
    ax.set_xlim(w, e)
    ax.set_ylim(s, n)
    ax.set_aspect(1 / math.cos(math.radians((s + n) / 2)))
    ax.set_facecolor(BG)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_color("#cccccc")
    ax.set_title(title, fontsize=15, loc="left", pad=10, color="#333333", fontweight="bold")


def draw_streets(ax, streets, bbox):
    w, s, e, n = bbox
    major = {"primary", "secondary", "tertiary", "trunk"}
    lines, widths = [], []
    for st in streets:
        hw = st["tags"]["highway"]
        if hw in ("footway", "path", "service", "steps", "cycleway"):
            continue
        lines.append([(g["lon"], g["lat"]) for g in st["geometry"]])
        widths.append(6 if hw in major else 3.5)
    ax.add_collection(LineCollection(lines, colors="#e2ddd2", linewidths=[x + 1.5 for x in widths], zorder=1, capstyle="round"))
    ax.add_collection(LineCollection(lines, colors=STREET, linewidths=widths, zorder=2, capstyle="round"))
    # one label per street name, at the middle of its longest segment inside the view
    best = {}
    for st in streets:
        name = st["tags"].get("name")
        pts = [(g["lon"], g["lat"]) for g in st["geometry"] if w < g["lon"] < e and s < g["lat"] < n]
        if name and len(pts) >= 2 and st["tags"]["highway"] not in ("footway", "path", "service"):
            if name not in best or len(pts) > len(best[name]):
                best[name] = pts
    k = math.cos(math.radians((s + n) / 2))
    for name, pts in best.items():
        i = len(pts) // 2
        (x0, y0), (x1, y1) = pts[max(i - 1, 0)], pts[min(i, len(pts) - 1)]
        ang = math.degrees(math.atan2(y1 - y0, (x1 - x0) * k))
        if ang > 90: ang -= 180
        if ang < -90: ang += 180
        ax.text(*pts[i], name, fontsize=6.5, color=STREET_LABEL, rotation=ang, rotation_mode="anchor",
                ha="center", va="center", zorder=3, style="italic", clip_on=True)


def poly(b, nodes):
    return [nodes[n] for n in b["nodes"] if n in nodes]


def centroid(pts):
    xs, ys = zip(*pts)
    return sum(xs) / len(xs), sum(ys) / len(ys)


def label(ax, xy, text, color):
    ax.text(*xy, text, fontsize=5.5, ha="center", va="center", color=color, zorder=6, fontweight="bold", clip_on=True)


def render(ax, bbox, data, after: bool):
    nodes, buildings, streets, addr_nodes, modified, new_nodes, review = data
    draw_streets(ax, streets, bbox)
    plain, had, new = [], [], []
    for wid, b in buildings.items():
        p = poly(b, nodes)
        if len(p) < 3:
            continue
        if "addr:housenumber" in b["tags"]:
            had.append((p, b["tags"]["addr:housenumber"]))
        elif after and wid in modified:
            t = modified[wid]
            new.append((p, t["addr:housenumber"] + (f" #{t['addr:unit']}" if "addr:unit" in t else "")))
        else:
            plain.append(p)
    ax.add_collection(PolyCollection(plain, facecolors=BLD, edgecolors=BLD_EDGE, linewidths=0.5, zorder=4))
    ax.add_collection(PolyCollection([p for p, _ in had], facecolors=HAD, edgecolors="#4f7fb5", linewidths=0.5, zorder=4))
    ax.add_collection(PolyCollection([p for p, _ in new], facecolors=NEW, edgecolors="#2e7d4a", linewidths=0.6, zorder=4))
    for p, hn in had:
        label(ax, centroid(p), hn, "#1d3d63")
    for p, hn in new:
        label(ax, centroid(p), hn, "#0d3b1e")
    # existing standalone address nodes
    if addr_nodes:
        ax.scatter([x for x, _, _ in addr_nodes], [y for _, y, _ in addr_nodes], s=9, c=HAD, edgecolors="#1d3d63",
                   linewidths=0.4, zorder=5)
    if after:
        if new_nodes:
            ax.scatter([x for x, _, _ in new_nodes], [y for _, y, _ in new_nodes], s=12, c=NODE, edgecolors="#7a4510",
                       linewidths=0.4, zorder=7)
            for x, y, t in new_nodes:
                ax.text(x, y + 0.000045, f"#{t['addr:unit']}" if "addr:unit" in t else t["addr:housenumber"], fontsize=4.5, ha="center", va="bottom", color="#7a4510", zorder=7, clip_on=True)
        if review:
            ax.scatter([f["geometry"]["coordinates"][0] for f in review], [f["geometry"]["coordinates"][1] for f in review],
                       s=28, marker="x", c=REVIEW, linewidths=1.1, zorder=8)
    return len(plain), len(had), len(new)


def outline(ax, bbox, geom):
    """Fade everything outside the block and draw its boundary."""
    from matplotlib.path import Path as MPath
    from matplotlib.patches import PathPatch
    w, s, e, n = bbox
    pad = 1.0
    verts = [(w - pad, s - pad), (e + pad, s - pad), (e + pad, n + pad), (w - pad, n + pad), (w - pad, s - pad)]
    codes = [MPath.MOVETO] + [MPath.LINETO] * 3 + [MPath.CLOSEPOLY]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    rings = []
    for poly in polys:
        for ring in poly:
            verts += [tuple(c) for c in ring]
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(ring) - 2) + [MPath.CLOSEPOLY]
            rings.append(ring)
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor="white", alpha=0.6, edgecolor="none", zorder=9))
    for ring in rings:
        ax.plot([c[0] for c in ring], [c[1] for c in ring], color="#333333", lw=1.4, ls=(0, (5, 3)), zorder=10)


def legend(ax, after):
    h = [Patch(facecolor=BLD, edgecolor=BLD_EDGE, label="Building, no address"),
         Patch(facecolor=HAD, edgecolor="#4f7fb5", label="Already addressed in OSM")]
    if after:
        h += [Patch(facecolor=NEW, edgecolor="#2e7d4a", label="Address added to building"),
              Line2D([], [], marker="o", ls="", markerfacecolor=NODE, markeredgecolor="#7a4510", label="New address node"),
              Line2D([], [], marker="x", ls="", color=REVIEW, label="Needs manual review")]
    ax.legend(handles=h, loc="lower left", fontsize=8, framealpha=0.92, edgecolor="#cccccc").set_zorder(20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--view", help="minlon,minlat,maxlon,maxlat to zoom into (default: --bbox)")
    ap.add_argument("--suffix", default="", help="appended to output file names")
    ap.add_argument("--polygon", help="GeoJSON block outline to draw")
    args = ap.parse_args()
    data = load(args)
    bbox = tuple(float(x) for x in (args.view or args.bbox).split(","))
    name = args.name or Path(args.out).name.title()
    geom = None
    if args.polygon:
        g = json.loads(Path(args.polygon).read_text())
        geom = g.get("geometry", g)
    w, s, e, n = bbox
    aspect = (n - s) / ((e - w) * math.cos(math.radians((s + n) / 2)))

    stats = {}
    for after in (False, True):
        fig, ax = plt.subplots(figsize=(11, 11 * aspect + 0.6), dpi=220)
        setup(ax, bbox, f"{name}: {'after' if after else 'before'}")
        stats[after] = render(ax, bbox, data, after)
        if geom:
            outline(ax, bbox, geom)
        legend(ax, after)
        fig.tight_layout()
        fig.savefig(f"{args.out}{args.suffix}_{'after' if after else 'before'}.png", facecolor="white")
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(20, 10 * aspect + 1), dpi=180)
    for ax, after in zip(axes, (False, True)):
        setup(ax, bbox, "Before" if not after else "After")
        render(ax, bbox, data, after)
        if geom:
            outline(ax, bbox, geom)
        legend(ax, after)
    plain0, had0, _ = stats[False]
    plain1, had1, new1 = stats[True]
    total = plain0 + had0
    fig.suptitle(f"{name}: addressed buildings {had0}/{total} → {had1 + new1}/{total}   "
                 f"(+{new1} buildings, +{len(data[5])} address nodes, {len(data[6])} for review)",
                 fontsize=14, x=0.01, ha="left", color="#333333")
    fig.tight_layout()
    fig.savefig(f"{args.out}{args.suffix}_compare.png", facecolor="white")
    print(f"buildings addressed: {had0}/{total} -> {had1 + new1}/{total}; new nodes {len(data[5])}; review {len(data[6])}")


if __name__ == "__main__":
    main()
