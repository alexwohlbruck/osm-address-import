#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["shapely>=2.0"]
# ///
"""
Conflate Mecklenburg County Master Address points onto existing OSM buildings.

Fetches county addresses and current OSM data for a bounding box, matches each
address to the building it falls inside, and writes:

  <out>.osm             JOSM file: existing buildings with addr:* added (action=modify)
                        plus new address nodes for multi-address buildings
  <out>_review.geojson  everything the tool refused to decide, with a reason
  <out>_summary.txt     counts + unmatched street names

Nothing is uploaded. Open the .osm in JOSM, review, then upload yourself.

Example:
  ./conflate.py --bbox -80.8600,35.2050,-80.8400,35.2200 --out out/dilworth
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

from shapely import STRtree
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import polygonize, unary_union

COUNTY_URL = "https://gis.charlottenc.gov/arcgis/rest/services/CountyData/MasterAddress/MapServer/0/query"
DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "meck-addr-conflate/0.1 (OSM import tooling)"

# --- street name handling ----------------------------------------------------

# County 2-letter type codes -> full word
COUNTY_TYPES = {
    "AL": "Alley", "AV": "Avenue", "BV": "Boulevard", "BY": "Bypass", "CR": "Circle",
    "CS": "Crossing", "CT": "Court", "CV": "Cove", "DR": "Drive", "FR": "Freeway",
    "HY": "Highway", "LN": "Lane", "LP": "Loop", "PL": "Place", "PY": "Parkway",
    "RD": "Road", "RN": "Run", "RW": "Row", "ST": "Street", "TC": "Trace",
    "TL": "Trail", "TR": "Terrace", "WY": "Way", "XG": "Crossing",
}
DIRECTIONS = {"N": "North", "S": "South", "E": "East", "W": "West"}
SUFFIXES = {**DIRECTIONS, "EXT": "Extension"}

# Everything -> one canonical token, used only to compare names (never output).
# Collisions (Saint/Street -> ST) are harmless because we only compare streets
# within a few hundred metres of each other.
_CANON = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "ALLEY": "AL", "ALY": "AL", "AVENUE": "AV", "AVE": "AV", "BOULEVARD": "BV",
    "BLVD": "BV", "BYPASS": "BY", "BYP": "BY", "CIRCLE": "CR", "CIR": "CR",
    "CROSSING": "XG", "XING": "XG", "CS": "XG", "COURT": "CT", "COVE": "CV",
    "DRIVE": "DR", "FREEWAY": "FR", "FWY": "FR", "HIGHWAY": "HY", "HWY": "HY",
    "LANE": "LN", "LOOP": "LP", "PLACE": "PL", "PARKWAY": "PY", "PKWY": "PY",
    "ROAD": "RD", "RUN": "RN", "ROW": "RW", "STREET": "ST", "SAINT": "ST",
    "TRACE": "TC", "TRCE": "TC", "TRAIL": "TL", "TRL": "TL", "TERRACE": "TR",
    "TER": "TR", "WAY": "WY", "EXTENSION": "EXT", "MOUNT": "MT",
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
    "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH",
}


def street_key(name: str) -> str:
    toks = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("'", "")).split()
    return " ".join(_CANON.get(t, t) for t in toks)


def title_word(w: str) -> str:
    if re.fullmatch(r"\d+(ST|ND|RD|TH)", w):
        return w.lower()
    if w.startswith("MC") and len(w) > 2:
        return "Mc" + w[2:].capitalize()
    if w.startswith("O'") and len(w) > 2:
        return "O'" + w[2:].capitalize()
    return "-".join(p.capitalize() for p in w.split("-"))


def county_street(p: dict) -> str:
    """Human-readable fallback street name built from county fields."""
    parts = []
    if p.get("Direction"):
        parts.append(DIRECTIONS.get(p["Direction"], p["Direction"]))
    parts += [title_word(w) for w in (p.get("StreetName") or "").split()]
    if p.get("StreetType"):
        parts.append(COUNTY_TYPES.get(p["StreetType"], p["StreetType"].title()))
    if p.get("Suffix"):
        parts.append(SUFFIXES.get(p["Suffix"], p["Suffix"].title()))
    return " ".join(parts)


def county_raw_street(p: dict) -> str:
    return " ".join(x for x in (p.get("Direction"), p.get("StreetName"), p.get("StreetType"), p.get("Suffix")) if x)


# --- local metric projection (good enough at county scale) ------------------

class Proj:
    def __init__(self, lat0: float):
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return lon * self.kx, lat * self.ky


# --- fetching -----------------------------------------------------------------

def http(url: str, data: dict | None = None, retries: int = 4) -> bytes:
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            print(f"  request failed ({e}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def fetch_county(bbox, cache: Path | None) -> list[dict]:
    if cache and cache.exists():
        return json.loads(cache.read_text())
    w, s, e, n = bbox
    feats, offset = [], 0
    while True:
        params = {
            "where": "StatusCode='A'",
            "geometry": f"{w},{s},{e},{n}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "AddressID,HouseNumber,Direction,StreetName,StreetType,Suffix,Unit,PostalCity,State,ZipCode,ParcelID",
            "orderByFields": "OBJECTID",
            "resultOffset": offset, "resultRecordCount": 2000,
            "f": "geojson",
        }
        page = json.loads(http(COUNTY_URL + "?" + urllib.parse.urlencode(params)))
        feats += page.get("features", [])
        if not page.get("exceededTransferLimit") and not page.get("properties", {}).get("exceededTransferLimit"):
            break
        offset += len(page["features"])
    if cache:
        cache.write_text(json.dumps(feats))
    return feats



CACHE_VERSION = "v2"


def fetch_osm(bbox, overpass: str, cache: Path | None) -> dict:
    if cache and cache.exists():
        return json.loads(cache.read_text())
    w, s, e, n = bbox
    pad = 0.004  # ~400 m, so streets and addresses just outside the box are still seen
    b = f"{s},{w},{n},{e}"
    bp = f"{s - pad},{w - pad},{n + pad},{e + pad}"
    # Buildings in the box and every addressed object in the padded box, with
    # full metadata (so any of them can be modified) and their nodes.
    q = f"""
