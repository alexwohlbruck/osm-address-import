#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["shapely"]
# ///
"""
Local web UI for the Mecklenburg address import.

  ./webapp/server.py            # http://127.0.0.1:8765
  ./webapp/server.py --port 9000

Wraps blocks.py: browse blocks on a map, generate/refresh changesets (one job
at a time, so Overpass isn't hammered), inspect changes and review items,
open a block in JOSM via remote control, and track upload status.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
BDIR = ROOT / "blocks"
STATIC = Path(__file__).resolve().parent / "static"
CONFIG = BDIR / "config.json"
STATUSES = ["pending", "ready", "nothing_to_do", "uploaded", "skipped", "error"]
SERVED_FILES = {"changeset.osm", "changeset_review.geojson", "changeset_compare.png",
                "changeset_before.png", "changeset_after.png", "changeset_summary.txt", "block.geojson",
                "changeset_corrections.json", "changeset_pois.json"}

sys.path.insert(0, str(ROOT))
from conflate import street_key  # noqa: E402  (same address normalisation as the conflation)

state_lock = threading.Lock()


# --- data ------------------------------------------------------------------------

def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_state() -> dict:
    return read_json(BDIR / "state.json", {})


def save_state(s: dict):
    tmp = BDIR / "state.json.tmp"
    tmp.write_text(json.dumps(s, indent=1, sort_keys=True))
    tmp.replace(BDIR / "state.json")


def load_config() -> dict:
    return {"wiki": "", "conflate_args": "", "osm_user": "", **read_json(CONFIG, {})}


_blocks_cache: dict = {}


def blocks_fc() -> dict:
    p = BDIR / "blocks.geojson"
    mtime = p.stat().st_mtime
    if _blocks_cache.get("mtime") != mtime:
        _blocks_cache.update(mtime=mtime, fc=json.loads(p.read_text()))
    return _blocks_cache["fc"]


def block_dir(i: int) -> Path:
    return BDIR / "out" / f"{i:04d}"


def blocks_with_state() -> dict:
    state = load_state()
    feats = []
    for f in blocks_fc()["features"]:
        p = dict(f["properties"])
        s = state.get(str(p["id"]), {})
        p.update({k: v for k, v in s.items() if k != "error"})
        p.setdefault("status", "pending")
        if (block_dir(p["id"]) / "decisions.json").exists():
            c = decision_counts(p["id"])
            p["rejected"] = c.get("rejected", 0)
            p["review_open"] = c.get("open", 0)
        elif p.get("review") is not None:
            p["review_open"] = p["review"]
        feats.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    return {"type": "FeatureCollection", "features": feats}


def summary_stats(i: int) -> dict:
    p = block_dir(i) / "changeset_summary.txt"
    out, streets = {}, []
    if not p.exists():
        return {"stats": out, "unmatched_streets": streets}
    in_streets = False
    for line in p.read_text().splitlines():
        parts = line.split()
        if line.startswith("Street names not matched"):
            in_streets = True
            continue
        if line.startswith("Suggested changeset"):
            in_streets = False
        if in_streets and line.strip():
            m = re.match(r"\s*(\d+)\s+(.+?)\s{2,}(?:OSM nearby: (.+))?$", line + "  ")
            if m:
                streets.append({"count": int(m.group(1)), "county": m.group(2).strip(),
                                "suggestion": (m.group(3) or "").strip()})
        elif len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return {"stats": out, "unmatched_streets": streets}


def changeset_tags(i: int) -> dict:
    p = block_dir(i) / "changeset_tags.txt"
    tags = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                tags[k] = v
    return tags


def tags_of(el) -> dict:
    return {t.get("k"): t.get("v") for t in el.findall("tag")}


def new_node_cid(tags: dict) -> str:
    """Stable id for a new node across regenerations (its negative id is not)."""
    return "new:" + "|".join(tags.get(k, "") for k in ("addr:housenumber", "addr:street", "addr:unit"))


def load_decisions(i: int) -> dict:
    """{change id: 'approved' | 'rejected'}; anything absent is undecided (included)."""
    return read_json(block_dir(i) / "decisions.json", {})


def save_decisions(i: int, dec: dict):
    p = block_dir(i) / "decisions.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dec, indent=1, sort_keys=True))
    tmp.replace(p)


def change_ids(i: int) -> set[str]:
    osm = block_dir(i) / "changeset.osm"
    if not osm.exists():
        return set()
    root = ET.parse(osm).getroot()
    ids = {f"{el.tag}/{el.get('id')}" for el in root if el.get("action") == "modify"}
    ids |= {new_node_cid(tags_of(n)) for n in root.findall("node") if n.get("id").startswith("-")}
    return ids


def decision_counts(i: int) -> dict:
    dec = load_decisions(i)
    live = change_ids(i)
    c = Counter("merged" if v.startswith("merge:") else v for k, v in dec.items() if k in live)
    rev = {review_cid(f["properties"]) for f in read_json(block_dir(i) / "changeset_review.geojson", {"features": []})["features"]}
    r = Counter(v for k, v in dec.items() if k in rev)
    return {"approved": c["approved"] + c["moved"], "rejected": c["rejected"], "merged": c["merged"],
            "edited": sum(1 for k in load_edits(i) if k in live),
            "undecided": len(live) - sum(c.values()),
            "resolved": r["resolved"], "ignored": r["ignored"], "open": len(rev) - sum(r.values())}


def review_cid(p: dict) -> str:
    return f"review:{p.get('county_id') or p.get('osm')}:{p.get('reason')}"


def load_edits(i: int) -> dict:
    """{change id: full tag dict the user wants instead of the generated tags}"""
    return read_json(block_dir(i) / "edits.json", {})


def save_edits(i: int, edits: dict):
    p = block_dir(i) / "edits.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(edits, indent=1, sort_keys=True))
    tmp.replace(p)


def load_moves(i: int) -> dict:
    """{new-node change id: [lon, lat]} for points the user dragged."""
    return read_json(block_dir(i) / "moves.json", {})


def save_moves(i: int, moves: dict):
    p = block_dir(i) / "moves.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(moves, indent=1, sort_keys=True))
    tmp.replace(p)


def metres(a, b) -> float:
    import math
    dx = (a[0] - b[0]) * 111320 * math.cos(math.radians(a[1]))
    return math.hypot(dx, (a[1] - b[1]) * 110540)


MAX_MOVE_M = 300


_base_cache: dict = {}


def osm_base(i: int) -> dict:
    """{ref: tags} of everything downloaded for the block (block + 400 m), as it is in OSM."""
    files = sorted((block_dir(i) / ".cache").glob("osm_v2_*.json")) + sorted((block_dir(i) / ".cache").glob("pois_v1_*.json"))
    key = tuple((str(f), f.stat().st_mtime) for f in files)
    if _base_cache.get(i, (None,))[0] == key:
        return _base_cache[i][1]
    base = {}
    for f in files:
        for el in json.loads(f.read_text()).get("elements", []):
            if "tags" in el:
                base[f"{el['type']}/{el['id']}"] = el["tags"]
    _base_cache[i] = (key, base)
    return base


def addr_keys(tags: dict) -> set:
    hns = [h.strip() for h in re.split(r"[;,]", tags.get("addr:housenumber", "")) if h.strip()]
    return {(h, street_key(tags.get("addr:street", "")), tags.get("addr:unit")) for h in hns}


def edit_clashes(i: int, cid: str, tags: dict) -> list[str]:
    """Objects that would end up with one of the same addresses as the edited object."""
    keys = addr_keys(tags)
    if not keys:
        return []
    state = dict(osm_base(i))
    root = ET.fromstring(filtered_changeset(i, skip_edit=cid))
    me = cid
    for el in root:
        if el.tag not in ("node", "way", "relation"):
            continue
        t = tags_of(el)
        if el.get("id").startswith("-"):
            ref = f"new node at {float(el.get('lat')):.5f},{float(el.get('lon')):.5f}"
            if new_node_cid(t) == cid:
                me = ref
            state[ref] = t
        elif el.get("action") == "modify":
            state[f"{el.tag}/{el.get('id')}"] = t
    merged_into = load_decisions(i).get(cid, "")
    if merged_into.startswith("merge:"):
        me = merged_into[len("merge:"):]
    return sorted(ref for ref, t in state.items() if ref not in (me, cid) and keys & addr_keys(t))[:5]


# --- fixes for review items ----------------------------------------------------------
# A review item is resolved in the UI by a "fix": new tags for existing OSM objects
# and/or new address points. Fixes go into the exported changeset like any change.
#   fixes.json = {review cid: {"objects": {ref: tags}, "points": [{"lon","lat","tags"}],
#                              "el": {ref: element snapshot}}}

_el_cache: dict = {}


def osm_elements(i: int) -> tuple[dict, dict]:
    """({ref: element with metadata}, {node id: (lon, lat)}) from the block's download cache."""
    files = sorted((block_dir(i) / ".cache").glob("osm_v2_*.json")) + sorted((block_dir(i) / ".cache").glob("pois_v1_*.json"))
    key = tuple((str(f), f.stat().st_mtime) for f in files)
    if _el_cache.get(i, (None,))[0] == key:
        return _el_cache[i][1]
    els, coords = {}, {}
    for f in files:
        for el in json.loads(f.read_text()).get("elements", []):
            ref = f"{el['type']}/{el['id']}"
            if el["type"] == "node" and "lat" in el:
                coords[el["id"]] = (el["lon"], el["lat"])
            # keep the richest copy: with metadata and tags if any download had them
            old = els.get(ref)
            if old is None or ("version" in el and "version" not in old) or ("tags" in el and "tags" not in old):
                els[ref] = el
    _el_cache[i] = (key, (els, coords))
    return els, coords


