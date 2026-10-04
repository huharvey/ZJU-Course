import io
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server


class LocalDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / '资料, 中文 course'
        self.folder.mkdir()
        self.popen = Mock()
        for item in [
            patch.object(server, 'CURRENT_STUID', 'test-user'),
            patch.object(server, 'get_download_dir', return_value=str(self.folder)),
            patch.object(server, 'COURSE_FOLDERS_FILE', str(self.root / 'folders.json')),
            patch.object(server, 'LIVE_EXPORTS_FILE', str(self.root / 'exports.json')),
            patch.object(server.subprocess, 'Popen', self.popen),
        ]:
            item.start()
            self.addCleanup(item.stop)

    def assert_revealed(self, result, target):
        self.assertTrue(result['ok'], result.get('error'))
        self.assertTrue(result['existing'])
        self.assertTrue(result['revealed'])
        self.assertEqual(result['path'], str(target))
        if server.os.name == 'nt':
            self.assertEqual(self.popen.call_args.args[0],
                             'explorer.exe /select,"%s"' % target)

    def test_existing_upload_never_downloads_or_changes_bytes(self):
        target = self.folder / '第1章.pdf'
        target.write_bytes(b'original')
        with patch.object(server, 'stream_upload', side_effect=AssertionError('network')):
            result = server.api_save_download(12, target.name)
        self.assert_revealed(result, target)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.folder.iterdir()), [target])

    def test_new_upload_repeat_and_delete_then_redownload(self):
        with patch.object(server, 'stream_upload', side_effect=lambda _: io.BytesIO(b'pdf')) as stream:
            first = server.api_save_download(12, 'slides.pdf')
            self.assertFalse(first['existing'])
            target = self.folder / 'slides.pdf'
            self.assertEqual(target.read_bytes(), b'pdf')
            self.assert_revealed(server.api_save_download(12, 'slides.pdf'), target)
            self.assertEqual(stream.call_count, 1)
            target.unlink()
            self.assertFalse(server.api_save_download(12, 'slides.pdf')['existing'])
            self.assertEqual(stream.call_count, 2)

    def test_failed_partial_download_is_removed_and_retry_works(self):
        class BrokenStream(io.BytesIO):
            def read(self, size):
                if self.tell():
                    raise OSError('connection interrupted')
                return super().read(size)

        with patch.object(server, 'stream_upload', return_value=BrokenStream(b'partial')):
            self.assertFalse(server.api_save_download(12, 'slides.pdf')['ok'])
        self.assertEqual(list(self.folder.iterdir()), [])
        with patch.object(server, 'stream_upload', return_value=io.BytesIO(b'complete')):
            self.assertTrue(server.api_save_download(12, 'slides.pdf')['ok'])
        self.assertEqual((self.folder / 'slides.pdf').read_bytes(), b'complete')

    def test_concurrent_clicks_only_fetch_once_and_status_hides_partial(self):
        started = threading.Event()
        release = threading.Event()

        def stream(_):
            started.set()
            if not release.wait(3):
                raise TimeoutError('test timed out')
            return io.BytesIO(b'complete')

        with patch.object(server, 'stream_upload', side_effect=stream) as fetch, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(server.api_save_download, 12, 'slides.pdf')
            self.assertTrue(started.wait(3))
            second = pool.submit(server.api_save_download, 12, 'slides.pdf')
            state = server.api_local_download_status({'files':[
                {'kind':'upload', 'id':12, 'name':'slides.pdf'}]})
            self.assertFalse(state['files'][0]['exists'])
            release.set()
            results = [first.result(3), second.result(3)]
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([r['existing'] for r in results], [False, True])
        self.assertEqual([p.name for p in self.folder.iterdir()], ['slides.pdf'])

    def test_external_file_appearing_during_download_is_not_overwritten(self):
        target = self.folder / 'slides.pdf'

        def writer(f):
            f.write(b'new download')
            target.write_bytes(b'external original')

        self.assert_revealed(server._save_local_download(str(target), writer), target)
        self.assertEqual(target.read_bytes(), b'external original')
        self.assertEqual(list(self.folder.iterdir()), [target])

    def test_existing_subtitles_are_found_before_authentication_or_network(self):
        name = '课程/2026-10-04'
        target = self.folder / server._subtitle_filename('abc', name)
        target.write_text('原字幕', encoding='utf-8-sig')
        with patch.object(server, 'fetch_live_subtitles', side_effect=AssertionError('network')):
            result = server.export_live_subtitles('abc', '', fallback_name=name)
        self.assert_revealed(result, target)
        self.assertEqual(target.read_text(encoding='utf-8-sig'), '原字幕')

    def test_new_subtitles_have_bom_and_no_english_translation(self):
        rows = [{'start':1, 'end':2, 'text':'中文原文', 'translation':'English'}]
        with patch.object(server, 'fetch_live_subtitles', return_value={'ok':True, 'subtitles':rows}) as fetch:
            result = server.export_live_subtitles('abc', '', fallback_name='录播')
            repeat = server.export_live_subtitles('abc', '', fallback_name='录播')
        self.assertEqual(result['count'], 1)
        data = Path(result['path']).read_bytes()
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn('中文原文', data.decode('utf-8-sig'))
        self.assertNotIn('English', data.decode('utf-8-sig'))
        self.assertTrue(repeat['existing'])
        self.assertEqual(fetch.call_count, 1)

    def ppt_session(self):
        session = Mock()
        info = {'code':0, 'data':{'file_name':'官方课件.pptx', 'path_name':'https://example.test/slides'}}
        session.request.side_effect = [SimpleNamespace(json=lambda: info),
                                       SimpleNamespace(status=200, body=b'ppt')]
        return session

    def test_legacy_ppt_matches_official_name_without_downloading_body(self):
        target = self.folder / '官方课件.pptx'
        target.write_bytes(b'old ppt')
        session = self.ppt_session()
        with patch.object(server, '_cmc_bearer_token', return_value='token'), \
                patch.object(server, 'HttpSession', return_value=session):
            self.assert_revealed(server.export_live_ppt('abc', ''), target)
        self.assertEqual(session.request.call_count, 1)  # Metadata only.
        self.assertEqual(target.read_bytes(), b'old ppt')

    def test_ppt_remembers_server_name_and_repeat_works_offline(self):
        session = self.ppt_session()
        with patch.object(server, '_cmc_bearer_token', return_value='token'), \
                patch.object(server, 'HttpSession', return_value=session):
            result = server.export_live_ppt('abc', '')
        self.assertFalse(result['existing'])
        self.assertEqual(Path(result['path']).read_bytes(), b'ppt')
        with patch.object(server, '_cmc_bearer_token', side_effect=AssertionError('network')):
            self.assert_revealed(server.export_live_ppt('abc', ''), Path(result['path']))
        Path(result['path']).unlink()
        with patch.object(server, '_cmc_bearer_token', return_value=None) as auth:
            self.assertFalse(server.export_live_ppt('abc', '')['ok'])
        auth.assert_called_once()

    def test_custom_course_folder_by_id_and_changed_destination(self):
        custom = self.root / 'custom'
        custom.mkdir()
        self.assertTrue(server.set_course_folder(42, 'Course', str(custom)))
        target = custom / 'slides.pdf'
        target.write_bytes(b'old')
        with patch.object(server, 'stream_upload', return_value=io.BytesIO(b'new')) as fetch:
            self.assert_revealed(server.api_save_download(12, target.name, 'Renamed Course', 42), target)
            fetch.assert_not_called()
            self.assertTrue(server.reset_course_folder(42, 'Course'))
            result = server.api_save_download(12, target.name, 'Course', 42)
        self.assertFalse(result['existing'])
        self.assertEqual(Path(result['path']).parent, self.folder / 'Course')

    def test_status_is_read_only_and_uses_disk_after_delete(self):
        upload = self.folder / 'slides.pdf'
        subtitle = self.folder / server._subtitle_filename('abc', '录播')
        ppt = self.folder / '官方课件.pptx'
        for file in [upload, subtitle, ppt]:
            file.write_bytes(b'original')
        server._remember_live_ppt(str(self.folder), 'abc', str(ppt))
        descriptors = [{'kind':'upload', 'id':12, 'name':upload.name},
                       {'kind':'subtitles', 'id':'abc', 'name':'录播'},
                       {'kind':'ppt', 'id':'abc'},
                       {'kind':'upload', 'id':13, 'name':'other.pdf', 'course':'Missing'}]
        states = server.api_local_download_status({'files':descriptors})['files']
        self.assertEqual([s['exists'] for s in states], [True, True, True, False])
        self.popen.assert_not_called()
        self.assertFalse((self.folder / 'Missing').exists())
        upload.unlink()
        self.assertFalse(server.api_local_download_status({'files':descriptors})['files'][0]['exists'])

    def test_reveal_failure_reports_location_without_redownloading(self):
        target = self.folder / 'slides.pdf'
        target.write_bytes(b'original')
        self.popen.side_effect = OSError('Explorer unavailable')
        with patch.object(server, 'stream_upload', side_effect=AssertionError('network')):
            result = server.api_save_download(12, target.name)
        self.assertTrue(result['ok'])
        self.assertTrue(result['existing'])
        self.assertFalse(result['revealed'])
        self.assertIn('Explorer unavailable', result['reveal_error'])

    def test_http_status_and_save_routes_use_the_same_file(self):
        target = self.folder / '第1章.pdf'
        target.write_bytes(b'original')
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % httpd.server_port

        def post(path, payload=None):
            request = urllib.request.Request(base + path, method='POST',
                data=json.dumps(payload or {}).encode(), headers={
                    'Content-Type':'application/json', 'X-Zjucourse-Token':server.TOKEN})
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.load(response)

        try:
            with patch.object(server, 'stream_upload', side_effect=AssertionError('network')):
                status = post('/api/local-download-status', {'files':[
                    {'kind':'upload', 'id':12, 'name':target.name}]})
                self.assertTrue(status['files'][0]['exists'])
                result = post('/api/save_download/12?' + urllib.parse.urlencode({'name':target.name}))
                self.assert_revealed(result, target)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
