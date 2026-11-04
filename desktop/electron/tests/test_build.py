import hashlib
import importlib.util
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('electron_builder', Path(__file__).resolve().parents[1] / 'build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'out'
        self.out.mkdir()
        self.zip = self.root / 'input.zip'

    def make(self, entries):
        with zipfile.ZipFile(self.zip, 'w') as archive:
            for name, data in entries:
                if isinstance(name, str):
                    # Bypass ZipInfo.__init__ normalization only in the fixture,
                    # so Windows writes the deliberately unsafe raw ZIP name too.
                    item = zipfile.ZipInfo()
                    item.filename = item.orig_filename = name
                else:
                    item = name
                archive.writestr(item, data)

    def test_regular_archive(self):
        self.make([('a/', b''), ('a/b.txt', b'hello')])
        self.assertEqual(build.unzip_checked(self.zip, self.out), {'a/b.txt'})
        self.assertEqual(build.inventory(self.out), {'a/b.txt': hashlib.sha256(b'hello').hexdigest()})

    def test_prefix(self):
        self.make([('pkg/Bridge/a', b'a'), ('pkg/private', b'no')])
        build.unzip_checked(self.zip, self.out, 'pkg/Bridge/')
        self.assertEqual(list(build.inventory(self.out)), ['a'])

    def test_unsafe_paths(self):
        for name in ['../outside', '/absolute', 'dir/../outside', 'a\\b', 'C:stream', 'a/./b', 'a\x00hidden', 'a//b']:
            with self.subTest(name=name):
                self.make([(name, b'x')])
                with zipfile.ZipFile(self.zip) as archive:
                    self.assertEqual(archive.infolist()[0].orig_filename, name)
                with self.assertRaises(ValueError):
                    build.unzip_checked(self.zip, self.out)
                self.assertEqual(list(self.out.rglob('*')), [])

    def test_windows_normalized_backslash_is_rejected(self):
        self.make([('a\\b', b'x')])
        with zipfile.ZipFile(self.zip) as archive:
            item = archive.infolist()[0]
            self.assertEqual(item.orig_filename, 'a\\b')
            # Reproduce Windows ZipInfo behavior on every test platform.
            item.filename = 'a/b'
            with mock.patch.object(build.zipfile, 'ZipFile', return_value=archive):
                with self.assertRaises(ValueError):
                    build.unzip_checked(self.zip, self.out)
        self.assertEqual(list(self.out.rglob('*')), [])

    def test_normalized_prefix_cannot_hide_unsafe_original_name(self):
        self.make([('pkg\\Bridge\\a', b'x')])
        with zipfile.ZipFile(self.zip) as archive:
            archive.infolist()[0].filename = 'pkg/Bridge/a'
            with mock.patch.object(build.zipfile, 'ZipFile', return_value=archive):
                with self.assertRaises(ValueError):
                    build.unzip_checked(self.zip, self.out, 'pkg/Bridge/')
        self.assertEqual(list(self.out.rglob('*')), [])

    def test_unsafe_directory_name_is_rejected_before_directory_skip(self):
        self.make([('a\\b/', b'')])
        with self.assertRaises(ValueError):
            build.unzip_checked(self.zip, self.out)
        self.assertEqual(list(self.out.rglob('*')), [])

    def test_all_selected_names_checked_before_extracting_any_file(self):
        self.make([('safe', b'good'), ('../unsafe', b'bad')])
        with self.assertRaises(ValueError):
            build.unzip_checked(self.zip, self.out)
        self.assertEqual(list(self.out.rglob('*')), [])

    def test_case_collision(self):
        self.make([('a', b'x'), ('A', b'y')])
        with self.assertRaises(ValueError):
            build.unzip_checked(self.zip, self.out)
        self.assertEqual(list(self.out.rglob('*')), [])

    def test_symlink(self):
        item = zipfile.ZipInfo('link')
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.make([(item, b'../outside')])
        with self.assertRaises(ValueError):
            build.unzip_checked(self.zip, self.out)

    def test_existing_file_not_overwritten(self):
        (self.out / 'a').write_bytes(b'keep')
        self.make([('a', b'new')])
        with self.assertRaises(FileExistsError):
            build.unzip_checked(self.zip, self.out)
        self.assertEqual((self.out / 'a').read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
