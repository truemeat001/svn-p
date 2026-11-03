"""Repository lock metadata, confirmation, exact scope, and partial-outcome tests."""
from dataclasses import asdict
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

from svn_client.backend import CommandResult, StatusEntry, SvnClient, SvnError
from svn_client.locks import LockError, LockManager, LockOperationError


class FakeRepository:
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.calls = []
        self.mutations = []
        self.mode = "normal"
        self.fail_postread = False
        self.uuid = "12345678-abcd-1234-abcd-123456789012"
        self.add("中文 @ file.bin")
        self.add("other.txt")

    def add(self, name):
        (self.root / name).write_bytes(b"content\0binary")
        self.files[name] = dict(item="normal", props="none", schedule="normal", revision=5,
                                changed=5, local="", remote="", owner="alice", comment="asset edit",
                                created="2026-11-03T09:00:00Z", expires="", copied=False)

    def url(self, name):
        return "https://example.test/repo/" + quote(name, safe="/@")

    def decode(self, value):
        if value.endswith("@"):
            value = value[:-1]
        # SVN XML and repository fixture keys use slash-separated paths.
        return Path(value).as_posix()

    def info(self, name, remote=False):
        root = ET.Element("info")
        state = self.files.get(name)
        kind = "file" if state is not None else "dir"
        entry = ET.SubElement(root, "entry", path=name, kind=kind, revision=str(state["revision"] if state else 5))
        ET.SubElement(entry, "url").text = self.url(name)
        repo = ET.SubElement(entry, "repository")
        ET.SubElement(repo, "uuid").text = self.uuid
        ET.SubElement(repo, "root").text = "https://example.test/repo"
        if not remote:
            wc = ET.SubElement(entry, "wc-info")
            ET.SubElement(wc, "wcroot-abspath").text = str(self.root)
            ET.SubElement(wc, "schedule").text = state["schedule"] if state else "normal"
        ET.SubElement(entry, "commit", revision=str(state["changed"] if state else 5))
        token = state["remote" if remote else "local"] if state else ""
        if token:
            lock = ET.SubElement(entry, "lock")
            ET.SubElement(lock, "token").text = token
            for field in ("owner", "comment", "created", "expires"):
                ET.SubElement(lock, field).text = state[field]
        return ET.tostring(root, encoding="unicode")

    def status(self):
        root = ET.Element("status")
        target = ET.SubElement(root, "target", path=".")
        for name, state in self.files.items():
            entry = ET.SubElement(target, "entry", path=name)
            ET.SubElement(entry, "wc-status", item=state["item"], props=state["props"],
                          revision=str(state["revision"]), copied=str(state["copied"]).lower())
        return ET.tostring(root, encoding="unicode")

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "status":
            return CommandResult(tuple(args), 0, self.status(), "")
        if args[0] == "info":
            target = args[-1]
            if "://" in target:
                if self.fail_postread and self.mutations:
                    raise SvnError("Server unavailable opaquelocktoken:SHOULD-NOT-LEAK")
                name = next((name for name in self.files if self.url(name) + "@HEAD" == target), None)
                if name is None:
                    raise SvnError("Unknown remote target")
                return CommandResult(tuple(args), 0, self.info(name, remote=True), "")
            name = self.decode(target)
            if name not in self.files and name != "." and not (self.root / name).is_dir():
                raise SvnError("Unversioned file")
            return CommandResult(tuple(args), 0, self.info(name), "")
        if args[0] not in {"lock", "unlock"}:
            raise AssertionError(args)
        self.mutations.append(args)
        names = [self.decode(value) for value in args[args.index("--") + 1:]]
        for index, name in enumerate(names):
            if self.mode in {"partial_zero", "partial_error"} and index > 0:
                continue
            state = self.files[name]
            if args[0] == "lock":
                if state["remote"]:
                    continue
                state["remote"] = state["local"] = "opaquelocktoken:SECRET-" + name
                state["comment"] = next(value.split("=", 1)[1] for value in args if value.startswith("--message="))
            else:
                if state["remote"] == state["local"]:
                    state["remote"] = state["local"] = ""
        if self.mode == "partial_error":
            raise SvnError("svn: E160035 failed opaquelocktoken:SHOULD-NOT-LEAK https://u:PASSWORD@host")
        return CommandResult(tuple(args), 0, "SVN completed with possible warnings", "")


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = FakeRepository(self.root)
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            self.client = SvnClient(self.root)
        self.command = patch.object(self.client, "command", side_effect=self.repo.command)
        self.command.start()
        self.addCleanup(self.command.stop)
        self.manager = LockManager(self.client)
        self.names = list(self.repo.files)

    def own(self, name=None, token="opaquelocktoken:SECRET"):
        state = self.repo.files[name or self.names[0]]
        state["local"] = state["remote"] = token
        return state

    def test_inspect_reads_server_xml_and_reports_owner_without_tokens(self):
        state = self.own()
        state["comment"] = "contains " + state["remote"] + " https://u:PASSWORD@host"
        info = self.manager.inspect([self.names[0]])[0]
        serialized = json.dumps(asdict(info), ensure_ascii=False)
        self.assertTrue(info.locked)
        self.assertTrue(info.owned_by_working_copy)
        self.assertEqual(info.owner, "alice")
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("PASSWORD", serialized)
        self.assertEqual(self.repo.mutations, [])
        self.assertTrue(any(args[-1] == self.repo.url(self.names[0]) + "@HEAD" for args in self.repo.calls))

    def test_same_owner_another_working_copy_is_not_ownership(self):
        self.repo.files[self.names[0]]["remote"] = "opaque:server-token"
        info = self.manager.inspect([self.names[0]])[0]
        self.assertFalse(info.owned_by_working_copy)
        with self.assertRaises(LockError):
            self.manager.preflight_unlock([self.names[0]])
        with self.assertRaises(LockError):
            self.manager.preflight_lock([self.names[0]])
        self.assertEqual(self.repo.mutations, [])

    def test_stale_token_is_not_usable_for_unlock_or_new_lock(self):
        self.repo.files[self.names[0]]["local"] = "opaque:old-token"
        self.assertTrue(self.manager.inspect([self.names[0]])[0].stale_local_token)
        for method in (self.manager.preflight_lock, self.manager.preflight_unlock):
            with self.assertRaises(LockError):
                method([self.names[0]])

    def test_preflight_lock_read_only_state_bound_to_comment(self):
        first = self.manager.preflight_lock(self.names, "reserve assets")
        second = self.manager.preflight_lock(list(reversed(self.names)), "reserve assets")
        self.assertEqual(first.state_sha256, second.state_sha256)
        self.assertEqual(first.paths, tuple(self.names))
        self.assertEqual(self.repo.mutations, [])
        with self.assertRaisesRegex(LockError, "reviewed lock state changed"):
            self.manager.lock(self.names, "different comment", confirmed=True, expected_state=first.state_sha256)
        self.assertEqual(self.repo.mutations, [])

    def test_explicit_confirmation_before_even_reading_server(self):
        for method in (self.manager.lock, self.manager.unlock):
            with self.assertRaisesRegex(LockError, "confirmation"):
                method(self.names)
        self.assertEqual(self.repo.calls, [])

    def test_lock_exact_files_no_force_no_token_and_postverify(self):
        plan = self.manager.preflight_lock([self.names[0]], "reserve binary")
        result = self.manager.lock([self.names[0]], "reserve binary", confirmed=True, expected_state=plan.state_sha256)
        self.assertTrue(result.verified)
        self.assertTrue(result.files[0].owned_by_working_copy)
        args = self.repo.mutations[0]
        self.assertEqual(args[-2:], ["--", self.names[0] + "@"])
        self.assertIn("--message=reserve binary", args)
        self.assertNotIn("--force", args)
        self.assertFalse(self.repo.files[self.names[1]]["remote"])
        self.assertNotIn("SECRET", repr(result))
        self.assertEqual(len(self.repo.mutations), 1)

    def test_unlock_only_own_exact_token_and_postverify(self):
        self.own()
        plan = self.manager.preflight_unlock([self.names[0]])
        result = self.manager.unlock([self.names[0]], confirmed=True, expected_state=plan.state_sha256)
        self.assertFalse(result.files[0].locked)
        self.assertFalse(result.files[0].local_token_present)
        self.assertEqual(self.repo.mutations[0], ["unlock", "--", self.names[0] + "@"])

    def test_lock_marks_only_its_comment_for_utf8_conversion(self):
        comment = "中文锁定说明 🚀"
        with patch.object(self.client, "_command_with_targets",
                          wraps=self.client._command_with_targets) as execute:
            result = self.manager.lock([self.names[0]], comment, confirmed=True)
        self.assertEqual(execute.call_args.kwargs, {"_message_index": 1})
        self.assertEqual(execute.call_args.args[0][1], "--message=" + comment)
        self.assertEqual(result.files[0].comment, comment)
        with patch.object(self.client, "_command_with_targets",
                          wraps=self.client._command_with_targets) as execute:
            self.manager.unlock([self.names[0]], confirmed=True)
        self.assertEqual(execute.call_args.kwargs, {"_message_index": None})

    def test_relocked_state_with_identical_public_metadata_invalidates_plan(self):
        self.own(token="custom:first")
        plan = self.manager.preflight_unlock([self.names[0]])
        self.own(token="custom:second")
        with self.assertRaisesRegex(LockError, "reviewed lock state changed"):
            self.manager.unlock([self.names[0]], confirmed=True, expected_state=plan.state_sha256)
        self.assertEqual(self.repo.mutations, [])

    def test_batch_preflight_refuses_before_any_mutation_if_one_locked(self):
        self.repo.files[self.names[1]]["remote"] = "custom:other"
        with self.assertRaises(LockError):
            self.manager.lock(self.names, confirmed=True)
        self.assertEqual(self.repo.mutations, [])

    def test_zero_exit_partial_result_is_uncertain_not_success(self):
        self.repo.mode = "partial_zero"
        with self.assertRaises(LockOperationError) as caught:
            self.manager.lock(self.names, confirmed=True)
        self.assertEqual(caught.exception.outcome, "uncertain")
        self.assertFalse(caught.exception.automatic_retry_safe)
        self.assertTrue(caught.exception.observed[0].owned_by_working_copy)
        self.assertFalse(caught.exception.observed[1].locked)
        self.assertEqual(len(self.repo.mutations), 1)

    def test_error_after_partial_write_is_not_retried_or_rolled_back(self):
        self.repo.mode = "partial_error"
        with self.assertRaises(LockOperationError) as caught:
            self.manager.lock(self.names, confirmed=True)
        self.assertTrue(self.repo.files[self.names[0]]["remote"])
        self.assertEqual(len(self.repo.mutations), 1)
        self.assertIn("E160035", str(caught.exception))
        self.assertNotIn("PASSWORD", str(caught.exception))
        self.assertNotIn("SHOULD-NOT-LEAK", str(caught.exception))

    def test_postquery_failure_reports_uncertain(self):
        self.repo.fail_postread = True
        with self.assertRaisesRegex(LockOperationError, "could not be verified"):
            self.manager.lock([self.names[0]], confirmed=True)
        self.assertEqual(len(self.repo.mutations), 1)

    def test_out_of_date_file_refused(self):
        self.repo.files[self.names[0]]["changed"] = 6
        with self.assertRaisesRegex(LockError, "out of date"):
            self.manager.preflight_lock([self.names[0]])
        self.assertEqual(self.repo.mutations, [])

    def test_modified_regular_file_is_lockable(self):
        self.repo.files[self.names[0]]["item"] = "modified"
        self.assertEqual(self.manager.preflight_lock([self.names[0]]).paths, (self.names[0],))

    def test_unsupported_local_states_rejected(self):
        for item in ("added", "deleted", "missing", "replaced", "conflicted", "obstructed", "external"):
            self.repo.files[self.names[0]]["item"] = item
            with self.subTest(item=item), self.assertRaises(LockError):
                self.manager.preflight_lock([self.names[0]])
        self.assertEqual(self.repo.mutations, [])

    def test_empty_outside_unversioned_directory_and_links_rejected(self):
        (self.root / "folder").mkdir()
        (self.root / "unversioned").write_text("unversioned")
        for paths in ([], ["../outside"], ["folder"], ["unversioned"], self.names[0]):
            with self.subTest(paths=paths), self.assertRaises(LockError):
                self.manager.preflight_lock(paths)
        self.assertEqual(self.repo.mutations, [])

    def test_bad_comments_and_expected_hash_do_not_mutate(self):
        for comment in ("a" * 4097, "bad\0comment", "https://u:PASSWORD@host", "opaquelocktoken:SECRET", None):
            with self.subTest(comment=comment), self.assertRaises(LockError):
                self.manager.preflight_lock([self.names[0]], comment)
        self.assertEqual(self.repo.calls, [])
        for digest in ("invalid", 42, "A" * 64):
            with self.assertRaises(LockError):
                self.manager.lock([self.names[0]], confirmed=True, expected_state=digest)
        self.assertEqual(self.repo.mutations, [])

    def test_hardlinked_file_refused_before_lock(self):
        other = self.root / "hardlink"
        try:
            os.link(self.root / self.names[0], other)
        except (OSError, NotImplementedError):
            self.skipTest("Hardlinks are unavailable")
        with self.assertRaisesRegex(LockError, "Hardlinked"):
            self.manager.preflight_lock([self.names[0]])
        self.assertEqual(self.repo.mutations, [])

    def test_missing_token_in_lock_xml_refused(self):
        original = self.repo.info
        def malformed(name, remote=False):
            root = ET.fromstring(original(name, remote))
            if remote:
                ET.SubElement(root.find("entry"), "lock")
            return ET.tostring(root, encoding="unicode")
        with patch.object(self.repo, "info", side_effect=malformed):
            with self.assertRaisesRegex(LockError, "incomplete lock metadata"):
                self.manager.inspect([self.names[0]])

    def test_repository_identity_change_refused(self):
        original = self.repo.info
        def changed(name, remote=False):
            root = ET.fromstring(original(name, remote))
            if remote:
                root.find("entry/repository/uuid").text = "DIFFERENT"
            return ET.tostring(root, encoding="unicode")
        with patch.object(self.repo, "info", side_effect=changed):
            with self.assertRaisesRegex(LockError, "identity changed"):
                self.manager.preflight_lock([self.names[0]])
        self.assertEqual(self.repo.mutations, [])

    def test_ssh_or_password_url_refused_before_server_lookup(self):
        for url in ("svn+ssh://user@host/file", "https://user:PASSWORD@host/file", "http:local", "file://"):
            self.repo.calls.clear()
            with patch.object(self.repo, "url", return_value=url):
                with self.assertRaisesRegex(LockError, "without embedded passwords") as caught:
                    self.manager.inspect([self.names[0]])
            self.assertNotIn("PASSWORD", str(caught.exception))
            self.assertFalse(any("://" in args[-1] for args in self.repo.calls if args[0] == "info"))

    def test_scheduled_structural_change_refused_before_server_lookup(self):
        self.repo.files[self.names[0]]["schedule"] = "add"
        with self.assertRaisesRegex(LockError, "scheduled structural"):
            self.manager.preflight_lock([self.names[0]])
        self.assertFalse(any("://" in args[-1] for args in self.repo.calls if args[0] == "info"))

    def test_permission_or_xml_read_error_never_mutates(self):
        original = self.repo.command
        def broken(args, **kwargs):
            if args[0] == "info" and "://" in args[-1]:
                return CommandResult(tuple(args), 0, "not XML opaquelocktoken:SECRET", "")
            return original(args, **kwargs)
        with patch.object(self.client, "command", side_effect=broken):
            with self.assertRaises(LockError) as caught:
                self.manager.preflight_lock([self.names[0]])
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.repo.mutations, [])

    def test_switched_files_refused(self):
        with patch.object(self.client, "status", return_value=[StatusEntry(self.names[0], "normal", switched=True)]):
            with self.assertRaisesRegex(LockError, "switched"):
                self.manager.preflight_lock([self.names[0]])
        self.assertEqual(self.repo.mutations, [])

    def test_selection_cap_and_invalid_types_rejected_before_reads(self):
        for paths in ([self.names[0]] * 129, None, iter(self.names)):
            with self.subTest(paths=type(paths)), self.assertRaises(LockError):
                self.manager.inspect(paths)
        self.assertEqual(self.repo.calls, [])

    def test_nested_regular_file_is_inspected_without_mutation(self):
        (self.root / "branch").mkdir()
        self.repo.add("branch/file.txt")
        inspected = self.manager.inspect(["branch/file.txt"])
        self.assertEqual([item.path for item in inspected], [str(Path("branch") / "file.txt")])
        self.assertFalse(inspected[0].locked)
        self.assertEqual(self.repo.mutations, [])
        self.assertTrue(any(args[-1] == self.repo.url("branch/file.txt") + "@HEAD"
                            for args in self.repo.calls if args[0] == "info"))

    def test_switched_or_added_ancestor_refused(self):
        (self.root / "branch").mkdir()
        self.repo.add("branch/file.txt")
        for ancestor in (StatusEntry("branch", "normal", switched=True), StatusEntry("branch", "added")):
            with patch.object(self.client, "status", return_value=[StatusEntry("branch/file.txt", "normal"), ancestor]):
                with self.assertRaisesRegex(LockError, "parent directories"):
                    self.manager.preflight_lock(["branch/file.txt"])
        self.assertEqual(self.repo.mutations, [])


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin required for integration")
class RealLockTests(unittest.TestCase):
    def test_temporary_local_repository_lock_unlock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, wc = root / "repo", root / "wc"
            subprocess.run(["svnadmin", "create", str(repo)], check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repo.as_uri(), str(wc)], check=True, capture_output=True)
            client = SvnClient(wc)
            name = "中文 @ binary.bin"
            (wc / name).write_bytes(b"\0\1\2")
            client.add([name])
            client.commit("base")
            client.update()
            manager = LockManager(client)
            comment = "中文锁定说明 🚀"
            plan = manager.preflight_lock([name], comment)
            result = manager.lock([name], comment, confirmed=True, expected_state=plan.state_sha256)
            self.assertTrue(result.files[0].owned_by_working_copy)
            self.assertTrue(result.files[0].owner)
            self.assertEqual(result.files[0].comment, comment)
            plan = manager.preflight_unlock([name])
            result = manager.unlock([name], confirmed=True, expected_state=plan.state_sha256)
            self.assertFalse(result.files[0].locked)
            self.assertFalse(result.files[0].local_token_present)


if __name__ == "__main__":
    unittest.main()
