"use strict";

// the import this UI is for (served by /api/project.js): dataset names and map centre
const PROJECT = window.PROJECT || { short_name: "source" };
const SRC = PROJECT.short_name || "source";               // "the county point", "the city point", ...
const SRCC = SRC[0].toUpperCase() + SRC.slice(1);

const STATUS = ["pending", "ready", "nothing_to_do", "uploaded", "skipped", "error"];
const LABEL = { pending: "Pending", ready: "Ready", nothing_to_do: "Nothing to do", uploaded: "Uploaded", skipped: "Skipped", error: "Error" };
const COLOR = { pending: "#c9ced6", ready: "#f59e0b", nothing_to_do: "#60a5fa", uploaded: "#16a34a", skipped: "#4ade80", error: "#e03e3e" };
// how strongly each status fills its block on the map: done work reads as solid, open work as a tint
const FILL = { pending: 0.06, ready: 0.28, nothing_to_do: 0.2, uploaded: 0.45, skipped: 0.35, error: 0.3 };
const REASON = {
  street_not_found_in_osm: "Street not found in OSM",
  building_has_different_address: "Building has a different address",
  address_spans_multiple_buildings: "Same address in several buildings",
  multipolygon_building: "Multipolygon building",
  minor_building_type: "Garage/shed type building",
  attached_building_points_misaligned: "Attached row: points look offset",
  multiple_addresses_in_building: "Several addresses in one building",
  snapped_to_nearby_building: "Snapped to nearby building",
  no_building: "No building",
  building_address_ambiguous: `Building address doesn't match any ${SRC} address inside`,
  osm_address_offset_from_county: `OSM number belongs to a neighbouring ${SRC} point (offset?)`,
  would_duplicate_existing: "Would duplicate an existing OSM address",
  would_duplicate_in_changeset: "Would be added twice in this changeset",
  pending_in_other_block: "Already proposed by another block",
  street_differs_and_not_in_osm: `OSM street differs; ${SRC} street not in OSM`,
  multi_value_housenumber_differs: "Multi-value housenumber differs",
  relation_address_needs_update: "Relation address needs update",
  osm_address_not_in_county: `OSM address not in ${SRC} data`,
};
const STALE_HOURS = 24;
const JOSM = "http://127.0.0.1:8111";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
};

let blocks = [];          // features
let byId = new Map();
let selected = null;      // block id
let jobs = { current: null, queue: [], log: [], done: [] };
let filters = new Set(); // empty = all
let lastDone = 0;

// --- map --------------------------------------------------------------------------

const CENTER = PROJECT.center ? [PROJECT.center[1], PROJECT.center[0]] : null;   // [lat, lon]
const map = L.map("map", { zoomControl: false, preferCanvas: true }).setView(CENTER || [20, 0], CENTER ? 13 : 2);
document.title = PROJECT.name ? `${PROJECT.name} import` : "Address import";
document.querySelector(".brand .subtitle").textContent = `${PROJECT.name || "Address data"} → OpenStreetMap`;
L.control.zoom({ position: "bottomright" }).addTo(map);
const BASEMAPS = {
  // standard OSM tiles, desaturated so the coloured changes stand out
  light: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20, maxNativeZoom: 19, className: "tiles-muted", attribution: "© OpenStreetMap contributors" }),
  osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20, maxNativeZoom: 19, attribution: "© OpenStreetMap contributors" }),
  // Esri imagery (cleared for OSM tracing) with street names on top
  sat: L.layerGroup([
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 20, maxNativeZoom: 19, opacity: 0.9 }),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 20, maxNativeZoom: 19 }),
  ]),
};
let basemap = BASEMAPS[localStorage.basemap] ? localStorage.basemap : "light";
let lastMap = basemap === "sat" ? (BASEMAPS[localStorage.lastMap] && localStorage.lastMap !== "sat" ? localStorage.lastMap : "light") : basemap;
BASEMAPS[basemap].addTo(map);

function setBasemap(name) {
  if (name === basemap || !BASEMAPS[name]) return;
  BASEMAPS[basemap].remove();
  basemap = localStorage.basemap = name;
  if (name !== "sat") lastMap = localStorage.lastMap = name;
  BASEMAPS[name].addTo(map);
  const rg = document.getElementById("basemap");
  if (rg.value !== name) rg.value = name;
  renderSatToggle();
}
/** Satellite on/off, back to whichever map was used before. Key: s */
const toggleSatellite = () => setBasemap(basemap === "sat" ? lastMap : "sat");

// a Google-Maps-style switch in the map corner: shows what you'd switch to
const SatToggle = L.Control.extend({
  options: { position: "bottomleft" },
  onAdd() {
    const b = L.DomUtil.create("button", "sat-toggle");
    b.type = "button";
    L.DomEvent.disableClickPropagation(b);
    b.onclick = toggleSatellite;
    return b;
  },
});
const satToggle = new SatToggle().addTo(map);
function renderSatToggle() {
  const b = satToggle.getContainer();
  const toSat = basemap !== "sat";
  // preview tile of the target layer around the area centre
  const c = CENTER || [map.getCenter().lat, map.getCenter().lng];
  const tx = Math.floor((c[1] + 180) / 360 * 2 ** 15);
  const ty = Math.floor((1 - Math.log(Math.tan(c[0] * Math.PI / 180) + 1 / Math.cos(c[0] * Math.PI / 180)) / Math.PI) / 2 * 2 ** 15);
  b.style.backgroundImage = toSat
    ? `url(https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/15/${ty}/${tx})`
    : `url(https://tile.openstreetmap.org/15/${tx}/${ty}.png)`;
  b.innerHTML = `<span>${toSat ? "Satellite" : "Map"}</span>`;
  b.title = `${toSat ? "Satellite view" : "Back to the map"} (s)`;
}
renderSatToggle();
document.getElementById("basemap").setAttribute("value", basemap);
document.getElementById("basemap").addEventListener("sl-change", (e) => setBasemap(e.target.value));
map.createPane("blocks").style.zIndex = 390;
map.createPane("changes").style.zIndex = 450;

const blockLayer = L.geoJSON(null, {
  pane: "blocks",
  style: (f) => blockStyle(f.properties),
  onEachFeature: (f, layer) => {
    layer.on("click", () => { if (!justClickedFeature() && f.properties.id !== selected) select(f.properties.id, false); });
    layer.bindTooltip(String(f.properties.id), { permanent: true, direction: "center", className: "block-label" });
  },
}).addTo(map);

function blockStyle(p) {
  const sel = p.id === selected;
  return {
    color: sel ? "#4f46e5" : "#5b6270", weight: sel ? 3 : 0.8, dashArray: sel ? "6 4" : null,
    fillColor: COLOR[p.status] || COLOR.pending, fillOpacity: sel ? 0.03 : FILL[p.status] ?? 0.1,
  };
}

function updateLabels() {
  const show = map.getZoom() >= 13 && $("#show-blocks").checked;
  blockLayer.eachLayer((l) => (show ? l.openTooltip() : l.closeTooltip()));
}
map.on("zoomend", updateLabels);

// --- change features on the map --------------------------------------------------------

map.createPane("highlight").style.zIndex = 460;

const GROUPS = [
  { key: "fix", label: "Number & street fixes", color: "#6b3fb3", hint: "Existing OSM addresses whose house number or street changes. Check these first." },
  { key: "poi", label: "Shops & offices", color: "#0b7f8a", hint: "Addresses put on existing business POIs, or new points next to POIs that need you to pick one." },
  { key: "fill", label: "Postcode, city & state", color: "#8e5cd9", hint: "Existing addresses that get a missing or wrong postcode, city or state." },
  { key: "node", label: "New address points", color: "#c46f12", hint: "Several addresses in one building, or individual units." },
  { key: "bldg", label: "Buildings tagged", color: "#2e7d4a", hint: `Address added to a building outline that had none and contains exactly one ${SRC} point. Confident: no review needed.` },
];
const GROUP = Object.fromEntries(GROUPS.map((g) => [g.key, g]));
const FILTERS = [["todo", "To do"], ["all", "All"], ["approved", "Approved"], ["rejected", "Rejected"], ["edited", "Edited"]];

function changeStyle(p) {
  const st = itemStatus(p);
  const off = st === "rejected" || st === "ignored" || st === "resolved" || st === "moved";
  if (p.kind === "tagged_building") {
    return off ? { color: "#888", weight: 1, dashArray: "3 3", fillColor: "#bbb", fillOpacity: 0.2 }
      : { color: st === "approved" ? "#14532d" : "#2e7d4a", weight: st === "approved" ? 2 : 1, dashArray: null, fillColor: "#4caf6e", fillOpacity: 0.6 };
  }
  const c = p.kind === "review" ? ["#8a1f1f", "#d64545", 6]
    : p.kind === "corrected" ? ["#4a2a7a", "#8e5cd9", 5]
    : p.kind === "poi_address" || st === "merged" ? ["#0b5560", "#22a6b3", 6]
    : ["#7a4510", "#e8902a", 4];
  if (off) return { radius: c[2] - 1, color: "#888", weight: 1, fillColor: "#ccc", fillOpacity: 0.6 };
  return { radius: c[2], color: st === "approved" ? "#111" : c[0], weight: st === "approved" ? 2.5 : 1.5, fillColor: c[1], fillOpacity: 0.95 };
}

// SVG (not canvas) for changes and the highlight ring: a canvas on top would
// swallow every click, even where it draws nothing, so the block outlines and
// earlier layers underneath could never be clicked.
const changeRenderer = L.svg({ pane: "changes" });
const highlightRenderer = L.svg({ pane: "highlight" });
const changeLayer = L.geoJSON(null, {
  pane: "changes",
  renderer: changeRenderer,
  style: (f) => changeStyle(f.properties),
  pointToLayer: (f, ll) => {
    const m = L.circleMarker(ll, { pane: "changes", renderer: changeRenderer, ...changeStyle(f.properties),
      className: f.properties.kind === "new_node" ? "draggable" : "" });
    if (f.properties.kind === "new_node") m.on("add", () => makeDraggable(m));
    return m;
  },
  onEachFeature: (f, layer) => layer.on("click", () => {
    if (P?.bpick || P?.picking != null) return;   // picking a location: the map click handler takes it
    featureClickAt = Date.now(); inspect(f.properties.cid, false);
  }),
});
const highlight = L.layerGroup().addTo(map);
// Canvas layers don't stop a click from also reaching the map and the block
// outline underneath, so those handlers ignore clicks that just hit a change.
let featureClickAt = 0;

// New points can be dragged to where the address really is (existing OSM
// objects never move). A native listener on the SVG element runs before
// Leaflet's map panning sees the mousedown, so it can stop it.
function makeDraggable(marker) {
  const el = marker.getElement();
  if (!el || el._dragWired) return;
  el._dragWired = true;
  el.addEventListener("mousedown", (down) => {
    if (down.button !== 0) return;
    down.stopPropagation();
    down.preventDefault();
    const start = [down.clientX, down.clientY];
    const from = marker.getLatLng();
    let moved = false;
    const move = (ev) => {
      if (!moved && Math.hypot(ev.clientX - start[0], ev.clientY - start[1]) < 3) return;
      if (!moved) { moved = true; map.getContainer().classList.add("dragging-point"); }
      marker.setLatLng(map.mouseEventToLatLng(ev));
      if (P?.open === marker.feature.properties.cid) showOnMap(P.items.find((i) => i.layer === marker), false);
    };
    const up = async () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      map.getContainer().classList.remove("dragging-point");
      if (!moved) return;   // a plain click: Leaflet's click handler opens it
      featureClickAt = Date.now();
      const it0 = P?.items.find((i) => i.layer === marker);
      const ok = await movePoint(marker.feature.properties.cid, marker.getLatLng(), false, it0?.p.moved ? from : null);
      if (!ok) marker.setLatLng(from);
      inspect(marker.feature.properties.cid, false);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

async function movePoint(cid, ll, silent = false, from = undefined) {
  if (!P) return false;
  const cur = P.items.find((i) => i.cid === cid);
  if (from === undefined) from = cur?.p.moved ? cur.layer.getLatLng() : null;
  const r = await fetch(`/api/blocks/${P.id}/move`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cid, lon: ll ? ll.lng : null, lat: ll ? ll.lat : null }) });
  const j = await r.json();
  if (!r.ok) { toast(`Couldn't move it: ${esc(j.error)}`, true); return false; }
  const it = P.items.find((i) => i.cid === cid);
  if (it) {
    it.p.moved = j.moved;
    const at = ll || L.latLng(it.p.orig[1], it.p.orig[0]);
    it.layer.setLatLng(at);
    it.layer.feature.geometry.coordinates = [at.lng, at.lat];
    refreshRow(cid);
    if (P.open === cid) { renderInspector(); showOnMap(it, false); }
  }
  if (!silent) {
    const id = P.id, to = ll;
    record({
      block: id, cid, label: `${to ? "Moved" : "Put back"} ${itemText(it.p).title}`,
      undo: () => movePoint(cid, from, true), redo: () => movePoint(cid, to, true),
    });
  }
  return true;
}
const justClickedFeature = () => Date.now() - featureClickAt < 300;

$("#show-changes").addEventListener("sl-change", (e) => (e.target.checked ? changeLayer.addTo(map) : changeLayer.remove()));
$("#show-blocks").addEventListener("sl-change", (e) => { e.target.checked ? blockLayer.addTo(map) : blockLayer.remove(); updateLabels(); });

// --- items ------------------------------------------------------------------------------

let P = null;   // state of the open block panel

const addrOf = (t) => t && t["addr:housenumber"]
  ? `${t["addr:housenumber"]} ${t["addr:street"] || ""}${t["addr:unit"] ? ` #${t["addr:unit"]}` : ""}`.trim() : "";
const finalTags = (p) => p.edit || p.tags || {};

// Changes the conflation is sure about don't need a human look: a building
// outline with no address that contains exactly one source point (anything
// less certain was held back as a review point instead).
const isConfident = (p) => p.kind === "tagged_building";

function itemStatus(p) {
  if (p.kind === "review") return p.decision || "open";
  if (p.decision?.startsWith?.("merge:")) return "merged";
  return p.decision || (isConfident(p) ? "auto" : "todo");
}

function groupOf(p) {
  if (p.kind === "corrected") return p.fields.some((f) => f === "housenumber" || f === "street") ? "fix" : "fill";
  if (p.kind === "poi_address" || p.poi_candidates?.length) return "poi";
  if (p.kind === "new_node") return "node";
  return "bldg";
}

function itemText(p) {
  if (p.kind === "review") {
    const sub = p.osm_address ? `OSM has ${p.osm_address}` : (p.osm_suggestion ? `OSM street nearby: ${p.osm_suggestion}` : "");
    const outcome = p.decision === "ignored" ? "skipped" : p.fix && p.decision === "resolved" ? "fix will upload" : p.decision ? "" : "undecided, will be skipped";
    return { title: p.county_address || p.osm_address || p.osm, sub: [outcome, sub].filter(Boolean).join(" · ") };
  }
  const t = finalTags(p);
  const title = addrOf(t) || p.osm;
  if (p.kind === "corrected") {
    const was = addrOf(p.before);
    return { title, sub: groupOf(p) === "fix" ? `was ${was}` : p.fields.join(", ") };
  }
  if (p.kind === "poi_address") return { title, sub: `on ${p.name}` };
  if (p.poi_candidates?.length) {
    const target = p.decision?.startsWith("merge:") && p.poi_candidates.find((c) => `merge:${c.osm}` === p.decision);
    return { title, sub: target ? `→ ${target.name}` : `near ${p.poi_candidates.map((c) => c.name).join(", ")}` };
  }
  if (p.kind === "tagged_building") return { title, sub: `building=${t.building || "yes"}` };
  return { title, sub: t["addr:unit"] ? "unit" : "" };
}

