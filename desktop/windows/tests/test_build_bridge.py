import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'build_bridge.py'
spec = importlib.util.spec_from_file_location('build_bridge', MODULE)
build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
ROOT = MODULE.parents[2]


class BuildInputTests(unittest.TestCase):
    def test_fixed_input_allowlist(self):
        files = build.inputs(ROOT)
        self.assertEqual(len(files), 27)
        self.assertTrue(all(not any(part in {'.git', '.svn', '__pycache__', '.env'} for part in Path(p).parts) for p in files))
        self.assertNotIn('README.md', files)
        self.assertIn('prototypes/svn-desk-web/desktop_bridge.py', files)
        self.assertIn('svn_client/file_identity.py', files)

    def test_manifest_covers_exact_inputs(self):
        files = build.inputs(ROOT)
        values = build.manifest(files)
        self.assertEqual(set(values), set(files))
        self.assertTrue(all(len(value) == 64 for value in values.values()))

    def test_dependency_scan_ignores_non_python_assets(self):
        files = build.inputs(ROOT)
        hidden = build.stdlib_imports(files.values())
        self.assertIn('xml.etree.ElementTree', hidden)
        self.assertIn('zlib', hidden)
        self.assertNotIn('svn_client', hidden)
        self.assertEqual(hidden, sorted(set(hidden)))

    def test_plain_file_allowed_but_directory_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'x').write_text('hello')
            self.assertEqual(build.regular_source(root, Path('x')), root/'x')
            (root/'sub').mkdir()
            with self.assertRaises(ValueError): build.regular_source(root, Path('sub'))

    def test_missing_file_fails_instead_of_incomplete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError): build.inputs(Path(directory))

    def test_runtime_notices_are_fixed_installed_dependency_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'LICENSE.txt').write_text('Python license')
            (root/'COPYING.txt').write_text('PyInstaller notice')
            dist = SimpleNamespace(files=[Path('COPYING.txt')], locate_file=lambda path: root/path)
            with patch.object(build.sys, 'base_prefix', str(root)), patch.object(build.importlib.metadata, 'distribution', return_value=dist):
                result = build.runtime_notices()
            self.assertEqual(result, {'Python-LICENSE.txt': root/'LICENSE.txt', 'PyInstaller-COPYING.txt': root/'COPYING.txt'})

    def test_missing_python_notice_blocks_bundle(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build.sys, 'base_prefix', directory):
            with self.assertRaisesRegex(RuntimeError, 'Python runtime license'): build.runtime_notices()

    def test_missing_freezer_notice_blocks_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'LICENSE.txt').write_text('Python license')
            with patch.object(build.sys, 'base_prefix', directory), patch.object(build.importlib.metadata, 'distribution', return_value=SimpleNamespace(files=[])):
                with self.assertRaisesRegex(RuntimeError, 'PyInstaller distribution notice'): build.runtime_notices()

    def test_unexpectedly_large_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root/'large.py').open('wb') as stream:
                stream.truncate(4 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError): build.regular_source(root, Path('large.py'))


if __name__ == '__main__':
    unittest.main()