[out:json][timeout:300];
(
  way["building"]({b});
  nwr["addr:housenumber"]({bp});
)->.all;
.all out meta center;
node(w.all);
out meta;
relation["building"]({b});
out meta geom;
way["highway"]["name"]({bp});
out geom tags;
"""
    data = json.loads(http(overpass, {"data": q}))
    if cache:
        cache.write_text(json.dumps(data))
    return data


OSM_API = "https://api.openstreetmap.org/api/0.6"


def fetch_relation_members(rid: int, cache: Path | None) -> dict:
    """A relation's member ways and their nodes, with metadata, from the OSM API:
    {"ways": {id: el}, "nodes": {id: el}}. Written unmodified next to a modified
    relation so JOSM has it complete (it warns about incomplete multipolygons)."""
    if cache and cache.exists():
        return json.loads(cache.read_text())
    import xml.etree.ElementTree as ET
    root = ET.fromstring(http(f"{OSM_API}/relation/{rid}/full"))
    meta = ("version", "timestamp", "changeset", "user", "uid")
    out = {"ways": {}, "nodes": {}}
    for e in root:
        attrs = {k: e.get(k) for k in meta if e.get(k) is not None}
        tags = {t.get("k"): t.get("v") for t in e.findall("tag")}
        if e.tag == "node":
            out["nodes"][e.get("id")] = {**attrs, "lat": e.get("lat"), "lon": e.get("lon"), "tags": tags}
        elif e.tag == "way":
            out["ways"][e.get("id")] = {**attrs, "nodes": [int(nd.get("ref")) for nd in e.findall("nd")], "tags": tags}
    if cache:
        cache.write_text(json.dumps(out))
    return out


POI_KEYS = ("shop", "amenity", "office", "craft", "healthcare", "leisure", "club", "tourism")
# amenities that are street furniture or parking, never a business with an address
NOT_POI_AMENITY = {"parking", "parking_entrance", "parking_space", "bicycle_parking", "motorcycle_parking",
                   "bench", "waste_basket", "waste_disposal", "recycling", "vending_machine", "atm",
                   "charging_station", "drinking_water", "toilets", "post_box", "telephone", "fountain",
                   "shelter", "loading_dock", "bicycle_rental", "car_sharing", "letter_box", "clock"}
POI_MAX_DIST = 25.0   # metres between a county point and the POI it is merged into


def fetch_pois(bbox, overpass: str, cache: Path | None) -> dict:
    """Business/amenity nodes in the box, with metadata so they can be modified."""
    if cache and cache.exists():
        return json.loads(cache.read_text())
    w, s, e, n = bbox
    b = f"{s},{w},{n},{e}"
    q = "[out:json][timeout:180];(" + "".join(f'node["{k}"]({b});' for k in POI_KEYS) + ");out meta;"
    data = json.loads(http(overpass, {"data": q}))
    if cache:
        cache.write_text(json.dumps(data))
    return data


def is_poi(tags: dict) -> bool:
    if "addr:housenumber" in tags or not (tags.get("name") or tags.get("brand")):
        return False
    if tags.get("amenity") in NOT_POI_AMENITY and not any(k in tags for k in POI_KEYS if k != "amenity"):
        return False
    return any(k in tags for k in POI_KEYS)


def poi_kind(tags: dict) -> str:
    return next((f"{k}={tags[k]}" for k in POI_KEYS if k in tags), "")


# --- model --------------------------------------------------------------------

@dataclass
class Addr:
    """A county address point."""
    lon: float
    lat: float
    hn: str
    street_raw: str
    street_key: str
    street_fallback: str
    unit: str | None
    postcode: str | None
    city: str | None
    county_id: str
    street: str | None = None       # spelling to write (nearby OSM highway name, or override)
    street_from_osm: bool = False   # street resolved against an OSM highway / override
    precise_unit: bool = False      # unit has its own point (not stacked with other units)
    in_scope: bool = True           # inside the block (only these are added)
    exists: bool = False            # represented by an existing OSM object
    handled: bool = False           # decided elsewhere (review / correction); don't add
    bidx: int | None = None         # building it falls in

    def key(self):
        return (self.hn, self.street_key)

    def ekey(self):
        """Identity of the thing we map: units only count when they have their own point."""
        return (self.hn, self.street_key, self.unit if self.precise_unit else None)


@dataclass
class OsmAddr:
    """An existing OSM object carrying addr:housenumber."""
    type: str
    id: int
    el: dict
    lon: float
    lat: float
    pt: Point
    in_scope: bool
    bidx: int | None = None        # set when the object is itself a building outline
    matched: list = field(default_factory=list)  # county Addr it represents
    fuzzy: bool = False            # matched with a different street spelling
    rekey: bool = False            # its address is being replaced by the county's
    handled: bool = False          # sent to review

    @property
    def tags(self) -> dict:
        return self.el.get("tags", {})

    @property
    def ref(self) -> str:
        return f"{self.type}/{self.id}"

    def hns(self) -> list[str]:
        return [h.strip() for h in re.split(r"[;,]", self.tags.get("addr:housenumber", "")) if h.strip()]

    def keys(self, tags: dict | None = None) -> list[tuple]:
        t = tags if tags is not None else self.tags
        hns = [h.strip() for h in re.split(r"[;,]", t.get("addr:housenumber", "")) if h.strip()]
        return [(h, street_key(t.get("addr:street", "")), t.get("addr:unit")) for h in hns]


@dataclass
class Building:
    osm_type: str
    osm_id: int
    poly: Polygon
    tags: dict
    raw: dict | None = None
    addrs: list[Addr] = field(default_factory=list)  # county points to add here
    county_count: int = 0                             # all county points inside
    osm_addr: OsmAddr | None = None                   # set if the outline itself has an address


MINOR_BUILDINGS = {"garage", "garages", "shed", "carport", "roof", "hut", "cabin", "greenhouse"}


def build_relation_polygon(el) -> Polygon | None:
    outers = [LineString([(g["lon"], g["lat"]) for g in m["geometry"]])
              for m in el.get("members", []) if m.get("role") == "outer" and m.get("geometry")]
    polys = list(polygonize(outers))
    if not polys:
        return None
    return unary_union(polys)


_AFFIX = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "EXT"} | set(COUNTY_TYPES) | {
    v for k, v in _CANON.items() if k not in ("SAINT", "MOUNT") and not v[0].isdigit()}


def similar_street(k1: str, k2: str) -> bool:
    """Same street with a spelling difference in the name itself (Spruell/Spruel).
    Directions (W/E Trade), types (Providence Rd/Ln) and numbers must match exactly."""
    if not k1 or not k2 or re.findall(r"\d+", k1) != re.findall(r"\d+", k2):
        return False
    t1, t2 = k1.split(), k2.split()
    if [t for t in t1 if t in _AFFIX] != [t for t in t2 if t in _AFFIX]:
        return False
    core1 = " ".join(t for t in t1 if t not in _AFFIX)
    core2 = " ".join(t for t in t2 if t not in _AFFIX)
    return bool(core1) and difflib.SequenceMatcher(None, core1, core2).ratio() >= 0.85


# --- main logic -----------------------------------------------------------------

def run(args):
    area = None
    if args.polygon:
        g = json.loads(Path(args.polygon).read_text())
        g = g.get("geometry", g) if g.get("type") == "Feature" else g["features"][0]["geometry"]
        area = shape(g)
        args.bbox = ",".join(f"{x:.5f}" for x in area.bounds)
    if not args.bbox:
        sys.exit("need --bbox or --polygon")
    bbox = tuple(float(x) for x in args.bbox.split(","))
    if len(bbox) != 4:
        sys.exit("--bbox must be minlon,minlat,maxlon,maxlat")
    if area is None:
        area = Polygon([(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = out.parent / ".cache" if args.cache else None
    if cache_dir:
        cache_dir.mkdir(exist_ok=True)
    tag = args.bbox.replace(",", "_")

    print("Fetching county addresses...")
    # padded like the OSM query, so OSM addresses near the block edge can still
    # be matched; only points inside the block are ever added
    pad = 0.004
    cfeats = fetch_county((bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
                          cache_dir / f"county_{CACHE_VERSION}_{tag}.json" if cache_dir else None)
    print(f"  {sum(area.contains(Point(f['geometry']['coordinates'])) for f in cfeats)} active address points in block")
    print("Fetching OSM data from Overpass...")
    osm = fetch_osm(bbox, args.overpass, cache_dir / f"osm_{CACHE_VERSION}_{tag}.json" if cache_dir else None)
    poi_data = {"elements": []}
    if args.merge_pois != "off":
        poi_data = fetch_pois(bbox, args.overpass, cache_dir / f"pois_v1_{tag}.json" if cache_dir else None)

    proj = Proj((bbox[1] + bbox[3]) / 2)
    P = lambda lon, lat: Point(*proj.xy(lon, lat))  # noqa: E731

    overrides = {}
    if args.street_overrides:
        with open(args.street_overrides) as f:
            for row in csv.DictReader(f):
                overrides[row["county"].strip().upper()] = row["osm"].strip()

    # --- parse OSM: merge the several outputs of each element
    els: dict[tuple, dict] = {}
    for el in osm["elements"]:
        els.setdefault((el["type"], el["id"]), {}).update(el)
    nodes = {i: e for (t, i), e in els.items() if t == "node" and "lat" in e}

    def way_coords(w):
        return [(nodes[n]["lon"], nodes[n]["lat"]) for n in w.get("nodes", []) if n in nodes]

    buildings: list[Building] = []
    bidx_of_way: dict[int, int] = {}
    for (t, i), e in els.items():
        tags = e.get("tags", {})
        if "building" not in tags:
            continue
        if t == "way" and "nodes" in e and e["nodes"][0] == e["nodes"][-1]:
            ll = way_coords(e)
            if len(ll) != len(e["nodes"]) or len(ll) < 4:
                continue
            poly = Polygon([proj.xy(*c) for c in ll])
            if not poly.is_valid:
                poly = poly.buffer(0)
            bidx_of_way[i] = len(buildings)
            buildings.append(Building("way", i, poly, tags, raw=e))
        elif t == "relation" and any("geometry" in m for m in e.get("members", [])):
            poly = build_relation_polygon(e)
            if poly is not None:
                buildings.append(Building("relation", i, affine_transform(poly, [proj.kx, 0, 0, proj.ky, 0, 0]), tags, raw=e))

    osm_addrs: list[OsmAddr] = []
    for (t, i), e in els.items():
        tags = e.get("tags", {})
        if "addr:housenumber" not in tags:
            continue
        if t == "node":
            lon, lat = e["lon"], e["lat"]
        elif "center" in e:
            lon, lat = e["center"]["lon"], e["center"]["lat"]
        else:
            continue
        oa = OsmAddr(t, i, e, lon, lat, P(lon, lat), area.contains(Point(lon, lat)))
        if t == "way" and i in bidx_of_way:
            oa.bidx = bidx_of_way[i]
            buildings[oa.bidx].osm_addr = oa
        osm_addrs.append(oa)

    highways = [e for (t, i), e in els.items() if t == "way" and "geometry" in e and "highway" in e.get("tags", {})]
    print(f"  {len(buildings)} buildings, {len(highways)} named highway ways, {len(osm_addrs)} existing OSM addresses")

    # street key -> list of (geometry, exact OSM name)
    street_index = defaultdict(list)
    for h in highways:
        line = LineString([proj.xy(g["lon"], g["lat"]) for g in h["geometry"]])
        for k in ("name", "alt_name", "old_name"):
            if k in h["tags"]:
                street_index[street_key(h["tags"][k])].append((line, h["tags"]["name"]))

    # existing addresses indexed by house number (every value of "12;14")
    by_hn = defaultdict(list)
    for oa in osm_addrs:
        for h in oa.hns():
            by_hn[h].append(oa)

    # --- parse county
    addrs: list[Addr] = []
    for f in cfeats:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        raw = county_raw_street(p)
        addrs.append(Addr(
            lon=lon, lat=lat, hn=str(p["HouseNumber"]), street_raw=raw,
            street_key=street_key(raw), street_fallback=county_street(p),
            unit=p.get("Unit"), postcode=(p.get("ZipCode") or "")[:5] or None,
            city=title_word_phrase(p.get("PostalCity")), county_id=p["AddressID"],
            in_scope=area.contains(Point(lon, lat)),
        ))

    review = []
    stats = Counter()
    unmatched_streets = Counter()
    suggestions: dict[str, str] = {}

    def suggest(a: Addr, pt):
        if a.street_raw in suggestions:
            return
        near = {name for k, lst in street_index.items() for line, name in lst
                if line.distance(pt) <= args.street_radius}
        digits = re.findall(r"\d+", a.street_key)
        by_key = {street_key(n): n for n in near if re.findall(r"\d+", street_key(n)) == digits}
        m = difflib.get_close_matches(a.street_key, list(by_key), n=1, cutoff=0.75)
        suggestions[a.street_raw] = by_key[m[0]] if m else ""

    def flag(a: Addr | None, reason: str, oa: OsmAddr | None = None, **extra):
        lon, lat = (a.lon, a.lat) if a else (oa.lon, oa.lat)
        props = {"reason": reason}
        if a:
            props.update(county_address=f"{a.hn} {a.street_raw}" + (f" #{a.unit}" if a.unit else ""),
                         county_id=a.county_id, proposed_street=a.street or a.street_fallback,
                         # the tags the import would write for this address, for fixing it in the web UI
                         county_tags={**addr_tags(a, args), "addr:street": a.street or a.street_fallback})
        if oa:
            props.update(osm=oa.ref, osm_address=f"{oa.tags.get('addr:housenumber')} {oa.tags.get('addr:street', '')}".strip())
        props.update(extra)
        review.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props})
        stats[f"review_{reason}"] += 1

    # 1. resolve county street spelling against nearby OSM highways
    resolved: list[Addr] = []
    for a in addrs:
        pt = P(a.lon, a.lat)
        if a.street_raw.upper() in overrides:
            a.street = overrides[a.street_raw.upper()]
            a.street_key = street_key(a.street)
            a.street_from_osm = True
        cands = [(line.distance(pt), name) for line, name in street_index.get(a.street_key, [])]
        cands = [c for c in cands if c[0] <= args.street_radius]
        if cands:
            a.street = a.street or min(cands)[1]
            a.street_from_osm = True
            resolved.append(a)
        else:
            if a.in_scope:
                unmatched_streets[a.street_raw] += 1
                suggest(a, pt)
            if args.allow_unmatched_streets:
                a.street = a.street or a.street_fallback
                resolved.append(a)
    # Unresolved points still take part in matching (so they're never re-added
    # and existing OSM copies still get checked); they just can't be added.
    resolved = [a for a in resolved if a.in_scope]

    # 2. precise units: a unit with a point of its own
    by_loc = defaultdict(list)
    for a in addrs:
        x, y = proj.xy(a.lon, a.lat)
        by_loc[(a.key(), round(x), round(y))].append(a)
    for group in by_loc.values():
        if len(group) == 1 and group[0].unit:
            group[0].precise_unit = True

    # 3. which building each county point falls in
    tree = STRtree([b.poly for b in buildings]) if buildings else None
    pts = [P(a.lon, a.lat) for a in addrs]
    if tree is not None and pts:
        inside = defaultdict(list)
        for i, j in zip(*tree.query(pts, predicate="within")):
            inside[i].append(j)
        for i, a in enumerate(addrs):
            js = inside.get(i, [])
            if not js and args.snap > 0:
                js = list(tree.query_nearest(pts[i], max_distance=args.snap)[:1])
                if js:
                    flag(a, "snapped_to_nearby_building", osm=f"{buildings[js[0]].osm_type}/{buildings[js[0]].osm_id}")
            if js:
                a.bidx = min(js, key=lambda j: buildings[j].poly.area)
                buildings[a.bidx].county_count += 1

    # unaddressed business POIs and the building they sit in
    pois = []
    for el in poi_data["elements"]:
        if el.get("type") != "node" or not is_poi(el.get("tags", {})):
            continue
        pt = P(el["lon"], el["lat"])
        js = list(tree.query(pt, predicate="within")) if tree is not None else []
        if js:
            pois.append({"el": el, "pt": pt, "bidx": min(js, key=lambda j: buildings[j].poly.area)})

    def same_street(a: Addr, oa_street_key: str) -> str | None:
        """'exact', 'fuzzy' or None."""
        if oa_street_key == a.street_key:
            return "exact"
        if similar_street(oa_street_key, a.street_key):
            return "fuzzy"
        return None

    # county addresses by key, to tell a wrong OSM number from an offset one
    county_at = defaultdict(list)
    for a in addrs:
        county_at[a.key()].append((P(a.lon, a.lat), a))

    def county_has(hn: str, osm_street: str, near: Point) -> Addr | None:
        """The nearest county point with this number and street, if one is within dup_radius."""
        sk = street_key(osm_street)
        best = None
        for (h, k), pts_ in county_at.items():
            if h == hn and (k == sk or similar_street(k, sk)):
                for p_, a_ in pts_:
                    d = p_.distance(near)
                    if d <= args.dup_radius and (best is None or d < best[0]):
                        best = (d, a_)
        return best[1] if best else None

    def other_pt_dist(a_: Addr, oa_: OsmAddr) -> float:
        return P(a_.lon, a_.lat).distance(oa_.pt)

    # 4. reconcile county points with addressed building outlines they fall in
    for b in buildings:
        oa = b.osm_addr
        if oa is None:
            continue
        inside_pts = [a for a in addrs if a.bidx is not None and buildings[a.bidx] is b]
        if not inside_pts:
            continue
        osm_hns = oa.hns()
        osm_sk = street_key(oa.tags.get("addr:street", ""))
        same = [a for a in inside_pts if a.hn in osm_hns and same_street(a, osm_sk)]
        if same:
            for a in same:
                if not a.precise_unit or a.unit == oa.tags.get("addr:unit"):
                    a.exists = True
                oa.matched.append(a)
                oa.fuzzy |= same_street(a, osm_sk) == "fuzzy"
            continue  # other county numbers in this outline are handled as additions later
        keys = {a.key() for a in inside_pts}
        if not oa.in_scope:
            for a in inside_pts:      # decided by the neighbouring block
                a.handled = True
            continue
        other = county_has(osm_hns[0], oa.tags.get("addr:street", ""), oa.pt) \
            if len(keys) == 1 and len(osm_hns) == 1 and oa.type == "way" else None
        if other is not None:
            # OSM's number is a real county address nearby: the outline and the
            # point disagree about which house is which (offset). Needs eyes.
            for a in inside_pts:
                a.handled = True
            oa.handled = True
            ob = buildings[other.bidx] if other.bidx is not None else None
            flag(inside_pts[0], "osm_address_offset_from_county", oa,
                 osm_number_county_at=[round(other.lon, 7), round(other.lat, 7)],
                 osm_number_county_tags={**addr_tags(other, args), "addr:street": other.street or other.street_fallback},
                 osm_number_dist_m=round(other_pt_dist(other, oa), 1),
                 osm_number_building=f"{ob.osm_type}/{ob.osm_id}" if ob else "",
                 osm_number_building_addr=(f"{ob.osm_addr.tags.get('addr:housenumber')} {ob.osm_addr.tags.get('addr:street', '')}".strip()
                                           if ob and ob.osm_addr else ""))
            continue
        if len(keys) == 1 and len(osm_hns) == 1 and oa.type == "way":
            # one county address inside and OSM's number doesn't exist in the
            # county data: county wins. The old number no longer counts as
            # "in OSM" for anyone else (step 5).
            for a in inside_pts:
                oa.matched.append(a)
                a.exists = not a.precise_unit or a.unit == oa.tags.get("addr:unit")
            oa.rekey = True
            continue
        for a in inside_pts:
            a.handled = True
        oa.handled = True
        flag(inside_pts[0], "building_address_ambiguous", oa,
             county_addresses="; ".join(sorted({f"{a.hn} {a.street_raw}" for a in inside_pts})))

    # 5. everything else: same number + same/similar street within dup_radius
    for a in addrs:
        if a.exists or a.handled:
            continue
        pt = P(a.lon, a.lat)
        best = None
        for oa in by_hn.get(a.hn, []):
            if oa.rekey:
                continue
            d = oa.pt.distance(pt)
            if d > args.dup_radius:
                continue
            how = same_street(a, street_key(oa.tags.get("addr:street", "")))
            if not how:
                continue
            if a.precise_unit and oa.tags.get("addr:unit") not in (None, a.unit):
                continue
            if a.precise_unit and oa.tags.get("addr:unit") is None and d > 5:
                continue  # building/complex address without unit; the unit itself is new
            rank = (how != "exact", d)
            if best is None or rank < best[0]:
                best = (rank, oa, how)
        if best:
            _, oa, how = best
            a.exists = True
            oa.matched.append(a)
            oa.fuzzy |= how == "fuzzy"
    stats["already_in_osm"] = sum(1 for a in addrs if a.exists and a.in_scope)
    resolved_ids = {id(a) for a in resolved}
    for a in addrs:
        if a.in_scope and not a.street_from_osm and not (a.exists or a.handled) and id(a) not in resolved_ids:
            flag(a, "street_not_found_in_osm", osm_suggestion=suggestions.get(a.street_raw, ""))

    # 6. corrections: county data is trusted for the fields it has
    corrections = []   # (oa, new_tags, fields)
    for oa in osm_addrs:
        if not oa.matched:
            if oa.in_scope and not oa.handled:
                stats["osm_address_not_in_county"] += 1
                if args.flag_unknown:
                    flag(None, "osm_address_not_in_county", oa)
            continue
        if not oa.in_scope:
            continue
        # representative: the plain (non-unit) county point if there is one
        a = next((m for m in oa.matched if not m.precise_unit), oa.matched[0])
        new = dict(oa.tags)
        fields = []
        osm_hns = oa.hns()
        if a.hn not in osm_hns:
            if len(osm_hns) != 1:
                flag(a, "multi_value_housenumber_differs", oa)
                continue
            new["addr:housenumber"] = a.hn
            fields.append("housenumber")
        if a.street_from_osm and oa.tags.get("addr:street") != a.street:
            new["addr:street"] = a.street
            fields.append("street")
        elif not a.street_from_osm and street_key(oa.tags.get("addr:street", "")) != a.street_key:
            flag(a, "street_differs_and_not_in_osm", oa)
            continue
        fill = args.corrections == "all"

        def setf(k, v, name):
            if v is None:
                return
            cur = oa.tags.get(k)
            if (cur is None and fill) or (cur is not None and cur != v):
                new[k] = v
                fields.append(name)

        setf("addr:postcode", a.postcode, "postcode")
        if args.city:
            setf("addr:city", a.city, "city")
        if args.state:
            setf("addr:state", "NC", "state")
        if a.precise_unit and len(oa.matched) == 1 and oa.tags.get("addr:unit") not in (None, a.unit):
            new["addr:unit"] = a.unit
            fields.append("unit")
        if not fields or args.corrections == "none":
            continue
        if oa.type == "relation" and "version" not in oa.el:
            flag(a, "relation_address_needs_update", oa, fields=",".join(fields))
            continue
        corrections.append((oa, new, fields))

    # 7. additions: county points that exist nowhere in OSM
    key_bldgs = defaultdict(set)
    for a in resolved:
        if not (a.exists or a.handled) and a.bidx is not None:
            buildings[a.bidx].addrs.append(a)
            key_bldgs[a.ekey()].add(a.bidx)
    spanning = {k for k, s in key_bldgs.items() if len(s) > 1}

    node_bldgs = defaultdict(set)
    for j, b in enumerate(buildings):
        if b.osm_type == "way" and b.tags.get("building") not in MINOR_BUILDINGS:
            for n in set(b.raw["nodes"]):
                node_bldgs[n].add(j)
    shared = Counter()
    for js in node_bldgs.values():
        for j in js:
            for k in js:
                if j != k:
                    shared[(j, k)] += 1
    neighbors = defaultdict(set)
    for (j, k), n in shared.items():
        if n >= 2:
            neighbors[j].add(k)

    tag_building: dict[int, tuple[Building, dict, Addr]] = {}   # bidx -> (building, new tags, addr)
    new_nodes: list[tuple[float, float, dict, Addr]] = []

    for j, b in enumerate(buildings):
        if not b.addrs:
            continue
        ref = f"{b.osm_type}/{b.osm_id}"
        by_key: dict[tuple, list[Addr]] = defaultdict(list)
        for a in b.addrs:
            by_key[a.ekey()].append(a)
        keys = list(by_key)
        firsts = [by_key[k][0] for k in keys]

        if b.osm_type != "way" and "version" not in (b.raw or {}):
            # downloaded without metadata (old cache): can't be written as a modification
            for a in firsts:
                flag(a, "multipolygon_building", osm=ref)
            continue
        if b.tags.get("building") in MINOR_BUILDINGS:
            for a in firsts:
                flag(a, "minor_building_type", osm=ref, building=b.tags["building"])
            continue
        if any(k in spanning for k in keys):
            for a in firsts:
                flag(a, "address_spans_multiple_buildings", osm=ref)
            continue
        if b.osm_addr is not None:
            # outline already carries (or is being corrected to) another address:
            # the remaining numbers go on nodes inside it
            for a in firsts:
                new_nodes.append((a.lon, a.lat, addr_tags(a, args), a))
            continue
        if len(keys) == 1:
            tag_building[j] = (b, {**b.tags, **addr_tags(firsts[0], args)}, firsts[0])
            continue
        numbers = {(hn, sk) for hn, sk, _ in keys}
        empty_neighbors = [k for k in neighbors[j]
                           if buildings[k].county_count == 0 and buildings[k].osm_addr is None]
        if len(numbers) > 1 and empty_neighbors:
            for a in firsts:
                flag(a, "attached_building_points_misaligned", osm=ref,
                     empty_neighbors=",".join(f"way/{buildings[n].osm_id}" for n in empty_neighbors))
        elif args.multi == "nodes":
            for a in firsts:
                new_nodes.append((a.lon, a.lat, addr_tags(a, args), a))
        else:
            for a in firsts:
                flag(a, "multiple_addresses_in_building", osm=ref)

    # 7b. strip malls and office buildings: a county point whose shop/office is
    #     already mapped as an unaddressed POI puts its address on that POI
    #     instead of on a new node. Only clear pairs: each is the other's
    #     nearest, within POI_MAX_DIST, and no other county point is nearly as close.
    poi_merge: dict[int, int] = {}                  # new_nodes index -> pois index
    poi_candidates: dict[int, list] = {}            # new_nodes index -> [(pois index, metres)]
    nodes_in = defaultdict(list)
    for i, (lon, lat, tags, a) in enumerate(new_nodes):
        if a.bidx is not None:
            nodes_in[a.bidx].append(i)
    pois_in = defaultdict(list)
    for k, poi in enumerate(pois):
        pois_in[poi["bidx"]].append(k)
    for j, nis in nodes_in.items():
        pks = pois_in.get(j)
        if not pks:
            continue
        d = {(i, k): P(new_nodes[i][0], new_nodes[i][1]).distance(pois[k]["pt"]) for i in nis for k in pks}
        for k in pks:
            ranked = sorted(nis, key=lambda i: d[i, k])
            i, d1 = ranked[0], d[ranked[0], k]
            d2 = d[ranked[1], k] if len(ranked) > 1 else math.inf
            if d1 > POI_MAX_DIST or d2 < max(1.5 * d1, d1 + 4):
                continue
            if min(pks, key=lambda kk: d[i, kk]) != k or i in poi_merge:
                continue
            if args.merge_pois == "auto":
                poi_merge[i] = k
        merged_pois = {poi_merge[i] for i in nis if i in poi_merge}
        for i in nis:
            if i not in poi_merge:
                poi_candidates[i] = sorted(((k, d[i, k]) for k in pks if k not in merged_pois), key=lambda x: x[1])

    seen_outside = set()
    for a in resolved:
        if a.exists or a.handled or a.bidx is not None or a.ekey() in seen_outside:
            continue
        seen_outside.add(a.ekey())
        if args.unmatched == "nodes":
            new_nodes.append((a.lon, a.lat, addr_tags(a, args), a))
        elif args.unmatched == "review":
            flag(a, "no_building")
        else:
            stats["skipped_no_building"] += 1

    # 8. duplicate gate. Every new or re-keyed address is checked against every
    #    address in the downloaded area (block + 400 m), against the other
    #    proposals, and against changesets generated for other blocks.
    ledger = read_ledger(args.ledger, args.block_id)

    def final_keys(tags):
        hns = [h.strip() for h in re.split(r"[;,]", tags.get("addr:housenumber", "")) if h.strip()]
        return {(h, street_key(tags.get("addr:street", "")), tags.get("addr:unit")) for h in hns}

    proposals = []  # (kind, payload, keys, lon, lat, addr-for-review, osm_addr)
    for j, (b, tags, a) in tag_building.items():
        c = b.poly.centroid
        proposals.append(("tag_building", j, final_keys(tags), c.x / proj.kx, c.y / proj.ky, a, None))
    for i, (lon, lat, tags, a) in enumerate(new_nodes):
        proposals.append(("new_node", i, final_keys(tags), lon, lat, a, None))
    for i, (oa, new, fields) in enumerate(corrections):
        if final_keys(new) != final_keys(oa.tags):
            proposals.append(("rekey", i, final_keys(new) - final_keys(oa.tags), oa.lon, oa.lat, oa.matched[0], oa))

    # what stays in OSM untouched (or touched without changing its key)
    rekeyed = {p[6].ref for p in proposals if p[0] == "rekey"}
    existing_keys = defaultdict(list)
    for oa in osm_addrs:
        if oa.ref in rekeyed:
            continue
        for k in oa.keys():
            existing_keys[k].append(oa.ref)
    proposal_count = Counter(k for p in proposals for k in p[2])

    rejected = set()
    for idx, (kind, payload, keys, lon, lat, a, oa) in enumerate(proposals):
        for k in keys:
            clash = [r for r in existing_keys.get(k, []) if not (oa and r == oa.ref)]
            if clash:
                why, other = "would_duplicate_existing", ",".join(clash[:3])
            elif proposal_count[k] > 1:
                why, other = "would_duplicate_in_changeset", ""
            elif (hit := ledger_hit(ledger, k, lon, lat)) is not None:
                why, other = "pending_in_other_block", f"block {hit}"
            else:
                continue
            rejected.add(idx)
            flag(a, why, oa, duplicate_of=other)
            break

    final_tag_building = {}
    final_new_nodes = []
    final_poi_merge = []    # (new_nodes index, pois index)
    final_corrections = []
    rekey_rejected = {proposals[i][1] for i in rejected if proposals[i][0] == "rekey"}
    for idx, (kind, payload, *_rest) in enumerate(proposals):
        if idx in rejected:
            stats["duplicates_prevented"] += 1
            continue
        if kind == "tag_building":
            final_tag_building[payload] = tag_building[payload]
        elif kind == "new_node" and payload in poi_merge:
            final_poi_merge.append((payload, poi_merge[payload]))
        elif kind == "new_node":
            final_new_nodes.append(new_nodes[payload])
    for i, c in enumerate(corrections):
        if i not in rekey_rejected:
            final_corrections.append(c)

    # --- assemble outputs
    modified_ways: dict[int, dict] = {}
    modified_nodes: dict[int, dict] = {}
    modified_relations: dict[int, dict] = {}
    for j, (b, tags, a) in final_tag_building.items():
        (modified_relations if b.osm_type == "relation" else modified_ways)[b.osm_id] = {**b.raw, "tags": tags}
        stats["building_tagged_with_unit" if a.precise_unit else "building_tagged"] += 1
    for lon, lat, tags, a in final_new_nodes:
        stats["unit_nodes" if a.precise_unit else "multi_address_nodes" if a.bidx is not None else "standalone_nodes"] += 1
    poi_records = {"merged": [], "candidates": {}}
    for i, k in final_poi_merge:
        lon, lat, tags, a = new_nodes[i]
        el = pois[k]["el"]
        modified_nodes[el["id"]] = {**el, "tags": {**el.get("tags", {}), **tags}}
        stats["poi_address_merged"] += 1
        poi_records["merged"].append({"osm": f"node/{el['id']}", "name": el["tags"].get("name") or el["tags"].get("brand"),
                                      "kind": poi_kind(el["tags"]), "lon": lon, "lat": lat, "addr": tags})
    final_new_ids = {id(t) for t in final_new_nodes}
    for i, cands in poi_candidates.items():
        if id(new_nodes[i]) not in final_new_ids or not cands:
            continue
        lon, lat, tags, a = new_nodes[i]
        cid = "new:" + "|".join(tags.get(x, "") for x in ("addr:housenumber", "addr:street", "addr:unit"))
        poi_records["candidates"][cid] = [
            {"osm": f"node/{pois[k]['el']['id']}", "name": pois[k]["el"]["tags"].get("name") or pois[k]["el"]["tags"].get("brand"),
             "kind": poi_kind(pois[k]["el"]["tags"]), "dist": round(dist, 1), "el": pois[k]["el"]}
            for k, dist in cands]
    stats["poi_merge_candidates"] = len(poi_records["candidates"])

    corr_records = []
    for oa, new, fields in final_corrections:
        if oa.type == "way":
            modified_ways[oa.id] = {**oa.el, "tags": new}
        elif oa.type == "relation":
            modified_relations[oa.id] = {**oa.el, "tags": new}
        else:
            modified_nodes[oa.id] = {**oa.el, "tags": new}
        stats["objects_corrected"] += 1
        for f_ in fields:
            stats[f"corrected_{f_}"] += 1
        corr_records.append({
            "osm": oa.ref, "lon": oa.lon, "lat": oa.lat, "fields": fields,
            "before": {k: v for k, v in oa.tags.items() if k.startswith("addr:")},
            "after": {k: v for k, v in new.items() if k.startswith("addr:")},
            "fuzzy_street": oa.fuzzy,
        })

    # Independent check: apply the changeset to the downloaded data and make sure
    # no address occurs more often than before. Fails the run if it does.
    before = Counter(k for oa in osm_addrs for k in oa.keys())
    after = Counter(before)
    changed = {("way", i): w["tags"] for i, w in modified_ways.items()}
    changed.update({("node", i): n["tags"] for i, n in modified_nodes.items()})
    changed.update({("relation", i): r["tags"] for i, r in modified_relations.items()})
    old_tags = {(oa.type, oa.id): oa.tags for oa in osm_addrs}
    for ref, tags in changed.items():
        for k in final_keys(old_tags.get(ref, {})):
            after[k] -= 1
        for k in final_keys(tags):
            after[k] += 1
    for _, _, tags, _ in final_new_nodes:
        for k in final_keys(tags):
            after[k] += 1
    violations = [k for k, n in after.items() if n > 1 and n > before[k]]
    if violations:
        sys.exit(f"SAFETY CHECK FAILED: changeset would duplicate {len(violations)} address(es): {violations[:5]}")
    stats["preexisting_duplicate_keys"] = sum(1 for n in before.values() if n > 1)

    stats["county_points"] = sum(a.in_scope for a in addrs)
    stats["review_total"] = len(review)
    stats["changes_total"] = len(modified_ways) + len(modified_nodes) + len(modified_relations) + len(final_new_nodes)

    members = {"ways": {}, "nodes": {}}
    for rid in modified_relations:
        m = fetch_relation_members(rid, cache_dir / f"relfull_{rid}.json" if cache_dir else None)
        members["ways"].update(m["ways"])
        members["nodes"].update(m["nodes"])
    write_osm(out.with_suffix(".osm"), modified_ways, modified_nodes, nodes, [(x, y, t) for x, y, t, _ in final_new_nodes],
              modified_relations, members)
    Path(f"{out}_review.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": review}))
    Path(f"{out}_corrections.json").write_text(json.dumps(corr_records))
    Path(f"{out}_pois.json").write_text(json.dumps(poi_records))
    keys_out = [{"key": list(k), "lon": lon, "lat": lat}
                for idx, (kind, payload, keys, lon, lat, a, oa) in enumerate(proposals) if idx not in rejected for k in keys]
    Path(f"{out}_keys.json").write_text(json.dumps(keys_out))

    summary = [f"bbox: {args.bbox}", ""]
    summary += [f"{k:32} {v}" for k, v in sorted(stats.items())]
    summary += ["", "Street names not matched to a nearby OSM highway (add to --street-overrides if OSM is right):"]
    summary += [f"  {n:6}  {s:30} " + (f"OSM nearby: {suggestions[s]}" if suggestions.get(s) else "")
                for s, n in unmatched_streets.most_common(50)]
    summary += ["", "Suggested changeset tags:",
                "  source=Mecklenburg County GIS Master Address",
                "  import=yes",
                "  url=<your import wiki page>"]
    Path(f"{out}_summary.txt").write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    print(f"\nWrote {out.with_suffix('.osm')}, {out}_review.geojson, {out}_corrections.json, {out}_summary.txt")


# --- cross-block ledger -----------------------------------------------------------

def read_ledger(path: str | None, block_id: int | None) -> dict:
    """{key tuple: [(lon, lat, block id)]} for addresses proposed by *other* blocks."""
    if not path or not Path(path).exists():
        return {}
    led = defaultdict(list)
    for bid, entries in json.loads(Path(path).read_text()).items():
        if block_id is not None and int(bid) == block_id:
            continue
        for e in entries:
            led[tuple(e["key"])].append((e["lon"], e["lat"], int(bid)))
    return led


def ledger_hit(ledger: dict, key: tuple, lon: float, lat: float, radius_m: float = 1000) -> int | None:
    for lo, la, bid in ledger.get(tuple(key), []):
        dx = (lo - lon) * 111320 * math.cos(math.radians(lat))
        dy = (la - lat) * 110540
        if math.hypot(dx, dy) <= radius_m:
            return bid
    return None


def title_word_phrase(s: str | None) -> str | None:
    return " ".join(title_word(w) for w in s.split()) if s else None


def addr_tags(a: Addr, args) -> dict:
    t = {"addr:housenumber": a.hn, "addr:street": a.street}
    if a.postcode:
        t["addr:postcode"] = a.postcode
    if a.city and args.city:
        t["addr:city"] = a.city
    if args.state:
        t["addr:state"] = "NC"
    if a.precise_unit:
        t["addr:unit"] = a.unit
    return t


def write_osm(path: Path, modified_ways: dict, modified_nodes: dict, nodes: dict, new_nodes: list,
              modified_relations: dict | None = None, members: dict | None = None):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<osm version="0.6" generator="meck-addr-conflate">']

    def tag_lines(tags):
        return [f"    <tag k={quoteattr(k)} v={quoteattr(str(v))}/>" for k, v in tags.items()]

    def meta(el):
        return " ".join(f'{k}={quoteattr(str(el[k]))}' for k in ("version", "timestamp", "changeset", "user", "uid") if k in el)

    needed = {n for w in modified_ways.values() for n in w["nodes"]} | set(modified_nodes)
    for nid in sorted(needed):
        n = modified_nodes.get(nid) or nodes[nid]
        action = ' action="modify"' if nid in modified_nodes else ""
        tl = tag_lines(n.get("tags", {}))
        head = f'  <node id="{nid}"{action} {meta(n)} lat="{n["lat"]}" lon="{n["lon"]}"'
        lines += [head + ">", *tl, "  </node>"] if tl else [head + "/>"]
    # unmodified member ways (and their nodes) of modified relations
    members = members or {"ways": {}, "nodes": {}}
    member_ways = {int(k): v for k, v in members["ways"].items() if int(k) not in modified_ways}
    for nid, n in sorted(members["nodes"].items(), key=lambda kv: int(kv[0])):
        if int(nid) in needed:
            continue
        tl = tag_lines(n.get("tags", {}))
        head = f'  <node id="{nid}" {meta(n)} lat="{n["lat"]}" lon="{n["lon"]}"'
        lines += [head + ">", *tl, "  </node>"] if tl else [head + "/>"]
    for i, (lon, lat, tags) in enumerate(new_nodes, start=1):
        lines += [f'  <node id="-{i}" lat="{lat:.7f}" lon="{lon:.7f}">', *tag_lines(tags), "  </node>"]
    for wid, w in member_ways.items():
        lines.append(f'  <way id="{wid}" {meta(w)}>')
        lines += [f'    <nd ref="{n}"/>' for n in w["nodes"]]
        lines += tag_lines(w.get("tags", {}))
        lines.append("  </way>")
    for wid, w in modified_ways.items():
        lines.append(f'  <way id="{wid}" action="modify" {meta(w)}>')
        lines += [f'    <nd ref="{n}"/>' for n in w["nodes"]]
        lines += tag_lines(w["tags"])
        lines.append("  </way>")
    # relations: their member ways were written above (unmodified) so JOSM has them complete
    for rid, r in (modified_relations or {}).items():
        lines.append(f'  <relation id="{rid}" action="modify" {meta(r)}>')
        lines += [f'    <member type="{m["type"]}" ref="{m["ref"]}" role={quoteattr(m.get("role", ""))}/>' for m in r.get("members", [])]
        lines += tag_lines(r["tags"])
        lines.append("  </relation>")
    lines.append("</osm>")
    path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", help="minlon,minlat,maxlon,maxlat (keep it neighborhood-sized)")
    ap.add_argument("--polygon", help="GeoJSON polygon: only county points / OSM addresses inside it are processed")
    ap.add_argument("--out", required=True, help="output path prefix, e.g. out/dilworth")
    ap.add_argument("--overpass", default=DEFAULT_OVERPASS)
    ap.add_argument("--cache", action="store_true", help="cache downloads in <out dir>/.cache")
    ap.add_argument("--multi", choices=["nodes", "review"], default="nodes",
                    help="building containing several different addresses: add separate nodes, or send to review")
    ap.add_argument("--unmatched", choices=["skip", "nodes", "review"], default="skip",
                    help="address not inside any building")
    ap.add_argument("--snap", type=float, default=0.0,
                    help="metres: attach an address outside any building to the nearest building within this distance (flagged)")
    ap.add_argument("--street-radius", type=float, default=300.0, help="metres to search for the matching OSM street")
    ap.add_argument("--dup-radius", type=float, default=150.0,
                    help="metres: an OSM address with the same number and street within this represents the county point")
    ap.add_argument("--street-overrides", help="CSV with columns county,osm (county = e.g. 'N TRYON ST')")
    ap.add_argument("--allow-unmatched-streets", action="store_true",
                    help="use the expanded county name when no nearby OSM street matches (default: send to review)")
    ap.add_argument("--corrections", choices=["all", "differences", "none"], default="all",
                    help="existing OSM addresses: fix differing fields and fill missing ones (all), "
                         "only fix differing fields (differences), or leave them alone (none)")
    ap.add_argument("--merge-pois", choices=["auto", "suggest", "off"], default="auto",
                    help="put a county address on the unaddressed shop/office POI it clearly belongs to (auto), "
                         "only list POIs as merge suggestions in the UI (suggest), or ignore POIs (off)")
    ap.add_argument("--flag-unknown", action="store_true",
                    help="send OSM addresses with no county counterpart to review (default: count only)")
    ap.add_argument("--ledger", help="JSON of addresses proposed by other blocks (written by blocks.py)")
    ap.add_argument("--block-id", type=int, help="this block's id, so its own ledger entries are ignored")
    ap.add_argument("--no-city", dest="city", action="store_false", help="omit addr:city")
    ap.add_argument("--no-state", dest="state", action="store_false", help="omit addr:state")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
