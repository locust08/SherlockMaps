"""Selected campaigns must not leak into the default nationwide query plan."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import batch_collect_malaysia_v2 as batch
from collector_control import CollectorController


class SelectionTests(unittest.TestCase):
    def test_default_canary_does_not_resume_foreign_work_or_block_selected_campaign(self):
        from collection_selection import selection_path
        selection = dict(self.selection, custom_keywords=['car repair'], custom_industry='Automotive')
        selection_path(self.db).write_text(json.dumps(selection))
        conn = batch.open_db(self.db)
        foreign = batch.QueryTask('dentist in Bangsar, Malaysia', 'Health, Fitness & Wellness',
                                  'Bangsar', 'Federal Territory', 'dentist')
        batch.register_manifest(conn, [foreign])
        for state in ('running', 'needs_review'):
            canary = dict(started_at=batch.utc_now(), status=state, prompts=[foreign.prompt],
                          attempts=0, failures=0, completed=0, qualified_new=0)
            conn.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
                         (batch.SCOPE_CANARY_KEY, json.dumps(canary)))
            conn.execute('UPDATE search_jobs SET status="pending"')
            conn.commit()
            visited = []
            def crawl(task, database):
                visited.append(task.prompt)
                return batch.CrawlOutcome(task, processed_count=1)
            args = batch.make_parser().parse_args(['--database', str(self.db), '--target', '10',
                                                   '--workers', '1', '--max-workers', '1'])
            with self.subTest(state=state), patch.object(batch, 'crawl_task', side_effect=crawl), \
                    patch.object(batch, 'available_memory_gb', return_value=8), \
                    patch.object(batch.concurrent.futures, 'ProcessPoolExecutor', ThreadPoolExecutor):
                batch.run_batch(args)
                self.assertEqual(visited, ['car repair in Kota Kinabalu, Sabah, Malaysia'])
                saved = json.loads(conn.execute('SELECT value FROM metadata WHERE key=?',
                                                (batch.SCOPE_CANARY_KEY,)).fetchone()[0])
                self.assertEqual(saved, canary)
                status = json.loads(self.db.with_suffix('.sqlite.status.json').read_text())
                self.assertEqual(status['scope_canary']['status'], 'not_applicable')
        conn.close()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name).resolve() / 'leads.sqlite'
        self.selection = {'mode': 'selected', 'industries': ['Automotive'], 'states': ['Sabah'],
                          'cities': [{'state': 'Sabah', 'locality': 'Kota Kinabalu'}]}

    def tearDown(self):
        self.directory.cleanup()

    def test_selected_manifest_contains_only_requested_industry_and_city(self):
        tasks = batch.build_manifest(selection=self.selection)
        self.assertGreater(len(tasks), 0)
        self.assertEqual({task.sector for task in tasks}, {'Automotive'})
        self.assertEqual({(task.locality, task.state) for task in tasks}, {('Kota Kinabalu', 'Sabah')})
        self.assertTrue(all('Kota Kinabalu, Sabah, Malaysia' in task.prompt for task in tasks))
        self.assertTrue(all(batch.expand_task(task) == [] for task in tasks))

    def test_custom_keywords_replace_presets_and_use_assigned_industry(self):
        selection = dict(self.selection, custom_keywords=['  commercial refrigeration contractor  '],
                         custom_industry='Industrial & Manufacturing', cities=[],
                         custom_locations=[{'state': 'Sabah', 'locality': '  Inanam Industrial Park  '}])
        tasks = batch.build_manifest(selection=selection)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].prompt, 'commercial refrigeration contractor in Inanam Industrial Park, Sabah, Malaysia')
        self.assertEqual(tasks[0].sector, 'Industrial & Manufacturing')
        self.assertTrue(batch.is_relevant(tasks[0], 'ABC Cold Room Specialists', 'Refrigeration service'))
        self.assertFalse(batch.is_relevant(tasks[0], 'XYZ Restaurant', 'Restaurant'))
        self.assertEqual(batch.expand_task(tasks[0]), [])

    def test_custom_search_keeps_contact_and_closed_business_checks(self):
        task = batch.build_manifest(selection=dict(self.selection, custom_keywords=['refrigeration'],
                                   custom_industry='Industrial & Manufacturing'))[0]
        conn = batch.open_db(self.db)
        try:
            raw = {'name': 'ABC Cold Room', 'address': 'Inanam Malaysia', 'category': 'Refrigeration service'}
            self.assertEqual(batch.persist_observation(conn, task, raw), 'missing_contact')
            self.assertEqual(batch.persist_observation(conn, replace(task, prompt='closed fixture'),
                             dict(raw, phone='0312345678', is_closed=True)), 'permanently_closed')
        finally:
            conn.close()

    def test_state_selection_without_cities_uses_known_cities_in_that_state(self):
        tasks = batch.build_manifest(selection=dict(self.selection, cities=[]))
        self.assertGreater(len({task.locality for task in tasks}), 1)
        self.assertEqual({task.state for task in tasks}, {'Sabah'})

    def test_invalid_or_mismatched_selections_fail_instead_of_broadening_scope(self):
        for changes in ({'industries': []}, {'states': []}, {'industries': ['Government']},
                        {'states': ['Unknown']}, {'cities': [{'state': 'Selangor', 'locality': 'Shah Alam'}]},
                        {'cities': [{'state': 'Sabah', 'locality': 'Made Up City'}]},
                        {'custom_keywords': ['refrigeration'], 'custom_industry': 'Unknown'},
                        {'custom_keywords': 'not a list'}, {'mode': 'unknown'}):
            with self.assertRaises(ValueError, msg=changes):
                batch.build_manifest(selection=dict(self.selection, **changes))

    def test_completed_searches_are_skipped_but_unselected_old_work_is_not_resumed(self):
        conn = batch.open_db(self.db)
        try:
            tasks = batch.build_manifest(selection=self.selection)
            foreign = batch.QueryTask('dentist in Bangsar, Malaysia', 'Health, Fitness & Wellness',
                                      'Bangsar', 'Federal Territory', 'dentist')
            batch.register_manifest(conn, tasks + [foreign])
            conn.execute('UPDATE search_jobs SET status="completed" WHERE prompt=?', (tasks[0].prompt,))
            conn.commit()
            remaining = batch.build_manifest(conn, selection=self.selection)
            self.assertEqual(len(remaining), len(tasks) - 1)
            self.assertNotIn(tasks[0].prompt, [task.prompt for task in remaining])
            self.assertNotIn(foreign.prompt, [task.prompt for task in remaining])
        finally:
            conn.close()

    def test_preview_is_read_only_and_matches_the_selected_manifest(self):
        from collection_selection import preview_selection
        result = preview_selection(self.selection, self.db)
        self.assertFalse(self.db.exists())
        self.assertGreater(result['query_count'], 0)
        self.assertEqual(result['query_count'], len(batch.build_manifest(selection=self.selection)))
        self.assertTrue(all(item['state'] == 'Sabah' and item['industry'] == 'Automotive' for item in result['sample']))

    def test_default_preview_excludes_completed_or_skipped_child_queries(self):
        from collection_selection import preview_selection
        conn = batch.open_db(self.db)
        child = batch.QueryTask('completed child fixture', 'Automotive', 'Bangsar', 'Federal Territory',
                                'car repair', geo_level='district')
        skipped = replace(child, prompt='skipped child fixture')
        batch.register_manifest(conn, [child, skipped])
        conn.execute('UPDATE search_jobs SET status="completed" WHERE prompt=?', (child.prompt,))
        conn.execute('UPDATE search_jobs SET status="skipped" WHERE prompt=?', (skipped.prompt,))
        conn.commit()
        # Keep manifest deterministic while exercising preview's real job-status filter.
        with patch.object(batch, 'build_manifest', return_value=[child, skipped]):
            result = preview_selection({'mode': 'default'}, self.db)
        conn.close()
        self.assertEqual(result['query_count'], 0)
        self.assertEqual(result['sample'], [])

    def test_preview_includes_interrupted_searches_without_changing_status(self):
        from collection_selection import preview_selection
        conn = batch.open_db(self.db)
        task = batch.build_manifest(selection=self.selection)[0]
        batch.register_manifest(conn, [task])
        conn.execute('UPDATE search_jobs SET status="running" WHERE prompt=?', (task.prompt,))
        conn.commit()
        result = preview_selection(self.selection, self.db)
        self.assertEqual(result['query_count'], len(batch.build_manifest(conn, selection=self.selection)))
        self.assertEqual(conn.execute('SELECT status FROM search_jobs WHERE prompt=?',
                                     (task.prompt,)).fetchone()[0], 'running')
        conn.close()

    def test_external_cli_selection_is_saved_for_future_resumes(self):
        from collection_selection import load_selection
        conn = batch.open_db(self.db)
        task = batch.QueryTask('fixture', 'Automotive', 'Kota Kinabalu', 'Sabah', 'car repair')
        batch.persist_observation(conn, task, {'name': 'Fixture Auto', 'address': 'Kota Kinabalu Malaysia',
                                 'category': 'Car repair', 'phone': '0312345678', 'place_id': 'cli-fixture'})
        conn.close()
        external = self.db.parent / 'external-selection.json'
        external.write_text(json.dumps(self.selection))
        result = subprocess.run([sys.executable, str(batch.PROJECT_ROOT / 'batch_collect_malaysia.py'),
            '--database', str(self.db), '--target', '1', '--selection-file', str(external)],
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_selection(self.db)['states'], ['Sabah'])

    def test_selection_survives_real_collector_start_and_dashboard_restart(self):
        from collection_selection import selection_path
        conn = batch.open_db(self.db)
        raw = {'name': 'Fixture Auto', 'address': 'Kota Kinabalu Malaysia', 'category': 'Car repair',
               'phone': '0312345678', 'place_id': 'selection-fixture'}
        task = batch.QueryTask('fixture', 'Automotive', 'Kota Kinabalu', 'Sabah', 'car repair')
        batch.persist_observation(conn, task, raw)
        conn.close()
        controller = CollectorController(self.db, self.db.with_suffix('.status.json'))
        controller.start(1, 1, selection=self.selection)
        deadline = time.monotonic() + 20
        while controller.snapshot()['state'] in ('starting', 'running') and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(controller.snapshot()['state'], 'completed')
        self.assertEqual(json.loads(selection_path(self.db).read_text())['states'], ['Sabah'])
        replacement = CollectorController(self.db, self.db.with_suffix('.status.json'))
        self.assertEqual(replacement.snapshot()['selection']['states'], ['Sabah'])
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute('SELECT DISTINCT sector,state,locality FROM search_jobs').fetchall()
            self.assertEqual(rows, [('Automotive', 'Sabah', 'Kota Kinabalu')])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0], 1)
