"""First-use and sales-edit browser journey against isolated data only."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
import progress_dashboard as dashboard
import batch_collect_malaysia_v2 as batch


class DashboardUXBrowserTests(unittest.TestCase):
    def test_preview_review_assign_and_record_outcome_on_desktop_and_mobile(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'ux.sqlite'
            conn = batch.open_db(db)
            task = batch.QueryTask('fixture auto', 'Automotive', 'Petaling Jaya', 'Selangor', 'car repair')
            batch.persist_observation(conn, task, {'name': 'Fixture Auto', 'address': 'PJ Malaysia',
                'category': 'Car repair', 'phone': '0312345678', 'place_id': 'ux-browser'})
            company_id = conn.execute('SELECT id FROM companies').fetchone()[0]
            conn.close()
            with patch.multiple(dashboard, DB_PATH=db, STATUS_PATH=db.with_suffix('.status.json'),
                                _collector_controller=None, _dashboard_cache=None, _dashboard_cache_at=0):
                server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as runtime:
                        browser = runtime.chromium.launch(headless=True)
                        page = browser.new_page(viewport={'width':1440, 'height':1000})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        base = f'http://127.0.0.1:{server.server_port}'
                        page.goto(base)
                        expect(page.locator('#environment-notice')).to_contain_text('Temporary database')
                        expect(page.locator('#collector-start')).to_be_disabled()
                        expect(page.locator('#advanced-diagnostics')).not_to_have_attribute('open', '')
                        page.locator('#collector-mode').select_option('selected')
                        page.locator('#collector-industries').select_option('Automotive')
                        page.locator('#collector-states').select_option('Selangor')
                        page.locator('#collector-cities').select_option(label='Petaling Jaya — Selangor')
                        page.locator('#collector-preview').click()
                        expect(page.locator('#collector-query-preview')).to_contain_text('Petaling Jaya')
                        expect(page.locator('#collector-start')).to_be_enabled()
                        page.locator('#collector-states').select_option('Sabah')
                        expect(page.locator('#collector-start')).to_be_disabled()
                        # No collection started; navigation and edits below use only fixture sales data.
                        page.goto(base + '/leads')
                        expect(page.locator('.desktop-leads th')).to_have_count(7)
                        page.get_by_role('link', name='Review Fixture Auto', exact=True).first.click()
                        expect(page.get_by_role('heading', name='Fixture Auto', exact=True)).to_be_visible()
                        page.get_by_label('Assigned salesperson').fill('Aisha')
                        page.get_by_label('Sales status', exact=True).select_option('CONTACTED')
                        page.get_by_label('Contact notes').fill('Interested; follow up tomorrow')
                        page.get_by_label('Record a contact made outside this dashboard').check()
                        page.get_by_role('button', name='Save sales update', exact=True).click()
                        expect(page.locator('#sales-result')).to_contain_text('Saved')
                        page.reload()
                        expect(page.get_by_label('Assigned salesperson')).to_have_value('Aisha')
                        expect(page.get_by_label('Sales status', exact=True)).to_have_value('CONTACTED')
                        expect(page.get_by_label('Contact notes')).to_have_value('Interested; follow up tomorrow')
                        expect(page.locator('#activity-history')).to_contain_text('Interested; follow up tomorrow')
                        page.goto(base + '/pipeline')
                        expect(page.locator('#pipeline-leads')).to_contain_text('Aisha')
                        expect(page.locator('#conversion-analytics')).not_to_have_attribute('open', '')
                        page.set_viewport_size({'width':390,'height':844})
                        page.goto(base + '/leads')
                        expect(page.locator('.desktop-leads')).not_to_be_visible()
                        expect(page.locator('.mobile-leads')).to_be_visible()
                        expect(page.locator('.mobile-leads')).to_contain_text('0312345678')
                        self.assertLessEqual(page.evaluate('document.body.scrollWidth'),390)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