def load_fixes(i: int) -> dict:
    return read_json(block_dir(i) / "fixes.json", {})


def save_fixes(i: int, fixes: dict):
    p = block_dir(i) / "fixes.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(fixes, indent=1, sort_keys=True))
    tmp.replace(p)


def element_snapshot(i: int, ref: str) -> dict:
    """Everything needed to write ref as action=modify: the element and, for ways, its nodes.
    From the download cache, or the OSM API when the cache doesn't have its metadata."""
    els, coords = osm_elements(i)
    typ, oid = ref.split("/")
    el = els.get(ref)
    if el and "version" in el and typ != "relation" and (typ != "way" or all(n in coords for n in el["nodes"])):
        snap = {k: el[k] for k in ("type", "id", "version", "timestamp", "changeset", "user", "uid", "lat", "lon",
                                   "nodes", "members", "tags") if k in el}
        if typ == "way":
            snap["node_coords"] = {str(n): coords[n] for n in el["nodes"]}
        return snap
    # relations come with their member ways and nodes so JOSM has the multipolygon complete
    root = ET.fromstring(osm_get(f"/{typ}/{oid}/full" if typ in ("way", "relation") else f"/{typ}/{oid}"))
    snap, node_coords = None, {}
    meta_keys = ("version", "timestamp", "changeset", "user", "uid")
    member_ways, member_nodes = {}, {}
    for e in root:
        if e.tag == "node":
            node_coords[e.get("id")] = (float(e.get("lon")), float(e.get("lat")))
            if typ == "relation":
                member_nodes[e.get("id")] = {**{k: e.get(k) for k in meta_keys if e.get(k)}, "lat": e.get("lat"), "lon": e.get("lon"), "tags": tags_of(e)}
        if e.tag == "way" and typ == "relation":
            member_ways[e.get("id")] = {**{k: e.get(k) for k in meta_keys if e.get(k)},
                                        "nodes": [int(nd.get("ref")) for nd in e.findall("nd")], "tags": tags_of(e)}
        if f"{e.tag}/{e.get('id')}" == ref:
            snap = {k: e.get(k) for k in ("version", "timestamp", "changeset", "user", "uid", "lat", "lon") if e.get(k)}
            snap.update(type=typ, id=int(oid), tags=tags_of(e))
            if typ == "way":
                snap["nodes"] = [int(nd.get("ref")) for nd in e.findall("nd")]
            if typ == "relation":
                snap["members"] = [{"type": m.get("type"), "ref": int(m.get("ref")), "role": m.get("role", "")} for m in e.findall("member")]
    if snap is None:
        raise ValueError(f"{ref} not found in OSM")
    if typ == "relation":
        snap["member_ways"], snap["member_nodes"] = member_ways, member_nodes
    if typ == "way":
        snap["node_coords"] = {str(n): node_coords[str(n)] for n in snap["nodes"]}
    return snap


