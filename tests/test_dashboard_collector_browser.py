"""Regression: controls must initialize after cards exist and keep polling."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

import progress_dashboard as dashboard
import batch_collect_malaysia_v2 as batch
from collector_control import write_json
from collection_selection import selection_path


class CollectorBrowserTests(unittest.TestCase):
    def test_controls_initialize_and_poll_lifecycle_without_javascript_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.multiple(dashboard, DB_PATH=root / 'leads.sqlite', STATUS_PATH=root / 'status.json',
                                _collector_controller=None, _dashboard_cache=None, _dashboard_cache_at=0):
                server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as runtime:
                        browser = runtime.chromium.launch(headless=True)
                        try:
                            page = browser.new_page()
                            errors = []
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            page.goto(f'http://127.0.0.1:{server.server_port}')
                            self.assertEqual(errors, [])
                            expect(page.locator('#collector-start')).to_be_disabled()
                            expect(page.locator('#collector-pause')).to_be_disabled()
                            self.assertEqual(errors, [])
                            page.locator('#collector-mode').select_option('selected')
                            page.locator('#collector-industries').select_option('Automotive')
                            page.locator('#collector-states').select_option('Sabah')
                            page.locator('#collector-cities').select_option(label='Kota Kinabalu — Sabah')
                            page.locator('#collector-preview').click()
                            expect(page.locator('#collector-query-preview')).to_contain_text('Kota Kinabalu, Sabah, Malaysia')
                            self.assertNotIn('Selangor', page.locator('#collector-query-preview').inner_text())
                            self.assertFalse(dashboard.DB_PATH.exists())
                            # Custom terms replace presets; unknown areas retain their state.
                            page.locator('#collector-cities').select_option([])
                            page.locator('#collector-keywords').fill('commercial refrigeration contractor')
                            page.locator('#collector-custom-industry').select_option('Industrial & Manufacturing')
                            page.locator('#collector-areas').fill('Inanam Industrial Park')
                            page.locator('#collector-preview').click()
                            expect(page.locator('#collector-query-preview')).to_contain_text('1 pending searches')
                            expect(page.locator('#collector-query-preview')).to_contain_text(
                                'commercial refrigeration contractor in Inanam Industrial Park, Sabah, Malaysia')
                            # Exercise real launch with an already-met target, without live Maps requests.
                            conn = batch.open_db(dashboard.DB_PATH)
                            fixture = batch.QueryTask('fixture', 'Industrial & Manufacturing', 'Inanam', 'Sabah',
                                                      'refrigeration', strategy_bucket='custom')
                            batch.persist_observation(conn, fixture, {'name': 'Fixture Cold Room',
                                'address': 'Inanam Malaysia', 'category': 'Refrigeration service',
                                'phone': '0312345678', 'place_id': 'browser-selection-fixture'})
                            conn.close()
                            page.locator('#collector-target').fill('1')
                            page.get_by_text('Advanced collection settings', exact=True).click()
                            page.locator('#collector-workers').fill('1')
                            page.locator('#collector-preview').click()
                            expect(page.locator('#collector-start')).to_be_enabled()
                            page.locator('#collector-start').click()
                            expect(page.locator('#collector-control-state')).to_have_text('completed', timeout=30000)
                            page.reload()
                            expect(page.locator('#collector-mode')).to_have_value('selected')
                            expect(page.locator('#collector-keywords')).to_have_value('commercial refrigeration contractor')
                            expect(page.locator('#collector-areas')).to_have_value('Inanam Industrial Park')
                            self.assertEqual(errors, [])
                            # Accepted CLI/API profiles can contain custom areas in several states.
                            custom = [{'state': 'Sabah', 'locality': 'Inanam Industrial Park'},
                                      {'state': 'Sarawak', 'locality': 'Samajaya Industrial Park'}]
                            write_json(selection_path(dashboard.DB_PATH), {'mode': 'selected',
                                'industries': ['Automotive'], 'states': ['Sabah', 'Sarawak'], 'cities': [],
                                'custom_keywords': ['car repair'], 'custom_industry': 'Automotive',
                                'custom_locations': custom})
                            page.reload()
                            self.assertEqual(page.evaluate('currentSelection().custom_locations'), custom)
                            page.locator('#collector-preview').click()
                            expect(page.locator('#collector-query-preview')).to_contain_text('Samajaya Industrial Park, Sarawak, Malaysia')
                            dashboard.collector_controller().pause()
                            expect(page.locator('#collector-control-state')).to_have_text('paused', timeout=10000)
                            expect(page.locator('#collector-state')).to_have_text('Paused')
                            self.assertEqual(errors, [])
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
