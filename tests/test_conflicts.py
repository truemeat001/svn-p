"""Conflict inspection, confirmation, scope, argv, and JSON safety tests."""
from __future__ import annotations

from dataclasses import replace
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from svn_client.backend import CommandResult, StatusEntry, SvnClient, SvnError
from svn_client.cli import EXIT_CODES, main
from svn_client import conflicts
from svn_client.conflicts import ACCEPT_CHOICES, ConflictError, ConflictManager


class FakeClient:
    def __init__(self, workspace):
        self.working_copy = workspace
        self._lock = threading.RLock()
        self.calls = []
        self.entries = {".": StatusEntry(".", "normal", revision=7),
                        "a.txt": StatusEntry("a.txt", "conflicted", revision=7)}
        self.base_info = {"kind": "file", "revision": 7, "wc_root": str(workspace),
                          "repository_uuid": "test-uuid", "schedule": "normal",
                          "url": "https://example.test/repo/a.txt"}
        self.info_overrides = {}
        self.resolve_error = None
        self.keep_conflict_after_resolve = False
        self.fail_post_status = False

    def info(self, target=None):
        if target in self.info_overrides:
            value = self.info_overrides[target]
            if isinstance(value, Exception):
                raise value
            return value
        return {**self.base_info, "kind": "dir" if target in (None, ".") else "file"}

    def status(self, paths=None, verbose=False):
        if paths is None:
            return list(self.entries.values())
        return [entry for path, entry in self.entries.items()
                if any(path == target or path.startswith(target + os.sep) for target in paths)]

    def command(self, args):
        self.calls.append(list(args))
        if args[0] == "resolve":
            if self.resolve_error:
                raise self.resolve_error
            if not self.keep_conflict_after_resolve:
                for target in args[args.index("--") + 1:]:
                    path = target[:-1] if "@" in target else target
                    entry = self.entries[path]
                    self.entries[path] = replace(entry, item="modified", props="none", tree_conflicted=False)
            return CommandResult(tuple(args), 0, "Resolved conflicted state\n", "")
        if args[0] != "status":
            raise AssertionError(args)
        if self.fail_post_status and any(command[0] == "resolve" for command in self.calls):
            raise SvnError("status unavailable")
        path = args[-1]
        if "@" in path:
            path = path[:-1]
        entry = self.entries.get(path, StatusEntry(path, "normal", revision=7))
        xml = ET.Element("status")
        target = ET.SubElement(xml, "target", path=path)
        node = ET.SubElement(target, "entry", path=entry.path)
        attrs = {"item": entry.item, "props": entry.props,
                 "revision": str(entry.revision) if entry.revision is not None else ""}
        for key, value in (("tree-conflicted", entry.tree_conflicted), ("copied", entry.copied),
                           ("switched", entry.switched), ("wc-locked", entry.wc_locked),
                           ("file-external", entry.file_external)):
            if value:
                attrs[key] = "true"
        if entry.moved_from:
            attrs["moved-from"] = entry.moved_from
        if entry.moved_to:
            attrs["moved-to"] = entry.moved_to
        ET.SubElement(node, "wc-status", attrs)
        return CommandResult(tuple(args), 0, ET.tostring(xml, encoding="unicode"), "")


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "a.txt").write_text("current working contents")
        self.client = FakeClient(self.root)
        self.manager = ConflictManager(self.client)

    def resolutions(self):
        return [args for args in self.client.calls if args[0] == "resolve"]

    def test_list_reports_text_property_and_tree_conflicts_without_resolving(self):
        for name, entry in (("properties", StatusEntry("properties", "normal", props="conflicted")),
                            ("tree", StatusEntry("tree", "normal", tree_conflicted=True))):
            (self.root / name).touch()
            self.client.entries[name] = entry
        result = self.manager.list()
        self.assertEqual({item.path for item in result}, {"a.txt", "properties", "tree"})
        self.assertEqual(self.resolutions(), [])

    def test_inspect_exact_text_file_is_read_only(self):
        result = self.manager.inspect("a.txt")
        self.assertTrue(result.text_conflicted)
        self.assertFalse(result.property_conflicted)
        self.assertFalse(result.tree_conflicted)
        self.assertEqual(result.allowed_accepts, ACCEPT_CHOICES)
        self.assertEqual(result.blocked_reasons, ())
        self.assertEqual(self.resolutions(), [])
        self.assertTrue(all("--depth" in args and args[args.index("--depth") + 1] == "empty"
                            for args in self.client.calls))

    def test_clean_target_cannot_be_resolved(self):
        self.client.entries["a.txt"] = StatusEntry("a.txt", "normal")
        self.assertIn("not_conflicted", self.manager.inspect("a.txt").blocked_reasons)
        with self.assertRaises(ConflictError):
            self.manager.preflight_resolve(["a.txt"], "working")

    def test_property_and_mixed_conflicts_require_external_resolution(self):
        for item in ("normal", "conflicted"):
            with self.subTest(item=item):
                self.client.entries["a.txt"] = StatusEntry("a.txt", item, props="conflicted")
                self.assertEqual(self.manager.inspect("a.txt").allowed_accepts, ())
                for choice in ("working", "base", "mine-full", "theirs-full"):
                    with self.assertRaises(ConflictError):
                        self.manager.preflight_resolve(["a.txt"], choice)

    def test_directory_property_conflict_cannot_be_resolved(self):
        (self.root / "folder").mkdir()
        self.client.entries["folder"] = StatusEntry("folder", "normal", props="conflicted")
        self.client.info_overrides["folder"] = {**self.client.base_info, "kind": "dir"}
        with self.assertRaises(ConflictError):
            self.manager.resolve(["folder"], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_tree_conflict_is_inspectable_but_all_resolutions_refused(self):
        self.client.entries["a.txt"] = replace(self.client.entries["a.txt"], tree_conflicted=True)
        self.assertTrue(self.manager.inspect("a.txt").tree_conflicted)
        for choice in ACCEPT_CHOICES:
            with self.subTest(choice=choice), self.assertRaises(ConflictError):
                self.manager.resolve(["a.txt"], choice, confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_tree_conflicted_ancestor_blocks_child_resolution(self):
        self.client.entries["."] = StatusEntry(".", "normal", tree_conflicted=True)
        self.assertIn("unsafe_ancestor", self.manager.inspect("a.txt").blocked_reasons)
        with self.assertRaises(ConflictError):
            self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_all_paths_preflight_before_any_resolution(self):
        (self.root / "bad").touch()
        self.client.entries["bad"] = StatusEntry("bad", "normal", tree_conflicted=True)
        with self.assertRaises(ConflictError):
            self.manager.resolve(["a.txt", "bad"], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_unsupported_node_states_fail_closed(self):
        cases = ({"file_external": True}, {"copied": True}, {"switched": True},
                 {"moved_from": "old"}, {"moved_to": "new"}, {"wc_locked": True},
                 {"item": "missing", "props": "conflicted"}, {"item": "replaced", "props": "conflicted"})
        base = self.client.entries["a.txt"]
        for changes in cases:
            with self.subTest(changes=changes):
                self.client.entries["a.txt"] = replace(base, **changes)
                with self.assertRaises(ConflictError):
                    self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_other_working_copy_or_repository_is_refused(self):
        cases = ({"wc_root": str(self.root / "nested")}, {"wc_root": ""},
                 {"repository_uuid": "different"}, {"repository_uuid": ""},
                 {"schedule": "add"})
        for change in cases:
            with self.subTest(change=change):
                self.client.info_overrides["a.txt"] = {**self.client.base_info, **change}
                with self.assertRaises(ConflictError):
                    self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_workspace_subdirectory_requires_reopening_actual_root(self):
        self.client.base_info["wc_root"] = str(self.root.parent)
        self.assertIn("select_working_copy_root", self.manager.inspect("a.txt").blocked_reasons)
        with self.assertRaises(ConflictError):
            self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [])

    def test_unavailable_info_remains_inspectable_but_cannot_resolve(self):
        self.client.info_overrides["a.txt"] = SvnError("missing")
        inspection = self.manager.inspect("a.txt")
        self.assertIn("metadata_unavailable", inspection.blocked_reasons)
        self.assertEqual(inspection.allowed_accepts, ())

    def test_missing_conflict_victim_cannot_be_marked_resolved(self):
        (self.root / "a.txt").unlink()
        self.assertIn("filesystem_node_unavailable", self.manager.inspect("a.txt").blocked_reasons)
        with self.assertRaises(ConflictError):
            self.manager.resolve(["a.txt"], "working", confirmed=True)

    def test_explicit_boolean_confirmation_required_before_any_svn_calls(self):
        for confirmation in (False, 1, "yes", None):
            with self.subTest(confirmation=confirmation), self.assertRaises(ConflictError):
                self.manager.resolve(["a.txt"], "working", confirmed=confirmation)
        self.assertEqual(self.client.calls, [])

    def test_automatic_interactive_or_unknown_choices_rejected(self):
        for choice in ("recommended", "postpone", "edit", "launch", "mine-conflict", "theirs-conflict",
                       "base", "mine-full", "theirs-full", "", "WORKING"):
            with self.subTest(choice=choice), self.assertRaises(ConflictError):
                self.manager.resolve(["a.txt"], choice, confirmed=True)
        self.assertEqual(self.client.calls, [])

    def test_path_scope_empty_list_and_administrative_paths_rejected(self):
        for paths in ([], None, "a.txt", ["../escape"], ["https://host/repo"], ["file:///tmp/a"],
                      [".svn/wc.db"], ["x/.svn"], [""], ["bad\0name"], ["a.txt"] * 129):
            with self.subTest(paths=paths), self.assertRaises(ConflictError):
                self.manager.preflight_resolve(paths, "working")
        self.assertEqual(self.client.calls, [])

    def test_control_and_nonportable_paths_are_refused_before_commands(self):
        paths = ["line\nfeed", "a\tb", "delete\x7f", "a.txt:stream", "trailing.", "trailing ",
                 "CON", "NUL.txt", "COM1.log", "C:relative", 123, None]
        if os.name != "nt":
            paths.append("back\\slash")
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ConflictError):
                self.manager.inspect(path)
        self.assertEqual(self.client.calls, [])

    def test_links_inside_working_copy_are_refused(self):
        try:
            (self.root / "alias").symlink_to(self.root / "a.txt")
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(ConflictError):
            self.manager.inspect("alias")
        self.assertEqual(self.client.calls, [])

    def test_preflight_has_stable_token_and_hashes_without_editing_contents(self):
        with patch.object(conflicts.os, "write", side_effect=AssertionError("No content writes")):
            first = self.manager.preflight_resolve(["a.txt"], "working")
            second = self.manager.preflight_resolve(["a.txt"], "working")
        self.assertEqual(first.state_sha256, second.state_sha256)
        self.assertEqual(len(first.state_sha256), 64)
        self.assertEqual(first.inspections[0].content_sha256,
                         hashlib.sha256(b"current working contents").hexdigest())
        self.assertEqual(self.resolutions(), [])

    def test_content_change_is_detected_even_with_same_metadata_fingerprint(self):
        plan = self.manager.preflight_resolve(["a.txt"], "working")
        fingerprint = plan.inspections[0].file_fingerprint
        (self.root / "a.txt").write_bytes(b"CURRENT working contents")
        with patch.object(conflicts, "_fingerprint", return_value=fingerprint):
            with self.assertRaisesRegex(ConflictError, "changed after review"):
                self.manager.resolve(["a.txt"], "working", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.resolutions(), [])

    def test_sidecar_driven_modes_rejected_before_and_after_artifact_edits(self):
        sidecar = self.root / "a.txt.mine"
        sidecar.write_text("original local snapshot")
        for mode in ("mine-full", "theirs-full", "base"):
            with self.subTest(mode=mode):
                with self.assertRaises(ConflictError):
                    self.manager.preflight_resolve(["a.txt"], mode)
                sidecar.write_text("a new edit to preserve")
                with self.assertRaises(ConflictError):
                    self.manager.resolve(["a.txt"], mode, confirmed=True)
        self.assertEqual(self.client.calls, [])

    def test_sidecar_removal_limit_is_explicit_in_supported_plan(self):
        plan = self.manager.preflight_resolve(["a.txt"], "working")
        self.assertTrue(any("sidecar" in warning and "not" in warning for warning in plan.warnings))
        self.assertTrue(any("will be removed" in warning for warning in plan.warnings))

    def test_oversized_victim_blocks_resolution_without_reading(self):
        with patch.object(conflicts, "MAX_CONFLICT_FILE_BYTES", 5), \
                patch.object(conflicts.os, "read", side_effect=AssertionError("Should not read oversized file")):
            inspection = self.manager.inspect("a.txt")
        self.assertIn("content_fingerprint_unavailable", inspection.blocked_reasons)
        self.assertEqual(inspection.allowed_accepts, ())

    def test_opened_different_file_is_rejected_before_content_read(self):
        original = self.root / "a.txt"
        other = self.root / "other.txt"
        other.write_text("different")
        expected = conflicts._fingerprint(original.stat())
        with patch.object(conflicts.os, "fstat", return_value=other.stat()), \
                patch.object(conflicts.os, "read", side_effect=AssertionError("Should not read changed file")):
            with self.assertRaises(ConflictError):
                conflicts._file_digest(original, expected)

    def test_file_change_during_hash_is_refused(self):
        original_read = os.read
        edited = False

        def changing_read(descriptor, length):
            nonlocal edited
            result = original_read(descriptor, length)
            if not edited:
                edited = True
                (self.root / "a.txt").write_text("modified while hashing")
            return result

        with patch.object(conflicts.os, "read", side_effect=changing_read):
            inspection = self.manager.inspect("a.txt")
        self.assertEqual(inspection.allowed_accepts, ())
        self.assertIn("content_fingerprint_unavailable", inspection.blocked_reasons)

    def test_binary_content_is_bound_without_claiming_a_text_merge(self):
        data = b"\x00\xff\xfe manually selected binary bytes"
        (self.root / "a.txt").write_bytes(data)
        plan = self.manager.preflight_resolve(["a.txt"], "working")
        self.assertEqual(plan.inspections[0].content_sha256, hashlib.sha256(data).hexdigest())

    def test_changed_metadata_snapshot_prevents_resolution(self):
        plan = self.manager.preflight_resolve(["a.txt"], "working")
        self.client.entries["a.txt"] = replace(self.client.entries["a.txt"], revision=8)
        with self.assertRaisesRegex(ConflictError, "changed after review"):
            self.manager.resolve(["a.txt"], "working", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.resolutions(), [])

    def test_file_changed_since_review_prevents_resolution(self):
        plan = self.manager.preflight_resolve(["a.txt"], "working")
        (self.root / "a.txt").write_text("new content with a different length")
        with self.assertRaises(ConflictError):
            self.manager.resolve(["a.txt"], "working", confirmed=True, expected_state_sha256=plan.state_sha256)
        self.assertEqual(self.resolutions(), [])

    def test_invalid_snapshot_refused(self):
        for digest in ("bad", "A" * 64, "g" * 64, True, 123):
            with self.subTest(digest=digest), self.assertRaises(ConflictError):
                self.manager.resolve(["a.txt"], "working", confirmed=True, expected_state_sha256=digest)
        self.assertEqual(self.client.calls, [])

    def test_exact_paths_deduplicated_peg_escaped_and_not_shell_expanded(self):
        name = "-中文 @ file;$(x).txt"
        (self.root / name).touch()
        self.client.entries[name] = StatusEntry(name, "conflicted")
        self.manager.resolve([name, name], "working", confirmed=True)
        self.assertEqual(self.resolutions(), [["resolve", "--accept", "working", "--depth", "empty", "--", name + "@"]])

    def test_resolution_failure_is_not_automatically_retried(self):
        self.client.resolve_error = SvnError("timed out")
        with self.assertRaises(SvnError):
            self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(len(self.resolutions()), 1)

    def test_success_exit_with_remaining_conflict_is_not_claimed_verified(self):
        self.client.keep_conflict_after_resolve = True
        with self.assertRaisesRegex(SvnError, "still reports conflicts"):
            self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(len(self.resolutions()), 1)

    def test_post_resolution_status_failure_is_uncertain_without_retry(self):
        self.client.fail_post_status = True
        with self.assertRaisesRegex(SvnError, "could not be verified"):
            self.manager.resolve(["a.txt"], "working", confirmed=True)
        self.assertEqual(len(self.resolutions()), 1)

    def test_invalid_status_xml_and_missing_exact_status_refused(self):
        for text in ("<broken", '<status><target path="a.txt"/></status>',
                     '<status><entry path="other"><wc-status item="conflicted"/></entry></status>'):
            with self.subTest(text=text), patch.object(self.client, "command", return_value=CommandResult((), 0, text, "")):
                with self.assertRaises((SvnError, ConflictError)):
                    self.manager.inspect("a.txt")


class ConflictCliTests(unittest.TestCase):
    setUp = ConflictTests.setUp
    resolutions = ConflictTests.resolutions

    def invoke(self, args):
        output = io.StringIO()
        with patch("svn_client.cli.SvnClient", return_value=self.client):
            code = main(["--workspace", str(self.root), "conflict", *args], stdout=output, stdin=io.StringIO())
        self.assertEqual(output.getvalue().count("\n"), 1)
        return code, json.loads(output.getvalue())

    def test_cli_list_and_inspect_are_read_only(self):
        code, result = self.invoke(["list"])
        self.assertEqual(code, 0)
        self.assertEqual(result["command"], "conflict.list")
        self.assertEqual(result["data"]["conflicts"][0]["path"], "a.txt")
        code, result = self.invoke(["inspect", "--path", "a.txt"])
        self.assertEqual(code, 0)
        self.assertIn("working", result["data"]["conflict"]["allowed_accepts"])
        self.assertEqual(self.resolutions(), [])

    def test_cli_missing_confirmation_refused_before_any_backend_call(self):
        code, result = self.invoke(["resolve", "--path", "a.txt", "--accept", "working"])
        self.assertEqual(code, EXIT_CODES["confirmation_required"])
        self.assertFalse(result["ok"])
        self.assertEqual(self.client.calls, [])

    def test_cli_explicit_choice_and_path_are_required(self):
        for args in (["resolve", "--path", "a.txt", "--yes"],
                     ["resolve", "--accept", "working", "--yes"],
                     ["resolve", "--path", "a.txt", "--accept", "recommended", "--yes"]):
            with self.subTest(args=args):
                code, result = self.invoke(args)
                self.assertEqual(code, EXIT_CODES["usage_error"])
        self.assertEqual(self.client.calls, [])

    def test_cli_dry_run_then_confirmed_resolve_with_snapshot(self):
        args = ["resolve", "--path", "a.txt", "--accept", "working"]
        code, result = self.invoke([*args, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "planned")
        token = result["data"]["preflight"]["state_sha256"]
        self.assertEqual(self.resolutions(), [])
        code, result = self.invoke([*args, "--yes", "--expect-state-sha256", token])
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(len(self.resolutions()), 1)

    def test_cli_stale_snapshot_refused_before_mutation(self):
        code, result = self.invoke(["resolve", "--path", "a.txt", "--accept", "working", "--yes", "--expect-state-sha256", "0" * 64])
        self.assertEqual(code, EXIT_CODES["validation_failed"])
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(self.resolutions(), [])

    def test_cli_tree_conflict_refused(self):
        self.client.entries["a.txt"] = replace(self.client.entries["a.txt"], tree_conflicted=True)
        code, result = self.invoke(["resolve", "--path", "a.txt", "--accept", "working", "--yes"])
        self.assertEqual(code, EXIT_CODES["validation_failed"])
        self.assertIn("tree_conflict", result["error"]["message"])
        self.assertEqual(self.resolutions(), [])

    def test_cli_mutation_failure_is_uncertain_and_never_retried(self):
        self.client.resolve_error = SvnError("timeout")
        code, result = self.invoke(["resolve", "--path", "a.txt", "--accept", "working", "--yes"])
        self.assertEqual(code, EXIT_CODES["operation_uncertain"])
        self.assertEqual(result["outcome"], "uncertain")
        self.assertFalse(result["error"]["details"]["automatic_retry_safe"])
        self.assertEqual(len(self.resolutions()), 1)

    def test_cli_success_exit_but_remaining_conflict_is_uncertain(self):
        self.client.keep_conflict_after_resolve = True
        code, result = self.invoke(["resolve", "--path", "a.txt", "--accept", "working", "--yes"])
        self.assertEqual(code, EXIT_CODES["operation_uncertain"])
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(len(self.resolutions()), 1)


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "Real SVN/svnadmin unavailable")
class ConflictIntegrationTests(unittest.TestCase):
    def test_real_text_conflict_inspect_and_keep_local_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, first, second = root / "repo", root / "first", root / "second"

            def run(*args, cwd=None):
                return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=30)

            run("svnadmin", "create", str(repository))
            run("svn", "checkout", repository.as_uri(), str(first), "--non-interactive")
            (first / "a.txt").write_text("base\n")
            run("svn", "add", "a.txt", cwd=first)
            run("svn", "commit", "-m", "initial", cwd=first)
            run("svn", "checkout", repository.as_uri(), str(second), "--non-interactive")
            (first / "a.txt").write_text("incoming\n")
            run("svn", "commit", "-m", "incoming", cwd=first)
            (second / "a.txt").write_text("local\n")
            run("svn", "update", "--accept", "postpone", "--non-interactive", cwd=second)
            manager = ConflictManager(SvnClient(second))
            self.assertTrue(manager.inspect("a.txt").text_conflicted)
            (second / "a.txt").write_text("local\n")  # Explicit manual repair, then keep these exact bytes.
            plan = manager.preflight_resolve(["a.txt"], "working")
            manager.resolve(["a.txt"], "working", confirmed=True, expected_state_sha256=plan.state_sha256)
            self.assertEqual((second / "a.txt").read_text(), "local\n")
            self.assertEqual(manager.list(), [])


if __name__ == "__main__":
    unittest.main()
