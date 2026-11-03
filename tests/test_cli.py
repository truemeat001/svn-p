"""JSON protocol/safety tests; fake backends never touch a user repository."""
from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import CommandResult, LogEntry, StatusEntry, SvnError
from svn_client.branches import BranchError, BranchOperationError, BranchPlan, BranchResult, CopyPlan
from svn_client.cli import DEFAULT_TEXT_BYTES, EXIT_CODES, MAX_INPUT_BYTES, main
from svn_client.connection import ConnectionReport, ProbeStep, RepositoryIdentity, WorkingCopyIdentity
from svn_client.shelves import ShelfConflict, ShelfError


class NoRead(io.StringIO):
    def read(self, *args, **kwargs):
        raise AssertionError("Unexpected stdin read")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.client = Mock()
        self.client.working_copy = self.workspace
        self.result = CommandResult(("svn", "--message=PRIVATE"), 0, "done\n", "")
        self.member = StatusEntry("中文 @ file.txt", "modified", revision=3, changelist="fix")
        self.clean = StatusEntry("clean.txt", "normal", revision=3, changelist="fix")
        self.client.status.return_value = [self.member]
        self.client.info.return_value = {"revision": 3, "url": "https://example.test/repo"}
        self.client.log.return_value = [LogEntry(3, "author", message="hi")]
        self.client.diff.return_value = "--- 中文\n+new\n"
        self.client.changelists.return_value = {"fix": [self.member, self.clean]}
        self.client.validate_changelist_paths.side_effect = lambda values: list(values)
        self.client.preflight_changelist_commit.return_value = [self.member, self.clean]
        for name in ("assign_changelist", "remove_changelist", "commit_changelist"):
            getattr(self.client, name).return_value = self.result
        self.shelves = Mock()
        self.shelf_id = "a" * 32
        self.shelf = {"id": self.shelf_id, "name": "backup", "entries": [], "paths": ["a.txt"]}
        self.shelves.list.return_value = [self.shelf]
        self.shelves.details.return_value = self.shelf
        self.shelves.preflight_create.return_value = {"can_create": True, "paths": ["a.txt"]}
        self.shelves.preflight_restore.return_value = {"can_restore": True, "paths": ["a.txt"]}
        self.shelves.create.return_value = self.shelf
        self.shelves.restore.return_value = {"shelf_retained": True, "restored_paths": ["a.txt"]}
        self.client_patch = patch("svn_client.cli.SvnClient", return_value=self.client)
        self.shelf_patch = patch("svn_client.cli.ShelfManager", return_value=self.shelves)
        self.client_factory = self.client_patch.start()
        self.shelf_factory = self.shelf_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.shelf_patch.stop)

    def invoke(self, args, *, stdin=None, globals_=(), workspace=True):
        output = io.StringIO()
        prefix = ["--workspace", str(self.workspace)] if workspace else []
        code = main([*prefix, *globals_, *args], stdout=output,
                    stdin=NoRead() if stdin is None else stdin)
        raw = output.getvalue()
        self.assertEqual(raw.count("\n"), 1)
        self.assertTrue(raw.isascii())
        payload = json.loads(raw)
        self.assertEqual(set(payload), {"schema_version", "ok", "command", "workspace",
                                      "dry_run", "outcome", "data", "error", "warnings"})
        self.assertEqual(payload["schema_version"], 1)
        return code, payload

    def assert_error(self, args, expected, **kwargs):
        code, payload = self.invoke(args, **kwargs)
        self.assertEqual(code, EXIT_CODES[expected], payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], expected)
        self.assertIsInstance(payload["error"]["message"], str)
        return payload

    def test_help_is_json_without_workspace_or_dependency(self):
        code, payload = self.invoke(["--help"], workspace=False)
        self.assertEqual(code, 0)
        self.assertIn("exit_codes", payload["data"])
        self.client_factory.assert_not_called()

    def test_leaf_help_is_json(self):
        code, payload = self.invoke(["changelist", "commit", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--expect-members-sha256", payload["data"]["help"])

    def test_missing_workspace_is_json_error(self):
        self.assert_error(["status"], "usage_error", workspace=False)

    def test_unknown_and_credential_flags_are_rejected(self):
        for args in (["status", "--password", "secret"], ["execute", "commit"],
                     ["status", "--yes"], ["status", "--dry-run"]):
            with self.subTest(args=args):
                self.assert_error(args, "usage_error")
        self.client_factory.assert_not_called()

    def test_unknown_options_do_not_echo_accidental_credentials(self):
        payload = self.assert_error(["status", "--password", "TOP-SECRET"], "usage_error")
        self.assertNotIn("TOP-SECRET", json.dumps(payload))

    def test_global_abbreviations_rejected(self):
        self.assert_error(["status"], "usage_error", globals_=["--time", "2"])

    def test_nonexistent_workspace(self):
        self.assert_error(["--workspace", str(self.workspace / "missing"), "status"],
                          "validation_failed", workspace=False)

    def test_invalid_timeout_is_json_error(self):
        for value in ("0", "-1", "nan", "inf", "text"):
            with self.subTest(value=value):
                self.assert_error(["status"], "usage_error", globals_=["--timeout", value])

    def test_valid_timeout_forwarded(self):
        self.invoke(["status"], globals_=["--timeout", "2.5"])
        self.client_factory.assert_called_once_with(self.workspace, timeout=2.5)

    def test_status_defaults_to_workspace_read_only(self):
        code, payload = self.invoke(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["command"], "status")
        self.assertEqual(payload["outcome"], "read")
        self.assertEqual(payload["workspace"], str(self.workspace))
        self.assertEqual(payload["data"]["entries"][0]["path"], self.member.path)
        self.client.status.assert_called_once_with(None, verbose=False)

    def test_status_all_and_literal_paths(self):
        paths = ["-option", "中文 @.txt", "a;$(echo bad).txt"]
        self.invoke(["status", "--all", *(arg for path in paths for arg in ["--path=" + path])])
        self.client.status.assert_called_once_with(paths, verbose=True)

    def test_absolute_path_deduplicated_and_normalized(self):
        self.invoke(["status", "--path", str(self.workspace / "a.txt"), "--path", "./a.txt"])
        self.client.status.assert_called_once_with(["a.txt"], verbose=False)

    def test_paths_outside_or_url_rejected_before_client(self):
        for path in ("../out.txt", "https://host/repo", "file:///tmp/foo", "file:other", ""):
            with self.subTest(path=path):
                self.assert_error(["status", "--path", path], "validation_failed")
        self.client_factory.assert_not_called()

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            link = self.workspace / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is unavailable")
            self.assert_error(["status", "--path", "escape/f.txt"], "validation_failed")

    def test_explicit_json_stdin_preserves_unusual_names(self):
        paths = ["中文 file", "line\nfeed", "-option", "ending@"]
        self.invoke(["status", "--paths-json", "-"], stdin=io.StringIO(json.dumps(paths)))
        self.client.status.assert_called_once_with(paths, verbose=False)

    def test_json_file_input(self):
        source = self.workspace / "paths.json"
        source.write_text('["中文.txt"]', encoding="utf-8")
        self.invoke(["diff", "--paths-json", str(source)])
        self.client.diff.assert_called_once_with(["中文.txt"], revision=None)

    def test_invalid_json_selection(self):
        for data in ("[", "{}", "[]", '["valid",3]', '["../bad"]', '["a\\u0000b"]'):
            with self.subTest(data=data):
                self.assert_error(["status", "--paths-json", "-"], "validation_failed", stdin=io.StringIO(data))
        self.client_factory.assert_not_called()

    def test_paths_json_and_paths_are_exclusive(self):
        self.assert_error(["status", "--path", "a", "--paths-json", "-"], "usage_error")

    def test_stdin_input_size_bounded(self):
        self.assert_error(["status", "--paths-json", "-"], "validation_failed",
                          stdin=io.StringIO(" " * (MAX_INPUT_BYTES + 1)))

    def test_invalid_utf8_input_rejected(self):
        source = self.workspace / "paths.json"
        source.write_bytes(b'\xff')
        self.assert_error(["status", "--paths-json", str(source)], "validation_failed")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_fifo_input_refused_without_blocking(self):
        source = self.workspace / "pipe"
        os.mkfifo(source)
        self.assert_error(["status", "--paths-json", str(source)], "validation_failed")

    def test_input_file_changed_to_nonregular_refused(self):
        source = self.workspace / "paths.json"
        source.write_text("[]", encoding="utf-8")
        with patch("svn_client.cli.os.fstat", return_value=Mock(st_mode=0)):
            self.assert_error(["status", "--paths-json", str(source)], "validation_failed")

    def test_input_io_failure_structured(self):
        self.assert_error(["status", "--paths-json", str(self.workspace / "absent")], "io_error")

    def test_info_and_log_local_paths(self):
        self.invoke(["info", "--path", "sub/a"])
        self.client.info.assert_called_once_with(str(Path("sub/a")))
        code, payload = self.invoke(["log", "--path", "a", "--limit", "2", "--revision", "HEAD:1"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["entries"][0]["revision"], 3)
        self.client.log.assert_called_once_with(2, "a", revision="HEAD:1")

    def test_invalid_revision_and_limit(self):
        self.assert_error(["diff", "--revision", "--config-option=x"], "usage_error")
        self.assert_error(["diff", "--revision", "HEAD garbage"], "validation_failed")
        self.assert_error(["log", "--limit", "10001"], "validation_failed")

    def test_diff_binary_boundary_and_utf8_truncation(self):
        self.client.diff.return_value = "中文é"
        code, payload = self.invoke(["diff"], globals_=["--max-text-bytes", "4"])
        self.assertEqual(code, 0)
        field = payload["data"]["diff"]
        self.assertEqual(field, {"text": "中", "truncated": True,
                                 "source_utf8_bytes": 8, "returned_utf8_bytes": 3})
        self.assertFalse(payload["data"]["binary_payloads_included"])
        self.assertTrue(payload["warnings"])

    def test_json_escapes_control_bytes(self):
        self.client.diff.return_value = "\x1b[31m\x00\n"
        code, payload = self.invoke(["diff"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["diff"]["text"], self.client.diff.return_value)

    def test_mutations_require_yes_without_backend_calls(self):
        commands = [
            ["changelist", "assign", "--name", "fix", "--path", "a"],
            ["changelist", "remove", "--path", "a"],
            ["changelist", "commit", "--name", "fix", "--message", "hi"],
            ["shelf", "create", "--name", "backup", "--path", "a"],
            ["shelf", "restore", self.shelf_id], ["shelf", "delete", self.shelf_id],
        ]
        for args in commands:
            with self.subTest(args=args):
                self.assert_error(args, "confirmation_required")
        self.client_factory.assert_not_called()
        self.shelf_factory.assert_not_called()

    def test_empty_mutation_selection_never_means_all(self):
        self.assert_error(["changelist", "assign", "--name", "fix", "--yes"], "usage_error")
        self.assert_error(["shelf", "create", "--name", "backup", "--yes"], "usage_error")
        self.assert_error(["shelf", "create", "--name", "backup", "--paths-json", "-", "--yes"],
                          "validation_failed", stdin=io.StringIO("[]"))

    def test_yes_and_dry_run_mutually_exclusive(self):
        self.assert_error(["shelf", "delete", self.shelf_id, "--yes", "--dry-run"], "usage_error")

    def test_changelist_list_preserves_clean_members(self):
        code, payload = self.invoke(["changelist", "list"])
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["data"]["changelists"]["fix"]), 2)
        code, payload = self.invoke(["changelist", "list", "--name", "missing"])
        self.assertEqual(payload["data"], {"changelists": {}})

    def test_assign_dry_run_validates_without_mutation(self):
        code, payload = self.invoke(["changelist", "assign", "--name", "fix", "--path", "a", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "planned")
        self.client.validate_changelist_paths.assert_called_once_with(["a"])
        self.client.assign_changelist.assert_not_called()
        self.assertEqual(payload["data"]["preflight"]["paths"], ["a"])

    def test_invalid_name_rejected_before_validation(self):
        for name in (" ", "bad\nname", "https://u:password@host"):
            with self.subTest(name=name):
                self.assert_error(["changelist", "assign", "--name", name, "--path", "a", "--dry-run"], "validation_failed")
        self.client.validate_changelist_paths.assert_not_called()

    def test_assign_remove_execute_validated_paths(self):
        self.client.validate_changelist_paths.side_effect = lambda values: ["normalized"]
        code, payload = self.invoke(["changelist", "assign", "--name", "fix", "--path", "a", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "completed")
        self.client.assign_changelist.assert_called_once_with("fix", ["normalized"])
        self.assertNotIn("PRIVATE", json.dumps(payload))
        self.invoke(["changelist", "remove", "--path", "a", "--yes"])
        self.client.remove_changelist.assert_called_once_with(["normalized"])

    def test_changelist_path_validation_failures_do_not_mutate(self):
        self.client.validate_changelist_paths.side_effect = ValueError("not a versioned file")
        self.assert_error(["changelist", "assign", "--name", "fix", "--path", ".", "--yes"], "validation_failed")
        self.client.assign_changelist.assert_not_called()

    def commit_args(self, *extra):
        return ["changelist", "commit", "--name", "fix", "--message", "private message", *extra]

    def test_commit_dry_run_uses_public_preflight_and_review_hash(self):
        code, payload = self.invoke(self.commit_args("--dry-run"))
        self.assertEqual(code, 0)
        self.client.preflight_changelist_commit.assert_called_once_with("fix")
        self.client.commit_changelist.assert_not_called()
        plan = payload["data"]["preflight"]
        self.assertEqual(plan["paths"], [self.member.path, self.clean.path])
        self.assertEqual(len(plan["members_sha256"]), 64)
        self.assertFalse(plan["server_checked"])
        self.assertNotIn("private message", json.dumps(payload))

    def test_commit_uses_exact_full_members_and_optional_hash(self):
        _, preview = self.invoke(self.commit_args("--dry-run"))
        digest = preview["data"]["preflight"]["members_sha256"]
        code, _ = self.invoke(self.commit_args("--yes", "--keep-changelist", "--expect-members-sha256", digest))
        self.assertEqual(code, 0)
        self.client.commit_changelist.assert_called_once_with("fix", "private message", keep_changelist=True,
                                                             expected_paths=[self.member.path, self.clean.path])

    def test_commit_hash_changed_or_malformed_refuses_without_write(self):
        for digest in ("0" * 64, "bad"):
            with self.subTest(digest=digest):
                self.assert_error(self.commit_args("--yes", "--expect-members-sha256", digest), "validation_failed")
        self.client.commit_changelist.assert_not_called()

    def test_commit_preflight_rejection_is_known_no_write(self):
        self.client.preflight_changelist_commit.side_effect = ValueError("added parent directory")
        payload = self.assert_error(self.commit_args("--yes"), "validation_failed")
        self.assertEqual(payload["outcome"], "refused")
        self.client.commit_changelist.assert_not_called()

    def test_commit_stdin_message_explicit_only(self):
        args = ["changelist", "commit", "--name", "fix", "--message-file", "-", "--yes"]
        self.invoke(args, stdin=io.StringIO("中文\nsecond"))
        self.assertEqual(self.client.commit_changelist.call_args.args[1], "中文\nsecond")

    def test_commit_message_restrictions(self):
        for message in (" ", "bad\0input", "x" * 65537):
            with self.subTest(size=len(message)):
                self.assert_error(["changelist", "commit", "--name", "fix", "--message", message, "--yes"], "validation_failed")
        self.client.commit_changelist.assert_not_called()

    def test_commit_failure_uncertain_never_retried(self):
        self.client.commit_changelist.side_effect = SvnError("timed out")
        payload = self.assert_error(self.commit_args("--yes"), "operation_uncertain")
        self.assertEqual(payload["outcome"], "uncertain")
        self.assertFalse(payload["error"]["details"]["automatic_retry_safe"])
        self.client.commit_changelist.assert_called_once()

    def test_interrupted_mutation_uncertain(self):
        self.client.commit_changelist.side_effect = KeyboardInterrupt()
        self.assert_error(self.commit_args("--yes"), "operation_uncertain")

    def test_interrupted_read_not_uncertain(self):
        self.client.status.side_effect = KeyboardInterrupt()
        self.assert_error(["status"], "interrupted")

    def test_shelf_reads_do_not_require_svn(self):
        for args in (["shelf", "list"], ["shelf", "inspect", self.shelf_id],
                     ["shelf", "delete", self.shelf_id, "--dry-run"]):
            with self.subTest(args=args):
                code, _ = self.invoke(args)
                self.assertEqual(code, 0)
        self.client_factory.assert_not_called()
        self.shelves.delete.assert_not_called()

    def test_shelf_inspect_reports_eligibility_not_checked(self):
        _, payload = self.invoke(["shelf", "inspect", self.shelf_id])
        self.assertTrue(payload["data"]["blob_integrity_verified"])
        self.assertFalse(payload["data"]["restore_eligibility_checked"])

    def test_shelf_store_forwarded(self):
        store = str(self.workspace.parent / "custom-shelves")
        self.invoke(["shelf", "list"], globals_=["--shelf-store", store])
        self.shelf_factory.assert_called_once_with(self.workspace, storage_dir=store)

    def test_shelf_create_dry_run_calls_readonly_preflight(self):
        code, payload = self.invoke(["shelf", "create", "--name", "backup", "--path", "a.txt", "--dry-run"])
        self.assertEqual(code, 0)
        self.shelves.preflight_create.assert_called_once_with(["a.txt"])
        self.shelves.create.assert_not_called()
        self.assertTrue(payload["data"]["working_copy_unchanged"])

    def test_shelf_create_validates_name_length_in_preflight(self):
        self.assert_error(["shelf", "create", "--name", "a" * 201, "--path", "a", "--dry-run"], "validation_failed")
        self.shelves.preflight_create.assert_not_called()

    def test_shelf_create_executes_only_after_preflight(self):
        self.invoke(["shelf", "create", "--name", "backup", "--path", "a.txt", "--yes"])
        self.shelves.preflight_create.assert_called_once_with(["a.txt"])
        self.shelves.create.assert_called_once_with("backup", ["a.txt"])

    def test_shelf_restore_dry_run_and_actual(self):
        self.invoke(["shelf", "restore", self.shelf_id, "--dry-run"])
        self.shelves.preflight_restore.assert_called_once_with(self.shelf_id)
        self.shelves.restore.assert_not_called()
        code, payload = self.invoke(["shelf", "restore", self.shelf_id, "--yes"])
        self.assertEqual(code, 0)
        self.shelves.restore.assert_called_once_with(self.shelf_id)
        self.assertTrue(payload["data"]["restore"]["shelf_retained"])

    def test_restore_conflict_prevents_write(self):
        self.shelves.preflight_restore.side_effect = ShelfConflict("dirty target")
        self.assert_error(["shelf", "restore", self.shelf_id, "--yes"], "validation_failed")
        self.shelves.restore.assert_not_called()

    def test_restore_partial_failure_uncertain(self):
        self.shelves.restore.side_effect = ShelfError("partial restore")
        self.assert_error(["shelf", "restore", self.shelf_id, "--yes"], "operation_uncertain")
        self.shelves.restore.assert_called_once()

    def test_shelf_delete_verifies_first(self):
        self.invoke(["shelf", "delete", self.shelf_id, "--yes"])
        self.shelves.details.assert_called_once_with(self.shelf_id)
        self.shelves.delete.assert_called_once_with(self.shelf_id)
        self.client_factory.assert_not_called()

    def test_corrupt_shelf_delete_refused(self):
        self.shelves.details.side_effect = ShelfError("damaged")
        self.assert_error(["shelf", "delete", self.shelf_id, "--yes"], "shelf_failed")
        self.shelves.delete.assert_not_called()

    def test_no_svn_dependency_structured(self):
        self.client_factory.side_effect = SvnError("SVN executable was not found")
        self.assert_error(["status"], "dependency_missing")

    def test_svn_read_failure_structured(self):
        self.client.status.side_effect = SvnError("SVN read failed")
        self.assert_error(["status"], "svn_failed")

    def test_url_passwords_redacted_in_diagnostics_and_data(self):
        self.client.info.return_value = {"url": "https://name:secret@example.test/repo"}
        _, payload = self.invoke(["info"])
        self.assertNotIn("secret", json.dumps(payload))
        self.client.status.side_effect = SvnError("https://name:secret@example.test/repo")
        payload = self.assert_error(["status"], "svn_failed")
        self.assertNotIn("secret", json.dumps(payload))

    def test_metadata_credential_keys_refused_without_silent_scope_changes(self):
        self.client.changelists.return_value = {"https://name:secret@example.test": [self.member]}
        payload = self.assert_error(["changelist", "list"], "validation_failed")
        self.assertNotIn("secret", json.dumps(payload))

    def test_url_password_with_empty_username_redacted(self):
        self.client.info.return_value = {"url": "https://:secret@example.test/repo"}
        _, payload = self.invoke(["info"])
        self.assertNotIn("secret", json.dumps(payload))

    def test_internal_failure_does_not_leak_exception_contents(self):
        self.client.status.side_effect = RuntimeError("secret private implementation detail")
        payload = self.assert_error(["status"], "internal_error")
        self.assertNotIn("secret", json.dumps(payload))

    def test_oversized_result_never_silent_partial_success(self):
        self.client.status.return_value = [StatusEntry("x" * 10000, "modified")]
        payload = self.assert_error(["status"], "output_limit", globals_=["--max-output-bytes", "2048"])
        self.assertIsNone(payload["data"])
        self.assertEqual(payload["error"]["details"]["original_outcome"], "read")

    def test_output_limit_after_mutation_preserves_completed_outcome(self):
        self.client.preflight_changelist_commit.return_value = [StatusEntry("x" * 5000, "modified", changelist="fix")]
        payload = self.assert_error(self.commit_args("--yes"), "output_limit", globals_=["--max-output-bytes", "2048"])
        self.assertEqual(payload["outcome"], "completed")
        self.assertTrue(payload["error"]["details"]["mutation_attempted"])
        self.client.commit_changelist.assert_called_once()

    def test_output_limit_fallback_stays_bounded_with_unicode_workspace(self):
        # Multiple short path components avoid filesystem component-byte limits.
        deep = self.workspace / ("😀" * 50) / ("😀" * 50) / ("😀" * 50)
        deep.mkdir(parents=True)
        self.client.status.return_value = [StatusEntry("x" * 10000, "modified")]
        output = io.StringIO()
        code = main(["--workspace", str(deep), "--max-output-bytes", "2048", "status"],
                    stdout=output, stdin=NoRead())
        self.assertEqual(code, EXIT_CODES["output_limit"])
        self.assertLessEqual(len(output.getvalue().encode("ascii")), 2048)
        self.assertIsNone(json.loads(output.getvalue())["workspace"])

    def test_output_limit_parameters_validated(self):
        for value in ("0", "1024", "999999999"):
            with self.subTest(value=value):
                self.assert_error(["status"], "usage_error", globals_=["--max-output-bytes", value])

    def test_broken_pipe_never_retries_work(self):
        output = Mock()
        output.write.side_effect = BrokenPipeError()
        code = main(["--workspace", str(self.workspace), "status"], stdout=output, stdin=NoRead())
        self.assertEqual(code, EXIT_CODES["io_error"])
        self.client.status.assert_called_once()


class CliBranchTests(unittest.TestCase):
    """CLI safety contract; branch transport tests live in test_branches.py."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.source = "https://svn.example.test/repo/branches/feature"
        self.destination = "https://svn.example.test/repo/tags/v1"
        self.digest = "a" * 64
        self.client = Mock()
        self.branches = Mock()
        self.switch_plan = BranchPlan("switch", str(self.workspace), self.source, 12,
                                      "https://svn.example.test/repo/trunk", 10,
                                      "uuid", "https://svn.example.test/repo", None,
                                      self.digest, "", False, False, ("Whole workspace only",))
        self.merge_plan = replace(self.switch_plan, operation="merge", start_revision=10,
                                  preview_text="U    src/a.txt\n", svn_dry_run=True)
        self.switch_result = BranchResult("switch", "switched", str(self.workspace),
                                          self.source, 12, "Switched to revision 12.\n", (), ())
        self.merge_result = BranchResult("merge", "needs_review", str(self.workspace),
                                         "https://svn.example.test/repo/trunk", 10,
                                         "U    src/a.txt\n", (StatusEntry("src/a.txt", "modified"),), ())
        self.copy_plan = CopyPlan("tag", self.source, 12, self.destination, "uuid", 14,
                                  "private planned message", self.digest, ("Plan only",))
        self.branches.preflight_switch.return_value = self.switch_plan
        self.branches.preflight_merge.return_value = self.merge_plan
        self.branches.preflight_copy.return_value = self.copy_plan
        self.branches.switch.return_value = self.switch_result
        self.branches.merge.return_value = self.merge_result
        client_patch = patch("svn_client.cli.SvnClient", return_value=self.client)
        branch_patch = patch("svn_client.cli.BranchManager", return_value=self.branches)
        self.client_factory = client_patch.start()
        self.branch_factory = branch_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(branch_patch.stop)

    def invoke(self, arguments, *, stdin=None, globals_=()):
        output = io.StringIO()
        code = main(["--workspace", str(self.workspace), *globals_, "branch", *arguments],
                    stdout=output, stdin=NoRead() if stdin is None else stdin)
        raw = output.getvalue()
        self.assertEqual(raw.count("\n"), 1)
        self.assertTrue(raw.isascii())
        return code, json.loads(raw)

    def switch(self, *extra):
        return ["switch", "--url", self.source, "--revision", "12", *extra]

    def merge(self, *extra):
        return ["merge", "--source-url", self.source, "--start-revision", "10",
                "--end-revision", "12", *extra]

    def copy(self, *extra):
        return ["plan-copy", "--kind", "tag", "--source-url", self.source,
                "--destination-url", self.destination, "--revision", "12",
                "--message", "private planned message", *extra]

    def test_switch_and_merge_require_explicit_confirmation(self):
        for arguments in (self.switch(), self.merge()):
            with self.subTest(arguments=arguments):
                code, payload = self.invoke(arguments)
                self.assertEqual(code, 3)
                self.assertEqual(payload["error"]["code"], "confirmation_required")
        self.client_factory.assert_not_called()
        self.branch_factory.assert_not_called()

    def test_yes_without_reviewed_hash_refused_before_client(self):
        for arguments in (self.switch("--yes"), self.merge("--yes")):
            with self.subTest(arguments=arguments):
                code, payload = self.invoke(arguments)
                self.assertEqual(code, 3)
                self.assertIn("--expect-state-sha256", payload["error"]["message"])
        self.client_factory.assert_not_called()

    def test_hash_without_yes_is_not_permission(self):
        code, _ = self.invoke(self.switch("--expect-state-sha256", self.digest))
        self.assertEqual(code, 3)
        self.client_factory.assert_not_called()

    def test_malformed_hash_refused_before_client(self):
        for value in ("bad", "A" * 64, "a" * 63):
            with self.subTest(value=value):
                code, payload = self.invoke(self.switch("--yes", "--expect-state-sha256", value))
                self.assertEqual(code, 4)
                self.assertEqual(payload["outcome"], "refused")
        self.client_factory.assert_not_called()

    def test_numeric_revision_only_including_zero(self):
        for value in ("HEAD", "BASE", "1:3", "-1", str(2**63)):
            with self.subTest(value=value):
                arguments = ["switch", "--url", self.source, "--revision", value, "--dry-run"]
                code, _ = self.invoke(arguments)
                self.assertEqual(code, 2)
        code, _ = self.invoke(["switch", "--url", self.source, "--revision", "0", "--dry-run"])
        self.assertEqual(code, 0)
        self.branches.preflight_switch.assert_called_once_with(self.source, revision=0)

    def test_credential_or_unsupported_url_rejected_before_client(self):
        for value in ("https://user:secret@host/repo", "svn+ssh://host/repo", "../branch", "https://host/repo?a=b"):
            with self.subTest(value=value):
                code, payload = self.invoke(["switch", "--url", value, "--revision", "12", "--dry-run"])
                self.assertEqual(code, 4)
                self.assertNotIn("secret", json.dumps(payload))
        self.client_factory.assert_not_called()

    def test_switch_preview_labels_no_svn_dry_run(self):
        code, payload = self.invoke(self.switch("--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "planned")
        plan = payload["data"]["preflight"]
        self.assertEqual(plan["state_sha256"], self.digest)
        self.assertEqual(plan["scope"], "whole_working_copy_root")
        self.assertFalse(plan["svn_dry_run"])
        self.assertEqual(plan["preview"]["text"], "")
        self.assertFalse(payload["data"]["commit_performed"])
        self.branches.switch.assert_not_called()

    def test_merge_preview_is_actual_svn_dry_run_and_bounded(self):
        self.branches.preflight_merge.return_value = replace(self.merge_plan, preview_text="中文" * 100)
        code, payload = self.invoke(self.merge("--dry-run"), globals_=["--max-text-bytes", "4"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["data"]["preflight"]["svn_dry_run"])
        self.assertTrue(payload["data"]["preflight"]["preview"]["truncated"])
        self.assertEqual(payload["data"]["preflight"]["preview"]["text"], "中")
        self.branches.preflight_merge.assert_called_once_with(self.source, start_revision=10, end_revision=12)
        self.branches.merge.assert_not_called()

    def test_review_hash_on_dry_run_rejects_mismatch(self):
        code, payload = self.invoke(self.merge("--dry-run", "--expect-state-sha256", "b" * 64))
        self.assertEqual(code, 4)
        self.assertEqual(payload["error"]["details"]["current_state_sha256"], self.digest)
        self.branches.merge.assert_not_called()

    def test_switch_executes_only_callers_reviewed_hash(self):
        code, payload = self.invoke(self.switch("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "completed")
        self.assertEqual(payload["data"]["result"]["outcome"], "switched")
        self.assertFalse(payload["data"]["commit_performed"])
        self.branches.switch.assert_called_once_with(self.source, revision=12, confirmed=True,
                                                     expected_state_sha256=self.digest)
        self.branches.preflight_switch.assert_not_called()

    def test_merge_needs_review_is_not_completed_or_committed(self):
        code, payload = self.invoke(self.merge("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "needs_review")
        self.assertTrue(payload["data"]["requires_review"])
        self.assertFalse(payload["data"]["commit_performed"])
        self.assertFalse(payload["data"]["automatic_retry_safe"])
        self.assertEqual(payload["data"]["result"]["changes"][0]["path"], "src/a.txt")
        self.branches.merge.assert_called_once_with(self.source, start_revision=10, end_revision=12,
                                                    confirmed=True, expected_state_sha256=self.digest)
        self.branches.preflight_merge.assert_not_called()

    def test_merge_conflicts_are_explicit_in_top_outcome(self):
        self.branches.merge.return_value = replace(self.merge_result, outcome="conflicted",
                                                   conflicts=("src/a.txt",))
        code, payload = self.invoke(self.merge("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["outcome"], "conflicted")
        self.assertEqual(payload["data"]["result"]["conflicts"], ["src/a.txt"])
        self.assertTrue(payload["data"]["requires_review"])

    def test_preflight_error_is_known_refusal(self):
        self.branches.preflight_switch.side_effect = BranchError("Dirty working copy")
        code, payload = self.invoke(self.switch("--dry-run"))
        self.assertEqual(code, 4)
        self.assertEqual(payload["outcome"], "refused")
        self.branches.switch.assert_not_called()

    def test_stale_guard_inside_mutator_is_known_prewritten_refusal(self):
        self.branches.switch.side_effect = BranchError("Reviewed state changed before apply")
        code, payload = self.invoke(self.switch("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 4)
        self.assertEqual(payload["outcome"], "refused")
        self.branches.switch.assert_called_once()

    def test_branch_operation_error_is_uncertain_never_retried(self):
        self.branches.merge.side_effect = BranchOperationError("Attempted but verification failed")
        code, payload = self.invoke(self.merge("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 8)
        self.assertEqual(payload["outcome"], "uncertain")
        self.assertFalse(payload["error"]["details"]["automatic_retry_safe"])
        self.branches.merge.assert_called_once()

    def test_interrupted_mutation_is_uncertain(self):
        self.branches.switch.side_effect = KeyboardInterrupt()
        code, payload = self.invoke(self.switch("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 8)
        self.assertEqual(payload["outcome"], "uncertain")
        self.branches.switch.assert_called_once()

    def test_unexpected_branch_result_is_uncertain(self):
        self.branches.merge.return_value = replace(self.merge_result, outcome="completed")
        code, payload = self.invoke(self.merge("--yes", "--expect-state-sha256", self.digest))
        self.assertEqual(code, 8)
        self.assertEqual(payload["outcome"], "uncertain")

    def test_copy_plan_cannot_execute_or_echo_message(self):
        code, payload = self.invoke(self.copy())
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "planned")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["data"]["execution_supported"])
        self.assertTrue(payload["data"]["repository_unchanged"])
        self.assertFalse(payload["data"]["commit_performed"])
        self.assertTrue(payload["data"]["preflight"]["would_commit_immediately"])
        self.assertNotIn("private planned message", json.dumps(payload))
        self.branches.preflight_copy.assert_called_once_with(self.source, self.destination,
                                                             revision=12, kind="tag", message="private planned message")
        self.branches.switch.assert_not_called()
        self.branches.merge.assert_not_called()

    def test_copy_plan_rejects_yes_and_invented_execute_modes(self):
        for extra in ("--yes", "--force", "--execute"):
            with self.subTest(extra=extra):
                code, _ = self.invoke(self.copy(extra))
                self.assertEqual(code, 2)
        code, _ = self.invoke(["create", "--yes"])
        self.assertEqual(code, 2)
        self.client_factory.assert_not_called()
        self.branch_factory.assert_not_called()

    def test_copy_plan_stdin_message_explicit_and_bounded(self):
        arguments = self.copy()[:-2] + ["--message-file", "-"]
        code, _ = self.invoke(arguments, stdin=io.StringIO("中文 planned message"))
        self.assertEqual(code, 0)
        self.assertEqual(self.branches.preflight_copy.call_args.kwargs["message"], "中文 planned message")
        self.branches.preflight_copy.reset_mock()
        code, payload = self.invoke(arguments, stdin=io.StringIO("x" * 8193))
        self.assertEqual(code, 4)
        self.assertEqual(payload["outcome"], "refused")
        self.branches.preflight_copy.assert_not_called()

    def test_output_limit_preserves_merge_review_state_and_no_retry(self):
        self.branches.merge.return_value = replace(self.merge_result,
                                                   changes=tuple(StatusEntry(str(i) + "x" * 100, "modified") for i in range(100)))
        code, payload = self.invoke(self.merge("--yes", "--expect-state-sha256", self.digest),
                                    globals_=["--max-output-bytes", "2048"])
        self.assertEqual(code, 9)
        self.assertEqual(payload["outcome"], "needs_review")
        self.assertTrue(payload["error"]["details"]["mutation_attempted"])
        self.branches.merge.assert_called_once()

    def test_branch_help_remains_json(self):
        code, payload = self.invoke(["switch", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--expect-state-sha256", payload["data"]["help"])
        self.client_factory.assert_not_called()


class CliDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.url = "https://svn.example.test/repo/trunk"
        self.identity = RepositoryIdentity(self.url, "https://svn.example.test/repo",
                                            "11111111-2222-3333-4444-555555555555", 12, "dir")
        self.local = WorkingCopyIdentity(str(self.workspace), str(self.workspace), self.identity)
        self.report = ConnectionReport(True, (ProbeStep("executable", "ok", "ok", "Version read"),),
                                        executable="/trusted/svn", version="1.14.3")
        self.diagnostics = Mock()
        self.diagnostics.probe.return_value = self.report
        diagnostic_patch = patch("svn_client.cli.ConnectionDiagnostics", return_value=self.diagnostics)
        client_patch = patch("svn_client.cli.SvnClient")
        self.factory = diagnostic_patch.start()
        self.client_factory = client_patch.start()
        self.addCleanup(diagnostic_patch.stop)
        self.addCleanup(client_patch.stop)

    def invoke(self, arguments=(), *, workspace=None, globals_=()):
        output = io.StringIO()
        prefix = ["--workspace", str(workspace)] if workspace is not None else []
        code = main([*prefix, *globals_, "diagnostics", *arguments], stdout=output, stdin=NoRead())
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertTrue(output.getvalue().isascii())
        return code, json.loads(output.getvalue())

    def test_installation_only_is_default_without_workspace(self):
        code, payload = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(payload["command"], "diagnostics")
        self.assertEqual(payload["outcome"], "read")
        self.assertFalse(payload["dry_run"])
        self.assertIsNone(payload["workspace"])
        self.factory.assert_called_once_with(None, timeout=15.0)
        self.diagnostics.probe.assert_called_once_with(url=None, check_remote=False)
        self.client_factory.assert_not_called()
        self.assertFalse(payload["data"]["report"]["remote_requested"])
        self.assertFalse(payload["data"]["report"]["remote_attempted"])
        self.assertTrue(payload["data"]["read_only"])
        self.assertTrue(any("do not establish server connectivity" in message for message in payload["warnings"]))

    def test_local_workspace_identity_never_becomes_remote_request(self):
        self.diagnostics.probe.return_value = replace(self.report, working_copy=self.local)
        code, payload = self.invoke(workspace=self.workspace)
        self.assertEqual(code, 0)
        self.factory.assert_called_once_with(str(self.workspace), timeout=15.0)
        self.diagnostics.probe.assert_called_once_with(url=None, check_remote=False)
        self.assertEqual(payload["workspace"], str(self.workspace))
        self.assertEqual(payload["data"]["report"]["working_copy"]["repository"]["url"], self.url)
        self.client_factory.assert_not_called()

    def test_remote_probe_requires_both_explicit_arguments_before_factory(self):
        for arguments in (["--check-remote"], ["--url", self.url]):
            with self.subTest(arguments=arguments):
                code, payload = self.invoke(arguments)
                self.assertEqual(code, 4)
                self.assertEqual(payload["error"]["code"], "validation_failed")
        self.factory.assert_not_called()
        self.client_factory.assert_not_called()

    def test_remote_url_is_never_inferred_even_with_working_copy(self):
        code, payload = self.invoke(["--check-remote"], workspace=self.workspace)
        self.assertEqual(code, 4)
        self.assertEqual(payload["outcome"], "refused")
        self.factory.assert_not_called()

    def test_invalid_credential_or_ssh_urls_refused_before_factory(self):
        for url in ("https://u:secret@host/repo", "https://user@host/repo", "svn+ssh://host/repo", "file://server/share", "file:////server/share", "file:///%2Fserver/share", "", "https://host/repo?token=secret"):
            with self.subTest(url=url):
                code, payload = self.invoke(["--check-remote", "--url", url])
                self.assertEqual(code, 4)
                self.assertNotIn("secret", json.dumps(payload))
        self.factory.assert_not_called()

    def test_explicit_remote_only_passes_exact_url(self):
        literal = self.url + "/name@123"
        self.diagnostics.probe.return_value = replace(self.report, remote=self.identity,
                                                     remote_requested=True, remote_attempted=True)
        code, payload = self.invoke(["--check-remote", "--url", literal])
        self.assertEqual(code, 0)
        self.factory.assert_called_once_with(None, timeout=15.0)
        self.diagnostics.probe.assert_called_once_with(url=literal, check_remote=True)
        self.assertTrue(payload["data"]["report"]["remote_attempted"])
        self.assertFalse(any("No repository URL was contacted" in text for text in payload["warnings"]))

    def test_explicit_timeout_is_forwarded(self):
        self.invoke(globals_=["--timeout", "6.5"])
        self.factory.assert_called_once_with(None, timeout=6.5)

    def test_report_failure_has_exit_code_and_preserves_steps(self):
        failure = ProbeStep("remote", "failed", "tls", "Certificate validation failed", ("230001",), 1)
        self.diagnostics.probe.return_value = replace(self.report, ok=False, steps=(failure,),
                                                     remote_requested=True, remote_attempted=True)
        code, payload = self.invoke(["--check-remote", "--url", self.url])
        self.assertEqual(code, 10)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(payload["error"]["code"], "diagnostics_failed")
        self.assertEqual(payload["data"]["report"]["steps"][0]["category"], "tls")
        self.assertEqual(payload["data"]["report"]["steps"][0]["error_codes"], ["230001"])
        self.diagnostics.probe.assert_called_once()

    def test_missing_svn_remains_structured_report_without_client_initialization(self):
        self.diagnostics.probe.return_value = ConnectionReport(False,
            (ProbeStep("executable", "failed", "dependency_missing", "SVN unavailable"),))
        code, payload = self.invoke()
        self.assertEqual(code, 10)
        self.assertEqual(payload["data"]["report"]["steps"][0]["category"], "dependency_missing")
        self.client_factory.assert_not_called()

    def test_invalid_workspace_can_be_reported_and_explicit_remote_still_allowed(self):
        missing = self.workspace / "absent"
        self.diagnostics.probe.return_value = replace(self.report, ok=False,
            steps=(ProbeStep("working_copy", "failed", "working_copy", "Folder unavailable"),),
            remote=self.identity, remote_requested=True, remote_attempted=True)
        code, payload = self.invoke(["--check-remote", "--url", self.url], workspace=missing)
        self.assertEqual(code, 10)
        self.factory.assert_called_once_with(str(missing), timeout=15.0)
        self.assertTrue(payload["data"]["report"]["remote_attempted"])

    def test_repository_uuid_mismatch_is_a_failed_diagnostic_not_mutation(self):
        self.diagnostics.probe.return_value = replace(self.report, ok=False,
            steps=(ProbeStep("identity", "failed", "identity_mismatch", "Repository UUIDs differ"),),
            working_copy=self.local, remote=self.identity, remote_requested=True,
            remote_attempted=True, repository_identity_matches=False)
        code, payload = self.invoke(["--check-remote", "--url", self.url], workspace=self.workspace)
        self.assertEqual(code, 10)
        self.assertFalse(payload["data"]["report"]["repository_identity_matches"])
        self.assertTrue(payload["data"]["working_copy_unchanged"])
        self.assertTrue(payload["data"]["repository_unchanged"])

    def test_report_values_are_redacted_by_json_boundary(self):
        identity = replace(self.identity, url="https://u:secret@host/repo")
        self.diagnostics.probe.return_value = replace(self.report, remote=identity)
        _, payload = self.invoke()
        self.assertNotIn("secret", json.dumps(payload))

    def test_diagnostics_has_no_write_retry_or_credential_flags(self):
        for flag in ("--yes", "--dry-run", "--force", "--password", "--trust-server-cert", "--retry"):
            with self.subTest(flag=flag):
                code, _ = self.invoke([flag])
                self.assertEqual(code, 2)
        self.factory.assert_not_called()

    def test_help_without_workspace_stays_json(self):
        code, payload = self.invoke(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--check-remote", payload["data"]["help"])
        self.factory.assert_not_called()

    def test_oversized_report_preserves_diagnostic_failure_code(self):
        steps = tuple(ProbeStep("remote", "failed", "svn_error", "safe" * 1000) for _ in range(20))
        self.diagnostics.probe.return_value = replace(self.report, ok=False, steps=steps)
        code, payload = self.invoke(globals_=["--max-output-bytes", "2048"])
        self.assertEqual(code, 9)
        self.assertEqual(payload["error"]["details"]["original_error_code"], "diagnostics_failed")
        self.assertFalse(payload["error"]["details"]["mutation_attempted"])
        self.diagnostics.probe.assert_called_once()


class CliDiagnosticsTransportTests(unittest.TestCase):
    """Wire the real diagnostic module to fake process responses, never a server."""
    def test_local_default_runs_only_version_and_local_info(self):
        with tempfile.TemporaryDirectory() as directory:
            xml = (f'<info><entry kind="dir" path="." revision="12"><url>https://server.invalid/repo/trunk</url>'
                   '<repository><root>https://server.invalid/repo</root><uuid>11111111-2222-3333-4444-555555555555</uuid></repository>'
                   f'<wc-info><wcroot-abspath>{directory}</wcroot-abspath></wc-info></entry></info>').encode()
            results = [subprocess.CompletedProcess([], 0, b"1.14.3\n", b""),
                       subprocess.CompletedProcess([], 0, xml, b"")]
            output = io.StringIO()
            with patch("svn_client.connection.discover_svn", return_value="/trusted/svn"), \
                 patch("svn_client.connection.subprocess.run", side_effect=results) as run:
                code = main(["--workspace", directory, "diagnostics"], stdout=output, stdin=NoRead())
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0], ["/trusted/svn", "--version", "--quiet"])
            self.assertEqual(run.call_args_list[1].args[0][-1], ".")
            for call in run.call_args_list:
                self.assertFalse(any("server.invalid" in arg for arg in call.args[0]))
                self.assertIs(call.kwargs["shell"], False)
                self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(call.kwargs["timeout"], 15.0)
            report = json.loads(output.getvalue())["data"]["report"]
            self.assertFalse(report["remote_requested"])
            self.assertFalse(report["remote_attempted"])
            self.assertIsNone(report["remote"])

    def test_explicit_remote_probe_exposes_safe_category_not_raw_failure(self):
        url = "https://server.invalid/repo/name@123"
        results = [subprocess.CompletedProcess([], 0, b"1.14.3\n", b""),
                   subprocess.CompletedProcess([], 1, b"raw stdout secret-token",
                                               b"svn: E215004: raw password secret-token certificate text\n")]
        output = io.StringIO()
        with patch("svn_client.connection.discover_svn", return_value="/trusted/svn"), \
             patch("svn_client.connection.subprocess.run", side_effect=results) as run:
            code = main(["diagnostics", "--check-remote", "--url", url], stdout=output, stdin=NoRead())
        self.assertEqual(code, 10)
        self.assertEqual(run.call_count, 2)
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[-1], url + "@HEAD")
        self.assertIn("--non-interactive", command)
        self.assertIn("--no-auth-cache", command)
        self.assertNotIn("--trust-server-cert", command)
        self.assertNotIn("secret-token", output.getvalue())
        report = json.loads(output.getvalue())["data"]["report"]
        self.assertTrue(report["remote_requested"])
        self.assertTrue(report["remote_attempted"])
        self.assertEqual(report["steps"][-1]["category"], "authentication")
        self.assertEqual(report["steps"][-1]["error_codes"], ["215004"])

    def test_diagnostic_timeout_upper_bound_before_any_process(self):
        output = io.StringIO()
        with patch("svn_client.connection.discover_svn") as discovery, \
             patch("svn_client.connection.subprocess.run") as run:
            code = main(["--timeout", "121", "diagnostics"], stdout=output, stdin=NoRead())
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "validation_failed")
        discovery.assert_not_called()
        run.assert_not_called()

    def test_missing_dependency_reports_without_workspace(self):
        output = io.StringIO()
        with patch("svn_client.connection.discover_svn", side_effect=SvnError("private raw diagnostic")), \
             patch("svn_client.connection.subprocess.run") as run:
            code = main(["diagnostics"], stdout=output, stdin=NoRead())
        self.assertEqual(code, 10)
        self.assertNotIn("private raw diagnostic", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["data"]["report"]["steps"][0]["category"], "dependency_missing")
        run.assert_not_called()


class CliProcessTests(unittest.TestCase):
    def test_module_help_stdout_contains_one_json_stderr_empty(self):
        result = subprocess.run([sys.executable, "-m", "svn_client.cli", "--help"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout)["schema_version"], 1)

    def test_large_plain_lines_and_late_urls_do_not_stall_redaction(self):
        # A process timeout bounds the regression itself. The old unanchored
        # scheme regex took quadratic time even for text containing no URL.
        script = (
            "from svn_client.cli import _redact; "
            "prefix = 'a' * 1048576; "
            "assert _redact(prefix) == prefix; "
            "assert _redact(prefix + ' https://u:secret@host') == "
            "prefix + ' https://u:***@host'; print('ok')"
        )
        result = subprocess.run([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"ok")

    def test_invalid_command_stays_in_json_without_traceback(self):
        result = subprocess.run([sys.executable, "-m", "svn_client.cli", "not-a-command"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "usage_error")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin are unavailable")
class CliSvnIntegrationTests(unittest.TestCase):
    """Only disposable local repositories are used, never a user's checkout."""
    def test_local_changelist_commit_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, workspace = root / "repo", root / "wc"
            def svn(*args):
                subprocess.run([shutil.which("svn"), "--non-interactive", *map(str, args)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=True, timeout=30)
            subprocess.run([shutil.which("svnadmin"), "create", str(repository)], check=True, timeout=30)
            svn("checkout", repository.as_uri(), workspace)
            filename = workspace / "中文 @ file.txt"
            filename.write_text("initial\n", encoding="utf-8")
            svn("add", str(filename) + "@")
            svn("commit", "--message", "seed", workspace)
            filename.write_text("changed\n", encoding="utf-8")
            def call(*args):
                output = io.StringIO()
                code = main(["--workspace", str(workspace), *args], stdout=output, stdin=NoRead())
                payload = json.loads(output.getvalue())
                self.assertEqual(code, 0, payload)
                return payload
            call("changelist", "assign", "--name", "fix", "--path", filename.name, "--yes")
            plan = call("changelist", "commit", "--name", "fix", "--message", "test", "--dry-run")["data"]["preflight"]
            call("changelist", "commit", "--name", "fix", "--message", "test", "--expect-members-sha256", plan["members_sha256"], "--yes")
            self.assertEqual(call("status")["data"]["entries"], [])
            self.assertEqual(call("log", "--limit", "1")["data"]["entries"][0]["message"], "test")


if __name__ == "__main__":
    unittest.main()
