"""Bounded real backend semantics with synthetic XML/transport; never invokes SVN."""
import http.client
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_reads import (FileReadService, StaleRead, history_module, strict_relative_path,
                        revision, file_identity)
from server import ReadOnlyAdapter, PrototypeServer
from workspace_bridge import WorkspaceCatalog, trusted_modules, working_copy_identity

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


class ValidationTests(unittest.TestCase):
    def test_paths_are_strict_relative_files(self):
        for path in ['', '.', '..', '/etc/passwd', '//server/share', r'C:\\secret', 'C:secret',
                     r'folder\a.txt' if os.name != 'nt' else r'..\a.txt', 'a/../b', './a',
                     '.svn/wc.db', 'a/.SvN/wc.db', 'a//b', 'a/', 'x:stream', 'https://host/a',
                     'a\x00b', 'a\nb', 'a\x7fb', 'a./b', 'a /b']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                strict_relative_path(path)
        self.assertEqual(strict_relative_path('src/中文 @.txt'), Path('src/中文 @.txt'))
        self.assertEqual(strict_relative_path('--config-option'), Path('--config-option'))

    def test_revision_wire_values_are_canonical_and_exact(self):
        for value in ['BASE', 'HEAD', '0', '9223372036854775807']:
            self.assertEqual(revision(value), value)
        self.assertEqual(revision('WORKING', working=True), 'WORKING')
        for value in [0, True, None, '', '01', '-1', '1:2', '{date}', '--help', 'WORKING',
                      'base', ' HEAD', '9223372036854775808']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                revision(value)