def snapshot_xml(snap: dict, tags: dict) -> tuple[ET.Element, list[ET.Element]]:
    """(element with action=modify and the given tags, [plain node elements a way needs])."""
    attrs = {k: str(snap[k]) for k in ("id", "version", "timestamp", "changeset", "user", "uid", "lat", "lon") if k in snap}
    el = ET.Element(snap["type"], attrs)
    el.set("action", "modify")
    extra = []
    if snap["type"] == "way":
        for n in snap["nodes"]:
            ET.SubElement(el, "nd", ref=str(n))
            lon, lat = snap["node_coords"][str(n)]
            extra.append(ET.Element("node", {"id": str(n), "version": "0", "lat": f"{lat:.7f}", "lon": f"{lon:.7f}"}))
    if snap["type"] == "relation":
        for m in snap.get("members", []):
            ET.SubElement(el, "member", type=m["type"], ref=str(m["ref"]), role=m.get("role", ""))
        for nid, n in snap.get("member_nodes", {}).items():
            extra.append(node_el({"id": nid, **{k: n[k] for k in ("version", "timestamp", "changeset", "user", "uid", "lat", "lon") if k in n}}, n.get("tags", {})))
        for wid, w in snap.get("member_ways", {}).items():
            we = ET.Element("way", {"id": wid, **{k: w[k] for k in ("version", "timestamp", "changeset", "user", "uid") if k in w}})
            for n in w["nodes"]:
                ET.SubElement(we, "nd", ref=str(n))
            for k, v in w.get("tags", {}).items():
                ET.SubElement(we, "tag", k=k, v=v)
            extra.append(we)
    set_tags(el, tags)
    return el, extra


def apply_fixes(i: int, root: ET.Element, skip: str | None = None):
    """Write every fix (except skip's) into a changeset tree."""
    fixes = load_fixes(i)
    dec = load_decisions(i)
    by_ref = {f"{el.tag}/{el.get('id')}": el for el in root}
    next_id = min([int(n.get("id")) for n in root.findall("node")] + [0]) - 1
    first_way = next((k for k, el in enumerate(root) if el.tag in ("way", "relation")), len(root))
    new_nodes, new_other = [], []
    for cid, fx in sorted(fixes.items()):
        if cid == skip or dec.get(cid) in ("ignored", "rejected", "approved") or (not cid.startswith("review:") and dec.get(cid) != "moved"):
            continue
        for ref, tags in fx.get("objects", {}).items():
            if ref in by_ref:
                set_tags(by_ref[ref], tags)
                by_ref[ref].set("action", "modify")
                continue
            snap = fx.get("el", {}).get(ref)
            if not snap:
                continue
            el, extra = snapshot_xml(snap, tags)
            by_ref[ref] = el
            for x in extra:   # plain nodes and member ways it needs, unless already there
                xref = f"{x.tag}/{x.get('id')}"
                if xref not in by_ref:
                    by_ref[xref] = x
                    (new_nodes if x.tag == "node" else new_other).append(x)
            (new_nodes if el.tag == "node" else new_other).append(el)
        for pt in fx.get("points", []):
            n = node_el({"id": next_id, "lat": f"{pt['lat']:.7f}", "lon": f"{pt['lon']:.7f}"}, pt["tags"])
            next_id -= 1
            new_nodes.append(n)
    # real node versions: plain way nodes were written with version 0 as a placeholder
    els, _ = osm_elements(i)
    for n in new_nodes:
        if n.get("version") == "0":
            e = els.get(f"node/{n.get('id')}")
            if e:
                for k in ("version", "timestamp", "changeset", "user", "uid"):
                    if k in e:
                        n.set(k, str(e[k]))
            else:
                n.attrib.pop("version")
    for k, n in enumerate(new_nodes):
        root.insert(first_way + k, n)
    # OSM XML order: nodes, ways, relations
    first_rel = next((k for k, el in enumerate(root) if el.tag == "relation"), len(root))
    for k, w in enumerate(x for x in new_other if x.tag == "way"):
        root.insert(first_rel + k, w)
    root.extend(x for x in new_other if x.tag == "relation")


def fix_clashes(i: int, cid: str, fx: dict, replaces_change: bool = False) -> list[str]:
    """Addresses this fix would duplicate: anything else in OSM or the changeset with the same address.
    replaces_change: the fix moves change cid elsewhere, so cid's own change doesn't count."""
    state = dict(osm_base(i))
    root = ET.fromstring(filtered_changeset(i, skip_fix=cid, also_reject=(cid,) if replaces_change else ()))
    if replaces_change and REF_RE.fullmatch(cid):
        state.pop(cid, None)   # the original object goes back to its OSM state, which had no such address
    for el in root:
        if el.tag in ("node", "way", "relation"):
            t = tags_of(el)
            if el.get("id").startswith("-"):
                state[f"new node at {float(el.get('lat')):.5f},{float(el.get('lon')):.5f}"] = t
            elif el.get("action") == "modify":
                state[f"{el.tag}/{el.get('id')}"] = t
    mine = dict(fx.get("objects", {}))
    for ref in mine:
        state.pop(ref, None)
    out = []
    for k, pt in enumerate(fx.get("points", [])):
        mine[f"new point {k + 1}"] = pt["tags"]
    seen = {}
    for ref, tags in mine.items():
        for key in addr_keys(tags):
            if key in seen:
                out.append(f"{ref} and {seen[key]} (both in this fix)")
            seen[key] = ref
            out += [r for r, t in state.items() if key in addr_keys(t)]
    return sorted(set(out))[:5]