function buildItems(fc) {
  const items = [];
  changeLayer.clearLayers().addData(fc);
  const layers = new Map();
  changeLayer.eachLayer((l) => layers.set(l.feature.properties.cid, l));
  for (const f of fc.features) {
    const p = f.properties;
    if (!p.cid) continue;
    items.push({ cid: p.cid, p, layer: layers.get(p.cid), review: p.kind === "review", group: p.kind === "review" ? p.reason : groupOf(p) });
  }
  return items;
}

function matchesFilter(it) {
  const st = itemStatus(it.p);
  const f = it.review ? P.rfilter : P.filter;
  if (f === "todo" && !(st === "todo" || st === "open")) return false;
  if (f === "approved" && !["approved", "merged", "resolved", "auto", "moved"].includes(st)) return false;
  if (f === "rejected" && !["rejected", "ignored"].includes(st)) return false;
  if (f === "edited" && !it.p.edit && !it.p.moved) return false;
  if (P.query) {
    const { title, sub } = itemText(it.p);
    if (!`${title} ${sub} ${it.p.osm || ""} ${it.p.name || ""}`.toLowerCase().includes(P.query)) return false;
  }
  return true;
}

function groupKeys(review) {
  if (!review) return GROUPS.map((g) => g.key);
  return [...new Set(P.items.filter((i) => i.review).map((i) => i.group))].sort();
}

/** Items in list order for the current tab. Respects filter + search, unless the
 *  open item is itself filtered out (then every item of the tab, so ‹ › still work). */
function visibleOrder() {
  const review = P.tab === "review";
  const all = groupKeys(review).flatMap((g) => P.items.filter((i) => i.review === review && i.group === g));
  const vis = all.filter(matchesFilter);
  return !P.open || vis.some((i) => i.cid === P.open) ? vis : all;
}

// --- panel ------------------------------------------------------------------------------

async function renderDetail(id) {
  const [d, fc] = await Promise.all([api(`/api/blocks/${id}`), api(`/api/blocks/${id}/layers`)]);
  if (selected !== id) return;
  const p = d.feature.properties;
  byId.get(id).properties = p;
  const keep = P && P.id === id ? P : null;
  P = {
    id, d, p,
    items: buildItems(fc),
    tab: keep?.tab || "changes",
    filter: keep?.filter || "todo", rfilter: keep?.rfilter || "todo",
    query: keep?.query || "",
    collapsed: keep?.collapsed || new Set(["bldg"]),
    open: null,
  };
  if ($("#show-changes").checked) changeLayer.addTo(map);
  renderPanel();
  if (keep?.open && P.items.some((i) => i.cid === keep.open)) inspect(keep.open, false);
}

function renderPanel() {
  const { id, d, p } = P;
  const js = jobState(id);
  const s = d.stats || {};
  const ageH = p.generated ? (Date.now() - Date.parse(p.generated)) / 36e5 : null;
  const has = (n) => d.files.includes(n);
  const nChanges = P.items.filter((i) => !i.review).length;
  const nReview = P.items.filter((i) => i.review).length;
  const pending = p.status === "pending" && !has("changeset.osm");
  if (pending) P.tab = "info";

  $("#detail").classList.remove("hidden");
  const csLink = p.changeset_id ? `<a target="_blank" href="https://www.openstreetmap.org/changeset/${esc(p.changeset_id)}">changeset ${esc(p.changeset_id)}</a>` : "";
  const menuItem = (v, label, icon, on = true, cls = "") => `<sl-menu-item value="${v}" ${on ? "" : "disabled"} class="${cls}"><sl-icon slot="prefix" name="${icon}"></sl-icon>${label}</sl-menu-item>`;
  $("#detail").innerHTML = `
  <div class="bp">
    <div class="bp-head">
      <div class="bp-top">
        <h2><span class="bp-id">${p.id}</span>${esc(p.name)}</h2>
        <span class="pill ${js || p.status}">${js || LABEL[p.status]}</span>
        <span class="spacer"></span>
        <sl-tooltip content="Close (Esc)"><sl-icon-button id="d-close" name="x-lg" label="Close"></sl-icon-button></sl-tooltip>
      </div>
      <div class="bp-sub" title="${esc(p.roads)} · ${p.area_km2} km² · ${p.dist_km} km from ${esc(PROJECT.center_name || "the centre")}">${esc(p.roads)}</div>
      <div class="bp-meta">${p.area_km2} km² · ${p.dist_km} km out${p.generated ? ` · generated ${timeAgo(p.generated)}` : ""}</div>
      ${p.status === "uploaded" ? `<sl-alert variant="success" open class="banner"><sl-icon slot="icon" name="cloud-check-fill"></sl-icon>
        <b>Uploaded</b>${p.uploaded ? ` ${new Date(p.uploaded).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : ""}${csLink ? ` in ${csLink}` : ""}
        ${p.uploaded_detected ? `<div class="banner-sub">Detected from your OSM changesets (${Math.round((p.upload_match || 1) * 100)}% of this block's changes).</div>`
          : p.changeset_id ? `<div class="banner-sub">Changeset found in your OSM history${p.upload_match ? ` (${Math.round(p.upload_match * 100)}% of this block's changes)` : ""}.</div>`
          : `<div class="banner-sub">Looking for its changeset in your OSM history; it's linked automatically when it shows up. <a href="#" id="a-relink">Check now</a></div>`}</sl-alert>` : ""}
      ${d.error ? `<sl-alert variant="danger" open class="banner"><sl-icon slot="icon" name="exclamation-octagon"></sl-icon><pre class="errtxt">${esc(d.error)}</pre></sl-alert>` : ""}
      ${p.status === "ready" && ageH > STALE_HOURS ? `<sl-alert variant="warning" open class="banner"><sl-icon slot="icon" name="hourglass-split"></sl-icon>Generated ${Math.round(ageH)} h ago. Regenerate with fresh data before uploading.</sl-alert>` : ""}
      <div class="bp-actions">
        ${pending
          ? `<sl-button variant="primary" class="grow" id="a-gen" ${js ? "disabled" : ""}><sl-icon slot="prefix" name="lightning-charge-fill"></sl-icon>Generate changeset</sl-button>`
          : `<sl-button variant="primary" class="grow" id="a-josm" ${has("changeset.osm") ? "" : "disabled"}><sl-icon slot="prefix" name="box-arrow-up-right"></sl-icon>Open in JOSM</sl-button>
             <sl-button id="a-uploaded" ${p.status === "ready" ? "" : "disabled"}><sl-icon slot="prefix" name="cloud-check"></sl-icon>Mark uploaded</sl-button>`}
        <sl-dropdown placement="bottom-end" hoist>
          <sl-button slot="trigger" id="a-menu" aria-label="More"><sl-icon name="three-dots"></sl-icon></sl-button>
          <sl-menu id="a-menu-list">
            ${pending ? "" : `${menuItem("regen", "Regenerate with fresh data", "arrow-repeat", !js)}
            ${menuItem("dl", "Download .osm", "download", has("changeset.osm"))}
            ${menuItem("josm-review", "Open review points in JOSM", "pin-map", nReview)}
            <sl-divider></sl-divider>`}
            ${menuItem("sync", "Check OSM for uploads", "cloud-arrow-down")}
            ${menuItem("skip", "Skip this block", "skip-forward", !["uploaded", "skipped"].includes(p.status))}
            ${["uploaded", "skipped", "nothing_to_do", "error"].includes(p.status) ? menuItem("reset", `Reset to ${has("changeset.osm") ? "ready" : "pending"}`, "arrow-counterclockwise", true, "danger") : ""}
          </sl-menu>
        </sl-dropdown>
      </div>
      ${pending ? "" : `<div id="progress-card"></div>`}
    </div>
    ${pending ? "" : `<nav class="tabs">
      <button data-tab="changes" class="${P.tab === "changes" ? "on" : ""}"><sl-icon name="list-check"></sl-icon>Changes <span class="count" id="tc-changes"></span></button>
      <button data-tab="review" class="${P.tab === "review" ? "on" : ""}"><sl-icon name="flag"></sl-icon>Needs review <span class="count" id="tc-review"></span></button>
      <button data-tab="info" class="${P.tab === "info" ? "on" : ""}"><sl-icon name="info-circle"></sl-icon>Info</button>
    </nav>`}
    <div class="bp-main">
      <div class="bp-body" id="bp-body"></div>
      <div class="insp hidden" id="insp"></div>
    </div>
  </div>`;

  $("#d-close").onclick = () => select(null);
  if ($("#a-gen")) $("#a-gen").onclick = () => generate([id], false);
  if ($("#a-josm")) $("#a-josm").onclick = async () => {
    const undecided = P.items.filter((i) => i.review && !i.p.decision);
    if (undecided.length) {
      const go = await ask({ title: `${undecided.length} review item${undecided.length > 1 ? "s are" : " is"} undecided`,
        body: `<p>Undecided review items are <b>skipped</b>: nothing is uploaded for them. Open the changeset anyway, or decide them first?</p>`,
        ok: "Open anyway" });
      if (!go) { P.tab = "review"; P.rfilter = "todo"; renderPanel(); inspect(undecided[0].cid); return; }
    }
    openInJosm(id, p);
  };
  if ($("#a-uploaded")) $("#a-uploaded").onclick = async () => {
    // the changeset id is looked up in the user's OSM history; no need to copy it from JOSM
    const btn = $("#a-uploaded");
    btn.loading = true;
    const r = await setStatus(id, "uploaded");
    if (r.linked) toast(`Block ${id} marked uploaded · changeset <a target="_blank" href="https://www.openstreetmap.org/changeset/${r.linked.changeset}">${r.linked.changeset}</a> (${Math.round(r.linked.share * 100)}% of its changes)`);
    else if (!r.user) toast(`Block ${id} marked uploaded. Set your OSM username in Settings to link its changeset.`);
    else { toast(`Block ${id} marked uploaded. Its changeset isn't in ${esc(r.user)}'s OSM history yet; it's linked automatically when it shows up.`); retryLink(id); }
    await refreshAfterChange(id);
  };
  if ($("#a-relink")) $("#a-relink").onclick = (e) => { e.preventDefault(); syncUploads(true); };
  $("#a-menu-list").addEventListener("sl-select", async (e) => {
    const v = e.detail.item.value;
    if (v === "regen") generate([id], true);
    if (v === "dl") location.href = `/api/blocks/${id}/file/changeset.osm?download=1`;
    if (v === "josm-review") josmImport(`/api/blocks/${id}/file/changeset_review.geojson`, `Block ${id} review`);
    if (v === "sync") syncUploads(true);
    if (v === "skip") { await setStatus(id, "skipped"); refreshAfterChange(id); }
    if (v === "reset") { await setStatus(id, has("changeset.osm") ? "ready" : "pending"); refreshAfterChange(id); }
  });
  document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => { P.tab = b.dataset.tab; closeInspector(); renderPanel(); });

  renderReviewProgress();
  renderBody();
}

function timeAgo(iso) {
  const m = (Date.now() - Date.parse(iso)) / 6e4;
  if (m < 1) return "just now";
  if (m < 60) return `${Math.round(m)} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function renderReviewProgress() {
  const el = $("#progress-card");
  if (!el) return;
  const ch = P.items.filter((i) => !i.review);
  const c = { approved: 0, auto: 0, merged: 0, rejected: 0, todo: 0, moved: 0 };
  ch.forEach((i) => c[itemStatus(i.p)]++);
  const edited = ch.filter((i) => i.p.edit).length;
  const rv = P.items.filter((i) => i.review);
  const open = rv.filter((i) => itemStatus(i.p) === "open").length;
  const n = ch.length || 1;
  const seg = (k, col) => c[k] ? `<div style="width:${(100 * c[k]) / n}%;background:${col}" title="${c[k]} ${k}"></div>` : "";
  const included = ch.length - c.rejected;
  const stat = (n, label, cls = "") => n ? `<span class="pc-stat ${cls}"><b>${n.toLocaleString()}</b> ${label}</span>` : "";
  el.innerHTML = `
    <div class="pc-row">
      <div class="pc-main">
        <div class="pc-line">
          ${stat(c.todo, "to review", "todo")}${stat(c.approved + c.merged, "approved", "ok")}${stat(c.auto, "confident", "auto")}${stat(c.rejected, "rejected", "bad")}${stat(open, "review undecided", "bad")}
          ${!c.todo && !open ? `<span class="green"><sl-icon name="check-circle-fill"></sl-icon> All decided</span>` : ""}
        </div>
        <div class="pc-bar" title="${included.toLocaleString()} changes ${P.p.status === "uploaded" ? "uploaded" : "in the changeset"}${edited ? `, ${edited} edited` : ""}">${seg("auto", "#b7e4c7")}${seg("approved", "#2f9e5b")}${seg("merged", "#1597a6")}${seg("rejected", "#e03e3e")}</div>
      </div>
      ${c.todo ? `<sl-button size="small" variant="primary" outline id="pc-start">Next<sl-icon slot="suffix" name="arrow-right"></sl-icon></sl-button>` : ""}
    </div>`;
  if ($("#pc-start")) $("#pc-start").onclick = () => {
    P.tab = "changes";
    if (P.filter !== "todo") P.filter = "todo";
    const first = GROUPS.map((g) => P.items.find((i) => !i.review && i.group === g.key && itemStatus(i.p) === "todo")).find(Boolean);
    if (first) { P.collapsed.delete(first.group); renderPanel(); inspect(first.cid); }
  };
  // tab badges count what's left to decide; a tick when nothing is
  const badge = (el, left, total, warn) => {
    if (!el) return;
    el.className = `count ${left ? (warn ? "warn" : "") : "done"}`;
    el.innerHTML = left ? String(left) : total ? `<sl-icon name="check-lg"></sl-icon>` : "0";
    el.title = left ? `${left} of ${total} still to decide` : `${total} total, all decided`;
  };
  badge($("#tc-changes"), c.todo, ch.length, false);
  badge($("#tc-review"), open, rv.length, true);
  const bp = byId.get(P.id)?.properties;
  if (bp && (bp.rejected !== c.rejected || bp.review_open !== open)) { bp.rejected = c.rejected; bp.review_open = open; renderList(); }
}

function renderBody() {
  const body = $("#bp-body");
  if (P.tab === "info") return renderInfo(body);
  const review = P.tab === "review";
  const items = P.items.filter((i) => i.review === review);
  const f = review ? P.rfilter : P.filter;
  const filters = review ? [["todo", "Undecided"], ["all", "All"], ["approved", "Fixed"], ["rejected", "Skipped"]] : FILTERS;
  const countFor = (k) => {
    const save = review ? P.rfilter : P.filter;
    review ? (P.rfilter = k) : (P.filter = k);
    const q = P.query; P.query = "";
    const n = items.filter(matchesFilter).length;
    P.query = q; review ? (P.rfilter = save) : (P.filter = save);
    return n;
  };
  if (!items.length) {
    body.innerHTML = `<div class="empty"><sl-icon name="${review ? "emoji-smile" : "inbox"}"></sl-icon>${review ? "Nothing needs review in this block." : "This block has no changes."}</div>`;
    return;
  }
  body.innerHTML = `
    <div class="toolbar">
      <sl-input id="bp-q" size="small" placeholder="Search address, street, POI…  /" value="${esc(P.query)}" clearable><sl-icon slot="prefix" name="search"></sl-icon></sl-input>
      <sl-radio-group id="bp-f" size="small" value="${f}">${filters.map(([k, l]) =>
        `<sl-radio-button value="${k}">${l}<span class="rc">${countFor(k)}</span></sl-radio-button>`).join("")}</sl-radio-group>
    </div>
    ${review ? `<div class="hint"><sl-icon name="info-circle"></sl-icon><span>These were held back from the changeset. Open one to see what the problem is, then pick a fix (<kbd>1</kbd>–<kbd>9</kbd>) or edit it by hand. Or leave it as is (<kbd>r</kbd>).</span></div>` : ""}
    <div id="groups"></div>`;
  const q = $("#bp-q");
  q.addEventListener("sl-input", () => { P.query = q.value.trim().toLowerCase(); renderGroups(); });
  $("#bp-f").addEventListener("sl-change", (e) => {
    review ? (P.rfilter = e.target.value) : (P.filter = e.target.value);
    renderBody();
  });
  renderGroups();
  const tb = body.querySelector(".toolbar");
  new ResizeObserver(() => body.style.setProperty("--tb-h", `${tb.offsetHeight}px`)).observe(tb);
}

function renderGroups() {
  const review = P.tab === "review";
  const html = [];
  for (const g of groupKeys(review)) {
    const all = P.items.filter((i) => i.review === review && i.group === g);
    if (!all.length) continue;
    const shown = all.filter(matchesFilter);
    const done = all.filter((i) => !["todo", "open"].includes(itemStatus(i.p))).length;
    const confident = !review && all.every((i) => isConfident(i.p));
    const meta = review ? { label: REASON[g] || g, color: "#b03030" } : GROUP[g];
    const collapsed = P.collapsed.has(g);
    const empty = !shown.length;
    const todo = all.filter((i) => itemStatus(i.p) === "todo").length;
    html.push(`<section class="grp ${collapsed ? "collapsed" : ""} ${empty ? "empty-grp" : ""}" data-g="${esc(g)}">
      <header class="grp-h" title="${esc(meta.hint || "")}">
        <sl-icon class="chev" name="chevron-right"></sl-icon>
        <span class="grp-dot" style="background:${meta.color}"></span>
        <span class="grp-name">${esc(meta.label)}</span>
        <span class="grp-count">${shown.length === all.length ? all.length : !shown.length && done === all.length ? `${all.length} · ${confident ? "confident, no review needed" : "all reviewed"}` : `${shown.length} of ${all.length}`}</span>
        <span class="grp-prog"><i style="width:${(100 * done) / all.length}%"></i></span>
        ${!review && todo ? `<button class="mini grp-approve" title="Approve all ${todo} unreviewed in this group"><sl-icon name="check2-all"></sl-icon>${todo}</button>` : ""}
      </header>
      ${collapsed || !shown.length ? "" : `<div class="grp-rows">${shown.map(rowHtml).join("")}</div>`}
    </section>`);
  }
  $("#groups").innerHTML = html.join("") || `<div class="empty"><sl-icon name="search"></sl-icon>Nothing matches.</div>`;
  document.querySelectorAll(".grp-h").forEach((h) => h.onclick = (e) => {
    const g = h.parentElement.dataset.g;
    if (e.target.closest(".grp-approve")) {
      const cids = P.items.filter((i) => !i.review && i.group === g && itemStatus(i.p) === "todo").map((i) => i.cid);
      if (cids.length <= 20) return decide(cids, "approved");
      return ask({ title: `Approve ${cids.length} changes?`, body: `<p>Every unreviewed change in <b>${esc(GROUP[g].label)}</b> will be approved. You can undo this with ⌘Z.</p>`, ok: "Approve all" })
        .then((y) => y && decide(cids, "approved"));
    }
    P.collapsed.has(g) ? P.collapsed.delete(g) : P.collapsed.add(g);
    renderGroups();
  });
  document.querySelectorAll(".it").forEach((r) => r.onclick = () => inspect(r.dataset.cid));
}

const ST_ICON = { todo: "", open: "", approved: "✓", auto: "✓", merged: "→", moved: "→", rejected: "✕", resolved: "✓", ignored: "–" };

function rowHtml(it) {
  const { title, sub } = itemText(it.p);
  const st = itemStatus(it.p);
  return `<div class="it st-${st} ${P.open === it.cid ? "sel" : ""}" data-cid="${esc(it.cid)}">
    <span class="it-st">${ST_ICON[st]}</span>
    <span class="it-main"><span class="it-t">${esc(title)}</span>${sub ? `<span class="it-s">${esc(sub)}</span>` : ""}</span>
    ${it.p.moved ? `<span class="it-flag" title="Moved">moved</span>` : ""}
    ${it.p.edit ? `<span class="it-flag" title="Tags edited">edited</span>` : ""}
  </div>`;
}

function refreshRow(cid) {
  const it = P.items.find((i) => i.cid === cid);
  const el = document.querySelector(`.it[data-cid="${CSS.escape(cid)}"]`);
  if (it && el) el.outerHTML = rowHtml(it);
  document.querySelectorAll(`.it[data-cid="${CSS.escape(cid)}"]`).forEach((r) => r.onclick = () => inspect(cid));
}

function renderInfo(body) {
  const { id, d, p } = P;
  const s = d.stats || {};
  const has = (n) => d.files.includes(n);
  const rows = [
    ["Buildings tagged", (s.building_tagged || 0) + (s.building_tagged_with_unit || 0)],
    ["Address points in buildings with several addresses", s.multi_address_nodes],
    ["Unit points", s.unit_nodes], ["Standalone points", s.standalone_nodes],
    ["Addresses put on shop/office POIs", s.poi_address_merged],
    ["Existing addresses corrected", s.objects_corrected],
    ["Duplicates prevented", s.duplicates_prevented],
    [`${SRCC} points already in OSM`, s.already_in_osm],
    [`${SRCC} points outside any building (skipped)`, s.skipped_no_building],
    [`OSM addresses not in ${SRC} data (left alone)`, s.osm_address_not_in_county],
    [`${SRCC} address points in block`, s.county_points],
  ].filter(([, v]) => v);
  const tagsText = Object.entries(d.changeset_tags).map(([k, v]) => `${k}=${v}`).join("\n");
  body.innerHTML = `<div class="info">
    ${p.status === "pending" && !has("changeset.osm") ? `<div class="hint"><sl-icon name="info-circle"></sl-icon><span>Not generated yet. Generating downloads the ${SRC} points and OSM data for this block and builds a changeset for you to review. Nothing is uploaded.</span></div>` : ""}
    ${has("changeset_compare.png") ? `<h4>Before / after</h4><img class="thumb" id="a-render" src="/api/blocks/${id}/file/changeset_compare.png?t=${Date.parse(p.generated) || 0}">` : ""}
    ${rows.length ? `<h4>Summary</h4><table class="kv">${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v.toLocaleString()}</td></tr>`).join("")}</table>` : ""}
    ${tagsText ? `<h4>Changeset tags</h4><div class="tags">${esc(tagsText)}<sl-copy-button value="${esc(tagsText)}" copy-label="Copy tags"></sl-copy-button></div>` : ""}
    ${d.unmatched_streets.length ? `<h4>${SRCC} streets not found in OSM</h4><div class="hint"><sl-icon name="info-circle"></sl-icon><span>Addresses on these streets were sent to review. If OSM has the right name, add it to a street overrides file.</span></div>
      <table class="kv">${d.unmatched_streets.map((u) => `<tr><td>${esc(u.source)}${u.suggestion ? `<div class="muted">OSM nearby: ${esc(u.suggestion)}</div>` : ""}</td><td>${u.count}</td></tr>`).join("")}</table>` : ""}
  </div>`;
  if ($("#a-render")) $("#a-render").onclick = () => { $("#render-img").src = $("#a-render").src; $("#render-dlg").show(); };
}

