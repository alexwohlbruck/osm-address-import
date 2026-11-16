import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webapp import server


class ReviewBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bdir = Path(self.tmp.name)
        self.path = self.bdir / 'out' / '0001'
        self.path.mkdir(parents=True)
        self.patch = patch.object(server, 'BDIR', self.bdir)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for name, value in [('changeset_review.geojson', {'features': [
            {'properties': {'county_id': str(n), 'reason': 'street_not_found_in_osm'}} for n in range(3)]}),
            ('fixes.json', {}), ('decisions.json', {})]:
            (self.path / name).write_text(json.dumps(value))
        (self.path / 'changeset.osm').write_text('<osm version="0.6"/>')

    def state(self, n, number='111', decision='resolved'):
        return {'cid': f'review:{n}:street_not_found_in_osm', 'decision': decision,
                'fix': {'objects': {}, 'points': [{'lon': -80, 'lat': 35, 'tags': {
                    'addr:housenumber': number, 'addr:street': 'Carson Boulevard'}}]}}

    def test_duplicate_batch_rejected_without_partial_save(self):
        result = server.review_batch(1, [self.state(0), self.state(1)])
        self.assertEqual(result['error'], 'duplicate')
        self.assertEqual(server.load_fixes(1), {})
        self.assertEqual(server.load_decisions(1), {})

    def test_consolidate_existing_duplicates_export_once_and_undo(self):
        states = [self.state(0), self.state(1)]
        server.save_fixes(1, {s['cid']: s['fix'] for s in states})
        server.save_decisions(1, {s['cid']: 'resolved' for s in states})
        expected = states
        after = [self.state(0), {'cid': states[1]['cid'], 'fix': None, 'decision': 'ignored'}]
        result = server.review_batch(1, after, expected)
        self.assertTrue(result['ok'])
        root = ET.fromstring(server.filtered_changeset(1))
        self.assertEqual(len(root.findall('node')), 1)
        # Restoring invalid pre-existing duplicates must fail validation, preserving the good state.
        self.assertEqual(server.review_batch(1, states, result['states'])['error'], 'duplicate')
        self.assertEqual(len(ET.fromstring(server.filtered_changeset(1)).findall('node')), 1)
        self.assertTrue(server.review_batch(1, states, result['states'], result['history_token'])['ok'])
        self.assertEqual(len(ET.fromstring(server.filtered_changeset(1)).findall('node')), 2)
        self.assertTrue(server.review_batch(1, after, states, result['history_token'])['ok'])

    def test_batch_undo_redo_and_stale_state(self):
        before = [{'cid': self.state(n)['cid'], 'fix': None, 'decision': None} for n in range(2)]
        after = [self.state(0), self.state(1, '225')]
        self.assertTrue(server.review_batch(1, after, before)['ok'])
        self.assertIn('error', server.review_batch(1, before, before))
        self.assertTrue(server.review_batch(1, before, after)['ok'])
        self.assertEqual(server.load_fixes(1), {})
        self.assertTrue(server.review_batch(1, after, before)['ok'])
        self.assertEqual(len(ET.fromstring(server.filtered_changeset(1)).findall('node')), 2)

    def test_bad_item_and_oversized_tag_do_not_write(self):
        bad = self.state(0)
        bad['fix']['points'][0]['tags']['addr:flats'] = '1' * 256
        with self.assertRaises(ValueError):
            server.review_batch(1, [bad])
        with self.assertRaises(ValueError):
            server.review_batch(1, [self.state(8)])
        self.assertEqual(server.load_fixes(1), {})


if __name__ == '__main__':
    unittest.main()
