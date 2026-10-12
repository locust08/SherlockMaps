"""UX contracts: honest memory and transactional direct sales editing."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import batch_collect_malaysia_v2 as batch
import progress_dashboard as dashboard
import test_collector_control as controls
import system_memory


class MemoryUXTests(unittest.TestCase):
    def test_non_windows_memory_is_not_an_invented_99gb(self):
        vm = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 32768.\nPages inactive: 32768.\nPages speculative: 0.\n'
        with patch('system_memory.platform.system', return_value='Darwin'), \
                patch('system_memory.subprocess.run', return_value=SimpleNamespace(stdout=vm)):
            self.assertEqual(batch.available_memory_gb(), 1.0)

    def test_linux_uses_available_not_total_memory(self):
        vm = 'MemTotal:       16777216 kB\nMemFree:          65536 kB\nMemAvailable:  2097152 kB\n'
        with patch('system_memory.platform.system', return_value='Linux'), \
                patch('system_memory.Path.read_text', return_value=vm):
            result = system_memory.available_memory()
            self.assertEqual(result['available_ram_gb'], 2.0)
            self.assertEqual(result['ram_operating_state'], 'healthy')

    def test_failed_memory_detection_is_unavailable_and_blocks_new_submissions(self):
        with patch('system_memory.platform.system', return_value='Darwin'), \
                patch('system_memory.subprocess.run', side_effect=OSError('measurement unavailable')):
            self.assertIsNone(system_memory.available_memory()['available_ram_gb'])
            self.assertEqual(system_memory.available_memory()['ram_operating_state'], 'unknown')
            self.assertFalse(batch.submission_allowed(batch.available_memory_gb(), 100, 0))
            self.assertLess(batch.desired_worker_count(batch.available_memory_gb(), current_limit=5),5)

    def test_legacy_status_does_not_display_fake_memory_as_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / 'status.json'
            status.write_text(json.dumps({'available_ram_gb': 99.0, 'ram_operating_state': 'healthy'}))
            with patch.object(dashboard, 'STATUS_PATH', status):
                result = dashboard.load_status()
            self.assertNotEqual(result['available_ram_gb'], 99.0)
            self.assertIn('memory_source', result)


class SalesUXTests(unittest.TestCase):
    request = controls.ControlTests.request

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'leads.sqlite'
        self.paths = patch.multiple(dashboard, DB_PATH=self.db,
                                    STATUS_PATH=self.db.with_suffix('.status.json'),
                                    _collector_controller=None, _dashboard_cache=None)
        self.paths.start()
        conn = batch.open_db(self.db)
        task = batch.QueryTask('fixture auto', 'Automotive', 'Petaling Jaya', 'Selangor', 'car repair')
        batch.persist_observation(conn, task, {'name': 'Fixture Auto', 'address': 'PJ Malaysia',
                                'category': 'Car repair', 'phone': '0312345678', 'place_id': 'ux-auto'})
        self.org, self.version = conn.execute('SELECT organization_id,updated_at FROM sales_leads').fetchone()
        self.company = conn.execute('SELECT id FROM companies').fetchone()[0]
        conn.close()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def payload(self, **fields):
        return {'organization_id': self.org, 'expected_updated_at': self.version,
                'status': 'CONTACTED', 'assigned_pic': 'Aisha', 'notes': 'Asked for a proposal',
                'record_contact': True, **fields}

    def test_direct_update_saves_assignment_and_contact_activity(self):
        code, _ = self.request('/api/sales/update', 'POST', self.payload())
        self.assertEqual(code, 200)
        conn = batch.open_db(self.db)
        row = conn.execute('SELECT status,assigned_pic,contact_count,notes FROM sales_leads').fetchone()
        self.assertEqual(row, ('CONTACTED', 'Aisha', 1, 'Asked for a proposal'))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sales_activities').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0], 1)
        conn.close()

    def test_repeat_contacts_increment_once_each_and_zero_proposal_value_is_saved(self):
        self.assertEqual(self.request('/api/sales/update', 'POST', self.payload(proposal_value='100'))[0],200)
        conn = batch.open_db(self.db)
        self.version = conn.execute('SELECT updated_at FROM sales_leads').fetchone()[0]
        conn.close()
        self.assertEqual(self.request('/api/sales/update', 'POST', self.payload(proposal_value='0'))[0],200)
        conn = batch.open_db(self.db)
        self.assertEqual(conn.execute('SELECT contact_count,proposal_value FROM sales_leads').fetchone(),(2,0))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sales_activities').fetchone()[0],2)
        conn.close()

    def test_notes_only_save_preserves_contact_time_and_can_clear_proposal_amount(self):
        self.assertEqual(self.request('/api/sales/update', 'POST', self.payload(proposal_value='5000'))[0],200)
        conn = batch.open_db(self.db)
        self.version, contact_time = conn.execute('SELECT updated_at,last_contact_at FROM sales_leads').fetchone()
        conn.close()
        self.assertEqual(self.request('/api/sales/update', 'POST', self.payload(record_contact=False, proposal_value='', notes='Assigned follow-up'))[0],200)
        conn = batch.open_db(self.db)
        self.assertEqual(conn.execute('SELECT last_contact_at,contact_count,proposal_value FROM sales_leads').fetchone(),(contact_time,1,None))
        conn.close()

    def test_fresh_device_pages_explain_next_step_without_creating_database(self):
        missing = self.db.parent / 'not-created.sqlite'
        with patch.object(dashboard, 'DB_PATH', missing):
            for route in ('/leads','/pipeline','/coverage','/searches'):
                code, html = self.request(route)
                self.assertEqual(code,200)
                self.assertIn('No leads yet',html)
            self.assertEqual(self.request('/lead?id=1')[0],404)
            self.assertEqual(self.request('/api/sales/update','POST',self.payload())[0],400)
            self.assertFalse(missing.exists())

    def test_followup_timezone_is_saved_and_restored_in_malaysia_time(self):
        self.assertEqual(self.request('/api/sales/update','POST',self.payload(next_action_at='2026-10-06T09:30:00+08:00'))[0],200)
        conn = batch.open_db(self.db)
        value = conn.execute('SELECT next_action_at FROM sales_leads').fetchone()[0]
        conn.close()
        self.assertEqual(value,'2026-10-06T09:30:00+08:00')
        self.assertEqual(dashboard.myt_input('2026-10-06T01:30:00+00:00'),'2026-10-06T09:30')
        self.assertIn('2026-10-06T09:30',self.request('/lead?id='+str(self.company))[1])

    def test_direct_updates_require_token_and_reject_stale_edits(self):
        code, _ = self.request('/api/sales/update', 'POST', self.payload(), authorized=False)
        self.assertEqual(code, 403)
        code, _ = self.request('/api/sales/update', 'POST', self.payload())
        self.assertEqual(code, 200)
        code, _ = self.request('/api/sales/update', 'POST', self.payload(notes='stale overwrite'))
        self.assertEqual(code, 409)
        conn = batch.open_db(self.db)
        self.assertEqual(conn.execute('SELECT notes FROM sales_leads').fetchone()[0], 'Asked for a proposal')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sales_activities').fetchone()[0], 1)
        conn.close()

    def test_direct_update_rejects_invalid_financial_and_status_values_without_writes(self):
        for field in ({'status': 'BAD'}, {'proposal_value': '-10'}, {'proposal_value': 'NaN'},
                      {'notes': 'x' * 5000}, {'record_contact': 'false'}):
            code, _ = self.request('/api/sales/update', 'POST', self.payload(**field))
            self.assertEqual(code, 400, field)
        conn = batch.open_db(self.db)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sales_activities').fetchone()[0], 0)
        conn.close()

    def test_do_not_contact_cannot_be_reactivated_by_direct_form(self):
        code, _ = self.request('/api/sales/update', 'POST', self.payload(status='DO_NOT_CONTACT', record_contact=False))
        self.assertEqual(code, 200)
        conn = batch.open_db(self.db)
        self.version = conn.execute('SELECT updated_at FROM sales_leads').fetchone()[0]
        conn.close()
        code, _ = self.request('/api/sales/update', 'POST', self.payload())
        self.assertEqual(code, 400)
        self.assertEqual(self.request('/api/sales/update', 'POST', self.payload(status='DO_NOT_CONTACT', record_contact=False, notes='Opt-out confirmed'))[0], 200)

    def test_detail_page_is_available_for_review_and_unknown_leads_return_404(self):
        code, html = self.request('/lead?id=' + str(self.company))
        self.assertEqual(code, 200)
        self.assertIn('Fixture Auto', html)
        self.assertIn('Save sales update', html)
        self.assertEqual(self.request('/lead?id=999999')[0], 404)

    def test_coverage_includes_selected_non_core_market_and_can_hide_unstarted_queries(self):
        conn = batch.open_db(self.db)
        task = batch.QueryTask('Sabah fixture', 'Automotive', 'Kota Kinabalu', 'Sabah', 'car repair')
        batch.persist_observation(conn, task, {'name': 'Sabah Auto', 'address': 'KK Malaysia',
                                'category': 'Car repair', 'phone': '0312345679', 'place_id': 'ux-sabah'})
        batch.register_manifest(conn, [task])
        conn.close()
        _, html = self.request('/coverage')
        self.assertIn('Sabah Auto', self.request('/leads')[1])
        self.assertIn('<td>Sabah</td>', html)
        self.assertIn('Planned searches', html)
        code, started = self.request('/coverage?started=1')
        self.assertEqual(code,200)
        self.assertNotIn('Estimated A/B per search</th></tr></thead><tbody><tr><td>Sabah</td>', started)
