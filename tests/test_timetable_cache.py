import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class TimetableCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.object(server, 'CURRENT_STUID', 'test-account'),
            patch.object(server, 'TIMETABLE_CACHE_FILE', str(root / 'cache.json')),
            patch.object(server, 'SCHEDULE_ADJUSTMENTS_FILE', str(root / 'adjustments.json')),
            patch.object(server, 'MANUAL_TASKS_FILE', str(root / 'tasks.json')),
            patch.object(server, '_course_id_map', return_value={'testcourse': {'id': 123}}),
            patch.object(server, '_eta_data', side_effect=self.eta_data),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temp.cleanup)
        server.invalidate_cache()
        self.addCleanup(server.invalidate_cache)
        self.table = {'kbList': {'1': [{'ksj': 1, 'ks': 2, 'ke': [{
            'kcmc': 'Test Course', 'kcdm': 'TEST', 'sksj': '秋{第1-8周}',
            'jsmc': 'Room', 'rkjs': 'Teacher',
        }]}]}}

    def eta_data(self, url, **kwargs):
        if url == server.ETA_CURRENT_TERM_URL:
            return {'xnxq': '2026-2027-1'}
        if url.startswith(server.ETA_TIMETABLE_URL):
            return self.table
        if url == server.ETA_DATE_INFO_URL:
            return {'currXn': '2026-2027', 'currXq': '秋冬', 'currZs': 5}
        return ['2026-2027-1']

    def sync(self):
        result = server.api_timetable('2026-10-12', weeks=9, force=True)
        self.assertTrue(result['ok'], result.get('error'))
        return result

    def test_batch_fetches_semester_once_and_local_switch_needs_no_network(self):
        result = self.sync()
        self.assertEqual(len(result['weeks']), 9)
        self.assertEqual(result['week_start'], '2026-10-12')
        self.assertEqual(len([c for c in server._eta_data.call_args_list
                              if c.args[0].startswith(server.ETA_TIMETABLE_URL)]), 1)
        server.invalidate_cache()  # Simulate restart: memory is gone, disk remains.
        with patch.object(server, '_eta_data', side_effect=AssertionError('network used')), \
                patch.object(server, '_course_id_map', side_effect=AssertionError('courses used')):
            offline = server.api_timetable('2026-11-02', weeks=9, cache_only=True)
            self.assertTrue(offline['ok'], offline.get('error'))
            self.assertEqual(offline['days'][0]['blocks'][0]['courses'][0]['course_id'], 123)
            self.assertTrue(server.api_status()['logged_in'])

    def test_failed_refresh_keeps_snapshot_and_returns_old_data(self):
        self.sync()
        before = Path(server.TIMETABLE_CACHE_FILE).read_bytes()
        with patch.object(server, '_eta_data', side_effect=OSError('offline')):
            result = server.api_timetable('2026-10-12', force=True, weeks=9)
        self.assertTrue(result['ok'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['sync_error'], 'offline')
        self.assertTrue(result['days'][0]['blocks'])
        self.assertEqual(Path(server.TIMETABLE_CACHE_FILE).read_bytes(), before)

    def test_missing_payload_does_not_erase_cache_but_valid_empty_table_does(self):
        self.sync()
        before = Path(server.TIMETABLE_CACHE_FILE).read_bytes()
        self.table = {}
        result = server.api_timetable('2026-10-12', force=True)
        self.assertTrue(result['stale'])
        self.assertEqual(Path(server.TIMETABLE_CACHE_FILE).read_bytes(), before)
        self.table = {'kbList': {}}
        result = server.api_timetable('2026-10-12', force=True)
        self.assertFalse(result['stale'])
        self.assertFalse(any(day['blocks'] for day in result['days']))

    def test_accounts_and_terms_are_isolated(self):
        self.sync()
        with patch.object(server, 'CURRENT_STUID', 'other-account'):
            self.assertFalse(server.api_timetable('2026-10-12', cache_only=True)['ok'])
        self.assertFalse(server.api_timetable('2026-10-12', term='other-term', cache_only=True)['ok'])

    def test_no_cache_offline_and_corrupt_file_report_failure(self):
        with patch.object(server, '_eta_data', side_effect=OSError('offline')):
            self.assertFalse(server.api_timetable('2026-10-12')['ok'])
        Path(server.TIMETABLE_CACHE_FILE).write_text('{broken', encoding='utf-8')
        self.assertFalse(server.api_timetable('2026-10-12', cache_only=True)['ok'])

    def test_expired_disk_cache_renders_without_network(self):
        self.sync()
        saved = server._saved_timetable_bundle('test-account')
        with patch.object(server.time, 'time', return_value=saved['synced_at'] + 301), \
                patch.object(server, '_eta_data', side_effect=AssertionError('network used')):
            result = server.api_timetable('2026-10-12', weeks=9, cache_only=True)
        self.assertTrue(result['ok'])
        self.assertTrue(result['stale'])
        self.assertTrue(result['days'][0]['blocks'])

    def test_local_move_updates_source_and_destination_without_network(self):
        result = self.sync()
        course = result['days'][0]['blocks'][0]['courses'][0]
        change = server.api_schedule_adjustment({
            'action': 'create', 'kind': 'move_once', 'term': result['term'],
            'slot_key': course['slot_key'], 'course_name': course['name'], 'course_id': 123,
            'occurrence_date': '2026-10-12', 'original_start_period': 1,
            'original_period_count': 2, 'original_weekday': 1,
            'new_date': '2026-10-20', 'new_start_period': 3, 'new_period_count': 2,
        })
        self.assertTrue(change['ok'], change.get('error'))
        with patch.object(server, '_eta_data', side_effect=AssertionError('network used')):
            result = server.api_timetable('2026-10-12', weeks=9, cache_only=True)
        self.assertFalse(result['days'][0]['blocks'])
        next_week = next(w for w in result['weeks'] if w['week_start'] == '2026-10-19')
        self.assertEqual(next_week['days'][1]['blocks'][0]['courses'][0]['local_adjustment'], 'move_once')

    def test_date_and_batch_limits(self):
        for date, weeks in [('bad-date', 9), ('2026-10-12', 0), ('2026-10-12', 18)]:
            self.assertFalse(server.api_timetable(date, weeks=weeks)['ok'])
        server._eta_data.assert_not_called()


if __name__ == '__main__':
    unittest.main()