// --- inspector ---------------------------------------------------------------------------

function inspect(cid, pan = true) {
  if (!P) return;
  const it = P.items.find((i) => i.cid === cid);
  if (!it) return;
  const wantTab = it.review ? "review" : "changes";
  if (P.tab !== wantTab) { P.tab = wantTab; renderPanel(); }
  P.collapsed.delete(it.group);
  if (!document.querySelector(`.it[data-cid="${CSS.escape(cid)}"]`)) renderGroups();
  P.open = cid;
  history.replaceState(null, "", `#${P.id}/${encodeURIComponent(cid)}`);
  P.work = { ...finalTags(it.p) };
  P.dirty = false;
  document.querySelectorAll(".it.sel").forEach((r) => r.classList.remove("sel"));
  const row = document.querySelector(`.it[data-cid="${CSS.escape(cid)}"]`);
  row?.classList.add("sel");
  renderInspector();
  showOnMap(it, pan);
}

function closeInspector() {
  if (!P) return;
  P.open = null;
  history.replaceState(null, "", `#${P.id}`);
  $("#insp")?.classList.add("hidden");
  highlight.clearLayers();
  fixLayer.clearLayers();
  P.picking = null;
  document.querySelectorAll(".it.sel").forEach((r) => r.classList.remove("sel"));
}

function showOnMap(it, pan) {
  highlight.clearLayers();
  drawFix(it);
  if (!it.layer) return;
  if (!$("#show-changes").checked) { $("#show-changes").checked = true; changeLayer.addTo(map); }
  const ll = it.layer.getLatLng ? it.layer.getLatLng() : it.layer.getBounds().getCenter();
  if (it.layer.getLatLng) {
    L.circleMarker(ll, { pane: "highlight", renderer: highlightRenderer, radius: 13, color: "#4f46e5", weight: 3, fill: false, interactive: false }).addTo(highlight);
  } else {
    L.geoJSON(it.layer.feature, { pane: "highlight", renderer: highlightRenderer, interactive: false, style: { color: "#4f46e5", weight: 3, fill: false } }).addTo(highlight);
  }
  // offset reviews: show where the source puts the number OSM has on this building
  const p = it.p;
  if (p.osm_number_county_at) {
    const other = L.latLng(p.osm_number_county_at[1], p.osm_number_county_at[0]);
    const lbl = (txt, cls) => ({ permanent: true, direction: "top", offset: [0, -8], className: `map-tag ${cls}` });
    L.polyline([ll, other], { pane: "highlight", renderer: highlightRenderer, color: "#4f46e5", weight: 2, dashArray: "5 5", interactive: false }).addTo(highlight);
    L.circleMarker(other, { pane: "highlight", renderer: highlightRenderer, radius: 7, color: "#4f46e5", weight: 2, fillColor: "#fff", fillOpacity: 1, interactive: false })
      .bindTooltip(`${SRCC} ${esc((p.osm_address || "").split(" ")[0])}`, lbl("", "other")).addTo(highlight);
    L.circleMarker(ll, { pane: "highlight", renderer: highlightRenderer, radius: 0, opacity: 0, interactive: false })
      .bindTooltip(`${SRCC} ${esc((p.county_address || "").split(" ")[0])} · OSM ${esc((p.osm_address || "").split(" ")[0])}`, lbl("", "here")).addTo(highlight);
    if (pan) map.fitBounds(L.latLngBounds([ll, other]).pad(1.2), { maxZoom: 19, animate: false });
    return;
  }
  const sib = it.review ? siblings(it) : [];
  if (pan && sib.length) {
    map.fitBounds(L.latLngBounds([ll, ...sib.map((x) => x.layer.getLatLng())]).pad(0.4), { maxZoom: 19, animate: false });
    return;
  }
  if (pan) {
    if (map.getZoom() < 18) map.setView(ll, 19, { animate: false });
    else if (!map.getBounds().pad(-0.15).contains(ll)) map.panTo(ll);
  }
}

function renderInspector() {
  const it = P.items.find((i) => i.cid === P.open);
  const el = $("#insp");
  if (!it) return el.classList.add("hidden");
  const p = it.p;
  const order = visibleOrder();
  const idx = order.findIndex((i) => i.cid === it.cid);
  const st = itemStatus(p);
  const meta = it.review ? { label: REASON[p.reason] || p.reason, color: "#b03030" } : GROUP[it.group];
  const { title } = itemText(p);
  const josmRef = (p.osm || "").split(",")[0];

  el.classList.remove("hidden");
  el.innerHTML = `
    <div class="insp-nav">
      <sl-button size="small" variant="text" id="i-back" title="Back to list (Esc)"><sl-icon slot="prefix" name="arrow-left"></sl-icon>${it.review ? "Review list" : "All changes"}</sl-button>
      <span class="insp-count">${idx >= 0 ? `${idx + 1} <span>of</span> ${order.length}` : ""}</span>
      <span class="insp-arrows">
        <sl-tooltip content="Previous (k)"><sl-icon-button id="i-prev" name="chevron-up" label="Previous" ${idx <= 0 ? "disabled" : ""}></sl-icon-button></sl-tooltip>
        <sl-tooltip content="Next (j)"><sl-icon-button id="i-next" name="chevron-down" label="Next" ${idx < 0 || idx >= order.length - 1 ? "disabled" : ""}></sl-icon-button></sl-tooltip>
      </span>
    </div>
    <div class="insp-body">
      <div class="insp-kind"><span class="grp-dot" style="background:${meta.color}"></span>${esc(meta.label)}</div>
      <h3 class="insp-title">${esc(title)}</h3>
      <div class="insp-links">
        ${p.osm ? `<span class="osm-ref"><sl-icon name="box-arrow-up-right"></sl-icon>${osmLinks(p.osm)}</span>` : (it.review ? `<span class="chip-new src-pt"><sl-icon name="geo-alt"></sl-icon>${SRC} point</span>` : `<span class="chip-new"><sl-icon name="plus-circle"></sl-icon>new node</span>`)}
        <sl-button size="small" id="i-josm" title="Zoom JOSM here${josmRef ? " and select the object" : ""}"><sl-icon slot="prefix" name="crosshair"></sl-icon>Zoom JOSM</sl-button>
      </div>
      ${p.kind === "new_node" ? `<div class="insp-move">${p.moved
        ? `<sl-icon name="arrows-move"></sl-icon>Moved ${Math.round(it.layer.getLatLng().distanceTo(L.latLng(p.orig[1], p.orig[0])))} m from the ${SRC} point <sl-button size="small" variant="text" id="i-unmove">Put back</sl-button>`
        : `<sl-icon name="arrows-move"></sl-icon><span class="muted">Drag the point on the map to move it</span>`}</div>` : ""}
      ${it.review ? reviewHelp(p) : ""}
      ${it.review ? fixSection(it) : ""}
      ${it.review ? "" : statusLine(it, st)}
      ${it.review ? "" : decisionButtons(it, st)}
      ${p.poi_candidates?.length ? poiPicker(p) : ""}
      ${p.kind === "tagged_building" ? moveSection(it) : ""}
      ${it.review ? reviewFields(p) : tagEditor(it)}
    </div>`;

  $("#i-back").onclick = closeInspector;
  $("#i-prev").onclick = () => step(-1);
  $("#i-next").onclick = () => step(1);
  $("#i-josm").onclick = () => josmZoomTo(it);
  if ($("#i-unmove")) $("#i-unmove").onclick = () => movePoint(it.cid, null);
  el.querySelectorAll("[data-d]").forEach((b) => b.onclick = () => decideAndAdvance(it, b.dataset.d));
  if (!it.review) { wireTagEditor(it); if (p.kind === "tagged_building") wireMoveSection(it); } else wireFixSection(it);
}

