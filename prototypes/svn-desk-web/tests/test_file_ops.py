"""Synthetic files and mocked bounded transport; never a real SVN working copy."""
from dataclasses import replace
import importlib
import ntpath
import os
from pathlib import Path
import posixpath
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_ops
from file_ops import FileOperationService, FileOperationUnavailable, guarded_manager, validate_request
from changelist_ops import MutationUncertain, status_rows
from file_reads import StaleRead
from workspace_bridge import trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


def native(path):
    return str(Path(path))


def request():
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', action='move',
                id=native('src/a @.txt'), destination='dest/new @.txt')


class RequestTests(unittest.TestCase):
    def test_exact_schema(self):
        validate_request('preflight', request())
        for field in ('force', 'parents', 'recursive', 'ids', 'url', 'args'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_request('preflight', {**request(), field: True})
        for field, values in {'action':['delete', '', None, True, []],
                              'id':['', '.', '../a', '.svn/a', '/a', 'C:a', 'a.', '\ud800', False, []],
                              'destination':['', '.', 'src/../x', '/a', '.svn/a', 'a ', '\ud800', False, []],
                              'selectionVersion':[0, True]}.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)), self.assertRaises(ValueError):
                    validate_request('preflight', {**request(), field: value})
        for destination in (request()['id'], request()['id'].upper()):
            with self.assertRaises(ValueError): validate_request('preflight', {**request(), 'destination':destination})

    def test_explicit_apply_no_scope_edits(self):
        data = dict(workspaceId='w', selectionVersion=1, snapshotToken='s', planToken='p', confirmed=True)
        validate_request('apply', data)
        for value in (False, 1, 'true', None):
            with self.assertRaises(ValueError): validate_request('apply', {**data, 'confirmed':value})
        for field in ('id', 'destination', 'action', 'force'):
            with self.assertRaises(ValueError): validate_request('apply', {**data, field:None})
        for token in ('', '\x00', '\ud800', 'x' * 65):
            with self.assertRaises(ValueError): validate_request('apply', {**data, 'planToken':token})
        with self.assertRaises(FileOperationUnavailable): FileOperationService(object(), lambda:None)


