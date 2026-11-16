# OSM address import

Tools for importing an authoritative address dataset (a county's or city's
address points) into OpenStreetMap, following the
[import guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines):
addresses are put on the buildings they fall in, matched against what OSM already
has, cut into small blocks, reviewed in a local web app and uploaded by you from
JOSM. Nothing is ever uploaded automatically.

It started as the Mecklenburg County, NC import (`sources/mecklenburg-nc.json`)
and works with any dataset that can be described by a source file.

## Address sources

A source file in `sources/` (this one is an illustration) says where the points come from and how their fields
map onto `addr:*` tags:

```json
{
  "name": "Wake County Address Points",
  "short_name": "county",
  "changeset": {"source": "Wake County GIS", "comment": "Add addresses from Wake County address points"},
  "area": {"relation": 1234567, "center": [-78.6382, 35.7796], "center_name": "Downtown"},
  "fetch": {"type": "arcgis", "url": "https://.../FeatureServer/0", "where": "STATUS = 'ACTIVE'"},
  "fields": {
    "id": "OBJECTID", "housenumber": "ADDR_NUM", "street": "FULL_STREET", "unit": "UNIT",
    "postcode": "ZIP", "city": "CITY", "state": {"value": "NC"}
  },
  "postcode_digits": 5,
  "street_types": {"AV": "Avenue"}
}
```

