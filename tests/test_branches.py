from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

from svn_client.backend import CommandResult, SvnClient, SvnError
from svn_client.branches import (BranchError, BranchManager, BranchOperationError,
                                 validate_branch_revision, validate_branch_url)


class FakeBranchRepository:
    """Transport fixture only; it does not establish real SVN compatibility."""
    def __init__(self, root):
        self.root = root
        self.base = "https://example.test/repo"
        self.url = self.base + "/trunk"
        self.source = self.base + "/branches/feature"
        self.uuid = "repo-uuid"
        self.revision = 8
        self.head = 12
        self.remote_uuid = self.uuid
        self.depth = "infinity"
        self.status_changes = {}
        self.info_changes = {}
        self.calls = []
        self.mutations = []
        self.preview = "U    file.txt\n"
        self.output = "U    file.txt\n"
        self.properties = {}
        self.names = []
        self.remote_entries = [("file.txt", "file", 9)]
        self.mode = "normal"
        self.fail_postread = False
        self.during_preview = None
        self.other_node = None
        self.other_nodes = []
        self.base_files = {"file.txt": b"original\n"}
        self.base_directories = {"."}
        (root / ".svn").mkdir()
        (root / "file.txt").write_bytes(self.base_files["file.txt"])

    def info(self, target, recursive):
        root = ET.Element("info")
        remote = "://" in target
        if remote:
            url, rev = target.rsplit("@", 1)
            items = [("remote", "dir", self.head if rev == "HEAD" else int(rev), url)]
        else:
            items = [(".", "dir", self.revision, self.url)]
            if recursive:
                items.append(("file.txt", "file", self.revision, self.url + "/file.txt"))
                if self.other_node:
                    items.append(self.other_node)
                items.extend(self.other_nodes)
        for name, kind, revision, url in items:
            override = self.info_changes.get(name, {}) if not remote else {}
            entry = ET.SubElement(root, "entry", path=name, kind=kind, revision=str(override.get("revision", revision)))
            ET.SubElement(entry, "url").text = override.get("url", url)
            repo = ET.SubElement(entry, "repository")
            ET.SubElement(repo, "root").text = self.base
            ET.SubElement(repo, "uuid").text = self.remote_uuid if remote else self.uuid
            if not remote:
                wc = ET.SubElement(entry, "wc-info")
                ET.SubElement(wc, "wcroot-abspath").text = override.get("wc_root", str(self.root))
                ET.SubElement(wc, "schedule").text = override.get("schedule", "normal")
                ET.SubElement(wc, "depth").text = override.get("depth", self.depth) if kind == "dir" else ""
            ET.SubElement(entry, "commit", revision=str(revision))
        return ET.tostring(root, encoding="unicode")

    def status(self):
        root = ET.Element("status")
        target = ET.SubElement(root, "target", path=".")
        names = [".", "file.txt"] + ([self.other_node[0]] if self.other_node else [])
        names.extend(item[0] for item in self.other_nodes)
        for name in names:
            entry = ET.SubElement(target, "entry", path=name)
            attrs = {"item": "normal", "props": "none", "revision": str(self.revision)}
            attrs.update(self.status_changes.get(name, {}))
            ET.SubElement(entry, "wc-status", **attrs)
        return ET.tostring(root, encoding="unicode")

    def command(self, args, **kwargs):
        self.calls.append(list(args))
        command = args[0]
        if self.fail_postread and self.mutations and command in {"info", "status", "proplist"}:
            raise SvnError("svn: E170001 https://user:SECRET@host/token failed")
        if command == "info":
            return CommandResult(tuple(args), 0, self.info(args[-1], "--depth" in args), "")
        if command == "status":
            return CommandResult(tuple(args), 0, self.status(), "")
        if command == "proplist":
            root = ET.Element("properties")
            revision = args[args.index("--revision") + 1] if "--revision" in args else None
            if revision is not None and revision != "BASE":
                revision = int(revision)
            values = self.properties.get((args[-1], revision), self.properties.get(args[-1], {}))
            if values:
                target = ET.SubElement(root, "target", path=args[-1])
                for key, value in values.items():
                    ET.SubElement(target, "property", name=key).text = value
            return CommandResult(tuple(args), 0, ET.tostring(root, encoding="unicode"), "")
        if command == "export":
            destination = Path(args[-1])
            for directory in sorted(self.base_directories):
                (destination / directory).mkdir(parents=True, exist_ok=True)
            for name, content in self.base_files.items():
                (destination / name).parent.mkdir(parents=True, exist_ok=True)
                (destination / name).write_bytes(content)
            return CommandResult(tuple(args), 0, "", "")
        if command == "list":
            root = ET.Element("lists")
            node = ET.SubElement(root, "list", path=args[-1].rsplit("@", 1)[0])
            entries = self.remote_entries if "--recursive" in args else [(name, "dir", None) for name in self.names]
            for name, kind, size in entries:
                item = ET.SubElement(node, "entry", kind=kind)
                ET.SubElement(item, "name").text = name
                if size is not None:
                    ET.SubElement(item, "size").text = str(size)
            return CommandResult(tuple(args), 0, ET.tostring(root, encoding="unicode"), "")
        if command == "merge" and "--dry-run" in args:
            if self.during_preview:
                self.during_preview()
            return CommandResult(tuple(args), 0, self.preview, "")
        if command not in {"switch", "merge"}:
            raise AssertionError(args)
        self.mutations.append(list(args))
        if command == "switch":
            if self.mode != "zero_noop":
                self.url, revision = args[-2].rsplit("@", 1)
                self.revision = int(revision)
        else:
            (self.root / "file.txt").write_text("merged\n")
            self.status_changes["file.txt"] = {"item": "conflicted" if self.mode == "conflict" else "modified"}
            self.status_changes["."] = {"props": "modified"}
        if self.mode == "failure":
            raise SvnError("svn: E155015 https://user:SECRET@host/token failed after a partial write")
        return CommandResult(tuple(args), 0, self.output, "")


class BranchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = FakeBranchRepository(self.root)
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            self.client = SvnClient(self.root)
        mocked = patch.object(self.client, "command", side_effect=self.repo.command)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.manager = BranchManager(self.client)

    def switch_plan(self):
        return self.manager.preflight_switch(self.repo.source, revision=10)

    def merge_plan(self):
        return self.manager.preflight_merge(self.repo.source, start_revision=8, end_revision=10)

    def test_public_validators_are_pure_and_match_plan_normalization(self):
        self.assertEqual(validate_branch_revision("00010"), 10)
        self.assertEqual(validate_branch_url("https://example.test/repo/中文@feature/"),
                         "https://example.test/repo/" + quote("中文@feature", safe=""))
        for value in ("https://user:SECRET@host/repo", "svn+ssh://host/repo", "--force"):
            with self.subTest(value=value), self.assertRaises(BranchError) as caught:
                validate_branch_url(value)
            self.assertNotIn("SECRET", str(caught.exception))
        for value in ("HEAD", "BASE", "1:2", True, -1):
            with self.subTest(value=value), self.assertRaises(BranchError):
                validate_branch_revision(value)
        self.assertEqual(self.repo.calls, [])

    def test_switch_preflight_is_read_only_not_a_dry_run(self):
        plan = self.switch_plan()
        self.assertFalse(plan.svn_dry_run)
        self.assertEqual(plan.operation, "switch")
        self.assertEqual(plan.working_revision, 8)
        self.assertEqual(len(plan.state_sha256), 64)
        self.assertEqual(self.repo.mutations, [])
        self.assertEqual((self.root / "file.txt").read_text(), "original\n")
        self.assertTrue(any("no dry-run" in warning for warning in plan.warnings))

    def test_merge_preflight_uses_real_pinned_dry_run_arguments(self):
        plan = self.merge_plan()
        self.assertTrue(plan.svn_dry_run)
        self.assertEqual(plan.preview_text, self.repo.preview)
        self.assertEqual(self.repo.mutations, [])
        args = next(args for args in self.repo.calls if args[0] == "merge")
        self.assertEqual(args, ["merge", "--revision", "8:10", "--depth", "infinity", "--accept", "postpone", "--dry-run", "--", self.repo.source + "@10", "."])
        self.assertNotIn("--ignore-externals", args)

    def test_switch_confirmed_state_bound_and_exact_root(self):
        plan = self.switch_plan()
        result = self.manager.switch(self.repo.source, revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(result.outcome, "switched")
        self.assertFalse(result.commit_performed)
        self.assertEqual(result.url, self.repo.source)
        self.assertEqual(self.repo.mutations, [["switch", "--revision", "10", "--depth", "infinity", "--ignore-externals", "--accept", "postpone", "--", self.repo.source + "@10", "."]])

    def test_merge_remains_local_and_requires_review(self):
        plan = self.merge_plan()
        result = self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(result.outcome, "needs_review")
        self.assertFalse(result.commit_performed)
        self.assertEqual({item.path for item in result.changes}, {".", "file.txt"})
        self.assertEqual(len(self.repo.mutations), 1)
        for forbidden in ("--force", "--ignore-ancestry", "--allow-mixed-revisions", "--record-only", "--reintegrate"):
            self.assertNotIn(forbidden, self.repo.mutations[0])
        self.assertFalse(any(call[0] in {"commit", "revert", "cleanup", "copy"} for call in self.repo.calls))

    def test_confirmation_and_review_hash_required_before_reads(self):
        for kwargs in ({}, {"confirmed": True}, {"confirmed": True, "expected_state_sha256": "invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BranchError):
                self.manager.switch(self.repo.source, revision=10, **kwargs)
        self.assertEqual(self.repo.calls, [])

    def test_change_of_operation_revision_or_content_invalidates_review(self):
        plan = self.switch_plan()
        with self.assertRaisesRegex(BranchError, "state changed"):
            self.manager.switch(self.repo.source, revision=11, confirmed=True, expected_state_sha256=plan.state_sha256)
        (self.root / "file.txt").write_text("different\n")
        with self.assertRaisesRegex(BranchError, "translated BASE"):
            self.manager.switch(self.repo.source, revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.repo.mutations, [])

    def test_edit_during_preview_refused(self):
        self.repo.during_preview = lambda: (self.root / "file.txt").write_text("edited during read\n")
        with self.assertRaisesRegex(BranchError, "translated BASE"):
            self.merge_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_same_size_restored_mtime_edit_refused_before_either_preview(self):
        path = self.root / "file.txt"
        before = path.stat()
        path.write_bytes(b"modified\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertTrue(all(item.item == "normal" for item in self.client.status(verbose=True)))
        for preflight in (self.switch_plan, self.merge_plan):
            with self.subTest(operation=preflight.__name__), self.assertRaisesRegex(BranchError, "translated BASE"):
                preflight()
        self.assertFalse(any(call[0] in {"switch", "merge"} for call in self.repo.calls))
        self.assertEqual(path.read_bytes(), b"modified\n")

    def test_hidden_edit_after_review_blocks_switch_and_merge_apply(self):
        path = self.root / "file.txt"
        for operation in ("switch", "merge"):
            path.write_bytes(b"original\n")
            plan = self.switch_plan() if operation == "switch" else self.merge_plan()
            before = path.stat()
            path.write_bytes(b"modified\n")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.subTest(operation=operation), self.assertRaisesRegex(BranchError, "translated BASE"):
                if operation == "switch":
                    self.manager.switch(self.repo.source, revision=10, confirmed=True,
                                        expected_state_sha256=plan.state_sha256)
                else:
                    self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True,
                                       expected_state_sha256=plan.state_sha256)
            self.assertEqual(path.read_bytes(), b"modified\n")
        self.assertEqual(self.repo.mutations, [])

    def test_hidden_edit_during_final_execution_export_blocks_apply(self):
        path = self.root / "file.txt"
        original = self.repo.command
        for operation in ("switch", "merge"):
            path.write_bytes(b"original\n")
            plan = self.switch_plan() if operation == "switch" else self.merge_plan()
            exports = []
            def edit_after_final_export(args, **kwargs):
                result = original(args, **kwargs)
                if args[0] == "export":
                    exports.append(Path(args[-1]))
                    if len(exports) == 2:
                        before = path.stat()
                        path.write_bytes(b"modified\n")
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result
            with self.subTest(operation=operation), patch.object(self.client, "command", side_effect=edit_after_final_export):
                with self.assertRaisesRegex(BranchError, "changed during BASE verification"):
                    if operation == "switch":
                        self.manager.switch(self.repo.source, revision=10, confirmed=True,
                                            expected_state_sha256=plan.state_sha256)
                    else:
                        self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True,
                                           expected_state_sha256=plan.state_sha256)
            self.assertEqual(len(exports), 2)
            self.assertTrue(all(not destination.parent.exists() for destination in exports))
            self.assertEqual(path.read_bytes(), b"modified\n")
        self.assertEqual(self.repo.mutations, [])

    def test_property_change_during_final_execution_export_blocks_apply(self):
        original = self.repo.command
        self.repo.properties[(".", "BASE")] = {}
        for operation in ("switch", "merge"):
            self.repo.properties["."] = {}
            plan = self.switch_plan() if operation == "switch" else self.merge_plan()
            exports = []
            def edit_after_final_export(args, **kwargs):
                result = original(args, **kwargs)
                if args[0] == "export":
                    exports.append(args[-1])
                    if len(exports) == 2:
                        self.repo.properties["."] = {"custom:local": "keep this edit"}
                return result
            with self.subTest(operation=operation), patch.object(self.client, "command", side_effect=edit_after_final_export):
                with self.assertRaisesRegex(BranchError, "changed during BASE verification"):
                    if operation == "switch":
                        self.manager.switch(self.repo.source, revision=10, confirmed=True,
                                            expected_state_sha256=plan.state_sha256)
                    else:
                        self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True,
                                           expected_state_sha256=plan.state_sha256)
            self.assertEqual(len(exports), 2)
            self.assertEqual(self.repo.properties["."], {"custom:local": "keep this edit"})
        self.assertEqual(self.repo.mutations, [])

    def test_metadata_or_status_change_during_export_refused(self):
        original = self.repo.command
        for change in ("metadata", "status"):
            self.repo.info_changes.clear()
            self.repo.status_changes.clear()
            def change_after_export(args, **kwargs):
                result = original(args, **kwargs)
                if args[0] == "export":
                    if change == "metadata":
                        self.repo.info_changes["file.txt"] = {"revision": 7}
                    else:
                        self.repo.status_changes["."] = {"props": "modified"}
                return result
            with self.subTest(change=change), patch.object(self.client, "command", side_effect=change_after_export):
                with self.assertRaisesRegex(BranchError, "changed during BASE verification"):
                    self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_nested_binary_and_empty_directory_compared_with_base(self):
        binary = "nested/中文@name.bin"
        content = bytes(range(256)) + b"\x00\xff\r\n"
        self.repo.other_nodes = [(name, kind, 8, self.repo.url + "/" + name) for name, kind in
                                 (("nested", "dir"), (binary, "file"), ("empty", "dir"))]
        self.repo.base_directories.update({"nested", "empty"})
        self.repo.base_files[binary] = content
        (self.root / "nested").mkdir()
        (self.root / "empty").mkdir()
        path = self.root / binary
        path.write_bytes(content)
        self.switch_plan()
        self.merge_plan()
        before = path.stat()
        path.write_bytes(content[:-1] + b"x")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        for preflight in (self.switch_plan, self.merge_plan):
            with self.subTest(operation=preflight.__name__), self.assertRaisesRegex(BranchError, "translated BASE"):
                preflight()
        self.assertEqual(self.repo.mutations, [])

    def test_base_verification_uses_isolated_translated_export_and_removes_it(self):
        # The fixture provides already translated bytes, not repository-normal
        # content. Real EOL/keyword behavior is covered by integration below.
        content = b"$Id: file.txt 8 2026-11-03 00:00:00Z author $\r\n"
        (self.root / "file.txt").write_bytes(content)
        self.repo.base_files["file.txt"] = content
        self.switch_plan()
        exports = [call for call in self.repo.calls if call[0] == "export"]
        self.assertEqual(len(exports), 2)
        for command in exports:
            self.assertEqual(command[:-1], ["export", "--quiet", "--ignore-externals", "--revision", "BASE", "--", "."])
            destination = Path(command[-1])
            self.assertFalse(destination.is_relative_to(self.root))
            self.assertFalse(destination.parent.exists())
        self.assertFalse(any(call[0] in {"cat", "diff", "revert", "cleanup"} for call in self.repo.calls))
        self.assertEqual((self.root / "file.txt").read_bytes(), content)

    def test_base_export_failure_is_closed_and_cleans_temporary_files(self):
        original = self.repo.command
        destinations = []
        def fail_export(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "export":
                destinations.append(Path(args[-1]))
                raise SvnError("svn: E155010 https://user:SECRET@host/base could not be exported")
            return result
        with patch.object(self.client, "command", side_effect=fail_export):
            with self.assertRaisesRegex(BranchError, "BASE") as caught:
                self.switch_plan()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIn("E155010", str(caught.exception))
        self.assertEqual(len(destinations), 1)
        self.assertFalse(destinations[0].parent.exists())
        self.assertEqual(self.repo.mutations, [])
        self.assertEqual((self.root / "file.txt").read_bytes(), b"original\n")

    def test_base_export_must_have_exact_files_directories_and_kinds(self):
        cases = (({}, {"."}), ({"file.txt": b"original\n", "extra": b""}, {"."}),
                 ({"file.txt": b"original\n"}, {".", "extra"}), ({}, {".", "file.txt"}))
        for files, directories in cases:
            self.repo.base_files, self.repo.base_directories = files, directories
            with self.subTest(files=files, directories=directories), self.assertRaisesRegex(BranchError, "BASE"):
                self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_base_export_entry_limit_is_enforced(self):
        self.repo.base_files["unexpected"] = b""
        with patch("svn_client.branches.MAX_TREE_ENTRIES", 2):
            with self.assertRaisesRegex(BranchError, "BASE exceeds"):
                self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_base_export_link_is_refused_without_reading_its_target(self):
        original = self.repo.command
        destinations = []
        def link_export(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "export":
                path = Path(args[-1]) / "file.txt"
                path.unlink()
                try:
                    path.symlink_to(self.root / "file.txt")
                except (OSError, NotImplementedError):
                    self.skipTest("No symlink support")
                destinations.append(path.parent)
            return result
        with patch.object(self.client, "command", side_effect=link_export):
            with self.assertRaisesRegex(BranchError, "unsupported symbolic link"):
                self.switch_plan()
        self.assertEqual(len(destinations), 1)
        self.assertFalse(destinations[0].parent.exists())
        self.assertEqual(self.repo.mutations, [])

    def test_temp_directory_inside_working_copy_is_refused_before_export(self):
        nested = self.root / "temporary"
        nested.mkdir()
        for directory in (self.root, nested):
            with self.subTest(directory=directory), patch("svn_client.branches.tempfile.gettempdir", return_value=str(directory)):
                with self.assertRaisesRegex(BranchError, "outside this working copy"):
                    self.switch_plan()
        self.assertFalse(any(call[0] == "export" for call in self.repo.calls))
        self.assertEqual(list(nested.iterdir()), [])

    def test_clean_properties_are_preserved_and_hidden_changes_refused(self):
        self.repo.properties["."] = {"svn:mergeinfo": "/branches/other:2-5", "custom": "unchanged"}
        self.switch_plan()
        self.repo.properties[(".", "BASE")] = {"svn:mergeinfo": "/branches/other:2-5", "custom": "different"}
        for preflight in (self.switch_plan, self.merge_plan):
            with self.subTest(operation=preflight.__name__), self.assertRaisesRegex(BranchError, "properties differ from BASE"):
                preflight()
        self.assertEqual(self.repo.mutations, [])

    def test_forbidden_base_properties_are_rejected_before_export(self):
        for name in ("svn:externals", "svn:special"):
            self.repo.properties[(".", "BASE")] = {name: "unsafe"}
            with self.subTest(name=name), self.assertRaisesRegex(BranchError, "unsupported"):
                self.switch_plan()
        self.assertFalse(any(call[0] == "export" for call in self.repo.calls))

    def test_dirty_and_unsupported_statuses_refused(self):
        cases = [{"item": value} for value in ("modified", "added", "deleted", "missing", "obstructed", "incomplete", "unversioned", "ignored", "external", "conflicted")]
        cases += [{key: "true"} for key in ("tree-conflicted", "copied", "switched", "wc-locked", "file-external")]
        cases += [{"props": "modified"}, {"props": "conflicted"}, {"moved-from": "other"}, {"moved-to": "other"}]
        for change in cases:
            self.repo.status_changes["file.txt"] = change
            with self.subTest(change=change), self.assertRaises(BranchError):
                self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_mixed_revisions_sparse_switched_or_foreign_root_refused(self):
        for change in ({"revision": 7}, {"url": self.repo.source + "/file.txt"}, {"wc_root": str(self.root / "nested")}, {"schedule": "add"}):
            self.repo.info_changes["file.txt"] = change
            with self.subTest(change=change), self.assertRaises(BranchError):
                self.switch_plan()
        self.repo.info_changes.clear()
        self.repo.depth = "empty"
        with self.assertRaisesRegex(BranchError, "sparse"):
            self.switch_plan()

    def test_status_revision_must_be_uniform(self):
        self.repo.status_changes["file.txt"] = {"revision": "7"}
        with self.assertRaisesRegex(BranchError, "Mixed-revision"):
            self.switch_plan()

    def test_local_and_remote_externals_and_specials_rejected(self):
        for target in (".", self.repo.source + "@10"):
            for name in ("svn:externals", "svn:special"):
                self.repo.properties = {target: {name: "definition"}}
                with self.subTest(target=target, name=name), self.assertRaisesRegex(BranchError, "unsupported"):
                    self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_known_oversize_incoming_tree_rejected_before_mutation(self):
        self.repo.remote_entries = [("huge.bin", "file", 256 * 1024 * 1024 + 1)]
        with self.assertRaisesRegex(BranchError, "incoming tree exceeds"):
            self.switch_plan()
        self.repo.remote_entries = [(f"file-{i}", "file", 0) for i in range(10_000)]
        with self.assertRaisesRegex(BranchError, "incoming tree exceeds"):
            self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_unsafe_incoming_paths_and_missing_sizes_rejected(self):
        for name, kind, size in (("../outside", "file", 2), ("/outside", "file", 2),
                                 ("bad\\name", "file", 2), (".svn/metadata", "file", 2),
                                 ("bad\nname", "file", 2), ("file", "unknown", 2),
                                 ("file", "file", None), ("file", "file", -1)):
            self.repo.remote_entries = [(name, kind, size)]
            with self.subTest(name=name, kind=kind, size=size), self.assertRaises(BranchError):
                self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_incoming_parent_kinds_and_duplicate_paths_must_be_consistent(self):
        cases = [
            [("dir/file", "file", 1)],
            [("dir", "file", 1), ("dir/file", "file", 1)],
            [("file", "file", 1), ("file", "file", 1)],
        ]
        for entries in cases:
            self.repo.remote_entries = entries
            with self.subTest(entries=entries), self.assertRaises(BranchError):
                self.switch_plan()
        self.repo.remote_entries = [("dir", "dir", None), ("dir/file", "file", 1)]
        self.switch_plan()
        self.assertEqual(self.repo.mutations, [])

    def test_different_repository_and_self_operation_refused(self):
        self.repo.remote_uuid = "different-repository"
        with self.assertRaisesRegex(BranchError, "same repository"):
            self.switch_plan()
        self.repo.remote_uuid = self.repo.uuid
        with self.assertRaisesRegex(BranchError, "different branch"):
            self.manager.preflight_switch(self.repo.url, revision=10)

    def test_urls_and_revisions_validated_without_network_or_credential_echo(self):
        for value in ("svn+ssh://user@host/repo", "https://user:SECRET@host/repo", "https://host/repo?arg=1", "https://host/repo#fragment", "https://host/a/../b", "https://host/a/%2E%2E/b", "https://host/a%2Fb", "https://host/a%5Cb", "https://host/a%00b", "https://host/a%ZZb", "https://host:invalid/repo", "^/branches/feature", "--force", " https://host/repo"):
            with self.subTest(value=value), self.assertRaises(BranchError) as caught:
                self.manager.preflight_switch(value, revision=10)
            self.assertNotIn("SECRET", str(caught.exception))
        for revision in (True, -1, "HEAD", "BASE", "1:10", "10 ", 2**63, 1.5, None):
            with self.subTest(revision=revision), self.assertRaises(BranchError):
                self.manager.preflight_switch(self.repo.source, revision=revision)
        self.assertEqual(self.repo.calls, [])

    def test_literal_at_and_unicode_url_use_one_explicit_peg(self):
        value = self.repo.base + "/branches/中文@feature"
        canonical = self.repo.base + "/branches/" + quote("中文@feature", safe="")
        plan = self.manager.preflight_switch(value, revision=10)
        self.assertEqual(plan.source_url, canonical)
        self.assertTrue(any(call[-1] == canonical + "@10" for call in self.repo.calls))

    def test_reverse_or_empty_merge_ranges_rejected(self):
        for start, end in ((10, 10), (11, 10), ("HEAD", 10), (-1, 10), (0, 65)):
            with self.subTest(start=start), self.assertRaises(BranchError):
                self.manager.preflight_merge(self.repo.source, start_revision=start, end_revision=end)
        self.assertEqual(self.repo.calls, [])

    def test_merge_scans_intermediate_states_with_one_end_peg(self):
        self.merge_plan()
        calls = [call for call in self.repo.calls if call[0] in {"list", "proplist"} and "://" in call[-1]]
        self.assertEqual({int(call[call.index("--revision") + 1]) for call in calls}, {8, 9, 10})
        self.assertTrue(all(call[-1] == self.repo.source + "@10" for call in calls))

    def test_mergeinfo_hole_cannot_introduce_intermediate_forbidden_property(self):
        for name in ("svn:externals", "svn:special"):
            self.repo.properties = {
                ".": {"svn:mergeinfo": "/branches/feature:10"},
                (self.repo.source + "@10", 9): {name: "unsafe intermediate state"},
            }
            with self.subTest(name=name), self.assertRaisesRegex(BranchError, "unsupported"):
                self.merge_plan()
        self.assertFalse(any(call[0] == "merge" for call in self.repo.calls))
        self.assertEqual(self.repo.mutations, [])

    def test_unexpected_postmerge_external_property_is_uncertain(self):
        plan = self.merge_plan()
        original = self.repo.command
        def command(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "merge" and "--dry-run" not in args:
                self.repo.properties["."] = {"svn:externals": "unexpected"}
            return result
        with patch.object(self.client, "command", side_effect=command):
            with self.assertRaises(BranchOperationError):
                self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(len(self.repo.mutations), 1)
        self.assertEqual((self.root / "file.txt").read_text(), "merged\n")

    def test_predicted_conflicts_are_visible_and_block_apply(self):
        for notification in ("C    file.txt\n", " C   file.txt\n", "   C file.txt\n", "UC   file.txt\n"):
            self.repo.preview = notification
            plan = self.merge_plan()
            self.assertTrue(plan.preview_has_conflicts)
            with self.assertRaisesRegex(BranchError, "predicts conflicts"):
                self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.repo.mutations, [])

    def test_dry_run_change_invalidates_review(self):
        plan = self.merge_plan()
        self.repo.preview = "U    other.txt\n"
        with self.assertRaisesRegex(BranchError, "state changed"):
            self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.repo.mutations, [])

    def test_real_apply_conflict_is_reported_without_resolve_revert_or_commit(self):
        plan = self.merge_plan()
        self.repo.mode = "conflict"
        result = self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(result.outcome, "conflicted")
        self.assertEqual(result.conflicts, ("file.txt",))
        self.assertEqual(len(self.repo.mutations), 1)

    def test_failed_apply_retains_changes_never_retries_and_suppresses_secrets(self):
        plan = self.merge_plan()
        self.repo.mode = "failure"
        with self.assertRaises(BranchOperationError) as caught:
            self.manager.merge(self.repo.source, start_revision=8, end_revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(caught.exception.outcome, "uncertain")
        self.assertFalse(caught.exception.automatic_retry_safe)
        self.assertIn("E155015", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual((self.root / "file.txt").read_text(), "merged\n")
        self.assertEqual(len(self.repo.mutations), 1)

    def test_zero_exit_without_matching_switch_is_uncertain(self):
        plan = self.switch_plan()
        self.repo.mode = "zero_noop"
        with self.assertRaises(BranchOperationError):
            self.manager.switch(self.repo.source, revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(len(self.repo.mutations), 1)

    def test_postread_failure_is_uncertain(self):
        plan = self.switch_plan()
        self.repo.fail_postread = True
        with self.assertRaises(BranchOperationError):
            self.manager.switch(self.repo.source, revision=10, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(len(self.repo.mutations), 1)

    def test_symlink_and_hardlink_files_refused(self):
        source = self.root / "file.txt"
        outside = self.root / "outside.txt"
        outside.write_text("outside\n")
        source.unlink()
        try:
            source.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("No symlink support")
        with self.assertRaisesRegex(BranchError, "Symbolic"):
            self.switch_plan()
        source.unlink()
        os.link(outside, source)
        with self.assertRaisesRegex(BranchError, "Hard-linked"):
            self.switch_plan()

    def test_nested_working_copy_refused(self):
        (self.root / "nested" / ".svn").mkdir(parents=True)
        self.repo.other_node = ("nested", "dir", 8, self.repo.url + "/nested")
        with self.assertRaisesRegex(BranchError, "Nested"):
            self.switch_plan()

    def test_copy_branch_tag_preview_never_executes_or_claims_execution_support(self):
        for kind in ("branch", "tag"):
            plan = self.manager.preflight_copy(self.repo.url, self.repo.base + "/" + kind + "s/new", revision=8, kind=kind, message="Create reviewed snapshot")
            self.assertFalse(plan.execution_supported)
            self.assertTrue(plan.would_commit_immediately)
            self.assertFalse(plan.local_changes_included)
            self.assertEqual(plan.destination_checked_revision, 12)
            self.assertEqual(len(plan.state_sha256), 64)
        self.assertFalse(hasattr(self.manager, "copy"))
        self.assertEqual(self.repo.mutations, [])

    def test_copy_destination_collision_and_invalid_scope_refused(self):
        self.repo.names = ["New"]
        with self.assertRaisesRegex(BranchError, "collision"):
            self.manager.preflight_copy(self.repo.url, self.repo.base + "/branches/new", revision=8, message="branch")
        for target in (self.repo.url, self.repo.url + "/nested", "https://other.test/repo/branches/new", self.repo.base):
            with self.subTest(target=target), self.assertRaises(BranchError):
                self.manager.preflight_copy(self.repo.url, target, revision=8, message="branch")
        self.assertEqual(self.repo.mutations, [])

    def test_copy_message_and_kind_validation(self):
        for message in ("", "  ", "https://u:SECRET@host", "bad\0message", "x" * 8193):
            with self.subTest(message=message[:20]), self.assertRaises(BranchError):
                self.manager.preflight_copy(self.repo.url, self.repo.base + "/branches/new", revision=8, message=message)
        with self.assertRaises(BranchError):
            self.manager.preflight_copy(self.repo.url, self.repo.base + "/branches/new", revision=8, kind="other", message="text")
        self.assertEqual(self.repo.calls, [])

    def test_plan_serialization_and_diagnostics_do_not_expose_passwords(self):
        self.repo.preview = "warning https://user:SECRET@host/repo\nU    file.txt\n"
        plan = self.merge_plan()
        self.assertNotIn("SECRET", json.dumps(asdict(plan)))


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "Real SVN branch tests require svn and svnadmin")
class BranchIntegrationTests(unittest.TestCase):
    """All mutations are confined to a newly created disposable file:// repo."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="svn-branches-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repository = self.root / "repo"
        subprocess.run([shutil.which("svnadmin"), "create", str(repository)], check=True, capture_output=True)
        self.base = repository.as_uri()
        self.run_svn("mkdir", self.base + "/trunk", self.base + "/branches", self.base + "/tags", "-m", "layout")
        self.wc = self.root / "wc"
        self.run_svn("checkout", self.base + "/trunk", str(self.wc))
        (self.wc / "file.txt").write_text("original\n")
        self.run_svn("add", str(self.wc / "file.txt"))
        self.run_svn("commit", str(self.wc), "-m", "seed")
        self.run_svn("copy", self.base + "/trunk", self.base + "/branches/feature", "-m", "branch")
        self.run_svn("update", str(self.wc))
        self.client = SvnClient(self.wc)
        self.manager = BranchManager(self.client)
        self.source = self.base + "/branches/feature"

    def run_svn(self, *args):
        return subprocess.run([shutil.which("svn"), args[0], "--non-interactive", "--no-auth-cache", *args[1:]], check=True, capture_output=True, text=True)

    def test_switch_real_pinned_revision_without_commit(self):
        plan = self.manager.preflight_switch(self.source, revision=3)
        result = self.manager.switch(self.source, revision=3, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(result.outcome, "switched")
        self.assertEqual(self.client.info()["url"], self.source)
        self.assertEqual(self.client.status(), [])

    def test_merge_real_dry_run_then_local_apply(self):
        branch_wc = self.root / "branch-wc"
        self.run_svn("checkout", self.source, str(branch_wc))
        (branch_wc / "file.txt").write_text("branch change\n")
        self.run_svn("commit", str(branch_wc), "-m", "feature change")
        self.run_svn("update", str(self.wc))
        plan = self.manager.preflight_merge(self.source, start_revision=3, end_revision=4)
        self.assertEqual((self.wc / "file.txt").read_text(), "original\n")
        result = self.manager.merge(self.source, start_revision=3, end_revision=4, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(result.outcome, "needs_review")
        self.assertEqual((self.wc / "file.txt").read_text(), "branch change\n")
        self.assertEqual(self.client.info()["revision"], 4)
        # r4 changed the branch, not trunk: path-scoped trunk history still ends
        # at r2. Check repository HEAD to prove the local merge did not commit.
        self.assertEqual(self.run_svn("info", "--show-item", "revision", self.base).stdout.strip(), "4")
        self.assertEqual(self.client.log(limit=1)[0].revision, 2)
        status = {entry.path: entry for entry in self.client.status()}
        self.assertEqual(status["file.txt"].item, "modified")
        self.assertEqual(status["."].props, "modified")
        self.assertEqual(self.run_svn("propget", "--strict", "svn:mergeinfo", str(self.wc)).stdout.strip(),
                         "/branches/feature:4")

    def test_copy_preview_checks_existing_parent_without_server_write(self):
        plan = self.manager.preflight_copy(self.base + "/trunk", self.base + "/tags/v1", revision=2, kind="tag", message="v1")
        self.assertFalse(plan.execution_supported)
        self.assertEqual(plan.destination_checked_revision, 3)
        self.assertEqual(self.client.list_entries(self.base + "/tags"), [])

    def test_dirty_working_copy_rejected(self):
        (self.wc / "file.txt").write_text("keep this edit\n")
        with self.assertRaises(BranchError):
            self.manager.preflight_switch(self.source, revision=3)
        self.assertEqual((self.wc / "file.txt").read_text(), "keep this edit\n")

    def test_same_size_restored_mtime_edit_is_not_clean_for_switch_or_merge(self):
        path = self.wc / "file.txt"
        before = path.stat()
        changed = path.read_bytes().replace(b"original", b"modified", 1)
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        status = {item.path: item for item in self.client.status(verbose=True)}
        # Prove this exercises SVN's actual size/mtime fast path, rather than a
        # fake normal status or the existing modified-status rejection.
        self.assertEqual(status["file.txt"].item, "normal")
        for operation in ("switch", "merge"):
            with self.subTest(operation=operation), self.assertRaisesRegex(BranchError, "translated BASE"):
                if operation == "switch":
                    self.manager.preflight_switch(self.source, revision=3)
                else:
                    self.manager.preflight_merge(self.source, start_revision=2, end_revision=3)
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual(self.client.info()["url"], self.base + "/trunk")
        self.assertEqual(self.run_svn("info", "--show-item", "revision", self.base).stdout.strip(), "3")

    def test_translated_keywords_crlf_binary_and_directory_properties_supported(self):
        (self.wc / "nested").mkdir()
        (self.wc / "empty").mkdir()
        text = self.wc / "nested" / "keywords.txt"
        text.write_bytes(b"$Id$\n$URL$\ntext\n")
        binary = self.wc / "nested" / "binary.bin"
        binary_content = bytes(range(256)) + b"\x00\xff\r\n"
        binary.write_bytes(binary_content)
        self.run_svn("add", str(self.wc / "nested"), str(self.wc / "empty"))
        self.run_svn("propset", "svn:eol-style", "CRLF", str(text))
        self.run_svn("propset", "svn:keywords", "Id URL", str(text))
        self.run_svn("propset", "svn:mime-type", "application/octet-stream", str(binary))
        self.run_svn("propset", "custom:directory", "preserved", str(self.wc / "nested"))
        self.run_svn("commit", str(self.wc), "-m", "translated content")
        translated_branch = self.base + "/branches/translated"
        self.run_svn("copy", self.base + "/trunk", translated_branch, "-m", "translated branch")
        # A fresh checkout guarantees genuine working-format EOL and keyword
        # expansion, independently of propset's handling of the original file.
        wc = self.root / "translated-wc"
        self.run_svn("checkout", self.base + "/trunk", str(wc))
        client = SvnClient(wc)
        manager = BranchManager(client)
        before = (wc / "nested" / "keywords.txt").read_bytes()
        self.assertIn(b"$Id: ", before)
        self.assertIn(b"$URL: ", before)
        self.assertEqual(before.count(b"\n"), before.count(b"\r\n"))
        manager.preflight_merge(self.source, start_revision=3, end_revision=5)
        plan = manager.preflight_switch(translated_branch, revision=5)
        self.assertEqual((wc / "nested" / "keywords.txt").read_bytes(), before)
        result = manager.switch(translated_branch, revision=5, confirmed=True,
                                expected_state_sha256=plan.state_sha256)
        self.assertEqual(result.outcome, "switched")
        self.assertEqual(client.status(), [])
        self.assertIn(translated_branch.encode("ascii"), (wc / "nested" / "keywords.txt").read_bytes())
        self.assertEqual((wc / "nested" / "binary.bin").read_bytes(), binary_content)
        self.assertTrue((wc / "empty").is_dir())
        self.assertEqual(self.run_svn("propget", "--strict", "custom:directory", str(wc / "nested")).stdout, "preserved")

    def test_directory_and_file_property_edits_rejected_without_mutation(self):
        for target in (self.wc, self.wc / "file.txt"):
            self.run_svn("propset", "custom:local", "keep this property", str(target))
            for operation in ("switch", "merge"):
                with self.subTest(target=target, operation=operation), self.assertRaises(BranchError):
                    if operation == "switch":
                        self.manager.preflight_switch(self.source, revision=3)
                    else:
                        self.manager.preflight_merge(self.source, start_revision=2, end_revision=3)
            self.assertEqual(self.run_svn("propget", "--strict", "custom:local", str(target)).stdout, "keep this property")
            self.run_svn("propdel", "custom:local", str(target))
        self.assertEqual(self.client.info()["url"], self.base + "/trunk")

    def test_ignored_file_rejected(self):
        self.run_svn("propset", "svn:ignore", "ignored.bin", str(self.wc))
        self.run_svn("commit", str(self.wc), "-m", "ignore rule")
        self.run_svn("update", str(self.wc))
        (self.wc / "ignored.bin").write_bytes(b"keep")
        with self.assertRaises(BranchError):
            self.manager.preflight_switch(self.source, revision=4)

    def test_external_definition_rejected_without_fetching_it(self):
        self.run_svn("propset", "svn:externals", "^/trunk external", str(self.wc))
        self.run_svn("commit", str(self.wc), "-m", "external definition")
        self.run_svn("update", "--ignore-externals", str(self.wc))
        with self.assertRaises(BranchError):
            self.manager.preflight_switch(self.source, revision=4)
        self.assertFalse((self.wc / "external").exists())


if __name__ == "__main__":
    unittest.main()
