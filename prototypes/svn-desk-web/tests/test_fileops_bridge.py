"""Local move/copy HTTP, adapter and catalog wiring with synthetic fixtures only."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from server import PrototypeServer, ReadOnlyAdapter, main
from file_ops import FileOperationUnavailable
from file_reads import StaleRead
from workspace_bridge import WorkspaceCatalog, Candidate, working_copy_identity


SERVICES = ('_changelist_service', '_shelf_service', '_commit_service',
            '_lock_service', '_add_service', '_fileop_service')
CAPABILITIES = ('fileReadsEnabled', 'propertyReadsEnabled', 'changelistWritesEnabled',
                'shelfWritesEnabled', 'commitsEnabled', 'locksEnabled', 'addsEnabled',
                'fileOpsEnabled', 'writesEnabled')


def scope(**extra):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', **extra)


def preview(**extra):
    return {**scope(action='move', id='a.txt', destination='renamed.txt'), **extra}


def outcome(**extra):
    return dict(action='move', files=['a.txt'], destination='renamed.txt',
                affectedPaths=['a.txt', 'renamed.txt'], outcome='verified',
                verified=True, commitRequired=True, warnings=[], **extra)


class FileOperationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Mock()
        self.catalog.file_operation.return_value = {'fixture': True}
        self.server = PrototypeServer(0, catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, route='/api/file-op-preflight', data=None, headers=None,
                raw=None, method='POST'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, route,
                body=raw if raw is not None else json.dumps(preview() if data is None else data),
                headers=headers if headers is not None else {
                    'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_move_copy_route_and_explicit_apply(self):
        for action in ('move', 'copy'):
            self.assertEqual(self.request(data=preview(action=action))[0], 200)
            self.catalog.file_operation.assert_called_with('preflight', preview(action=action))
        self.catalog.file_operation.reset_mock()
        for confirmed in (False, 1, 'true', None):
            self.assertEqual(self.request('/api/file-op-apply', scope(planToken='p', confirmed=confirmed))[0], 400)
        for field in ('force', 'id', 'ids', 'action', 'destination', 'comment', 'url', 'token'):
            self.assertEqual(self.request('/api/file-op-apply',
                scope(planToken='p', confirmed=True, **{field: 'x'}))[0], 400)
        self.catalog.file_operation.assert_not_called()
        self.assertEqual(self.request('/api/file-op-apply', scope(planToken='p', confirmed=True))[0], 200)
        self.catalog.file_operation.assert_called_once_with('apply', scope(planToken='p', confirmed=True))

    def test_strict_preflight_scope_and_no_arbitrary_commands(self):
        for change in ({'id': []}, {'ids': ['a.txt']}, {'action': 'rename'}, {'action': 'delete'},
                       {'action': 'force'}, {'command': 'move'}, {'recursive': True},
                       {'parents': True}, {'force': True}, {'url': 'https://outside.test'},
                       {'selectionVersion': True}, {'selectionVersion': 0}, {'snapshotToken': ''},
                       {'workspaceId': 'w\n'}, {'destination': None}, {'destination': ''}):
            with self.subTest(change=change):
                self.assertEqual(self.request(data=preview(**change))[0], 400)
        self.catalog.file_operation.assert_not_called()

    def test_source_and_destination_reject_traversal_absolute_and_metadata(self):
        for field in ('id', 'destination'):
            for path in ('../secret', '/absolute', 'C:\\secret', '//server/share',
                         '.svn/entries', 'a/../secret', 'a//b', 'a/./b', 'https://outside.test/a'):
                with self.subTest(field=field, path=path):
                    self.assertEqual(self.request(data=preview(**{field: path}))[0], 400)
        self.catalog.file_operation.assert_not_called()

    def test_auth_origin_host_and_fetch_site_fail_closed(self):
        auth = {'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token}
        for headers in ({'Content-Type': 'application/json'},
                        {**auth, 'Origin': 'https://outside.test'},
                        {**auth, 'Host': 'outside.test'}, {**auth, 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.catalog.file_operation.assert_not_called()

    def test_duplicate_fields_body_limit_and_framing_rejected(self):
        raw = json.dumps(preview()).replace('"workspaceId": "w"', '"workspaceId":"w","workspaceId":"w"')
        self.assertEqual(self.request(raw=raw)[0], 400)
        auth = {'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token}
        self.assertEqual(self.request(headers={**auth, 'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertEqual(self.request(headers={**auth, 'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.request(raw=' ' * 262145)[0], 413)
        self.assertEqual(self.request(raw='')[0], 413)
        self.assertEqual(self.request(raw='[]')[0], 400)
        self.catalog.file_operation.assert_not_called()

    def test_sanitized_failures_never_retry(self):
        for kind in ('preflight', 'apply'):
            data = preview() if kind == 'preflight' else scope(planToken='p', confirmed=True)
            for error, expected in ((FileOperationUnavailable('SECRET'), 403),
                                    (StaleRead('SECRET'), 409), (BlockingIOError('SECRET'), 409),
                                    (RuntimeError('SECRET'), 503)):
                with self.subTest(kind=kind, expected=expected):
                    self.catalog.file_operation.reset_mock()
                    self.catalog.file_operation.side_effect = error
                    code, body = self.request('/api/file-op-' + kind, data)
                    self.assertEqual(code, expected)
                    self.assertNotIn(b'SECRET', body)
                    self.catalog.file_operation.assert_called_once_with(kind, data)
                    if expected == 503 and kind == 'apply':
                        self.assertIn('没有自动重试或回滚', body.decode())

    def test_demo_wrong_methods_and_query_never_dispatch(self):
        self.assertEqual(self.request('/api/file-op-preflight?force=true')[0], 400)
        for method in ('GET', 'PUT', 'PATCH', 'DELETE'):
            self.assertIn(self.request(method=method)[0], (404, 405))
        self.server.catalog = None
        for kind in ('preflight', 'apply'):
            self.assertEqual(self.request('/api/file-op-' + kind)[0], 405)
        self.catalog.file_operation.assert_not_called()


class FileOperationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        self.adapter.workspace = Path('/SYNTHETIC/wc')
        self.adapter.client = Mock()
        self.adapter.client.status.return_value = []
        self.adapter._check_workspace = Mock()
        self.adapter._busy = threading.Lock()
        self.adapter._read_snapshot = ('s', {})
        for name in SERVICES:
            setattr(self.adapter, name, None)
        self.adapter._file_service = None
        self.adapter._property_service = None
        self.adapter._fileop_service = Mock()
        self.result = outcome()
        self.adapter._fileop_service.apply.return_value = (self.result, {})

    def test_workspace_identity_failure_invalidates_all_snapshot_capabilities(self):
        del self.adapter._check_workspace
        self.adapter._identity = ('old',)
        for name in SERVICES:
            setattr(self.adapter, name, Mock())
        with patch('server.working_copy_identity', return_value=('changed',)):
            with self.assertRaises(ValueError):
                self.adapter._check_workspace()
        self.assertIsNone(self.adapter._read_snapshot)
        for name in SERVICES:
            getattr(self.adapter, name).invalidate.assert_called_once()

    def test_constructor_default_off_reviewed_gate_and_manager_wiring(self):
        backend = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1, SvnClient=Mock())
        stream = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1)
        history = object()
        with patch('server.local_path', return_value=Path('/synthetic')), \
             patch('server.working_copy_identity', return_value=()), \
             patch('server.trusted_modules', return_value=(backend, stream, None, None)), \
             patch('server.history_module', return_value=history), patch('server.FileReadService'), \
             patch('server.reviewed_commit_backend', return_value=False) as reviewed, \
             patch('server.fileops_module') as module, \
             patch('server.guarded_fileops_manager') as manager, patch('server.FileOperationService') as service:
            adapter = ReadOnlyAdapter(Path('/synthetic'), Path('/backend'))
            self.assertIsNone(adapter._fileop_service)
            module.assert_not_called()
            adapter = ReadOnlyAdapter(Path('/synthetic'), Path('/backend'), enable_file_ops=True)
            self.assertIsNone(adapter._fileop_service)
            module.assert_not_called()
            service.assert_not_called()
            reviewed.return_value = True
            module.return_value = None
            adapter = ReadOnlyAdapter(Path('/synthetic'), Path('/backend'), enable_file_ops=True)
            self.assertIsNone(adapter._fileop_service)
            manager.assert_not_called()
            module.return_value = object()
            adapter = ReadOnlyAdapter(Path('/synthetic'), Path('/backend'), enable_file_ops=True)
            self.assertIs(adapter._fileop_service, service.return_value)
            manager.assert_called_once_with(module.return_value, history, adapter.client)
            service.assert_called_once_with(manager.return_value, adapter._check_workspace)
            for name in SERVICES[:-1]:
                self.assertIsNone(getattr(adapter, name))

    def test_snapshot_refresh_invalidates_even_when_read_fails(self):
        self.adapter.client.status.side_effect = ValueError('fixture')
        with self.assertRaises(ValueError):
            self.adapter.snapshot()
        self.adapter._fileop_service.invalidate.assert_called_once()
        self.assertIsNone(self.adapter._read_snapshot)
        self.assertFalse(self.adapter._busy.locked())

    def test_fileops_only_status_is_local_verbose_and_keeps_clean_rows(self):
        rows = {'a.txt': {'id': 'a.txt', 'path': 'a.txt', 'item': 'normal', 'props': 'none'}}
        with patch('server.status_rows', return_value=rows) as status:
            payload = self.adapter.snapshot()
        status.assert_called_once_with(self.adapter.client)
        self.assertEqual(payload['rows'], list(rows.values()))
        self.assertTrue(payload['fileOpsEnabled'])
        self.assertFalse(payload['locksEnabled'])
        self.assertFalse(payload['commitsEnabled'])
        self.adapter.snapshot()
        self.adapter.client.status.assert_called_once_with(verbose=True)

    def test_disabled_and_busy_never_queue(self):
        service = self.adapter._fileop_service
        self.adapter._fileop_service = None
        with self.assertRaises(FileOperationUnavailable):
            self.adapter.file_operation('preflight', preview())
        self.adapter._fileop_service = service
        self.adapter._busy.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                self.adapter.file_operation('preflight', preview())
        finally:
            self.adapter._busy.release()
        service.preflight.assert_not_called()

    def test_preflight_uses_snapshot_and_invalidates_on_postcheck_failure(self):
        self.adapter._check_workspace.side_effect = [None, ValueError('changed')]
        with self.assertRaises(ValueError):
            self.adapter.file_operation('preflight', preview())
        self.adapter._fileop_service.preflight.assert_called_once_with(preview(), ('s', {}))
        self.adapter._fileop_service.invalidate.assert_called_once()
        self.assertFalse(self.adapter._busy.locked())

    def test_apply_invalidates_all_plans_before_execution_and_no_refresh_retry(self):
        for name in SERVICES[:-1]:
            setattr(self.adapter, name, Mock())
        def apply(data, snapshot, invalidate):
            self.assertEqual(snapshot, ('s', {}))
            invalidate()
            self.assertIsNone(self.adapter._read_snapshot)
            for name in SERVICES:
                getattr(self.adapter, name).invalidate.assert_called_once()
            return self.result, {}
        self.adapter._fileop_service.apply.side_effect = apply
        payload = self.adapter.file_operation('apply', scope(planToken='p', confirmed=True))
        self.assertTrue(payload['statusRefreshed'])
        self.assertEqual(payload['fileOpResult'], self.result)
        self.adapter.client.status.assert_not_called()
        self.assertTrue(payload['fileOpsEnabled'])

    def test_every_other_mutation_invalidates_fileop_plan(self):
        for operation in ('changelist', 'shelf', 'commit', 'lock', 'add'):
            with self.subTest(operation=operation):
                self.adapter._read_snapshot = ('s', {})
                self.adapter._fileop_service.invalidate.reset_mock()
                service = Mock()
                setattr(self.adapter, '_' + operation + '_service', service)
                def apply(data, snapshot, invalidate):
                    invalidate()
                    self.adapter._fileop_service.invalidate.assert_called_once()
                    return (self.result, {}) if operation in ('changelist', 'add') else self.result
                service.apply.side_effect = apply
                getattr(self.adapter, operation + '_operation')('apply', scope(planToken='p', confirmed=True))
                setattr(self.adapter, '_' + operation + '_service', None)

    def assert_unknown(self, payload):
        self.assertFalse(payload['statusRefreshed'])
        self.assertTrue(payload['fileOpResult']['verified'])
        self.assertEqual(payload['fileOpResult']['warnings'], ['status-refresh-failed'])
        for flag in CAPABILITIES:
            self.assertFalse(payload[flag], flag)
        self.assertEqual(payload['rows'], [])
        self.assertIsNone(payload['snapshotToken'])
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter.client.status.assert_not_called()
        self.adapter._fileop_service.apply.assert_called_once()

    def test_verified_result_survives_missing_refreshed_rows(self):
        self.adapter._fileop_service.apply.return_value = (self.result, None)
        self.assert_unknown(self.adapter.file_operation('apply', scope(planToken='p', confirmed=True)))

    def test_verified_result_survives_postcheck_failure(self):
        self.adapter._check_workspace.side_effect = [None, ValueError('changed')]
        self.assert_unknown(self.adapter.file_operation('apply', scope(planToken='p', confirmed=True)))

    def test_verified_result_survives_publication_failure(self):
        with patch.object(self.adapter, '_publish_snapshot', side_effect=ValueError('fixture')):
            self.assert_unknown(self.adapter.file_operation('apply', scope(planToken='p', confirmed=True)))

    def test_other_unknown_results_revoke_fileops(self):
        for operation in ('add', 'commit', 'lock'):
            with self.subTest(operation=operation):
                self.adapter._fileop_service.invalidate.reset_mock()
                payload = getattr(self.adapter, '_' + operation + '_status_unknown')(self.result, 'fixture')
                self.assertFalse(payload['fileOpsEnabled'])
                self.adapter._fileop_service.invalidate.assert_called_once()


class FileOperationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wc = Path(self.tmp.name) / 'synthetic'
        (self.wc / '.svn').mkdir(parents=True)
        self.candidate = Candidate('w', self.wc, working_copy_identity(self.wc), 'explicit')
        self.adapter = Mock()
        self.adapter._read_snapshot = ('s', {})
        for name in SERVICES:
            setattr(self.adapter, name, None)
        self.adapter._fileop_service = Mock()
        self.adapter.file_operation.return_value = {'fixture': True}
        self.catalog = WorkspaceCatalog.__new__(WorkspaceCatalog)
        self.catalog._selection = (self.candidate, self.adapter, 1)
        self.catalog._busy = threading.Lock()
        self.catalog.parent_identities = {}
        self.catalog.settings_file = None
        self.catalog.settings_warning = None
        self.catalog.candidates = {'w': self.candidate}

    def replace_metadata(self):
        (self.wc / '.svn').rename(self.wc / 'old')
        (self.wc / '.svn').mkdir()

    def test_routes_both_operations_with_identity(self):
        for kind, data in (('preflight', preview()), ('apply', scope(planToken='p', confirmed=True))):
            result = self.catalog.file_operation(kind, data)
            self.adapter.file_operation.assert_called_with(kind, data)
            self.assertEqual(result['workspaceId'], 'w')
            self.assertEqual(result['selectionVersion'], 1)
            self.assertTrue(result['fileOpsEnabled'])

    def test_stale_scope_rejected_before_adapter(self):
        for change in ({'workspaceId': 'other'}, {'selectionVersion': 2}):
            with self.assertRaises(StaleRead):
                self.catalog.file_operation('preflight', preview(**change))
        self.adapter.file_operation.assert_not_called()

    def test_replaced_working_copy_revokes_snapshot_and_plans(self):
        self.replace_metadata()
        with self.assertRaises(ValueError):
            self.catalog.file_operation('preflight', preview())
        self.adapter.file_operation.assert_not_called()
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter._fileop_service.invalidate.assert_called_once()
        self.assertFalse(self.catalog.config()['fileOpsEnabled'])

    def test_shared_lock_prevents_selection_and_status_races(self):
        self.catalog._busy.acquire()
        try:
            with self.assertRaises(BlockingIOError):
                self.catalog.file_operation('preflight', preview())
        finally:
            self.catalog._busy.release()
        self.adapter.file_operation.assert_not_called()

    def test_capability_only_with_valid_snapshot_and_is_independent(self):
        self.assertTrue(self.catalog.config()['fileOpsEnabled'])
        self.assertTrue(self.catalog.config()['writesEnabled'])
        self.assertFalse(self.catalog.config()['commitsEnabled'])
        for snapshot in (None, (), ('', {}), ('s', []), ['s', {}]):
            self.adapter._read_snapshot = snapshot
            self.assertFalse(self.catalog.config()['fileOpsEnabled'])
            self.assertFalse(self.catalog.config()['writesEnabled'])
        self.adapter._read_snapshot = ('s', {})
        self.adapter._fileop_service = None
        self.assertFalse(self.catalog.config()['fileOpsEnabled'])

    def test_unknown_snapshot_revokes_all_configured_write_capabilities(self):
        for name in SERVICES:
            setattr(self.adapter, name, Mock())
        self.adapter._read_snapshot = None
        payload = self.catalog.config()
        for flag in CAPABILITIES:
            self.assertFalse(payload[flag], flag)

    def test_unknown_response_capabilities_win_over_configuration(self):
        self.adapter.file_operation.return_value = {'statusRefreshed': False,
            **{flag: False for flag in CAPABILITIES}, 'fileOpResult': outcome()}
        result = self.catalog.file_operation('apply', scope(planToken='p', confirmed=True))
        for flag in CAPABILITIES:
            self.assertFalse(result[flag], flag)
        self.assertTrue(result['fileOpResult']['verified'])

    def test_replaced_workspace_after_verified_result_preserves_fact(self):
        result = outcome()
        def change(kind, data):
            self.replace_metadata()
            return {'fileOpResult': result}
        self.adapter.file_operation.side_effect = change
        self.adapter._fileop_status_unknown.return_value = {'fileOpResult': result,
            'statusRefreshed': False, **{flag: False for flag in CAPABILITIES}}
        payload = self.catalog.file_operation('apply', scope(planToken='p', confirmed=True))
        self.assertTrue(payload['fileOpResult']['verified'])
        for flag in CAPABILITIES:
            self.assertFalse(payload[flag], flag)
        self.adapter._fileop_status_unknown.assert_called_once_with(result, 'workspace-changed')

    def test_changed_preflight_cannot_publish_plan(self):
        def change(kind, data):
            self.replace_metadata()
            return {'planToken': 'p'}
        self.adapter.file_operation.side_effect = change
        with self.assertRaises(ValueError):
            self.catalog.file_operation('preflight', preview())
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter._fileop_service.invalidate.assert_called()
        self.adapter._fileop_status_unknown.assert_not_called()

    def test_failed_status_revokes_existing_plan(self):
        self.adapter.snapshot.side_effect = ValueError('fixture')
        with self.assertRaises(ValueError):
            self.catalog.snapshot()
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter._fileop_service.invalidate.assert_called_once()

    def test_successful_selection_revokes_previous_plan(self):
        next_adapter = Mock()
        next_adapter._read_snapshot = ('new', {})
        next_adapter.snapshot.return_value = {'mode': 'live', 'rows': []}
        self.catalog.adapter_factory = Mock(return_value=next_adapter)
        self.catalog.select('w')
        self.adapter._fileop_service.invalidate.assert_called_once()
        self.assertIsNone(self.adapter._read_snapshot)
        self.assertIs(self.catalog.adapter, next_adapter)
        self.assertEqual(self.catalog.generation, 2)


class FileOperationCLITests(unittest.TestCase):
    def test_flag_requires_authorized_working_copy(self):
        with patch('sys.argv', ['server.py', '--enable-file-ops']), \
             patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_default_and_explicit_flag_reach_adapter(self):
        for enabled in (False, True):
            args = ['server.py', '--workspace', '/synthetic', '--backend-source', '/backend']
            if enabled:
                args.append('--enable-file-ops')
            with patch('sys.argv', args), patch('server.trusted_modules', return_value=(None, None, Mock(), Mock())), \
                 patch('server.WorkspaceCatalog') as catalog, patch('server.ReadOnlyAdapter') as adapter, \
                 patch('server.PrototypeServer') as server, patch('sys.stdout', new=io.StringIO()):
                server.return_value.serve_forever.side_effect = KeyboardInterrupt
                main()
                catalog.call_args.kwargs['adapter_factory'](Path('/synthetic'))
                self.assertEqual(adapter.call_args.kwargs['enable_file_ops'], enabled)
                server.return_value.server_close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