// --- putting an auto-tagged address on a different building ---------------------------------
// The conflation tags the outline the source point falls in. When that is the wrong one (a
// parking deck inside an apartment complex, say), pick the right outline on the map: the
// original stays as it is in OSM and the address goes on the picked building instead.

const addrPart = (t) => Object.fromEntries(Object.entries(t || {}).filter(([k]) => k.startsWith("addr:")));

function moveSection(it) {
  const p = it.p;
  const target = Object.keys(p.fix?.objects || {})[0];
  if (p.decision === "moved" && target) {
    const o = p.fix_objects?.[target];
    return `<div class="insp-sec"><div class="outcome up"><div class="oc-h"><sl-icon name="arrow-left-right"></sl-icon><b>Moved to ${osmLinks(target)}</b>
      <span class="spacer"></span><sl-button size="small" variant="text" id="mv-undo">Undo move</sl-button></div>
      <p>${o?.tags.name ? `${esc(o.tags.name)} · ` : ""}${o?.tags.building ? `building=${esc(o.tags.building)} · ` : ""}gets ${esc(addrLine(p.fix.objects[target]))}. ${osmLinks(p.osm)} is left as it is.</p></div></div>`;
  }
  const cands = P.bcands?.cid === it.cid ? P.bcands.list : null;
  return `<div class="insp-sec move">
    <div class="sec-h"><h4>Wrong building?</h4><span class="spacer"></span>
      <sl-button size="small" id="mv-pick" class="${P.bpick === it.cid ? "picking" : ""}"><sl-icon slot="prefix" name="cursor"></sl-icon>${P.bpick === it.cid ? "Click the right building…" : "Pick another building"}<kbd slot="suffix">m</kbd></sl-button></div>
    ${cands ? (cands.length ? `<div class="fix-opts">${cands.map((b, k) => `
      <button class="fix-opt" data-mv="${k}">
        <span class="fo-n"><sl-icon name="building"></sl-icon></span>
        <span class="fo-main"><b>Put ${esc(addrLine(addrPart(finalTags(p))))} on ${esc(b.tags.name || b.ref)}</b>
          <span>${b.ref} · building=${esc(b.tags.building || "yes")}${b.tags["addr:housenumber"] ? ` · <b class="red">replaces its address ${esc(addrLine(b.tags))}</b>` : ""}. ${osmLinks(p.osm)} stays as it is.</span></span>
        <sl-icon name="chevron-right"></sl-icon></button>`).join("")}</div>`
      : `<p class="muted small">No other building outline there. Click inside an outline.</p>`) : ""}
  </div>`;
}

function wireMoveSection(it) {
  if ($("#mv-pick")) $("#mv-pick").onclick = () => startBuildingPick(it);
  if ($("#mv-undo")) $("#mv-undo").onclick = () => saveFix(it, null);
  document.querySelectorAll("[data-mv]").forEach((b) => b.onclick = () => {
    const c = P.bcands.list[+b.dataset.mv];
    it.p.fix_objects = { [c.ref]: { tags: c.tags, geom: c.geom } };
    saveFix(it, { objects: { [c.ref]: withAddr(c.tags, addrPart(finalTags(it.p))) }, points: [] }).then((ok) => {
      if (ok) { P.bcands = null; drawFix(it); }
    });
  });
}

function startBuildingPick(it) {
  P.bpick = P.bpick === it.cid ? null : it.cid;
  map.getContainer().classList.toggle("picking", !!P.bpick);
  if (P.bpick) toast("Click inside the building the address belongs to");
  renderInspector();
}

// map click while picking a building
map.on("click", async (e) => {
  if (!P?.bpick) return;
  const it = P.items.find((i) => i.cid === P.bpick);
  featureClickAt = Date.now();   // keep the inspector open
  P.bpick = null;
  map.getContainer().classList.remove("picking");
  if (!it) return;
  const list = (await api(`/api/blocks/${P.id}/buildings-at?lon=${e.latlng.lng}&lat=${e.latlng.lat}`)).filter((b) => b.ref !== it.p.osm);
  P.bcands = { cid: it.cid, list };
  if (P.open === it.cid) { renderInspector(); drawFix(it); }
});

function statusLine(it, st) {
  const p = it.p;
  const txt = {
    todo: p.kind === "new_node" && p.poi_candidates?.length ? "Not reviewed: will be added as its own point" : "Not reviewed: included in the changeset",
    approved: "Approved",
    auto: `Confident: one ${SRC} point in an outline without an address. Included without review.`,
    rejected: p.kind === "poi_address" ? "Rejected: the POI is left alone and the address gets its own point"
      : p.kind === "corrected" ? "Rejected: the OSM object stays as it is" : "Rejected: left out of the changeset",
    merged: `Will be put on ${p.poi_candidates?.find((c) => `merge:${c.osm}` === p.decision)?.name || "the POI"}`,
    open: "Waiting for you. Nothing in the changeset touches this.",
    resolved: p.fix ? "Fixed: the fix above is in the changeset" : "Marked fixed (nothing added to the changeset)",
    ignored: "Left as is",
    moved: `Moved: the address goes on ${Object.keys(p.fix?.objects || {}).join(", ") || "another object"} instead`,
  }[st];
  return `<div class="insp-status st-${st}"><span class="it-st">${ST_ICON[st]}</span><span>${esc(txt)}</span></div>`;
}

function decisionButtons(it, st) {
  const [yes, no, yesL, noL] = it.review ? ["resolved", "ignored", "Fixed", "Leave as is"]
    : it.p.poi_candidates?.length ? ["approved", "rejected", "Keep as own point", "Reject"]
    : ["approved", "rejected", "Approve", "Reject"];
  const onYes = st === yes || st === "auto", onNo = st === no;
  if (it.review) {
    // a fix (above) resolves a review item; the only other choice is to leave it
    return `<div class="insp-decide">
      <sl-button class="dbtn no ${onNo ? "on" : ""}" variant="neutral" ${onNo ? "" : "outline"} data-d="${no}"><sl-icon slot="prefix" name="dash-circle"></sl-icon>${onNo ? "Left as is" : "Leave as is, no fix"}<kbd slot="suffix">r</kbd></sl-button>
      ${it.p.decision && !it.p.fix ? `<sl-tooltip content="Clear decision (u)"><sl-button class="dbtn clear" data-d=""><sl-icon name="arrow-counterclockwise"></sl-icon></sl-button></sl-tooltip>` : ""}
    </div>`;
  }
  return `<div class="insp-decide">
    <sl-button class="dbtn yes ${onYes ? "on" : ""}" variant="success" ${onYes ? "" : "outline"} data-d="${yes}"><sl-icon slot="prefix" name="check-lg"></sl-icon>${yesL}<kbd slot="suffix">a</kbd></sl-button>
    <sl-button class="dbtn no ${onNo ? "on" : ""}" variant="danger" ${onNo ? "" : "outline"} data-d="${no}"><sl-icon slot="prefix" name="x-lg"></sl-icon>${noL}<kbd slot="suffix">r</kbd></sl-button>
    ${it.p.decision ? `<sl-tooltip content="Clear decision (u)"><sl-button class="dbtn clear" data-d=""><sl-icon name="arrow-counterclockwise"></sl-icon></sl-button></sl-tooltip>` : ""}
  </div>`;
}

function poiPicker(p) {
  return `<div class="insp-sec"><h4>Put this address on a POI in the same building</h4>
    <div class="poi-pick">${p.poi_candidates.map((c) => `<button class="poi-opt ${p.decision === `merge:${c.osm}` ? "on" : ""}" data-d="merge:${esc(c.osm)}">
      <sl-icon name="${p.decision === `merge:${c.osm}` ? "check-circle-fill" : "shop"}"></sl-icon>
      <span class="poi-txt"><b>${esc(c.name)}</b><span>${esc(c.kind.replace("=", " · "))} · ${c.dist} m away</span></span></button>`).join("")}</div></div>`;
}

// Plain-language help for review items: what the problem is and how to fix it.
// Each entry gets the review properties and returns { what, steps }.
const b = (s) => `<b>${esc(s)}</b>`;
const ref = (r) => (r ? osmLinks(r) : "");
const EXPLAIN = {
  osm_address_offset_from_county: (p) => {
    const osmHn = (p.osm_address || "").split(" ")[0];
    const where = p.osm_number_building
      ? ` That point is inside ${ref(p.osm_number_building)}, ${p.osm_number_building_addr ? `which OSM has as ${b(p.osm_number_building_addr)}` : "which has no address in OSM"}.`
      : p.osm_number_county_at ? " That point isn't inside any building." : "";
    const countyHn = esc((p.county_address || "").split(" ")[0]);
    const duplex = !p.osm_number_building && p.osm_number_dist_m != null && p.osm_number_dist_m < 15;
    return {
      what: `OSM has this building (${ref(p.osm)}) as ${b(p.osm_address)}, but the ${SRC} point inside it is ${b(p.county_address)}.
        The ${SRC} does have a ${b(osmHn)}, ${p.osm_number_dist_m != null ? `${Math.round(p.osm_number_dist_m)} m away` : "nearby"} (the dashed line on the map).${where}
        So either OSM put the numbers on the wrong houses, or the ${SRC}'s points are shifted by one lot. The import changed neither.`,
      steps: [
        `Compare the two points on the map with the houses. Switch to <b>Satellite</b> (<kbd>s</kbd>), or check house numbers on street-level imagery, if you're not sure which house is which.`,
        `<b>If OSM is wrong</b> (usual): in JOSM select ${ref(p.osm)} and set <code>addr:housenumber=${countyHn}</code>${p.osm_number_building ? `, then check ${ref(p.osm_number_building)} has ${b(osmHn)}` : ""}.`,
        ...(duplex ? [`<b>If it's one building with two addresses</b> (a duplex, say): set <code>addr:housenumber=${esc(osmHn)};${countyHn}</code> on ${ref(p.osm)}, or add each address as a point on its door.`] : []),
        `<b>If the ${SRC} is wrong</b>: leave OSM as it is.`,
        `Then mark it <b>Fixed</b> (<kbd>a</kbd>) or <b>Leave as is</b> (<kbd>r</kbd>).`,
      ],
    };
  },
  building_address_ambiguous: (p) => ({
    what: `OSM has this building (${ref(p.osm)}) as ${b(p.osm_address)}, but none of the ${SRC} points inside it match: ${b(p.county_addresses || p.county_address)}. The import can't tell which address is right, so it changed nothing.`,
    steps: [`Check which address the building really has (imagery, the ${SRC} points on the map).`,
      `Fix ${ref(p.osm)} in JOSM if OSM is wrong, or add the missing addresses as points if the building has several.`, `Mark it Fixed or Leave as is.`],
  }),
  street_not_found_in_osm: (p) => ({
    what: `The ${SRC} street ${b((p.county_address || "").replace(/^\S+\s/, ""))} doesn't match any street name in OSM near this point${p.osm_suggestion ? `. The closest OSM name is ${b(p.osm_suggestion)}` : ""}. Addresses must use the street's OSM name, so this one was held back.`,
    steps: [p.osm_suggestion ? `If ${b(p.osm_suggestion)} is the same street, add it to the street overrides file and regenerate the block. Every address on that street is then imported.`
      : `Check whether the street is missing or misnamed in OSM. Fix the street name in JOSM if it's wrong.`,
      `If the ${SRC} name is the one that's wrong, leave it.`, `Mark it Fixed or Leave as is.`],
  }),
  building_has_different_address: (p) => ({
    what: `The ${SRC} point ${b(p.county_address)} falls in a building that OSM already has as ${b(p.osm_address)}.`,
    steps: [`Check which is right. Fix it in JOSM if OSM is wrong.`, `Mark it Fixed or Leave as is.`],
  }),
  address_spans_multiple_buildings: (p, it) => {
    const sib = it ? siblings(it) : [];
    if (!sib.length) return {
      what: `The ${SRC} has ${b(p.county_address)} in more than one building (${ref(p.osm)} is one of them), so the import can't tell which is right.`,
      steps: [] };
    const where = (x) => { const bl = mainBuilding(x); return bl ? `${ref(bl[0])}${bl[1].tags.name ? ` (${esc(bl[1].tags.name)})` : ""}` : "a spot outside any building"; };
    return {
      what: `The ${SRC} data has ${b(p.county_address)} ${sib.length + 1} times: here in ${where(it)}, and ${sib.map((x) => `${Math.round(dist(it, x))} m away in ${where(x)}`).join(", ")}.
        An address exists once, so all but one are duplicates in the ${SRC} data. Keep the right one; the others are dropped.`,
      steps: [] };
  },
  multipolygon_building: (p) => ({
    what: `${b(p.county_address)} falls in a multipolygon building (${ref(p.osm)}). The import doesn't edit relations automatically.`,
    steps: [`In JOSM, add the address tags to the relation ${ref(p.osm)} if it's the right building.`, `Mark it Fixed or Leave as is.`],
  }),
  minor_building_type: (p) => ({
    what: `${b(p.county_address)} falls in a ${b(p.building || "garage/shed")} outline (${ref(p.osm)}). Addresses usually belong on the main house, not on a garage or shed.`,
    steps: [`If the point really belongs to the main building next to it, add the address there in JOSM.`, `Otherwise leave it.`],
  }),
  attached_building_points_misaligned: (p) => ({
    what: `${b(p.county_address)} is in a row of attached buildings (${ref(p.osm)}), and the ${SRC} points don't line up one-per-unit with the outlines. Tagging them would likely put numbers on the wrong units.`,
    steps: [`Check the row against imagery. In JOSM, tag each unit outline with its number.`, `Mark it Fixed or Leave as is.`],
  }),
  multiple_addresses_in_building: (p) => ({
    what: `The building ${ref(p.osm)} contains several ${SRC} addresses, including ${b(p.county_address)}.`,
    steps: [`Add the addresses as separate points inside the building in JOSM, if they're real.`, `Mark it Fixed or Leave as is.`],
  }),
  snapped_to_nearby_building: (p) => ({
    what: `The ${SRC} point ${b(p.county_address)} isn't inside a building. The nearest outline is ${ref(p.osm)}, but that's a guess.`,
    steps: [`If that's the right building, add the address to it in JOSM.`, `Mark it Fixed or Leave as is.`],
  }),
  no_building: (p) => ({
    what: `The ${SRC} point ${b(p.county_address)} isn't inside or near any building outline in OSM. The building may be missing.`,
    steps: [`If there's a building on imagery, draw it in JOSM and add the address.`, `Otherwise leave it.`],
  }),
  osm_address_not_in_county: (p) => ({
    what: `OSM has ${b(p.osm_address)} on ${ref(p.osm)}, but the ${SRC} has no such address nearby. It could be a mistake in OSM or a new or unofficial address.`,
    steps: [`Check the address. Fix or remove it in JOSM only if you're sure it's wrong.`, `Mark it Fixed or Leave as is.`],
  }),
  would_duplicate_existing: (p) => ({
    what: `Adding ${b(p.county_address)} would duplicate an address OSM already has on ${ref(p.duplicate_of || p.osm)}, so it was left out.`,
    steps: [`Usually nothing to do. If the existing one is on the wrong object, move it in JOSM.`, `Mark it Fixed or Leave as is.`],
  }),
  would_duplicate_in_changeset: (p) => ({
    what: `${b(p.county_address)} would be added twice in this changeset, so the second copy was left out.`,
    steps: [`Usually nothing to do.`, `Leave as is.`],
  }),
  pending_in_other_block: (p) => ({
    what: `${b(p.county_address)} is already proposed by a neighbouring block (${esc(p.duplicate_of || "")}), so it was left out here to avoid a duplicate.`,
    steps: [`Nothing to do here. It's reviewed in the other block.`, `Leave as is.`],
  }),
  street_differs_and_not_in_osm: (p) => ({
    what: `OSM has ${b(p.osm_address)}, but the ${SRC} says ${b(p.county_address)}, and the ${SRC}'s street name isn't on any OSM street nearby.`,
    steps: [`Check which street the building really fronts. Fix the address or the street name in JOSM.`, `Mark it Fixed or Leave as is.`],
  }),
  multi_value_housenumber_differs: (p) => ({
    what: `OSM has several numbers on ${ref(p.osm)} (${b(p.osm_address)}), and they don't match the ${SRC}'s ${b(p.county_address)}.`,
    steps: [`Decide whether the building has one address or several. Fix it in JOSM.`, `Mark it Fixed or Leave as is.`],
  }),
  relation_address_needs_update: (p) => ({
    what: `The address on the relation ${ref(p.osm)} differs from the ${SRC} in ${b(p.fields || "some fields")}. The import doesn't edit relations automatically.`,
    steps: [`Update the relation's address tags in JOSM.`, `Mark it Fixed or Leave as is.`],
  }),
};

