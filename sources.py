"""
Address sources: where the authoritative address points come from and how
their fields map onto addr:* tags.

A source is a JSON file (see sources/*.json). Minimal example, an ArcGIS layer:

  {
    "name": "Example County Address Points",
    "short_name": "county",
    "changeset": {"source": "Example County GIS", "comment": "Add addresses from Example County"},
    "area": {"relation": 123456},
    "fetch": {"type": "arcgis", "url": "https://.../FeatureServer/0", "where": "STATUS='Active'"},
    "fields": {"id": "OBJECTID", "housenumber": "ADDNUM", "street": "FULLNAME",
               "unit": "UNIT", "postcode": "ZIP", "city": "CITY", "state": "STATE"}
  }

fetch.type:
  arcgis   ArcGIS REST FeatureServer / MapServer layer, queried per block by bbox.
  file     A local GeoJSON, GeoJSON-lines (e.g. OpenAddresses .geojson) or CSV
           file ("path"; CSV also needs "lon" and "lat" column names). Read once
           and filtered by bbox.

fields: each value names a source field, or is {"value": "..."} for a constant
(e.g. "state": {"value": "NC"}). "street" is either one field holding the full
name, or {"prefix", "name", "type", "suffix"} fields that are joined. Only
"housenumber" and "street" are required. Optional "filter": {field: value}
keeps only matching records (applied after "where").

street_types: abbreviation -> full word, for expanding the source's street types
when no nearby OSM street matches (merged over the USPS abbreviations below).
"""

from __future__ import annotations

import csv
import json
import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# USPS Publication 28 street suffix abbreviations (the common ones)
USPS_TYPES = {
    "ALY": "Alley", "AVE": "Avenue", "AV": "Avenue", "BLVD": "Boulevard", "BYP": "Bypass",
    "CIR": "Circle", "CT": "Court", "CV": "Cove", "CRES": "Crescent", "XING": "Crossing",
    "DR": "Drive", "EXPY": "Expressway", "FWY": "Freeway", "HWY": "Highway", "LN": "Lane",
    "LOOP": "Loop", "PKWY": "Parkway", "PL": "Place", "PLZ": "Plaza", "RD": "Road", "RUN": "Run",
    "ROW": "Row", "SQ": "Square", "ST": "Street", "TER": "Terrace", "TRCE": "Trace",
    "TRL": "Trail", "WAY": "Way", "WY": "Way", "PT": "Point", "PATH": "Path", "PIKE": "Pike",
}
DIRECTIONS = {"N": "North", "S": "South", "E": "East", "W": "West",
              "NE": "Northeast", "NW": "Northwest", "SE": "Southeast", "SW": "Southwest"}
SUFFIXES = {**DIRECTIONS, "EXT": "Extension"}


def title_word(w: str) -> str:
    if re.fullmatch(r"\d+(ST|ND|RD|TH)", w):
        return w.lower()
    if w.startswith("MC") and len(w) > 2:
        return "Mc" + w[2:].capitalize()
    if w.startswith("O'") and len(w) > 2:
        return "O'" + w[2:].capitalize()
    return "-".join(p.capitalize() for p in w.split("-"))


def nice(s: str | None) -> str | None:
    """Title-case values the source writes in capitals; leave mixed case alone."""
    if not s:
        return None
    s = " ".join(str(s).split())
    return " ".join(title_word(w) for w in s.split()) if s.isupper() else s


