"""Regression coverage for Windows stat/fstat timestamp semantics and races."""
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from svn_client import conflicts, file_identity, fileops, shelves


class FileIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "payload.bin"
        self.payload = b"ordinary unchanged contents\x00\xff"
        self.path.write_bytes(self.payload)

    def node(self, **changes):
        node = self.path.lstat()
        values = {key: getattr(node, key) for key in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")}
        values["st_birthtime_ns"] = getattr(node, "st_birthtime_ns", node.st_ctime_ns)
        values.update(changes)
        return SimpleNamespace(**values)

    def readers(self):
        return (
            lambda: fileops._snapshot_bytes(self.path)[0],
            lambda: conflicts._file_digest(self.path, conflicts._fingerprint(self.path.lstat())),
            lambda: shelves._file_digest(self.path),
        )

    def test_windows_creation_time_is_comparable_across_path_and_handle(self):
        opened = self.node(st_ctime_ns=self.path.stat().st_ctime_ns + 12345)
        with patch.object(file_identity, "_WINDOWS", True):
            for reader in self.readers():
                with self.subTest(reader=reader), patch.object(os, "fstat", return_value=opened):
                    self.assertEqual(reader(), hashlib.sha256(self.payload).hexdigest())

    def test_handle_change_time_during_read_is_still_rejected(self):
        opened = self.node(st_ctime_ns=self.path.stat().st_ctime_ns + 12345)
        changed = self.node(st_ctime_ns=opened.st_ctime_ns + 1)
        with patch.object(file_identity, "_WINDOWS", True):
            for reader in self.readers():
                with self.subTest(reader=reader), patch.object(os, "fstat", side_effect=[opened, changed]):
                    with self.assertRaises((fileops.FileOperationError, conflicts.ConflictError, shelves.ShelfError)):
                        reader()

    def test_replaced_or_modified_handle_is_rejected(self):
        baseline = self.node()
        with patch.object(file_identity, "_WINDOWS", True):
            for field in ("st_ino", "st_size", "st_mtime_ns"):
                opened = self.node(**{field: getattr(baseline, field) + 1})
                for reader in self.readers():
                    with self.subTest(field=field, reader=reader), patch.object(os, "fstat", return_value=opened):
                        with self.assertRaises((fileops.FileOperationError, conflicts.ConflictError, shelves.ShelfError)):
                            reader()

    def test_modified_real_file_with_preserved_mtime_can_be_read(self):
        before = self.path.stat()
        self.path.write_bytes(self.payload + b"modified")
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns - 5_000_000_000))
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        for reader in self.readers():
            with self.subTest(reader=reader):
                self.assertEqual(reader(), expected)

    def test_posix_keeps_metadata_change_time_even_when_birthtime_exists(self):
        node = self.node(st_ctime_ns=20, st_birthtime_ns=10)
        with patch.object(file_identity, "_WINDOWS", False):
            self.assertEqual(file_identity.identity_time_ns(node), 20)

    def test_older_windows_python_uses_its_consistent_creation_time_field(self):
        with patch.object(file_identity, "_WINDOWS", True):
            self.assertEqual(file_identity.identity_time_ns(SimpleNamespace(st_ctime_ns=10)), 10)
            self.assertEqual(file_identity.identity_time_ns(
                SimpleNamespace(st_ctime_ns=10, st_birthtime_ns=None)), 10)


if __name__ == "__main__":
    unittest.main()
