#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["shapely>=2.0", "matplotlib>=3.8"]
# ///
"""
Split an import area (a county, city, ... given by an OSM boundary relation in
the address source, see sources/*.json) into import blocks bounded by arterial
roads and railways, ordered outward from the area's centre, and generate one
changeset per block.

  ./blocks.py init sources/mecklenburg-nc.json   # pick the source, fetch roads, build blocks
  ./blocks.py fetch                 # area boundary, roads, place names -> network.json, places.json
  ./blocks.py build                 # network.json + places.json -> blocks.geojson
  ./blocks.py overview              # blocks/overview.png (block map, coloured by status)
  ./blocks.py run --next 5          # conflate the next 5 pending blocks
  ./blocks.py run --ids 3 7         # (re)generate specific blocks
  ./blocks.py status                # table of blocks and their state
  ./blocks.py mark uploaded 3       # record that you uploaded block 3

Everything lives in the project directory (default blocks/, or --dir DIR, so
several imports can sit side by side). Per block, DIR/out/NNNN/ holds changeset.osm (open in JOSM), review.geojson,
summary.txt, changeset_tags.txt and compare.png.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from shapely import STRtree
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from sources import Source, find_source  # noqa: E402

BDIR = ROOT / "blocks"
DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "osm-address-import/0.2 (OSM import tooling)"
CENTER = (0.0, 0.0)  # blocks are numbered outward from here (set_origin)
TO_M = TO_LL = None


def set_origin(lon: float, lat: float):
    global CENTER, TO_M, TO_LL
    CENTER = (lon, lat)
    kx, ky = 111320.0 * math.cos(math.radians(lat)), 110540.0
    TO_M = [kx, 0, 0, ky, 0, 0]
    TO_LL = [1 / kx, 0, 0, 1 / ky, 0, 0]


def load_config() -> dict:
    p = BDIR / "config.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(cfg: dict):
    (BDIR / "config.json").write_text(json.dumps(cfg, indent=1))


def source() -> Source:
    return find_source(None, BDIR)


def area_center(src: Source) -> tuple[float, float]:
    """The source's area.center, else the point the blocks were built around, else a point in the boundary."""
    if src.area.get("center"):
        return tuple(src.area["center"])
    if load_config().get("center"):
        return tuple(load_config()["center"])
    bnd = boundary_ll(src)
    if bnd is not None:
        c = bnd.representative_point()
        return c.x, c.y
    return (0.0, 0.0)


def boundary_ll(src: Source):
    p = BDIR / "network.json"
    if not p.exists():
        return None
    rid = src.area.get("relation")
    for e in json.loads(p.read_text())["elements"]:
        if e["type"] == "relation" and (not rid or e["id"] == rid):
            outers = [LineString([(g["lon"], g["lat"]) for g in mb["geometry"]])
                      for mb in e["members"] if mb.get("role") == "outer" and mb.get("geometry")]
            return unary_union(list(polygonize(unary_union(outers))))
    return None

BOUNDARY_CLASSES = {"motorway", "trunk", "primary", "secondary"}
SPLIT_CLASSES = {"tertiary"}
MAX_AREA = 2.5e6     # m²: faces bigger than this get split along tertiary roads
MIN_AREA = 6e4       # m²: smaller faces (interchanges, medians) merge into a neighbour
MIN_COMPACT = 0.12   # 4πA/P²: thinner faces (strips between parallel roads) merge too
MIN_COMPACT_AREA = 3e5


def m(geom):
    return affine_transform(geom, TO_M)


def ll(geom):
    return affine_transform(geom, TO_LL)


def compact(p):
    return 4 * math.pi * p.area / (p.length ** 2) if p.length else 0


def is_sliver(p):
    return p.area < MIN_AREA or (p.area < MIN_COMPACT_AREA and compact(p) < MIN_COMPACT)


def merge_slivers(polys):
    polys = [p for p in polys if not p.is_empty]
    while True:
        cands = sorted((i for i, p in enumerate(polys) if is_sliver(p)), key=lambda i: polys[i].area)
        merged = False
        for i in cands:
            p = polys[i]
            grow = p.buffer(1.0)
            best, best_len = None, 0.0
            for j, q in enumerate(polys):
                if j == i or not grow.intersects(q):
                    continue
                shared = q.boundary.intersection(grow).length
                if shared > best_len:
                    best, best_len = j, shared
            if best is None:
                continue
            # close hairline gaps between the sliver and its neighbour; if it
            # still doesn't touch, keep it as a separate part of the same block
            polys[best] = unary_union([polys[best], p]).buffer(1.0).buffer(-1.0)
            polys.pop(i)
            merged = True
            break
        if not merged:
            return polys