def load_pois(i: int) -> dict:
    return read_json(block_dir(i) / "changeset_pois.json", {"merged": [], "candidates": {}})


def node_el(attrs: dict, tags: dict, action: str | None = None) -> ET.Element:
    n = ET.Element("node", {k: str(v) for k, v in attrs.items()})
    if action:
        n.set("action", action)
    for k, v in tags.items():
        ET.SubElement(n, "tag", k=k, v=str(v))
    return n


def set_tags(el: ET.Element, tags: dict):
    for t in list(el.findall("tag")):
        el.remove(t)
    for k, v in tags.items():
        ET.SubElement(el, "tag", k=k, v=str(v))


def filtered_changeset(i: int, skip_edit: str | None = None, skip_fix: str | None = None, also_reject: tuple = ()) -> bytes:
    """changeset.osm with the user's decisions applied: rejected changes removed
    (rejected corrections are reverted), manual POI merges and tag edits applied."""
    osm = block_dir(i) / "changeset.osm"
    dec = load_decisions(i)
    edits = {k: v for k, v in load_edits(i).items() if k != skip_edit}
    moves = load_moves(i)
    # "moved": the change was put on another object by a fix; the original is dropped like a rejection
    rejected = {k for k, v in dec.items() if v in ("rejected", "moved")} | set(also_reject)
    merges = {k: v[len("merge:"):] for k, v in dec.items() if v.startswith("merge:")}
    has_fixes = any(k != skip_fix for k in load_fixes(i))
    if not rejected and not merges and not edits and not moves and not has_fixes and not also_reject:
        return osm.read_bytes()

    def edited_addr(cid, default):
        e = edits.get(cid)
        return {k: v for k, v in e.items() if k.startswith("addr:")} if e else default
    pois = load_pois(i)
    before = {c["osm"]: c["before"] for c in read_json(block_dir(i) / "changeset_corrections.json", [])}
    auto_merged = {m["osm"]: m for m in pois["merged"]}
    root = ET.parse(osm).getroot()
    next_id = min([int(n.get("id")) for n in root.findall("node")] + [0]) - 1
    first_way = next((k for k, el in enumerate(root) if el.tag == "way"), len(root))
    added = []
    for el in list(root):
        ref = f"{el.tag}/{el.get('id')}"
        if el.tag == "node" and el.get("id").startswith("-"):
            cid = new_node_cid(tags_of(el))
            if cid in moves:
                el.set("lon", f"{moves[cid][0]:.7f}")
                el.set("lat", f"{moves[cid][1]:.7f}")
            if cid in rejected:
                root.remove(el)
            elif cid in merges:
                # address goes onto the chosen POI instead of a new node
                cand = next((c for c in pois["candidates"].get(cid, []) if c["osm"] == merges[cid]), None)
                if cand:
                    root.remove(el)
                    e = cand["el"]
                    attrs = {k: e[k] for k in ("id", "version", "timestamp", "changeset", "user", "uid", "lat", "lon") if k in e}
                    poi_tags = {k: v for k, v in e.get("tags", {}).items() if not k.startswith("addr:")}
                    added.append(node_el(attrs, {**poi_tags, **edited_addr(cid, tags_of(el))}, "modify"))
            elif cid in edits:
                set_tags(el, edits[cid])
        elif el.tag in ("way", "relation") and ref in rejected:
            root.remove(el)
        elif el.tag == "node" and ref in rejected and ref in auto_merged:
            # POI merge rejected: leave the POI alone, the address gets its own node
            m = auto_merged[ref]
            added.append(node_el({"id": next_id, "lat": f"{m['lat']:.7f}", "lon": f"{m['lon']:.7f}"},
                                 edited_addr(ref, m["addr"])))
            next_id -= 1
            root.remove(el)
        elif el.get("action") == "modify" and ref in edits and ref not in rejected:
            set_tags(el, edits[ref])
    for k, n in enumerate(added):
        root.insert(first_way + k, n)
    used = {nd.get("ref") for w in root.findall("way") for nd in w.findall("nd")}
    for n in list(root.findall("node")):
        nid, ref = n.get("id"), f"node/{n.get('id')}"
        if nid.startswith("-"):
            continue
        if ref in rejected and nid in used:
            # node is still needed by a way: keep it, but with its original address
            for t in list(n.findall("tag")):
                if t.get("k").startswith("addr:"):
                    n.remove(t)
            for k, v in before.get(ref, {}).items():
                ET.SubElement(n, "tag", k=k, v=v)
            n.attrib.pop("action", None)
        elif (ref in rejected or n.get("action") != "modify") and nid not in used:
            root.remove(n)
    apply_fixes(i, root, skip_fix)
    ET.indent(root)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"


def relation_geom(el: dict | None) -> dict | None:
    """GeoJSON (Multi)Polygon for a multipolygon from its members' geometry (downloaded with
    'out geom'). Outer and inner rings may be split over several ways; they are joined."""
    if not el:
        return None
    from shapely.geometry import LineString, MultiPolygon, mapping
    from shapely.ops import polygonize, unary_union
    lines = {"outer": [], "inner": []}
    for m in el.get("members", []):
        g = [(p["lon"], p["lat"]) for p in m.get("geometry") or [] if p]
        if len(g) >= 2:
            lines["inner" if m.get("role") == "inner" else "outer"].append(LineString(g))
    if not lines["outer"]:
        return None
    outer = unary_union(list(polygonize(unary_union(lines["outer"]))))
    if lines["inner"]:
        outer = outer.difference(unary_union(list(polygonize(unary_union(lines["inner"])))))
    if outer.is_empty:
        return None
    if outer.geom_type not in ("Polygon", "MultiPolygon"):
        outer = MultiPolygon([g for g in getattr(outer, "geoms", []) if g.geom_type == "Polygon"])
    return mapping(outer)


