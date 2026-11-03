"""Guarded original Shelf algorithms with deterministic SVN XML, temp files only.

No real SVN executable, network repository, or user workspace is used.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', ROOT.parent / 'svn-release-review-9bbc406'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))
import shelf_ops as ops
from file_reads import StaleRead
from changelist_ops import MutationUncertain
from svn_client import shelves as original
spec = importlib.util.spec_from_file_location('original_shelf_test_fixture', BACKEND / 'tests/test_shelves.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def scope(**extra):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', **extra)


class ShelfRequestTests(unittest.TestCase):
    def test_three_explicit_schemas(self):
        ops.validate_request('list', scope())
        ops.validate_request('preflight', scope(operation='create', ids=['a'], name='Backup'))
        ops.validate_request('preflight', scope(operation='restore', shelfId='a'*32))
        ops.validate_request('apply', scope(planToken='p', confirmed=True))

    def test_invalid_scope_and_unbounded_selection(self):
        data = scope(operation='create', ids=['a'], name='Backup')
        for key, values in {
            'workspaceId': ['', None, 'a'*65], 'snapshotToken': ['', 3],
            'selectionVersion': [True, 0, -1, '1', 2**53],
            'ids': [[], ['a']*2, ['a']*101, ['.'], ['../x'], ['/etc/passwd'], ['.svn/wc.db'], ['C:x'], ['dir/../a'], ['x\\a']],
            'name': ['', ' ', ' x', 'x ', 'x\n', 'x'*129, '\ud800'],
            'operation': ['revert', 'delete', 'commit', None],
        }.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)), self.assertRaises(ValueError):
                    ops.validate_request('preflight', {**data, key: value})
        for extra in [{'path':'a'}, {'recursive':True}, {'url':'https://example.com'}]:
            with self.assertRaises(ValueError): ops.validate_request('preflight', {**data, **extra})

    def test_confirmation_is_literal_and_cannot_edit_plan(self):
        data = scope(planToken='p', confirmed=True)
        for value in [False, 1, 'true', None]:
            with self.assertRaises(ValueError): ops.validate_request('apply', {**data, 'confirmed':value})
        for key in ['ids', 'name', 'operation', 'shelfId']:
            with self.assertRaises(ValueError): ops.validate_request('apply', {**data, key:'x'})


class ShelfGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.wc = self.root/'wc'; self.wc.mkdir()
        self.store = self.root/'shelves'
        self.fake = fixture.XmlManager(self.wc, self.store)
        self.calls = []; self.invalidated = 0
        client = SimpleNamespace(working_copy=self.wc, executable='synthetic-svn', timeout=5, _lock=threading.RLock())
        def transport(argv, **kwargs):
            self.assertEqual(argv[:3], ['synthetic-svn', '--non-interactive', '--no-auth-cache'])
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['stdout_limit'], ops.MAX_FILE_BYTES)
            self.calls.append((argv, kwargs))
            return self.fake._svn(*argv[3:])
        self.history = SimpleNamespace(_bounded_process=transport)
        self.manager = ops.guarded_manager(original, self.history, client, lambda:None, self.store)
        self.service = ops.ShelfService(self.manager, lambda:None)
        self.service.status_client = client
        self.patch = patch.object(ops, 'status_rows', side_effect=lambda _: self.rows())
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.payload = b'changed\x00\xff\r\n' * 100
        self.fake.node('a.bin', 'modified', content=self.payload)
        self.snapshot = ('s', self.rows())

    def rows(self):
        return {r['path']:dict(r, id=r['path'], kind='unknown') for r in fixture.FakeManager._status(self.fake)}

    def invalidate(self): self.invalidated += 1

    def preview(self, **extra):
        return self.service.preflight(scope(**(extra or dict(operation='create', ids=['a.bin'], name='Backup'))), self.snapshot)

    def apply(self, plan):
        return self.service.apply(scope(planToken=plan['planToken'], confirmed=True), self.snapshot, self.invalidate)

    def create(self): return self.apply(self.preview())

    def manifest_path(self, shelf): return self.store/shelf['shelfId']/'manifest.json'

    def test_nested_file_with_clean_metadata_ancestor_round_trip(self):
        self.fake.node('sub', 'normal', kind='dir')
        self.fake.node('sub/nested.bin', 'modified', content=self.payload)
        self.snapshot = ('s', self.rows())
        shelf = self.apply(self.preview(operation='create', ids=['sub/nested.bin'], name='Nested'))
        self.assertEqual(shelf['files'], ['sub/nested.bin'])
        self.fake.clean()
        self.apply(self.preview(operation='restore', shelfId=shelf['shelfId']))
        self.assertEqual((self.wc/'sub/nested.bin').read_bytes(), self.payload)
        self.assertEqual((self.wc/'a.bin').read_bytes(), b'base')

    def test_oversized_property_cannot_publish_unreadable_manifest(self):
        self.fake.current_props['a.bin'] = {'large':fixture.enc(b'x' * 800000)}
        self.snapshot = ('s', self.rows())
        with self.assertRaises((ValueError, original.ShelfError)): self.preview()
        self.assertFalse(self.store.exists())
        with self.manager.bounded(), self.assertRaises((ValueError, original.ShelfError)):
            self.manager.create('Oversized', paths=['a.bin'])
        if self.store.exists():
            self.assertFalse(any(ops.SHELF_ID.fullmatch(p.name) for p in self.store.iterdir()))
        self.assertEqual((self.wc/'a.bin').read_bytes(), self.payload)

    def test_invalid_base_hash_type_isolated_and_list_byte_limit_bounded(self):
        shelf = self.create(); path = self.manifest_path(shelf)
        manifest = json.loads(path.read_text()); manifest['entries'][0]['base']['sha256'] = 5
        path.write_text(json.dumps(manifest))
        result = self.service.listing(scope(), self.snapshot)
        self.assertIn('error', result['shelves'][0])
        with patch.object(ops, 'MAX_LIST_BYTES', 4096), self.assertRaises(ValueError):
            self.service.listing(scope(), self.snapshot)

    def test_create_is_backup_only_and_preflight_creates_no_storage(self):
        before = (self.wc/'a.bin').read_bytes()
        plan = self.preview()
        self.assertFalse(self.store.exists()); self.assertTrue(plan['backupOnly'])
        result = self.apply(plan)
        self.assertEqual((self.wc/'a.bin').read_bytes(), before)
        self.assertEqual(self.fake.states['a.bin']['item'], 'modified')
        self.assertEqual(self.invalidated, 1)
        self.assertTrue(result['shelfRetained'])
        self.assertTrue(self.manifest_path(result).is_file())
        self.assertFalse(any(c[0][3] in {'add','delete','propset','propdel','revert','commit'} for c in self.calls))

    def test_v2_restore_clean_original_base_retains_shelf(self):
        shelf = self.create(); self.fake.clean()
        plan = self.preview(operation='restore', shelfId=shelf['shelfId'])
        self.assertFalse(plan['backupOnly'])
        self.apply(plan)
        self.assertEqual((self.wc/'a.bin').read_bytes(), self.payload)
        self.assertTrue(self.manifest_path(shelf).is_file())

    def test_v1_restore_without_rewriting_manifest(self):
        shelf = self.create(); path = self.manifest_path(shelf)
        manifest = json.loads(path.read_text()); manifest['format'] = 1
        for entry in manifest['entries']:
            (path.parent/'blobs'/entry['blob']).write_bytes(self.payload)
            del entry['blob_encoding'], entry['stored_size']
        path.write_text(json.dumps(manifest)); before = path.read_bytes()
        self.fake.clean(); self.apply(self.preview(operation='restore', shelfId=shelf['shelfId']))
        self.assertEqual((self.wc/'a.bin').read_bytes(), self.payload)
        self.assertEqual(path.read_bytes(), before)

    def test_replay_expiry_invalidation_and_changed_scope(self):
        plan = self.preview(); self.create()
        with self.assertRaises(StaleRead): self.apply(plan)
        plan = self.preview(); self.service.plan['expires'] = 0
        with self.assertRaises(StaleRead): self.apply(plan)
        plan = self.preview(); self.service.invalidate()
        with self.assertRaises(StaleRead): self.apply(plan)
        plan = self.preview()
        with self.assertRaises(StaleRead):
            self.service.apply({**scope(planToken=plan['planToken'], confirmed=True), 'workspaceId':'other'}, self.snapshot, self.invalidate)
        self.assertIsNone(self.service.plan)

    def test_snapshot_mismatch_rejected(self):
        with self.assertRaises(StaleRead): self.service.listing(scope(), ('new', self.rows()))
        with self.assertRaises(StaleRead): self.service.listing(scope(), None)

    def test_source_bytes_and_properties_stale_before_apply(self):
        for change in ['bytes','properties']:
            with self.subTest(change=change):
                plan = self.preview()
                if change == 'bytes': (self.wc/'a.bin').write_bytes(b'new content')
                else: self.fake.current_props['a.bin']['p'] = fixture.enc(b'new')
                with self.assertRaises((ValueError, original.ShelfError)): self.apply(plan)
                self.assertIsNone(self.service.plan)
                self.assertFalse(self.store.exists())

    def test_dirty_restore_collision_and_changed_base_rejected(self):
        shelf = self.create()
        with self.assertRaises(original.ShelfConflict): self.preview(operation='restore', shelfId=shelf['shelfId'])
        self.fake.clean(); self.fake.revision = '2'
        with self.assertRaises(original.ShelfConflict): self.preview(operation='restore', shelfId=shelf['shelfId'])
        self.assertEqual((self.wc/'a.bin').read_bytes(), b'base')

    def test_manifest_and_blob_tampering_rejected(self):
        shelf = self.create(); self.fake.clean(); plan = self.preview(operation='restore', shelfId=shelf['shelfId'])
        path = self.manifest_path(shelf); manifest = json.loads(path.read_text()); manifest['name'] = 'tampered'; path.write_text(json.dumps(manifest))
        with self.assertRaises(StaleRead): self.apply(plan)
        entry = manifest['entries'][0]; (path.parent/'blobs'/entry['blob']).write_bytes(b'corrupt')
        with self.assertRaises((ValueError, original.ShelfError)): self.preview(operation='restore', shelfId=shelf['shelfId'])
        self.assertEqual((self.wc/'a.bin').read_bytes(), b'base')

    def test_directory_scope_and_missing_snapshot_row_rejected(self):
        (self.wc/'dir').mkdir(); self.fake.states['dir'] = dict(item='unversioned', kind='dir'); self.snapshot = ('s', self.rows())
        with self.assertRaises(ValueError): self.preview(operation='create', ids=['dir'], name='No')
        with self.assertRaises(StaleRead): self.preview(operation='create', ids=['absent'], name='No')

    def test_symlink_hardlink_nested_wc_and_fifo_rejected(self):
        path = self.wc/'a.bin'; path.unlink()
        outside = self.root/'outside'; outside.write_bytes(b'outside')
        try: path.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314: self.skipTest('Windows symlink privilege unavailable')
            raise
        with self.assertRaises(ValueError): self.preview()
        path.unlink(); os.link(outside, path)
        with self.assertRaises(ValueError): self.preview()
        path.unlink(); path.write_bytes(self.payload)
        (self.wc/'sub').mkdir(); (self.wc/'sub'/'.svn').mkdir(); self.fake.node('sub/f','modified')
        self.snapshot = ('s', self.rows())
        with self.assertRaises(ValueError): self.preview(operation='create', ids=['sub/f'], name='No')
        if hasattr(os, 'mkfifo'):
            path.unlink(); os.mkfifo(path)
            with self.assertRaises(ValueError): self.preview()
        self.assertEqual(outside.read_bytes(), b'outside')

    def test_size_entry_count_and_case_collision_limits(self):
        with patch('changelist_ops.MAX_FILE_BYTES', 2), self.assertRaises(ValueError): self.preview()
        good = dict(path='a', kind='file', state='modified', size=1)
        for entries in [[], [good]*101, [good, {**good,'path':'A'}], [{**good,'size':ops.MAX_FILE_BYTES+1}], [{**good,'kind':'dir'}]]:
            with self.assertRaises(ValueError): ops._files(entries)
        with patch.object(ops, 'MAX_TOTAL_BYTES', 1), self.assertRaises(ValueError): ops._files([good, {**good,'path':'b'}])

    def test_deadline_and_transport_failure_are_bounded(self):
        with self.manager.bounded():
            self.manager.deadline = 0
            with self.assertRaises(ValueError): self.manager._svn('info','--','.@')
        with patch.object(self.history, '_bounded_process', side_effect=TimeoutError('synthetic')):
            with self.assertRaises(original.ShelfError): self.preview()
        self.assertFalse(self.store.exists())

    def test_added_unversioned_deleted_and_properties_round_trip(self):
        self.fake.node('added.bin', 'added', content=b'new addition')
        self.fake.node('loose.bin', 'unversioned', content=b'local only')
        self.fake.node('deleted.bin', 'deleted', base=b'deleted base')
        self.fake.current_props['a.bin'] = {'binary':fixture.enc(b'\x00\xff')}
        self.snapshot = ('s', self.rows())
        ids = ['a.bin', 'added.bin', 'deleted.bin', 'loose.bin']
        shelf = self.apply(self.preview(operation='create', ids=ids, name='Mixed'))
        self.fake.clean()
        self.apply(self.preview(operation='restore', shelfId=shelf['shelfId']))
        self.assertEqual((self.wc/'added.bin').read_bytes(), b'new addition')
        self.assertEqual((self.wc/'loose.bin').read_bytes(), b'local only')
        self.assertFalse((self.wc/'deleted.bin').exists())
        self.assertEqual(self.fake.states['added.bin']['item'], 'added')
        self.assertEqual(self.fake.current_props['a.bin']['binary'], fixture.enc(b'\x00\xff'))
        self.assertTrue(self.manifest_path(shelf).exists())

    def test_partial_restore_transport_failure_retains_backup(self):
        self.fake.current_props['a.bin'] = {'property':fixture.enc(b'changed')}
        self.snapshot = ('s', self.rows()); shelf = self.create(); self.fake.clean()
        plan = self.preview(operation='restore', shelfId=shelf['shelfId'])
        transport = self.history._bounded_process
        def fail_properties(argv, **kwargs):
            if argv[3] == 'propset': raise TimeoutError('synthetic uncertain property write')
            return transport(argv, **kwargs)
        with patch.object(self.history, '_bounded_process', side_effect=fail_properties):
            with self.assertRaises(MutationUncertain): self.apply(plan)
        self.assertEqual((self.wc/'a.bin').read_bytes(), self.payload)
        self.assertTrue(self.manifest_path(shelf).exists())
        self.assertIsNone(self.service.plan)

    def test_failed_execution_is_uncertain_consumed_and_invalidates(self):
        plan = self.preview()
        with patch.object(self.manager, 'create', side_effect=OSError('synthetic partial write')):
            with self.assertRaises(MutationUncertain): self.apply(plan)
        self.assertEqual(self.invalidated, 1); self.assertIsNone(self.service.plan)
        with self.assertRaises(StaleRead): self.apply(plan)

    def test_restore_target_mode_and_inode_change_invalidates_plan(self):
        shelf = self.create(); self.fake.clean()
        path = self.wc/'a.bin'
        for change in ['mode', 'inode']:
            with self.subTest(change=change):
                plan = self.preview(operation='restore', shelfId=shelf['shelfId'])
                if change == 'mode': path.chmod(path.stat().st_mode ^ 0o100)
                else:
                    alternate = self.wc/'replacement'; alternate.write_bytes(path.read_bytes()); os.replace(alternate, path)
                with self.assertRaises((ValueError, original.ShelfError)): self.apply(plan)
                self.assertEqual(path.read_bytes(), b'base')

    def test_cross_device_leaf_rejected_before_transport(self):
        real = os.lstat
        def changed(path, *args, **kwargs):
            value = real(path, *args, **kwargs)
            if Path(path) == self.wc/'a.bin':
                fields = {name:getattr(value, name) for name in dir(value) if name.startswith('st_')}
                fields['st_dev'] += 1
                return SimpleNamespace(**fields)
            return value
        with patch('file_reads.os.lstat', side_effect=changed), self.assertRaises(ValueError): self.preview()
        self.assertFalse(self.calls)

    def test_mutating_transport_requires_executing_review(self):
        for command in ['add','delete','propset','propdel','revert','commit','changelist']:
            with self.assertRaises(ValueError): self.manager._svn(command,'--','a.bin@')

    def test_list_returns_display_metadata_not_payload_or_local_paths(self):
        shelf = self.create(); result = self.service.listing(scope(), self.snapshot)
        self.assertEqual(result['shelves'][0]['id'], shelf['shelfId'])
        self.assertEqual(result['shelves'][0]['format'], 2)
        self.assertNotIn(str(self.wc), json.dumps(result))
        self.assertNotIn('base64', json.dumps(result))

    def test_duplicate_manifest_fields_and_hardlinked_manifest_rejected(self):
        shelf = self.create(); path = self.manifest_path(shelf); raw = path.read_text()
        path.write_text('{"format":2,' + raw[1:])
        with self.assertRaises(ValueError): self.manager._load(shelf['shelfId'])
        path.write_text(raw); os.link(path, self.root/'manifest-link')
        with self.assertRaises(ValueError): self.manager._load(shelf['shelfId'])

    def test_malformed_manifest_listing_isolated_as_unavailable(self):
        shelf = self.create(); path = self.manifest_path(shelf)
        for malformed in [[], {'entries':[{}]}, {'entries':[None]}, {'entries':'bad'}]:
            with self.subTest(malformed=malformed):
                path.write_text(json.dumps(malformed))
                result = self.service.listing(scope(), self.snapshot)
                self.assertIn('error', result['shelves'][0])


class ShelfHTTPTests(unittest.TestCase):
    def setUp(self):
        from server import ReadOnlyAdapter, PrototypeServer
        from workspace_bridge import WorkspaceCatalog, trusted_modules
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.wc = self.root/'wc'; (self.wc/'.svn').mkdir(parents=True)
        (self.wc/'a.bin').write_bytes(b'changed')
        backend, _, discovery, settings = trusted_modules(BACKEND)
        self.backend = backend
        with patch.object(backend, 'discover_svn', return_value='synthetic-svn'):
            self.adapter = ReadOnlyAdapter(self.wc, BACKEND, enable_shelves=True, shelf_storage=self.root/'shelves')
        self.fake = fixture.XmlManager(self.wc, self.root/'shelves')
        self.fake.node('a.bin', 'modified', content=b'changed')
        self.adapter.client.status = lambda **kw: [backend.StatusEntry('a.bin', self.fake.states['a.bin']['item'])]
        self.adapter.client.info = lambda *a, **kw: dict(wc_root=str(self.wc), kind='dir')
        import svn_client.history as history
        self.patch = patch.object(history, '_bounded_process', side_effect=lambda argv, **kw: self.fake._svn(*argv[3:]))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.catalog = WorkspaceCatalog(roots=[self.wc], discovery=discovery, settings=settings, adapter_factory=lambda path:self.adapter)
        self.snapshot = self.catalog.snapshot()
        self.data = {key:self.snapshot[key] for key in ops.COMMON}
        self.server = PrototypeServer(0, catalog=self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)

    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join()

    def post(self, path, data, origin=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1])
        try:
            conn.request('POST', path, json.dumps(data), {'Content-Type':'application/json','Origin':origin or self.server.origin,'X-SVN-Desk-Token':self.server.token})
            response = conn.getresponse(); return response.status, json.loads(response.read())
        finally: conn.close()

    def test_enabled_http_create_refresh_replay_and_default_disabled(self):
        from server import ReadOnlyAdapter
        self.assertTrue(self.snapshot['shelfWritesEnabled'])
        status, plan = self.post('/api/shelf-preflight', dict(self.data, operation='create', ids=['a.bin'], name='Backup'))
        self.assertEqual(status, 200, plan)
        apply = dict(self.data, planToken=plan['planToken'], confirmed=True)
        status, result = self.post('/api/shelf-apply', apply)
        self.assertEqual(status, 200, result); self.assertIn('shelfResult', result)
        self.assertNotEqual(result['snapshotToken'], self.data['snapshotToken'])
        self.assertEqual((self.wc/'a.bin').read_bytes(), b'changed')
        self.assertEqual(self.post('/api/shelf-apply', apply)[0], 409)
        with patch.object(self.backend, 'discover_svn', return_value='synthetic-svn'):
            disabled = ReadOnlyAdapter(self.wc, BACKEND)
        self.assertIsNone(disabled._shelf_service)
        with self.assertRaises(ops.ShelfUnavailable): disabled.shelf_operation('list', self.data)

    def test_http_origin_scope_and_unsupported_operations_rejected(self):
        self.assertEqual(self.post('/api/shelves-list', self.data, 'https://evil.test')[0], 403)
        self.assertEqual(self.post('/api/shelves-list', dict(self.data, workspaceId='other'))[0], 409)
        self.assertEqual(self.post('/api/shelf-preflight', dict(self.data, operation='delete', shelfId='a'*32))[0], 400)
        self.assertEqual(self.post('/api/shelf-apply', dict(self.data, planToken='a', confirmed=False))[0], 400)

    def test_unsupported_backend_cannot_enable_shelves(self):
        from server import ReadOnlyAdapter
        with patch.object(self.backend, 'discover_svn', return_value='synthetic-svn'), patch('server.shelf_module', return_value=None):
            disabled = ReadOnlyAdapter(self.wc, BACKEND, enable_shelves=True)
        self.assertIsNone(disabled._shelf_service)


if __name__ == '__main__': unittest.main()