def faces(lines, frame):
    noded = unary_union(lines + [frame.boundary])
    return [f for f in polygonize(noded) if frame.contains(f.representative_point())]


def overpass(q: str, url: str, retries: int = 4) -> bytes:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=900) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"  Overpass request failed ({e}); retrying in {20 * (attempt + 1)}s", file=sys.stderr)
            time.sleep(20 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch(args):
    """Download the area boundary, the road/rail network that cuts it into blocks,
    and place names for labelling blocks."""
    src = source()
    rid = src.area.get("relation")
    if not rid:
        sys.exit(f"{src.path}: set area.relation to the OSM id of the import area's boundary relation")
    a = f"area({3600000000 + int(rid)})->.a;"
    classes = "|".join(sorted(BOUNDARY_CLASSES | SPLIT_CLASSES))
    print(f"Fetching boundary relation {rid} and its roads (this can take a few minutes)...")
    net = overpass(f"""[out:json][timeout:900];
rel({rid});out geom;
{a}
(way["highway"~"^({classes})$"](area.a);way["railway"="rail"][!"service"](area.a););
out geom tags;""", args.overpass)
    (BDIR / "network.json").write_bytes(net)
    print("Fetching place names...")
    places = overpass(f"""[out:json][timeout:300];
{a}
node["place"~"^(city|town|village|hamlet|suburb|quarter|neighbourhood)$"]["name"](area.a);
out;""", args.overpass)
    (BDIR / "places.json").write_bytes(places)
    n = json.loads(net)["elements"]
    print(f"wrote network.json ({sum(e['type'] == 'way' for e in n)} ways) and places.json "
          f"({len(json.loads(places)['elements'])} places)")


def init(args):
    BDIR.mkdir(parents=True, exist_ok=True)
    src = Source(args.source)
    cfg = load_config()
    try:
        cfg["source"] = str(Path(args.source).resolve().relative_to(ROOT))
    except ValueError:
        cfg["source"] = str(Path(args.source).resolve())
    save_config(cfg)
    print(f"{BDIR}: source {src.name}")
    if args.no_fetch:
        return
    fetch(args)
    set_origin(*area_center(src))
    build(args)


def build(args):
    net = json.loads((BDIR / "network.json").read_text())["elements"]
    center_m = m(Point(CENTER))

    bnd = boundary_ll(source())
    if bnd is None:
        sys.exit("area boundary not found in network.json (run ./blocks.py fetch)")
    frame = m(bnd)
    cfg = load_config()
    cfg["center"] = [round(CENTER[0], 6), round(CENTER[1], 6)]
    save_config(cfg)

    def lines_of(classes):
        out = []
        for e in net:
            t = e.get("tags", {})
            cls = t.get("highway") or ("rail" if t.get("railway") == "rail" else None)
            if e["type"] == "way" and cls in classes and "geometry" in e:
                out.append(m(LineString([(g["lon"], g["lat"]) for g in e["geometry"]])))
        return out

    major = lines_of(BOUNDARY_CLASSES | {"rail"})
    tertiary = lines_of(SPLIT_CLASSES)
    print(f"{len(major)} arterial/rail ways, {len(tertiary)} tertiary ways")

    blocks = merge_slivers(faces(major, frame))
    print(f"{len(blocks)} arterial blocks")
    ttree = STRtree(tertiary)
    out = []
    for b in blocks:
        if b.area <= MAX_AREA:
            out.append(b)
            continue
        cut = [tertiary[i] for i in ttree.query(b, predicate="intersects")]
        subs = merge_slivers(faces(cut, b)) if cut else [b]
        out += subs
    blocks = out
    print(f"{len(blocks)} blocks after splitting large ones on tertiary roads")

    # names: place nodes inside, else nearest; plus bounding road names
    places = [(m(Point(e["lon"], e["lat"])), e["tags"]["name"], e["tags"]["place"])
              for e in json.loads((BDIR / "places.json").read_text())["elements"] if e.get("tags", {}).get("name")]
    named_roads = []
    for e in net:
        t = e.get("tags", {})
        if e["type"] == "way" and "geometry" in e and t.get("highway") in BOUNDARY_CLASSES | SPLIT_CLASSES and t.get("name"):
            named_roads.append((m(LineString([(g["lon"], g["lat"]) for g in e["geometry"]])), t["name"]))
    rtree = STRtree([g for g, _ in named_roads])

    feats = []
    blocks.sort(key=lambda b: b.centroid.distance(center_m))
    for i, b in enumerate(blocks, start=1):
        inside = [n for p, n, k in places if b.contains(p) and k in ("neighbourhood", "suburb", "quarter")]
        if not inside and places:
            inside = [min(places, key=lambda pl: pl[0].distance(b))[1]]
        if not inside:
            inside = [f"Block {i}"]
        ring = b.boundary.buffer(25)
        lengths = Counter()
        for j in rtree.query(ring, predicate="intersects"):
            g, n = named_roads[j]
            lengths[n] += g.intersection(ring).length
        roads = [n for n, _ in lengths.most_common(4)]
        feats.append({
            "type": "Feature",
            "geometry": mapping(ll(b.simplify(2))),
            "properties": {
                "id": i, "name": " / ".join(inside[:2]), "roads": ", ".join(roads),
                "area_km2": round(b.area / 1e6, 3),
                "dist_km": round(b.centroid.distance(center_m) / 1000, 2),
            },
        })
    (BDIR / "blocks.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    areas = sorted(f["properties"]["area_km2"] for f in feats)
    print(f"wrote {len(feats)} blocks; area km² median {areas[len(areas)//2]}, max {areas[-1]}")


# --- state ---------------------------------------------------------------------

def load_blocks():
    return {f["properties"]["id"]: f for f in json.loads((BDIR / "blocks.geojson").read_text())["features"]}


def load_state():
    p = BDIR / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(s):
    tmp = BDIR / "state.json.tmp"
    tmp.write_text(json.dumps(s, indent=1, sort_keys=True))
    tmp.replace(BDIR / "state.json")


def set_block_state(i, entry):
    """Re-read before writing so status changes made elsewhere (web UI) aren't clobbered."""
    s = load_state()
    s[str(i)] = entry
    save_state(s)


def parse_summary(path: Path) -> dict:
    stats = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            stats[parts[0]] = int(parts[1])
    return stats


def write_ledger(exclude=None):
    """Addresses proposed by other blocks that are ready or uploaded, so a block
    never proposes an address another block's changeset already adds."""
    state = load_state()
    led = {}
    for p in (BDIR / "out").glob("*/changeset_keys.json"):
        bid = int(p.parent.name)
        if bid != exclude and state.get(str(bid), {}).get("status") in ("ready", "uploaded"):
            led[str(bid)] = json.loads(p.read_text())
    (BDIR / "ledger.json").write_text(json.dumps(led))


def run(args):
    blocks, state = load_blocks(), load_state()
    src = source()
    if args.ids:
        ids = args.ids
    else:
        ids = [i for i in sorted(blocks) if state.get(str(i), {}).get("status", "pending") == "pending"][: args.next]
    for i in ids:
        f = blocks[i]
        p = f["properties"]
        d = BDIR / "out" / f"{i:04d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "block.geojson").write_text(json.dumps(f))
        if args.refresh and (d / ".cache").exists():
            import shutil
            shutil.rmtree(d / ".cache")
        print(f"\n=== block {i}: {p['name']} ({p['roads']}), {p['area_km2']} km², {p['dist_km']} km out")
        write_ledger(exclude=i)
        r = subprocess.run([str(ROOT / "conflate.py"), f"--source={src.path}", f"--polygon={d / 'block.geojson'}",
                            f"--out={d / 'changeset'}", "--cache", f"--ledger={BDIR / 'ledger.json'}",
                            f"--block-id={i}", *args.conflate_args],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-3000:])
            set_block_state(i, {"status": "error", "error": (r.stdout + r.stderr)[-800:]})
            continue
        stats = parse_summary(d / "changeset_summary.txt")
        changes = stats.get("changes_total", 0)
        npts = stats.get("county_points", 0)
        cover = stats.get("already_in_osm", 0) / npts if npts else 1
        status = "ready" if changes else "nothing_to_do"
        (d / "changeset_tags.txt").write_text(
            f"comment={src.changeset['comment']}: {p['name']} (block {i})\n"
            f"source={src.changeset['source']}\n"
            "import=yes\n"
            f"url={args.wiki or '<import wiki page>'}\n")
        if changes:
            bounds = shape(f["geometry"]).bounds
            subprocess.run([str(ROOT / "render.py"), f"--bbox={','.join(f'{x:.5f}' for x in bounds)}",
                            f"--out={d / 'changeset'}", f"--polygon={d / 'block.geojson'}",
                            f"--name=Block {i}: {p['name']}"], capture_output=True, text=True)
        entry = {"status": status, "changes": changes, "county_points": npts,
                 "osm_coverage_before": round(cover, 3), "review": stats.get("review_total", 0),
                 "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        set_block_state(i, entry)
        print(f"  -> {status}: {changes} changes, {entry['review']} for review, "
              f"{cover:.0%} of {src.short_name} addresses already in OSM")


def status(args):
    blocks, state = load_blocks(), load_state()
    print(f"{'id':>5}  {'status':14} {'chg':>5} {'rev':>4} {'cov':>5}  {'km':>5}  name / roads")
    for i in sorted(blocks)[: args.limit]:
        s = state.get(str(i), {})
        p = blocks[i]["properties"]
        cov = f"{s['osm_coverage_before']:.0%}" if "osm_coverage_before" in s else ""
        print(f"{i:>5}  {s.get('status', 'pending'):14} {s.get('changes', ''):>5} {s.get('review', ''):>4} {cov:>5}  "
              f"{p['dist_km']:>5}  {p['name']}: {p['roads']}")
    c = Counter(state.get(str(i), {}).get("status", "pending") for i in blocks)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())))