REF_RE = re.compile(r"(node|way|relation)/\d+")


def object_geom(ref: str, els: dict, coords: dict) -> dict | None:
    el = els.get(ref)
    if el and el["type"] == "way" and all(n in coords for n in el.get("nodes", [])):
        return {"type": "Polygon", "coordinates": [[list(coords[n]) for n in el["nodes"]]]}
    if el and el["type"] == "relation":
        return relation_geom(el)
    if el and el["type"] == "node":
        return {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
    return None


def buildings_at(i: int, lon: float, lat: float) -> list[dict]:
    """Building outlines (ways and multipolygons) containing a point, smallest first."""
    from shapely.geometry import Point, shape
    els, coords = osm_elements(i)
    base = osm_base(i)
    pt = Point(lon, lat)
    out = []
    for ref, el in els.items():
        if el["type"] == "node" or "building" not in el.get("tags", {}):
            continue
        g = object_geom(ref, els, coords)
        if not g or g["type"] not in ("Polygon", "MultiPolygon"):
            continue
        try:
            shp = shape(g)
            if not shp.is_valid:
                shp = shp.buffer(0)
        except Exception:
            continue
        if shp.contains(pt):
            out.append((shp.area, {"ref": ref, "tags": base.get(ref, el.get("tags", {})), "geom": g}))
    return [b for _, b in sorted(out, key=lambda x: x[0])]


def related_objects(p: dict, els: dict, coords: dict, base: dict) -> dict:
    """{ref: {tags, geom}} for the OSM objects a review item is about, so the UI can edit and draw them."""
    refs = []
    for k in ("osm", "osm_number_building", "empty_neighbors", "duplicate_of"):
        refs += [r for r in (m.group(0) for m in REF_RE.finditer(str(p.get(k) or "")))]
    out = {}
    for ref in dict.fromkeys(refs):
        el = els.get(ref)
        geom = None
        if el and el["type"] == "way" and all(n in coords for n in el.get("nodes", [])):
            geom = {"type": "Polygon", "coordinates": [[list(coords[n]) for n in el["nodes"]]]}
        elif el and el["type"] == "relation":
            geom = relation_geom(el)
        elif el and el["type"] == "node":
            geom = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
        out[ref] = {"tags": base.get(ref, el.get("tags", {}) if el else {}), "geom": geom}
    return out


def change_layers(i: int) -> dict:
    """GeoJSON of what the changeset does: tagged buildings, new nodes, review points."""
    feats = []
    fixes = load_fixes(i)
    els, coords = osm_elements(i)
    dec = load_decisions(i)
    edits = load_edits(i)
    moves = load_moves(i)
    base = osm_base(i)
    full_tags = {}
    osm = block_dir(i) / "changeset.osm"
    if osm.exists():
        root = ET.parse(osm).getroot()
        full_tags = {f"{el.tag}/{el.get('id')}": tags_of(el) for el in root if el.get("action") == "modify"}
        nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.findall("node")}
        for n in root.findall("node"):
            if n.get("id").startswith("-"):
                tags = tags_of(n)
                cid = new_node_cid(tags)
                orig = list(nodes[n.get("id")])
                feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": moves.get(cid, orig)},
                              "properties": {"kind": "new_node", "osm": None, "tags": tags, "orig": orig,
                                             "moved": cid in moves, "cid": cid, "decision": dec.get(cid)}})
        corrected = {c["osm"] for c in read_json(block_dir(i) / "changeset_corrections.json", [])}
        pois = load_pois(i)
        auto_merged = {m["osm"]: m for m in pois["merged"]}
        for f in feats:
            cands = pois["candidates"].get(f["properties"]["cid"])
            if cands:
                f["properties"]["poi_candidates"] = [{k: c[k] for k in ("osm", "name", "kind", "dist")} for c in cands]
        for n in root.findall("node"):
            ref = f"node/{n.get('id')}"
            if n.get("action") == "modify" and ref in auto_merged:
                tags = tags_of(n)
                feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(nodes[n.get("id")])},
                              "properties": {"kind": "poi_address", "osm": ref, "tags": tags,
                                             "name": auto_merged[ref]["name"], "poi_kind": auto_merged[ref]["kind"],
                                             "cid": ref, "decision": dec.get(ref)}})
        for w in root.findall("way"):
            if w.get("action") != "modify" or f"way/{w.get('id')}" in corrected:
                continue
            ring = [list(nodes[nd.get("ref")]) for nd in w.findall("nd") if nd.get("ref") in nodes]
            cid = f"way/{w.get('id')}"
            feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                          "properties": {"kind": "tagged_building", "osm": cid, "tags": tags_of(w),
                                         "cid": cid, "decision": dec.get(cid)}})
        els, _ = osm_elements(i)
        for r in root.findall("relation"):
            cid = f"relation/{r.get('id')}"
            geom = relation_geom(els.get(cid))
            if r.get("action") != "modify" or cid in corrected or not geom:
                continue
            feats.append({"type": "Feature", "geometry": geom,
                          "properties": {"kind": "tagged_building", "osm": cid, "tags": tags_of(r),
                                         "cid": cid, "decision": dec.get(cid)}})
    for c in read_json(block_dir(i) / "changeset_corrections.json", []):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
                      "properties": {"kind": "corrected", **c, "cid": c["osm"], "decision": dec.get(c["osm"])}})
    review = read_json(block_dir(i) / "changeset_review.geojson", {"features": []})
    for idx, f in enumerate(review["features"]):
        cid = review_cid(f["properties"])
        f["properties"] = {"kind": "review", "idx": idx, **f["properties"], "cid": cid, "decision": dec.get(cid)}
        f["properties"]["objects"] = related_objects(f["properties"], els, coords, base)
        f["properties"]["fix"] = {k: v for k, v in fixes.get(cid, {}).items() if k != "el"} or None
        feats.append(f)
    for f in feats:
        p = f["properties"]
        if p["kind"] == "review":
            continue
        if p["kind"] == "corrected":
            p["tags"] = full_tags.get(p["osm"], p.get("after", {}))
        p["base"] = base.get(p["osm"], {}) if p.get("osm") else {}
        p["edit"] = edits.get(p["cid"])
        fx = fixes.get(p["cid"])
        p["fix"] = {k: v for k, v in fx.items() if k != "el"} if fx else None
        if fx:
            p["fix_objects"] = {r: {"tags": base.get(r, {}), "geom": object_geom(r, els, coords)} for r in fx.get("objects", {})}
    return {"type": "FeatureCollection", "features": feats}