@unittest.skipUnless((BACKEND / 'svn_client/history.py').is_file(), 'Trusted history source is required.')
class FileReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name); self.root = self.home / 'wc'
        (self.root / '.svn').mkdir(parents=True)
        (self.root / 'src').mkdir(); (self.root / 'src/a.txt').write_text('working')
        self.backend, _, self.discovery, self.settings = trusted_modules(BACKEND)
        self.history = history_module(BACKEND)
        self.assertIsNotNone(self.history, 'Update the paired strict-history source.')
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.client = self.backend.SvnClient(self.root)
        self.service = FileReadService(self.client, self.history)
        self.run = Mock(side_effect=self.response)
        self.service.manager._run = self.run
        self.row = {'id': 'src/a.txt', 'path': 'src/a.txt', 'item': 'modified'}

    def info(self, target, *, kind=None, switched=False, root=None):
        relative = target[:-1] if '@' in target else target
        tree = ET.Element('info')
        entry = ET.SubElement(tree, 'entry', kind=kind or ('dir' if target == '.' else 'file'))
        ET.SubElement(entry, 'url').text = 'https://example.test/repo' + ('/wrong' if switched else '' if target == '.' else '/' + quote(Path(relative).as_posix()))
        repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'uuid').text = 'fixture'
        wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(root or self.root)
        return ET.tostring(tree)

    def response(self, args, limit):
        if args[0] == 'info': return self.info(args[-1])
        if args[0] == 'diff': return b'--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n'
        if args[0] == 'log': return b'<log><logentry revision="9007199254740993"><author>&lt;script&gt;</author><msg>Read only</msg><paths><path action="M" kind="file">/other/file.txt</path></paths></logentry></log>'
        raise AssertionError('Unexpected command ' + str(args))

    def diff(self, **extra):
        return self.service.read('diff', self.row, {'fromRevision': 'BASE', 'toRevision': 'WORKING', **extra})

    def history_page(self, **extra):
        return self.service.read('history', self.row, {'startRevision': 'HEAD', **extra})

    def test_exact_file_diff_reuses_bounded_backend_and_internal_peg_semantics(self):
        result = self.diff()
        self.assertEqual(result['path'], 'src/a.txt')
        self.assertEqual(result['text'], '--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n')
        self.assertFalse(result['truncated']); self.assertFalse(result['binaryPayloadsIncluded'])
        call = next(call for call in self.run.call_args_list if call.args[0][0] == 'diff')
        self.assertEqual(call.args[0], ['diff', '--old=' + str(Path('src/a.txt')) + '@', '--new=' + str(Path('src/a.txt')) + '@', '--revision', 'BASE'])
        self.assertEqual(call.args[1], 8 * 1024 * 1024)
        self.assertTrue(all(call.args[0][0] in {'info', 'diff'} for call in self.run.call_args_list))

    def test_final_transport_explicitly_prevents_diff_recursion(self):
        manager = FileReadService(self.client, self.history).manager
        with patch.object(self.history, '_bounded_process', return_value=b'') as run:
            manager._run(['diff', '--old=src/a.txt@', '--new=src/a.txt@', '--revision', '1:2'], 1024)
        argv = run.call_args.args[0]
        self.assertIn('--internal-diff', argv)
        self.assertEqual(argv[argv.index('--depth') + 1], 'empty')
        self.assertEqual(argv[-1], '1:2')
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_invalid_revision_never_runs_info(self):
        with self.assertRaises(ValueError): self.diff(fromRevision='1:HEAD')
        self.run.assert_not_called()

    def test_history_preserves_large_revision_as_string_and_scopes_whole_commit_paths(self):
        result = self.history_page()
        self.assertEqual(result['entries'][0]['revision'], '9007199254740993')
        self.assertEqual(result['entries'][0]['changedPaths'][0]['path'], '/other/file.txt')
        self.assertEqual(result['entries'][0]['author'], '<script>')  # UI escapes, server JSON is text.
        self.assertFalse(result['hasMore']); self.assertIsNone(result['nextStartRevision'])
        command = next(c for c in self.run.call_args_list if c.args[0][0] == 'log')
        self.assertEqual(command.args[0], ['log', '--xml', '--verbose', '--limit', '51', '--revision', 'HEAD:0', '--', str(Path('src/a.txt'))])
        self.assertEqual(command.args[1], 4 * 1024 * 1024)

    def test_fixture_urls_use_repository_separators_on_windows(self):
        with patch(__name__ + '.Path', PureWindowsPath):
            info = ET.fromstring(self.info(r'src\a.txt'))
        self.assertEqual(info.findtext('entry/url'), 'https://example.test/repo/src/a.txt')
        self.assertNotIn('%5C', info.findtext('entry/url'))

    def test_encoded_backslash_repository_url_still_fails_closed(self):
        original = self.response
        def wrong_separator(args, limit):
            data = original(args, limit)
            if args[0] == 'info' and args[-1] != '.':
                info = ET.fromstring(data)
                info.find('entry/url').text = 'https://example.test/repo/src%5Ca.txt'
                return ET.tostring(info)
            return data
        self.run.side_effect = wrong_separator
        with self.assertRaises(ValueError): self.diff()
        self.assertFalse(any(c.args[0][0] == 'diff' for c in self.run.call_args_list))

    def test_old_diff_text_policy_disables_reads(self):
        with patch.object(self.history, 'DIFF_TEXT_POLICY', 0):
            self.assertIsNone(history_module(BACKEND))

    def test_mixed_encoding_warning_is_kept_through_bridge(self):
        original = self.response
        self.run.side_effect = lambda args, limit: ('Index: 中文.txt\n'.encode('cp936') + '+新正文\n'.encode()) if args[0] == 'diff' else original(args, limit)
        result = self.diff()
        self.assertIn('+新正文', result['text'])
        self.assertEqual(result['textEncoding'], 'utf-8-visible-escapes')
        self.assertEqual(result['undecodableBytes'], 4)
        self.assertEqual(result['escapedControls'], 0)
        self.assertEqual(result['returnedUtf8Bytes'], len(result['text'].encode()))

    def test_base_to_base_rejected_before_any_info(self):
        with self.assertRaises(ValueError): self.diff(toRevision='BASE')
        self.run.assert_not_called()

    def test_nul_payload_is_never_shown_as_text(self):
        original = self.response
        self.run.side_effect = lambda args, limit: b'private prefix\x00binary' if args[0] == 'diff' else original(args, limit)
        with self.assertRaises(RuntimeError): self.diff()

    def test_51st_entry_is_only_pagination_sentinel(self):
        original = self.response
        self.run.side_effect = lambda args, limit: (b'<log>' + b''.join(f'<logentry revision="{n}"/>'.encode() for n in range(100, 49, -1)) + b'</log>') if args[0] == 'log' else original(args, limit)
        result = self.history_page()
        self.assertEqual(len(result['entries']), 50); self.assertTrue(result['hasMore'])
        self.assertEqual(result['nextStartRevision'], '50')

    def test_missing_deleted_file_is_verified_by_svn_kind_without_directory_fallback(self):
        (self.root / 'src/a.txt').unlink(); self.row['item'] = 'deleted'
        self.assertIn('text', self.diff())
        self.assertTrue(all(c.args[0][-1] != 'src' for c in self.run.call_args_list))

    def test_unsafe_rows_are_refused_before_svn(self):
        for updates in [{'item': x} for x in ['unversioned', 'ignored', 'external', 'obstructed', 'incomplete']] + [{'switched': True}, {'file_external': True}, {'id': '../secret'}, {'id': '.svn/wc.db'}, {'id': '.'}]:
            self.run.reset_mock()
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.service.read('diff', {**self.row, **updates}, {'fromRevision': 'BASE', 'toRevision': 'WORKING'})
            self.run.assert_not_called()

    def test_directory_cannot_expand_diff_recursively(self):
        self.row['id'] = 'src'
        with self.assertRaises(ValueError): self.diff()
        self.run.assert_not_called()

    def test_missing_target_with_svn_directory_kind_is_refused(self):
        (self.root / 'src/a.txt').unlink()
        self.run.side_effect = lambda args, limit: self.info(args[-1], kind='dir')
        with self.assertRaises(ValueError): self.diff()
        self.assertEqual(self.run.call_count, 1)

    def test_nested_working_copy_is_rejected_before_svn(self):
        (self.root / 'src/.svn').mkdir()
        with self.assertRaises(ValueError): self.diff()
        self.run.assert_not_called()

    def test_symlink_and_hardlink_are_refused(self):
        target = self.root / 'src/a.txt'; target.unlink(); outside = self.home / 'secret'; outside.write_text('secret')
        try: target.symlink_to(outside)
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314: self.skipTest('Symlink fixture privilege unavailable.')
            raise
        with self.assertRaises(ValueError): self.diff()
        target.unlink(); os.link(outside, target)
        with self.assertRaises(ValueError): self.diff()
        self.run.assert_not_called()

    def test_switched_target_and_other_wc_metadata_are_refused(self):
        self.run.side_effect = lambda args, limit: self.info(args[-1], switched=args[-1] != '.')
        with self.assertRaises(ValueError): self.diff()
        self.assertFalse(any(c.args[0][0] == 'diff' for c in self.run.call_args_list))
        self.run.side_effect = lambda args, limit: self.info(args[-1], root=self.home)
        with self.assertRaises(ValueError): self.diff()

    def test_file_mutation_during_read_discards_success(self):
        def changed(args, limit):
            if args[0] == 'diff': (self.root / 'src/a.txt').write_text('new bytes during read')
            return self.response(args, limit)
        self.run.side_effect = changed
        with self.assertRaises(ValueError): self.diff()

    def test_preview_truncation_disclosed_and_history_response_cap_rejects_whole_page(self):
        original = self.response
        self.run.side_effect = lambda args, limit: ('字' * 100).encode() if args[0] == 'diff' else original(args, limit)
        with patch('file_reads.DIFF_TEXT_BYTES', 20):
            result = self.diff()
        self.assertTrue(result['truncated']); self.assertEqual(result['returnedUtf8Bytes'], 18)
        self.assertEqual(result['sourceUtf8Bytes'], 300)
        with patch('file_reads.HISTORY_RESPONSE_BYTES', 20), self.assertRaises(ValueError): self.history_page()

    def adapter(self):
        adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        adapter.workspace = self.root; adapter.client = self.client
        adapter._identity = working_copy_identity(self.root); adapter._busy = threading.Lock()
        adapter._file_service = self.service; adapter._read_snapshot = None
        self.client.status = Mock(return_value=[self.backend.StatusEntry('src/a.txt', 'modified')])
        return adapter

    def test_status_token_is_explicit_and_invalidated_on_refresh_failure(self):
        adapter = self.adapter(); status = adapter.snapshot()
        data = {'snapshotToken': status['snapshotToken'], 'id': 'src/a.txt', 'fromRevision': 'BASE', 'toRevision': 'WORKING'}
        self.assertTrue(status['fileReadsEnabled']); self.assertIn('text', adapter.file_read('diff', data))
        fresh = adapter.snapshot(); self.assertNotEqual(fresh['snapshotToken'], status['snapshotToken'])
        with self.assertRaises(StaleRead): adapter.file_read('diff', data)
        self.client.status.side_effect = RuntimeError('fail')
        with self.assertRaises(RuntimeError): adapter.snapshot()
        with self.assertRaises(StaleRead): adapter.file_read('diff', {**data, 'snapshotToken': fresh['snapshotToken']})
        self.assertFalse(adapter._busy.locked())

    def test_duplicate_status_ids_fail_closed(self):
        adapter = self.adapter()
        self.client.status.return_value *= 2
        with self.assertRaises(ValueError): adapter.snapshot()
        self.assertIsNone(adapter._read_snapshot)
        self.assertFalse(adapter._busy.locked())

    def test_missing_history_policy_disables_only_file_reads(self):
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'), patch('server.history_module', return_value=None):
            adapter = ReadOnlyAdapter(self.root, BACKEND)
        adapter.client.status = Mock(return_value=[self.backend.StatusEntry('src/a.txt', 'modified')])
        result = adapter.snapshot()
        self.assertEqual(result['mode'], 'live'); self.assertFalse(result['fileReadsEnabled'])
        with self.assertRaises(ValueError):
            adapter.file_read('diff', {'snapshotToken': result['snapshotToken'], 'id': 'src/a.txt'})

    def test_unknown_row_cannot_be_requested_with_valid_snapshot(self):
        adapter = self.adapter(); status = adapter.snapshot()
        with self.assertRaises(StaleRead):
            adapter.file_read('diff', {'snapshotToken': status['snapshotToken'], 'id': 'secret'})
        self.run.assert_not_called()

    def test_workspace_switch_rejects_same_filename_old_generation_before_backend(self):
        other = self.home / 'other'; (other / '.svn').mkdir(parents=True)
        adapters = {}
        def factory(path):
            value = Mock(); value.snapshot.return_value = {'mode': 'live', 'rows': []}
            adapters[path] = value; return value
        catalog = WorkspaceCatalog(roots=[self.root, other], discovery=self.discovery, settings=self.settings, adapter_factory=factory)
        first, second = list(catalog.candidates.values())
        selected = catalog.select(first.id)
        catalog.select(second.id)
        with self.assertRaises(StaleRead):
            catalog.file_read('diff', {'workspaceId': first.id, 'selectionVersion': selected['selectionVersion'], 'id': 'src/a.txt'})
        for adapter in adapters.values(): adapter.file_read.assert_not_called()


class FileReadHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Mock(); cls.catalog.file_read.return_value = {'mode': 'live', 'text': 'safe'}
        cls.server = PrototypeServer(0, catalog=cls.catalog)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)

    def setUp(self):
        self.catalog.reset_mock(); self.catalog.file_read.side_effect = None
        self.data = {'workspaceId': 'allowed', 'selectionVersion': 1, 'snapshotToken': 'snap',
                     'id': 'src/a.txt', 'fromRevision': 'BASE', 'toRevision': 'WORKING'}

    def request(self, data=None, path='/api/file-diff', headers=None, raw=None):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        c.request('POST', path, body=raw if raw is not None else json.dumps(data or self.data),
                  headers={'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token, **(headers or {})})
        r = c.getresponse(); result = r.status, r.read(); c.close(); return result

    def test_only_strict_file_read_schema_reaches_catalog(self):
        self.assertEqual(self.request()[0], 200)
        self.catalog.file_read.assert_called_once_with('diff', self.data)
        history = {k: v for k, v in self.data.items() if k not in {'fromRevision', 'toRevision'}}
        history['startRevision'] = 'HEAD'
        self.assertEqual(self.request(history, path='/api/file-history')[0], 200)

    def test_extra_fields_revisions_wrong_types_and_duplicates_are_rejected(self):
        for change in [{'path': '/etc/passwd'}, {'command': 'commit'}, {'selectionVersion': True},
                       {'selectionVersion': 0}, {'selectionVersion': 2**53}, {'fromRevision': '1:2'},
                       {'fromRevision': 'WORKING'}, {'toRevision': '--help'}, {'snapshotToken': ''},
                       {'id': 'x\ny'}, {'workspaceId': 4}]:
            with self.subTest(change=change): self.assertEqual(self.request({**self.data, **change})[0], 400)
        self.assertEqual(self.request(raw=json.dumps(self.data)[:-1] + ',"id":"other"}')[0], 400)
        self.assertEqual(self.request(raw='[' * 1500 + ']' * 1500)[0], 400)
        self.assertEqual(self.request(raw='x' * 4097)[0], 413)
        self.catalog.file_read.assert_not_called()

    def test_base_to_base_has_clear_400_without_backend_call(self):
        status, raw = self.request({**self.data, 'toRevision': 'BASE'})
        self.assertEqual(status, 400)
        self.assertIn('BASE → BASE', json.loads(raw)['error'])
        self.assertIn('BASE → WORKING', json.loads(raw)['error'])
        self.catalog.file_read.assert_not_called()

    def test_host_origin_fetch_metadata_and_session_required(self):
        for headers in [{'Host': 'evil.test'}, {'Origin': 'https://evil.test'}, {'Sec-Fetch-Site': 'cross-site'}, {'X-SVN-Desk-Token': ''}, {'Content-Type': 'text/plain'}]:
            self.assertIn(self.request(headers=headers)[0], {400, 403})
        self.catalog.file_read.assert_not_called()

    def test_failures_are_sanitized_and_not_retried(self):
        for error, expected in [(StaleRead('secret'), 409), (BlockingIOError('secret'), 409), (ValueError('secret'), 503)]:
            self.catalog.file_read.side_effect = error
            status, body = self.request(); self.assertEqual(status, expected); self.assertNotIn(b'secret', body)
        self.assertEqual(self.catalog.file_read.call_count, 3)


if __name__ == '__main__': unittest.main()