def mark(args):
    state = load_state()
    for i in args.ids:
        state.setdefault(str(i), {})["status"] = args.status
    save_state(state)


def overview(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.patches import Polygon as MPoly

    blocks, state = load_blocks(), load_state()
    colors = {"pending": "#ece8df", "ready": "#4caf6e", "nothing_to_do": "#7fa7d6",
              "uploaded": "#2e6b45", "skipped": "#bbbbbb", "error": "#d64545"}
    fig, ax = plt.subplots(figsize=(12, 14), dpi=170)
    xs, ys = [], []
    for i, f in blocks.items():
        st = state.get(str(i), {}).get("status", "pending")
        g = shape(f["geometry"])
        for part in getattr(g, "geoms", [g]):
            c = list(part.exterior.coords)
            xs += [x for x, _ in c]
            ys += [y for _, y in c]
            ax.add_patch(MPoly(c, closed=True, facecolor=colors.get(st, "#ece8df"), edgecolor="#8a8578", linewidth=0.3))
        if i <= args.label:
            pt = g.representative_point()
            ax.text(pt.x, pt.y, str(i), fontsize=4 if not args.zoom else 7, ha="center", va="center",
                    color="#333333", clip_on=True)
    if args.zoom:
        cx, cy, r = CENTER[0], CENTER[1], args.zoom / 111.0
        ax.set_xlim(cx - r / math.cos(math.radians(cy)), cx + r / math.cos(math.radians(cy)))
        ax.set_ylim(cy - r, cy + r)
    else:
        ax.set_xlim(min(xs), max(xs))
        ax.set_ylim(min(ys), max(ys))
    ax.set_aspect(1 / math.cos(math.radians(CENTER[1])))
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(handles=[Patch(facecolor=v, edgecolor="#8a8578", label=k) for k, v in colors.items()],
              loc="lower right", fontsize=8)
    c = Counter(state.get(str(i), {}).get("status", "pending") for i in blocks)
    ax.set_title(f"{source().name} import: {len(blocks)} blocks  ({', '.join(f'{k} {v}' for k, v in sorted(c.items()))})",
                 loc="left", fontsize=12)
    fig.tight_layout()
    out = BDIR / (f"overview_{args.zoom}km.png" if args.zoom else "overview.png")
    fig.savefig(out, facecolor="white")
    print(f"wrote {out}")


def main():
    global BDIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="project directory (default: blocks/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    it = sub.add_parser("init", help="start an import: choose the address source, fetch roads, build blocks")
    it.add_argument("source", help="address source definition, e.g. sources/mecklenburg-nc.json")
    it.add_argument("--no-fetch", action="store_true", help="only record the source")
    it.add_argument("--overpass", default=DEFAULT_OVERPASS)
    ft = sub.add_parser("fetch", help="download boundary, roads and place names for the area")
    ft.add_argument("--overpass", default=DEFAULT_OVERPASS)
    sub.add_parser("build")
    r = sub.add_parser("run")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--next", type=int)
    g.add_argument("--ids", type=int, nargs="+")
    r.add_argument("--wiki", help="import wiki page URL for changeset tags")
    r.add_argument("--refresh", action="store_true", help="re-download OSM and source data (do this right before uploading)")
    r.add_argument("conflate_args", nargs="*", help="extra args passed to conflate.py (after --)")
    s = sub.add_parser("status")
    s.add_argument("--limit", type=int, default=40)
    mk = sub.add_parser("mark")
    mk.add_argument("status", choices=["pending", "ready", "uploaded", "skipped", "nothing_to_do"])
    mk.add_argument("ids", type=int, nargs="+")
    o = sub.add_parser("overview")
    o.add_argument("--zoom", type=float, help="km radius around the area centre")
    o.add_argument("--label", type=int, default=400, help="label block ids up to this number")
    a = ap.parse_args()
    if a.dir:
        BDIR = Path(a.dir).resolve()
    if a.cmd not in ("init", "fetch"):
        set_origin(*area_center(source()))
    {"init": init, "fetch": fetch, "build": build, "run": run, "status": status, "mark": mark,
     "overview": overview}[a.cmd](a)


if __name__ == "__main__":
    main()