# --- upload detection -------------------------------------------------------------
# Changesets are uploaded from JOSM, outside this app. To notice them, look at
# the configured OSM user's recent changesets and compare each one's contents
# with the changeset of every ready block it overlaps.

OSM_API = "https://api.openstreetmap.org/api/0.6"
UA = "meck-addr-import (local review UI)"
MATCH_SHARE = 0.6        # share of a block's changes that must be in the changeset
sync_lock = threading.Lock()
_seen_changesets: dict[int, set] = {}   # changeset id -> refs/keys it contains


def osm_get(path: str) -> bytes:
    import urllib.request
    req = urllib.request.Request(OSM_API + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def block_fingerprint(i: int) -> set:
    """What block i's (filtered) changeset does: modified refs and new addresses."""
    root = ET.fromstring(filtered_changeset(i))
    out = set()
    for el in root:
        if el.tag not in ("node", "way", "relation"):
            continue
        if el.get("id").startswith("-"):
            out |= {("addr",) + k for k in addr_keys(tags_of(el))}
        elif el.get("action") == "modify":
            out.add(f"{el.tag}/{el.get('id')}")
    return out


def changeset_fingerprint(cs_id: int) -> set:
    if cs_id not in _seen_changesets:
        root = ET.fromstring(osm_get(f"/changeset/{cs_id}/download"))
        out = set()
        for action in root:
            for el in action:
                if action.tag == "create" and el.tag == "node":
                    out |= {("addr",) + k for k in addr_keys(tags_of(el))}
                elif action.tag == "modify":
                    out.add(f"{el.tag}/{el.get('id')}")
        _seen_changesets[cs_id] = out
    return _seen_changesets[cs_id]


def block_bbox(i: int):
    f = next(f for f in blocks_fc()["features"] if f["properties"]["id"] == i)
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for x in c:
                walk(x)
    walk(f["geometry"]["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def sync_uploads(only: set[int] | None = None) -> list[dict]:
    """Link uploaded changesets to blocks: ready blocks whose changes show up in one of the
    user's changesets are marked uploaded, and blocks marked uploaded by hand get their
    changeset id. A changeset may hold several blocks. Returns what was linked."""
    user = load_config().get("osm_user", "").strip()
    if not user:
        return []
    with sync_lock:
        state = load_state()
        cands = {int(k): v for k, v in state.items() if v.get("generated") and (only is None or int(k) in only) and
                 (v.get("status") == "ready" or (v.get("status") == "uploaded" and not v.get("changeset_id")))}
        if not cands:
            return []
        since = min(v["generated"] for v in cands.values())
        from urllib.parse import quote
        data = json.loads(osm_get(f"/changesets.json?display_name={quote(user)}&time={quote(since)}&limit=100"))
        marked = []
        for i, st in cands.items():
            ours = block_fingerprint(i)
            if not ours:
                continue
            w, so, e, n = block_bbox(i)
            best = None
            for cs in data.get("changesets", []):
                if "min_lon" not in cs or cs["created_at"] < st["generated"]:
                    continue
                if cs["max_lon"] < w or cs["min_lon"] > e or cs["max_lat"] < so or cs["min_lat"] > n:
                    continue
                share = len(ours & changeset_fingerprint(cs["id"])) / len(ours)
                if share >= MATCH_SHARE and (best is None or share > best[0]):
                    best = (share, cs)
            if best:
                share, cs = best
                with state_lock:
                    s = load_state()
                    entry = s.setdefault(str(i), {})
                    was_ready = entry.get("status") == "ready"
                    entry.update(status="uploaded", changeset_id=str(cs["id"]), upload_match=round(share, 3),
                                 uploaded_detected=was_ready or entry.get("uploaded_detected", False))
                    if was_ready or not entry.get("uploaded"):
                        entry["uploaded"] = cs.get("closed_at") or cs["created_at"]
                    save_state(s)
                marked.append({"block": i, "changeset": cs["id"], "share": round(share, 3), "was_ready": was_ready})
        return marked


def sync_loop():
    while True:
        try:
            for m in sync_uploads():
                print(f"block {m['block']}: uploaded in changeset {m['changeset']} ({m['share']:.0%} of its changes)", file=sys.stderr)
        except Exception as e:  # network trouble shouldn't kill the server
            print(f"upload check failed: {e}", file=sys.stderr)
        time.sleep(600)


# --- job queue -------------------------------------------------------------------

class Jobs:
    def __init__(self):
        self.queue: deque = deque()
        self.current = None
        self.log: deque = deque(maxlen=400)
        self.done: deque = deque(maxlen=50)
        self.cv = threading.Condition()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, ids: list[int], refresh: bool):
        with self.cv:
            queued = {j["id"] for j in self.queue} | ({self.current["id"]} if self.current else set())
            added = []
            for i in ids:
                if i not in queued:
                    self.queue.append({"id": i, "refresh": refresh, "queued": time.time()})
                    added.append(i)
            self.cv.notify()
        return added

    def cancel_queued(self):
        with self.cv:
            n = len(self.queue)
            self.queue.clear()
        return n

    def snapshot(self):
        with self.cv:
            return {"current": self.current, "queue": [j["id"] for j in self.queue],
                    "log": list(self.log)[-200:], "done": list(self.done)}

    def _worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.popleft()
                self.current = {**job, "started": time.time()}
            cfg = load_config()
            cmd = [str(ROOT / "blocks.py"), "run", "--ids", str(job["id"])]
            if job["refresh"]:
                cmd.append("--refresh")
            if cfg.get("wiki"):
                cmd += ["--wiki", cfg["wiki"]]
            extra = cfg.get("conflate_args", "").split()
            if extra:
                cmd += ["--", *extra]
            self.log.append(f"$ block {job['id']}{' (refresh)' if job['refresh'] else ''}")
            try:
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    if line.strip():
                        self.log.append(line.rstrip())
                rc = proc.wait()
            except Exception as e:  # noqa: BLE001
                rc = -1
                self.log.append(f"failed: {e}")
            with self.cv:
                self.done.append({"id": job["id"], "rc": rc, "finished": time.time()})
                self.current = None


JOBS: Jobs | None = None


# --- HTTP ------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "meck-addr/0.1"

    def log_message(self, fmt, *args):
        if "/api/jobs" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, p: Path, download_name: str | None = None):
        if not p.exists():
            return self.send_json({"error": "not found"}, 404)
        self.send_bytes(p.read_bytes(), p.name, download_name)

    def send_bytes(self, body: bytes, name: str, download_name: str | None = None):
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".osm"):
            ctype = "application/xml"
        if name.endswith(".geojson"):
            ctype = "application/geo+json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # JOSM remote control fetches files from here
        self.send_header("Access-Control-Allow-Origin", "*")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/static/"):
            name = "index.html" if path == "/" else path[len("/static/"):]
            p = (STATIC / name).resolve()
            if STATIC not in p.parents and p != STATIC / "index.html":
                return self.send_json({"error": "bad path"}, 400)
            return self.send_file(p)
        if path == "/api/blocks":
            return self.send_json(blocks_with_state())
        if path == "/api/config":
            return self.send_json(load_config())
        if path == "/api/jobs":
            return self.send_json(JOBS.snapshot())
        if path == "/api/progress":
            state = load_state()
            ids = [f["properties"]["id"] for f in blocks_fc()["features"]]
            c = Counter(state.get(str(i), {}).get("status", "pending") for i in ids)
            changes = Counter()
            for i in ids:
                s = state.get(str(i), {})
                changes[s.get("status", "pending")] += s.get("changes", 0)
            return self.send_json({"blocks": dict(c), "changes": dict(changes), "total": len(ids)})
        m = re.fullmatch(r"/api/blocks/(\d+)(/[\w.-]+)?(/[\w.]+)?", path)
        if m:
            i = int(m.group(1))
            sub = (m.group(2) or "")[1:]
            if sub == "":
                feat = next((f for f in blocks_with_state()["features"] if f["properties"]["id"] == i), None)
                if not feat:
                    return self.send_json({"error": "no such block"}, 404)
                d = block_dir(i)
                return self.send_json({
                    "feature": feat, **summary_stats(i), "changeset_tags": changeset_tags(i),
                    "error": load_state().get(str(i), {}).get("error"),
                    "decisions": decision_counts(i),
                    "files": sorted(f.name for f in d.glob("*") if f.name in SERVED_FILES) if d.exists() else [],
                })
            if sub == "layers":
                return self.send_json(change_layers(i))
            if sub == "buildings-at":
                q = parse_qs(urlparse(self.path).query)
                return self.send_json(buildings_at(i, float(q["lon"][0]), float(q["lat"][0])))
            if sub == "file" and m.group(3):
                name = m.group(3)[1:]
                if name not in SERVED_FILES:
                    return self.send_json({"error": "bad file"}, 400)
                dl = f"block{i:04d}_{name}" if "download" in (urlparse(self.path).query or "") else None
                if name == "changeset.osm" and (block_dir(i) / name).exists():
                    return self.send_bytes(filtered_changeset(i), name, dl)
                return self.send_file(block_dir(i) / name, dl)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.body()
        if path == "/api/generate":
            ids = [int(x) for x in data.get("ids", [])]
            if data.get("next"):
                state = load_state()
                pending = [f["properties"]["id"] for f in blocks_fc()["features"]
                           if state.get(str(f["properties"]["id"]), {}).get("status", "pending") == "pending"]
                queued = set(JOBS.snapshot()["queue"])
                ids += [i for i in sorted(pending) if i not in queued][: int(data["next"])]
            return self.send_json({"queued": JOBS.add(ids, bool(data.get("refresh")))})
        if path == "/api/sync-uploads":
            try:
                return self.send_json({"marked": sync_uploads(), "user": load_config().get("osm_user", "")})
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        if path == "/api/jobs/cancel":
            return self.send_json({"cancelled": JOBS.cancel_queued()})
        if path == "/api/config":
            cfg = load_config()
            for k in ("wiki", "conflate_args", "osm_user"):
                if k in data:
                    cfg[k] = str(data[k]).strip()
            CONFIG.write_text(json.dumps(cfg, indent=1))
            return self.send_json(cfg)
        m = re.fullmatch(r"/api/blocks/(\d+)/decisions", path)
        if m:
            i, d = int(m.group(1)), data.get("decision")
            if d not in ("approved", "rejected", "resolved", "ignored", None) and not str(d).startswith("merge:node/"):
                return self.send_json({"error": "bad decision"}, 400)
            if d and d.startswith("merge:"):
                cands = load_pois(i)["candidates"]
                taken = {v for k, v in load_decisions(i).items() if k not in data.get("ids", [])}
                for cid in data.get("ids", []):
                    if d[len("merge:"):] not in {c["osm"] for c in cands.get(cid, [])}:
                        return self.send_json({"error": "not a merge candidate for this address"}, 400)
                if d in taken or len(data.get("ids", [])) > 1:
                    return self.send_json({"error": "that POI already has another address merged into it"}, 409)
            with state_lock:
                dec = load_decisions(i)
                for cid in data.get("ids", []):
                    if d is None:
                        dec.pop(str(cid), None)
                    else:
                        dec[str(cid)] = d
                save_decisions(i, dec)
            return self.send_json({"ok": True, "decisions": decision_counts(i)})
        m = re.fullmatch(r"/api/blocks/(\d+)/move", path)
        if m:
            i, cid = int(m.group(1)), str(data.get("cid", ""))
            osm = block_dir(i) / "changeset.osm"
            orig = None
            if osm.exists():
                for n in ET.parse(osm).getroot().findall("node"):
                    if n.get("id").startswith("-") and new_node_cid(tags_of(n)) == cid:
                        orig = (float(n.get("lon")), float(n.get("lat")))
            if orig is None:
                return self.send_json({"error": "only new points can be moved"}, 400)
            with state_lock:
                moves = load_moves(i)
                if data.get("lon") is None:
                    moves.pop(cid, None)
                else:
                    to = (float(data["lon"]), float(data["lat"]))
                    if metres(orig, to) > MAX_MOVE_M:
                        return self.send_json({"error": f"more than {MAX_MOVE_M} m from the county point"}, 400)
                    moves[cid] = [round(to[0], 7), round(to[1], 7)]
                save_moves(i, moves)
            return self.send_json({"ok": True, "moved": cid in moves})
        m = re.fullmatch(r"/api/blocks/(\d+)/edit", path)
        if m:
            i, cid, tags = int(m.group(1)), str(data.get("cid", "")), data.get("tags")
            if cid not in change_ids(i) and not any(m_["osm"] == cid for m_ in load_pois(i)["merged"]):
                return self.send_json({"error": "unknown change"}, 404)
            with state_lock:
                edits = load_edits(i)
                if tags is None:
                    edits.pop(cid, None)
                else:
                    if not isinstance(tags, dict):
                        return self.send_json({"error": "tags must be an object"}, 400)
                    tags = {str(k).strip(): str(v).strip() for k, v in tags.items() if str(k).strip() and str(v).strip()}
                    clashes = edit_clashes(i, cid, tags)
                    if clashes:
                        return self.send_json({"error": "duplicate", "clashes": clashes}, 409)
                    edits[cid] = tags
                save_edits(i, edits)
            return self.send_json({"ok": True, "edit": edits.get(cid)})
        m = re.fullmatch(r"/api/blocks/(\d+)/fix", path)
        if m:
            i, cid, fx = int(m.group(1)), str(data.get("cid", "")), data.get("fix")
            rev = {review_cid(f["properties"]) for f in read_json(block_dir(i) / "changeset_review.geojson", {"features": []})["features"]}
            # a fix on a change (not a review item) moves it: e.g. the address goes on another building
            is_change = cid not in rev and cid in change_ids(i)
            if cid not in rev and not is_change:
                return self.send_json({"error": "unknown review item or change"}, 404)
            done = "moved" if is_change else "resolved"
            with state_lock:
                fixes = load_fixes(i)
                dec = load_decisions(i)
                if not fx or (not fx.get("objects") and not fx.get("points")):
                    fixes.pop(cid, None)
                    if dec.get(cid) == done:
                        dec.pop(cid)
                else:
                    clean = lambda t: {str(k).strip(): str(v).strip() for k, v in (t or {}).items() if str(k).strip() and str(v).strip()}
                    objects = {r: clean(t) for r, t in (fx.get("objects") or {}).items() if REF_RE.fullmatch(r)}
                    points = [{"lon": round(float(p_["lon"]), 7), "lat": round(float(p_["lat"]), 7), "tags": clean(p_["tags"])}
                              for p_ in fx.get("points") or [] if clean(p_.get("tags"))]
                    new = {"objects": objects, "points": points}
                    clashes = fix_clashes(i, cid, new, replaces_change=is_change)
                    if clashes:
                        return self.send_json({"error": "duplicate", "clashes": clashes}, 409)
                    try:
                        old = fixes.get(cid, {}).get("el", {})
                        new["el"] = {r: old.get(r) or element_snapshot(i, r) for r in objects}
                    except Exception as e:
                        return self.send_json({"error": f"couldn't load the OSM object: {e}"}, 502)
                    fixes[cid] = new
                    dec[cid] = done
                save_fixes(i, fixes)
                save_decisions(i, dec)
            f = fixes.get(cid)
            return self.send_json({"ok": True, "fix": {k: v for k, v in f.items() if k != "el"} if f else None,
                                   "decision": dec.get(cid)})
        m = re.fullmatch(r"/api/blocks/(\d+)/status", path)
        if m:
            st = data.get("status")
            if st not in STATUSES:
                return self.send_json({"error": "bad status"}, 400)
            with state_lock:
                s = load_state()
                entry = s.setdefault(m.group(1), {})
                entry["status"] = st
                for k in ("changeset_id", "upload_match", "uploaded_detected", "uploaded"):
                    entry.pop(k, None)
                if st == "uploaded":
                    entry["uploaded"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    if data.get("changeset"):
                        entry["changeset_id"] = str(data["changeset"])
                save_state(s)
            linked = None
            if st == "uploaded" and not data.get("changeset"):
                # find the changeset in the user's OSM history
                try:
                    linked = next(iter(sync_uploads({int(m.group(1))})), None)
                except Exception as e:
                    return self.send_json({"ok": True, "linked": None, "lookup_error": str(e)})
            return self.send_json({"ok": True, "linked": linked, "user": load_config().get("osm_user", "")})
        self.send_json({"error": "not found"}, 404)


def main():
    global JOBS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    JOBS = Jobs()
    threading.Thread(target=sync_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mecklenburg address import UI: http://{args.host}:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
