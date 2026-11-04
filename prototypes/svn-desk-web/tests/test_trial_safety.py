"""Independent trial checks: completed Shelf facts and shared capability safety.

Only synthetic services, metadata and disposable directories are used. These
tests do not launch SVN, contact repositories or stand in for Windows GUI QA.
"""
import io
import json
from email.message import Message
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from changelist_ops import MutationUncertain
from file_reads import StaleRead
from server import Handler, ReadOnlyAdapter
from workspace_bridge import Candidate, WorkspaceCatalog, working_copy_identity


SERVICES = ('_changelist_service', '_shelf_service', '_commit_service',
            '_lock_service', '_add_service', '_fileop_service')
CAPABILITIES = ('fileReadsEnabled', 'propertyReadsEnabled', 'changelistWritesEnabled',
                'shelfWritesEnabled', 'commitsEnabled', 'locksEnabled', 'addsEnabled',
                'fileOpsEnabled', 'writesEnabled')


def scope(**extra):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', **extra)


def completed(operation='create'):
    return dict(operation=operation, shelfId='a' * 32, name='Trial backup',
                files=['a.txt'], shelfRetained=True)


def adapter_fixture(root):
    adapter = ReadOnlyAdapter.__new__(ReadOnlyAdapter)
    adapter.workspace = root
    adapter._read_snapshot = ('s', {})
    adapter._busy = threading.Lock()
    adapter._check_workspace = Mock()
    adapter._status_rows = Mock(return_value=[])
    adapter._file_service = object()
    adapter._property_service = object()
    for name in SERVICES:
        setattr(adapter, name, Mock())
    return adapter


class ShelfCompletionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = adapter_fixture(Path(self.tmp.name) / 'wc')
        self.service = self.adapter._shelf_service
        self.result = completed()

        def apply(data, snapshot, invalidate):
            self.assertEqual(snapshot, ('s', {}))
            self.service.invalidate()  # Actual ShelfService consumes its plan.
            invalidate()
            self.assertIsNone(self.adapter._read_snapshot)
            for name in SERVICES:
                getattr(self.adapter, name).invalidate.assert_called()
            return self.result
        self.service.apply.side_effect = apply

    def execute(self):
        return self.adapter.shelf_operation('apply', scope(planToken='p', confirmed=True))

    def assert_unknown_status_with_known_shelf(self, payload):
        self.assertFalse(payload['statusRefreshed'])
        self.assertEqual(payload['rows'], [])
        self.assertIsNone(payload['snapshotToken'])
        self.assertIsNone(self.adapter._read_snapshot)
        self.assertEqual(payload['shelfResult']['outcome'], 'verified')
        self.assertIs(payload['shelfResult']['verified'], True)
        for key, value in self.result.items():
            self.assertEqual(payload['shelfResult'][key], value)
        self.assertEqual(payload['shelfResult']['warnings'], ['status-refresh-failed'])
        for capability in CAPABILITIES:
            self.assertIs(payload[capability], False, capability)
        for name in SERVICES:
            getattr(self.adapter, name).invalidate.assert_called()
        self.assertFalse(self.adapter._busy.locked())
        self.service.apply.assert_called_once()

    def test_create_and_restore_success_have_separate_known_result_and_status(self):
        for operation in ('create', 'restore'):
            with self.subTest(operation=operation):
                self.adapter._read_snapshot = ('s', {})
                self.result = completed(operation)
                payload = self.execute()
                self.assertTrue(payload['statusRefreshed'])
                self.assertTrue(payload['shelfResult']['verified'])
                self.assertEqual(payload['shelfResult']['outcome'], 'verified')
                self.assertEqual(payload['shelfResult']['operation'], operation)
                self.assertEqual(payload['shelfResult']['warnings'], [])
                self.assertTrue(payload['snapshotToken'])

    def test_completed_create_survives_followup_status_failure(self):
        self.adapter._status_rows.side_effect = OSError('SECRET DIAGNOSTIC')
        payload = self.execute()
        self.assert_unknown_status_with_known_shelf(payload)
        self.assertNotIn('SECRET', json.dumps(payload))
        self.adapter._status_rows.assert_called_once()

    def test_completed_restore_survives_followup_workspace_replacement(self):
        self.result = completed('restore')
        self.adapter._check_workspace.side_effect = [None, ValueError('changed')]
        self.assert_unknown_status_with_known_shelf(self.execute())

    def test_completed_fact_survives_snapshot_publication_failure(self):
        self.adapter._publish_snapshot = Mock(side_effect=ValueError('unsafe status'))
        self.assert_unknown_status_with_known_shelf(self.execute())

    def test_service_failure_never_becomes_a_verified_result_or_automatic_retry(self):
        self.service.apply.side_effect = MutationUncertain('partial restore')
        with self.assertRaises(MutationUncertain):
            self.execute()
        self.service.apply.assert_called_once()
        self.adapter._status_rows.assert_not_called()
        self.assertFalse(self.adapter._busy.locked())

    def test_preflight_is_not_reported_as_completed_or_refreshed(self):
        self.service.preflight.return_value = scope(planToken='p')
        payload = self.adapter.shelf_operation('preflight', scope())
        self.assertNotIn('shelfResult', payload)
        self.assertNotIn('statusRefreshed', payload)
        self.service.apply.assert_not_called()
        self.adapter._status_rows.assert_not_called()


class ShelfCompletionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'wc'
        (root / '.svn').mkdir(parents=True)
        self.adapter = adapter_fixture(root)
        self.adapter._shelf_service.manager.storage_dir = Path(self.tmp.name) / 'shelves'
        self.candidate = Candidate('w', root, working_copy_identity(root), 'explicit')
        self.catalog = WorkspaceCatalog.__new__(WorkspaceCatalog)
        self.catalog._busy = threading.Lock()
        self.catalog._selection = (self.candidate, self.adapter, 1)
        self.catalog.roots, self.catalog.parents = (root,), ()
        self.catalog.parent_identities = {}
        self.catalog.settings_file = self.catalog.settings_warning = None
        self.result = {**completed(), 'outcome': 'verified', 'verified': True, 'warnings': []}

    def execute(self, kind='apply'):
        return self.catalog.shelf_operation(kind, scope(planToken='p', confirmed=True))

    def replace_metadata(self):
        path = self.candidate.path / '.svn'
        path.rename(self.candidate.path / '.svn-before')
        path.mkdir()

    def test_completed_fact_survives_catalog_postcheck_failure(self):
        def apply(*args):
            self.replace_metadata()
            return dict(shelfResult=self.result, rows=[], statusRefreshed=True)
        self.adapter.shelf_operation = Mock(side_effect=apply)
        payload = self.execute()
        self.assertTrue(payload['shelfResult']['verified'])
        self.assertEqual(payload['shelfResult']['shelfId'], self.result['shelfId'])
        self.assertEqual(payload['shelfResult']['warnings'], ['workspace-changed'])
        self.assertFalse(payload['statusRefreshed'])
        self.assertIsNone(payload['snapshotToken'])
        self.assertEqual(payload['workspaceId'], 'w')
        for capability in CAPABILITIES:
            self.assertFalse(payload[capability], capability)
        self.adapter.shelf_operation.assert_called_once()

    def test_catalog_does_not_overwrite_revoked_response_capabilities(self):
        # Deliberately keep config live to prove response merge ordering.
        payload = dict(shelfResult=self.result, rows=[], statusRefreshed=False,
                       snapshotToken=None, **{key: False for key in CAPABILITIES})
        self.assertTrue(self.catalog.config()['shelfWritesEnabled'])
        self.adapter.shelf_operation = Mock(return_value=payload)
        result = self.execute()
        for capability in CAPABILITIES:
            self.assertFalse(result[capability], capability)
        self.assertEqual(result['workspaceId'], 'w')

    def test_unverified_result_is_not_upgraded_after_catalog_postcheck_failure(self):
        def apply(*args):
            self.replace_metadata()
            return {'shelfResult': {**self.result, 'verified': False}}
        self.adapter.shelf_operation = Mock(side_effect=apply)
        with self.assertRaises(MutationUncertain):
            self.execute()

    def test_preflight_parent_change_cannot_publish_plan(self):
        def preflight(*args):
            self.replace_metadata()
            return scope(planToken='p')
        self.adapter.shelf_operation = Mock(side_effect=preflight)
        with self.assertRaises(ValueError):
            self.execute('preflight')
        self.assertIsNone(self.adapter._read_snapshot)
        self.adapter._shelf_service.invalidate.assert_called()

    def test_stale_scope_rejected_before_adapter_execution(self):
        self.adapter.shelf_operation = Mock()
        for change in ({'workspaceId': 'old'}, {'selectionVersion': 0}):
            with self.subTest(change=change), self.assertRaises(StaleRead):
                self.catalog.shelf_operation('apply', {**scope(), **change})
        self.adapter.shelf_operation.assert_not_called()

    def test_catalog_lock_blocks_all_cross_flows_while_shelf_executes(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def apply(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Synthetic test release not received.')
            return dict(shelfResult=self.result, statusRefreshed=True)
        self.adapter.shelf_operation = Mock(side_effect=apply)
        worker = threading.Thread(target=lambda: results.append(self.execute()))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            operations = [self.catalog.snapshot, self.catalog.discover,
                          lambda: self.catalog.select('w')]
            operations += [lambda name=name: getattr(self.catalog, name)('apply', scope())
                           for name in ('changelist_operation', 'shelf_operation',
                                        'commit_operation', 'lock_operation',
                                        'add_operation', 'file_operation')]
            operations.append(lambda: self.catalog.file_read('properties', scope()))
            for action in operations:
                with self.assertRaises(BlockingIOError):
                    action()
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['shelfResult']['verified'])


class ShelfCompletionSyntheticIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the original Shelf algorithms over deterministic SVN XML and
        # temporary files. No SVN executable or production directory is used.
        import test_shelf_ops as fixture
        self.fixture = fixture
        self.fx = fixture.ShelfGuardTests('test_nested_file_with_clean_metadata_ancestor_round_trip')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.adapter = adapter_fixture(self.fx.wc)
        self.adapter._shelf_service = self.fx.service
        self.adapter._read_snapshot = self.fx.snapshot
        self.adapter._status_rows.side_effect = OSError('synthetic later read failure')

    def execute(self, plan):
        return self.adapter.shelf_operation('apply', scope(planToken=plan['planToken'], confirmed=True))

    def test_real_create_result_retained_and_replay_does_not_duplicate_backup(self):
        plan = self.fx.preview()
        payload = self.execute(plan)
        self.assertTrue(payload['shelfResult']['verified'])
        self.assertFalse(payload['statusRefreshed'])
        self.assertTrue(self.fx.manifest_path(payload['shelfResult']).is_file())
        folders = list(self.fx.store.iterdir())
        self.assertIsNone(self.fx.service.plan)
        with self.assertRaises(StaleRead):
            self.execute(plan)
        self.assertEqual(list(self.fx.store.iterdir()), folders)
        self.adapter._status_rows.assert_called_once()

    def test_real_restore_result_retained_without_second_restore(self):
        created = self.fx.create()
        self.fx.fake.clean()
        self.adapter._read_snapshot = self.fx.snapshot = ('s', self.fx.rows())
        plan = self.fx.preview(operation='restore', shelfId=created['shelfId'])
        payload = self.execute(plan)
        self.assertEqual((self.fx.wc / 'a.bin').read_bytes(), self.fx.payload)
        self.assertTrue(payload['shelfResult']['verified'])
        self.assertEqual(payload['shelfResult']['operation'], 'restore')
        self.assertFalse(payload['statusRefreshed'])
        calls = list(self.fx.calls)
        with self.assertRaises(StaleRead):
            self.execute(plan)
        self.assertEqual(self.fx.calls, calls)
        self.assertTrue(self.fx.manifest_path(created).is_file())


class CrossFlowHTTPAuthorizationTests(unittest.TestCase):
    def handler(self, route, *, changes=None, body=None):
        # Exercise the actual route/auth/body/schema dispatcher without sockets.
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, origin='http://127.0.0.1:8765',
                                         token='trial-token', catalog=Mock())
        handler.path = route
        payload = json.dumps(scope(planToken='p', confirmed=True)).encode() if body is None else body
        headers = {'Host': '127.0.0.1:8765', 'Origin': handler.server.origin,
                   'X-SVN-Desk-Token': handler.server.token, 'Content-Type': 'application/json',
                   'Content-Length': str(len(payload))}
        headers.update(changes or {})
        handler.headers = Message()
        for key, value in headers.items():
            handler.headers[key] = value
        handler.rfile = io.BufferedReader(io.BytesIO(payload))
        handler.connection = Mock()
        handler.respond = Mock()
        return handler

    def test_every_mutation_route_rejects_bad_host_origin_token_or_fetch_site(self):
        for route in ('changelist', 'shelf', 'commit', 'lock', 'add', 'file-op'):
            for change in ({'Host': 'evil.test'}, {'Origin': 'https://evil.test'},
                           {'X-SVN-Desk-Token': 'wrong'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(route=route, change=change):
                    handler = self.handler('/api/' + route + '-apply', changes=change)
                    handler.do_POST()
                    self.assertEqual(handler.respond.call_args.args[0], 403)
                    self.assertEqual(handler.server.catalog.mock_calls, [])

    def test_every_mutation_route_rejects_body_and_confirmation_ambiguity(self):
        cases = [(b'{"confirmed":true,"confirmed":false}', {}, 400),
                 (json.dumps(scope(planToken='p', confirmed=1)).encode(), {}, 400),
                 (b'{}', {'Transfer-Encoding': 'chunked'}, 400),
                 (b'{}', {'Content-Length': '+2'}, 400),
                 (b' ' * 262145, {}, 413)]
        for route in ('changelist', 'shelf', 'commit', 'lock', 'add', 'file-op'):
            for body, changes, expected in cases:
                with self.subTest(route=route, changes=changes, size=len(body)):
                    handler = self.handler('/api/' + route + '-apply', changes=changes, body=body)
                    handler.do_POST()
                    self.assertEqual(handler.respond.call_args.args[0], expected)
                    self.assertEqual(handler.server.catalog.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
