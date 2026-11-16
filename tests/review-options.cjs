const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const src = fs.readFileSync('webapp/static/app.js', 'utf8');
const sandbox = { console, structuredClone, assert };
vm.createContext(sandbox);
vm.runInContext(`let P = {items: []}; const addrOf = t => t['addr:housenumber'] + ' ' + t['addr:street'];\n` +
  src.slice(src.indexOf('const hnOf ='), src.indexOf('function optionOn(')) + `
  const item = (id, hn, unit, ll = [-80, 35]) => ({cid: id, review: true,
    p: {reason: 'street_not_found_in_osm', county_address: hn + ' E CARSON BV' + (unit ? ' #' + unit : ''),
      county_tags: {'addr:housenumber': hn, 'addr:street': 'East Carson Boulevard'}, osm_suggestion: 'Carson Boulevard'},
    layer: {getLatLng: () => ({lng: ll[0], lat: ll[1]})}});
  assert.equal(flatRanges(['3', '4', '5', '7', '9', '10']), '3-5;7;9-10');
  assert.equal(flatRanges(['01', '02', 'A']), '01;02;A');
  P.items = [item('a', '111', ''), item('b', '111', '3'), item('c', '111', '4'), item('d', '225', '')];
  let o = stackOption(P.items[1]);
  assert.equal(o.states.length, 3);
  assert.equal(o.fix.points[0].tags['addr:flats'], '3-4');
  assert.equal(o.fix.points[0].tags['addr:unit'], undefined);
  assert.equal(o.states.filter(s => s.decision === 'resolved').length, 1);
  assert.equal(streetBatchOption(P.items[3]).states.length, 4);
  P.items[0].p.decision = 'resolved';
  assert.equal(streetBatchOption(P.items[3]).states.length, 1);
  P.items.push(item('other', '111', '5', [-80.001, 35]));
  assert.equal(stackedAddress(P.items[1]).length, 3);
  P.items.push(item('wrong', '226', ''));
  P.items.at(-1).p.osm_suggestion = 'Other Street';
  assert.equal(streetBatchOption(P.items[3]).states.length, 2);
`, sandbox);
console.log('Review option tests passed');
