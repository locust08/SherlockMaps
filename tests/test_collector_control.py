"""Dashboard control contracts, real process locks and cooperative pause."""

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import batch_collect_malaysia_v2 as batch
import progress_dashboard as dashboard


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.db = self.root / 'leads.sqlite'
        self.status = self.root / 'status.json'
        self.paths = patch.multiple(dashboard, DB_PATH=self.db, STATUS_PATH=self.status)
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.directory.cleanup()

    def request(self, path, method='GET', payload=None, authorized=True, extra_headers=None):
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        body = json.dumps(payload or {}).encode()
        handler.rfile = io.BytesIO(body)
        handler.headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        if authorized:
            handler.headers['X-Collector-Token'] = getattr(dashboard, 'COLLECTOR_TOKEN', '')
        handler.headers.update(extra_headers or {})
        self.response_code = 200
        self.response = None
        def respond(value, content_type):
            self.response = json.loads(value) if content_type.startswith('application/json') else value
        def error(code, *args):
            self.response_code = code
        handler.respond = respond
        handler.send_error = error
        handler.send_response = lambda code: setattr(self, 'response_code', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        (handler.do_GET if method == 'GET' else handler.do_POST)()
        if self.response is None and handler.wfile.getvalue():
            self.response = json.loads(handler.wfile.getvalue())
        return self.response_code, self.response

    def test_status_reports_stopped_not_running_on_fresh_device(self):
        code, data = self.request('/api/collector')
        self.assertEqual(code, 200)
        self.assertEqual(data['state'], 'stopped')
        self.assertEqual(data['database'], str(self.db))

    def test_preview_endpoint_validates_scope_without_creating_leads(self):
        selection = {'mode': 'selected', 'industries': ['Automotive'], 'states': ['Sabah'],
                     'cities': [{'state': 'Sabah', 'locality': 'Kota Kinabalu'}]}
        code, data = self.request('/api/collector/preview', 'POST', {'selection': selection})
        self.assertEqual(code, 200)
        self.assertGreater(data['query_count'], 0)
        self.assertTrue(all(row['state'] == 'Sabah' for row in data['sample']))
        self.assertFalse(self.db.exists())
        code, _ = self.request('/api/collector/preview', 'POST', authorized=False)
        self.assertEqual(code, 403)

    def test_preview_accepts_larger_valid_selection_requests(self):
        selection = {'mode': 'selected', 'industries': ['Automotive'], 'states': ['Sabah'],
                     'cities': [{'state': 'Sabah', 'locality': 'Kota Kinabalu'}]}
        code, _ = self.request('/api/collector/preview', 'POST', {'selection': selection, 'padding': 'x' * 5000})
        self.assertEqual(code, 200)

    def test_pause_is_persistent_and_does_not_create_a_database(self):
        code, data = self.request('/api/collector/pause', 'POST')
        self.assertEqual(code, 200)
        self.assertEqual(data['state'], 'paused')
        self.assertFalse(self.db.exists())
        _, data = self.request('/api/collector')
        self.assertEqual(data['state'], 'paused')

    def test_control_requests_require_same_origin_and_token(self):
        code, _ = self.request('/api/collector/pause', 'POST', authorized=False)
        self.assertEqual(code, 403)
        for headers in ({'Origin': 'https://attacker.example'}, {'Host': 'attacker.example:8765'},
                        {'X-Collector-Token': 'wrong'}):
            code, _ = self.request('/api/collector/pause', 'POST', extra_headers=headers)
            self.assertEqual(code, 403)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_malformed_control_body_is_rejected(self):
        for headers in ({'Content-Length': '70000'}, {'Content-Type': 'text/plain'}):
            code, data = self.request('/api/collector/start', 'POST', extra_headers=headers)
            self.assertEqual(code, 400)
            self.assertIn('error', data)
        code, data = self.request('/api/collector/start', 'POST', payload=['invalid'])
        self.assertEqual(code, 400)

    def test_invalid_start_settings_do_not_spawn_collector(self):
        for payload in ({'target': 0, 'workers': 1}, {'target': 10, 'workers': 7},
                        {'target': 'shell command', 'workers': 1}, {'target': True, 'workers': 1}):
            code, _ = self.request('/api/collector/start', 'POST', payload)
            self.assertEqual(code, 400, payload)
        self.assertFalse(self.db.exists())

    def test_dashboard_contains_actionable_controls(self):
        page = dashboard.dashboard_page(dashboard._empty_dashboard_data())
        self.assertIn('id="collector-start"', page)
        self.assertIn('id="collector-pause"', page)
        self.assertIn('/api/collector/', page)

    def test_lock_prevents_two_collectors_and_recovers_after_release(self):
        from collector_control import CollectorLock, CollectorBusy, CollectorController
        with CollectorLock(self.db):
            controller = CollectorController(self.db, self.status)
            self.assertEqual(controller.snapshot()['state'], 'running')
            with self.assertRaises(CollectorBusy):
                with CollectorLock(self.db):
                    self.fail('Second collector acquired the same database lock')
            result = controller.start(10, 1)
            self.assertEqual(result['state'], 'running')
            self.assertFalse(self.db.exists())
        self.assertEqual(controller.snapshot()['state'], 'stopped')
        with CollectorLock(self.db):
            self.assertEqual(controller.pause()['state'], 'pausing')
        self.assertEqual(controller.snapshot()['state'], 'paused')

    def test_real_launch_uses_dashboard_database_and_preserves_leads(self):
        from collector_control import CollectorController
        connection = batch.open_db(self.db)
        task = batch.QueryTask('dentist Bangsar', 'Health, Fitness & Wellness', 'Bangsar',
                               'Federal Territory', 'dentist')
        raw = {'name': 'Fixture Dental', 'address': 'Bangsar Malaysia', 'category': 'Dental clinic',
               'phone': '0312345678', 'place_id': 'fixture-control-id'}
        batch.persist_observation(connection, task, raw)
        lead_id = connection.execute('SELECT id FROM companies').fetchone()[0]
        connection.close()
        # Target is already met: exercise real startup without accessing Google.
        controller = CollectorController(self.db, self.status)
        self.assertIn(controller.start(1, 1)['state'], ('starting', 'running', 'completed'))
        deadline = time.monotonic() + 30
        while controller.snapshot()['state'] in ('starting', 'running') and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(controller.snapshot()['state'], 'completed')
        payload = json.loads(self.status.read_text())
        self.assertEqual(payload['database'], str(self.db))
        self.assertEqual(payload['preferred_workers'], 1)
        self.assertEqual(payload['maximum_workers'], 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT id FROM companies').fetchall(), [(lead_id,)])

    def test_simultaneous_start_requests_launch_one_process(self):
        from collector_control import CollectorController
        controller = CollectorController(self.db, self.status)
        # Substitute only the long-lived external process, retaining control logic.
        class Process:
            pid = 12345
            def poll(self):
                return None
        launches = []
        def launch(*args, **kwargs):
            launches.append(args)
            return Process()
        with patch('collector_control.subprocess.Popen', side_effect=launch):
            with ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(lambda _: controller.start(10, 1)['state'], range(8)))
        self.assertEqual(len(launches), 1)
        self.assertEqual(states, ['starting'] * 8)

    def test_new_dashboard_instance_recognizes_launch_in_progress(self):
        from collector_control import CollectorController
        class Process:
            pid = 12345
            def poll(self):
                return None
        with patch('collector_control.subprocess.Popen', return_value=Process()) as spawn:
            first = CollectorController(self.db, self.status)
            second = CollectorController(self.db, self.status)
            self.assertEqual(first.start(10, 1)['state'], 'starting')
            self.assertEqual(second.start(10, 1)['state'], 'starting')
            self.assertEqual(spawn.call_count, 1)

    def test_spawn_error_is_visible_and_retryable(self):
        from collector_control import CollectorController
        controller = CollectorController(self.db, self.status)
        with patch('collector_control.subprocess.Popen', side_effect=OSError('fixture launch failed')):
            self.assertEqual(controller.start(10, 1)['state'], 'failed')
        self.assertIn('fixture launch failed', controller.snapshot()['error'])

    def test_abandoned_startup_reservation_is_reported_as_failure(self):
        from collector_control import CollectorController, write_state
        write_state(self.db, 'starting', requested_at=time.time() - 60)
        self.assertEqual(CollectorController(self.db, self.status).snapshot()['state'], 'failed')

    def test_lifecycle_writes_from_independent_launchers_are_atomic(self):
        from collector_control import write_state, read_state
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: write_state(self.db, 'running', pid=i), range(40)))
        state = read_state(self.db)
        self.assertEqual(state['state'], 'running')
        self.assertIn(state['pid'], range(40))

    def test_cli_respects_pause_before_startup_and_exclusivity(self):
        from collector_control import CollectorController, CollectorLock
        controller = CollectorController(self.db, self.status)
        controller.pause()
        command = [sys.executable, str(dashboard.ROOT / 'batch_collect_malaysia.py'),
                   '--database', str(self.db), '--status-file', str(self.status)]
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.db.exists())
        with CollectorLock(self.db):
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 3, result.stderr)


