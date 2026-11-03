"""Exact local label workflow with real backend semantics and synthetic SVN I/O.

No production workspace, real SVN executable, repository or Shelf is touched.
"""
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from changelist_ops import (ChangelistService, ChangelistUnavailable, MutationUncertain,
                           PLAN_SECONDS, status_rows, validate_request, visible_rows)
from file_reads import StaleRead
from server import PrototypeServer, ReadOnlyAdapter
from workspace_bridge import WorkspaceCatalog, trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


def request(**changes):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s',
                ids=['src/a.txt'], action='assign', name='Feature', **changes)


class RequestTests(unittest.TestCase):
    def test_accepts_only_two_bounded_exact_operations(self):
        data = request(); validate_request('preflight', data)
        validate_request('preflight', {**data, 'action': 'remove', 'name': None})
        validate_request('apply', dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken='p', confirmed=True))

    def test_invalid_scope_selection_name_and_unknown_keys_rejected(self):
        data = request()
        for field, values in {
            'workspaceId': [None, '', 'w\n', 'w'*65],
            'selectionVersion': [True, 0, -1, 2**53, '1'],
            'snapshotToken': ['', 1],
            'ids': [[], ['a']*2, ['a']*101, 'a', [1], ['../a'], ['/etc/passwd'], ['.svn/wc.db'], ['C:a'], ['https://x/a'], ['x\x00']],
            'name': [None, '', ' ', ' Name', 'Name ', 'x'*129, 'x\n', 'x\x7f', 'x\x85', '\ud800'],
            'action': [None, 'commit', 'revert', 'shelf'],
        }.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)), self.assertRaises(ValueError):
                    validate_request('preflight', {**data, field: value})
        with self.assertRaises(ValueError): validate_request('preflight', {**data, 'path': '/outside'})
        with self.assertRaises(ValueError): validate_request('preflight', {**data, 'action': 'remove'})

    def test_literal_confirmation_required_and_apply_cannot_change_plan(self):
        data = dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken='p', confirmed=True)
        for value in [False, 1, 'true', None]:
            with self.subTest(value=value), self.assertRaises(ValueError): validate_request('apply', {**data, 'confirmed': value})
        for field in ['name', 'action', 'ids', 'path']:
            with self.assertRaises(ValueError): validate_request('apply', {**data, field: 'anything'})