class MetadataPathTests(unittest.TestCase):
    def test_windows_wc_root_only_normalizes_separators(self):
        workspace = r'C:\fixture\authorized\工作区甲'
        for actual in (workspace, 'C:/fixture/authorized/工作区甲', r'C:/fixture\authorized/工作区甲'):
            with self.subTest(actual=actual):
                self.assertTrue(file_ops._svn_path_matches(actual, workspace, pathmod=ntpath))
        for actual in (None, '', '.', 'C:fixture/authorized/工作区甲',
                       r'\fixture\authorized\工作区甲', r'D:\fixture\authorized\工作区甲',
                       workspace + r'\nested', workspace + r'\..\工作区甲',
                       r'C:\fixture\authorized\other\..\工作区甲',
                       workspace.upper(), workspace + '.', workspace + ' ',
                       '\\\\?\\' + workspace, r'\\server\fixture\authorized\工作区甲'):
            with self.subTest(actual=actual):
                self.assertFalse(file_ops._svn_path_matches(actual, workspace, pathmod=ntpath))

    def test_posix_wc_root_keeps_literal_separators_and_case(self):
        workspace = '/fixture/authorized/工作区甲'
        self.assertTrue(file_ops._svn_path_matches(workspace, workspace, pathmod=posixpath))
        for actual in (workspace.replace('/', '\\'), workspace.upper(), workspace + '/.',
                       workspace + '/../工作区甲', workspace + '/', workspace + '/nested'):
            with self.subTest(actual=actual):
                self.assertFalse(file_ops._svn_path_matches(actual, workspace, pathmod=posixpath))

    def test_native_windows_exact_status_move_examples_both_directions(self):
        # Captured from the real Windows SVN diagnostic: the base is the exact
        # status target INCLUDING its filename, not that file's parent.
        examples = [
            (r'src\other.txt', r'src\renamed.txt', r'..\renamed.txt', r'..\other.txt'),
            (r'src\clean.txt', 'moved-clean.txt', r'..\..\moved-clean.txt', r'..\src\clean.txt'),
            (r'src\中文 @.txt', r'dest\复制 @.txt', r'..\..\dest\复制 @.txt', r'..\..\src\中文 @.txt'),
        ]
        for source, destination, moved_to, moved_from in examples:
            for target, counterpart, actual in ((source, destination, moved_to), (destination, source, moved_from)):
                for spelling in (actual, actual.replace('\\', '/')):
                    with self.subTest(target=target, actual=spelling):
                        self.assertTrue(file_ops._status_move_matches(spelling, target, counterpart, pathmod=ntpath))
                        self.assertTrue(file_ops._status_move_matches(spelling.replace('\\', '/'),
                            target.replace('\\', '/'), counterpart.replace('\\', '/'), pathmod=posixpath))

    def test_move_metadata_cannot_match_wrong_base_or_ambiguous_paths(self):
        for pathmod in (ntpath, posixpath):
            source, destination = 'src/other.txt', 'dest/renamed.txt'
            good = '../../dest/renamed.txt'
            bad = [None, '', destination, '../dest/renamed.txt', '../renamed.txt',
                   '../../dest/other.txt', '../../DEST/renamed.txt',
                   '../../dest/renamed.txt.', '../../dest/renamed.txt ',
                   '../../dest/junction/../renamed.txt', '../../dest/.svn/../renamed.txt',
                   '../../../other-wc/dest/renamed.txt', '../../../wc/dest/renamed.txt',
                   '../../dest/./renamed.txt', '../../dest//renamed.txt',
                   '/wc/dest/renamed.txt', 'C:/wc/dest/renamed.txt',
                   'C:dest/renamed.txt', '//host/share/dest/renamed.txt',
                   'file:///wc/dest/renamed.txt', good + '\x00']
            for actual in bad:
                with self.subTest(platform=pathmod.__name__, actual=actual):
                    self.assertFalse(file_ops._status_move_matches(actual, source, destination, pathmod=pathmod))


