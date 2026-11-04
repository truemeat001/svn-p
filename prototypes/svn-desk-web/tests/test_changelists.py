"""Exact local label workflow with real backend semantics and synthetic SVN I/O.

No production workspace, real SVN executable, repository or Shelf is touched.
"""
from dataclasses import replace
import hashlib
import http.client
import json
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from changelist_ops import (ChangelistService, ChangelistUnavailable, MutationUncertain,
                           PLAN_SECONDS, status_rows, validate_request, visible_rows,
                           FingerprintBudget, content_fingerprint)
from file_reads import StaleRead, file_identity
from server import PrototypeServer, ReadOnlyAdapter
from workspace_bridge import WorkspaceCatalog, trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


def native(value):
    """Synthetic status/argv identities must match the host SVN separator."""
    return str(Path(value))


def request(**changes):
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s',
                ids=[native('src/a.txt')], action='assign', name='Feature', **changes)


def make_file_symlink(case, link, target):
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            case.skipTest('Windows symlink fixture privilege unavailable (WinError 1314).')
        raise


class RequestTests(unittest.TestCase):
    def test_native_fixture_ids_use_windows_separators(self):
        with patch(__name__ + '.Path', PureWindowsPath):
            self.assertEqual(native('src/a.txt'), r'src\a.txt')
            self.assertEqual(native('src/b @.txt') + '@', 'src\\b @.txt@')

    def test_symlink_fixture_skips_only_windows_privilege_error(self):
        link = Mock(); denied = OSError('privilege'); denied.winerror = 1314
        link.symlink_to.side_effect = denied
        with self.assertRaises(unittest.SkipTest): make_file_symlink(self, link, Path('target'))
        for error in [PermissionError('denied'), OSError('I/O error')]:
            link.symlink_to.side_effect = error
            with self.assertRaises(OSError): make_file_symlink(self, link, Path('target'))

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


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / 'selected.txt'
        self.payload = b'bounded original contents'; self.path.write_bytes(self.payload)

    def fingerprint(self, budget=None):
        return content_fingerprint(self.root, Path('selected.txt'), budget or FingerprintBudget())

    def node(self, **changes):
        info = self.path.stat()
        fields = {key: getattr(info, key) for key in
                  ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_nlink','st_mode')}
        return SimpleNamespace(**{**fields, **changes})

    def test_hash_is_chunked_and_content_never_returned(self):
        read = os.read
        with patch('changelist_ops.HASH_CHUNK_BYTES', 3), patch('changelist_ops.os.read', wraps=read) as reads:
            identity, digest = self.fingerprint()
        self.assertEqual(identity, file_identity(self.root, Path('selected.txt')))
        self.assertEqual(digest, hashlib.sha256(self.payload).hexdigest())
        self.assertTrue(all(call.args[1] <= 3 for call in reads.call_args_list))

    def test_wrong_descriptor_is_rejected_before_read(self):
        for change in [{'st_ino':self.path.stat().st_ino+1}, {'st_nlink':2}, {'st_file_attributes':0x400}]:
            with self.subTest(change=change), patch('changelist_ops.os.fstat', return_value=self.node(**change)), patch('changelist_ops.os.read') as reads:
                with self.assertRaises(StaleRead): self.fingerprint()
                reads.assert_not_called()

    def test_windows_path_and_handle_ctime_may_differ(self):
        node = self.node(st_ctime_ns=self.path.stat().st_ctime_ns+12345)
        with patch('changelist_ops.os.fstat', return_value=node):
            self.assertEqual(self.fingerprint()[1], hashlib.sha256(self.payload).hexdigest())

    def test_handle_ctime_change_during_read_is_rejected(self):
        before = self.node(); after = self.node(st_ctime_ns=before.st_ctime_ns+1)
        with patch('changelist_ops.os.fstat', side_effect=[before, after]), self.assertRaises(StaleRead):
            self.fingerprint()

    def test_replaced_file_during_open_is_rejected_before_read(self):
        original = os.open
        def replace_then_open(path, flags):
            self.path.rename(self.root/'old.txt'); self.path.write_bytes(self.payload)
            return original(path, flags)
        with patch('changelist_ops.os.open', side_effect=replace_then_open), patch('changelist_ops.os.read') as reads:
            with self.assertRaises(StaleRead): self.fingerprint()
            reads.assert_not_called()

    def test_short_read_and_growth_fail_closed(self):
        for blocks in [[b''], [self.payload, b'x']]:
            with self.subTest(blocks=blocks), patch('changelist_ops.os.read', side_effect=blocks), self.assertRaises(StaleRead):
                self.fingerprint()

    def test_file_aggregate_and_elapsed_limits_fail_closed(self):
        with patch('changelist_ops.MAX_FILE_BYTES', 1), patch('changelist_ops.os.open') as opened:
            with self.assertRaises(ValueError): self.fingerprint()
            opened.assert_not_called()
        budget = FingerprintBudget(); budget.remaining = len(self.payload)-1
        with patch('changelist_ops.os.open') as opened:
            with self.assertRaises(ValueError): self.fingerprint(budget)
            opened.assert_not_called()
        with patch('changelist_ops.time.monotonic', side_effect=[0.0, 11.0, 11.0]), self.assertRaises(ValueError):
            self.fingerprint()

    def test_absent_is_distinct_from_empty_and_rechecked(self):
        self.path.write_bytes(b''); self.assertEqual(self.fingerprint()[1], hashlib.sha256(b'').hexdigest())
        self.path.unlink(); absent = self.fingerprint(); self.assertIsNone(absent[1])
        self.path.write_bytes(b''); present = file_identity(self.root, Path('selected.txt'))
        with patch('changelist_ops.file_identity', side_effect=[absent[0], present]), self.assertRaises(StaleRead):
            self.fingerprint()


@unittest.skipUnless((BACKEND / 'svn_client/history.py').is_file(), 'Trusted paired backend required.')
class ChangelistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'SYNTHETIC-changelists'; (self.root / '.svn').mkdir(parents=True)
        (self.root / 'src').mkdir()
        for name in [native('src/a.txt'), native('src/b @.txt'), 'unrelated.txt']:
            (self.root / name).write_text('unchanged file contents\n')
        self.backend, _, discovery, settings = trusted_modules(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.adapter = ReadOnlyAdapter(self.root, BACKEND, enable_changelists=True)
        self.client = self.adapter.client
        self.entries = [self.backend.StatusEntry(native('src/a.txt'), 'modified'),
                        self.backend.StatusEntry(native('src/b @.txt'), 'normal', changelist='Old'),
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
        self.data.update(ids=[native('src/a.txt')], action='assign', name='Feature')
        self.command_error = None; self.stderr = ''; self.mutate = True

    def info_dict(self, target=None):
        return {'wc_root': str(self.root), 'kind': 'dir' if target in {None, '.'} else 'file'}

    def info_xml(self, target):
        raw = target[:-1] if '@' in target else target
        tree = ET.Element('info'); entry = ET.SubElement(tree, 'entry', kind='dir' if target == '.' else 'file')
        ET.SubElement(entry, 'url').text = 'https://example.test/repo' + ('' if target == '.' else '/' + quote(raw.replace('\\', '/')))
        repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'uuid').text = 'fixture'
        wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(self.root)
        return ET.tostring(tree)

    def command(self, args, **kwargs):
        self.assertEqual(args[0], 'changelist')
        self.assertEqual(args[args.index('--depth') + 1], 'empty')
        tail = args[args.index('--') + 1:]
        removing = '--remove' in args[:args.index('--')]  # Never interpret literal label as an option.
        name, targets = (None, tail) if removing else (tail[0], tail[1:])
        ids = [native((x[:-1] if '@' in x else x).replace('\\', '/')) for x in targets]
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
        self.assertEqual([r['id'] for r in self.snapshot['rows']], [native('src/a.txt'), native('src/b @.txt')])
        self.assertTrue(all(c.kwargs == {'verbose': True} for c in self.client.status.call_args_list))

    def test_old_history_backend_cannot_enable_local_writes(self):
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'), patch('server.history_module', return_value=None):
            disabled = ReadOnlyAdapter(self.root, BACKEND, enable_changelists=True)
        self.assertIsNone(disabled._changelist_service)
        with self.assertRaises(ChangelistUnavailable): disabled.changelist_operation('preflight', self.data)

    def test_option_like_label_is_a_literal_and_old_read_capability_expires(self):
        plan = self.preflight(name='--remove')
        self.apply(plan)
        self.assertEqual(self.client.command.call_args.args[0], ['changelist','--depth','empty','--','--remove',native('src/a.txt')])
        with self.assertRaises(StaleRead): self.adapter.file_read('diff', {**self.data, 'id':native('src/a.txt')})

    def test_scheduled_deleted_file_is_metadata_verified_without_parent_expansion(self):
        (self.root/native('src/a.txt')).unlink()
        self.entries[0] = replace(self.entries[0], item='deleted')
        self.data['snapshotToken'] = self.catalog.snapshot()['snapshotToken']
        result = self.apply(self.preflight())
        self.assertEqual(result['changelistResult']['count'], 1)
        self.assertFalse((self.root/native('src/a.txt')).exists())
        self.assertEqual(self.client.command.call_args.args[0][-1], native('src/a.txt'))

    def test_preflight_is_read_only_and_binds_full_exact_review(self):
        plan = self.preflight(ids=[native('src/b @.txt'), native('src/a.txt')])
        self.assertEqual(plan['files'], [{'id': native('src/b @.txt'), 'path': 'src/b @.txt', 'fromChangelist': 'Old'},
                                        {'id': native('src/a.txt'), 'path': 'src/a.txt', 'fromChangelist': None}])
        self.assertEqual(plan['expiresIn'], PLAN_SECONDS); self.assertTrue(plan['localOnly'])
        self.client.command.assert_not_called()
        self.assertTrue(all(c.args[0][0] == 'info' for c in self.run.call_args_list))

    def test_assign_then_move_back_keeps_contents_and_never_expands_selection(self):
        before = {p: (self.root/p).read_bytes() for p in [native('src/a.txt'), native('src/b @.txt'), 'unrelated.txt']}
        result = self.apply(self.preflight(ids=[native('src/a.txt'), native('src/b @.txt')]))
        self.assertEqual(result['changelistResult'], {'action': 'assign', 'name': 'Feature', 'count': 2})
        self.assertNotEqual(result['snapshotToken'], self.data['snapshotToken'])
        self.assertEqual(self.client.command.call_args.args[0], ['changelist', '--depth', 'empty', '--', 'Feature', native('src/a.txt'), native('src/b @.txt') + '@'])
        self.data.update(snapshotToken=result['snapshotToken'], ids=[native('src/b @.txt')], action='remove', name=None)
        result = self.apply(self.preflight())
        self.assertEqual([r['id'] for r in result['rows']], [native('src/a.txt')])  # Clean default disappears.
        self.assertEqual(next(e for e in self.entries if e.path==native('src/a.txt')).changelist, 'Feature')
        self.assertEqual(self.client.command.call_args.args[0], ['changelist', '--remove', '--depth', 'empty', '--', native('src/b @.txt') + '@'])
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
        plan = self.preflight(); (self.root/native('src/a.txt')).write_text('new content')
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertIsNone(self.service.plan); self.client.command.assert_not_called()
        plan = self.preflight(); self.entries[0] = replace(self.entries[0], props='modified')
        with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_same_length_edit_with_restored_mtime_and_unchanged_path_identity_is_refused(self):
        path = self.root/native('src/a.txt'); before = path.stat()
        identity = file_identity(self.root, Path(native('src/a.txt')))
        with patch('changelist_ops.file_identity', return_value=identity):
            plan = self.preflight()
            path.write_bytes(b'x'*before.st_size); os.utime(path, ns=(before.st_atime_ns,before.st_mtime_ns))
            with self.assertRaises(StaleRead): self.apply(plan)
        self.assertIsNone(self.service.plan); self.client.command.assert_not_called()

    def test_same_length_edit_during_backend_validation_is_refused(self):
        path = self.root/native('src/a.txt'); before = path.stat()
        identity = file_identity(self.root, Path(native('src/a.txt'))); original = self.client.validate_changelist_paths
        def changed(paths):
            result = original(paths); path.write_bytes(b'x'*before.st_size)
            os.utime(path, ns=(before.st_atime_ns,before.st_mtime_ns)); return result
        with patch('changelist_ops.file_identity', return_value=identity):
            plan = self.preflight()
            with patch.object(self.client, 'validate_changelist_paths', side_effect=changed), self.assertRaises(StaleRead):
                self.apply(plan)
        self.client.command.assert_not_called()

    def test_same_length_edit_during_native_operation_is_uncertain(self):
        path = self.root/native('src/a.txt'); before = path.stat()
        identity = file_identity(self.root, Path(native('src/a.txt'))); original = self.command
        def changed(args, **kwargs):
            result = original(args, **kwargs); path.write_bytes(b'x'*before.st_size)
            os.utime(path, ns=(before.st_atime_ns,before.st_mtime_ns)); return result
        with patch('changelist_ops.file_identity', return_value=identity):
            plan = self.preflight(); self.client.command.side_effect = changed
            with self.assertRaises(MutationUncertain): self.apply(plan)
        self.assertIsNone(self.adapter._read_snapshot); self.assertEqual(self.client.command.call_count,1)

    def test_fingerprint_byte_limits_cannot_issue_native_write(self):
        plan = self.preflight()
        with patch('changelist_ops.MAX_FILE_BYTES',1), self.assertRaises(ValueError): self.apply(plan)
        self.client.command.assert_not_called(); self.assertIsNone(self.service.plan)

    def test_fixture_repository_urls_and_native_targets_use_forward_slashes(self):
        entry = ET.fromstring(self.info_xml(r'src\b @.txt@'))
        self.assertEqual(entry.findtext('entry/url'), 'https://example.test/repo/src/b%20%40.txt')
        self.command(['changelist','--depth','empty','--','Fixture',r'src\a.txt'])
        self.assertEqual(self.entries[0].changelist, 'Fixture')

    def test_unsafe_status_rows_rejected_and_no_partial_selection(self):
        for changes in [{'item':x} for x in ['unversioned','ignored','external','obstructed','incomplete','missing','conflicted']] + [
                {'props':'conflicted'}, {'switched':True}, {'file_external':True}, {'tree_conflicted':True}, {'wc_locked':True}]:
            self.entries[0] = replace(self.backend.StatusEntry(native('src/a.txt'), 'modified'), **changes)
            self.snapshot = self.catalog.snapshot(); self.data['snapshotToken'] = self.snapshot['snapshotToken']
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.preflight(ids=[native('src/b @.txt'),native('src/a.txt')])
        self.client.command.assert_not_called()

    def test_directory_hardlink_nested_wc_are_rejected(self):
        self.entries.append(self.backend.StatusEntry('src', 'modified'))
        self.snapshot = self.catalog.snapshot(); self.data['snapshotToken'] = self.snapshot['snapshotToken']
        with self.assertRaises(ValueError): self.preflight(ids=['src'])
        file = self.root/native('src/a.txt'); file.unlink(); os.link(self.root/'unrelated.txt', file)
        with self.assertRaises(ValueError): self.preflight()
        file.unlink(); file.write_text('new'); (self.root/'src/.svn').mkdir()
        with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_symlink_is_rejected(self):
        file = self.root/native('src/a.txt'); file.unlink()
        make_file_symlink(self, file, self.root/'unrelated.txt')
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
        from http_body_test_helpers import assert_oversized_body_rejected
        with patch.object(self.catalog, 'changelist_operation', wraps=self.catalog.changelist_operation) as dispatch:
            assert_oversized_body_rejected(self, server, '/api/changelist-preflight',
                body=' ' * 262145, limit=262144, assert_no_dispatch=dispatch.assert_not_called)
        self.client.command.assert_not_called()
        self.assertEqual(post('/api/changelist-preflight',{**self.data,'ids':[native('src/a.txt')]*101})[0],400)
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