function reviewHelp(p) {
  const it = P.items.find((x) => x.p === p);
  const e = EXPLAIN[p.reason]?.(p, it);
  if (!e) return "";
  return `<div class="help">
    <div class="help-what"><h4>What's going on</h4><p>${e.what}</p></div>
  </div>`;
}

function reviewFields(p) {
  const skip = new Set(["kind", "idx", "reason", "cid", "decision", "osm_number_county_at", "osm_number_dist_m"]);
  const label = { county_address: `${SRCC} address`, county_id: `${SRCC} ID`, proposed_street: "Proposed street", osm: "OSM object",
    osm_address: "OSM address", county_addresses: `${SRCC} points inside`, osm_suggestion: "OSM street nearby", empty_neighbors: "Empty neighbours",
    duplicate_of: "Duplicate of", building: "Building type", osm_number_building: "Building at OSM's number", osm_number_building_addr: "Its OSM address" };
  return `<sl-details summary="All details" class="raw"><table class="kv">${Object.entries(p).filter(([k]) => !skip.has(k)).map(([k, v]) =>
    `<tr><td>${esc(label[k] || k)}</td><td>${["osm", "empty_neighbors", "osm_number_building"].includes(k) ? osmLinks(v) : esc(v)}</td></tr>`).join("")}</table></sl-details>`;
}

// tag editor: compares the tags that will be uploaded with what OSM has now

function tagEditor(it) {
  const base = it.p.base || {};
  const work = P.work;
  const keys = [...new Set([...Object.keys(base), ...Object.keys(work)])]
    .sort((a, b) => (b.startsWith("addr:") - a.startsWith("addr:")) || a.localeCompare(b));
  const rows = keys.map((k) => {
    const inB = k in base, inW = k in work;
    const state = !inW ? "removed" : !inB ? "added" : base[k] !== work[k] ? "changed" : "same";
    if (state === "removed") {
      return `<tr class="tg removed"><td class="tk">${esc(k)}</td><td><span class="old">${esc(base[k])}</span></td>
        <td><sl-icon-button name="arrow-counterclockwise" data-restore="${esc(k)}" label="Keep this tag" title="Keep this tag"></sl-icon-button></td></tr>`;
    }
    return `<tr class="tg ${state}"><td class="tk">${esc(k)}</td>
      <td><input data-k="${esc(k)}" value="${esc(work[k])}" spellcheck="false">${state === "changed" ? `<div class="old">was ${esc(base[k])}</div>` : ""}</td>
      <td><sl-icon-button name="x" data-del="${esc(k)}" label="Remove tag" title="Remove tag"></sl-icon-button></td></tr>`;
  }).join("");
  const isNew = it.p.kind === "new_node";
  return `<div class="insp-sec">
    <div class="sec-h"><h4>Tags</h4>${isNew ? "" : `<span class="muted">compared with OSM now</span>`}<span class="spacer"></span>
      ${it.p.edit ? `<sl-badge variant="primary" pill>edited</sl-badge>` : ""}</div>
    <table class="tagtab">${rows}
      <tr class="tg add"><td><input id="t-newk" placeholder="key" spellcheck="false"></td><td><input id="t-newv" placeholder="value" spellcheck="false"></td>
        <td><sl-icon-button name="plus-lg" id="t-add" label="Add tag" title="Add tag"></sl-icon-button></td></tr>
    </table>
    <div class="legend-mini"><span class="added">added</span><span class="changed">changed</span><span class="removed">removed</span></div>
    <div id="t-err"></div>
    <div class="t-actions">
      <sl-button variant="primary" size="small" id="t-save" ${P.dirty ? "" : "disabled"} title="⌘↵">Save tags<kbd slot="suffix">⌘↵</kbd></sl-button>
      ${P.dirty ? `<sl-button size="small" variant="text" id="t-cancel">Discard</sl-button>` : ""}
      ${it.p.edit ? `<sl-button size="small" variant="text" id="t-revert"><sl-icon slot="prefix" name="arrow-counterclockwise"></sl-icon>Generated tags</sl-button>` : ""}
    </div>
  </div>`;
}

function wireTagEditor(it) {
  const rerender = () => { P.dirty = true; const f = document.activeElement?.dataset?.k; renderInspector(); if (f) document.querySelector(`[data-k="${CSS.escape(f)}"]`)?.focus(); };
  document.querySelectorAll(".tagtab input[data-k]").forEach((inp) => {
    inp.oninput = () => {
      P.work[inp.dataset.k] = inp.value;
      if (!P.dirty) { P.dirty = true; $("#t-save").disabled = false; }
    };
  });
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => { delete P.work[b.dataset.del]; rerender(); });
  document.querySelectorAll("[data-restore]").forEach((b) => b.onclick = () => { P.work[b.dataset.restore] = it.p.base[b.dataset.restore]; rerender(); });
  const add = () => {
    const k = $("#t-newk").value.trim(), v = $("#t-newv").value.trim();
    if (!k || !v) return $("#t-newk").focus();
    P.work[k] = v; P.dirty = true; renderInspector(); $("#t-newk").focus();
  };
  $("#t-add").onclick = add;
  $("#t-newv").onkeydown = (e) => { if (e.key === "Enter") add(); };
  $("#t-save").onclick = () => saveTags(it);
  if ($("#t-cancel")) $("#t-cancel").onclick = () => { P.work = { ...finalTags(it.p) }; P.dirty = false; renderInspector(); };
  if ($("#t-revert")) $("#t-revert").onclick = () => saveTags(it, true);
}

async function saveTags(it, revert = false) {
  const tags = revert ? null : Object.fromEntries(Object.entries(P.work).map(([k, v]) => [k.trim(), String(v).trim()]).filter(([k, v]) => k && v));
  const before = it.p.edit || null;
  const after = await setEdit(it.cid, tags);
  if (after === undefined) return;
  const id = P.id, cid = it.cid;
  record({
    block: id, cid, label: `${revert ? "Reverted tags of" : "Edited tags of"} ${itemText(it.p).title}`,
    undo: () => setEdit(cid, before), redo: () => setEdit(cid, after),
  });
}

/** Set (or with null clear) the user's tags for a change. Returns the stored edit, or undefined on failure. */
async function setEdit(cid, tags) {
  const it = P?.items.find((i) => i.cid === cid);
  if (!it) return undefined;
  const post = (t) => fetch(`/api/blocks/${P.id}/edit`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cid, tags: t }) });
  const r = await post(tags);
  const j = await r.json();
  if (!r.ok) {
    const msg = j.error === "duplicate"
      ? `Not saved: this address already exists on ${j.clashes.map((c) => c.startsWith("new node") ? esc(c) : osmLinks(c)).join(", ")}. The import never adds duplicates.`
      : esc(j.error);
    if (P.open === cid && $("#t-err")) $("#t-err").innerHTML = `<sl-alert variant="danger" open class="banner"><sl-icon slot="icon" name="exclamation-octagon"></sl-icon>${msg}</sl-alert>`;
    else toast(msg, true);
    return undefined;
  }
  // tags identical to the generated ones are not an edit
  const same = j.edit && JSON.stringify(Object.entries(j.edit).sort()) === JSON.stringify(Object.entries(it.p.tags || {}).sort());
  if (same) await post(null);
  it.p.edit = same ? null : j.edit;
  if (P.open === cid) { P.work = { ...finalTags(it.p) }; P.dirty = false; renderInspector(); }
  refreshRow(cid);
  renderReviewProgress();
  return it.p.edit;
}


// --- fixing review items in the app ------------------------------------------------------
// A fix sets new tags on existing OSM objects and/or adds address points. The server
// writes it into the changeset and marks the item resolved.

const hnOf = (s) => String(s || "").split(" ")[0];
const stripAddr = (t) => Object.fromEntries(Object.entries(t || {}).filter(([k]) => !k.startsWith("addr:")));
const withAddr = (t, addr) => ({ ...stripAddr(t), ...addr });
const addrLine = (t) => addrOf(t) || "no address";
const NO_FIX = new Set(["would_duplicate_existing", "would_duplicate_in_changeset", "pending_in_other_block"]);
const sameFix = (a, b) => JSON.stringify(normFix(a)) === JSON.stringify(normFix(b));
function normFix(f) {
  if (!f) return null;
  const sortT = (t) => Object.fromEntries(Object.entries(t).sort());
  return { objects: Object.fromEntries(Object.entries(f.objects || {}).sort().map(([r, t]) => [r, sortT(t)])),
    points: (f.points || []).map((p) => ({ lon: +(+p.lon).toFixed(7), lat: +(+p.lat).toFixed(7), tags: sortT(p.tags) })) };
}

function reviewLatLng(it) {
  const ll = it.layer?.getLatLng?.();
  return ll ? [ll.lng, ll.lat] : null;
}

const normAddr = (s) => String(s || "").toUpperCase().replace(/\s+/g, " ").trim();
/** Other review items in this block for the same source address (source duplicates). */
function siblings(it) {
  const a = normAddr(it.p.county_address);
  if (!a) return [];
  return P.items.filter((x) => x.review && x.cid !== it.cid && normAddr(x.p.county_address) === a);
}
const dist = (x, y) => (x.layer?.getLatLng && y.layer?.getLatLng ? x.layer.getLatLng().distanceTo(y.layer.getLatLng()) : 0);
/** [ref, object] of the building a review item's source point is in, if any. */
const mainBuilding = (x) => Object.entries(x.p.objects || {}).find(([r, o]) => o.tags.building) || null;

function countyTagsOf(p) {
  return p.county_tags || (p.county_address && p.proposed_street
    ? { "addr:housenumber": hnOf(p.county_address), "addr:street": p.proposed_street } : null);
}

function inRing(ll, ring) {
  let inside = false;
  for (let a = 0, b = ring.length - 1; a < ring.length; b = a++) {
    const [xa, ya] = ring[a], [xb, yb] = ring[b];
    if ((ya > ll[1]) !== (yb > ll[1]) && ll[0] < ((xb - xa) * (ll[1] - ya)) / (yb - ya) + xa) inside = !inside;
  }
  return inside;
}
/** Other addresses in a building: its own OSM address, plus source points of other changes and review items inside it. */
function otherAddressesIn(bl, addr) {
  const ring = bl?.[1].geom?.type === "Polygon" ? bl[1].geom.coordinates[0] : null;
  const out = new Set();
  if (bl?.[1].tags["addr:housenumber"]) out.add(addrLine(bl[1].tags));
  if (!ring) return [...out];
  for (const x of P.items) {
    const ll = x.layer?.getLatLng?.();
    if (!ll || !inRing([ll.lng, ll.lat], ring)) continue;
    const a = x.review ? x.p.county_address : addrOf(finalTags(x.p));
    if (a && normAddr(a) !== normAddr(addr)) out.add(a);
  }
  return [...out];
}

/** Options that keep one of several source points with the same address and drop the rest.
 *  Each can be kept on its building or as its own point; a building that holds other
 *  addresses too gets the point first. */
function keepOneOptions(it) {
  const sib = siblings(it);
  if (!sib.length) return [];
  const opts = [];
  for (const x of [it, ...sib]) {
    const ct = countyTagsOf(x.p), bl = mainBuilding(x), ll = reviewLatLng(x);
    const where = x === it ? "this one" : `the one ${Math.round(dist(it, x))} m away`;
    const name = bl?.[1].tags.name ? `: ${bl[1].tags.name}` : "";
    const ignore = [it, ...sib].filter((y) => y !== x).map((y) => y.cid);
    const dropped = `The other ${SRC} point${sib.length > 1 ? "s are" : " is"} dropped as a duplicate.`;
    const others = bl ? otherAddressesIn(bl, x.p.county_address) : [];
    const onBuilding = bl && {
      title: `Keep ${where} on the building${name}`,
      desc: `${bl[0]} gets ${addrLine(ct)}.${others.length ? ` Careful: the building also has ${others.slice(0, 3).join(", ")}${others.length > 3 ? "…" : ""}.` : ""} ${dropped}`,
      fix: { objects: { [bl[0]]: withAddr(bl[1].tags, ct) }, points: [] }, target: x.cid, ignore,
    };
    const asPoint = ll && {
      title: `Keep ${where} as its own point${name}`,
      desc: `A new point with ${addrLine(ct)} at the ${SRC} location; ${bl ? "the building isn't changed" : "it isn't inside a building"}. ${dropped}`,
      fix: { objects: {}, points: [{ lon: ll[0], lat: ll[1], tags: ct }] }, target: x.cid, ignore,
    };
    const pair = others.length ? [asPoint, onBuilding] : [onBuilding, asPoint];
    opts.push(...pair.filter(Boolean));
  }
  return opts;
}

function optionOn(it, o) {
  const t = P.items.find((x) => x.cid === (o.target || it.cid));
  if (!t?.p.fix || t.p.decision === "ignored" || !sameFix(o.fix, t.p.fix)) return false;
  return (o.ignore || []).every((c) => P.items.find((x) => x.cid === c)?.p.decision === "ignored");
}

/** Confirm the default: nothing is uploaded for this item. */
function skipItem(it) {
  return decideAndAdvance(it, "ignored");
}