class CooperativePauseTests(unittest.TestCase):
    def test_pause_finishes_active_query_without_submitting_next_one(self):
        from collector_control import CollectorController
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            db, status = root / 'leads.sqlite', root / 'status.json'
            tasks = [batch.QueryTask(f'fixture {i}', 'Health, Fitness & Wellness', 'Bangsar',
                                     'Federal Territory', 'dentist') for i in range(2)]
            controller = CollectorController(db, status)
            completed = []
            # Replace browser/process work only; use real scheduler and SQLite.
            def crawl(task, path):
                self.assertEqual(path, str(db))
                controller.pause()
                completed.append(task.prompt)
                return batch.CrawlOutcome(task, processed_count=1)
            args = batch.make_parser().parse_args(['--database', str(db), '--status-file', str(status),
                                                   '--target', '10', '--workers', '1', '--max-workers', '1'])
            with patch.object(batch, 'build_manifest', return_value=tasks), \
                    patch.object(batch, 'crawl_task', side_effect=crawl), \
                    patch.object(batch.concurrent.futures, 'ProcessPoolExecutor', ThreadPoolExecutor):
                self.assertEqual(batch.run_batch(args), 0)
            self.assertEqual(completed, ['fixture 0'])
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT status FROM search_jobs ORDER BY prompt').fetchall(),
                                 [('completed',), ('pending',)])
            self.assertEqual(controller.snapshot()['state'], 'paused')
            # Resume must skip the completed first query and finish the second.
            from collector_control import pause_path
            pause_path(db).unlink()
            def resume_crawl(task, path):
                completed.append(task.prompt)
                return batch.CrawlOutcome(task, processed_count=1)
            with patch.object(batch, 'build_manifest', return_value=tasks), \
                    patch.object(batch, 'crawl_task', side_effect=resume_crawl), \
                    patch.object(batch.concurrent.futures, 'ProcessPoolExecutor', ThreadPoolExecutor):
                self.assertEqual(batch.run_batch(args), 2)  # Small fixture manifest exhausted.
            self.assertEqual(completed, ['fixture 0', 'fixture 1'])
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT status FROM search_jobs ORDER BY prompt').fetchall(),
                                 [('completed',), ('completed',)])
