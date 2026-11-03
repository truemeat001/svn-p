"""Backend unit tests plus optional real-SVN integration tests."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from svn_client.backend import CommandResult, SvnClient, SvnError, _validate_executable, discover_svn, redact_credentials


STATUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<status><target path=".">
<entry path="中文 @ file.txt"><wc-status item="modified" props="normal" revision="5">
<commit revision="4"><author>甲</author></commit></wc-status></entry>
<entry path="renamed.txt"><wc-status item="added" props="none" copied="true" moved-from="old.txt"/></entry>
<entry path="conflict"><wc-status item="normal" props="conflicted" tree-conflicted="true" revision="6"/></entry>
<entry path="external"><wc-status item="external" props="none"/></entry>
</target><changelist name="fixes"><entry path="a.txt"><wc-status item="modified" props="none" revision="2"/></entry></changelist></status>"""

INFO_XML = """<info><entry path="." kind="dir" revision="7"><url>file:///repo/trunk</url>
<relative-url>^/trunk</relative-url><repository><root>file:///repo</root><uuid>123</uuid></repository>
<wc-info><wcroot-abspath>/tmp/wc</wcroot-abspath><schedule>normal</schedule><depth>infinity</depth></wc-info>
<commit revision="6"><author>测试</author><date>2026-01-01T00:00:00Z</date></commit></entry></info>"""

LOG_XML = """<log><logentry revision="7"><author>作者</author><date>2026-01-01T00:00:00Z</date>
<paths><path action="A" kind="file" copyfrom-path="/old" copyfrom-rev="6">/新文件</path></paths>
<msg>提交 &amp; review\nsecond line</msg></logentry><logentry revision="0"/></log>"""

LIST_XML = """<lists><list path="file:///repo"><entry kind="file"><name>中文.txt</name><size>42</size>
<commit revision="3"><author>alice</author><date>today</date></commit></entry>
<entry kind="dir"><name>folder</name><commit revision="2"/></entry></list></lists>"""


class BackendUnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.discovery = patch("svn_client.backend.discover_svn", return_value="/trusted/svn")
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.client = SvnClient(self.directory.name)

    def result(self, output="", code=0, stderr=""):
        return subprocess.CompletedProcess([], code, output.encode("utf-8"), stderr.encode("utf-8"))

    def test_no_shell_unicode_and_noninteractive(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result("成功")) as run:
            result = self.client.command(["status", "--", "文件;$(echo bad).txt"])
        self.assertEqual(result.stdout, "成功")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/trusted/svn", "status", "--non-interactive", "--no-auth-cache", "--", "文件;$(echo bad).txt"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], Path(self.directory.name))

    def test_status_xml_metadata(self):
        with patch.object(self.client, "command", return_value=CommandResult((), 0, STATUS_XML, "")):
            entries = self.client.status()
        self.assertEqual(entries[0].path, "中文 @ file.txt")
        self.assertEqual(entries[0].revision, 5)
        self.assertEqual(entries[0].last_changed_author, "甲")
        self.assertEqual(entries[1].moved_from, "old.txt")
        self.assertTrue(entries[1].copied)
        self.assertTrue(entries[2].conflicted)
        self.assertEqual(entries[3].item, "external")
        self.assertEqual(entries[4].changelist, "fixes")

    def test_info_xml(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result(INFO_XML)):
            result = self.client.info()
        self.assertEqual(result["repository_uuid"], "123")
        self.assertEqual(result["last_changed_revision"], 6)
        self.assertEqual(result["last_changed_author"], "测试")
        self.assertEqual(result["wc_root"], "/tmp/wc")

    def test_log_xml(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result(LOG_XML)):
            results = self.client.log()
        self.assertEqual(results[0].message, "提交 & review\nsecond line")
        self.assertEqual(results[0].changed_paths[0]["copyfrom_rev"], 6)
        self.assertEqual(results[1].message, "")

    def test_list_xml(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result(LIST_XML)):
            entries = self.client.list_entries()
        self.assertEqual(entries[0].size, 42)
        self.assertEqual(entries[0].name, "中文.txt")
        self.assertIsNone(entries[1].size)

    def test_local_target_escaping(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result("diff")) as run:
            self.client.diff(["-danger", "a@b.txt", "ends@", Path(self.directory.name) / "file"])
        argv = run.call_args.args[0]
        self.assertIn("--internal-diff", argv)
        self.assertEqual(argv[-5:], ["--", "-danger", "a@b.txt@", "ends@@", "file"])

    def test_url_peg_is_preserved(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result(LOG_XML)) as run:
            self.client.log(target="https://svn.example.test/project@7")
        self.assertEqual(run.call_args.args[0][-1], "https://svn.example.test/project@7")

    def test_empty_selection_never_mutates_whole_wc(self):
        with patch("svn_client.backend.subprocess.run") as run:
            for action in (lambda: self.client.revert([]), lambda: self.client.commit("message", []),
                           lambda: self.client.add([]), lambda: self.client.update([])):
                with self.assertRaises(ValueError):
                    action()
            run.assert_not_called()

    def test_paths_cannot_escape(self):
        for target in ("../outside", "/outside", "https://svn.example.test/repo"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.client.add([target])

    def test_symlink_ancestor_cannot_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            link = Path(self.directory.name) / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks is not supported in this environment")
            with self.assertRaises(ValueError):
                self.client.add(["link/child.txt"])

    def test_passwords_and_external_commands_rejected(self):
        commands = [
            ["info", "--password", "secret"], ["info", "--password=secret"],
            ["info", "https://person:secret@example.test/repo"],
            ["info", "--trust-server-cert"], ["info", "--trust-server-cert-failures=unknown-ca"],
            ["diff", "--diff-cmd", "evil"], ["propedit", "x"],
            ["update", "--accept", "launch"], ["update", "--accept=edit"],
            ["info", "--config-option", "config:helpers:editor-cmd=evil"],
        ]
        with patch("svn_client.backend.subprocess.run") as run:
            for command in commands:
                with self.subTest(command=command), self.assertRaises(ValueError):
                    self.client.command(command)
            run.assert_not_called()

    def test_invalid_argument_types_rejected(self):
        for args in ("status", [], ["--password"], ["status", "bad\0arg"], ["status", None]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.client.command(args)

    def test_literal_file_named_password_is_allowed_after_separator(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result()) as run:
            self.client.add(["--password"])
        self.assertEqual(run.call_args.args[0][-2:], ["--", "--password"])

    def test_empty_commit_message_rejected(self):
        with self.assertRaises(ValueError):
            self.client.commit(" \n")

    def test_message_hidden_from_result_arguments(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result()) as run:
            result = self.client.commit("Private working note")
        self.assertNotIn("Private working note", result.args)
        self.assertIn("Private working note", run.call_args.args[0])

    def test_error_has_result_and_redacts_url(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result(code=1, stderr="https://user:secret@example.test failed")):
            with self.assertRaises(SvnError) as caught:
                self.client.command(["info"])
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("secret", caught.exception.result.stderr)
        self.assertEqual(caught.exception.result.returncode, 1)

    def test_timeout_explicitly_reports_uncertain_no_retry(self):
        with patch("svn_client.backend.subprocess.run", side_effect=subprocess.TimeoutExpired(["svn"], 1)) as run:
            with self.assertRaisesRegex(SvnError, "uncertain"):
                self.client.commit("msg")
        self.assertEqual(run.call_count, 1)

    def test_process_start_error_wrapped(self):
        with patch("svn_client.backend.subprocess.run", side_effect=OSError("no executable")):
            with self.assertRaisesRegex(SvnError, "Could not start"):
                self.client.command(["status"])

    def test_invalid_xml_reported(self):
        with patch.object(self.client, "command", return_value=CommandResult((), 0, "not XML", "")):
            with self.assertRaisesRegex(SvnError, "invalid XML"):
                self.client.status()

    def test_missing_info_reported(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result("<info/>")):
            with self.assertRaises(SvnError):
                self.client.info()

    def test_version_uses_global_option(self):
        with patch("svn_client.backend.subprocess.run", return_value=self.result("1.14.5\n")) as run:
            self.assertEqual(self.client.version(), "1.14.5")
        self.assertEqual(run.call_args.args[0], ["/trusted/svn", "--version", "--quiet"])

    def test_subdirectory_clients_share_lock(self):
        root = Path(self.directory.name)
        (root / ".svn").mkdir()
        (root / "sub").mkdir()
        self.assertIs(SvnClient(root)._lock, SvnClient(root / "sub")._lock)

    def test_limits_and_timeout_checked(self):
        for limit in (0, -1, True, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.client.log(limit=limit)
        with self.assertRaises(ValueError):
            self.client.command(["status"], timeout=-1)

    def test_native_encoding_fallback(self):
        with patch("svn_client.backend.locale.getencoding", return_value="cp1252", create=True):
            self.assertEqual(self.client._decode(b"caf\xe9"), "café")


class DiscoveryTests(unittest.TestCase):
    def test_explicit_missing_executable_does_not_fall_back(self):
        with patch("svn_client.backend.shutil.which", return_value=None):
            with self.assertRaises(SvnError):
                discover_svn("/definitely/not/a/program")

    def test_windows_rejects_batch_wrappers(self):
        with patch("svn_client.backend.os.name", "nt"):
            for path in ("svn.cmd", "svn.bat", "svn.py"):
                with self.subTest(path=path), self.assertRaises(SvnError):
                    _validate_executable(path)
            self.assertEqual(_validate_executable("svn.EXE"), "svn.EXE")


class CredentialRedactionTests(unittest.TestCase):
    def test_basic_unicode_empty_username_and_last_at(self):
        cases = {
            "https://user:secret@example.test/path": "https://user:***@example.test/path",
            "https://:secret@example.test": "https://:***@example.test",
            "https://用户:密码@example.test": "https://用户:***@example.test",
            "https://user:secret@more@example.test": "https://user:***@example.test",
            "https://user@example.org:secret@host.test": "https://user@example.org:***@host.test",
            "https://user:@example.test": "https://user:***@example.test",
            "unknown+scheme://u:pass@host": "unknown+scheme://u:***@host",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(redact_credentials(source), expected)

    def test_query_path_and_nonpassword_authorities_preserved(self):
        for source in ("https://example.test:443/path", "https://user@example.test/path", "https://[::1]:443/path",
                       "https://example.test/path:a@b", "https://example.test?email=a:b@c",
                       "https://example.test#email=a:b@c"):
            with self.subTest(source=source):
                self.assertEqual(redact_credentials(source), source)

    def test_adjacent_urls_are_both_redacted(self):
        source = "https://u:first@onehttps://v:second@two"
        self.assertEqual(redact_credentials(source), "https://u:***@onehttps://v:***@two")

    def test_megabyte_without_url_and_with_late_url(self):
        # Large enough to expose the old quadratic scheme regex without a
        # fragile wall-clock assertion that depends on CI machine load.
        prefix = "a" * 1_000_000
        self.assertEqual(redact_credentials(prefix), prefix)
        self.assertEqual(redact_credentials(prefix + " https://user:secret@host/path"),
                         prefix + " https://user:***@host/path")

    def test_long_malformed_authority_and_many_markers(self):
        value = "https://user:" + ":" * 250_000
        self.assertEqual(redact_credentials(value), value)
        value = "https://host-without-password/" * 10_000 + "https://user:secret@host"
        self.assertEqual(redact_credentials(value), value[:-len("https://user:secret@host")] + "https://user:***@host")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin required for integration")
class BackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.wc = self.root / "工作 副本"
        subprocess.run(["svnadmin", "create", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["svn", "checkout", self.repo.as_uri(), str(self.wc)], check=True, capture_output=True)
        self.client = SvnClient(self.wc)

    def test_add_commit_status_diff_log_revert_update_list(self):
        file = self.wc / "中文 @ name.txt"
        file.write_text("first\n", encoding="utf-8")
        self.client.add([file.name])
        self.assertEqual(self.client.status()[0].item, "added")
        self.client.commit("初次提交", [file.name])
        self.assertEqual(self.client.status(), [])
        file.write_text("second\n", encoding="utf-8")
        self.assertEqual(self.client.status()[0].item, "modified")
        self.assertIn("+second", self.client.diff([file.name]))
        self.client.revert([file.name])
        self.assertEqual(file.read_text(encoding="utf-8"), "first\n")
        self.client.update()
        self.assertEqual(self.client.info()["revision"], 1)
        self.assertEqual(self.client.log()[0].message, "初次提交")
        self.assertEqual(self.client.list_entries()[0].name, file.name)
        self.assertTrue(self.client.version())

    def test_leading_option_filename(self):
        file = self.wc / "--password"
        file.write_text("content", encoding="utf-8")
        self.client.add([file.name])
        self.client.commit("literal filename", [file.name])
        self.assertEqual(self.client.status(), [])

    def test_conflict_is_reported_without_prompt(self):
        file = self.wc / "conflict.txt"
        file.write_text("first\n", encoding="utf-8")
        self.client.add([file.name])
        self.client.commit("base")
        second_wc = self.root / "other"
        subprocess.run(["svn", "checkout", self.repo.as_uri(), str(second_wc)], check=True, capture_output=True)
        other = SvnClient(second_wc)
        (second_wc / file.name).write_text("remote\n", encoding="utf-8")
        other.commit("remote")
        file.write_text("local\n", encoding="utf-8")
        self.client.update()
        self.assertTrue(any(entry.conflicted for entry in self.client.status()))


if __name__ == "__main__":
    unittest.main()