/** Apply a suggested fix; "keep one" options also drop the duplicates. One undo step. */
async function applyOption(it, o) {
  if (!o.target && !o.ignore) return saveFix(it, o.fix);
  const cids = [o.target, ...o.ignore];
  const before = cids.map((c) => { const x = P.items.find((y) => y.cid === c); return [c, x.p.fix ? structuredClone(x.p.fix) : null, x.p.decision || null]; });
  const run = async (state) => {
    for (const [c, fix, dec] of state) {
      const x = P.items.find((y) => y.cid === c);
      if (!sameFix(x.p.fix, fix)) { if (!(await saveFix(x, fix, true))) return false; }
      if ((x.p.decision || null) !== dec) await decide([c], dec || "", true);
    }
    return true;
  };
  const after = cids.map((c) => c === o.target ? [c, o.fix, "resolved"] : [c, null, "ignored"]);
  // drop the duplicates' fixes first so the kept one doesn't clash with them
  if (!(await run([...after.slice(1), after[0]]))) return;
  inspect(it.cid, false);
  record({ block: P.id, cid: it.cid, label: `${o.title} (${it.p.county_address})`,
    undo: () => run(before), redo: () => run([...after.slice(1), after[0]]) });
}

/** One-click fixes for a review item, most likely first. */
function fixOptions(it) {
  const p = it.p, objs = p.objects || {}, ll = reviewLatLng(it);
  // blocks generated before county_tags existed: rebuild the basics from the source address
  const ct = p.county_tags || (p.county_address && p.proposed_street
    ? { "addr:housenumber": hnOf(p.county_address), "addr:street": p.proposed_street } : null);
  const main = (p.osm || "").split(",")[0];
  const opts = keepOneOptions(it);
  const add = (o) => { if (!opts.some((x) => sameFix(x.fix, o.fix))) opts.push(o); };
  const point = (tags, at = ll) => ({ lon: at[0], lat: at[1], tags });
  if (NO_FIX.has(p.reason) || opts.length) return opts;

  if (p.reason === "osm_address_offset_from_county" && ct && objs[main]) {
    const osmHn = hnOf(p.osm_address), cHn = ct["addr:housenumber"];
    const objects = { [main]: withAddr(objs[main].tags, ct) };
    const points = [];
    const other = p.osm_number_building && objs[p.osm_number_building];
    if (other) objects[p.osm_number_building] = withAddr(other.tags, p.osm_number_county_tags);
    else if (p.osm_number_county_at && p.osm_number_county_tags) points.push(point(p.osm_number_county_tags, p.osm_number_county_at));
    add({ title: `This building is ${cHn}`, desc: `OSM had the wrong number. ${main} becomes ${addrLine(ct)}; ${osmHn} ${other ? `goes on ${p.osm_number_building}` : `becomes a point where the ${SRC} has it`}.`, fix: { objects, points } });
    add({ title: `This building has both ${osmHn} and ${cHn}`, desc: `A duplex or similar: ${main} gets addr:housenumber=${osmHn};${cHn}.`,
      fix: { objects: { [main]: withAddr(objs[main].tags, { ...ct, "addr:housenumber": `${osmHn};${cHn}` }) }, points: [] } });
    add({ title: `Keep ${osmHn}, add ${cHn} as a point`, desc: `Leaves the building as it is and puts ${addrLine(ct)} on a point at the ${SRC} location.`, fix: { objects: {}, points: [point(ct)] } });
  }
  if (p.reason === "street_not_found_in_osm" && ct && ll) {
    if (p.osm_suggestion) add({ title: `Add it on ${p.osm_suggestion}`, desc: `Uses the OSM street name instead of the ${SRC}'s ${(p.county_address || "").replace(/^\S+\s/, "")}.`,
      fix: { objects: {}, points: [point({ ...ct, "addr:street": p.osm_suggestion })] } });
  }
  if (p.reason === "osm_address_not_in_county" && objs[main]) {
    add({ title: `Remove the address from ${main}`, desc: `Only if you're sure ${p.osm_address} is wrong. Other tags stay.`, fix: { objects: { [main]: stripAddr(objs[main].tags) }, points: [] } });
  }
  if (ct) {
    for (const [ref, o] of Object.entries(p.reason === "osm_address_offset_from_county" ? {} : objs)) {
      if (ref.startsWith("node/") && !o.tags.building) continue;
      const has = o.tags["addr:housenumber"];
      add({ title: has ? `Replace ${addrLine(o.tags)} with ${addrLine(ct)}` : `Put ${addrLine(ct)} on ${o.tags.building ? "the building" : ref}`,
        desc: `${ref}${o.tags.building ? ` (building=${o.tags.building})` : ""} gets the ${SRC} address.`, fix: { objects: { [ref]: withAddr(o.tags, ct) }, points: [] } });
    }
    if (ll) add({ title: `Add ${addrLine(ct)} as a point`, desc: `A new address point at the ${SRC} location. You can move it on the map.`, fix: { objects: {}, points: [point(ct)] } });
  }
  return opts;
}

function fixSummary(fix, objs) {
  const lines = [];
  for (const [ref, t] of Object.entries(fix.objects || {})) {
    const base = objs?.[ref]?.tags || {};
    const ch = [...new Set([...Object.keys(base), ...Object.keys(t)])].filter((k) => base[k] !== t[k])
      .map((k) => !(k in t) ? `<span class="rm">${esc(k)}</span>` : `${esc(k)}=<b>${esc(t[k])}</b>`);
    lines.push(`<li><sl-icon name="pencil"></sl-icon><span>${osmLinks(ref)}: ${ch.join(", ") || "no change"}</span></li>`);
  }
  for (const pt of fix.points || []) lines.push(`<li><sl-icon name="plus-circle"></sl-icon><span>new point <b>${esc(addrLine(pt.tags))}</b></span></li>`);
  return `<ul class="fix-sum">${lines.join("")}</ul>`;
}

/** The "do nothing" choice, worded for the reason. It is also what happens if the item is never decided. */
function skipOption(it) {
  const p = it.p, dup = p.duplicate_of || "";
  const title = {
    would_duplicate_existing: "Skip it: OSM already has this address",
    would_duplicate_in_changeset: "Skip it: already added in this changeset",
    pending_in_other_block: "Skip it: the neighbouring block adds it",
    osm_address_not_in_county: "Leave OSM as it is",
  }[p.reason] || "Skip it";
  const desc = {
    would_duplicate_existing: `Nothing is uploaded for this ${SRC} point.${/^(node|way|relation)\//.test(dup) ? ` ${dup} keeps its address.` : ""}`,
    osm_address_not_in_county: `${p.osm || "The OSM object"} keeps ${p.osm_address || "its address"}. Nothing is uploaded.`,
  }[p.reason] || "Nothing is uploaded for this point. OSM stays as it is.";
  return { skip: true, title, desc };
}

function outcomeBox(it, opts) {
  const p = it.p;
  const keptElsewhere = p.decision === "ignored" && opts.find((o) => o.target && o.target !== it.cid && optionOn(it, o));
  if (p.fix && p.decision === "resolved") {
    const manual = !opts.some((o) => !o.skip && sameFix(o.fix, p.fix));
    return `<div class="outcome up"><div class="oc-h"><sl-icon name="cloud-arrow-up-fill"></sl-icon><b>Will upload${manual ? " (edited by hand)" : ""}</b>
      <span class="spacer"></span><sl-button size="small" variant="text" id="fx-clear">Remove fix</sl-button></div>${fixSummary(p.fix, p.objects)}</div>`;
  }
  if (p.decision === "ignored") {
    return `<div class="outcome skip"><div class="oc-h"><sl-icon name="slash-circle"></sl-icon><b>${keptElsewhere ? "Dropped as a duplicate" : "Skipped"}</b>
      <span class="spacer"></span><sl-button size="small" variant="text" data-d="">Undo</sl-button></div>
      <p>${keptElsewhere ? `You ${esc(keptElsewhere.title.replace(/^Keep /, "kept "))}. ` : ""}Nothing is uploaded for this point.</p></div>`;
  }
  if (p.decision === "resolved") {
    return `<div class="outcome skip"><div class="oc-h"><sl-icon name="check-circle"></sl-icon><b>Marked fixed outside the app</b>
      <span class="spacer"></span><sl-button size="small" variant="text" data-d="">Undo</sl-button></div><p>Nothing is uploaded for this point.</p></div>`;
  }
  const sk = skipOption(it);
  return `<div class="outcome todo"><div class="oc-h"><sl-icon name="hourglass-split"></sl-icon><b>Not decided: will be skipped</b></div>
    <p>Unless you choose otherwise, <b>nothing is uploaded</b> for this point (“${esc(sk.title)}”). Confirm with <kbd>a</kbd>, or pick a fix.</p></div>`;
}

function fixSection(it) {
  const p = it.p;
  const opts = [...fixOptions(it), skipOption(it)];
  const saved = p.fix;
  if (!P.fx || P.fxFor !== it.cid) { P.fx = structuredClone(saved || { objects: {}, points: [] }); P.fxFor = it.cid; P.fxDirty = false; }
  const manual = saved && p.decision === "resolved" && !opts.some((o) => !o.skip && sameFix(o.fix, saved));
  const isOn = (o) => o.skip ? p.decision === "ignored" && !opts.some((x) => x.target && optionOn(it, x)) : optionOn(it, o);
  return `<div class="insp-sec fixes">
    ${outcomeBox(it, opts)}
    <h4>What should happen?</h4>
    <div class="fix-opts">${opts.map((o, k) => `
      <button class="fix-opt ${o.skip ? "skip" : ""} ${isOn(o) ? "on" : ""}" data-opt="${k}">
        <span class="fo-n">${o.skip ? "r" : k < 9 ? k + 1 : ""}</span>
        <span class="fo-main"><b>${esc(o.title)}${o.skip && !p.decision ? ` <span class="default">default</span>` : ""}</b><span>${esc(o.desc)}</span></span>
        <sl-icon name="${isOn(o) ? "check-circle-fill" : "chevron-right"}"></sl-icon>
      </button>`).join("")}</div>
    ${!fixOptions(it).length && !NO_FIX.has(p.reason) ? `<p class="muted small">No automatic fix for this one. To change OSM, edit it by hand below.</p>` : ""}
    ${saved && p.decision === "ignored" ? `<p class="muted small">A fix you made earlier is saved but not used, because this is skipped.</p>` : ""}
    <sl-details class="manual" summary="Edit by hand" ${manual || P.fxDirty || P.fxOpen ? "open" : ""}>${manualEditor(it)}</sl-details>
  </div>`;
}