@unittest.skipUnless((BACKEND / 'svn_client/fileops.py').is_file(), 'Trusted paired backend required.')
class FileOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'SYNTHETIC-file-operations'
        (self.root / '.svn').mkdir(parents=True)
        (self.root / 'src').mkdir(); (self.root / 'dest').mkdir()
        self.source = native('src/a @.txt'); self.destination = native('dest/new @.txt')
        (self.root / self.source).write_bytes(b'original local bytes\n')
        (self.root / 'other.txt').write_text('unselected')
        self.backend, *_ = trusted_modules(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.client = self.backend.SvnClient(self.root)
        self.module = file_ops.fileops_module(BACKEND)
        self.assertIsNotNone(self.module)
        self.history = importlib.import_module('svn_client.history')
        self.manager = guarded_manager(self.module, self.history, self.client)
        self.check = Mock(); self.invalidate = Mock()
        self.service = FileOperationService(self.manager, self.check)
        self.entries = [self.backend.StatusEntry(p, 'normal', revision=7) for p in ('.', 'src', 'dest', self.source, 'other.txt')]
        self.client.status = Mock(side_effect=lambda **kw:list(self.entries))
        self.data = request(); self.snapshot = ('s', status_rows(self.client))
        self.props = {'review:note':'test', 'svn:eol-style':'LF'}
        self.info_changes = {}; self.status_changes = {}; self.raw_hook = lambda cmd, path, raw: raw
        self.commands = []; self.reads = []; self.mode = 'normal'; self.after_command = lambda: None
        self.after_read = lambda cmd, path: None
        transport = patch.object(self.history, '_bounded_process', side_effect=self.transport)
        transport.start(); self.addCleanup(transport.stop)

    def url(self, path):
        return 'https://example.test/repo' + ('' if path == '.' else '/' + quote(Path(path).as_posix(), safe='/'))

    def info(self, path):
        status = next(e for e in self.entries if e.path == path)
        root = ET.Element('info'); entry = ET.SubElement(root, 'entry', path=path, kind='dir' if path in {'.','src','dest'} else 'file', revision='7')
        ET.SubElement(entry, 'url').text = self.url(path)
        repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'root').text = 'https://example.test/repo'; ET.SubElement(repo, 'uuid').text = 'fixture'
        # SVN info uses forward slashes even on native Windows.
        wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = self.root.as_posix()
        ET.SubElement(wc, 'schedule').text = 'add' if status.item == 'added' else 'normal'
        if status.copied:
            ET.SubElement(wc, 'copy-from-url').text = self.url(self.source)
            ET.SubElement(wc, 'copy-from-rev').text = '7'
        for key, value in self.info_changes.get(path, {}).items():
            if key.startswith('@'):
                entry.set(key[1:], value)
            else:
                node = entry.find(key)
                if node is None:
                    parent, _, tag = key.rpartition('/')
                    node = ET.SubElement(entry.find(parent) if parent else entry, tag or key)
                node.text = value
        return ET.tostring(root)

    def status(self, path):
        root = ET.Element('status'); target = ET.SubElement(root, 'target', path=path)
        value = next((e for e in self.entries if e.path == path), None)
        if value:
            entry = ET.SubElement(target, 'entry', path=path)
            attrs = dict(item=value.item, props=value.props)
            if value.revision is not None: attrs['revision'] = str(value.revision)
            for name in ('tree_conflicted', 'copied', 'switched', 'wc_locked', 'file_external', 'moved_from', 'moved_to'):
                val = getattr(value, name)
                if val and name in {'moved_from', 'moved_to'}:
                    # Full-WC status uses root-relative counterparts; exact
                    # status XML instead uses the file target itself as base.
                    val = os.path.relpath(val, start=path)
                if val: attrs[name.replace('_','-')] = 'true' if val is True else val
            wc = ET.SubElement(entry, 'wc-status', attrs)
            if self.status_changes.get(path) == 'lock': ET.SubElement(wc,'lock')
        return ET.tostring(root)

    def properties(self, path):
        root = ET.Element('properties'); target = ET.SubElement(root, 'target', path=path)
        values = dict(self.props)
        if self.mode == 'bad-destination-properties' and path == self.destination:
            values['review:note'] = 'different'
        for key, value in values.items(): ET.SubElement(target, 'property', name=key).text = value
        return ET.tostring(root)

    def transport(self, argv, **kwargs):
        self.assertEqual(kwargs['cwd'], self.root)
        self.assertGreater(kwargs['timeout'], 0); self.assertLessEqual(kwargs['timeout'], 90)
        self.assertLessEqual(kwargs['stdout_limit'], file_ops.XML_BYTES)
        cmd = argv[1]
        self.assertEqual(argv[2:4], ['--non-interactive','--no-auth-cache'])
        if cmd in {'move','copy'}:
            self.assertIsNone(self.service.plan)
            self.invalidate.assert_called_once()
            self.commands.append(argv)
            if self.mode == 'raise-before-mutate': raise TimeoutError('private diagnostic')
            operands = argv[argv.index('--')+1:]
            self.assertEqual(len(operands), 2)
            source = operands[0][:-1] if '@' in operands[0] else operands[0]
            destination = operands[1][:-1] if cmd == 'copy' and '@' in operands[1] else operands[1]
            self.assertEqual(source, self.source); self.assertEqual(destination, self.destination)
            if self.mode != 'no-mutation':
                if cmd == 'move':
                    (self.root/source).rename(self.root/destination)
                    self.entries = [replace(e,item='deleted',moved_to=destination) if e.path == source else e for e in self.entries]
                else:
                    shutil.copyfile(self.root/source,self.root/destination)
                self.entries.append(self.backend.StatusEntry(destination,'added',copied=True,moved_from=source if cmd=='move' else None))
            if self.mode == 'bad-destination-bytes': (self.root/destination).write_text('different')
            if self.mode == 'unselected-change': self.entries[-2] = replace(self.entries[-2],item='modified')
            self.after_command()
            if self.mode == 'raise-after-mutate': raise TimeoutError('private diagnostic')
            if self.mode == 'oversized-output': return b'x'*(file_ops.OUTPUT_BYTES+1)
            return b''
        self.assertIn(cmd, {'info','status','proplist'})
        self.assertIn('--depth', argv); self.assertEqual(argv[argv.index('--depth')+1],'empty')
        target = argv[-1]; path = target[:-1] if '@' in target else target
        self.reads.append((cmd,path))
        raw = {'info':self.info,'status':self.status,'proplist':self.properties}[cmd](path)
        raw = self.raw_hook(cmd,path,raw)
        self.after_read(cmd,path)
        return raw

    def preflight(self, **changes):
        return self.service.preflight({**self.data, **changes}, self.snapshot)

    def apply(self, plan, **changes):
        scope = {key:self.data[key] for key in file_ops.COMMON}
        return self.service.apply(dict(scope, planToken=plan['planToken'], confirmed=True, **changes),self.snapshot,self.invalidate)

    def test_move_exact_peg_destination_and_one_use(self):
        plan = self.preflight()
        self.assertFalse(self.commands); self.assertEqual(plan['expiresIn'],120)
        self.assertTrue(plan['localOnly']); self.assertTrue(plan['commitRequired'])
        self.assertEqual(plan['source']['id'],self.source)
        result, rows = self.apply(plan)
        self.assertEqual(result['outcome'],'verified'); self.assertTrue(result['verified'])
        self.assertEqual(result['warnings'],[]); self.assertEqual(rows[self.destination]['item'],'added')
        self.assertEqual(self.commands[0][-3:],['--',self.source+'@',self.destination])
        self.assertTrue((self.root/self.destination).is_file()); self.assertFalse((self.root/self.source).exists())
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertEqual(len(self.commands),1)

    def test_move_same_directory_uses_target_relative_metadata(self):
        self.destination = native('src/renamed.txt'); self.data['destination'] = self.destination
        result, _ = self.apply(self.preflight())
        self.assertTrue(result['verified'])
        self.service._start(); self.addCleanup(self.service._finish)
        self.assertEqual(self.manager._status(self.source).moved_to, native('../renamed.txt'))
        self.assertEqual(self.manager._status(self.destination).moved_from, native('../a @.txt'))

    def test_move_to_root_uses_target_relative_metadata(self):
        self.destination = 'moved-clean.txt'; self.data['destination'] = self.destination
        result, _ = self.apply(self.preflight())
        self.assertTrue(result['verified'])
        self.service._start(); self.addCleanup(self.service._finish)
        self.assertEqual(self.manager._status(self.source).moved_to, native('../../moved-clean.txt'))
        self.assertEqual(self.manager._status(self.destination).moved_from, native('../src/a @.txt'))

    def test_post_move_rejects_wrong_counterparts_and_noncanonical_traversals(self):
        plan = self.preflight(); native_plan = self.service.plan['native']
        self.apply(plan)
        self.service._start(); self.addCleanup(self.service._finish)
        for target, attribute, counterpart in ((self.source, 'moved-to', self.destination),
                                               (self.destination, 'moved-from', self.source)):
            expected = os.path.relpath(counterpart, start=target)
            bad_values = [counterpart, str(self.root / counterpart), expected.upper(),
                          os.path.relpath('other.txt', start=target),
                          native('../../other-wc/') + '/' + counterpart,
                          expected.replace('..', '.', 1),
                          expected.rsplit(os.sep, 1)[0] + os.sep + 'junction' + os.sep + '..' + os.sep + Path(counterpart).name,
                          expected.rsplit(os.sep, 1)[0] + os.sep + '.svn' + os.sep + '..' + os.sep + Path(counterpart).name]
            for value in bad_values:
                def corrupt(cmd, path, raw):
                    root = ET.fromstring(raw)
                    if cmd == 'status' and path == target:
                        root.find('.//wc-status').set(attribute, value)
                    return ET.tostring(root)
                self.raw_hook = corrupt
                with self.subTest(attribute=attribute, value=value), self.assertRaises(ValueError):
                    self.manager._verify_result(native_plan)
        self.assertEqual(len(self.commands), 1)

    def assert_bad_move_metadata_is_uncertain(self, target, attribute, value):
        def corrupt(cmd, path, raw):
            if cmd == 'status' and path == target and self.commands:
                root = ET.fromstring(raw); root.find('.//wc-status').set(attribute, value)
                return ET.tostring(root)
            return raw
        self.raw_hook = corrupt
        plan = self.preflight()
        with self.assertRaises(MutationUncertain): self.apply(plan)
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertEqual(len(self.commands), 1)
        self.assertFalse((self.root / self.source).exists())
        self.assertTrue((self.root / self.destination).is_file())

    def test_wrong_moved_to_remains_uncertain_and_single_attempt(self):
        self.assert_bad_move_metadata_is_uncertain(self.source, 'moved-to', self.destination)

    def test_wrong_moved_from_remains_uncertain_and_single_attempt(self):
        self.assert_bad_move_metadata_is_uncertain(self.destination, 'moved-from', self.source)

    def test_copy_preserves_source_and_escapes_destination_peg(self):
        plan = self.preflight(action='copy')
        result, rows = self.apply(plan)
        self.assertEqual(result['action'],'copy'); self.assertTrue(result['verified'])
        self.assertEqual(self.commands[0][-3:],['--',self.source+'@',self.destination+'@'])
        self.assertEqual(rows[self.source]['item'],'normal')
        self.assertEqual((self.root/self.source).read_bytes(),(self.root/self.destination).read_bytes())

    def test_modified_source_and_properties_allowed(self):
        self.entries[3] = replace(self.entries[3],item='modified',props='modified')
        self.snapshot = ('s',status_rows(self.client))
        result,_ = self.apply(self.preflight(action='copy'))
        self.assertTrue(result['verified'])

    def test_unknown_stale_and_case_alias_rows(self):
        for changes in ({'snapshotToken':'stale'},{'id':'missing.txt'}):
            with self.assertRaises(StaleRead): self.preflight(**changes)
        self.entries.append(replace(self.entries[3],path=self.source.upper()))
        with self.assertRaises(StaleRead): self.preflight()
        self.assertFalse(self.commands)

    def test_unsafe_source_statuses(self):
        original = self.entries[3]
        for change in ([{'item':v} for v in ('added','deleted','replaced','missing','conflicted','unversioned','external')]
                       + [{key:True} for key in ('tree_conflicted','copied','switched','file_external','wc_locked')]
                       + [{'moved_from':'x'},{'moved_to':'x'},{'props':'conflicted'}]):
            self.entries[3] = replace(original,**change); self.snapshot=('s',status_rows(self.client))
            with self.subTest(change=change), self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.commands)

    def test_parent_status_and_repository_identity(self):
        original = self.entries[2]
        for change in ({'item':'added'},{'wc_locked':True},{'copied':True},{'props':'conflicted'}):
            self.entries[2] = replace(original,**change)
            with self.subTest(change=change), self.assertRaises(ValueError): self.preflight()
        self.entries[2] = original
        for changes in ({'url':'https://example.test/other'},{'wc-info/wcroot-abspath':str(self.root/'dest')},
                        {'wc-info/schedule':'add'},{'repository/uuid':'foreign'},{'lock':'secret'},
                        {'wc-info/copy-from-url':'https://example.test/other'}):
            self.info_changes['dest'] = changes
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.commands)

    def test_metadata_locks_conflicts_and_copy_source_refused(self):
        for changes in ({'lock':'SECRET'},{'conflict':'SECRET'},{'tree-conflict':'SECRET'},
                        {'wc-info/copy-from-url':self.url(self.source)},{'@revision':'-1'}):
            self.info_changes[self.source] = changes
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.preflight()
        self.info_changes={}; self.status_changes[self.source]='lock'
        with self.assertRaises(ValueError): self.preflight()

    def test_existing_destination_missing_parent_metadata_and_case_collision(self):
        path = self.root/self.destination; path.write_text('existing')
        with self.assertRaises(ValueError): self.preflight()
        path.unlink(); path.with_name(path.name.upper()).write_text('alias')
        with self.assertRaises(ValueError): self.preflight()
        path.with_name(path.name.upper()).unlink()
        self.entries.append(self.backend.StatusEntry(self.destination,'deleted'))
        with self.assertRaises(ValueError): self.preflight()
        self.entries.pop()
        with self.assertRaises(ValueError): self.preflight(destination='missing/new.txt')
        self.entries = [e for e in self.entries if e.path != 'dest']
        with self.assertRaises(ValueError): self.preflight()

    def test_alias_in_source_or_destination_parent(self):
        if os.name == 'nt': self.skipTest('Case-sensitive synthetic collision tested on POSIX.')
        (self.root/'SRC').mkdir()
        with self.assertRaises(ValueError): self.preflight()
        (self.root/'SRC').rmdir(); (self.root/'DEST').mkdir()
        with self.assertRaises(ValueError): self.preflight()

    def test_source_directory_hardlink_missing_and_nested_wc(self):
        path=self.root/self.source; path.unlink()
        with self.assertRaises(ValueError): self.preflight()
        path.mkdir()
        with self.assertRaises(ValueError): self.preflight()
        path.rmdir(); os.link(self.root/'other.txt',path)
        with self.assertRaises(ValueError): self.preflight()
        path.unlink(); path.write_text('regular'); (self.root/'dest'/'.svn').mkdir()
        with self.assertRaises(ValueError): self.preflight()

    def test_symlink_source_and_destination_parent(self):
        path=self.root/self.source; path.unlink()
        try: path.symlink_to(self.root/'other.txt')
        except OSError as error:
            if os.name == 'nt' and getattr(error,'winerror',None)==1314: self.skipTest('Windows symlink permission unavailable.')
            raise
        with self.assertRaises(ValueError): self.preflight()
        path.unlink(); path.write_text('regular'); (self.root/'dest').rmdir(); (self.root/'dest').symlink_to(self.root/'src',target_is_directory=True)
        with self.assertRaises(ValueError): self.preflight()

    def test_oversized_file_refused_before_metadata(self):
        with (self.root/self.source).open('r+b') as stream: stream.truncate(32*1024*1024+1)
        with self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.reads); self.assertFalse(self.commands)

    def test_content_property_and_parent_changes_consume_plan_without_dispatch(self):
        plan=self.preflight(); (self.root/self.source).write_bytes(b'changed content')
        with self.assertRaises(ValueError): self.apply(plan)
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertFalse(self.commands); self.invalidate.assert_not_called()

    def test_property_change_after_review(self):
        plan=self.preflight(); self.props['review:note']='new'
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertFalse(self.commands)

    def test_parent_metadata_change_after_review(self):
        plan=self.preflight(); self.info_changes['dest']={'@revision':'8'}
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertFalse(self.commands)

    def test_destination_appeared_after_review(self):
        plan=self.preflight(); (self.root/self.destination).write_text('new unrelated data')
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual((self.root/self.destination).read_text(),'new unrelated data')
        self.assertFalse(self.commands)

    def test_expired_and_wrong_scope_consume_plan(self):
        plan=self.preflight(); self.service.plan['expires']=0
        with self.assertRaises(StaleRead): self.apply(plan)
        plan=self.preflight()
        with self.assertRaises(StaleRead): self.apply(plan,selectionVersion=2)
        self.assertFalse(self.commands)

    def test_wrong_manager_argv_cannot_widen_dispatch(self):
        plan=self.preflight()
        original=self.manager._argv
        self.manager._argv=lambda *args:[*original(*args), 'other.txt']
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertFalse(self.commands)

    def test_guard_refuses_any_unreviewed_or_repeated_dispatch(self):
        guard=self.manager.client
        for args in (['delete','--',self.source],['move','--',self.source,self.destination],['copy','--',self.source,self.destination]):
            with self.assertRaises(ValueError): guard.command(args)
        self.assertFalse(self.commands)

    def test_post_attempt_failures_are_uncertain_and_one_use(self):
        self.mode='raise-after-mutate'; plan=self.preflight()
        with self.assertRaises(MutationUncertain): self.apply(plan)
        self.assertTrue((self.root/self.destination).exists()); self.invalidate.assert_called_once()
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertEqual(len(self.commands),1)

    def test_failed_launch_still_uncertain_and_never_retried(self):
        self.mode='raise-before-mutate'; plan=self.preflight()
        with self.assertRaises(MutationUncertain): self.apply(plan)
        self.assertTrue((self.root/self.source).exists()); self.assertEqual(len(self.commands),1)

    def test_missing_postcondition_is_uncertain(self):
        self.mode='no-mutation'; plan=self.preflight()
        with self.assertRaises(MutationUncertain): self.apply(plan)

    def test_bad_destination_content_is_uncertain(self):
        self.mode='bad-destination-bytes'; plan=self.preflight()
        with self.assertRaises(MutationUncertain): self.apply(plan)

    def test_bad_destination_properties_is_uncertain(self):
        self.mode='bad-destination-properties'; plan=self.preflight()
        with self.assertRaises(MutationUncertain): self.apply(plan)

    def test_bad_copy_provenance_is_uncertain(self):
        self.after_command=lambda:self.info_changes.update({self.destination:{'wc-info/copy-from-rev':'8'}})
        with self.assertRaises(MutationUncertain): self.apply(self.preflight())

    def test_copy_foreign_wc_root_remains_uncertain(self):
        self.after_command=lambda:self.info_changes.update({self.destination:{'wc-info/wcroot-abspath':str(self.root/'other-wc')}})
        with self.assertRaises(MutationUncertain): self.apply(self.preflight(action='copy'))
        self.assertEqual(len(self.commands), 1)

    def test_copy_wc_root_traversal_alias_remains_uncertain(self):
        alias = str(self.root) + '/nested/..'
        self.after_command=lambda:self.info_changes.update({self.destination:{'wc-info/wcroot-abspath':alias}})
        with self.assertRaises(MutationUncertain): self.apply(self.preflight(action='copy'))

    def test_post_move_nested_working_copy_parent_remains_uncertain(self):
        self.after_command=lambda:(self.root/'dest'/'.svn').mkdir()
        with self.assertRaises(MutationUncertain): self.apply(self.preflight())
        self.assertEqual(len(self.commands), 1)

    def test_post_move_case_alias_parent_remains_uncertain(self):
        if os.name == 'nt': self.skipTest('Case-sensitive synthetic collision tested on POSIX.')
        self.after_command=lambda:(self.root/'DEST').mkdir()
        with self.assertRaises(MutationUncertain): self.apply(self.preflight())
        self.assertEqual(len(self.commands), 1)

    def test_copy_source_change_is_uncertain(self):
        self.after_command=lambda:(self.root/self.source).write_bytes(b'changed after dispatch')
        with self.assertRaises(MutationUncertain): self.apply(self.preflight(action='copy'))

    def test_unselected_status_change_is_uncertain(self):
        self.mode='unselected-change'
        with self.assertRaises(MutationUncertain): self.apply(self.preflight())

    def test_oversized_command_output_is_uncertain(self):
        self.mode='oversized-output'
        with self.assertRaises(MutationUncertain): self.apply(self.preflight())

    def test_verified_result_survives_final_status_failure(self):
        original=self.manager._verify_result; calls=[]
        def verify(plan):
            original(plan); calls.append(1)
            if len(calls)==2: self.client.status.side_effect=RuntimeError('private refresh error')
        self.manager._verify_result=verify
        result,rows=self.apply(self.preflight())
        self.assertTrue(result['verified']); self.assertIsNone(rows)
        self.assertEqual(result['warnings'],['status-refresh-failed']); self.assertNotIn('private',repr(result))

    def test_workspace_change_after_dispatch_is_uncertain(self):
        self.after_command=lambda:setattr(self.check,'side_effect',ValueError('root changed'))
        with self.assertRaises(MutationUncertain): self.apply(self.preflight())

    def test_workspace_check_failure_before_dispatch(self):
        plan=self.preflight(); self.check.side_effect=ValueError('root changed')
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertFalse(self.commands)

    def test_bad_and_oversized_metadata_is_refused(self):
        for payload in (b'<!DOCTYPE info><info/>',b'<info>\0</info>',b'<info/>',b'x'*(file_ops.XML_BYTES+1)):
            self.raw_hook=lambda cmd,path,raw,value=payload:value
            with self.subTest(payload=payload[:30]), self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.commands)

    def test_duplicate_info_and_special_property_refused(self):
        def duplicate(cmd,path,raw):
            root=ET.fromstring(raw)
            if cmd=='info': ET.SubElement(root[0],'wc-info')
            return ET.tostring(root)
        self.raw_hook=duplicate
        with self.assertRaises(ValueError): self.preflight()
        self.raw_hook=lambda cmd,path,raw:raw; self.props['svn:special']='*'
        with self.assertRaises(ValueError): self.preflight()

    def test_malformed_xml_structure_cannot_hide_in_projection(self):
        def corrupt(cmd, path, raw, kind):
            root=ET.fromstring(raw)
            if kind=='comment': root.append(ET.Comment('hidden'))
            elif kind=='processing': root.append(ET.ProcessingInstruction('test','hidden'))
            elif kind=='root-text': root.text='non-whitespace'
            elif kind=='tail': root[0].tail='non-whitespace'
            elif kind=='entry-text' and cmd=='info': root[0].text='non-whitespace'
            elif kind=='nested-entry' and cmd=='info': ET.SubElement(root[0].find('wc-info'),'entry')
            elif kind=='duplicate-schedule' and cmd=='info': ET.SubElement(root[0].find('wc-info'),'schedule').text='normal'
            elif kind=='status-child' and cmd=='status' and path==self.source: ET.SubElement(root.find('.//wc-status'),'entry')
            elif kind=='status-duplicate' and cmd=='status' and path==self.source: ET.SubElement(root.find('.//entry'),'wc-status',item='normal',props='none')
            elif kind=='status-unknown' and cmd=='status' and path==self.source: root.find('.//wc-status').set('unknown','true')
            return ET.tostring(root)
        for kind in ('comment','processing','root-text','tail','entry-text','nested-entry','duplicate-schedule',
                     'status-child','status-duplicate','status-unknown'):
            self.raw_hook=lambda cmd,path,raw,kind=kind:corrupt(cmd,path,raw,kind)
            with self.subTest(kind=kind), self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.commands)

    def test_original_status_stream_receives_remaining_budget_and_output_cap(self):
        del self.client.status
        self.manager.client.deadline=file_ops.time.monotonic()+3
        stream=importlib.import_module('svn_client.status_stream')
        with patch.object(stream,'run_status',return_value=self.entries) as run:
            rows=status_rows(self.manager.client)
        self.assertIn(self.source,rows)
        self.assertGreater(run.call_args.kwargs['timeout'],0)
        self.assertLessEqual(run.call_args.kwargs['timeout'],3)
        self.assertEqual(run.call_args.kwargs['max_bytes'],8*1024*1024)
        self.assertEqual(run.call_args.args[0][-2:],['--','.'])

    def test_total_metadata_and_directory_budget(self):
        with patch.object(file_ops,'TOTAL_XML_BYTES',64), self.assertRaises(ValueError): self.preflight()
        with patch.object(file_ops,'MAX_DIRECTORY_ENTRIES',1), self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.commands)

    def test_cooperative_deadline_checked_after_metadata(self):
        def expire(cmd,path): self.manager.client.deadline=0
        self.after_read=expire
        with self.assertRaises(ValueError): self.preflight()
        self.assertFalse(self.commands); self.assertIsNone(self.manager.client.deadline)

    def test_original_manager_and_globals_are_not_modified(self):
        original=self.module._snapshot_bytes
        self.preflight()
        self.assertIs(self.module._snapshot_bytes,original)
        self.assertEqual(self.module.FileOperationManager._preflight.__module__,self.module.__name__)
        self.assertFalse(getattr(self.client,'write_attempted',False))


if __name__ == '__main__': unittest.main()