@unittest.skipUnless((BACKEND / 'svn_client/history.py').is_file(), 'Trusted paired backend required.')
class ChangelistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'SYNTHETIC-changelists'; (self.root / '.svn').mkdir(parents=True)
        (self.root / 'src').mkdir()
        for name in ['src/a.txt', 'src/b @.txt', 'unrelated.txt']:
            (self.root / name).write_text('unchanged file contents\n')
        self.backend, _, discovery, settings = trusted_modules(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.adapter = ReadOnlyAdapter(self.root, BACKEND, enable_changelists=True)
        self.client = self.adapter.client
        self.entries = [self.backend.StatusEntry('src/a.txt', 'modified'),
                        self.backend.StatusEntry('src/b @.txt', 'normal', changelist='Old'),
                        self.backend.StatusEntry('unrelated.txt', 'normal')]
        self.client.status = Mock(side_effect=lambda **kw: list(self.entries))
        self.client.info = Mock(side_effect=self.info_dict)
        self.client.command = Mock(side_effect=self.command)
        self.run = Mock(side_effect=lambda args, limit: self.info_xml(args[-1]))
        self.adapter._file_service.manager._run = self.run
        self.service = self.adapter._changelist_service
        self.catalog = WorkspaceCatalog(roots=[self.root], discovery=discovery, settings=settings,
                                        adapter_factory=lambda path: self.adapter)
        self.snapshot = self.catalog.snapshot()
        self.data = {key: self.snapshot[key] for key in ('workspaceId', 'selectionVersion', 'snapshotToken')}
        self.data.update(ids=['src/a.txt'], action='assign', name='Feature')
        self.command_error = None; self.stderr = ''; self.mutate = True

    def info_dict(self, target=None):
        return {'wc_root': str(self.root), 'kind': 'dir' if target in {None, '.'} else 'file'}

    def info_xml(self, target):
        raw = target[:-1] if '@' in target else target
        tree = ET.Element('info'); entry = ET.SubElement(tree, 'entry', kind='dir' if target == '.' else 'file')
        ET.SubElement(entry, 'url').text = 'https://example.test/repo' + ('' if target == '.' else '/' + quote(Path(raw).as_posix()))
        repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'uuid').text = 'fixture'
        wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(self.root)
        return ET.tostring(tree)

    def command(self, args, **kwargs):
        self.assertEqual(args[0], 'changelist')
        self.assertEqual(args[args.index('--depth') + 1], 'empty')
        tail = args[args.index('--') + 1:]
        removing = '--remove' in args[:args.index('--')]  # Never interpret literal label as an option.
        name, targets = (None, tail) if removing else (tail[0], tail[1:])
        ids = [x[:-1] if '@' in x else x for x in targets]
        if self.mutate:
            self.entries = [replace(e, changelist=name) if e.path in ids else e for e in self.entries]
        if self.command_error: raise self.command_error
        return self.backend.CommandResult(tuple(args), 0, 'native local label output', self.stderr)

    def preflight(self, **changes):
        return self.catalog.changelist_operation('preflight', {**self.data, **changes})

    def apply_data(self, plan):
        return {**{key: self.data[key] for key in ('workspaceId', 'selectionVersion', 'snapshotToken')},
                'planToken': plan['planToken'], 'confirmed': True}

    def apply(self, plan):
        return self.catalog.changelist_operation('apply', self.apply_data(plan))

    def test_disabled_by_default_and_no_generic_write(self):
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            disabled = ReadOnlyAdapter(self.root, BACKEND)
        self.assertIsNone(disabled._changelist_service)
        with self.assertRaises(ChangelistUnavailable): disabled.changelist_operation('preflight', self.data)
        self.client.command.assert_not_called()

    def test_enabled_snapshot_keeps_clean_named_members_not_unrelated_clean_rows(self):
        self.assertTrue(self.snapshot['changelistWritesEnabled'])
        self.assertEqual([r['id'] for r in self.snapshot['rows']], ['src/a.txt', 'src/b @.txt'])
        self.assertTrue(all(c.kwargs == {'verbose': True} for c in self.client.status.call_args_list))

    def test_old_history_backend_cannot_enable_local_writes(self):
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'), patch('server.history_module', return_value=None):
            disabled = ReadOnlyAdapter(self.root, BACKEND, enable_changelists=True)
        self.assertIsNone(disabled._changelist_service)
        with self.assertRaises(ChangelistUnavailable): disabled.changelist_operation('preflight', self.data)

    def test_option_like_label_is_a_literal_and_old_read_capability_expires(self):
        plan = self.preflight(name='--remove')
        self.apply(plan)
        self.assertEqual(self.client.command.call_args.args[0], ['changelist','--depth','empty','--','--remove','src/a.txt'])
        with self.assertRaises(StaleRead): self.adapter.file_read('diff', {**self.data, 'id':'src/a.txt'})

    def test_scheduled_deleted_file_is_metadata_verified_without_parent_expansion(self):
        (self.root/'src/a.txt').unlink()
        self.entries[0] = replace(self.entries[0], item='deleted')
        self.data['snapshotToken'] = self.catalog.snapshot()['snapshotToken']
        result = self.apply(self.preflight())
        self.assertEqual(result['changelistResult']['count'], 1)
        self.assertFalse((self.root/'src/a.txt').exists())
        self.assertEqual(self.client.command.call_args.args[0][-1], 'src/a.txt')

    def test_preflight_is_read_only_and_binds_full_exact_review(self):
        plan = self.preflight(ids=['src/b @.txt', 'src/a.txt'])
        self.assertEqual(plan['files'], [{'id': 'src/b @.txt', 'path': 'src/b @.txt', 'fromChangelist': 'Old'},
                                        {'id': 'src/a.txt', 'path': 'src/a.txt', 'fromChangelist': None}])
        self.assertEqual(plan['expiresIn'], PLAN_SECONDS); self.assertTrue(plan['localOnly'])
        self.client.command.assert_not_called()
        self.assertTrue(all(c.args[0][0] == 'info' for c in self.run.call_args_list))

    def test_assign_then_move_back_keeps_contents_and_never_expands_selection(self):
        before = {p: (self.root/p).read_bytes() for p in ['src/a.txt', 'src/b @.txt', 'unrelated.txt']}
        result = self.apply(self.preflight(ids=['src/a.txt', 'src/b @.txt']))
        self.assertEqual(result['changelistResult'], {'action': 'assign', 'name': 'Feature', 'count': 2})
        self.assertNotEqual(result['snapshotToken'], self.data['snapshotToken'])
        self.assertEqual(self.client.command.call_args.args[0], ['changelist', '--depth', 'empty', '--', 'Feature', 'src/a.txt', 'src/b @.txt@'])
        self.data.update(snapshotToken=result['snapshotToken'], ids=['src/b @.txt'], action='remove', name=None)
        result = self.apply(self.preflight())
        self.assertEqual([r['id'] for r in result['rows']], ['src/a.txt'])  # Clean default disappears.
        self.assertEqual(next(e for e in self.entries if e.path=='src/a.txt').changelist, 'Feature')
        self.assertEqual(self.client.command.call_args.args[0], ['changelist', '--remove', '--depth', 'empty', '--', 'src/b @.txt@'])
        self.assertEqual(before, {p: (self.root/p).read_bytes() for p in before})

    def test_plan_one_use_refresh_expiry_replacement_and_wrong_scope(self):
        plan = self.preflight(); self.apply(plan)
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertEqual(self.client.command.call_count, 1)
        self.snapshot = self.catalog.snapshot(); self.data['snapshotToken'] = self.snapshot['snapshotToken']
        plan = self.preflight(); self.catalog.snapshot()
        with self.assertRaises(StaleRead): self.apply(plan)
        self.data['snapshotToken'] = self.adapter._read_snapshot[0]
        first = self.preflight(); second = self.preflight(name='Second')
        with self.assertRaises(StaleRead): self.apply(first)
        with self.assertRaises(StaleRead): self.apply(second)  # First bad attempt consumed pending plan.
        plan = self.preflight(); self.service.plan['expires'] = 0
        with self.assertRaises(StaleRead): self.apply(plan)
        plan = self.preflight()
        for field, value in [('workspaceId', 'wrong'), ('selectionVersion', 2)]:
            with self.assertRaises(StaleRead): self.catalog.changelist_operation('apply', {**self.apply_data(plan), field: value})
        self.assertEqual(self.client.command.call_count, 1)

    def test_unknown_or_stale_members_cannot_preflight(self):
        for changes in [{'ids':['unrelated.txt']}, {'snapshotToken':'old'}]:
            with self.assertRaises(StaleRead): self.preflight(**changes)
        self.entries[0] = replace(self.entries[0], changelist='Other process')
        with self.assertRaises(StaleRead): self.preflight()
        self.client.command.assert_not_called()

    def test_changed_content_or_status_after_review_consumes_plan_without_write(self):
        plan = self.preflight(); (self.root/'src/a.txt').write_text('new content')
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertIsNone(self.service.plan); self.client.command.assert_not_called()
        plan = self.preflight(); self.entries[0] = replace(self.entries[0], props='modified')
        with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_unsafe_status_rows_rejected_and_no_partial_selection(self):
        for changes in [{'item':x} for x in ['unversioned','ignored','external','obstructed','incomplete','missing','conflicted']] + [
                {'props':'conflicted'}, {'switched':True}, {'file_external':True}, {'tree_conflicted':True}, {'wc_locked':True}]:
            self.entries[0] = replace(self.backend.StatusEntry('src/a.txt', 'modified'), **changes)
            self.snapshot = self.catalog.snapshot(); self.data['snapshotToken'] = self.snapshot['snapshotToken']
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.preflight(ids=['src/b @.txt','src/a.txt'])
        self.client.command.assert_not_called()

    def test_directory_hardlink_symlink_nested_wc_are_rejected(self):
        self.entries.append(self.backend.StatusEntry('src', 'modified'))
        self.snapshot = self.catalog.snapshot(); self.data['snapshotToken'] = self.snapshot['snapshotToken']
        with self.assertRaises(ValueError): self.preflight(ids=['src'])
        file = self.root/'src/a.txt'; file.unlink(); os.link(self.root/'unrelated.txt', file)
        with self.assertRaises(ValueError): self.preflight()
        file.unlink(); file.symlink_to(self.root/'unrelated.txt')
        with self.assertRaises(ValueError): self.preflight()
        file.unlink(); file.write_text('new'); (self.root/'src/.svn').mkdir()
        with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_wrong_kind_wcroot_and_switched_parent_url_rejected(self):
        original = self.info_xml
        for kind in ['kind', 'root', 'url']:
            def bad(args, limit, kind=kind):
                tree = ET.fromstring(original(args[-1]))
                if args[-1] != '.':
                    if kind == 'kind': tree.find('entry').set('kind', 'dir')
                    elif kind == 'root': tree.find('entry/wc-info/wcroot-abspath').text = str(self.root/'nested')
                    else: tree.find('entry/url').text = 'https://example.test/repo/switched/a.txt'
                return ET.tostring(tree)
            self.run.side_effect = bad
            with self.subTest(kind=kind), self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_changed_label_during_backend_validation_is_rejected_before_write(self):
        plan = self.preflight()
        original = self.client.validate_changelist_paths
        def altered(paths):
            result = original(paths)
            self.entries[0] = replace(self.entries[0], changelist='Concurrent edit')
            return result
        with patch.object(self.client, 'validate_changelist_paths', side_effect=altered):
            with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_workspace_metadata_replaced_during_validation_is_rejected(self):
        plan = self.preflight()
        original = self.client.validate_changelist_paths
        def replaced(paths):
            result = original(paths)
            (self.root/'.svn').rename(self.root/'.old-svn')
            (self.root/'.svn').mkdir()
            return result
        with patch.object(self.client, 'validate_changelist_paths', side_effect=replaced):
            with self.assertRaises(ValueError): self.apply(plan)
        self.client.command.assert_not_called()

    def test_expiry_during_validation_is_rejected_before_write(self):
        plan = self.preflight()
        original = self.client.validate_changelist_paths
        clock = [1.0]
        self.service.plan['expires'] = 2.0
        def late(paths):
            result = original(paths); clock[0] = 3.0; return result
        with patch('changelist_ops.time.monotonic', side_effect=lambda: clock[0]), patch.object(self.client, 'validate_changelist_paths', side_effect=late):
            with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_backend_scope_mismatch_rejected(self):
        with patch.object(self.client, 'validate_changelist_paths', return_value=['unrelated.txt']):
            with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_zero_exit_diagnostics_partial_exception_and_postcheck_are_uncertain(self):
        for failure in ['stderr', 'exception', 'postcheck']:
            self.entries[0] = replace(self.entries[0], changelist=None)
            self.snapshot = self.catalog.snapshot(); self.data['snapshotToken'] = self.snapshot['snapshotToken']
            plan = self.preflight(); self.stderr = 'private raw warning' if failure=='stderr' else ''
            self.command_error = RuntimeError('PRIVATE raw diagnostic') if failure=='exception' else None
            self.mutate = failure != 'postcheck'
            with self.subTest(failure=failure), self.assertRaises(MutationUncertain) as raised: self.apply(plan)
            self.assertNotIn('PRIVATE', str(raised.exception)); self.assertIsNone(self.adapter._read_snapshot); self.assertIsNone(self.service.plan)
            calls = self.client.command.call_count
            with self.assertRaises(StaleRead): self.apply(plan)
            self.assertEqual(self.client.command.call_count, calls)

    def test_busy_catalog_and_adapter_do_not_start_mutation(self):
        plan = self.preflight()
        for lock in [self.catalog._busy, self.adapter._busy]:
            with lock, self.assertRaises(BlockingIOError): self.apply(plan)
        self.client.command.assert_not_called()

    def test_all_rows_limit_and_duplicate_identity_fail_closed(self):
        with patch('changelist_ops.MAX_ROWS', 2), self.assertRaises(ValueError): self.catalog.snapshot()
        self.assertIsNone(self.adapter._read_snapshot)
        self.entries.append(self.entries[0])
        with self.assertRaises(ValueError): self.catalog.snapshot()

    def test_http_contract_auth_confirmation_and_unknown_routes(self):
        server = PrototypeServer(0, catalog=self.catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        def post(path, data, **headers):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            conn.request('POST', path, body=data if isinstance(data,str) else json.dumps(data),
                         headers={'X-SVN-Desk-Token':server.token,'Content-Type':'application/json',**headers})
            response=conn.getresponse(); result=response.status,json.loads(response.read()); conn.close(); return result
        self.assertEqual(post('/api/changelist-preflight',self.data,Origin='https://evil.test')[0],403)
        self.assertEqual(post('/api/changelist-preflight',self.data,**{'X-SVN-Desk-Token':'bad'})[0],403)
        self.assertEqual(post('/api/changelist-preflight',json.dumps(self.data)[:-1]+',"name":"duplicate"}')[0],400)
        self.assertEqual(post('/api/changelist-preflight',{**self.data,'command':'commit'})[0],400)
        self.assertEqual(post('/api/changelist-preflight',' ' * 262145)[0],413)
        self.assertEqual(post('/api/changelist-preflight',{**self.data,'ids':['src/a.txt']*101})[0],400)
        status, plan=post('/api/changelist-preflight',self.data); self.assertEqual(status,200)
        self.assertEqual(post('/api/changelist-apply',{**self.apply_data(plan),'confirmed':1})[0],400)
        self.client.command.assert_not_called()
        status,result=post('/api/changelist-apply',self.apply_data(plan)); self.assertEqual(status,200)
        self.assertEqual(result['changelistResult']['count'],1)
        self.assertEqual(post('/api/changelist-apply',self.apply_data(plan))[0],409)
        for path in ['/api/commit','/api/revert','/api/shelf','/api/exec']:
            self.assertEqual(post(path,self.data)[0],405)
        self.assertEqual(self.client.command.call_count,1)


if __name__=='__main__': unittest.main()
