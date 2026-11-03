"""Native SVN changelist safety and scope tests."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from svn_client.backend import CommandResult, StatusEntry, SvnClient, SvnError


class ChangelistUnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            self.client = SvnClient(self.root)
        self.info_patch = patch.object(self.client, "info", side_effect=self.info)
        self.info_mock = self.info_patch.start()
        self.addCleanup(self.info_patch.stop)

    def info(self, path=None):
        return {"kind": "dir" if path is None or str(path) == "folder" else "file",
                "wc_root": str(self.root)}

    def run_mutation(self, method, *args, **kwargs):
        with patch.object(self.client, "status", return_value=[]), patch.object(
            self.client, "command", return_value=CommandResult((), 0, "ok", "")
        ) as command:
            method(*args, **kwargs)
        return command.call_args.args[0]

    def test_list_includes_clean_members_and_sorts(self):
        entries = [StatusEntry("z.txt", "normal", changelist="测试 组"),
                   StatusEntry("a.txt", "modified", changelist="测试 组"),
                   StatusEntry("unassigned", "modified"),
                   StatusEntry("external", "normal", changelist="skip", file_external=True)]
        with patch.object(self.client, "status", return_value=entries) as status:
            groups = self.client.changelists()
        status.assert_called_once_with(verbose=True)
        self.assertEqual(list(groups), ["测试 组"])
        self.assertEqual([entry.path for entry in groups["测试 组"]], ["a.txt", "z.txt"])

    def test_status_verbose_preserves_clean_changelist_xml(self):
        xml = '<status><changelist name="任务 甲"><entry path="clean.txt"><wc-status item="normal" props="none" revision="3"/></entry></changelist></status>'
        with patch.object(self.client, "command", return_value=CommandResult((), 0, xml, "")) as command:
            entry = self.client.status(verbose=True)[0]
        self.assertEqual(entry.changelist, "任务 甲")
        self.assertEqual(entry.item, "normal")
        self.assertIn("--verbose", command.call_args.args[0])

    def test_assignment_unicode_spaces_and_at_literal(self):
        argv = self.run_mutation(self.client.assign_changelist, "测试 修复", ["中文 @ file.txt", "--password"])
        self.assertEqual(argv, ["changelist", "--depth", "empty", "--", "测试 修复", "中文 @ file.txt@", "--password"])

    def test_option_looking_label_is_literal(self):
        argv = self.run_mutation(self.client.assign_changelist, "--remove", ["file.txt"])
        self.assertEqual(argv[-3:], ["--", "--remove", "file.txt"])

    def test_option_label_reaches_real_runner_as_one_argument(self):
        completed = subprocess.CompletedProcess([], 0, b"ok", b"")
        with patch.object(self.client, "status", return_value=[]), patch(
            "svn_client.backend.subprocess.run", return_value=completed
        ) as run:
            self.client.assign_changelist("--password", ["a@b.txt"])
        self.assertEqual(run.call_args.args[0][-3:], ["--", "--password", "a@b.txt@"])
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_credential_bearing_name_is_not_logged_or_executed(self):
        with patch.object(self.client, "status", return_value=[]), patch("svn_client.backend.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "Passwords"):
                self.client.assign_changelist("https://user:secret@example.test/list", ["file"])
            run.assert_not_called()

    def test_remove_is_explicit_nonrecursive_and_deduplicated(self):
        argv = self.run_mutation(self.client.remove_changelist, ["f@.txt", "f@.txt"])
        self.assertEqual(argv, ["changelist", "--remove", "--depth", "empty", "--", "f@.txt@"])

    def test_names_and_empty_selections_rejected_without_mutation(self):
        with patch.object(self.client, "command") as command:
            for name in ("", "   ", "bad\nname", "bad\0name", "tab\tname", None):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.client.assign_changelist(name, ["file.txt"])
            for paths in ([], None, "file.txt"):
                with self.subTest(paths=paths), self.assertRaises(ValueError):
                    self.client.assign_changelist("valid", paths)
            command.assert_not_called()

    def test_directory_anywhere_in_batch_rejects_entire_batch(self):
        with patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "directories"):
                self.client.assign_changelist("fix", ["good.txt", "folder"])
            command.assert_not_called()

    def test_unversioned_file_rejects_entire_batch(self):
        def checked_info(path=None):
            if path == "unversioned.txt":
                raise SvnError("not a versioned resource")
            return self.info(path)
        self.info_mock.side_effect = checked_info
        with patch.object(self.client, "command") as command:
            with self.assertRaises(SvnError):
                self.client.assign_changelist("fix", ["good.txt", "unversioned.txt"])
            command.assert_not_called()

    def test_nested_working_copy_rejected(self):
        self.info_mock.side_effect = lambda path=None: {
            "kind": "dir" if path is None else "file",
            "wc_root": str(self.root if path is None else self.root / "nested"),
        }
        with patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "nested"):
                self.client.assign_changelist("fix", ["nested/file"])
            command.assert_not_called()

    def test_external_file_rejected(self):
        with patch.object(self.client, "status", return_value=[StatusEntry("external", "normal", file_external=True)]), patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "External"):
                self.client.assign_changelist("fix", ["external"])
            command.assert_not_called()

    def test_outside_path_rejected(self):
        with patch.object(self.client, "command") as command:
            for method in (lambda: self.client.assign_changelist("fix", ["../outside"]),
                           lambda: self.client.remove_changelist(["../outside"])):
                with self.assertRaises(ValueError):
                    method()
            command.assert_not_called()

    def test_internal_symlink_rejected(self):
        (self.root / "target").write_text("content")
        try:
            (self.root / "link").symlink_to(self.root / "target")
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is not supported")
        with patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                self.client.assign_changelist("fix", ["link"])
            command.assert_not_called()

    def commit(self, *, keep=False, expected=None, name="任务 甲"):
        members = [StatusEntry("clean.txt", "normal", changelist=name),
                   StatusEntry("中文 @ file.txt", "modified", changelist=name)]
        with patch.object(self.client, "changelists", return_value={name: members}):
            return self.run_mutation(self.client.commit_changelist, name, "只提交这组",
                                     keep_changelist=keep, expected_paths=expected)

    def test_commit_has_exact_files_and_filter_without_scope_expansion(self):
        argv = self.commit(expected=["中文 @ file.txt", "clean.txt"])
        self.assertEqual(argv[0], "commit")
        self.assertIn("--changelist=任务 甲", argv)
        self.assertEqual(argv[-3:], ["--", "clean.txt", "中文 @ file.txt@"])
        self.assertNotIn(".", argv)
        self.assertNotIn("--keep-changelists", argv)

    def test_commit_keep_and_option_looking_name(self):
        argv = self.commit(keep=True, name="--password")
        self.assertIn("--keep-changelists", argv)
        self.assertIn("--changelist=--password", argv)

    def test_commit_marks_only_its_explicit_message_for_conversion(self):
        members = [StatusEntry("file.txt", "modified", changelist="--message=label")]
        with patch.object(self.client, "preflight_changelist_commit", return_value=members), \
                patch.object(self.client, "_command_with_targets") as command:
            self.client.commit_changelist("--message=label", "中文提交 🚀")
        options, targets = command.call_args.args
        self.assertEqual(options[1:3], ["--changelist=--message=label", "--message=中文提交 🚀"])
        self.assertEqual(command.call_args.kwargs, {"_message_index": 2})
        self.assertEqual(targets, ["file.txt"])

    @unittest.skipUnless(os.name == "nt", "Windows native argv encoding")
    def test_unicode_commit_message_file_direct_and_targets_cleanup(self):
        names = [f"ordinary-long-filename-{index:03d}.txt" for index in range(30)]
        members = [StatusEntry(name, "modified", changelist="fix") for name in names]
        message = "中文提交\nemoji 🚀"
        for limit in (24000, 600):
            for outcome in ("success", "failure", "start_error", "timeout"):
                files, mutations = [], []
                def run(argv, **kwargs):
                    if "--targets" in argv:
                        targets = Path(argv[argv.index("--targets") + 1])
                        files.append(targets)
                        self.assertNotIn(message.encode("utf-8"), targets.read_bytes())
                    if argv[1] == "info":
                        root = ET.Element("info")
                        for name in names:
                            ET.SubElement(root, "entry", path=name)
                        return subprocess.CompletedProcess(argv, 0, ET.tostring(root), b"")
                    mutations.append(argv)
                    temporary = Path(argv[argv.index("--file") + 1])
                    files.append(temporary)
                    self.assertEqual(temporary.read_bytes(), message.encode("utf-8"))
                    self.assertNotIn(message, argv)
                    self.assertEqual(argv[argv.index("--encoding") + 1], "UTF-8")
                    if outcome == "start_error":
                        raise OSError("synthetic process-start failure")
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(argv, 1)
                    return subprocess.CompletedProcess(argv, int(outcome == "failure"), b"", b"synthetic failure")
                with self.subTest(limit=limit, outcome=outcome), \
                        patch.object(self.client, "preflight_changelist_commit", return_value=members), \
                        patch("svn_client.backend._DIRECT_TARGET_LIMIT", limit), \
                        patch("svn_client.backend.subprocess.run", side_effect=run) as execute:
                    if outcome == "success":
                        result = self.client.commit_changelist("fix", message)
                        self.assertNotIn(message, str(result.args))
                    else:
                        with self.assertRaises(SvnError):
                            self.client.commit_changelist("fix", message)
                    self.assertEqual(execute.call_count, 2 if limit == 600 else 1)
                self.assertEqual(len(mutations), 1)
                self.assertTrue(files)
                self.assertTrue(all(not path.exists() for path in files))

    def test_stale_membership_refused_before_mutation(self):
        with patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "membership changed"):
                self.commit(expected=["clean.txt"])
            command.assert_not_called()

    def test_unknown_empty_clean_or_invalid_commit_refused(self):
        with patch.object(self.client, "changelists", return_value={}), patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "no members"):
                self.client.commit_changelist("missing", "message")
            command.assert_not_called()
        with patch.object(self.client, "changelists", return_value={"clean": [StatusEntry("f", "normal", changelist="clean")]}):
            with self.assertRaisesRegex(ValueError, "no modified"):
                self.client.commit_changelist("clean", "message")
        for message in ("", "   ", None):
            with self.assertRaises(ValueError):
                self.client.commit_changelist("fix", message)

    def test_property_only_modification_is_committable(self):
        with patch.object(self.client, "changelists", return_value={"fix": [StatusEntry("f", "normal", props="modified", changelist="fix")]}):
            argv = self.run_mutation(self.client.commit_changelist, "fix", "property fix")
        self.assertEqual(argv[-2:], ["--", "f"])

    def test_public_path_validation_returns_raw_names_without_mutation(self):
        with patch.object(self.client, "status", return_value=[]), patch.object(self.client, "command") as command:
            result = self.client.validate_changelist_paths(["a@b.txt", "ends@", "a@b.txt", self.root / "plain.txt"])
        self.assertEqual(result, ["a@b.txt", "ends@", "plain.txt"])
        command.assert_not_called()

    def test_public_commit_preflight_is_read_only_and_includes_clean(self):
        members = [StatusEntry("f", "modified", changelist="fix"), StatusEntry("clean", "normal", changelist="fix")]
        with patch.object(self.client, "changelists", return_value={"fix": members}), patch.object(
            self.client, "status", return_value=members
        ), patch.object(self.client, "command") as command:
            result = self.client.preflight_changelist_commit("fix", expected_paths=["clean", "f"])
        self.assertEqual(result, members)
        command.assert_not_called()

    def test_preflight_rejects_conflicts_missing_and_obstructed_members(self):
        members = [StatusEntry("f", "conflicted", changelist="fix"),
                   StatusEntry("f", "missing", changelist="fix"),
                   StatusEntry("f", "obstructed", changelist="fix"),
                   StatusEntry("f", "normal", props="conflicted", changelist="fix")]
        with patch.object(self.client, "command") as command:
            for entry in members:
                with self.subTest(entry=entry), patch.object(self.client, "changelists", return_value={"fix": [entry]}):
                    with self.assertRaisesRegex(ValueError, "Resolve"):
                        self.client.preflight_changelist_commit("fix")
            command.assert_not_called()

    def test_preflight_rejects_directory_dependency(self):
        members = [StatusEntry("newdir/file.txt", "added", changelist="fix")]
        with patch.object(self.client, "changelists", return_value={"fix": members}), patch.object(
            self.client, "status", return_value=[*members, StatusEntry("newdir", "added")]
        ), patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "depends on"):
                self.client.preflight_changelist_commit("fix")
            command.assert_not_called()

    def test_preflight_requires_moved_counterpart_in_same_reviewed_group(self):
        members = [StatusEntry("new.txt", "added", changelist="fix", moved_from="old.txt")]
        with patch.object(self.client, "changelists", return_value={"fix": members}), patch.object(
            self.client, "status", return_value=members
        ), patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "both source and destination"):
                self.client.preflight_changelist_commit("fix")
            command.assert_not_called()
        members.append(StatusEntry("old.txt", "deleted", changelist="fix", moved_to="new.txt"))
        with patch.object(self.client, "changelists", return_value={"fix": members}), patch.object(self.client, "status", return_value=members):
            self.assertEqual(self.client.preflight_changelist_commit("fix"), members)

    def test_preflight_ignores_unrelated_outside_symlink_status(self):
        members = [StatusEntry("f", "modified", changelist="fix")]
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "unrelated-link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks is not supported")
            with patch.object(self.client, "changelists", return_value={"fix": members}), patch.object(
                self.client, "status", return_value=[*members, StatusEntry("unrelated-link", "normal")]
            ):
                self.assertEqual(self.client.preflight_changelist_commit("fix"), members)

    def test_listing_metadata_does_not_follow_unrelated_named_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "linked").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks is not supported")
            entries = [StatusEntry("linked", "normal", changelist="other"), StatusEntry("f", "modified", changelist="fix")]
            with patch.object(self.client, "status", return_value=entries):
                self.assertEqual(set(self.client.changelists()), {"other", "fix"})

    def test_validation_ignores_unrelated_external_link(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "linked").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks is not supported")
            with patch.object(self.client, "status", return_value=[StatusEntry("linked", "external", file_external=True)]):
                self.assertEqual(self.client.validate_changelist_paths(["f"]), ["f"])


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin required for integration")
class ChangelistIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.wc = self.root / "wc"
        subprocess.run(["svnadmin", "create", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["svn", "checkout", self.repo.as_uri(), str(self.wc)], check=True, capture_output=True)
        self.client = SvnClient(self.wc)
        self.names = ["中文 @ file.txt", "other.txt"]
        for name in self.names:
            (self.wc / name).write_text("base\n", encoding="utf-8")
        self.client.add(self.names)
        self.client.commit("base")
        self.client.update()

    def test_assign_reassign_remove_and_clean_members(self):
        self.client.assign_changelist("任务 甲", self.names)
        self.assertEqual(len(self.client.changelists()["任务 甲"]), 2)
        self.client.assign_changelist("任务 乙", [self.names[0]])
        groups = self.client.changelists()
        self.assertEqual([e.path for e in groups["任务 乙"]], [self.names[0]])
        self.assertEqual([e.path for e in groups["任务 甲"]], [self.names[1]])
        self.client.remove_changelist(self.names)
        self.assertEqual(self.client.changelists(), {})

    def test_option_looking_name_is_a_label_not_an_option(self):
        self.client.assign_changelist("--password", [self.names[0]])
        (self.wc / self.names[0]).write_text("changed\n", encoding="utf-8")
        self.client.commit_changelist("--password", "literal label")
        self.assertEqual(self.client.changelists(), {})

    def test_added_and_deleted_file_members(self):
        new = self.wc / "added.txt"
        new.write_text("added\n", encoding="utf-8")
        self.client.add([new.name])
        deleted = self.names[1]
        self.client.command(["delete", "--", deleted])
        self.client.assign_changelist("file changes", [new.name, deleted])
        self.assertEqual({entry.item for entry in self.client.changelists()["file changes"]}, {"added", "deleted"})
        self.client.commit_changelist("file changes", "add and delete")
        self.assertEqual(self.client.changelists(), {})
        self.assertEqual(self.client.status(), [])

    def test_targets_file_roundtrip_for_literal_paths(self):
        names = [*self.names, *[f"ordinary-long-filename-{number:03d}.txt" for number in range(30)]]
        for name in names[2:]:
            (self.wc / name).write_text("base\n", encoding="utf-8")
        self.client.add(names[2:])
        self.client.commit("more files")
        self.client.update()
        with patch("svn_client.backend._DIRECT_TARGET_LIMIT", 500):
            self.client.assign_changelist("large group", names)
            self.assertEqual(len(self.client.changelists()["large group"]), len(names))
            for name in names:
                (self.wc / name).write_text("changed\n", encoding="utf-8")
            self.client.commit_changelist("large group", "中文批量提交 🚀", expected_paths=names)
        self.assertEqual(self.client.changelists(), {})
        self.assertEqual(self.client.log(limit=1)[0].message, "中文批量提交 🚀")

    def test_commit_only_one_list_and_default_clears_label(self):
        for name in self.names:
            (self.wc / name).write_text("modified\n", encoding="utf-8")
        self.client.assign_changelist("任务 甲", [self.names[0]])
        self.client.assign_changelist("任务 乙", [self.names[1]])
        self.client.commit_changelist("任务 甲", "只提交第一组 🚀", expected_paths=[self.names[0]])
        self.assertEqual(self.client.log(limit=1)[0].message, "只提交第一组 🚀")
        changes = self.client.status()
        self.assertEqual([(entry.path, entry.item) for entry in changes], [(self.names[1], "modified")])
        self.assertNotIn("任务 甲", self.client.changelists())
        self.assertIn("任务 乙", self.client.changelists())

    def test_commit_keep_changelist_keeps_clean_assignment(self):
        name = self.names[0]
        (self.wc / name).write_text("modified\n", encoding="utf-8")
        self.client.assign_changelist("keep", [name])
        self.client.commit_changelist("keep", "keep group", keep_changelist=True)
        entries = self.client.changelists()["keep"]
        self.assertEqual(entries[0].item, "normal")

    def test_stale_membership_does_not_commit(self):
        self.client.assign_changelist("fix", self.names)
        (self.wc / self.names[0]).write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "membership changed"):
            self.client.commit_changelist("fix", "must not commit", expected_paths=[self.names[0]])
        self.assertEqual(self.client.log(target=self.repo.as_uri())[0].message, "base")

    def test_subdirectory_scope_excludes_same_named_sibling_group(self):
        (self.wc / "sub").mkdir()
        (self.wc / "sub" / "f.txt").write_text("base\n", encoding="utf-8")
        self.client.add(["sub"])
        self.client.commit("subdir")
        self.client.update()
        self.client.assign_changelist("same", [self.names[0], "sub/f.txt"])
        (self.wc / self.names[0]).write_text("outside\n", encoding="utf-8")
        (self.wc / "sub" / "f.txt").write_text("inside\n", encoding="utf-8")
        subclient = SvnClient(self.wc / "sub")
        subclient.commit_changelist("same", "inside only", expected_paths=["f.txt"])
        self.assertEqual([(entry.path, entry.item) for entry in self.client.status()], [(self.names[0], "modified")])


class TargetFileUnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            self.client = SvnClient(self.directory.name)
        self.names = [f"ordinary-long-filename-{index:03d}.txt" for index in range(20)]
        self.files = []
        self.payloads = []
        self.commands = []
        self.limit = patch("svn_client.backend._DIRECT_TARGET_LIMIT", 450)
        self.limit.start()
        self.addCleanup(self.limit.stop)

    def response(self, args, *, mismatch=False, fail=False):
        self.commands.append(args)
        filename = Path(args[args.index("--targets") + 1])
        self.files.append(filename)
        self.assertTrue(filename.exists())
        self.payloads.append(filename.read_bytes())
        if args[0] == "info":
            root = ET.Element("info")
            encoding = "utf-8"
            for line in filename.read_text(encoding=encoding).splitlines():
                raw = line[2:]
                if raw.endswith("@"):
                    raw = raw[:-1]
                ET.SubElement(root, "entry", path="wrong-file.txt" if mismatch else raw)
            return CommandResult((), 0, ET.tostring(root, encoding="unicode"), "")
        if fail:
            raise SvnError("simulated command failure")
        return CommandResult((), 0, "done", "")

    def run_targets(self, names, **kwargs):
        targets = self.client._local_targets(names)
        with patch("svn_client.backend.locale.getencoding", return_value="utf-8", create=True), patch.object(
            self.client, "command", side_effect=lambda args: self.response(args, **kwargs)
        ):
            return self.client._command_with_targets(["commit", "--changelist=fix", "--message=PRIVATE MESSAGE"], targets)

    def test_large_scope_private_file_cleanup_and_no_message_in_file(self):
        self.run_targets(self.names)
        self.assertEqual([args[0] for args in self.commands], ["info", "commit"])
        self.assertTrue(all(not filename.exists() for filename in self.files))
        self.assertNotIn(b"PRIVATE MESSAGE", self.payloads[0])
        self.assertNotIn(b"--changelist", self.payloads[0])
        self.assertEqual(len(self.payloads[0].splitlines()), len(self.names))
        self.assertNotIn(".", self.commands[-1])

    def test_marked_message_position_survives_direct_and_targets_routes(self):
        targets = self.client._local_targets(self.names)
        options = ["commit", "--changelist=fix", "--message=中文提交"]
        def command(args, **kwargs):
            if "--targets" in args:
                return self.response(args)
            return CommandResult((), 0, "done", "")
        for limit in (24000, 450):
            with self.subTest(limit=limit), \
                    patch("svn_client.backend._DIRECT_TARGET_LIMIT", limit), \
                    patch("svn_client.backend.locale.getencoding", return_value="utf-8", create=True), \
                    patch.object(self.client, "command", side_effect=command) as execute:
                self.client._command_with_targets(options, targets, _message_index=2)
                self.assertEqual(execute.call_count, 2 if limit == 450 else 1)
                self.assertEqual(execute.call_args.kwargs, {"_message_index": 2})
                self.assertEqual(execute.call_args.args[0][2], options[2])
                if limit == 450:
                    self.assertEqual(execute.call_args_list[0].kwargs, {})

    def test_spaces_unicode_at_and_leading_dash_roundtrip(self):
        names = [*self.names, " leading.txt", "中文 @ file.txt", "--password"]
        if os.name != "nt":
            names.append("trailing.txt ")
        self.run_targets(names)
        payload = self.payloads[0].decode("utf-8")
        self.assertIn("./ leading.txt@\n", payload)
        if os.name != "nt":
            self.assertIn("./trailing.txt @\n", payload)
        self.assertIn("./中文 @ file.txt@\n", payload)
        self.assertIn("./--password@\n", payload)

    def test_newline_paths_stay_literal_on_argv(self):
        self.run_targets([*self.names, "line\nbreak.txt", "carriage\rreturn.txt"])
        self.assertNotIn(b"line\nbreak", self.payloads[0])
        self.assertEqual(self.commands[-1][-2:], ["line\nbreak.txt", "carriage\rreturn.txt"])

    def test_decode_mismatch_never_mutates_and_cleans_up(self):
        with self.assertRaisesRegex(SvnError, "decoding changed"):
            self.run_targets(self.names, mismatch=True)
        self.assertEqual([args[0] for args in self.commands], ["info"])
        self.assertTrue(all(not filename.exists() for filename in self.files))

    def test_operation_failure_still_cleans_up(self):
        with self.assertRaisesRegex(SvnError, "simulated"):
            self.run_targets(self.names, fail=True)
        self.assertTrue(all(not filename.exists() for filename in self.files))

    def test_no_safe_targets_refused_without_any_command(self):
        with patch.object(self.client, "command") as command:
            with self.assertRaisesRegex(ValueError, "too large"):
                self.client._command_with_targets(["commit"], ["line\nbreak" * 100])
            command.assert_not_called()

    def test_unencodable_paths_stay_on_argv_without_replacement(self):
        with patch("svn_client.backend.locale.getencoding", return_value="ascii", create=True), patch.object(
            self.client, "command", side_effect=self.response
        ):
            self.client._command_with_targets(["commit"], self.client._local_targets([*self.names, "中文.txt"]))
        self.assertEqual(self.commands[-1][-1], "中文.txt")
        self.assertNotIn(b"?", self.payloads[0])

    def test_credential_url_never_written_or_run(self):
        with patch.object(self.client, "command") as command, patch("svn_client.backend.tempfile.TemporaryDirectory") as temporary:
            with self.assertRaisesRegex(ValueError, "Passwords"):
                self.client._command_with_targets(["commit"], [*self.names, "unknown://user:secret@example.test/file"])
            command.assert_not_called()
            temporary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
