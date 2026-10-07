import tempfile
import json
import unittest
from pathlib import Path

import batch_collect_malaysia_v2 as collector


class ScopeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = collector.open_db(Path(self.temp.name) / 'fixture.sqlite')

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def samples(self, new, ab, duplicate=0, processed=100, age=0):
        tasks = [collector.QueryTask(f'fixture-{age}-{i}', 'Automotive', 'Town',
                                   'Kedah', 'car workshop') for i in range(3)]
        collector.register_manifest(self.conn, tasks)
        for task in tasks:
            self.conn.execute("""UPDATE search_jobs SET status='completed',qualified_new=?,
                ab_leads_new=?,duplicate_count=?,processed_count=?,
                started_at=strftime('%Y-%m-%dT%H:%M:%SZ','now',?),
                completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now',?) WHERE prompt=?""",
                (new, ab, duplicate, processed, f'-{age * 60 + 10} minutes',
                 f'-{age * 60} minutes', task.prompt))
        self.conn.commit()
        return tasks

    def test_recent_zero_speed_overrides_old_success(self):
        self.samples(30, 30, age=100)
        self.samples(0, 0)
        rates = collector.observed_ab_hourly_rates(self.conn)
        self.assertEqual(rates[('Automotive', 'car workshop', 'Kedah', 'city')], 0)

    def test_full_cycle_assigns_40_percent_to_expansion(self):
        tasks = [collector.QueryTask(f'{state}-{i}', 'Automotive', 'Town', state,
                 'car workshop') for state in ('Selangor', 'Johor', 'Penang', 'Kedah')
                 for i in range(50)]
        cycle = collector.weighted_market_order(tasks)[:collector.MARKET_REORDER_INTERVAL]
        self.assertAlmostEqual(sum(t.state == 'Kedah' for t in cycle) / len(cycle), .4)

    def test_manifest_does_not_spend_market_slots_on_finished_jobs(self):
        finished = collector.QueryTask('completed child fixture', 'Automotive', 'Town',
                                       'Selangor', 'car workshop', 'district')
        skipped = collector.QueryTask('skipped child fixture', 'Automotive', 'Town',
                                      'Johor', 'car workshop', 'district')
        collector.register_manifest(self.conn, [finished, skipped])
        self.conn.execute("UPDATE search_jobs SET status='completed' WHERE prompt=?", (finished.prompt,))
        self.conn.execute("UPDATE search_jobs SET status='skipped' WHERE prompt=?", (skipped.prompt,))
        self.conn.commit()
        prompts = {t.prompt for t in collector.build_manifest(self.conn)}
        self.assertFalse(finished.prompt in prompts, 'completed job consumed a market slot')
        self.assertFalse(skipped.prompt in prompts, 'skipped job consumed a market slot')

    def test_group_pause_is_local_recent_and_reversible(self):
        task = self.samples(0, 0)[0]
        pause = getattr(collector, 'paused_query_groups', None)
        self.assertTrue(callable(pause), 'group pause policy is missing')
        key = (task.sector, task.term, task.state, task.geo_level)
        self.assertIn(key, pause(self.conn))
        self.assertNotIn((task.sector, task.term, 'Melaka', task.geo_level), pause(self.conn))
        self.conn.execute("UPDATE search_jobs SET completed_at=datetime('now','-4 days')")
        self.conn.commit()
        self.assertNotIn(key, pause(self.conn))

    def test_saturation_pauses_even_if_ab_leads_exist(self):
        task = self.samples(5, 4, 85)[0]
        pause = getattr(collector, 'paused_query_groups', None)
        self.assertTrue(callable(pause), 'group pause policy is missing')
        self.assertIn((task.sector, task.term, task.state, task.geo_level), pause(self.conn))

    def test_canary_waits_and_checks_failure_yield_and_blocks(self):
        assess = getattr(collector, 'assess_scope_canary', None)
        self.assertTrue(callable(assess), 'scope canary policy is missing')
        self.assertEqual(assess(59, 600, 1, 60, False), 'running')
        self.assertEqual(assess(60, 480, 2, 62, False), 'passed')
        self.assertEqual(assess(60, 479, 0, 60, False), 'needs_review')
        self.assertEqual(assess(60, 600, 4, 64, False), 'needs_review')
        self.assertEqual(assess(60, 600, 0, 60, True), 'needs_review')

    def test_canary_persists_and_deduplicates_new_locations(self):
        tasks = self.samples(10, 8)
        started = self.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')").fetchone()[0]
        for company_id, age in ((1, '-10 minutes'), (2, '-2 days')):
            self.conn.execute("""INSERT INTO companies(id,identity,sector,business_name,address,
                city_state,website,website_domain,first_seen_at,last_seen_at)
                VALUES(?,?,'Automotive','Workshop','Malaysia','Kedah','','',
                strftime('%Y-%m-%dT%H:%M:%SZ','now',?),datetime('now'))""",
                (company_id, str(company_id), age))
            for task in tasks:
                self.conn.execute('INSERT INTO provenance VALUES(?,?,datetime(\'now\'))',
                                  (company_id, task.prompt))
        self.conn.commit()
        state = dict(started_at=started, status='running', prompts=[t.prompt for t in tasks],
                     attempts=3, failures=0)
        collector.update_scope_canary(self.conn, state)
        restored = json.loads(self.conn.execute('SELECT value FROM metadata WHERE key=?',
                             (collector.SCOPE_CANARY_KEY,)).fetchone()[0])
        self.assertEqual(restored['qualified_new'], 1)
        self.assertEqual(restored['completed'], 3)
        collector.update_scope_canary(self.conn, restored)
        self.assertEqual(restored, state)
        collector.update_scope_canary(self.conn, restored, persistently_blocked=True)
        collector.update_scope_canary(self.conn, restored)
        self.assertTrue(restored.get('persistently_blocked'), 'a restart must retain block evidence')


if __name__ == '__main__':
    unittest.main()
