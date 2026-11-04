"""Commit safety against synthetic temporary files and mocked SVN only."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from commit_ops import CommitService, CommitUnavailable, validate_request, MAX_MESSAGE_BYTES
from changelist_ops import status_rows, MutationUncertain
from file_reads import StaleRead
from server import ReadOnlyAdapter
from workspace_bridge import trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))

def native(path):
    return str(Path(path))

def request(**changes):
    return {**dict(workspaceId='w', selectionVersion=1, snapshotToken='s', ids=[native('src/a.txt')],
                   operation='selected', name=None, message='Review this change\nDetails', keepChangelist=False), **changes}


class RequestTests(unittest.TestCase):
    def test_explicit_bounded_schema(self):
        validate_request('preflight', request())
        validate_request('preflight', request(operation='changelist', name='Feature'))
        for key, values in {
            'ids': [[], ['a', 'A'], ['a', 'a'], ['../x'], ['.svn/wc.db'], ['/x'], [True], ['x']*101],
            'operation': ['commit', None], 'name': ['Feature', ''],
            'message': ['', ' ', '\x00', '\ud800', 'x'*(MAX_MESSAGE_BYTES+1), '汉'*(MAX_MESSAGE_BYTES//2)],
            'keepChangelist': [None, 0, 1, 'true'], 'selectionVersion': [True, 0],
        }.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)[:80]), self.assertRaises(ValueError):
                    validate_request('preflight', request(**{key:value}))
        with self.assertRaises(ValueError): validate_request('preflight', {**request(), 'args': []})
        for name in [None, '', ' x', 'x\n', '\ud800']:
            with self.assertRaises(ValueError): validate_request('preflight', request(operation='changelist', name=name))

    def test_confirmation_literal_and_no_plan_changes(self):
        data = dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken='p', confirmed=True)
        validate_request('apply', data)
        for confirmed in [1, False, 'true', None]:
            with self.assertRaises(ValueError): validate_request('apply', {**data, 'confirmed': confirmed})
        for key in ['ids', 'name', 'message', 'operation', 'keepChangelist']:
            with self.assertRaises(ValueError): validate_request('apply', {**data, key: None})


@unittest.skipUnless((BACKEND / 'svn_client/history.py').is_file(), 'Paired backend unavailable.')
class CommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'synthetic'; (self.root / '.svn').mkdir(parents=True)
        (self.root/'src').mkdir()
        for name in ['src/a.txt', 'src/b @.txt', 'other.txt']:
            (self.root/name).write_text('original contents\n')
        self.backend, _, _, _ = trusted_modules(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.adapter = ReadOnlyAdapter(self.root, BACKEND)
        self.client = self.adapter.client
        self.entries = [self.backend.StatusEntry(native('src/a.txt'), 'modified', changelist='Feature'),
                        self.backend.StatusEntry(native('src/b @.txt'), 'normal', changelist='Feature'),
                        self.backend.StatusEntry('other.txt', 'modified')]
        self.client.status = Mock(side_effect=lambda **kw: list(self.entries))
        self.client.info = Mock(side_effect=lambda target=None: {'wc_root': str(self.root), 'kind': 'dir' if target in {None, '.'} else 'file'})
        self.manager = self.adapter._file_service.manager
        self.manager._run = Mock(side_effect=lambda args, limit: self.info_xml(args[-1]))
        self.service = CommitService(self.client, self.manager, lambda: None)
        self.props = {}; self.info_revision = '1'
        self.service._property_bytes = Mock(side_effect=self.properties_xml)
        self.client.command = Mock(return_value=self.backend.CommandResult((), 0, 'PRIVATE OUTPUT', ''))
        self.invalidated = Mock()
        self.snapshot = ('s', status_rows(self.client))

    def info_xml(self, target):
        raw = target[:-1] if '@' in target else target
        tree = ET.Element('info'); node = ET.SubElement(tree, 'entry', kind='dir' if target == '.' else 'file', revision=self.info_revision)
        ET.SubElement(node, 'url').text = 'https://example.test/repo' + ('' if target == '.' else '/' + quote(raw.replace('\\', '/')))
        repo = ET.SubElement(node, 'repository'); ET.SubElement(repo, 'uuid').text = 'fixture'
        wc = ET.SubElement(node, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(self.root)
        return ET.tostring(tree)

    def properties_xml(self, target):
        raw = target[:-1] if '@' in target else target
        tree = ET.Element('properties'); node = ET.SubElement(tree, 'target', path=raw)
        for name, value in self.props.items(): ET.SubElement(node, 'property', name=name).text = value
        return ET.tostring(tree)

    def refresh(self):
        self.snapshot = ('s', status_rows(self.client))

    def preflight(self, **changes):
        return self.service.preflight(request(**changes), self.snapshot)

    def apply_data(self, plan, **changes):
        return {**dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken=plan['planToken'], confirmed=True), **changes}

    def apply(self, plan, **changes):
        return self.service.apply(self.apply_data(plan, **changes), self.snapshot, self.invalidated)

    def test_preflight_local_and_message_private(self):
        plan = self.preflight()
        self.client.command.assert_not_called()
        self.assertNotIn('message', repr(plan)); self.assertNotIn('original contents', repr(plan))
        self.assertTrue(plan['localOnly']); self.assertEqual(plan['files'][0]['fromChangelist'], 'Feature')
        self.assertEqual(plan['expiresIn'], 120)

    def test_selected_exact_depth_keep_message_and_peg(self):
        plan = self.preflight(ids=[native('src/a.txt'), native('src/b @.txt')], keepChangelist=True)
        result = self.apply(plan)
        args = self.client.command.call_args.args[0]
        self.assertEqual(args, ['commit', '--message=Review this change\nDetails', '--encoding', 'UTF-8', '--depth', 'empty', '--keep-changelists', '--', native('src/a.txt'), native('src/b @.txt')+'@'])
        self.assertEqual(self.client.command.call_args.kwargs, {'_message_index':1})
        self.assertEqual(result['files'], ['src/a.txt', 'src/b @.txt'])
        self.assertTrue(result['commandCompleted']); self.assertNotIn('PRIVATE OUTPUT', repr(result))
        self.invalidated.assert_called_once()

    def test_named_requires_complete_clean_members(self):
        with self.assertRaises(StaleRead): self.preflight(operation='changelist', name='Feature')
        plan = self.preflight(operation='changelist', name='Feature', ids=[native('src/a.txt'), native('src/b @.txt')])
        self.apply(plan)
        args = self.client.command.call_args.args[0]
        self.assertEqual(args[:3], ['commit', '--changelist=Feature', '--message=Review this change\nDetails'])
        self.assertEqual(self.client.command.call_args.kwargs, {'_message_index':2})
        self.assertNotIn('other.txt', args)

    def test_clean_only_rejected(self):
        with self.assertRaises(ValueError): self.preflight(ids=[native('src/b @.txt')])
        self.client.command.assert_not_called()

    def test_unsafe_status_and_moves_rejected(self):
        for change in [{'item':v} for v in ['missing', 'unversioned', 'conflicted', 'ignored', 'external', 'obstructed', 'incomplete']] + [
            {'props':'conflicted'}, {'tree_conflicted':True}, {'switched':True}, {'file_external':True},
            {'wc_locked':True}, {'moved_to':'other.txt'}, {'moved_from':'other.txt'}]:
            with self.subTest(change=change):
                self.entries[0] = replace(self.backend.StatusEntry(native('src/a.txt'), 'modified'), **change)
                self.refresh()
                with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_directory_dependencies_and_switched_parent_rejected(self):
        for change in [{'item':v} for v in ['added', 'deleted', 'replaced', 'missing', 'conflicted']] + [{'switched':True}, {'props':'conflicted'}]:
            self.entries = self.entries[:3] + [replace(self.backend.StatusEntry('src', 'normal'), **change)]
            self.refresh()
            with self.subTest(change=change), self.assertRaises(ValueError): self.preflight()

    def test_deleted_file_allowed_but_normal_missing_not(self):
        (self.root/'src/a.txt').unlink()
        with self.assertRaises(ValueError): self.preflight()
        self.entries[0] = replace(self.entries[0], item='deleted'); self.refresh()
        self.apply(self.preflight())
        self.assertFalse((self.root/'src/a.txt').exists())

    def test_directory_hardlink_and_nested_working_copy_rejected(self):
        path = self.root/'src/a.txt'; path.unlink(); path.mkdir()
        with self.assertRaises(ValueError): self.preflight()
        path.rmdir(); os.link(self.root/'other.txt', path)
        with self.assertRaises(ValueError): self.preflight()
        path.unlink(); path.write_text('original'); (self.root/'src/.svn').mkdir()
        with self.assertRaises(ValueError): self.preflight()
        self.service._property_bytes.assert_not_called()

    def test_symlink_rejected(self):
        path = self.root/'src/a.txt'; path.unlink()
        try: path.symlink_to(self.root/'other.txt')
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314: self.skipTest('Symlink privilege unavailable.')
            raise
        with self.assertRaises(ValueError): self.preflight()
        self.service._property_bytes.assert_not_called()

    def test_same_size_mtime_restored_content_edit_rejected(self):
        plan = self.preflight(); path = self.root/'src/a.txt'; metadata = path.stat()
        path.write_bytes(b'x'*metadata.st_size); os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()
        with self.assertRaises(StaleRead): self.apply(plan)

    def test_same_status_properties_and_base_changes_rejected(self):
        self.entries[0] = replace(self.entries[0], props='modified'); self.refresh()
        self.props = {'custom':'first'}; plan = self.preflight(); self.props = {'custom':'other'}
        with self.assertRaises(StaleRead): self.apply(plan)
        plan = self.preflight(); self.info_revision = '2'
        with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_property_order_not_semantic_change(self):
        self.props = {'a':'1', 'b':'2'}; plan = self.preflight(); self.props = {'b':'2', 'a':'1'}
        self.apply(plan)

    def test_unsafe_property_xml_rejected(self):
        for raw in [b'<properties>unexpected text</properties>', b'<properties><target path="src/a.txt"><property name="x" encoding="base64">!</property></target></properties>', b'<!DOCTYPE properties><properties/>', b'<properties>\x00</properties>', b'<properties><target path="other.txt"/></properties>', b'<properties><target path="src/a.txt"><property name="svn:special">*</property></target></properties>', b'x'*(256*1024+1)]:
            self.service._property_bytes = Mock(return_value=raw)
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError): self.preflight()

    def test_membership_changed_after_review_rejected(self):
        plan = self.preflight(operation='changelist', name='Feature', ids=[native('src/a.txt'), native('src/b @.txt')])
        self.entries[2] = replace(self.entries[2], changelist='Feature')
        with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_plan_scope_replacement_expiry_and_one_use(self):
        first = self.preflight(); second = self.preflight()
        with self.assertRaises(StaleRead): self.apply(first)
        with self.assertRaises(StaleRead): self.apply(second)
        for changes in [{'workspaceId':'other'}, {'selectionVersion':2}, {'snapshotToken':'other'}]:
            plan = self.preflight()
            with self.assertRaises(StaleRead): self.apply(plan, **changes)
        plan = self.preflight(); self.service.plan['expires'] = 0
        with self.assertRaises(StaleRead): self.apply(plan)
        plan = self.preflight(); self.apply(plan)
        with self.assertRaises(StaleRead): self.apply(plan)

    def test_command_error_warning_timeout_uncertain_sanitized_and_consumed(self):
        for failure in [TimeoutError('PRIVATE MESSAGE'), RuntimeError('PRIVATE STDERR')]:
            self.client.command.reset_mock(); self.invalidated.reset_mock()
            self.client.command.side_effect = failure
            self.client.command.return_value = self.backend.CommandResult((), 0, 'SECRET', 'PRIVATE STDERR')
            plan = self.preflight()
            with self.assertRaises(MutationUncertain) as error: self.apply(plan)
            self.assertNotIn('PRIVATE', str(error.exception)); self.assertIsNone(error.exception.__cause__)
            self.client.command.assert_called_once(); self.invalidated.assert_called_once()
            with self.assertRaises(StaleRead): self.apply(plan)

    def test_zero_exit_with_stderr_preserves_completion_and_warns(self):
        self.client.command.return_value = self.backend.CommandResult((),0,'Committed revision 3.','PRIVATE STDERR')
        plan = self.preflight(); result = self.apply(plan)
        self.assertEqual(result['outcome'],'committed')
        self.assertEqual(result['reportedRevision'],'3')
        self.assertIn('stderr-output',result['warnings'])
        self.assertNotIn('PRIVATE',repr(result))
        self.assertFalse(result['automaticRetryAllowed'])
        with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_called_once()

    def test_expiry_during_revalidation_never_dispatches(self):
        plan = self.preflight()
        original = self.service._verify
        saved = self.service.plan
        def expire(*args):
            result = original(*args)
            saved['expires'] = 0
            return result
        with patch.object(self.service, '_verify', side_effect=expire):
            with self.assertRaises(StaleRead): self.apply(plan)
        self.client.command.assert_not_called()

    def test_metadata_race_during_preflight_rejected(self):
        original = self.service._metadata
        count = 0
        def changing(expected):
            nonlocal count
            count += 1
            if count == 2: self.props = {'custom':'new value'}
            return original(expected)
        with patch.object(self.service, '_metadata', side_effect=changing):
            with self.assertRaises(StaleRead): self.preflight()
        self.assertIsNone(self.service.plan)
        self.client.command.assert_not_called()

    def test_property_only_commit_supported(self):
        self.entries[0] = replace(self.entries[0], item='normal', props='modified')
        self.refresh(); self.apply(self.preflight())
        self.client.command.assert_called_once()

    def test_revalidation_detects_late_directory_dependency(self):
        plan = self.preflight()
        self.entries.append(self.backend.StatusEntry('src', 'added'))
        with self.assertRaises(ValueError): self.apply(plan)
        self.client.command.assert_not_called()

    def test_property_transport_fixed_local_bounded(self):
        del self.service._property_bytes
        with patch('svn_client.history._bounded_process', return_value=b'<properties/>') as run:
            self.service._property_bytes(native('src/a.txt'))
        argv = run.call_args.args[0]
        self.assertEqual(argv, [self.client.executable, 'proplist', '--non-interactive', '--no-auth-cache', '--xml', '--verbose', '--depth', 'empty', '--', native('src/a.txt')])
        self.assertEqual(run.call_args.kwargs['stdout_limit'], 256*1024)


if __name__ == '__main__':
    unittest.main()