function manualEditor(it) {
  const p = it.p, objs = p.objects || {};
  const fx = P.fx;
  const objRows = Object.entries(objs).map(([ref, o], k) => {
    const editing = ref in fx.objects;
    return `<div class="me-obj">
      <div class="me-h"><span class="me-dot o"></span><span>${osmLinks(ref)}</span><span class="muted">${esc(addrLine(o.tags))}</span><span class="spacer"></span>
        ${editing ? `<sl-button size="small" variant="text" data-unedit="${esc(ref)}">Don't change</sl-button>` : `<sl-button size="small" data-edit="${esc(ref)}"><sl-icon slot="prefix" name="pencil"></sl-icon>Edit tags</sl-button>`}</div>
      ${editing ? kvEditor(`o${k}`, o.tags, fx.objects[ref]) : ""}</div>`;
  }).join("");
  const ptRows = fx.points.map((pt, k) => `<div class="me-obj">
      <div class="me-h"><span class="me-dot p"></span><span>New point ${fx.points.length > 1 ? k + 1 : ""}</span><span class="spacer"></span>
        <sl-button size="small" class="${P.picking === k ? "picking" : ""}" data-pick="${k}"><sl-icon slot="prefix" name="crosshair"></sl-icon>${P.picking === k ? "Click the map…" : "Place on map"}</sl-button>
        <sl-icon-button name="trash" label="Remove point" data-rmpt="${k}"></sl-icon-button></div>
      ${kvEditor(`p${k}`, {}, pt.tags)}</div>`).join("");
  return `<div class="me">
    ${objRows || `<p class="muted small">No existing OSM object is involved.</p>`}
    ${ptRows}
    <sl-button size="small" id="fx-addpt"><sl-icon slot="prefix" name="plus-lg"></sl-icon>Add address point</sl-button>
    <div id="fx-err"></div>
    <div class="t-actions">
      <sl-button variant="primary" size="small" id="fx-save" ${P.fxDirty ? "" : "disabled"}>Save fix<kbd slot="suffix">⌘↵</kbd></sl-button>
      ${P.fxDirty ? `<sl-button size="small" variant="text" id="fx-discard">Discard</sl-button>` : ""}
    </div></div>`;
}

/** Tag table: base = tags in OSM now (empty for new points), work = tags being edited. */
function kvEditor(scope, base, work) {
  const keys = [...new Set([...Object.keys(base), ...Object.keys(work)])]
    .sort((a, b) => (b.startsWith("addr:") - a.startsWith("addr:")) || a.localeCompare(b));
  const rows = keys.map((k) => {
    const inB = k in base, inW = k in work;
    const state = !inW ? "removed" : !inB ? (Object.keys(base).length ? "added" : "same") : base[k] !== work[k] ? "changed" : "same";
    if (state === "removed") {
      return `<tr class="tg removed"><td class="tk">${esc(k)}</td><td><span class="old">${esc(base[k])}</span></td>
        <td><sl-icon-button name="arrow-counterclockwise" data-scope="${scope}" data-restore="${esc(k)}" label="Keep this tag"></sl-icon-button></td></tr>`;
    }
    return `<tr class="tg ${state}"><td class="tk">${esc(k)}</td>
      <td><input data-scope="${scope}" data-k="${esc(k)}" value="${esc(work[k])}" spellcheck="false">${state === "changed" ? `<div class="old">was ${esc(base[k])}</div>` : ""}</td>
      <td><sl-icon-button name="x" data-scope="${scope}" data-del="${esc(k)}" label="Remove tag"></sl-icon-button></td></tr>`;
  }).join("");
  return `<table class="tagtab">${rows}
    <tr class="tg add"><td><input data-scope="${scope}" data-newk placeholder="key" spellcheck="false"></td><td><input data-scope="${scope}" data-newv placeholder="value" spellcheck="false"></td>
      <td><sl-icon-button name="plus-lg" data-scope="${scope}" data-addtag label="Add tag"></sl-icon-button></td></tr></table>`;
}

function scopeTags(it, scope) {
  const refs = Object.keys(it.p.objects || {});
  return scope[0] === "o" ? P.fx.objects[refs[+scope.slice(1)]] : P.fx.points[+scope.slice(1)].tags;
}
function scopeBase(it, scope) {
  const refs = Object.keys(it.p.objects || {});
  return scope[0] === "o" ? it.p.objects[refs[+scope.slice(1)]].tags : {};
}

function wireFixSection(it) {
  const root = $("#insp");
  const dirty = (focusSel) => { P.fxDirty = true; P.fxOpen = true; renderInspector(); drawFix(it); if (focusSel) root.querySelector(focusSel)?.focus(); };
  root.querySelectorAll("[data-opt]").forEach((b) => b.onclick = () => {
    const o = [...fixOptions(it), skipOption(it)][+b.dataset.opt];
    return o.skip ? skipItem(it) : applyOption(it, o);
  });
  if ($("#fx-clear")) $("#fx-clear").onclick = () => saveFix(it, null);
  root.querySelectorAll(".outcome [data-d]").forEach((b) => b.onclick = () => decide([it.cid], ""));
  root.querySelector("sl-details.manual")?.addEventListener("sl-show", (e) => { if (e.target.matches("sl-details.manual")) P.fxOpen = true; });
  root.querySelector("sl-details.manual")?.addEventListener("sl-hide", (e) => { if (e.target.matches("sl-details.manual")) P.fxOpen = false; });
  root.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => { P.fx.objects[b.dataset.edit] = { ...it.p.objects[b.dataset.edit].tags }; dirty(); });
  root.querySelectorAll("[data-unedit]").forEach((b) => b.onclick = () => { delete P.fx.objects[b.dataset.unedit]; dirty(); });
  root.querySelectorAll("[data-rmpt]").forEach((b) => b.onclick = () => { P.fx.points.splice(+b.dataset.rmpt, 1); P.picking = null; dirty(); });
  root.querySelectorAll("[data-pick]").forEach((b) => b.onclick = () => {
    P.picking = P.picking === +b.dataset.pick ? null : +b.dataset.pick;
    map.getContainer().classList.toggle("picking", P.picking != null);
    renderInspector();
  });
  if ($("#fx-addpt")) $("#fx-addpt").onclick = () => {
    const ll = reviewLatLng(it) || [map.getCenter().lng, map.getCenter().lat];
    const ct = it.p.county_tags || (it.p.county_address ? { "addr:housenumber": hnOf(it.p.county_address), "addr:street": it.p.proposed_street || "" } : {});
    P.fx.points.push({ lon: ll[0], lat: ll[1], tags: { ...ct } });
    dirty();
  };
  root.querySelectorAll(".manual input[data-k]").forEach((inp) => inp.oninput = () => {
    scopeTags(it, inp.dataset.scope)[inp.dataset.k] = inp.value;
    if (!P.fxDirty) { P.fxDirty = true; $("#fx-save").disabled = false; }
  });
  root.querySelectorAll(".manual [data-del]").forEach((b) => b.onclick = () => { delete scopeTags(it, b.dataset.scope)[b.dataset.del]; dirty(); });
  root.querySelectorAll(".manual [data-restore]").forEach((b) => b.onclick = () => {
    scopeTags(it, b.dataset.scope)[b.dataset.restore] = scopeBase(it, b.dataset.scope)[b.dataset.restore]; dirty();
  });
  const addTag = (scope) => {
    const k = root.querySelector(`[data-newk][data-scope="${scope}"]`).value.trim();
    const v = root.querySelector(`[data-newv][data-scope="${scope}"]`).value.trim();
    if (!k || !v) return;
    scopeTags(it, scope)[k] = v;
    dirty(`[data-newk][data-scope="${scope}"]`);
  };
  root.querySelectorAll(".manual [data-addtag]").forEach((b) => b.onclick = () => addTag(b.dataset.scope));
  root.querySelectorAll(".manual [data-newv]").forEach((inp) => inp.onkeydown = (e) => { if (e.key === "Enter") addTag(inp.dataset.scope); });
  if ($("#fx-save")) $("#fx-save").onclick = () => saveFix(it, manualFix(it));
  if ($("#fx-discard")) $("#fx-discard").onclick = () => { P.fx = null; P.picking = null; renderInspector(); drawFix(it); };
}

/** The hand-edited fix, without objects whose tags didn't change. */
function manualFix(it) {
  const objects = Object.fromEntries(Object.entries(P.fx.objects).filter(([ref, t]) =>
    JSON.stringify(Object.entries(t).filter(([k, v]) => k && v).sort()) !== JSON.stringify(Object.entries(it.p.objects[ref].tags).sort())));
  return { objects, points: P.fx.points };
}

async function saveFix(it, fix, silent = false) {
  const before = it.p.fix ? structuredClone(it.p.fix) : null;
  const beforeDec = it.p.decision || null;
  const r = await fetch(`/api/blocks/${P.id}/fix`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cid: it.cid, fix }) });
  const j = await r.json();
  if (!r.ok) {
    const msg = j.error === "duplicate"
      ? `Not saved: that address already exists on ${j.clashes.map((c) => /^(node|way|relation)\//.test(c) ? osmLinks(c) : esc(c)).join(", ")}. The import never adds duplicates.`
      : esc(j.error);
    if ($("#fx-err")) { $("#fx-err").innerHTML = `<sl-alert variant="danger" open class="banner"><sl-icon slot="icon" name="exclamation-octagon"></sl-icon>${msg}</sl-alert>`; P.fxOpen = true; }
    toast(msg, true);
    return false;
  }
  it.p.fix = j.fix;
  it.p.decision = j.decision;
  it.layer?.setStyle(changeStyle(it.p));
  P.fx = null; P.picking = null; P.fxDirty = false;
  map.getContainer().classList.remove("picking");
  refreshRow(it.cid);
  renderReviewProgress();
  if (P.open === it.cid) { renderInspector(); drawFix(it); }
  if (!silent) {
    const id = P.id, cid = it.cid, after = j.fix;
    record({
      block: id, cid, label: `${after ? "Fixed" : "Removed fix for"} ${itemText(it.p).title}`,
      undo: async () => { const x = P.items.find((i) => i.cid === cid); await saveFix(x, before, true); if (!before && beforeDec) await decide([cid], beforeDec, true); },
      redo: () => saveFix(P.items.find((i) => i.cid === cid), after, true),
    });
  }
  return true;
}

// related objects (orange outlines) and the fix's points (green) on the map
const fixLayer = L.layerGroup().addTo(map);
function drawFix(it) {
  fixLayer.clearLayers();
  if (it && !it.review) {
    // a moved change: the building it now goes on (green); building candidates while picking (indigo)
    for (const [ref, o] of Object.entries(it.p.decision === "moved" ? it.p.fix_objects || {} : {})) {
      if (o.geom) L.geoJSON(o.geom, { pane: "highlight", renderer: highlightRenderer, interactive: false,
        style: { color: "#14532d", weight: 3, fillColor: "#2f9e5b", fillOpacity: 0.35 } }).bindTooltip(`Moved here: ${ref}`, { sticky: true }).addTo(fixLayer);
    }
    for (const b of P.bcands?.cid === it.cid ? P.bcands.list : []) {
      L.geoJSON(b.geom, { pane: "highlight", renderer: highlightRenderer, interactive: false,
        style: { color: "#4f46e5", weight: 3, dashArray: "6 4", fillColor: "#4f46e5", fillOpacity: 0.12 } }).addTo(fixLayer);
    }
    return;
  }
  if (!it?.review) return;
  for (const [ref, o] of Object.entries(it.p.objects || {})) {
    if (!o.geom) continue;
    const changed = (P.fx?.objects && ref in P.fx.objects) || (it.p.fix?.objects && ref in it.p.fix.objects);
    L.geoJSON(o.geom, { pane: "highlight", renderer: highlightRenderer, interactive: false,
      pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 8 }),
      style: { color: changed ? "#2f9e5b" : "#e8902a", weight: 2.5, fillOpacity: 0.12, fillColor: changed ? "#2f9e5b" : "#e8902a" } })
      .bindTooltip(`${ref} · ${addrLine(o.tags)}`, { sticky: true }).addTo(fixLayer);
  }
  for (const x of siblings(it)) {
    const a = it.layer.getLatLng(), b2 = x.layer.getLatLng();
    L.polyline([a, b2], { pane: "highlight", renderer: highlightRenderer, color: "#7c3aed", weight: 2, dashArray: "5 5", interactive: false }).addTo(fixLayer);
    const bl = mainBuilding(x);
    if (bl?.[1].geom) L.geoJSON(bl[1].geom, { pane: "highlight", renderer: highlightRenderer, interactive: false,
      style: { color: "#7c3aed", weight: 2.5, fillOpacity: 0.1, fillColor: "#7c3aed" } }).addTo(fixLayer);
    L.circleMarker(b2, { pane: "highlight", renderer: highlightRenderer, radius: 11, color: "#7c3aed", weight: 2.5, fill: false, interactive: false })
      .bindTooltip(`Same address, ${Math.round(dist(it, x))} m away${bl?.[1].tags.name ? `: ${esc(bl[1].tags.name)}` : ""}`, { permanent: true, direction: "top", offset: [0, -10], className: "map-tag dup" })
      .addTo(fixLayer);
  }
  const pts = (P.fx && P.fxFor === it.cid ? P.fx : it.p.fix)?.points || [];
  pts.forEach((pt, k) => {
    const m = L.circleMarker([pt.lat, pt.lon], { pane: "highlight", renderer: highlightRenderer, radius: 7, color: "#14532d", weight: 2, fillColor: "#2f9e5b", fillOpacity: 1, interactive: false });
    m.bindTooltip(`New: ${esc(addrLine(pt.tags))}`, { permanent: true, direction: "right", offset: [8, 0], className: "map-tag new" }).addTo(fixLayer);
  });
}

// picking a location for a new point: the next map click sets it
map.on("click", (e) => {
  if (P?.picking == null || !P.fx) return;
  const pt = P.fx.points[P.picking];
  if (pt) { pt.lon = +e.latlng.lng.toFixed(7); pt.lat = +e.latlng.lat.toFixed(7); }
  P.picking = null; P.fxDirty = true; P.fxOpen = true;
  map.getContainer().classList.remove("picking");
  featureClickAt = Date.now();   // keep the inspector open
  const it = P.items.find((i) => i.cid === P.open);
  renderInspector(); drawFix(it);
});

// --- decisions ---------------------------------------------------------------------------

async function decide(cids, d, silent = false) {
  if (!P || !cids.length) return;
  const id = P.id;
  const before = new Map(cids.map((c) => [c, P.items.find((i) => i.cid === c)?.p.decision || null]));
  try {
    await api(`/api/blocks/${id}/decisions`, { ids: cids, decision: d || null });
  } catch (err) {
    toast(String(err).includes("409") ? "That POI already has another address put on it." : `Couldn't save: ${esc(err.message)}`, true);
    return false;
  }
  if (!P || P.id !== id) return;
  const set = new Set(cids);
  for (const it of P.items) {
    if (!set.has(it.cid)) continue;
    it.p.decision = d || null;
    it.layer?.setStyle(changeStyle(it.p));
  }
  if (cids.length > 1) renderGroups(); else refreshRow(cids[0]);
  if (P.open && set.has(P.open)) renderInspector();
  renderReviewProgress();
  if (!silent) {
    const it = P.items.find((i) => i.cid === cids[0]);
    const verb = !d ? "Cleared decision on" : d.startsWith("merge:") ? "Put on POI:" : { approved: "Approved", rejected: "Rejected", resolved: "Marked fixed:", ignored: "Left as is:" }[d];
    record({
      block: id, cid: cids.length === 1 ? cids[0] : null,
      label: `${verb} ${cids.length === 1 ? itemText(it.p).title : `${cids.length} changes`}`,
      undo: () => restoreDecisions(before),
      redo: () => decide(cids, d, true),
    });
  }
  return true;
}

async function restoreDecisions(before) {
  const byValue = new Map();
  for (const [c, v] of before) byValue.set(v, [...(byValue.get(v) || []), c]);
  for (const [v, cs] of byValue) await decide(cs, v, true);
}

async function setStatus(id, status, changeset = "", silent = false) {
  const before = byId.get(id)?.properties.status || "pending";
  const r = await api(`/api/blocks/${id}/status`, { status, changeset });
  if (!silent) {
    record({
      block: null, label: `Marked block ${id} ${LABEL[status].toLowerCase()}`,
      undo: async () => { await setStatus(id, before, "", true); await refreshAfterChange(id); },
      redo: async () => { await setStatus(id, status, changeset, true); await refreshAfterChange(id); },
    });
  }
  return r;
}

/** OSM's changeset list can lag a little behind the upload: look again a few times. */
function retryLink(id, delays = [15, 45, 120, 300]) {
  if (!delays.length) return;
  setTimeout(async () => {
    const p = byId.get(id)?.properties;
    if (p?.status !== "uploaded" || p.changeset_id) return;
    await syncUploads(false);
    if (!byId.get(id)?.properties.changeset_id) retryLink(id, delays.slice(1));
  }, delays[0] * 1000);
}

// --- undo / redo -----------------------------------------------------------------------------
// Every action pushes {label, undo, redo}. ⌘Z / ⇧⌘Z (Ctrl+Z / Ctrl+Y) walk the
// history; an entry that belongs to another block switches to that block first.

const undoStack = { undo: [], redo: [] };
let historyBusy = false;

function record(entry) {
  undoStack.undo.push(entry);
  if (undoStack.undo.length > 500) undoStack.undo.shift();
  undoStack.redo.length = 0;
}

async function travel(fromStack, toStack, verb) {
  if (historyBusy) return;
  const e = fromStack.pop();
  if (!e) return toast(`Nothing to ${verb.toLowerCase()}`);
  historyBusy = true;
  try {
    if (e.block != null && selected !== e.block) await select(e.block);
    await (verb === "Undo" ? e.undo() : e.redo());
    toStack.push(e);
    if (e.cid && P?.items.some((i) => i.cid === e.cid)) inspect(e.cid);
    toast(`${verb}: ${e.label}`);
  } catch (err) {
    toast(`${verb} failed: ${err.message}`, true);
  } finally {
    historyBusy = false;
  }
}
const undo = () => travel(undoStack.undo, undoStack.redo, "Undo");
const redo = () => travel(undoStack.redo, undoStack.undo, "Redo");

function toast(msg, bad = false) {
  const a = Object.assign(document.createElement("sl-alert"), { variant: bad ? "danger" : "neutral", closable: true, duration: bad ? 7000 : 2800 });
  a.innerHTML = `<sl-icon slot="icon" name="${bad ? "exclamation-octagon" : "check2-circle"}"></sl-icon>${msg}`;
  // keep the stack short when undo/redo is held down
  const stack = document.querySelector(".sl-toast-stack");
  if (stack && stack.children.length >= 3) stack.firstElementChild.remove();
  document.body.append(a);
  customElements.whenDefined("sl-alert").then(() => a.toast());
}

async function decideAndAdvance(it, d) {
  const order = visibleOrder();
  const i = order.findIndex((x) => x.cid === it.cid);
  const ok = await decide([it.cid], d);
  if (!ok || !d || d.startsWith("merge:")) return;
  const next = [...order.slice(i + 1), ...order.slice(0, Math.max(i, 0))]
    .find((x) => ["todo", "open"].includes(itemStatus(x.p)));
  const f = it.review ? P.rfilter : P.filter;
  if (f === "todo") renderGroups();
  if (next) inspect(next.cid);
}

function step(delta) {
  const order = visibleOrder();
  const i = order.findIndex((x) => x.cid === P.open);
  const n = order[i < 0 ? 0 : i + delta];
  if (n) inspect(n.cid);
}

document.addEventListener("keydown", (e) => {
  const mod = e.metaKey || e.ctrlKey;
  const typing = e.target.matches?.("input, textarea, sl-input, sl-textarea");
  if (document.querySelector("sl-dialog[open]")) return;
  // in a text field ⌘Z is the browser's own text undo
  if (mod && !typing && e.key.toLowerCase() === "z") { e.preventDefault(); return e.shiftKey ? redo() : undo(); }
  if (mod && !typing && e.key.toLowerCase() === "y") { e.preventDefault(); return redo(); }
  if (e.metaKey && e.key === "Enter" && P?.open && P.fxDirty) { e.preventDefault(); const it = P.items.find((i) => i.cid === P.open); return saveFix(it, manualFix(it)); }
  if (e.metaKey && e.key === "Enter" && P?.open && P.dirty) { e.preventDefault(); const it = P.items.find((i) => i.cid === P.open); return saveTags(it); }
  if (typing) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "c") { const cb = $("#show-changes"); cb.checked = !cb.checked; cb.checked ? changeLayer.addTo(map) : changeLayer.remove(); }
  if (e.key === "?") { $("#keys-dlg").show(); return; }
  if (e.key === "s") { toggleSatellite(); return; }
  if (e.key === "n") selectNext("ready") || selectNext("pending");
  if (e.key === "/") { e.preventDefault(); $("#bp-q")?.focus(); }
  if (e.key === "Escape") {
    if (P?.open) closeInspector(); else select(null);
  }
  if (!P?.open) return;
  const it = P.items.find((i) => i.cid === P.open);
  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); step(1); }
  if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); step(-1); }
  if (e.key === "e") { e.preventDefault(); document.querySelector(".tagtab input[data-k]")?.focus(); }
  if (it.review && /^[1-9]$/.test(e.key)) {
    const o = fixOptions(it)[+e.key - 1];
    if (o) applyOption(it, o);
    return;
  }
  if (it.review && e.key === "u" && it.p.fix) return saveFix(it, null);
  if (!it.review && it.p.kind === "tagged_building" && e.key === "m") { startBuildingPick(it); return; }
  if (it.review && e.key === "a") { if (!it.p.decision) skipItem(it); return; }
  if (["a", "r", "u"].includes(e.key)) {
    const d = it.review ? { a: "resolved", r: "ignored", u: "" }[e.key] : { a: "approved", r: "rejected", u: "" }[e.key];
    decideAndAdvance(it, d);
  }
});

