"""Exact-file property bridge safety with synthetic adapters and loopback HTTP only."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from file_reads import StaleRead
from server import PrototypeServer, ReadOnlyAdapter
from workspace_bridge import Candidate, WorkspaceCatalog, working_copy_identity


def request_data(**changes):
    return {'workspaceId': 'w', 'selectionVersion': 1, 'snapshotToken': 's',
            'id': 'a.txt', **changes}


class PropertyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Mock()
        self.catalog.file_read.return_value = {'fixture': True}
        self.server = PrototypeServer(0, catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, data=None, *, path='/api/file-properties', headers=None, raw=None, method='POST'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw if raw is not None else json.dumps(
                request_data() if data is None else data), headers=headers if headers is not None else {
                    'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_exact_four_field_route(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'fixture': True})
        self.catalog.file_read.assert_called_once_with('properties', request_data())

    def test_rejects_extra_fields_missing_fields_and_wrong_types(self):
        invalid = [request_data(**{field: 'x'}) for field in
                   ('revision', 'fromRevision', 'toRevision', 'startRevision', 'url', 'path', 'command', 'recursive')]
        invalid += [{k: v for k, v in request_data().items() if k != field} for field in request_data()]
        invalid += [request_data(selectionVersion=value) for value in (True, False, 0, -1, 1.5, '1', None, 2**53)]
        for field, maximum in (('workspaceId', 64), ('snapshotToken', 64), ('id', 2048)):
            invalid += [request_data(**{field: value}) for value in ('', None, 1, [], 'x\n', 'x\x7f', 'x' * (maximum + 1))]
        invalid += [[], 'string', 5, None]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.request(raw=json.dumps(value))[0], 400)
        self.catalog.file_read.assert_not_called()

    def test_auth_origin_host_and_fetch_site(self):
        valid = {'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token}
        for headers in ({'Content-Type': 'application/json'}, {**valid, 'X-SVN-Desk-Token': 'wrong'},
                        {**valid, 'Origin': 'https://outside.test'}, {**valid, 'Host': 'outside.test'},
                        {**valid, 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.catalog.file_read.assert_not_called()
        self.assertEqual(self.request(headers={**valid, 'Origin': self.server.origin})[0], 200)

    def test_bounded_json_and_duplicate_keys(self):
        valid = {'Content-Type': 'application/json', 'X-SVN-Desk-Token': self.server.token}
        for raw in ('{', '{"id":"a","id":"a"}', json.dumps(request_data())[:-1] + ',"workspaceId":"w"}'):
            self.assertEqual(self.request(raw=raw)[0], 400)
        from http_body_test_helpers import assert_oversized_body_rejected
        assert_oversized_body_rejected(self, self.server, '/api/file-properties',
            body=' ' * 4097, limit=4096,
            assert_no_dispatch=lambda: self.assertEqual(self.catalog.mock_calls, []))
        self.assertEqual(self.request(headers={**valid, 'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.request(headers={**valid, 'Transfer-Encoding': 'chunked'})[0], 400)
        self.catalog.file_read.assert_not_called()

    def test_failures_sanitized_without_partial_result(self):
        for exception, expected in ((StaleRead('SECRET'), 409), (BlockingIOError('SECRET'), 409),
                                    (RuntimeError('SECRET partial-property'), 503),
                                    (ValueError('SECRET disabled capability'), 503)):
            self.catalog.file_read.side_effect = exception
            code, body = self.request()
            self.assertEqual(code, expected)
            self.assertNotIn(b'SECRET', body)
            self.assertEqual(set(json.loads(body)), {'error'})

    def test_disabled_or_missing_adapter_capability_returns_sanitized_503(self):
        adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        adapter._busy = threading.Lock()
        adapter._check_workspace = Mock()
        adapter._read_snapshot = ('s', {'a.txt': {'id': 'a.txt', 'item': 'modified'}})
        adapter._file_service = Mock()
        self.catalog.file_read.side_effect = adapter.file_read
        for missing in (False, True):
            if missing:
                del adapter._property_service
            else:
                adapter._property_service = None
            code, body = self.request()
            self.assertEqual(code, 503)
            self.assertEqual(set(json.loads(body)), {'error'})
        adapter._file_service.read.assert_not_called()
        for data in (request_data(snapshotToken='old'), request_data(id='other.txt')):
            self.assertEqual(self.request(data)[0], 409)

    def test_no_demo_fallback_and_wrong_method_or_query(self):
        self.assertEqual(self.request(method='GET')[0], 404)
        self.assertEqual(self.request(path='/api/file-properties?path=x')[0], 400)
        self.server.catalog = None
        self.assertEqual(self.request()[0], 405)
        self.catalog.file_read.assert_not_called()

    def test_existing_read_endpoints_keep_their_schema_and_dispatch(self):
        diff = request_data(fromRevision='BASE', toRevision='WORKING')
        history = request_data(startRevision='HEAD')
        for path, kind, data in (('/api/file-diff', 'diff', diff), ('/api/file-history', 'history', history)):
            self.assertEqual(self.request(data, path=path)[0], 200)
            self.catalog.file_read.assert_called_with(kind, data)
            self.assertEqual(self.request(path=path)[0], 400)
        self.assertEqual(self.request(diff)[0], 400)
        self.assertEqual(self.request(history)[0], 400)


class PropertyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
        self.adapter.workspace = Path('/synthetic/wc')
        self.adapter._busy = threading.Lock()
        self.adapter._check_workspace = Mock()
        self.adapter._file_service = Mock()
        self.adapter._property_service = Mock()
        self.adapter._property_service.read.return_value = {'properties': [], 'baseAvailable': True}
        self.row = {'id': 'a.txt', 'path': 'a.txt', 'item': 'modified'}
        self.adapter._read_snapshot = ('s', {'a.txt': self.row})

    def test_exact_snapshot_row_and_post_read_workspace_validation(self):
        result = self.adapter.file_read('properties', request_data())
        self.adapter._property_service.read.assert_called_once_with(self.row)
        self.adapter._file_service.read.assert_not_called()
        self.assertEqual(self.adapter._check_workspace.call_count, 2)
        self.assertEqual(result, {'properties': [], 'baseAvailable': True, 'id': 'a.txt', 'snapshotToken': 's'})
        self.assertFalse(self.adapter._busy.locked())

    def test_old_snapshot_unknown_row_and_revoked_snapshot(self):
        for data in (request_data(snapshotToken='old'), request_data(id='other.txt')):
            with self.assertRaises(StaleRead):
                self.adapter.file_read('properties', data)
        self.adapter._read_snapshot = None
        with self.assertRaises(StaleRead):
            self.adapter.file_read('properties', request_data())
        self.adapter._property_service.read.assert_not_called()

    def test_disabled_and_missing_service_fail_closed(self):
        for missing in (False, True):
            if missing:
                del self.adapter._property_service
            else:
                self.adapter._property_service = None
            with self.assertRaises(ValueError):
                self.adapter.file_read('properties', request_data())
        self.adapter._file_service.read.assert_not_called()

    def test_busy_guard_and_failure_release_lock(self):
        self.adapter._busy.acquire()
        with self.assertRaises(BlockingIOError):
            self.adapter.file_read('properties', request_data())
        self.adapter._busy.release()
        self.adapter._property_service.read.assert_not_called()
        self.adapter._property_service.read.side_effect = RuntimeError('private')
        with self.assertRaises(RuntimeError):
            self.adapter.file_read('properties', request_data())
        self.assertFalse(self.adapter._busy.locked())

    def test_workspace_replacement_never_returns_results(self):
        self.adapter._check_workspace.side_effect = [None, ValueError('changed')]
        with self.assertRaises(ValueError):
            self.adapter.file_read('properties', request_data())
        self.assertFalse(self.adapter._busy.locked())

    def test_capability_only_advertised_when_service_exists(self):
        self.assertTrue(self.adapter._publish_snapshot([self.row])['propertyReadsEnabled'])
        self.adapter._property_service = None
        self.assertFalse(self.adapter._publish_snapshot([self.row])['propertyReadsEnabled'])
        del self.adapter._property_service
        self.assertFalse(self.adapter._publish_snapshot([self.row])['propertyReadsEnabled'])

    def test_unknown_mutation_snapshots_revoke_property_capability(self):
        for method in ('_commit_status_unknown', '_lock_status_unknown', '_add_status_unknown'):
            self.adapter._read_snapshot = ('s', {'a.txt': self.row})
            result = getattr(self.adapter, method)({'verified': True}, 'status-refresh-failed')
            self.assertFalse(result['propertyReadsEnabled'])
            self.assertIsNone(result['snapshotToken'])
            self.assertIsNone(self.adapter._read_snapshot)

    def test_initialization_enables_property_reads_without_write_flags(self):
        backend = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1, SvnClient=Mock())
        stream = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1)
        history = object()
        file_service = Mock()
        with patch('server.local_path', return_value=self.adapter.workspace), \
             patch('server.working_copy_identity', return_value='identity'), \
             patch('server.trusted_modules', return_value=(backend, stream, None, None)), \
             patch('server.history_module', return_value=history), \
             patch('server.FileReadService', return_value=file_service), \
             patch('server.PropertyReadService') as properties:
            adapter = ReadOnlyAdapter(self.adapter.workspace, Path('/synthetic/backend'))
            properties.assert_called_once_with(backend.SvnClient.return_value, history, file_service.manager)
            self.assertTrue(adapter._publish_snapshot([])['propertyReadsEnabled'])
            self.assertIsNone(adapter._commit_service)
            self.assertIsNone(adapter._add_service)
            self.assertIsNone(adapter._lock_service)


    def test_missing_strict_history_backend_does_not_create_property_service(self):
        backend = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1, SvnClient=Mock())
        stream = SimpleNamespace(STATUS_DIAGNOSTICS_POLICY=1)
        with patch('server.local_path', return_value=self.adapter.workspace), \
             patch('server.working_copy_identity', return_value='identity'), \
             patch('server.trusted_modules', return_value=(backend, stream, None, None)), \
             patch('server.history_module', return_value=None), \
             patch('server.PropertyReadService') as properties:
            adapter = ReadOnlyAdapter(self.adapter.workspace, Path('/synthetic/backend'))
            properties.assert_not_called()
            self.assertFalse(adapter._publish_snapshot([])['propertyReadsEnabled'])


class PropertyCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.wc = Path(temporary.name) / 'synthetic'
        (self.wc / '.svn').mkdir(parents=True)
        candidate = Candidate('w', self.wc, working_copy_identity(self.wc), 'explicit')
        self.adapter = Mock()
        self.adapter.file_read.return_value = {'properties': [], 'snapshotToken': 's'}
        self.catalog = WorkspaceCatalog.__new__(WorkspaceCatalog)
        self.catalog._selection = (candidate, self.adapter, 1)
        self.catalog._busy = threading.Lock()
        self.catalog.parent_identities = {}
        self.catalog.settings_file = None
        self.catalog.settings_warning = None

    def test_selection_identity_checked_before_read(self):
        for changes in ({'workspaceId': 'other'}, {'selectionVersion': 2}):
            with self.assertRaises(StaleRead):
                self.catalog.file_read('properties', request_data(**changes))
        self.adapter.file_read.assert_not_called()
        result = self.catalog.file_read('properties', request_data())
        self.assertEqual(result['properties'], [])
        self.adapter.file_read.assert_called_once_with('properties', request_data())

    def test_catalog_lock_and_workspace_identity_guard(self):
        self.catalog._busy.acquire()
        with self.assertRaises(BlockingIOError):
            self.catalog.file_read('properties', request_data())
        self.catalog._busy.release()
        (self.wc / '.svn').rename(self.wc / 'old')
        (self.wc / '.svn').mkdir()
        with self.assertRaises(ValueError):
            self.catalog.file_read('properties', request_data())
        self.adapter.file_read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
