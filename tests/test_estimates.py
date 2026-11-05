import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch
import estimates
import jobs
import store


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        store.migrate(self.conn)
        self.conn.executescript(jobs.SCHEMA)
        for column, kind in jobs.COLUMNS:
            self.conn.execute(f'ALTER TABLE jobs ADD COLUMN {column} {kind}')
        self.cpu = json.dumps({'backend': 'cpu', 'model': 'medium'})
        self.gpu = json.dumps({'backend': 'vulkan', 'model': 'medium'})
        self.number = 0

    def sample(self, key, elapsed, startup=2, work=600, eligible=True, platform='recording'):
        self.number += 1
        result = {'video_id': f'test{self.number}', 'platform': platform, 'title': 'Test',
                  'channel': None, 'duration': 600, 'source': 'whisper-medium',
                  'timings': {'whisper': elapsed, 'download': .6, 'metadata': 1, 'total': elapsed + 2},
                  'performance': {'profile': key, 'startup': startup, 'work_seconds': work,
                                  'speech_fraction': work / 600, 'eligible': eligible} if key else None}
        store.upsert_video(self.conn, result)
        store.record_submission(self.conn, result)

    def test_cpu_gpu_and_unlabelled_legacy_runs_do_not_mix(self):
        for _ in range(3):
            self.sample(self.cpu, 152)
            self.sample(self.gpu, 14)
            self.sample(None, 1000)
        cpu = estimates.load(self.conn, self.cpu)
        gpu = estimates.load(self.conn, self.gpu)
        self.assertAlmostEqual(cpu['rate'], .25)
        self.assertAlmostEqual(gpu['rate'], .02)
        self.assertEqual(gpu['count'], 3)
        self.assertAlmostEqual(gpu['startup'], 2)
        self.assertLess(estimates.total(600, 'recording', gpu), 20)

    def test_excludes_repetition_and_limits_outlier_effect(self):
        for _ in range(5):
            self.sample(self.gpu, 14)
        self.sample(self.gpu, 3, eligible=False)
        self.sample(self.gpu, 1000)
        model = estimates.load(self.conn, self.gpu)
        self.assertAlmostEqual(model['rate'], .02)
        self.assertEqual(model['count'], 5)

    def test_vad_uses_speech_duration_and_local_conversion_is_separate(self):
        for _ in range(3):
            self.sample(self.gpu, 8, work=300)
        model = estimates.load(self.conn, self.gpu)
        self.assertAlmostEqual(model['speech_fraction'], .5)
        self.assertAlmostEqual(model['rate'], .02)
        self.assertLess(estimates.total(600, 'recording', model), estimates.total(600, 'rutube', model))
        self.assertEqual(batch.estimate_seconds(600, 'recording', {'predictor': model}),
                         estimates.total(600, 'recording', model))

    def test_cold_start_is_backend_specific_with_wide_uncertainty(self):
        gpu = estimates.load(self.conn, self.gpu)
        cpu = estimates.load(self.conn, self.cpu)
        self.assertLess(gpu['rate'], cpu['rate'])
        self.assertGreaterEqual(gpu['uncertainty'], .75)
        self.assertEqual(gpu['count'], 0)
        self.assertIsNone(estimates.total(None, 'recording', gpu))

    def job(self, samples=None, pct=30, phase='transcribing', stage='whisper'):
        live = {'phase': phase, 'work_seconds': 1000, 'profile': self.cpu,
                'pct': pct, 'updated_at': 130, 'samples': samples or [[10, 110], [20, 120], [30, 130]]}
        return {'stage': stage, 'stage_started_at': 100, 'progress_json': json.dumps(live)}

    def test_live_rate_adapts_without_fabricating_progress_between_updates(self):
        model = estimates.load(self.conn, self.cpu)
        job = self.job()
        initial = estimates.progress(job, 1000, model, 'recording', now=130)
        later = estimates.progress(job, 1000, model, 'recording', now=135)
        self.assertEqual(initial['fraction'], .3)
        self.assertEqual(later['fraction'], .3)
        self.assertLess(initial['remaining'], 1000 * .25 * .7)
        self.assertAlmostEqual(initial['remaining'] - later['remaining'], 5)
        self.assertLessEqual(initial['low'], initial['remaining'])
        self.assertGreaterEqual(initial['high'], initial['remaining'])

    def test_live_rate_can_increase_eta_when_gpu_is_busy(self):
        model = estimates.load(self.conn, self.gpu)
        fast = estimates.progress(self.job(), 1000, model, 'recording', now=130)
        slow = self.job(samples=[[10, 10], [20, 70], [30, 130]])
        slow_result = estimates.progress(slow, 1000, model, 'recording', now=130)
        self.assertGreater(slow_result['remaining'], fast['remaining'])

    def test_stall_hides_expired_eta_and_completion_waits_for_saving(self):
        model = estimates.load(self.conn, self.cpu)
        stalled = estimates.progress(self.job(), 1000, model, 'recording', now=1000)
        self.assertIsNone(stalled['remaining'])
        self.assertEqual(stalled['fraction'], .3)
        self.assertEqual(stalled['phase'], 'waiting_progress')
        finished = estimates.progress(self.job(pct=100), 1000, model, 'recording', now=130)
        self.assertIsNone(finished['remaining'])
        self.assertLess(finished['fraction'], 1)
        self.assertEqual(finished['phase'], 'finishing')
        for stage in ('metadata', 'captions', 'saving'):
            waiting = estimates.progress(self.job(stage=stage), 1000, model, 'recording', now=130)
            self.assertIsNone(waiting['fraction'])

    def test_progress_is_persisted_monotonically_and_reset_on_new_stage(self):
        job_id = jobs.enqueue(self.conn, 'video', 'recording:r0123456789abcde', 'record')
        self.conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        with patch.object(jobs.time, 'time', return_value=100):
            jobs.report_progress(self.conn, job_id, {'phase': 'loading', 'profile': self.gpu})
            jobs.report_progress(self.conn, job_id, {'pct': 20})
        with patch.object(jobs.time, 'time', return_value=101):
            jobs.report_progress(self.conn, job_id, {'pct': 10})
            jobs.report_progress(self.conn, job_id, {'pct': 20})
        live = estimates.decode(jobs.get(self.conn, job_id)['progress_json'])
        self.assertEqual(live['profile'], self.gpu)
        self.assertEqual(live['pct'], 20)
        self.assertEqual(live['samples'], [[20, 100]])
        jobs.set_stage(self.conn, job_id, 'saving')
        self.assertIsNone(jobs.get(self.conn, job_id)['progress_json'])

    def test_validation_uses_only_previous_runs_of_the_same_profile(self):
        for _ in range(4):
            self.sample(self.cpu, 152)
            self.sample(self.gpu, 14)
        report = estimates.backtest(self.conn)
        self.assertEqual(report['predictions'], 2)
        self.assertEqual(report['median_error_seconds'], 0)
        self.assertEqual(report['range_coverage'], 1)

    def test_migration_preserves_legacy_rows_without_guessing_their_backend(self):
        self.sample(None, 100)
        self.conn.execute('ALTER TABLE submissions DROP COLUMN performance')
        store.migrate(self.conn)
        store.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0], 1)
        self.assertIsNone(self.conn.execute('SELECT performance FROM submissions').fetchone()[0])
        self.assertEqual(estimates.load(self.conn, self.gpu)['count'], 0)


if __name__ == '__main__':
    unittest.main()