map.on("click", () => { if (P?.open && !justClickedFeature()) closeInspector(); });

// --- selection ----------------------------------------------------------------------------

async function select(id, fit = true) {
  selected = id;
  blockLayer.setStyle((f) => blockStyle(f.properties));
  renderList();
  changeLayer.clearLayers();
  highlight.clearLayers();
  history.replaceState(null, "", id == null ? location.pathname : `#${id}`);
  if (id == null) { P = null; $("#detail").classList.add("hidden"); return; }
  if (P && P.id !== id) P = null;
  const f = byId.get(id);
  if (fit) map.fitBounds(L.geoJSON(f).getBounds(), { padding: [30, 30], animate: false });
  document.querySelector(`.block-row[data-id="${id}"]`)?.scrollIntoView({ block: "nearest" });
  await renderDetail(id);
}

function selectNext(status) {
  const ids = blocks.filter((f) => f.properties.status === status).map((f) => f.properties.id).sort((a, b) => a - b);
  const next = ids.find((i) => selected == null || i > selected) ?? ids[0];
  if (next != null) { select(next); return true; }
  return false;
}

async function refreshAfterChange(id) {
  await loadBlocks();
  if (selected === id) renderDetail(id);
}

function osmLinks(v) {
  return String(v || "").split(",").filter(Boolean).map((r) =>
    `<a href="https://www.openstreetmap.org/${esc(r)}" target="_blank">${esc(r)}</a>`).join(", ");
}

function josmZoomTo(it) {
  const ll = it.layer?.getLatLng ? it.layer.getLatLng() : it.layer?.getBounds().getCenter();
  if (!ll) return;
  const d = 0.0006;
  const ref = (it.p.osm || "").split(",")[0];
  const sel = ref ? `&select=${ref.replace("/", "")}` : "";
  josmCall(`/zoom?left=${ll.lng - d}&right=${ll.lng + d}&top=${ll.lat + d / 1.6}&bottom=${ll.lat - d / 1.6}${sel}`);
}
// --- data loading -----------------------------------------------------------------

async function loadBlocks() {
  const fc = await api("/api/blocks");
  blocks = fc.features;
  byId = new Map(blocks.map((f) => [f.properties.id, f]));
  blockLayer.clearLayers().addData(fc);
  updateLabels();
  renderProgress();
  renderList();
}

function renderProgress() {
  const c = Object.fromEntries(STATUS.map((s) => [s, 0]));
  let ready = 0, uploaded = 0;
  for (const f of blocks) {
    c[f.properties.status]++;
    if (f.properties.status === "ready") ready += f.properties.changes || 0;
    if (f.properties.status === "uploaded") uploaded += f.properties.changes || 0;
  }
  const total = blocks.length || 1;
  $("#progress").innerHTML = ["uploaded", "nothing_to_do", "ready", "skipped", "error"].map((s) =>
    `<div title="${LABEL[s]}: ${c[s]}" style="width:${(100 * c[s]) / total}%;background:${COLOR[s]}"></div>`).join("");
  const done = c.uploaded + c.nothing_to_do + c.skipped;
  $("#progress-text").innerHTML = `
    <span><b>${done}</b>/${blocks.length} blocks done</span>
    <span><i class="dot" style="background:${COLOR.ready}"></i><b>${c.ready}</b> ready · ${ready.toLocaleString()} changes</span>
    <span><i class="dot" style="background:${COLOR.uploaded}"></i><b>${uploaded.toLocaleString()}</b> uploaded</span>`;

  $("#status-filters").innerHTML = STATUS.filter((s) => c[s] || filters.has(s)).map((s) =>
    `<button class="chip ${filters.has(s) ? "on" : ""}" data-s="${s}"><span class="dot" style="background:${COLOR[s]}"></span>${LABEL[s]}<span class="n">${c[s]}</span></button>`).join("");
  document.querySelectorAll("#status-filters .chip").forEach((b) => b.onclick = () => {
    filters.has(b.dataset.s) ? filters.delete(b.dataset.s) : filters.add(b.dataset.s);
    renderProgress(); renderList();
  });
}

function jobState(id) {
  if (jobs.current && jobs.current.id === id) return "running";
  if (jobs.queue.includes(id)) return "queued";
  return null;
}

function renderList() {
  const q = $("#search").value.trim().toLowerCase();
  const rows = blocks.filter((f) => {
    const p = f.properties;
    if (filters.size && !filters.has(p.status)) return false;
    if (!q) return true;
    return String(p.id) === q || `${p.name} ${p.roads}`.toLowerCase().includes(q);
  }).slice(0, 600);
  $("#list-count").textContent = `${rows.length.toLocaleString()} block${rows.length === 1 ? "" : "s"}${filters.size || q ? " match" : ""}`;
  $("#block-list").innerHTML = rows.map((f) => {
    const p = f.properties;
    const js = jobState(p.id);
    const st = js || p.status;
    const pill = `<span class="pill ${st}">${js || LABEL[p.status]}</span>`;
    const meta = p.status === "pending" || js ? `<span class="m-dist">${p.dist_km} km</span>`
      : `<span title="changes"><sl-icon name="pencil-square"></sl-icon>${((p.changes ?? 0) - (p.rejected || 0)).toLocaleString()}</span>${p.review ? ((p.review_open ?? p.review)
        ? `<span class="m-rev" title="review items undecided"><sl-icon name="flag-fill"></sl-icon>${p.review_open ?? p.review}</span>`
        : `<span class="m-ok" title="all ${p.review} review items decided"><sl-icon name="flag-fill"></sl-icon><sl-icon name="check-lg"></sl-icon></span>`) : ""}`;
    return `<div class="block-row s-${st} ${p.id === selected ? "sel" : ""}" data-id="${p.id}">
      <span class="id">${p.id}</span>
      <span class="name"><b>${esc(p.name)}</b><small>${esc(p.roads)}</small></span>
      <span class="meta">${pill}<span class="m-n">${meta}</span></span></div>`;
  }).join("") || `<div class="empty"><sl-icon name="search"></sl-icon>No blocks match.</div>`;
  document.querySelectorAll(".block-row").forEach((r) => r.onclick = () => select(+r.dataset.id));
}
$("#search").addEventListener("sl-input", renderList);

// --- generation jobs -----------------------------------------------------------------

async function generate(ids, refresh) {
  await api("/api/generate", { ids, refresh });
  pollJobs();
}
$("#gen-next").onclick = async () => {
  await api("/api/generate", { next: +$("#gen-n").value || 5 });
  pollJobs();
};
$("#jobs-cancel").onclick = async () => { await api("/api/jobs/cancel", {}); pollJobs(); };

let pollTimer = null;
async function pollJobs() {
  clearTimeout(pollTimer);
  try {
    jobs = await api("/api/jobs");
  } catch { pollTimer = setTimeout(pollJobs, 5000); return; }
  const busy = jobs.current || jobs.queue.length;
  $("#jobs").classList.toggle("hidden", !busy && !jobs.log.length);
  $("#jobs-head").textContent = busy
    ? `Generating block ${jobs.current ? jobs.current.id : "…"}${jobs.queue.length ? ` · ${jobs.queue.length} queued` : ""}`
    : "Idle";
  $("#jobs-cancel").classList.toggle("hidden", !jobs.queue.length);
  $("#jobs-spin").classList.toggle("hidden", !busy);
  const log = $("#jobs-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
  log.textContent = jobs.log.slice(-60).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;

  const finished = jobs.done.filter((d) => d.finished > lastDone);
  if (finished.length) {
    lastDone = Math.max(...finished.map((d) => d.finished));
    await loadBlocks();
    if (selected != null && finished.some((d) => d.id === selected)) renderDetail(selected);
  } else {
    renderList();
  }
  pollTimer = setTimeout(pollJobs, busy ? 1500 : 6000);
}

// --- JOSM remote control ----------------------------------------------------------------

async function checkJosm() {
  const el = $("#josm-status"), tip = $("#josm-tip");
  try {
    const r = await fetch(`${JOSM}/version`, { signal: AbortSignal.timeout(1500) });
    const v = await r.json();
    el.className = "josm on"; tip.content = `JOSM remote control connected (protocol ${v.protocolversion?.major}.${v.protocolversion?.minor})`;
  } catch {
    el.className = "josm off"; tip.content = "JOSM not reachable on localhost:8111. Start JOSM and enable Preferences → Remote Control.";
  }
}
async function josmCall(path) {
  try {
    await fetch(`${JOSM}${path}`, { mode: "no-cors" });
  } catch {
    toast("Couldn't reach JOSM. Start JOSM and enable Preferences → Remote Control (and “Import data from URL”).", true);
  }
}
function josmImport(path, layer) {
  const url = new URL(path, location.origin).href;
  return josmCall(`/import?new_layer=true&layer_name=${encodeURIComponent(layer)}&url=${encodeURIComponent(url)}`);
}
async function openInJosm(id, p) {
  await josmImport(`/api/blocks/${id}/file/changeset.osm`, `Block ${id}: ${p.name}`);
  const b = L.geoJSON(byId.get(id)).getBounds();
  // pre-fill the upload dialog with this block's import changeset tags
  const tags = Object.entries((P?.id === id ? P.d.changeset_tags : null) || {})
    .filter(([k, v]) => v && !v.startsWith("<"))
    .map(([k, v]) => `${k}=${v.replaceAll("|", "/")}`).join("|");
  await josmCall(`/zoom?left=${b.getWest()}&right=${b.getEast()}&top=${b.getNorth()}&bottom=${b.getSouth()}` +
    (tags ? `&changeset_tags=${encodeURIComponent(tags)}` : ""));
}

// --- settings --------------------------------------------------------------------------

$("#settings-btn").onclick = async () => {
  const c = await api("/api/config");
  $("#cfg-wiki").value = c.wiki || "";
  $("#cfg-args").value = c.conflate_args || "";
  $("#cfg-user").value = c.osm_user || "";
  $("#settings").show();
};
$("#cfg-cancel").onclick = () => $("#settings").hide();
$("#cfg-save").onclick = async () => {
  await api("/api/config", { wiki: $("#cfg-wiki").value, conflate_args: $("#cfg-args").value, osm_user: $("#cfg-user").value });
  $("#settings").hide();
  toast("Settings saved");
  syncUploads(true);
};
$("#keys-btn").onclick = () => $("#keys-dlg").show();
$("#undo-btn").onclick = () => undo();
$("#redo-btn").onclick = () => redo();

/** Small modal in place of confirm()/prompt(). Resolves to the input value (or true) on OK, null on cancel. */
function ask({ title, body = "", input = null, ok = "OK", variant = "primary" }) {
  const dlg = $("#ask-dlg"), inp = $("#ask-input");
  dlg.label = title;
  $("#ask-body").innerHTML = body;
  inp.classList.toggle("hidden", input === null);
  if (input !== null) { inp.label = input.label || ""; inp.placeholder = input.placeholder || ""; inp.value = input.value || ""; }
  $("#ask-ok").textContent = ok;
  $("#ask-ok").variant = variant;
  return new Promise((resolve) => {
    let result = null;
    const done = (v) => { result = v; dlg.hide(); };
    $("#ask-ok").onclick = () => done(input === null ? true : inp.value);
    $("#ask-cancel").onclick = () => done(null);
    inp.onkeydown = (e) => { if (e.key === "Enter") done(inp.value); };
    const onHide = (e) => { if (e.target !== dlg) return; dlg.removeEventListener("sl-after-hide", onHide); resolve(result); };
    dlg.addEventListener("sl-after-hide", onHide);
    dlg.show();
    if (input !== null) setTimeout(() => inp.focus(), 50);
  });
}

// Blocks are uploaded from JOSM; the server matches the OSM user's changesets
// against ready blocks and marks them uploaded.
async function syncUploads(verbose = false) {
  let r;
  try { r = await api("/api/sync-uploads", {}); } catch { if (verbose) toast("Couldn't reach the OSM API", true); return; }
  if (!r.user) { if (verbose) toast("Set your OSM username in Settings to detect uploads"); return; }
  if (r.marked.length) {
    toast(r.marked.map((m) => m.was_ready ? `Block ${m.block} was uploaded in changeset ${m.changeset}` : `Block ${m.block}: found its changeset ${m.changeset}`).join("<br>"));
    await loadBlocks();
    if (selected != null) renderDetail(selected);
  } else if (verbose) toast(`No new uploads by ${esc(r.user)} found`);
}

// --- boot ----------------------------------------------------------------------------------

(async () => {
  // component properties (switch.checked, input.value) only exist once Shoelace has upgraded them
  await Promise.all(["sl-switch", "sl-input", "sl-button", "sl-radio-group", "sl-dialog"].map((t) => customElements.whenDefined(t)));
  await loadBlocks();
  if (!CENTER && blocks.length) map.fitBounds(blockLayer.getBounds(), { animate: false });
  pollJobs();
  checkJosm();
  syncUploads();
  setInterval(checkJosm, 15000);
  // deep links: #67 opens a block, #67/<change id> opens one change in it
  const hb = location.hash.slice(1).split("/")[0];
  const hashCid = location.hash.slice(1).split("/").slice(1).join("/");
  if (hb && byId.has(+hb)) {
    await select(+hb);
    if (hashCid) inspect(decodeURIComponent(hashCid));
  } else {
    const first = blocks.find((f) => f.properties.status === "ready");
    if (first) select(first.properties.id);
  }
})();
