"""JSON CLI lock scope, confirmation, binding, and uncertainty contracts."""
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import SvnClient
from svn_client.cli import EXIT_CODES, main
from svn_client.locks import FileLockInfo, LockError, LockOperationError, LockPlan, LockResult


class LockCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.client, self.manager = Mock(), Mock()
        self.client.working_copy = self.workspace
        self.name = "中文 @ asset.bin"
        self.free = FileLockInfo(self.name, "https://host/repo/asset.bin", "repo-id", 4, 4, False)
        self.owned = replace(self.free, locked=True, owner="artist", comment="texture edit",
                             local_token_present=True, owned_by_working_copy=True)
        self.plan = LockPlan("lock", (self.free,), "a" * 64, "texture edit")
        self.release = LockPlan("unlock", (self.owned,), "b" * 64)
        self.manager.inspect.return_value = [self.owned]
        self.manager.preflight_lock.return_value = self.plan
        self.manager.preflight_unlock.return_value = self.release
        self.manager.lock.return_value = LockResult("lock", (self.owned,))
        self.manager.unlock.return_value = LockResult("unlock", (self.free,))
        client_patch = patch("svn_client.cli.SvnClient", return_value=self.client)
        lock_patch = patch("svn_client.cli.LockManager", return_value=self.manager)
        self.factory = client_patch.start()
        self.lock_factory = lock_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(lock_patch.stop)

    def invoke(self, args, *, globals_=(), stdin=None):
        output = io.StringIO()
        code = main(["--workspace", str(self.workspace), *globals_, "lock", *args],
                    stdout=output, stdin=io.StringIO("") if stdin is None else stdin)
        raw = output.getvalue()
        self.assertTrue(raw.isascii())
        self.assertEqual(raw.count("\n"), 1)
        payload = json.loads(raw)
        self.assertEqual(payload["schema_version"], 1)
        return code, payload

    def assert_error(self, args, error, **kwargs):
        code, payload = self.invoke(args, **kwargs)
        self.assertEqual(code, EXIT_CODES[error], payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], error)
        return payload

    def test_inspect_exact_normalized_selection_and_token_free_shape(self):
        code, result = self.invoke(["inspect", "--path", str(self.workspace / self.name),
                                    "--path", "./" + self.name])
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "read")
        self.assertEqual(result["command"], "lock.inspect")
        self.manager.inspect.assert_called_once_with([self.name])
        self.assertTrue(result["data"]["server_checked"])
        self.assertFalse(result["data"]["raw_tokens_included"])
        info = result["data"]["files"][0]
        self.assertTrue(info["owned_by_working_copy"])
        self.assertTrue(info["local_token_present"])
        self.assertFalse({"token", "local_token", "server_token"} & info.keys())
        self.manager.lock.assert_not_called()
        self.manager.unlock.assert_not_called()

    def test_acquire_dry_run_calls_real_preflight_contract_only(self):
        code, result = self.invoke(["acquire", "--path", self.name, "--comment", "texture edit", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "planned")
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["data"]["repository_unchanged"])
        self.assertEqual(result["data"]["preflight"]["state_sha256"], self.plan.state_sha256)
        self.assertEqual(result["data"]["preflight"]["files"][0]["path"], self.name)
        self.manager.preflight_lock.assert_called_once_with([self.name], "texture edit")
        self.manager.lock.assert_not_called()

    def test_release_dry_run(self):
        code, result = self.invoke(["release", "--path", self.name, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["preflight"]["action"], "unlock")
        self.manager.preflight_unlock.assert_called_once_with([self.name])
        self.manager.unlock.assert_not_called()

    def test_mutations_without_confirmation_rejected_before_client(self):
        for action in ("acquire", "release"):
            self.assert_error([action, "--path", self.name], "confirmation_required")
        self.factory.assert_not_called()
        self.lock_factory.assert_not_called()

    def test_mutation_flags_mutually_exclusive(self):
        self.assert_error(["acquire", "--path", self.name, "--yes", "--dry-run"], "usage_error")
        self.factory.assert_not_called()

    def test_inspect_rejects_mutation_flags(self):
        for flag in ("--yes", "--dry-run"):
            self.assert_error(["inspect", "--path", self.name, flag], "usage_error")
        self.factory.assert_not_called()

    def test_paths_are_required_never_implicit_workspace(self):
        for args in (["inspect"], ["acquire", "--yes"], ["release", "--dry-run"]):
            self.assert_error(args, "usage_error")
        self.factory.assert_not_called()

    def test_force_steal_tokens_and_credentials_have_no_cli_surface(self):
        for flag in ("--force", "--steal", "--break", "--token", "--password"):
            result = self.assert_error(["acquire", "--path", self.name, "--yes", flag, "PRIVATE"], "usage_error")
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.factory.assert_not_called()

    def test_expected_state_format_rejected_before_network_preflight(self):
        for value in ("bad", "A" * 64, "a" * 63, ""):
            self.assert_error(["acquire", "--path", self.name, "--yes", "--expect-state-sha256", value], "validation_failed")
        self.manager.preflight_lock.assert_not_called()
        self.manager.lock.assert_not_called()

    def test_stale_review_hash_rejected_before_mutation(self):
        result = self.assert_error(["release", "--path", self.name, "--yes", "--expect-state-sha256", "a" * 64], "validation_failed")
        self.assertEqual(result["error"]["details"]["current_state_sha256"], self.release.state_sha256)
        self.manager.unlock.assert_not_called()

    def test_yes_acquire_binds_fresh_plan_and_exact_paths(self):
        code, result = self.invoke(["acquire", "--path", self.name, "--comment", "texture edit",
                                    "--expect-state-sha256", self.plan.state_sha256, "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "completed")
        self.manager.lock.assert_called_once_with(self.plan.paths, self.plan.comment,
                                                  confirmed=True, expected_state=self.plan.state_sha256)
        self.assertTrue(result["data"]["result"]["verified"])

    def test_yes_release_binds_fresh_plan_without_optional_review_hash(self):
        code, result = self.invoke(["release", "--path", self.name, "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "completed")
        self.manager.unlock.assert_called_once_with(self.release.paths, confirmed=True,
                                                    expected_state=self.release.state_sha256)

    def test_read_failure_is_validation_failure_not_attempted_mutation(self):
        self.manager.inspect.side_effect = LockError("Cannot read current server metadata.")
        result = self.assert_error(["inspect", "--path", self.name], "validation_failed")
        self.assertEqual(result["outcome"], "refused")
        self.manager.lock.assert_not_called()

    def test_preflight_failure_never_mutates(self):
        self.manager.preflight_lock.side_effect = LockError("Already locked; no stealing.")
        self.assert_error(["acquire", "--path", self.name, "--yes"], "validation_failed")
        self.manager.lock.assert_not_called()

    def test_partial_operation_returns_observed_states_and_no_retry(self):
        self.manager.lock.side_effect = LockOperationError("Partial result. Inspect locks first.", [self.owned])
        result = self.assert_error(["acquire", "--path", self.name, "--yes"], "operation_uncertain")
        self.assertEqual(result["outcome"], "uncertain")
        details = result["error"]["details"]
        self.assertFalse(details["automatic_retry_safe"])
        self.assertFalse(details["raw_tokens_included"])
        self.assertTrue(details["observed_locks"][0]["owned_by_working_copy"])
        self.manager.lock.assert_called_once()
        self.manager.unlock.assert_not_called()

    def test_second_preflight_failure_is_conservatively_uncertain(self):
        self.manager.unlock.side_effect = LockError("The reviewed lock state changed.")
        result = self.assert_error(["release", "--path", self.name, "--yes"], "operation_uncertain")
        self.assertEqual(result["outcome"], "uncertain")
        self.assertFalse(result["error"]["details"]["automatic_retry_safe"])
        self.manager.unlock.assert_called_once()

    def test_outside_url_and_empty_paths_rejected_before_client(self):
        for value in ("../outside", "https://host/repo/file", ""):
            self.assert_error(["inspect", "--path", value], "validation_failed")
        self.factory.assert_not_called()

    def test_selection_cap_before_manager_queries(self):
        args = ["inspect", *["--path=file" + str(i) for i in range(129)]]
        self.assert_error(args, "validation_failed")
        self.lock_factory.assert_not_called()

    def test_json_paths_preserve_unicode_whitespace_and_literal_at(self):
        names = ["line\nfeed", "-option", " ends@", "中文 name"]
        self.invoke(["inspect", "--paths-json", "-"], stdin=io.StringIO(json.dumps(names)))
        self.manager.inspect.assert_called_once_with(names)

    def test_output_limit_does_not_repeat_completed_lock(self):
        self.manager.lock.return_value = LockResult("lock", (replace(self.owned, comment="a" * 10000),))
        result = self.assert_error(["acquire", "--path", self.name, "--yes"], "output_limit",
                                   globals_=["--max-output-bytes", "2048"])
        self.assertEqual(result["outcome"], "completed")
        self.manager.lock.assert_called_once()

    def test_leaf_help_is_json_and_documents_binding(self):
        code, result = self.invoke(["acquire", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--expect-state-sha256", result["data"]["help"])
        self.factory.assert_not_called()


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin required for integration")
class RealLockCliTests(unittest.TestCase):
    def test_local_repository_review_bound_lock_and_unlock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, wc = root / "repo", root / "wc"
            subprocess.run(["svnadmin", "create", str(repo)], check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repo.as_uri(), str(wc)], check=True, capture_output=True)
            name = "中文 @ asset.bin"
            (wc / name).write_bytes(b"\0binary")
            client = SvnClient(wc)
            client.add([name])
            client.commit("initial file")
            client.update()
            def invoke(action, *flags):
                output = io.StringIO()
                code = main(["--workspace", str(wc), "lock", action, "--path", name, *flags], stdout=output)
                data = json.loads(output.getvalue())
                self.assertEqual(code, 0, data)
                self.assertNotIn("opaquelocktoken:", output.getvalue())
                return data["data"]
            self.assertFalse(invoke("inspect")["files"][0]["locked"])
            plan = invoke("acquire", "--comment", "edit", "--dry-run")["preflight"]
            self.assertTrue(invoke("acquire", "--comment", "edit", "--expect-state-sha256", plan["state_sha256"], "--yes")["result"]["files"][0]["owned_by_working_copy"])
            plan = invoke("release", "--dry-run")["preflight"]
            self.assertFalse(invoke("release", "--expect-state-sha256", plan["state_sha256"], "--yes")["result"]["files"][0]["locked"])


if __name__ == "__main__":
    unittest.main()
