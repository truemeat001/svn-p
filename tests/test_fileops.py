"""File-operation safety tests. Fakes and disposable temp repositories only."""
from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

from svn_client.backend import CommandResult, StatusEntry, SvnClient, SvnError
from svn_client.cli import main
from svn_client.fileops import FileOperationError, FileOperationManager, MAX_DELETE_PATHS, _snapshot_bytes


class FakeClient:
    def __init__(self, root):
        self.working_copy = root
        self._lock = threading.RLock()
        self.statuses = {".": StatusEntry(".", "normal", revision=7)}
        self.props = {}
        self.bases = {}
        self.overrides = {}
        self.calls = []
        self.empty_property_targets = False
        self.mutate_successfully = True
        self.fail_command = False

    def add(self, path, contents=b"original\n", *, item="normal", props=None):
        target = self.working_copy / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        for parent in Path(path).parents:
            self.statuses[str(parent)] = StatusEntry(str(parent), "normal", revision=7)
        self.statuses[path] = StatusEntry(path, item, revision=7)
        self.props[path] = props or {}
        self.bases[path] = contents

    def info(self, path=None):
        path = path or "."
        info = {"path": path, "kind": "dir" if (self.working_copy / path).is_dir() else "file",
                "revision": 7, "url": "file:///repo/trunk" + ("/" + quote(Path(path).as_posix(), safe="/") if path != "." else ""),
                "wc_root": str(self.working_copy), "repository_uuid": "uuid-1", "schedule": "normal"}
        info.update(self.overrides.get(path, {}))
        return info

    @staticmethod
    def target(value):
        return value[:-1] if "@" in value and value.endswith("@") else value

    def _xml(self, args):
        self.calls.append(tuple(args))
        path = self.target(args[-1])
        if args[0] == "status":
            root = ET.Element("status")
            if path in self.statuses:
                row = self.statuses[path]
                parent = ET.SubElement(root, "changelist", name=row.changelist) if row.changelist else ET.SubElement(root, "target", path=path)
                entry = ET.SubElement(parent, "entry", path=path)
                fields = {"item": row.item, "props": row.props, "revision": str(row.revision or "")}
                for name in ("tree_conflicted", "copied", "switched", "wc_locked", "file_external"):
                    if getattr(row, name):
                        fields[name.replace("_", "-")] = "true"
                for name in ("moved_from", "moved_to"):
                    if getattr(row, name):
                        fields[name.replace("_", "-")] = getattr(row, name)
                ET.SubElement(entry, "wc-status", fields)
            return root
        if args[0] == "proplist":
            root = ET.Element("properties")
            if self.empty_property_targets and not self.props.get(path):
                return root
            target = ET.SubElement(root, "target", path=path)
            for name, value in self.props.get(path, {}).items():
                ET.SubElement(target, "property", name=name).text = value
            return root
        raise AssertionError(args)

    @staticmethod
    def _command_size(args):
        return sum(len(arg) for arg in args) + 100

    @property
    def mutations(self):
        return [args for args in self.calls if args[0] in {"delete", "move", "copy"}]

    def command(self, args):
        self.calls.append(tuple(args))
        if args[0] == "export":
            Path(args[-1]).write_bytes(self.bases[self.target(args[-2])])
        elif args[0] in {"delete", "move", "copy"}:
            if self.fail_command:
                raise SvnError("Timeout: outcome is uncertain")
            if self.mutate_successfully:
                if args[0] == "delete":
                    for value in args[2:]:
                        path = self.target(value)
                        (self.working_copy / path).unlink()
                        self.statuses[path] = replace(self.statuses[path], item="deleted")
                else:
                    source = self.target(args[-2])
                    destination = self.target(args[-1]) if args[0] == "copy" else args[-1]
                    shutil.copyfile(self.working_copy / source, self.working_copy / destination)
                    self.props[destination] = dict(self.props[source])
                    self.statuses[destination] = StatusEntry(destination, "added", copied=True)
                    if args[0] == "move":
                        (self.working_copy / source).unlink()
                        self.statuses[source] = replace(self.statuses[source], item="deleted", moved_to=destination)
        else:
            raise AssertionError(args)
        return CommandResult(tuple(args), 0, "done\n", "")


class FileOperationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.client = FakeClient(self.root)
        self.client.add("a.txt")
        self.manager = FileOperationManager(self.client)

    def test_delete_preflight_is_exact_deduplicated_and_read_only(self):
        self.client.add("b.txt")
        plan = self.manager.preflight_delete(["b.txt", "a.txt", "a.txt"])
        self.assertEqual(plan.paths, ("a.txt", "b.txt"))
        self.assertEqual(plan.affected_paths, plan.paths)
        self.assertTrue(plan.commit_required)
        self.assertFalse(plan.server_checked)
        self.assertEqual(plan.scope, "regular_files_only")
        self.assertEqual(len(plan.state_sha256), 64)
        self.assertTrue((self.root / "a.txt").exists())
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(len([c for c in self.client.calls if c[0] == "export"]), 2)

    def test_confirmation_is_required_before_any_inspection(self):
        for callback, args in ((self.manager.delete, (["a.txt"],)), (self.manager.move, ("a.txt", "b.txt")),
                               (self.manager.copy, ("a.txt", "b.txt"))):
            with self.subTest(callback=callback.__name__), self.assertRaises(FileOperationError):
                callback(*args)
        self.assertEqual(self.client.calls, [])

    def test_clean_delete_uses_no_force_and_exact_targets(self):
        self.client.add("--flag@name.txt")
        plan = self.manager.preflight_delete(["--flag@name.txt", "a.txt"])
        self.manager.delete(plan.paths, confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.client.mutations, [("delete", "--", "--flag@name.txt@", "a.txt")])
        self.assertFalse((self.root / "a.txt").exists())

    def test_delete_refuses_content_or_property_modifications(self):
        for fields in ({"item": "modified"}, {"props": "modified"}):
            with self.subTest(fields=fields):
                self.client.statuses["a.txt"] = replace(StatusEntry("a.txt", "normal"), **fields)
                with self.assertRaises(FileOperationError):
                    self.manager.preflight_delete(["a.txt"])
        self.assertEqual(self.client.mutations, [])

    def test_delete_checks_bytes_even_when_status_reports_clean(self):
        previous = (self.root / "a.txt").stat()
        (self.root / "a.txt").write_bytes(b"different")
        os.utime(self.root / "a.txt", ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.assertRaisesRegex(FileOperationError, "BASE"):
            self.manager.preflight_delete(["a.txt"])
        self.assertEqual(self.client.mutations, [])

    def test_hashing_does_not_follow_endlessly_appended_file(self):
        original = os.fdopen
        sizes = []
        class GrowingReader:
            def __init__(self, descriptor, mode):
                self.handle = original(descriptor, mode)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def fileno(self):
                return self.handle.fileno()
            def read(self, size):
                sizes.append(size)
                return b"x" * size
        with patch("svn_client.fileops.os.fdopen", GrowingReader), self.assertRaisesRegex(FileOperationError, "grew"):
            _snapshot_bytes(self.root / "a.txt")
        self.assertEqual(sum(sizes), len(b"original\n") + 1)

    def test_entire_delete_batch_preflight_precedes_mutation(self):
        self.client.add("b.txt", item="modified")
        with self.assertRaises(FileOperationError):
            self.manager.delete(["a.txt", "b.txt"], confirmed=True)
        self.assertTrue((self.root / "a.txt").exists())
        self.assertEqual(self.client.mutations, [])

    def test_no_properties_target_is_supported(self):
        self.client.empty_property_targets = True
        self.manager.preflight_delete(["a.txt"])

    def test_unexpected_property_target_refused(self):
        original = self.client._xml
        def wrong(args):
            root = original(args)
            if args[0] == "proplist":
                root.find("target").set("path", "other.txt")
            return root
        self.client._xml = wrong
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("a.txt", "b.txt")

    def test_empty_oversize_and_implicit_selections_refused(self):
        for paths in ([], None, "a.txt", Path("a.txt"), ["a.txt"] * (MAX_DELETE_PATHS + 1)):
            with self.subTest(paths=str(paths)[:40]), self.assertRaises(FileOperationError):
                self.manager.preflight_delete(paths)
        self.assertEqual(self.client.calls, [])

    def test_paths_cannot_escape_or_target_admin_root_urls_or_nonportable_names(self):
        for source in ("../outside", ".", "file:///tmp/a", "https://example.test/a", ".svn/entries", ".SVN/a",
                       "a\x00b", "a\nb", "C:\\outside", "a.txt:stream", "NUL", "con.txt", "folder./a"):
            with self.subTest(source=source), self.assertRaises(FileOperationError):
                self.manager.preflight_copy(source, "b.txt")
        self.assertEqual(self.client.calls, [])

    def test_absolute_in_root_source_supported(self):
        self.assertEqual(self.manager.preflight_copy(self.root / "a.txt", self.root / "b.txt").paths, ("a.txt",))

    def test_symlink_source_destination_and_ancestor_refused(self):
        try:
            (self.root / "link").symlink_to(self.root / "a.txt")
            (self.root / "dirlink").symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        for source, destination in (("link", "b.txt"), ("a.txt", "link"), ("dirlink/a.txt", "b.txt"), ("a.txt", "dirlink/b.txt")):
            with self.subTest(source=source, destination=destination), self.assertRaises(FileOperationError):
                self.manager.preflight_copy(source, destination)

    def test_nested_working_copy_refused(self):
        self.client.add("nested/a.txt")
        (self.root / "nested/.svn").mkdir()
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("nested/a.txt", "b.txt")

    def test_directory_source_refused_without_recursion(self):
        (self.root / "folder").mkdir()
        with self.assertRaises(FileOperationError):
            self.manager.preflight_delete(["folder"])
        self.assertEqual(self.client.mutations, [])

    def test_special_file_source_refused(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        os.mkfifo(self.root / "fifo")
        self.client.statuses["fifo"] = StatusEntry("fifo", "normal")
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("fifo", "copy")

    def test_root_must_be_actual_working_copy_root(self):
        self.client.overrides["."] = {"wc_root": str(self.root.parent)}
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("a.txt", "b.txt")

    def test_cross_root_uuid_mapping_and_schedule_refused(self):
        for fields in ({"wc_root": str(self.root / "other")}, {"repository_uuid": "uuid-2"},
                       {"url": "file:///repo/branch/a.txt"}, {"schedule": "add"}, {"schedule": "delete"}):
            with self.subTest(fields=fields):
                self.client.overrides["a.txt"] = fields
                with self.assertRaises(FileOperationError):
                    self.manager.preflight_copy("a.txt", "b.txt")

    def test_unsafe_source_status_flags_refused(self):
        cases = [{name: True} for name in ("tree_conflicted", "copied", "switched", "file_external", "wc_locked")]
        cases += [{"item": value} for value in ("conflicted", "added", "deleted", "replaced", "missing", "unversioned", "ignored", "obstructed")]
        cases += [{"props": "conflicted"}, {"moved_from": "old.txt"}, {"moved_to": "new.txt"}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.client.statuses["a.txt"] = replace(StatusEntry("a.txt", "normal"), **fields)
                with self.assertRaises(FileOperationError):
                    self.manager.preflight_copy("a.txt", "b.txt")

    def test_unsafe_ancestor_refused(self):
        self.client.add("folder/a.txt")
        self.client.statuses["folder"] = StatusEntry("folder", "normal", tree_conflicted=True)
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("folder/a.txt", "b.txt")
        self.client.statuses["folder"] = StatusEntry("folder", "normal")
        self.client.overrides["folder"] = {"url": "file:///repo/switched"}
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("folder/a.txt", "b.txt")

    def test_destination_must_not_exist_or_be_existing_directory(self):
        self.client.add("b.txt")
        (self.root / "folder").mkdir()
        for destination in ("a.txt", "b.txt", "folder", "missing/b.txt", "../b.txt"):
            with self.subTest(destination=destination), self.assertRaises(FileOperationError):
                self.manager.preflight_move("a.txt", destination)

    def test_pending_deleted_destination_refused_even_when_file_absent(self):
        self.client.statuses["b.txt"] = StatusEntry("b.txt", "deleted")
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("a.txt", "b.txt")

    def test_case_only_and_case_collision_refused(self):
        self.client.add("B.txt")
        for destination in ("A.txt", "b.txt"):
            with self.subTest(destination=destination), self.assertRaises(FileOperationError):
                self.manager.preflight_move("a.txt", destination)

    def test_move_and_copy_keep_modified_content_and_properties(self):
        self.client.statuses["a.txt"] = StatusEntry("a.txt", "modified", props="modified", changelist="fix")
        (self.root / "a.txt").write_bytes(b"edited locally")
        self.client.props["a.txt"] = {"custom:note": "edited"}
        plan = self.manager.preflight_copy("a.txt", "b.txt")
        self.assertEqual(plan.files[0].status.changelist, "fix")
        self.manager.copy("a.txt", "b.txt", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual((self.root / "b.txt").read_bytes(), b"edited locally")
        self.assertEqual(self.client.props["b.txt"], {"custom:note": "edited"})
        self.manager.move("a.txt", "c.txt", confirmed=True)
        self.assertEqual((self.root / "c.txt").read_bytes(), b"edited locally")
        self.assertFalse((self.root / "a.txt").exists())

    def test_literal_at_sign_and_leading_dash_arguments(self):
        self.client.add("-中文@source")
        self.manager.copy("-中文@source", "-copy@dest", confirmed=True)
        self.manager.move("-中文@source", "-move@dest", confirmed=True)
        self.assertEqual(self.client.mutations, [("copy", "--", "-中文@source@", "-copy@dest@"),
                                               ("move", "--", "-中文@source@", "-move@dest")])
        self.assertTrue((self.root / "-move@dest").exists())
        self.assertFalse((self.root / "-move@dest@").exists())

    def test_stale_state_rejects_same_length_bytes_even_with_same_mtime(self):
        plan = self.manager.preflight_move("a.txt", "b.txt")
        previous = (self.root / "a.txt").stat()
        (self.root / "a.txt").write_bytes(b"changed!\n")
        os.utime(self.root / "a.txt", ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.assertRaisesRegex(FileOperationError, "changed after review"):
            self.manager.move("a.txt", "b.txt", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.client.mutations, [])

    def test_stale_properties_and_changelist_rejected(self):
        plan = self.manager.preflight_copy("a.txt", "b.txt")
        self.client.props["a.txt"] = {"custom:note": "new"}
        with self.assertRaises(FileOperationError):
            self.manager.copy("a.txt", "b.txt", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.client.props["a.txt"] = {}
        self.client.statuses["a.txt"] = replace(self.client.statuses["a.txt"], changelist="other")
        with self.assertRaises(FileOperationError):
            self.manager.copy("a.txt", "b.txt", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.client.mutations, [])

    def test_invalid_state_hash_refused_before_inspection(self):
        for digest in ("x", "A" * 64, 1):
            with self.subTest(digest=digest), self.assertRaises(FileOperationError):
                self.manager.delete(["a.txt"], confirmed=True, expected_state_sha256=digest)
        self.assertEqual(self.client.calls, [])

    def test_destination_created_after_review_refused(self):
        plan = self.manager.preflight_copy("a.txt", "b.txt")
        (self.root / "b.txt").write_text("do not overwrite")
        with self.assertRaises(FileOperationError):
            self.manager.copy("a.txt", "b.txt", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual((self.root / "b.txt").read_text(), "do not overwrite")
        self.assertEqual(self.client.mutations, [])

    def test_command_length_refused_without_batching(self):
        self.client._command_size = lambda args: 24_001
        with self.assertRaises(FileOperationError):
            self.manager.preflight_copy("a.txt", "b.txt")
        self.assertEqual(self.client.mutations, [])

    def test_no_automatic_retry_after_uncertain_mutation(self):
        self.client.fail_command = True
        with self.assertRaises(SvnError):
            self.manager.copy("a.txt", "b.txt", confirmed=True)
        self.assertEqual(len(self.client.mutations), 1)

    def test_success_without_exact_postconditions_is_uncertain(self):
        self.client.mutate_successfully = False
        for callback, args in ((self.manager.delete, (["a.txt"],)), (self.manager.move, ("a.txt", "b.txt")),
                               (self.manager.copy, ("a.txt", "b.txt"))):
            with self.subTest(callback=callback.__name__), self.assertRaises(SvnError):
                callback(*args, confirmed=True)
        self.assertEqual(len(self.client.mutations), 3)

    def test_postcommand_validation_error_is_uncertain_svn_error(self):
        with patch.object(self.manager, "_verify_result", side_effect=FileOperationError("path was replaced")):
            with self.assertRaisesRegex(SvnError, "outcome is uncertain"):
                self.manager.copy("a.txt", "b.txt", confirmed=True)
        self.assertTrue((self.root / "b.txt").exists())
        self.assertEqual(len(self.client.mutations), 1)

    def test_copy_postcondition_detects_changed_payload(self):
        command = self.client.command
        def corrupted(args):
            result = command(args)
            if args[0] == "copy":
                (self.root / "b.txt").write_bytes(b"corruption")
            return result
        self.client.command = corrupted
        with self.assertRaisesRegex(SvnError, "content/properties"):
            self.manager.copy("a.txt", "b.txt", confirmed=True)
        self.assertEqual(len(self.client.mutations), 1)

    def test_status_is_exact_nonrecursive_and_ignores_externals(self):
        self.manager.preflight_copy("a.txt", "b.txt")
        for args in self.client.calls:
            if args[0] == "status":
                self.assertIn("--ignore-externals", args)
                self.assertEqual(args[args.index("--depth") + 1], "empty")
                self.assertEqual(args[-2], "--")


class FileOperationCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.client = FakeClient(self.root)
        self.client.add("a.txt")
        factory = patch("svn_client.cli.SvnClient", return_value=self.client)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    def invoke(self, *args, stdin=None):
        output = io.StringIO()
        result = main(["--workspace", str(self.root), "file", *args], stdout=output, stdin=stdin or io.StringIO())
        self.assertEqual(output.getvalue().count("\n"), 1)
        return result, json.loads(output.getvalue())

    def test_confirmation_required_before_dependency_and_read(self):
        result, payload = self.invoke("delete", "--path", "a.txt")
        self.assertEqual(result, 3)
        self.assertEqual(payload["error"]["code"], "confirmation_required")
        self.factory.assert_not_called()

    def test_leaf_help_includes_review_hash(self):
        result, payload = self.invoke("move", "--help")
        self.assertEqual(result, 0)
        self.assertIn("--expect-state-sha256", payload["data"]["help"])

    def test_delete_dry_run_then_confirmed_exact_hash(self):
        result, payload = self.invoke("delete", "--path", "a.txt", "--dry-run")
        self.assertEqual(result, 0, payload)
        self.assertEqual(payload["outcome"], "planned")
        self.assertTrue(payload["data"]["working_copy_unchanged"])
        self.assertEqual(self.client.mutations, [])
        result, payload = self.invoke("delete", "--path", "a.txt", "--yes", "--expect-state-sha256",
                                      payload["data"]["preflight"]["state_sha256"])
        self.assertEqual(result, 0, payload)
        self.assertEqual(payload["command"], "file.delete")
        self.assertEqual(payload["outcome"], "completed")
        self.assertFalse((self.root / "a.txt").exists())

    def test_copy_and_move_cli(self):
        for action, destination in (("copy", "b.txt"), ("move", "c.txt")):
            result, payload = self.invoke(action, "--source", "a.txt", "--destination", destination, "--yes")
            self.assertEqual(result, 0, payload)
            self.assertTrue((self.root / destination).exists())
            self.assertTrue(payload["data"]["preflight"]["commit_required"])

    def test_json_delete_selection(self):
        result, payload = self.invoke("delete", "--paths-json", "-", "--dry-run", stdin=io.StringIO('["a.txt"]'))
        self.assertEqual(result, 0, payload)
        self.assertEqual(payload["data"]["preflight"]["paths"], ["a.txt"])

    def test_invalid_or_changed_state_hash_fails_before_mutation(self):
        for digest in ("x", "0" * 64):
            result, payload = self.invoke("move", "--source", "a.txt", "--destination", "b.txt",
                                          "--yes", "--expect-state-sha256", digest)
            self.assertEqual(result, 4, payload)
        self.assertEqual(self.client.mutations, [])

    def test_uncertain_backend_failure_has_no_retry(self):
        self.client.fail_command = True
        result, payload = self.invoke("move", "--source", "a.txt", "--destination", "b.txt", "--yes")
        self.assertEqual(result, 8)
        self.assertEqual(payload["outcome"], "uncertain")
        self.assertFalse(payload["error"]["details"]["automatic_retry_safe"])
        self.assertEqual(len(self.client.mutations), 1)

    def test_existing_destination_is_refused_without_mutation(self):
        self.client.add("b.txt")
        result, payload = self.invoke("copy", "--source", "a.txt", "--destination", "b.txt", "--yes")
        self.assertEqual(result, 4, payload)
        self.assertEqual(self.client.mutations, [])

    def test_missing_explicit_arguments_refused(self):
        for args in (("delete", "--yes"), ("move", "--source", "a.txt", "--yes"), ("copy", "--yes")):
            with self.subTest(args=args):
                result, _ = self.invoke(*args)
                self.assertEqual(result, 2)
        self.factory.assert_not_called()


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "SVN binaries unavailable: real SVN unverified")
class FileOperationIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.wc = self.root / "wc"
        subprocess.run(["svnadmin", "create", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["svn", "checkout", self.repo.as_uri(), str(self.wc), "--non-interactive"], check=True, capture_output=True)
        for name in ("a.txt", "source@name.txt"):
            (self.wc / name).write_bytes(b"original\n")
        self.svn("add", "--", "a.txt", "source@name.txt@")
        self.svn("commit", "--message", "fixture")
        self.manager = FileOperationManager(SvnClient(self.wc))

    def svn(self, *args):
        return subprocess.run(["svn", args[0], "--non-interactive", "--no-auth-cache", *args[1:]], cwd=self.wc,
                              check=True, capture_output=True, text=True)

    def test_clean_delete_is_pending_only(self):
        plan = self.manager.preflight_delete(["a.txt"])
        self.manager.delete(["a.txt"], confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertFalse((self.wc / "a.txt").exists())
        self.assertIn("deleted", self.svn("status", "--xml").stdout)
        self.assertIn("a.txt", self.svn("list", self.repo.as_uri()).stdout)

    def test_copy_and_move_literal_at_sign_preserve_history(self):
        self.manager.copy("source@name.txt", "copy@name.txt", confirmed=True)
        self.manager.move("source@name.txt", "moved@name.txt", confirmed=True)
        self.assertEqual((self.wc / "copy@name.txt").read_bytes(), b"original\n")
        self.assertEqual((self.wc / "moved@name.txt").read_bytes(), b"original\n")
        self.assertFalse((self.wc / "moved@name.txt@").exists())
        for name in ("copy@name.txt@", "moved@name.txt@"):
            self.assertIn("copy-from-url", self.svn("info", "--xml", "--", name).stdout)

    def test_modified_content_and_properties_copy(self):
        (self.wc / "a.txt").write_bytes(b"edited\n")
        self.svn("propset", "custom:note", "local", "a.txt")
        self.manager.copy("a.txt", "b.txt", confirmed=True)
        self.assertEqual((self.wc / "b.txt").read_bytes(), b"edited\n")
        self.assertEqual(self.svn("propget", "custom:note", "b.txt").stdout.strip(), "local")
        with self.assertRaises(FileOperationError):
            self.manager.delete(["a.txt"], confirmed=True)

    def test_translated_base_and_stale_review(self):
        self.svn("propset", "svn:eol-style", "CRLF", "a.txt")
        self.svn("commit", "--message", "translation")
        self.svn("update")
        # Force SVN to materialize the configured working representation.
        (self.wc / "a.txt").unlink()
        self.svn("revert", "a.txt")
        self.manager.preflight_delete(["a.txt"])
        plan = self.manager.preflight_move("a.txt", "b.txt")
        (self.wc / "a.txt").write_bytes(b"edited\r\n")
        with self.assertRaises(FileOperationError):
            self.manager.move("a.txt", "b.txt", confirmed=True, expected_state_sha256=plan.state_sha256)


if __name__ == "__main__":
    unittest.main()
