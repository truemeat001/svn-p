"""Exact-file add tests: synthetic temporary WC and fake SVN I/O only."""
from dataclasses import replace
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from add_ops import AddService, AddUnavailable, validate_request
from changelist_ops import MutationUncertain, status_rows
from file_reads import StaleRead
from workspace_bridge import trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


def native(path):
    return str(Path(path))


def request():
    return dict(workspaceId='w', selectionVersion=1, snapshotToken='s', ids=[native('src/new.txt')])


class RequestTests(unittest.TestCase):
    def test_strict_selection(self):
        validate_request('preflight', request())
        for ids in ([], ['a', 'A'], ['a'] * 101, ['../a'], ['a/../b'], ['.svn/a'], ['/a'], ['C:a'], ['a.'], [1], ['\ud800']):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_request('preflight', {**request(), 'ids': ids})
        for key in ('force', 'parents', 'action', 'autoProps'):
            with self.assertRaises(ValueError): validate_request('preflight', {**request(), key: True})

    def test_explicit_apply(self):
        data = {k: v for k, v in request().items() if k != 'ids'}
        data.update(planToken='p', confirmed=True)
        validate_request('apply', data)
        for confirmation in (False, 1, 'true', None):
            with self.assertRaises(ValueError): validate_request('apply', {**data, 'confirmed': confirmation})
        with self.assertRaises(ValueError): validate_request('apply', {**data, 'ids': ['a']})
        with self.assertRaises(AddUnavailable): AddService(object(), object(), lambda: None)