class Source:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        cfg = json.loads(self.path.read_text())
        self.cfg = cfg
        self.name = cfg.get("name", self.path.stem)
        self.short_name = cfg.get("short_name", "source")
        self.changeset = {"source": self.name, "comment": f"Add addresses from {self.name}", **cfg.get("changeset", {})}
        self.area = cfg.get("area", {})
        self.fetch_cfg = cfg["fetch"]
        self.fields = cfg["fields"]
        self.filter = cfg.get("filter", {})
        self.types = {**USPS_TYPES, **{k.upper(): v for k, v in cfg.get("street_types", {}).items()}}
        self.postcode_digits = cfg.get("postcode_digits")
        self._file_cache = None

    # --- field access

    def _get(self, p: dict, spec):
        if spec is None:
            return None
        if isinstance(spec, dict) and "value" in spec:
            return spec["value"]
        v = p.get(spec)
        return None if v is None or str(v).strip() == "" else str(v).strip()

    def _source_fields(self) -> list[str]:
        out = set(self.filter)
        for k, spec in self.fields.items():
            if isinstance(spec, str):
                out.add(spec)
            elif isinstance(spec, dict) and "value" not in spec:
                out |= {v for v in spec.values() if isinstance(v, str)}
        return sorted(out)

    def street(self, p: dict) -> tuple[str, str]:
        """(raw street as the source writes it, readable expanded name)."""
        spec = self.fields["street"]
        if isinstance(spec, dict) and "value" not in spec:
            parts = {k: (self._get(p, spec.get(k)) or "").upper() for k in ("prefix", "name", "type", "suffix")}
            raw = " ".join(v for v in parts.values() if v)
            out = []
            if parts["prefix"]:
                out.append(DIRECTIONS.get(parts["prefix"], parts["prefix"].title()))
            out += [title_word(w) for w in parts["name"].split()]
            if parts["type"]:
                out.append(self.types.get(parts["type"], parts["type"].title()))
            if parts["suffix"]:
                out.append(SUFFIXES.get(parts["suffix"], parts["suffix"].title()))
            return raw, " ".join(out)
        full = self._get(p, spec) or ""
        raw = " ".join(full.upper().split())
        toks = raw.split()
        out = [title_word(t) for t in toks]
        if len(toks) > 1 and toks[0] in DIRECTIONS:
            out[0] = DIRECTIONS[toks[0]]
        last = len(toks) - 1
        if len(toks) > 2 and toks[last] in SUFFIXES and toks[last - 1] in self.types:
            out[last] = SUFFIXES[toks[last]]
            last -= 1
        if len(toks) > 1 and toks[last] in self.types:
            out[last] = self.types[toks[last]]
        # a mixed-case source name ("North Tryon Street") is already readable
        return raw, " ".join(out) if full.isupper() else " ".join(full.split())

    def record(self, p: dict, lon: float, lat: float, n: int) -> dict | None:
        if any(str(p.get(k)) != str(v) for k, v in self.filter.items()):
            return None
        hn = self._get(p, self.fields["housenumber"])
        raw, readable = self.street(p)
        if not hn or not raw:
            return None
        if re.fullmatch(r"\d+\.0", hn):
            hn = hn[:-2]
        pc = self._get(p, self.fields.get("postcode"))
        if pc and self.postcode_digits:
            pc = pc[: self.postcode_digits]
        return {
            "lon": lon, "lat": lat, "hn": hn, "street_raw": raw, "street_fallback": readable,
            "unit": self._get(p, self.fields.get("unit")), "postcode": pc,
            "city": nice(self._get(p, self.fields.get("city"))),
            "state": self._get(p, self.fields.get("state")),
            "id": self._get(p, self.fields.get("id")) or f"{lon:.7f},{lat:.7f},{n}",
        }

    # --- fetching

    def fetch(self, bbox, http) -> list[dict]:
        kind = self.fetch_cfg["type"]
        if kind == "arcgis":
            feats = self._fetch_arcgis(bbox, http)
        elif kind == "file":
            feats = self._fetch_file(bbox)
        else:
            raise SystemExit(f"{self.path}: unknown fetch type {kind!r}")
        out = []
        for n, (p, lon, lat) in enumerate(feats):
            r = self.record(p, lon, lat, n)
            if r:
                out.append(r)
        return out

    def _fetch_arcgis(self, bbox, http):
        url = self.fetch_cfg["url"].rstrip("/")
        if not url.endswith("/query"):
            url += "/query"
        w, s, e, n = bbox
        feats, offset = [], 0
        while True:
            params = {
                "where": self.fetch_cfg.get("where", "1=1"),
                "geometry": f"{w},{s},{e},{n}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326, "outSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": ",".join(self._source_fields()) or "*",
                "orderByFields": self.fetch_cfg.get("order_by", "OBJECTID"),
                "resultOffset": offset, "resultRecordCount": self.fetch_cfg.get("page_size", 2000),
                "f": "geojson",
            }
            page = json.loads(http(url + "?" + urllib.parse.urlencode(params)))
            if "error" in page:
                raise SystemExit(f"ArcGIS error from {url}: {page['error']}")
            got = page.get("features", [])
            feats += [(f["properties"], *f["geometry"]["coordinates"][:2]) for f in got if f.get("geometry")]
            if not got or not (page.get("exceededTransferLimit") or page.get("properties", {}).get("exceededTransferLimit")):
                break
            offset += len(got)
        return feats

    def _load_file(self):
        if self._file_cache is not None:
            return self._file_cache
        path = Path(self.fetch_cfg["path"])
        if not path.is_absolute():
            path = (self.path.parent / path) if (self.path.parent / path).exists() else ROOT / path
        rows = []
        if path.suffix.lower() == ".csv":
            lon_k, lat_k = self.fetch_cfg.get("lon", "lon"), self.fetch_cfg.get("lat", "lat")
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        rows.append((r, float(r[lon_k]), float(r[lat_k])))
                    except (KeyError, ValueError):
                        continue
        else:
            text = path.read_text()
            if text.lstrip().startswith("{") and '"FeatureCollection"' in text[:2000]:
                feats = json.loads(text)["features"]
            else:  # GeoJSON-lines (OpenAddresses)
                feats = [json.loads(line) for line in text.splitlines() if line.strip()]
            for f in feats:
                g = f.get("geometry")
                if g and g.get("type") == "Point":
                    rows.append((f.get("properties") or {}, *g["coordinates"][:2]))
        self._file_cache = rows
        return rows

    def _fetch_file(self, bbox):
        w, s, e, n = bbox
        return [(p, lon, lat) for p, lon, lat in self._load_file() if w <= lon <= e and s <= lat <= n]


def find_source(explicit: str | None = None, project_dir: Path | None = None) -> Source:
    """--source, else the project's config.json "source", else the only file in sources/."""
    if explicit:
        return Source(explicit)
    if project_dir and (project_dir / "config.json").exists():
        s = json.loads((project_dir / "config.json").read_text()).get("source")
        if s:
            return Source(s if Path(s).is_absolute() else ROOT / s)
    cands = [p for p in sorted((ROOT / "sources").glob("*.json")) if not p.name.endswith(".example.json")]
    if len(cands) == 1:
        return Source(cands[0])
    raise SystemExit("no address source: pass --source sources/<name>.json "
                     "(or set \"source\" in the project's config.json)")
