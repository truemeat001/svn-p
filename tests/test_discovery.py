"""Working-copy discovery never needs SVN, user file reads, or writes."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from svn_client import discovery
from svn_client.discovery import DiscoveryError, discover_working_copies


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wc(self, relative="."):
        path = self.root / relative
        (path / ".svn").mkdir(parents=True)
        return path

    def link(self, target, link):
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")

    def paths(self, result):
        return {Path(item.path) for item in result.working_copies}

    def test_empty_parent_is_complete(self):
        result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertFalse(result.truncated)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.working_copies, ())
        self.assertEqual(result.directories_scanned, 1)
        self.assertEqual(result.entries_scanned, 0)

    def test_siblings_and_nested_candidates_have_independent_paths(self):
        first = self.wc("first")
        second = self.wc("second")
        nested = self.wc("first/vendor/nested")
        deeper = self.wc("first/vendor/nested/another")
        result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(self.paths(result), {first, second, nested, deeper})
        values = {Path(item.path): item for item in result.working_copies}
        self.assertEqual(values[first].relation, "root")
        self.assertIsNone(values[first].containing_root)
        self.assertEqual(values[nested].relation, "nested")
        self.assertEqual(values[nested].containing_root, str(first))
        self.assertEqual(values[deeper].containing_root, str(nested))
        self.assertTrue(all(not item.verified for item in values.values()))
        self.assertNotIn("external", {item.relation for item in values.values()})

    def test_parent_working_copy_and_nested_root(self):
        parent = self.wc()
        child = self.wc("dependencies/pkg")
        result = discover_working_copies(parent)
        self.assertEqual(self.paths(result), {parent, child})
        self.assertEqual(next(item for item in result.working_copies if item.path == str(child)).containing_root, str(parent))

    def test_does_not_search_ancestors_outside_selected_scope(self):
        self.wc()
        child = self.root / "subdir"
        child.mkdir()
        result = discover_working_copies(child)
        self.assertTrue(result.complete)
        self.assertEqual(result.working_copies, ())

    def test_svn_marker_is_candidate_not_validation(self):
        path = self.wc("empty-marker")
        result = discover_working_copies(self.root)
        self.assertEqual(self.paths(result), {path})
        self.assertFalse(result.working_copies[0].verified)

    def test_never_reads_administrative_contents_or_user_files(self):
        wc = self.wc("actual")
        forbidden = wc / ".svn"
        (forbidden / "fake-wc" / ".svn").mkdir(parents=True)
        (self.root / "private.txt").write_text("private content")
        real_scandir = os.scandir
        forbidden_id = (forbidden.stat().st_dev, forbidden.stat().st_ino)

        def checked_scandir(target):
            info = os.fstat(target) if isinstance(target, int) else os.lstat(target)
            self.assertNotEqual((info.st_dev, info.st_ino), forbidden_id)
            return real_scandir(target)

        with patch("builtins.open", side_effect=AssertionError("No file reads")), \
                patch.object(Path, "open", side_effect=AssertionError("No file reads")), \
                patch.object(discovery.os, "scandir", side_effect=checked_scandir):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(self.paths(result), {wc})
        self.assertEqual(result.directories_scanned, 2)

    def test_regular_file_svn_marker_is_not_a_candidate(self):
        (self.root / ".svn").write_text("not a working copy")
        self.assertEqual(discover_working_copies(self.root).working_copies, ())

    def test_linked_marker_is_excluded_without_following_it(self):
        target = self.root / "metadata"
        target.mkdir()
        self.link(target, self.root / ".svn")
        result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(result.working_copies, ())
        self.assertEqual(result.skipped_links, 1)

    def test_symlinks_outside_scope_and_loops_are_skipped(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / ".svn").mkdir()
            self.link(outside, self.root / "escape")
            self.link(self.root, self.root / "loop")
            real = self.wc("real")
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(self.paths(result), {real})
        self.assertEqual(result.skipped_links, 2)

    def test_symlink_selected_parent_or_ancestor_is_refused(self):
        actual = self.wc("actual")
        (actual / "child").mkdir()
        alias = self.root / "alias"
        self.link(actual, alias)
        for parent in (alias, alias / "child"):
            with self.subTest(parent=parent), self.assertRaises(DiscoveryError):
                discover_working_copies(parent)

    def test_windows_reparse_marker_and_directory_detection(self):
        directory = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(discovery._unsafe(directory))
        directory.st_file_attributes = 0
        self.assertFalse(discovery._unsafe(directory))
        original = os.lstat

        def with_reparse(path):
            info = original(path)
            if Path(path) == self.root:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(discovery.os, "lstat", side_effect=with_reparse):
            with self.assertRaises(DiscoveryError):
                discover_working_copies(self.root)

    def test_administrative_parent_or_descendant_refused(self):
        self.wc()
        (self.root / ".svn" / "private").mkdir()
        for path in (self.root / ".svn", self.root / ".svn" / "private"):
            with self.subTest(path=path), self.assertRaises(DiscoveryError):
                discover_working_copies(path)

    def test_invalid_or_missing_parent(self):
        ordinary = self.root / "file"
        ordinary.write_text("x")
        for path in ("", "  ", "bad\0path", b"bytes", self.root / "missing", ordinary):
            with self.subTest(path=path), self.assertRaises(DiscoveryError):
                discover_working_copies(path)

    def test_bounds_validated_before_scanning(self):
        options = ({"max_depth": -1}, {"max_depth": True}, {"max_depth": 1.5},
                   {"max_directories": 0}, {"max_entries": 0}, {"max_working_copies": 0},
                   {"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")},
                   {"timeout": float("inf")}, {"timeout": "1"}, {"timeout": True}, {"timeout": 10 ** 400},
                   {"cancelled": False})
        with patch.object(discovery.os, "scandir") as scan:
            for kwargs in options:
                with self.subTest(kwargs=kwargs), self.assertRaises(DiscoveryError):
                    discover_working_copies(self.root, **kwargs)
            scan.assert_not_called()

    def test_depth_limit_scans_boundary_marker_but_no_descendants(self):
        near = self.wc("near")
        self.wc("near/deeper")
        result = discover_working_copies(self.root, max_depth=1)
        self.assertEqual(self.paths(result), {near})
        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)
        self.assertIn("depth_limit", result.reasons)
        self.assertEqual(result.directories_scanned, 2)

    def test_depth_zero_can_find_selected_root(self):
        self.wc()
        result = discover_working_copies(self.root, max_depth=0)
        self.assertTrue(result.complete)
        self.assertEqual(self.paths(result), {self.root})
        self.wc("child")
        result = discover_working_copies(self.root, max_depth=0)
        self.assertEqual(self.paths(result), {self.root})
        self.assertIn("depth_limit", result.reasons)

    def test_directory_count_bounds_scan_and_pending_queue(self):
        for name in ("a", "b", "c", "d"):
            self.wc(name)
        result = discover_working_copies(self.root, max_directories=2)
        self.assertEqual(result.directories_scanned, 2)
        self.assertEqual(len(result.working_copies), 1)
        self.assertIn("directory_limit", result.reasons)
        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)

    def test_entry_count_bounds_even_large_file_only_directory(self):
        for index in range(10):
            (self.root / f"file{index}").touch()
        real_scandir = os.scandir
        consumed = []

        class Counted:
            def __enter__(inner):
                inner.entries = real_scandir(self.root)
                return inner

            def __exit__(inner, *args):
                inner.entries.close()

            def __next__(inner):
                item = next(inner.entries)
                consumed.append(item)
                return item

        with patch.object(discovery.os, "scandir", return_value=Counted()):
            result = discover_working_copies(self.root, max_entries=3)
        self.assertEqual(result.entries_scanned, 3)
        self.assertEqual(len(consumed), 3)
        self.assertIn("entry_limit", result.reasons)
        self.assertFalse(result.complete)

    def test_working_copy_limit_and_exact_limit(self):
        self.wc("a")
        self.wc("b")
        result = discover_working_copies(self.root, max_working_copies=1)
        self.assertEqual(len(result.working_copies), 1)
        self.assertIn("working_copy_limit", result.reasons)
        result = discover_working_copies(self.root, max_working_copies=2)
        self.assertEqual(len(result.working_copies), 2)
        self.assertTrue(result.complete)

    def test_breadth_first_finds_sibling_before_deep_candidate(self):
        self.wc("a")
        self.wc("a/b")
        self.wc("z")
        result = discover_working_copies(self.root, max_working_copies=2)
        self.assertEqual(self.paths(result), {self.root / "a", self.root / "z"})
        self.assertIn("working_copy_limit", result.reasons)

    def test_pre_cancel_does_not_touch_filesystem(self):
        with patch.object(discovery.os, "lstat", side_effect=AssertionError("No access")):
            result = discover_working_copies(self.root, cancelled=lambda: True)
        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)
        self.assertEqual(result.reasons, ("cancelled",))
        self.assertEqual(result.directories_scanned, 0)

    def test_cancelled_during_scan_returns_partial_result(self):
        self.wc()
        self.wc("child")
        should_stop = False
        original = os.scandir

        def cancel_at_scan(target):
            nonlocal should_stop
            should_stop = True
            return original(target)

        with patch.object(discovery.os, "scandir", side_effect=cancel_at_scan):
            result = discover_working_copies(self.root, cancelled=lambda: should_stop)
        self.assertEqual(self.paths(result), {self.root})
        self.assertEqual(result.entries_scanned, 0)
        self.assertEqual(result.reasons, ("cancelled",))

    def test_elapsed_timeout_returns_explicit_partial_result(self):
        times = iter((0.0, 0.0, 2.0))
        with patch.object(discovery.time, "monotonic", side_effect=lambda: next(times, 2.0)):
            result = discover_working_copies(self.root, timeout=1)
        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)
        self.assertEqual(result.reasons, ("time_limit",))
        self.assertEqual(result.directories_scanned, 0)
        self.assertEqual(result.elapsed_seconds, 2.0)

    def test_inaccessible_directory_is_explicit_not_silent_success(self):
        self.wc()
        with patch.object(discovery.os, "scandir", side_effect=PermissionError("private")):
            result = discover_working_copies(self.root)
        self.assertFalse(result.complete)
        self.assertFalse(result.truncated)
        self.assertIn("directory_unavailable", result.reasons)
        self.assertEqual(len(result.issues), 1)
        self.assertNotIn("private", result.issues[0].message)

    def test_portable_path_scanner_without_descriptor_support(self):
        self.wc("a")
        self.wc("a/nested")
        with patch.object(discovery, "_CAN_OPEN_NOFOLLOW", False):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.working_copies), 2)

    def test_replaced_child_is_skipped_and_never_followed(self):
        victim = self.wc("victim")
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other)
            (outside / ".svn").mkdir()
            original = discovery._directory

            def replace_before_open(root, relative, *args):
                if str(relative) == "victim":
                    victim.rename(self.root / "old")
                    self.link(outside, victim)
                return original(root, relative, *args)

            with patch.object(discovery, "_directory", side_effect=replace_before_open):
                result = discover_working_copies(self.root)
        self.assertEqual(result.working_copies, ())
        self.assertFalse(result.complete)
        self.assertTrue(set(result.reasons) & {"directory_changed", "directory_unavailable"})

    def test_zero_inode_fallback_does_not_merge_unrelated_paths(self):
        info = SimpleNamespace(st_dev=1, st_ino=0)
        self.assertNotEqual(discovery._identity(info, self.root / "a"), discovery._identity(info, self.root / "b"))

    def _transformed_entries(self, transform):
        """Expose real entries with controlled directory metadata for tests."""
        original = os.scandir

        class Entries:
            def __init__(inner, target):
                inner.target = target
                inner.entries = original(target)

            def __enter__(inner):
                return inner

            def __exit__(inner, *args):
                inner.entries.close()

            def __next__(inner):
                entry = next(inner.entries)

                def transformed_stat(**kwargs):
                    # Start with complete IDs on every host. Windows' real
                    # DirEntry.stat() would otherwise erase fixture identities.
                    info = (os.stat(entry.name, dir_fd=inner.target, **kwargs)
                            if isinstance(inner.target, int) else os.stat(Path(inner.target) / entry.name, **kwargs))
                    return transform(entry, info)

                return SimpleNamespace(name=entry.name, is_symlink=entry.is_symlink,
                                       is_dir=entry.is_dir, stat=transformed_stat)

        return patch.object(discovery.os, "scandir", side_effect=Entries)

    def test_cross_device_directory_is_not_traversed(self):
        self.wc("mount")

        def different_device(entry, info):
            if entry.name == "mount":
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev + 1, st_ino=info.st_ino)
            return info

        with self._transformed_entries(different_device):
            result = discover_working_copies(self.root)
        self.assertFalse(result.complete)
        self.assertEqual(result.reasons, ("mount_boundary",))
        self.assertEqual(result.skipped_mounts, 1)
        self.assertEqual(result.working_copies, ())
        self.assertEqual(result.directories_scanned, 1)

    def test_incomplete_direntry_ids_are_refreshed_before_boundary_and_identity_checks(self):
        first = self.wc("first")
        second = self.wc("second")
        nested = self.wc("first/nested")

        def incomplete_ids(entry, info):
            return SimpleNamespace(st_mode=info.st_mode, st_dev=0, st_ino=0)

        # Exercise the portable path traversal even on a POSIX test host.
        with patch.object(discovery, "_CAN_OPEN_NOFOLLOW", False), self._transformed_entries(incomplete_ids):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(self.paths(result), {first, second, nested})
        self.assertEqual(result.skipped_mounts, 0)
        self.assertEqual(result.skipped_duplicates, 0)
        self.assertEqual(result.directories_scanned, 4)

    def test_incomplete_direntry_refresh_still_rejects_reparse_points(self):
        child = self.wc("child")
        original = os.lstat

        def incomplete_ids(entry, info):
            return SimpleNamespace(st_mode=info.st_mode, st_dev=0, st_ino=0)

        def changed_metadata(path):
            info = original(path)
            if Path(path) == child:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(discovery, "_CAN_OPEN_NOFOLLOW", False), self._transformed_entries(incomplete_ids), \
                patch.object(discovery.os, "lstat", side_effect=changed_metadata):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(result.working_copies, ())
        self.assertEqual(result.skipped_links, 1)
        self.assertEqual(result.directories_scanned, 1)

    def test_incomplete_direntry_refresh_failure_is_reported(self):
        child = self.wc("child")
        original = os.lstat

        def incomplete_ids(entry, info):
            return SimpleNamespace(st_mode=info.st_mode, st_dev=0, st_ino=0)

        def unavailable(path):
            if Path(path) == child:
                raise PermissionError("private detail")
            return original(path)

        with patch.object(discovery, "_CAN_OPEN_NOFOLLOW", False), self._transformed_entries(incomplete_ids), \
                patch.object(discovery.os, "lstat", side_effect=unavailable):
            result = discover_working_copies(self.root)
        self.assertFalse(result.complete)
        self.assertEqual(result.working_copies, ())
        self.assertEqual(result.reasons, ("entry_unavailable",))
        self.assertNotIn("private detail", result.issues[0].message)

    def test_incomplete_direntry_refresh_keeps_real_mount_boundary(self):
        child = self.wc("mount")
        original = os.lstat

        def incomplete_ids(entry, info):
            return SimpleNamespace(st_mode=info.st_mode, st_dev=0, st_ino=0)

        def other_device(path):
            info = original(path)
            if Path(path) == child:
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev + 1, st_ino=info.st_ino)
            return info

        with patch.object(discovery, "_CAN_OPEN_NOFOLLOW", False), self._transformed_entries(incomplete_ids), \
                patch.object(discovery.os, "lstat", side_effect=other_device):
            result = discover_working_copies(self.root)
        self.assertFalse(result.complete)
        self.assertEqual(result.reasons, ("mount_boundary",))
        self.assertEqual(result.skipped_mounts, 1)
        self.assertEqual(result.working_copies, ())

    def test_duplicate_directory_identity_is_never_queued_twice(self):
        self.wc("alias")
        root_info = self.root.stat()

        def same_identity(entry, info):
            return root_info if entry.name == "alias" else info

        with self._transformed_entries(same_identity):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(result.skipped_duplicates, 1)
        self.assertEqual(result.directories_scanned, 1)

    def test_junction_entry_is_skipped_even_when_reported_as_directory(self):
        self.wc("junction")

        def reparse(entry, info):
            if entry.name == "junction":
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with self._transformed_entries(reparse):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(result.skipped_links, 1)
        self.assertEqual(result.working_copies, ())
        self.assertEqual(result.directories_scanned, 1)

    def test_error_details_are_bounded_without_hiding_incompleteness(self):
        for index in range(105):
            (self.root / f"folder{index}").mkdir()
        original = os.scandir
        root_id = self.root.stat().st_ino

        def root_only(target):
            info = os.fstat(target) if isinstance(target, int) else os.lstat(target)
            if info.st_ino != root_id:
                raise PermissionError("denied")
            return original(target)

        with patch.object(discovery.os, "scandir", side_effect=root_only):
            result = discover_working_copies(self.root)
        self.assertFalse(result.complete)
        self.assertTrue(result.issues_truncated)
        self.assertEqual(len(result.issues), 100)
        self.assertEqual(result.reasons, ("directory_unavailable",))

    @unittest.skipUnless(discovery._CAN_OPEN_NOFOLLOW, "Descriptor traversal unavailable")
    def test_all_descriptor_opens_are_read_only_directories(self):
        self.wc("one/nested")
        original = os.open

        def readonly_directory(path, flags, *args, **kwargs):
            self.assertTrue(flags & os.O_DIRECTORY)
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC))
            return original(path, flags, *args, **kwargs)

        with patch.object(discovery.os, "open", side_effect=readonly_directory):
            result = discover_working_copies(self.root)
        self.assertTrue(result.complete)

    def test_candidates_are_immutable_and_serializable(self):
        self.wc()
        result = discover_working_copies(self.root)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.working_copies[0].verified = True
        data = dataclasses.asdict(result)
        self.assertEqual(data["working_copies"][0]["path"], str(self.root))


if __name__ == "__main__":
    unittest.main()
