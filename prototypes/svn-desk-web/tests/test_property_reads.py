"""Synthetic exact-file WC metadata and mocked transport only; no SVN calls."""
import base64
from dataclasses import asdict
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from property_reads import PropertyReadService, _properties, _value, XML_BYTES
from file_reads import history_module, StaleRead
from workspace_bridge import trusted_modules

BACKEND = Path(os.environ.get('SVN_DESK_BACKEND_SOURCE', Path(__file__).resolve().parents[2] / 'svn-release-review-9bbc406'))


def props(path='src/a.txt', values=None):
    root = ET.Element('properties')
    if values is not None:
        target = ET.SubElement(root, 'target', path=path)
        for name, value in values.items():
            prop = ET.SubElement(target, 'property', name=name, encoding='base64')
            prop.text = base64.b64encode(value).decode()
    return ET.tostring(root)


@unittest.skipUnless((BACKEND / 'svn_client/history.py').is_file(), 'Trusted backend required.')
class PropertyReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'wc'
        (self.root / '.svn').mkdir(parents=True)
        (self.root / 'src').mkdir(); (self.root / 'src/a.txt').write_text('working')
        self.backend, _, _, _ = trusted_modules(BACKEND)
        self.module = history_module(BACKEND)
        with patch.object(self.backend, 'discover_svn', return_value='/trusted/svn'):
            self.client = self.backend.SvnClient(self.root)
        self.entry = self.backend.StatusEntry('src/a.txt', 'modified', props='modified', revision=3)
        self.client.status = Mock(side_effect=lambda *a, **kw: [self.entry])
        self.manager = self.module.HistoryManager(self.client)
        self.manager._run = Mock(side_effect=lambda args, limit: self.info(args[-1]))
        self.service = PropertyReadService(self.client, self.module, self.manager)
        self.working = {'a': b'new', 'added': b'value', 'same': b'x'}
        self.base = {'a': b'old', 'deleted': b'gone', 'same': b'x'}
        self.run = Mock(side_effect=self.response)
        patcher = patch.object(self.module, '_bounded_process', self.run)
        patcher.start(); self.addCleanup(patcher.stop)

    def row(self):
        return dict(asdict(self.entry), id=self.entry.path, kind='unknown')

    def info(self, target):
        relative = target[:-1] if target.endswith('@') else target
        root = ET.Element('info')
        entry = ET.SubElement(root, 'entry', kind='dir' if target == '.' else 'file', revision='3')
        ET.SubElement(entry, 'url').text = 'https://example.test/repo' + ('' if target == '.' else '/' + quote(Path(relative).as_posix()))
        repo = ET.SubElement(entry, 'repository'); ET.SubElement(repo, 'uuid').text = 'fixture'
        wc = ET.SubElement(entry, 'wc-info'); ET.SubElement(wc, 'wcroot-abspath').text = str(self.root)
        return ET.tostring(root)

    def response(self, argv, **kwargs):
        return props(self.entry.path, self.base if '--revision' in argv else self.working)

    def read(self):
        return self.service.read(self.row())

    def test_changes_and_fixed_local_argv(self):
        result = self.read()
        self.assertEqual([(p['name'], p['change']) for p in result['properties']],
                         [('a', 'modified'), ('added', 'added'), ('deleted', 'deleted'), ('same', 'unchanged')])
        self.assertTrue(result['localOnly']); self.assertTrue(result['displayOnly'])
        self.assertTrue(result['baseAvailable']); self.assertIsNone(result['baseUnavailableReason'])
        self.assertEqual(len(self.run.call_args_list), 4)
        for call in self.run.call_args_list:
            argv = call.args[0]
            prefix = ['/trusted/svn', 'proplist', '--non-interactive', '--no-auth-cache', '--xml', '--verbose', '--depth', 'empty']
            target = str(Path('src/a.txt'))
            self.assertIn(argv, [prefix + ['--', target], prefix + ['--revision', 'BASE', '--', target]])
            self.assertEqual(call.kwargs['stdout_limit'], XML_BYTES)
            self.assertEqual(call.kwargs['cwd'], self.root)
        self.client.status.assert_called_with(['src/a.txt'], verbose=True)

    def test_peg_and_option_like_filenames_remain_exact_targets(self):
        original = self.root / 'src/a.txt'
        for path in ['src/a@b.txt', '--config-option']:
            original.rename(self.root / path)
            self.entry = self.backend.StatusEntry(path, 'normal')
            result = self.read()
            self.assertEqual(result['path'], path)
            self.assertEqual(self.run.call_args.args[0][-1], str(Path(path)) + ('@' if '@' in path else ''))
            (self.root / path).rename(original)

    def test_invalid_paths_and_absent_file_never_read_properties(self):
        for path in ['', '.', '..', '/etc/passwd', 'src/../a', '.svn/wc.db', 'https://host/a', 'C:secret']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.service.read(dict(self.row(), id=path))
        (self.root / 'src/a.txt').unlink()
        with self.assertRaises(ValueError): self.read()
        self.run.assert_not_called()

    def test_timeout_invalid_never_invokes_transport(self):
        for timeout in [True, 0, -1, float('inf'), float('nan'), '10']:
            self.client.timeout = timeout
            with self.subTest(timeout=timeout), self.assertRaises(ValueError): self.read()
        self.run.assert_not_called()

    def test_added_skips_base_and_marks_unbased(self):
        self.entry = self.backend.StatusEntry('src/a.txt', 'added', props='modified')
        result = self.read()
        self.assertFalse(result['baseAvailable']); self.assertEqual(result['baseUnavailableReason'], 'added-file')
        self.assertTrue(all(p['change'] == 'unbased' and p['base'] is None for p in result['properties']))
        self.assertTrue(all('--revision' not in c.args[0] for c in self.run.call_args_list))

    def test_empty_successful_xml_is_empty_not_missing_base(self):
        self.run.return_value = props(); self.run.side_effect = None
        self.assertEqual(self.read()['properties'], [])
        self.assertTrue(self.read()['baseAvailable'])

    def test_base_failure_never_falls_back_and_does_not_leak(self):
        def response(argv, **kw):
            if '--revision' in argv: raise RuntimeError('password=secret raw stdout')
            return props(values=self.working)
        self.run.side_effect = response
        with self.assertRaisesRegex(ValueError, '^Local property read failed; no result was accepted.$'):
            self.read()

    def test_unsafe_status_refused_before_property_transport(self):
        for field, values in {'item': ['conflicted', 'deleted', 'replaced', 'unversioned', 'external', 'ignored', 'obstructed'],
                              'props': ['conflicted', 'added'], 'copied': [True], 'moved_from': ['x'], 'moved_to': ['y'],
                              'wc_locked': [True], 'switched': [True], 'file_external': [True], 'tree_conflicted': [True]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.service.read(dict(self.row(), **{field: value}))
        self.run.assert_not_called()

    def test_status_stale_before_and_after(self):
        with self.assertRaises(StaleRead): self.service.read(dict(self.row(), revision=4))
        self.run.assert_not_called()
        changed = self.backend.StatusEntry('src/a.txt', 'modified', props='modified', revision=4)
        self.client.status.side_effect = [[self.entry], [changed]]
        with self.assertRaises(StaleRead): self.read()

    def test_duplicate_and_foreign_status(self):
        for entries in [[], [self.entry, self.entry], [self.backend.StatusEntry('other', 'modified')]]:
            self.client.status.side_effect = None; self.client.status.return_value = entries
            with self.assertRaises(StaleRead): self.read()

    def test_observed_properties_and_identity_change_rejected(self):
        calls = 0
        def response(argv, **kw):
            nonlocal calls
            calls += 1
            if calls == 3: return props(values={'changed': b'yes'})
            return self.response(argv, **kw)
        self.run.side_effect = response
        with self.assertRaises(StaleRead): self.read()
        def replace_file(argv, **kw):
            result = self.response(argv, **kw)
            (self.root / 'src/a.txt').write_text('changed length')
            return result
        self.run.side_effect = replace_file
        with self.assertRaises(StaleRead): self.read()

    def test_info_attribute_order_is_ignored_but_values_bound(self):
        count = 0
        def response(args, limit):
            nonlocal count
            count += 1
            root = ET.fromstring(self.info(args[-1]))
            entry = root[0]
            if count > 3: entry.attrib = dict(reversed(list(entry.attrib.items())))
            return ET.tostring(root)
        self.manager._run.side_effect = response
        self.read()
        count = 0
        def changed(args, limit):
            nonlocal count
            count += 1
            root = ET.fromstring(self.info(args[-1]))
            if count > 3: root[0].set('revision', '4')
            return ET.tostring(root)
        self.manager._run.side_effect = changed
        with self.assertRaises(StaleRead): self.read()

    def test_foreign_root_and_repository_refused(self):
        original = self.info
        for tag, value in [('wc-info/wcroot-abspath', str(self.root.parent)), ('repository/uuid', 'foreign')]:
            def bad(args, limit):
                root = ET.fromstring(original(args[-1]))
                if args[-1] != '.': root[0].find(tag).text = value
                return ET.tostring(root)
            self.manager._run.side_effect = bad
            with self.assertRaises(ValueError): self.read()
        self.run.assert_not_called()

    def test_ordinary_file_only(self):
        path = self.root / 'src/a.txt'
        path.unlink(); path.mkdir()
        with self.assertRaises(ValueError): self.read()
        path.rmdir(); path.write_text('x'); os.link(path, self.root / 'hardlink')
        with self.assertRaises(ValueError): self.read()
        (self.root / 'hardlink').unlink(); (self.root / 'src/.svn').mkdir()
        with self.assertRaises(ValueError): self.read()
        self.run.assert_not_called()

    def test_symlink_rejected(self):
        path = self.root / 'src/a.txt'
        path.unlink()
        try:
            path.symlink_to(self.root / 'src', target_is_directory=True)
        except OSError as exc:
            if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
                self.skipTest('Windows account lacks symlink creation privilege (WinError 1314).')
            raise
        with self.assertRaises(ValueError): self.read()
        self.run.assert_not_called()

    def test_combined_limits(self):
        self.working = {str(i): b'' for i in range(128)}; self.base = {'extra': b''}
        with self.assertRaisesRegex(ValueError, 'Combined'): self.read()
        self.working = {str(i): b'x' * 65536 for i in range(3)}
        self.base = dict(self.working)
        with self.assertRaisesRegex(ValueError, 'Combined'): self.read()

    def test_byte_equality_precedes_redaction_and_truncation(self):
        self.base = {'a': b'https://user:one@host/path', 'b': b'x' * 5000 + b'1'}
        self.working = {'a': b'https://user:two@host/path', 'b': b'x' * 5000 + b'2'}
        for prop in self.read()['properties']:
            self.assertEqual(prop['change'], 'modified')
            self.assertEqual(prop['base']['text'], prop['working']['text'])
            self.assertNotEqual(prop['base']['sha256'], prop['working']['sha256'])


class PropertyParserTests(unittest.TestCase):
    def test_structural_rejections(self):
        cases = [b'\0', b'<!DOCTYPE properties><properties/>', b'<!ENTITY a "x"><properties/>',
                 b'<properties extra="1"/>', b'<properties>unexpected</properties>',
                 b'<properties><!-- hidden --></properties>', b'<properties><?x y?></properties>',
                 b'<properties><target path="src/a.txt"/><target path="src/a.txt"/></properties>',
                 b'<properties><target path="src/a.txt"><property name="x"/><property name="x"/></target></properties>',
                 b'<properties><target path="src/a.txt"><property name="x"><x/></property></target></properties>',
                 b'<properties><target path="src/a.txt"><property name="x" encoding="other"/></target></properties>',
                 b'<properties><target path="src/a.txt"><property name="x" encoding="base64">!!</property></target></properties>',
                 props('/src/a.txt', {}), props('src/../src/a.txt', {}), props('other', {}),
                 props(values={'a': b'x' * 65537}), b' ' * (XML_BYTES + 1)]
        for raw in cases:
            with self.subTest(raw=raw[:160]), self.assertRaises(ValueError):
                _properties(raw, Path('src/a.txt'))

    def test_windows_separator_only_xml_equivalence(self):
        relative = PurePosixPath('src/a.txt')
        # Replace only file_reads' module reference, never the process-wide
        # os.name used by pathlib. Pure paths work on both Windows and POSIX.
        with patch('file_reads.os', SimpleNamespace(name='nt')), patch('file_reads.Path', PurePosixPath):
            for name in ['src/a.txt', 'src\\a.txt']:
                self.assertEqual(_properties(props(name, {'x': b'y'}), relative), {'x': b'y'})
            for name in ['SRC/a.txt', './src/a.txt', 'src/../src/a.txt',
                         'C:\\src\\a.txt', '/src/a.txt', 'src//a.txt']:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    _properties(props(name, {}), relative)
        with patch('file_reads.os', SimpleNamespace(name='posix')), patch('file_reads.Path', PurePosixPath):
            with self.assertRaises(ValueError):
                _properties(props('src\\a.txt', {}), relative)

    def test_root_trailing_content_is_rejected(self):
        with self.assertRaises(ValueError):
            _properties(b'<properties/>extra', Path('src/a.txt'))
        root = ET.Element('properties'); root.tail = 'extra'
        with patch('property_reads.ET.fromstring', return_value=root):
            with self.assertRaises(ValueError):
                _properties(b'<properties/>', Path('src/a.txt'))

    def test_name_bounds_and_invalid_base64_forms(self):
        for name in ['', 'a b', 'a\t', 'a\u202e', 'x' * 257, '😀' * 129]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                _properties(props(values={name: b'x'}), Path('src/a.txt'))
        for value in ['YR==', 'YQ===', 'YQ', 'YQ==x', 'YQ==\u00a0']:
            root = ET.Element('properties'); target = ET.SubElement(root, 'target', path='src/a.txt')
            ET.SubElement(target, 'property', name='x', encoding='base64').text = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                _properties(ET.tostring(root), Path('src/a.txt'))

    def test_base64_wrapping_and_plain_xml_entities(self):
        raw = b'<properties><target path="src/a.txt"><property name="a" encoding="base64">YQ==\n</property><property name="b">&lt;&amp;</property></target></properties>'
        self.assertEqual(_properties(raw, Path('src/a.txt')), {'a': b'a', 'b': b'<&'})

    def test_binary_and_visible_control_metadata(self):
        for raw in [b'\xff', b'a\0b']:
            value = _value(raw, lambda text: text)
            self.assertEqual(value['kind'], 'binary'); self.assertIsNone(value['text'])
            self.assertFalse(value['redacted']); self.assertFalse(value['textTruncated'])
            self.assertEqual(value['escapedControls'], 0)
        value = _value('x\n\t\u202e\u2066\x7f\x85'.encode(), lambda text: text)
        self.assertEqual(value['escapedControls'], 6)
        self.assertEqual(value['text'], r'x\u000a\u0009\u202e\u2066\u007f\u0085')
        value = _value(('中' * 2000).encode(), lambda text: text)
        self.assertLessEqual(len(value['text'].encode()), 4096); self.assertTrue(value['textTruncated'])


if __name__ == '__main__': unittest.main()
