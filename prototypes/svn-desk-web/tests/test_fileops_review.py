"""Independent move/copy review. All SVN transport is synthetic; no real WC."""
from dataclasses import replace
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

import file_ops
from changelist_ops import MutationUncertain, status_rows
from file_reads import StaleRead
from workspace_bridge import trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


class ExactStatusTextReview(unittest.TestCase):
    def test_leaf_working_status_and_commit_diagnostics_are_not_ignored(self):
        for body in ('unexpected diagnostic', '<commit revision="7">unexpected diagnostic</commit>'):
            raw = ('<status><target path="a.txt"><entry path="a.txt">'
                   '<wc-status item="normal" props="none" revision="7">' + body +
                   '</wc-status></entry></target></status>')
            with self.subTest(body=body), self.assertRaises(ValueError):
                root = ET.fromstring(raw)
                file_ops._xml_structure(root)
                file_ops._exact_status(root, 'a.txt', Path('/synthetic'))


@unittest.skipUnless((BACKEND / 'svn_client/fileops.py').is_file(), 'Trusted paired backend required.')
class IndependentFileOperationReview(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'SYNTHETIC-fileops-review'
        (self.root / '.svn').mkdir(parents=True)
        (self.root / 'src').mkdir(); (self.root / 'dest').mkdir()
        self.source = str(Path('src/source@name.txt'))
        self.destination = str(Path('dest/target@name.txt'))
        (self.root / self.source).write_bytes(b'review source\n')
        self.backend, *_ = trusted_modules(BACKEND)
        self.history = importlib.import_module('svn_client.history')
        module = file_ops.fileops_module(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/synthetic/svn'):
            self.client = self.backend.SvnClient(self.root)
        self.entries = [self.backend.StatusEntry(p, 'normal', revision=7) for p in ('.', 'src', 'dest', self.source)]
        self.client.status = Mock(side_effect=lambda **kw: list(self.entries))
        self.manager = file_ops.guarded_manager(module, self.history, self.client)
        self.check = Mock(); self.invalidate = Mock()
        self.service = file_ops.FileOperationService(self.manager, self.check)
        self.data = dict(workspaceId='w', selectionVersion=1, snapshotToken='s', action='copy', id=self.source, destination=self.destination.replace(os.sep, '/'))
        self.snapshot = ('s', status_rows(self.client))
        self.info_edit = lambda path, entry: None
        self.after_write = lambda: None
        self.writes = []
        self.stdout = b''
        self.transport = patch.object(self.history, '_bounded_process', side_effect=self.process)
        self.transport.start(); self.addCleanup(self.transport.stop)

    def info(self, path):
        status = next(e for e in self.entries if e.path == path)
        root = ET.Element('info')
        entry = ET.SubElement(root, 'entry', path=path, kind='dir' if path in {'.', 'src', 'dest'} else 'file', revision='7')
        ET.SubElement(entry, 'url').text = self.url(path)
        repo = ET.SubElement(entry, 'repository')
        ET.SubElement(repo, 'root').text = 'https://example.test/repo'
        ET.SubElement(repo, 'uuid').text = 'synthetic-review'
        wc = ET.SubElement(entry, 'wc-info')
        ET.SubElement(wc, 'wcroot-abspath').text = str(self.root)
        ET.SubElement(wc, 'schedule').text = 'add' if status.item == 'added' else 'normal'
        ET.SubElement(wc, 'depth').text = 'infinity'
        ET.SubElement(wc, 'checksum').text = 'original-metadata'
        if status.copied:
            ET.SubElement(wc, 'copy-from-url').text = self.url(self.source)
            ET.SubElement(wc, 'copy-from-rev').text = '7'
        self.info_edit(path, entry)
        return ET.tostring(root)

    def url(self, path):
        return 'https://example.test/repo' + ('' if path == '.' else '/' + quote(path.replace(os.sep, '/')))

    def status(self, path):
        root = ET.Element('status'); target = ET.SubElement(root, 'target', path=path)
        status = next((e for e in self.entries if e.path == path), None)
        if status:
            entry = ET.SubElement(target, 'entry', path=path)
            node = ET.SubElement(entry, 'wc-status', item=status.item, props=status.props, revision='7')
            for key in ('copied', 'switched', 'file_external', 'wc_locked', 'tree_conflicted', 'moved_from', 'moved_to'):
                value = getattr(status, key)
                if value:
                    node.set(key.replace('_', '-'), 'true' if value is True else value)
        return ET.tostring(root)

    def process(self, argv, **options):
        self.assertEqual(options['cwd'], self.root)
        self.assertGreater(options['timeout'], 0)
        action = argv[1]
        if action in {'copy', 'move'}:
            self.assertIsNone(self.service.plan)
            self.invalidate.assert_called_once()
            self.writes.append(argv)
            source_arg, destination_arg = argv[-2:]
            self.assertEqual(source_arg, self.source + '@')
            self.assertEqual(destination_arg, self.destination + ('@' if action == 'copy' else ''))
            src = self.root / self.source; dst = self.root / self.destination
            if action == 'copy':
                dst.write_bytes(src.read_bytes())
            else:
                src.rename(dst)
                self.entries = [replace(e, item='deleted', moved_to=self.destination) if e.path == self.source else e for e in self.entries]
            self.entries.append(self.backend.StatusEntry(self.destination, 'added', copied=True, moved_from=self.source if action == 'move' else None))
            self.after_write()
            return self.stdout
        target = argv[-1]
        path = target[:-1] if '@' in target else target
        if action == 'info': return self.info(path)
        if action == 'status': return self.status(path)
        if action == 'proplist': return b'<properties/>'
        self.fail('Unexpected synthetic command: ' + repr(argv))

    def preflight(self, action='copy'):
        self.data['action'] = action
        return self.service.preflight(self.data, self.snapshot)

    def apply(self, plan):
        data = {key: self.data[key] for key in ('workspaceId', 'selectionVersion', 'snapshotToken')}
        return self.service.apply(dict(data, confirmed=True, planToken=plan['planToken']), self.snapshot, self.invalidate)

    def test_at_source_and_copy_destination_keep_exact_names_and_source(self):
        plan = self.preflight()
        result, rows = self.apply(plan)
        self.assertTrue(result['verified'])
        self.assertTrue((self.root / self.source).is_file())
        self.assertTrue((self.root / self.destination).is_file())
        self.assertFalse((self.root / (self.destination + '@')).exists())
        self.assertEqual(rows[self.source]['item'], 'normal')
        with self.assertRaises(StaleRead): self.apply(plan)
        self.assertEqual(len(self.writes), 1)

    def test_at_move_destination_never_gains_peg_suffix(self):
        result, _ = self.apply(self.preflight('move'))
        self.assertTrue(result['verified'])
        self.assertFalse((self.root / self.source).exists())
        self.assertTrue((self.root / self.destination).is_file())
        self.assertFalse((self.root / (self.destination + '@')).exists())

    def test_copy_source_disappearance_is_uncertain_not_verified(self):
        plan = self.preflight()
        self.after_write = lambda: (self.root / self.source).unlink()
        with self.assertRaises(MutationUncertain): self.apply(plan)
        self.assertEqual(len(self.writes), 1)
        with self.assertRaises(StaleRead): self.apply(plan)

    def test_destination_wrong_url_is_not_verified_copy_provenance(self):
        plan = self.preflight()
        def wrong_url(path, entry):
            if path == self.destination:
                entry.find('url').text = 'https://example.test/repo/other-file'
        self.info_edit = wrong_url
        with self.assertRaises(MutationUncertain): self.apply(plan)

    def test_move_source_unsafe_post_status_is_not_verified(self):
        plan = self.preflight('move')
        self.after_write = lambda: setattr(self, 'entries', [replace(e, wc_locked=True) if e.path == self.source else e for e in self.entries])
        with self.assertRaises(MutationUncertain): self.apply(plan)

    def test_non_projected_info_field_change_consumes_plan_before_write(self):
        plan = self.preflight()
        def changed(path, entry):
            if path == self.source:
                entry.find('wc-info/checksum').text = 'changed-metadata'
        self.info_edit = changed
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.writes, [])
        self.assertIsNone(self.service.plan)

    def test_equivalent_xml_attribute_order_is_not_a_state_change(self):
        plan = self.preflight()
        def reorder(path, entry):
            for node in entry.iter():
                attrs = list(node.attrib.items())[::-1]
                node.attrib.clear(); node.attrib.update(attrs)
        self.info_edit = reorder
        result, _ = self.apply(plan)
        self.assertTrue(result['verified'])

    def test_expiry_during_last_preflight_never_dispatches(self):
        public = self.preflight(); internal = self.service.plan
        original = self.manager.preflight_copy
        def expire(*args, **kwargs):
            result = original(*args, **kwargs)
            internal['expires'] = 0
            return result
        with patch.object(self.manager, 'preflight_copy', side_effect=expire):
            with self.assertRaises(StaleRead): self.apply(public)
        self.assertEqual(self.writes, [])
        self.invalidate.assert_not_called()

    def test_expiry_during_final_dispatch_callback_never_dispatches(self):
        public = self.preflight(); internal = self.service.plan
        # The final snapshot invalidation is a callback owned by the adapter.
        # Expiry during that callback must still prevent the subsequent spawn.
        self.invalidate.side_effect = lambda: internal.update(expires=0)
        with self.assertRaises(StaleRead): self.apply(public)
        self.assertEqual(self.writes, [])
        self.assertIsNone(self.service.plan)

    def test_unexpected_stdout_after_mutation_is_uncertain_never_retried(self):
        public = self.preflight()
        self.stdout = b'Warning: an unexpected diagnostic after local mutation\n'
        with self.assertRaises(MutationUncertain): self.apply(public)
        self.assertEqual(len(self.writes), 1)
        self.assertTrue((self.root / self.destination).is_file())
        with self.assertRaises(StaleRead): self.apply(public)
        self.assertEqual(len(self.writes), 1)


if __name__ == '__main__': unittest.main()