@unittest.skipUnless((BACKEND / 'svn_client/history.py').is_file(), 'Trusted paired backend required.')
class AddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'SYNTHETIC-adds'
        (self.root / '.svn').mkdir(parents=True); (self.root / 'src').mkdir()
        self.names = [native('src/new.txt'), native('src/b @.txt'), '--option.txt']
        for name in self.names: (self.root / name).write_bytes(b'original contents\n')
        self.backend, *_ = trusted_modules(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.client = self.backend.SvnClient(self.root)
        self.manager = importlib.import_module('svn_client.history').HistoryManager(self.client)
        self.entries = [self.backend.StatusEntry('.', 'normal', revision=1),
                        self.backend.StatusEntry('src', 'normal', revision=1)]
        self.entries += [self.backend.StatusEntry(name, 'unversioned') for name in self.names]
        self.client.status = Mock(side_effect=lambda **kw: list(self.entries))
        self.client.command = Mock(side_effect=self.command)
        self.manager._run = Mock(side_effect=lambda args, limit: self.info_xml(args[-1]))
        self.check = Mock(); self.invalidate = Mock()
        self.service = AddService(self.client, self.manager, self.check)
        self.data = request(); self.snapshot = ('s', status_rows(self.client))
        self.stderr = ''; self.mutate = True; self.command_error = None
        self.info_overrides = {}; self.after_command = lambda: None

    def info_xml(self, target):
        raw = target[:-1] if '@' in target else target
        entry_status = next(e for e in self.entries if e.path == raw)
        tree = ET.Element('info')
        entry = ET.SubElement(tree, 'entry', kind='dir' if raw in {'.', 'src'} else 'file', revision='1')
        ET.SubElement(entry, 'url').text = 'https://example.test/repo' + ('' if raw == '.' else '/' + quote(raw.replace('\\', '/')))
        repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'uuid').text = 'fixture'
        wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(self.root)
        ET.SubElement(wc, 'schedule').text = 'add' if entry_status.item == 'added' else 'normal'
        for key, value in self.info_overrides.get(raw, {}).items():
            entry.find(key).text = value
        return ET.tostring(tree)

    def command(self, args):
        self.assertIsNone(self.service.plan)
        self.invalidate.assert_called_once()
        self.assertEqual(args[:6], ['add', '--depth', 'empty', '--no-auto-props', '--', args[5]])
        ids = [target[:-1] if '@' in target else target for target in args[5:]]
        if self.mutate:
            self.entries = [replace(e, item='added') if e.path in ids else e for e in self.entries]
        self.after_command()
        if self.command_error: raise self.command_error
        return self.backend.CommandResult(tuple(args), 0, 'A selected file', self.stderr)

    def preflight(self, **changes):
        return self.service.preflight({**self.data, **changes}, self.snapshot)

    def apply(self, plan, **changes):
        data = {k: self.data[k] for k in ('workspaceId', 'selectionVersion', 'snapshotToken')}
        return self.service.apply(dict(data, planToken=plan['planToken'], confirmed=True, **changes), self.snapshot, self.invalidate)

    def test_preflight_read_only_apply_exact_and_one_use(self):
        plan = self.preflight(ids=self.names)
        self.assertFalse(plan['autoProps']); self.assertTrue(plan['localOnly']); self.assertEqual(plan['expiresIn'], 120)
        self.client.command.assert_not_called()
        self.assertTrue(all(c.args[0][0] == 'info' and '--depth' in c.args[0] for c in self.manager._run.call_args_list))
        result, rows = self.apply(plan)
        self.assertEqual(result, dict(action='add', files=[p.replace(os.sep, '/') for p in self.names], outcome='verified', verified=True, autoProps=False))
        self.assertEqual(self.client.command.call_args.args[0], ['add', '--depth', 'empty', '--no-auto-props', '--', self.names[0], self.names[1] + '@', self.names[2]])
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertEqual(self.client.command.call_count, 1)

    def test_stale_unknown_and_case_alias_rows(self):
        for changes in ({'snapshotToken': 'old'}, {'ids': ['unknown.txt']}):
            with self.assertRaises(StaleRead): self.preflight(**changes)
        self.entries.append(replace(self.entries[2], path=self.names[0].upper()))
        with self.assertRaises(StaleRead): self.preflight()
        self.client.command.assert_not_called()

    def test_all_unsafe_selected_states(self):
        original = self.entries[2]
        for change in ([{'item': item} for item in ('normal', 'ignored', 'added', 'external', 'conflicted', 'missing')]
                       + [{key: True} for key in ('switched', 'copied', 'tree_conflicted', 'wc_locked', 'file_external')]
                       + [{'props': 'conflicted'}, {'moved_from': 'other'}, {'changelist': 'name'}]):
            with self.subTest(change=change):
                self.entries[2] = replace(original, **change); self.snapshot = ('s', status_rows(self.client))
                with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_unsafe_parent_and_foreign_or_switched_info(self):
        original = self.entries[1]
        for change in ({'item': 'added'}, {'wc_locked': True}, {'switched': True}, {'tree_conflicted': True}):
            self.entries[1] = replace(original, **change)
            with self.assertRaises(ValueError): self.preflight()
        self.entries[1] = original
        for overrides in ({'url': 'https://example.test/repo/switched'}, {'wc-info/wcroot-abspath': str(self.root / 'src')}, {'wc-info/schedule': 'add'}):
            self.info_overrides['src'] = overrides
            with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_missing_directory_hardlink_and_nested_wc_rejected(self):
        path = self.root / self.names[0]
        path.unlink()
        with self.assertRaises(ValueError): self.preflight()
        path.mkdir()
        with self.assertRaises(ValueError): self.preflight()
        path.rmdir(); os.link(self.root / self.names[1], path)
        with self.assertRaises(ValueError): self.preflight()
        path.unlink(); path.write_text('ordinary'); (self.root / 'src' / '.svn').mkdir()
        with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_symlink_rejected(self):
        path = self.root / self.names[0]; path.unlink()
        try: path.symlink_to(self.root / self.names[1])
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314: self.skipTest('Windows symlink privilege unavailable.')
            raise
        with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_content_and_parent_changes_consume_plan(self):
        plan = self.preflight(); path = self.root / self.names[0]
        old = path.stat(); path.write_bytes(b'x' * old.st_size); os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertIsNone(self.service.plan); self.client.command.assert_not_called()
        plan = self.preflight(); self.info_overrides['src'] = {'repository/uuid': 'changed'}
        with self.assertRaises(ValueError): self.apply(plan)
        self.client.command.assert_not_called()

    def test_equivalent_xml_attribute_order_does_not_expire_plan(self):
        original = self.manager._run.side_effect
        count = 0
        def alternating(args, limit):
            nonlocal count
            count += 1
            root = ET.fromstring(original(args, limit))
            for node in root.iter():
                attributes = sorted(node.attrib.items(), reverse=bool(count % 2))
                node.attrib.clear()
                node.attrib.update(attributes)
            return ET.tostring(root)
        self.manager._run.side_effect = alternating
        plan = self.preflight()
        result, _ = self.apply(plan)
        self.assertTrue(result['verified'])
        self.assertGreater(count, 4)

    def test_xml_identity_keeps_all_values_text_and_child_order(self):
        from add_ops import _xml_identity
        first = ET.fromstring('<entry kind="dir" revision="1"><a>value</a><b /></entry>')
        reordered = ET.fromstring('<entry revision="1" kind="dir"><a>value</a><b /></entry>')
        self.assertEqual(_xml_identity(first), _xml_identity(reordered))
        for xml in ('<entry kind="dir" revision="2"><a>value</a><b /></entry>',
                    '<entry kind="dir" revision="1"><a>changed</a><b /></entry>',
                    '<entry kind="dir" revision="1"><b /><a>value</a></entry>',
                    '<entry kind="dir" revision="1"><a>value</a>tail<b /></entry>'):
            self.assertNotEqual(_xml_identity(first), _xml_identity(ET.fromstring(xml)))

    def test_parent_alias_and_missing_parent_rows(self):
        self.entries.append(replace(self.entries[1], path='SRC'))
        with self.assertRaises(ValueError): self.preflight()
        self.entries = [entry for entry in self.entries if entry.path not in {'src', 'SRC'}]
        with self.assertRaises(ValueError): self.preflight()
        self.client.command.assert_not_called()

    def test_bounded_fingerprints_before_write(self):
        with patch('changelist_ops.MAX_FILE_BYTES', 1), self.assertRaises(ValueError):
            self.preflight()
        self.client.command.assert_not_called()

    def test_file_change_during_parent_validation_is_rejected(self):
        original = self.manager._run.side_effect
        changed = False
        def change(args, limit):
            nonlocal changed
            if not changed:
                changed = True
                (self.root / self.names[0]).write_text('changed during local info')
            return original(args, limit)
        self.manager._run.side_effect = change
        with self.assertRaises(StaleRead): self.preflight()
        self.client.command.assert_not_called()

    def test_snapshot_invalidated_before_a_partial_write(self):
        plan = self.preflight(ids=self.names)
        self.command_error = RuntimeError('partial add')
        with self.assertRaises(MutationUncertain): self.apply(plan)
        self.invalidate.assert_called_once()
        self.assertEqual(self.client.command.call_count, 1)
        self.assertIsNone(self.service.plan)

    def test_expired_and_replaced_plans(self):
        plan = self.preflight(); self.service.plan['expires'] = 0
        with self.assertRaises(StaleRead): self.apply(plan)
        first = self.preflight(); second = self.preflight()
        with self.assertRaises(StaleRead): self.apply(first)
        with self.assertRaises(StaleRead): self.apply(second)
        self.client.command.assert_not_called()

    def test_diagnostics_failure_and_bad_postconditions_never_retry(self):
        for failure in ('diagnostics', 'timeout', 'nochange', 'content', 'unselected'):
            with self.subTest(failure=failure):
                self.setUp()
                plan = self.preflight()
                if failure == 'diagnostics': self.stderr = 'warning'
                if failure == 'timeout': self.command_error = TimeoutError('unknown')
                if failure == 'nochange': self.mutate = False
                if failure == 'content': self.after_command = lambda: (self.root / self.names[0]).write_text('changed')
                if failure == 'unselected': self.after_command = lambda: self.entries.append(self.backend.StatusEntry('other', 'added'))
                with self.assertRaises(MutationUncertain): self.apply(plan)
                with self.assertRaises(StaleRead): self.apply(plan)
                self.assertEqual(self.client.command.call_count, 1)
                self.invalidate.assert_called_once()


if __name__ == '__main__': unittest.main()
