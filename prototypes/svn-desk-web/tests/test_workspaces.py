"""Workspace bridge regressions. Real discovery/settings, synthetic status only."""
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import PrototypeServer, ReadOnlyAdapter
from workspace_bridge import WorkspaceCatalog, working_copy_identity, trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


def make_test_symlink(case, link, target, *, target_is_directory=True):
    """Skip only unavailable fixture privilege, never a product assertion."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            case.skipTest('Windows cannot create the symlink fixture without Developer Mode or the required privilege (WinError 1314).')
        raise


@unittest.skipUnless((BACKEND / 'svn_client/discovery.py').is_file(), 'Set SVN_DESK_BACKEND_SOURCE to test the unchanged backend integration.')
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.parent = self.home / 'authorized'
        self.parent.mkdir()
        self.a = self.parent / 'alpha'
        self.b = self.parent / 'beta'
        for root in (self.a, self.b):
            (root / '.svn').mkdir(parents=True)
        self.settings_file = self.home / 'web-preferences.json'
        self.backend, _, self.discovery, self.settings = trusted_modules(BACKEND)
        self.factory = Mock(side_effect=self.make_adapter)
        self.catalog = self.new_catalog()

    def make_adapter(self, path):
        adapter = Mock()
        adapter.snapshot.return_value = {'mode': 'live', 'workspace': path.name, 'root': str(path),
            'rows': [{'id': 'same.txt', 'path': 'same.txt', 'item': 'modified', 'props': 'none'}]}
        return adapter

    def new_catalog(self, **kwargs):
        return WorkspaceCatalog(parents=[self.parent], discovery=self.discovery,
            settings=self.settings, adapter_factory=self.factory, **kwargs)

    def candidate(self, path):
        return next(c for c in self.catalog.candidates.values() if c.path == path)

    def test_discovers_authorized_candidates_without_svn_or_content_reads(self):
        self.assertEqual({c.path for c in self.catalog.candidates.values()}, {self.a, self.b})
        self.factory.assert_not_called()
        self.assertEqual(self.catalog.config()['mode'], 'select')
        self.assertTrue(all(not c['verified'] for c in self.catalog.describe()['candidates']))
        self.assertEqual(self.catalog.snapshot()['rows'], [])

    def test_nested_candidate_is_not_claimed_external(self):
        nested = self.a / 'nested'
        (nested / '.svn').mkdir(parents=True)
        result = self.catalog.discover()
        self.assertEqual(next(c for c in result['candidates'] if c['path'] == str(nested))['relation'], 'nested')

    def test_select_status_and_switch_generation(self):
        first = self.catalog.select(self.candidate(self.a).id)
        second = self.catalog.select(self.candidate(self.b).id)
        self.assertEqual(first['root'], str(self.a))
        self.assertEqual(second['root'], str(self.b))
        self.assertGreater(second['selectionVersion'], first['selectionVersion'])
        self.assertFalse(second['selectionSaved'])
        self.assertEqual(self.catalog.snapshot()['workspaceId'], self.candidate(self.b).id)

    def test_forged_paths_and_ids_never_construct_adapter(self):
        for identifier in [str(self.a), '../beta', '/etc', 'forged', '--force']:
            with self.assertRaises(KeyError):
                self.catalog.select(identifier)
        self.factory.assert_not_called()

    def test_save_and_restore_only_authorized_selection(self):
        self.catalog = self.new_catalog(settings_file=self.settings_file)
        chosen = self.catalog.select(self.candidate(self.b).id)
        self.assertTrue(chosen['selectionSaved'])
        saved = json.loads(self.settings_file.read_text())
        self.assertEqual(saved['last_workspace'], str(self.b))
        self.assertNotIn('svn_executable', saved)
        restored = self.new_catalog(settings_file=self.settings_file)
        self.assertEqual(restored.config()['root'], str(self.b))
        self.assertEqual(restored.snapshot()['root'], str(self.b))

    def test_tampered_settings_cannot_authorize_path_or_executable(self):
        outside = self.home / 'outside'
        (outside / '.svn').mkdir(parents=True)
        self.settings_file.write_text(json.dumps({'last_workspace': str(outside), 'svn_executable': '/tmp/untrusted', 'password': 'DO_NOT_KEEP'}))
        self.catalog = self.new_catalog(settings_file=self.settings_file)
        self.assertIsNone(self.catalog.selected)
        self.catalog.select(self.candidate(self.a).id)
        stored = self.settings_file.read_text()
        self.assertNotIn('outside', stored)
        self.assertNotIn('untrusted', stored)
        self.assertNotIn('DO_NOT_KEEP', stored)
        self.factory.assert_called_once_with(self.a)

    def test_corrupt_settings_never_silently_overwritten(self):
        self.settings_file.write_text('{broken')
        self.catalog = self.new_catalog(settings_file=self.settings_file)
        selected = self.catalog.select(self.candidate(self.a).id)
        self.assertFalse(selected['selectionSaved'])
        self.assertTrue(selected['warning'])
        self.assertEqual(self.settings_file.read_text(), '{broken')

    def test_save_failure_is_disclosed_without_discarding_valid_status(self):
        self.catalog = self.new_catalog(settings_file=self.settings_file)
        with patch.object(self.settings, 'save_settings', side_effect=OSError('secret raw error')):
            selected = self.catalog.select(self.candidate(self.a).id)
        self.assertEqual(selected['mode'], 'live')
        self.assertFalse(selected['selectionSaved'])
        self.assertIn('保存失败', selected['warning'])
        self.assertNotIn('secret', selected['warning'])

    def test_status_failure_keeps_previous_selection_and_preferences(self):
        self.catalog = self.new_catalog(settings_file=self.settings_file)
        first = self.catalog.select(self.candidate(self.a).id)
        before = self.settings_file.read_bytes()
        broken = Mock(); broken.snapshot.side_effect = RuntimeError('raw diagnostics')
        self.factory.side_effect = None; self.factory.return_value = broken
        with self.assertRaises(RuntimeError):
            self.catalog.select(self.candidate(self.b).id)
        self.assertEqual(self.catalog.config()['workspaceId'], first['workspaceId'])
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_rescan_invalidates_removed_current_selection(self):
        self.catalog.select(self.candidate(self.a).id)
        generation = self.catalog.generation
        (self.a / '.svn').rmdir()
        catalog = self.catalog.discover()
        self.assertIsNone(catalog['selectedId'])
        self.assertEqual(catalog['current']['mode'], 'select')
        self.assertGreater(catalog['selectionVersion'], generation)
        self.assertEqual(self.catalog.snapshot()['rows'], [])

    def test_incomplete_scan_retains_unchanged_selected_candidate(self):
        selected = self.catalog.select(self.candidate(self.a).id)
        real = self.discovery.discover_working_copies
        with patch.object(self.discovery, 'discover_working_copies', side_effect=lambda parent: real(parent, max_directories=1)):
            result = self.catalog.discover()
        self.assertEqual(result['selectedId'], selected['workspaceId'])
        self.assertIn(selected['workspaceId'], self.catalog.candidates)
        self.assertEqual(self.catalog.snapshot()['root'], str(self.a))

    def test_zero_exit_warning_http_preserves_selection_generation_and_preferences(self):
        self.catalog = self.new_catalog(settings_file=self.settings_file)
        self.catalog.adapter_factory = lambda path: ReadOnlyAdapter(path, BACKEND)
        real_popen = subprocess.Popen
        observed = []
        def synthetic_status(argv, **kwargs):
            observed.append(argv)
            self.assertEqual(argv[1], 'status')
            warning = 'svn: warning: W155007: not a working copy; secret diagnostic' if Path(kwargs['cwd']) == self.b else ''
            script = "import sys; print('<status><target path=\".\"/></status>'); sys.stderr.write(" + repr(warning) + ")"
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        server = PrototypeServer(0, catalog=self.catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(path, body=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                headers = {'X-SVN-Desk-Token': server.token, 'Content-Type': 'application/json'}
                connection.request('POST' if body else 'GET', path, body=json.dumps(body) if body else None, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            with patch.object(self.backend, 'discover_svn', return_value=sys.executable), patch('svn_client.status_stream.subprocess.Popen', side_effect=synthetic_status):
                status, valid = request('/api/workspace-selection', {'id': self.candidate(self.a).id})
                self.assertEqual(status, 200)
                self.assertEqual(valid['rows'], [])  # Clean empty status is valid when stderr is empty.
                before = self.settings_file.read_bytes()
                generation = self.catalog.generation
                status, invalid = request('/api/workspace-selection', {'id': self.candidate(self.b).id})
                self.assertEqual(status, 503)
                self.assertNotIn('mode', invalid)
                self.assertNotIn('secret diagnostic', str(invalid))
                self.assertEqual(self.catalog.selected.path, self.a)
                self.assertEqual(self.catalog.generation, generation)
                self.assertEqual(self.settings_file.read_bytes(), before)
                status, recovered = request('/api/status')
                self.assertEqual(status, 200)
                self.assertEqual(recovered['root'], str(self.a))
                self.assertEqual(recovered['mode'], 'live')
            self.assertEqual(len(observed), 3)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_older_or_partial_backend_policy_is_rejected_before_client_creation(self):
        _, stream, _, _ = trusted_modules(BACKEND)
        for module in (self.backend, stream):
            for policy in (None, False, 0):
                with self.subTest(module=module.__name__, policy=policy), patch.object(module, 'STATUS_DIAGNOSTICS_POLICY', policy), patch.object(self.backend, 'SvnClient') as client:
                    with self.assertRaisesRegex(ValueError, 'strict status diagnostics'):
                        ReadOnlyAdapter(self.a, BACKEND)
                    client.assert_not_called()

    def test_stale_disappeared_id_is_rejected(self):
        stale = self.candidate(self.b).id
        (self.b / '.svn').rmdir()
        self.catalog.discover()
        with self.assertRaises(KeyError):
            self.catalog.select(stale)
        self.factory.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'symlink'), 'Symlink support required')
    def test_symlink_marker_swap_rejected_before_status(self):
        candidate = self.candidate(self.a)
        (self.a / '.svn').rmdir()
        make_test_symlink(self, self.a / '.svn', self.b / '.svn')
        with self.assertRaises(ValueError):
            self.catalog.select(candidate.id)
        self.factory.assert_not_called()

    def test_root_replacement_rejected_even_at_same_path(self):
        candidate = self.candidate(self.a)
        self.a.rename(self.parent / 'old-alpha')
        (self.a / '.svn').mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.catalog.select(candidate.id)
        self.factory.assert_not_called()

    def test_parent_swap_rejected_after_selection(self):
        self.catalog.select(self.candidate(self.a).id)
        self.parent.rename(self.home / 'old-parent')
        (self.a / '.svn').mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.catalog.snapshot()

    def test_change_during_status_discards_result(self):
        candidate = self.candidate(self.a)
        def changed():
            (self.a / '.svn').rename(self.a / '.old-svn')
            (self.a / '.svn').mkdir()
            return {'mode': 'live', 'rows': []}
        adapter = Mock(); adapter.snapshot.side_effect = changed
        self.factory.side_effect = None; self.factory.return_value = adapter
        with self.assertRaises(ValueError):
            self.catalog.select(candidate.id)
        self.assertIsNone(self.catalog.selected)

    def test_inflight_operation_cannot_switch_or_scan(self):
        with self.catalog.operation():
            for action in [self.catalog.snapshot, self.catalog.discover,
                           lambda: self.catalog.select(self.candidate(self.a).id)]:
                with self.assertRaises(BlockingIOError):
                    action()
        self.factory.assert_not_called()

    def test_partial_discovery_is_exposed_not_claimed_complete(self):
        real = self.discovery.discover_working_copies
        with patch.object(self.discovery, 'discover_working_copies', side_effect=lambda parent: real(parent, max_directories=1)):
            result = self.catalog.discover()
        self.assertFalse(result['scans'][0]['complete'])
        self.assertTrue(result['scans'][0]['truncated'])
        self.assertIn('directory_limit', result['scans'][0]['reasons'])

    def test_settings_inside_scope_or_working_copy_are_refused(self):
        for path in [self.parent / 'prefs.json', self.a / '.svn' / 'prefs.json', self.a / 'prefs.json']:
            with self.assertRaises(ValueError):
                self.new_catalog(settings_file=path)

    def test_preferences_symlink_is_refused(self):
        target = self.home / 'target.json'; target.write_text('{}')
        make_test_symlink(self, self.settings_file, target, target_is_directory=False)
        with self.assertRaises(ValueError):
            self.new_catalog(settings_file=self.settings_file)
        self.assertEqual(target.read_text(), '{}')

    def test_explicit_root_wins_over_saved_selection(self):
        self.settings_file.write_text(json.dumps({'last_workspace': str(self.b)}))
        catalog = WorkspaceCatalog(roots=[self.a], parents=[self.parent], discovery=self.discovery,
            settings=self.settings, adapter_factory=self.factory, settings_file=self.settings_file)
        self.assertEqual(catalog.config()['root'], str(self.a))

    def test_explicit_missing_svn_does_not_fall_back_to_demo(self):
        self.catalog.adapter_factory = lambda path: ReadOnlyAdapter(path, BACKEND, str(self.home / 'missing-svn'))
        with self.assertRaises(RuntimeError):
            self.catalog.select(self.candidate(self.a).id)
        self.assertEqual(self.catalog.config()['mode'], 'select')
        self.assertIsNone(self.catalog.selected)

    def test_adapter_rejects_links_before_backend_import(self):
        link = self.home / 'linked'; make_test_symlink(self, link, self.a)
        with self.assertRaises(ValueError), patch('server.trusted_modules') as load:
            ReadOnlyAdapter(link, BACKEND)
        load.assert_not_called()


class SymlinkFixturePolicyTests(unittest.TestCase):
    def test_only_windows_1314_skips_fixture(self):
        link = Mock()
        error = OSError('fixture privilege unavailable'); error.winerror = 1314
        link.symlink_to.side_effect = error
        with self.assertRaises(unittest.SkipTest):
            make_test_symlink(self, link, Path('target'))

    def test_other_errors_remain_test_failures(self):
        for error in (PermissionError('not a Windows privilege error'), FileExistsError('wrong fixture'), OSError('unexpected failure')):
            link = Mock(); link.symlink_to.side_effect = error
            with self.assertRaises(type(error)):
                make_test_symlink(self, link, Path('target'))



class SelectionHttpTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Mock()
        self.catalog.select.return_value = {'mode': 'live', 'rows': [], 'workspaceId': 'allowed'}
        self.server = PrototypeServer(0, catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, body='{"id":"allowed"}', headers=None, path='/api/workspace-selection', method='POST'):
        defaults = {'X-SVN-Desk-Token': self.server.token, 'Content-Type': 'application/json'}
        defaults.update(headers or {})
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        c.request(method, path, body=body, headers=defaults)
        response = c.getresponse(); data = response.read(); code = response.status; c.close()
        return code, json.loads(data)

    def test_one_id_selection_only(self):
        self.assertEqual(self.request()[0], 200)
        self.catalog.select.assert_called_once_with('allowed')

    def test_cross_origin_and_missing_token_blocked(self):
        for headers in [{'Origin': 'https://evil.test'}, {'Host': 'evil.test'}, {'Sec-Fetch-Site': 'cross-site'}, {'X-SVN-Desk-Token': ''}]:
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.catalog.select.assert_not_called()

    def test_malformed_schema_paths_duplicates_and_content_types_blocked(self):
        for body in ['{}', '[]', '{', '{"id":1}', '{"id":""}', '{"id":"x","path":"/tmp"}', '{"id":"x","command":"commit"}', '{"id":"x","id":"y"}', '[' * 1500 + ']' * 1500]:
            self.assertEqual(self.request(body=body)[0], 400)
        self.assertEqual(self.request(headers={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.request(path='/api/workspace-selection?path=/tmp')[0], 400)
        self.assertEqual(self.request(body='x' * 4097)[0], 413)
        self.catalog.select.assert_not_called()

    def test_selection_errors_are_sanitized_and_not_retried(self):
        for error, status in [(KeyError('raw secret'), 404), (BlockingIOError('raw secret'), 409), (RuntimeError('raw secret'), 503)]:
            self.catalog.select.side_effect = error
            code, data = self.request()
            self.assertEqual(code, status)
            self.assertNotIn('raw secret', str(data))
        self.assertEqual(self.catalog.select.call_count, 3)

    def test_no_svn_write_endpoint_or_method(self):
        for path in ['/api/commit', '/api/revert', '/api/exec', '/api/shelf']:
            self.assertEqual(self.request(path=path)[0], 405)
        for method in ['PUT', 'PATCH', 'DELETE']:
            self.assertEqual(self.request(method=method)[0], 405)
        self.catalog.select.assert_not_called()


@unittest.skipUnless(shutil.which('svn') and (BACKEND / 'svn_client/status_stream.py').is_file(), 'Real invalid-WC regression requires installed SVN and backend source.')
class RealInvalidWorkingCopyTests(unittest.TestCase):
    def test_empty_svn_marker_is_not_a_valid_status_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / '.svn').mkdir()
            adapter = ReadOnlyAdapter(path, BACKEND)
            with self.assertRaises(RuntimeError):
                adapter.snapshot()
        # Only a read-only status subprocess; no checkout, update or commit.


@unittest.skipUnless(shutil.which('svn') and os.environ.get('SVN_DESK_TEST_WORKSPACE'), 'Real SVN not run: requires installed svn and explicit SVN_DESK_TEST_WORKSPACE.')
class RealStatusTests(unittest.TestCase):
    def test_real_read_only_status(self):
        adapter = ReadOnlyAdapter(Path(os.environ['SVN_DESK_TEST_WORKSPACE']), BACKEND)
        result = adapter.snapshot()
        self.assertEqual(result['mode'], 'live')
        self.assertIsInstance(result['rows'], list)
        # No fixture checkout/update/commit. This test performs only local status.


if __name__ == '__main__':
    unittest.main()