- `fetch.type`: `arcgis` (an ArcGIS REST FeatureServer/MapServer layer, queried
  per block) or `file` (a local GeoJSON, GeoJSON-lines such as an
  [OpenAddresses](https://batch.openaddresses.io/) download, or a CSV with
  `"lon"`/`"lat"` column names). See `sources/openaddresses.example.json`.
- `fields`: source field names, or `{"value": ...}` for a constant. `street` is
  one field with the full name, or `{"prefix", "name", "type", "suffix"}` fields
  that get joined. Only `housenumber` and `street` are required; `id` should be
  a stable record id (review decisions are keyed on it). `filter`
  (`{"field": "value"}`) keeps only matching records.
- `short_name` is how the UI refers to the data ("the county point").
- `area.relation` is the OSM boundary relation of the import area, used to cut
  blocks; `center` is where block numbering starts (default: inside the
  boundary).
- `street_types` adds the source's own street type abbreviations to the USPS
  ones, so `CR` matches `Circle`. Street names written to OSM always use the
  spelling of a nearby OSM street; the expanded name is only a fallback.

Start an import with:

    ./blocks.py init sources/your-source.json   # records the source, fetches roads, builds blocks
    ./webapp/server.py                          # http://127.0.0.1:8765

Each import lives in a project directory (`blocks/` by default). Use
`--dir imports/raleigh` on both `blocks.py` and `webapp/server.py` to run
several side by side.

## Conflation (one area)

    ./conflate.py --source sources/mecklenburg-nc.json --bbox=-80.87,35.43,-80.85,35.445 --out out/huntersville --cache

(`--bbox=` with `=` because the value starts with `-`. Requires `uv`.)

Outputs, nothing is uploaded:
- `out/X.osm`: open in JOSM. Modified buildings plus new address nodes.
- `out/X_review.geojson`: open as a layer in JOSM, iD or Rapid. Cases the tool refused to decide.
- `out/X_summary.txt`: counts, unmatched street names, OSM spelling suggestions.

Rules:
- Only records the source's `where` / `filter` selects are used.
- Street names use the spelling of a nearby OSM highway with the same normalized
  name. If none matches, the point goes to review. Override with `--street-overrides file.csv`.
- If the same number and street already exist in OSM within 150 m, the point is skipped.
- 1 address inside a building with no address: tags are added to the building.
- Units: many sources stack most units of a complex on one point. Those collapse to
  the plain street number. A unit is kept (`addr:unit`) only when it has its own
  point. Then a building holding just that unit gets the unit on the building, and a
  building holding several such units gets one node per unit.
- Several different addresses inside one building (townhome row drawn as one
  outline, duplex): one node each. `--multi review` sends them to review instead.
- Townhomes drawn as individual attached ways: each unit normally gets its one
  point. If one unit holds 2+ numbers while an attached neighbour (shares a wall)
  got none, the points are probably offset: review.
- Building already has a different address, is a garage/shed/etc., or one
  address falls in several buildings: review.
- Multipolygon buildings (relations) are handled like outlines: tagged, or their
  address corrected. The relation is written with its member list but not the
  member ways; JOSM loads those as incomplete members and uploads only the tag
  change. Data downloaded before relations were fetched with metadata still sends
  them to review; regenerate with fresh data to fix that.
- Address outside every building: skipped. Use `--unmatched nodes|review` to keep
  it, or `--snap 5` to attach it to a building within 5 m (flagged).

## Block-by-block import UI

    ./blocks.py init sources/X.json   # once: fetch roads and place names, cut the area into blocks
    ./webapp/server.py         # http://127.0.0.1:8765

- The map shows every block, coloured by status. Click a block or a list row to
  see its changes (green = address added to a building, orange = new node,
  red = review). Press `c` to toggle the changes over the OSM map (before/after)
  and `n` to jump to the next ready block.
- "Generate next N" queues pending blocks in order outward from the area centre. Jobs run
  one at a time so Overpass isn't hammered.
- "Regenerate (fresh data)" re-downloads OSM for that block. Do this right before
  uploading. Blocks generated more than 24 h ago get a warning.
- "Open in JOSM" loads the changeset as a new layer and zooms to the block.
  Needs JOSM with Preferences → Remote Control enabled, including "Import data
  from URL". "Review layer → JOSM" loads the review points.
- After uploading from JOSM, use "Mark uploaded…" (paste the changeset id) and
  the UI moves to the next ready block.
- ⚙ settings: import wiki URL (for the `url=` changeset tag) and extra
  conflate.py flags.

### Reviewing a block

The block panel has three tabs. **Changes** groups everything the changeset
does (number & street fixes, shops & offices, postcode/city/state fixes, new
points, buildings), with search and a To review / Approved / Rejected / Edited
filter. **Needs review** lists what was held back, grouped by reason. **Info**
has the summary, before/after render and changeset tags.

Click a row or a feature on the map to open it in the inspector:

| key | |
|---|---|
| `a` / `r` / `u` | approve / reject / undo (review points: `r` leaves it as is, `u` removes its fix), then go to the next unreviewed item |
| `1`–`9` | review points: apply that suggested fix |
| `j` `k` (↓ ↑) | next / previous |
| `e` | edit tags; `⌘↵` saves |
| `⌘Z` / `⇧⌘Z` (Ctrl+Z / Ctrl+Y) | undo / redo any decision, bulk approve, tag edit, move or block status change |
| `/` | search |
| `Esc` | back to the list, then close the block |

The tag table compares the tags that will be uploaded with what OSM has now
(added / changed / removed), and every value can be edited. An edit that would
give the object an address that already exists anywhere in the downloaded area
is refused. New points can be dragged on the map (up to 300 m from the source
point; "Put back" undoes it); existing OSM objects never move. Decisions, edits
and moves live in `blocks/out/NNNN/decisions.json`, `edits.json` and
`moves.json` and survive regeneration. Rejected changes are left out of the
`.osm` sent to JOSM or downloaded; unreviewed ones are included. Links like
`http://127.0.0.1:8765/#67` open a block.

### Fixing review points

Every review point is fixed in the app; nothing has to be done in JOSM. Opening
one explains the problem, outlines the OSM objects involved on the map, and offers
one-click fixes (e.g. for an offset address: "this building is 2313", "this
building has both 2309 and 2313", "keep 2309, add 2313 as a point"). **Edit by
hand** edits the tags of any involved object and adds address points (placed by
clicking the map). A saved fix goes into `blocks/out/NNNN/fixes.json`, marks the
point resolved, and is written into the exported changeset: objects get
`action="modify"` with the current version (from the download cache, or the OSM
API when the cache lacks it), ways with their nodes, and new points as new
nodes. Fixes are checked for duplicate addresses like every other change. A
point marked "leave as is" keeps its fix on disk but doesn't use it.

### Wrong building

A tagged building whose address belongs on another outline (a parking deck
inside an apartment complex, say): open it, **Pick another building** (`m`),
click the right outline on the map and choose it. The original outline is left
as it is in OSM and the address goes on the picked building (way or
multipolygon) instead; stored as a fix with decision `moved`.

### Shops and offices (POI merging)

In strip malls and office buildings the source often has one point per storefront,
and OSM often already has the businesses as unaddressed POI nodes. Instead of
adding an address node next to each POI, conflate.py puts the address on the
POI when the pair is clear-cut: within 25 m, each is the other's nearest, and
no other source point is nearly as close (`--merge-pois auto`, the default).
Ambiguous ones are added as nodes, and the UI lists the nearby POIs in the
node's popup ("Merge into a POI in this building"), so you can pick the right
one. Rejecting an automatic merge leaves the POI alone and gives the address
its own node. `--merge-pois suggest` makes every merge manual; `off` ignores
POIs.

### Uploads

Upload from JOSM as usual; "Open in JOSM" pre-fills the changeset tags
(comment, `source`, `import=yes`, `url`). With your OSM username set in
Settings, the server checks your changesets on start-up and every 10 minutes
(or ⋯ → Check OSM for uploads): a changeset that contains at least 60% of a
ready block's changes marks that block uploaded and links it.
