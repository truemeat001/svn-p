"""Read-only history/compare tests, including bounded real process fixtures."""
from __future__ import annotations

import io
import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

from svn_client.backend import LogEntry, SvnClient
from svn_client.cli import main
from svn_client.history import (BoundedText, ComparisonResult, HistoryError, HistoryManager,
                                HistoryResult, MAX_DIFF_BYTES, MAX_HISTORY_BYTES,
                                _bounded_process, normalize_revision)


INFO = b'<info><entry kind="file"><url>https://example.test/repo/a.txt</url></entry></info>'
LOG = b'''<log>
<logentry revision="9"><author>alice</author><date>2026-11-03</date><msg>Fix a</msg>
<paths><path action="M" kind="file">/a.txt</path><path action="A" kind="file" copyfrom-path="/old.txt" copyfrom-rev="3">/copied.txt</path></paths></logentry>
<logentry revision="5"><author>bob</author><msg>Second</msg></logentry>
<logentry revision="2"><msg>Initial</msg></logentry></log>'''


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            self.client = SvnClient(self.workspace)
        self.manager = HistoryManager(self.client)

    def info_for(self, target):
        root = ET.Element("info")
        entry = ET.SubElement(root, "entry", kind="dir" if target == "." else "file")
        relative = target[:-1] if "@" in target else target
        ET.SubElement(entry, "url").text = "https://example.test/repo" + ("/" + quote(Path(relative).as_posix()) if relative != "." else "")
        repository = ET.SubElement(entry, "repository")
        ET.SubElement(repository, "uuid").text = "test-uuid"
        info = ET.SubElement(entry, "wc-info")
        ET.SubElement(info, "wcroot-abspath").text = str(self.workspace)
        return ET.tostring(root)

    @contextmanager
    def responses(self, *responses):
        values = iter(responses)
        info_seen = False
        def respond(args, limit):
            nonlocal info_seen
            if args[0] == "info" and args[-1] == "." and info_seen:
                return self.info_for(".")
            value = next(values)
            if args[0] == "info":
                info_seen = True
                if value == INFO:
                    return self.info_for(args[-1])
            return value
        with patch.object(self.manager, "_run", side_effect=respond) as run:
            yield run

    def run_responses(self, *responses):
        context = self.responses(*responses)
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def test_path_history_paging_and_complete_commit_paths(self):
        run = self.run_responses(INFO, LOG)
        result = self.manager.history("a.txt", limit=2)
        self.assertEqual(result.path, "a.txt")
        self.assertEqual([entry.revision for entry in result.entries], [9, 5])
        self.assertTrue(result.has_more)
        self.assertEqual(result.next_start_revision, 4)
        self.assertEqual(result.entries[0].changed_paths[1]["copyfrom_rev"], 3)
        self.assertEqual(run.call_args.args,
                         (["log", "--xml", "--verbose", "--limit", "3", "--revision", "HEAD:0", "--", "a.txt"], MAX_HISTORY_BYTES))

    def test_end_of_history_and_older_cursor(self):
        self.run_responses(INFO, b'<log><logentry revision="2"><msg>Initial</msg></logentry></log>')
        result = self.manager.history(start_revision=4)
        self.assertFalse(result.has_more)
        self.assertIsNone(result.next_start_revision)
        self.assertEqual(result.start_revision, "4")

    def test_empty_history_is_valid(self):
        self.run_responses(INFO, b"<log/>")
        self.assertEqual(self.manager.history().entries, ())

    def test_all_revision_pair_commands(self):
        for left in ("BASE", "HEAD", "3"):
            for right in ("WORKING", "BASE", "HEAD", "7"):
                with self.subTest(left=left, right=right):
                    with self.responses(INFO, b"diff") as run:
                        result = self.manager.compare("中文 @.txt", from_revision=left, to_revision=right)
                    expected = left if right == "WORKING" else left + ":" + right
                    self.assertEqual(run.call_args.args,
                                     (["diff", "--old=中文 @.txt@", "--new=中文 @.txt@", "--revision", expected], MAX_DIFF_BYTES))
                    self.assertEqual(result.path, "中文 @.txt")
                    self.assertFalse(result.binary_payloads_included)

    def test_literal_option_and_peg_names_remain_targets(self):
        for target, escaped in (("--password", "--password"), ("ends@", "ends@@"), ("name@4", "name@4@")):
            with self.responses(INFO, b"") as run:
                self.manager.compare(target)
            self.assertEqual(run.call_args.args[0][1:3], ["--old=" + target + "@", "--new=" + target + "@"])

    def test_path_and_revision_validation_precedes_subprocess(self):
        with patch.object(self.manager, "_run") as run:
            for path in ("../escape", "/outside", "https://user:secret@host/repo", ".svn/wc.db", "a/.SVN/b", ""):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.manager.history(path)
            for value in ("HEAD:3", "{today}", "--password", "-1", "WORKING", True, "9" * 30):
                with self.subTest(revision=value), self.assertRaises(ValueError):
                    self.manager.compare("a", from_revision=value)
            for limit in (0, -1, 1001, True, "10"):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    self.manager.history(limit=limit)
            for limit in (0, True, MAX_DIFF_BYTES + 1):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    self.manager.compare("a", max_text_bytes=limit)
            run.assert_not_called()

    def test_symlink_inside_workspace_rejected(self):
        (self.workspace / "real").write_text("x")
        try:
            (self.workspace / "alias").symlink_to(self.workspace / "real")
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.manager.history("alias")

    def test_info_transport_and_credentials_fail_closed(self):
        for url in ("svn+ssh://host/repo", "https://user:SECRET@host/repo", "https://host/repo?token=SECRET", "bad"):
            with patch.object(self.manager, "_run", return_value=(f'<info><entry kind="file"><url>{url}</url></entry></info>').encode()) as run:
                with self.assertRaises(ValueError) as caught:
                    self.manager.compare("a")
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(run.call_count, 1)

    def test_invalid_or_unsafe_xml_refused(self):
        declaration = '<!DOCTYPE info [<!ENTITY x "secret">]><info>&x;</info>'
        for info in (b"garbage", b"<info/>", declaration.encode(), declaration.encode("utf-16")):
            self.run_responses(info)
            with self.assertRaises(HistoryError):
                self.manager.history("a")

    def test_utf16_declaration_refused_before_xml_parser(self):
        raw = '<!DOCTYPE info [<!ENTITY x "secret">]><info>&x;</info>'.encode("utf-16")
        with patch.object(self.manager, "_run", return_value=raw), patch("svn_client.history.ET.fromstring") as parser:
            with self.assertRaises(HistoryError):
                self.manager._xml(["info", "--xml", "--", "."], 1024)
            parser.assert_not_called()

    def test_wrong_root_order_excess_count_or_revision_refused(self):
        for log in (b"<status/>", b'<log><logentry revision="no"/></log>',
                    b'<log><logentry revision="2"/><logentry revision="2"/></log>',
                    b'<log><logentry revision="1"/><logentry revision="2"/></log>',
                    b'<log><logentry revision="9"/><logentry revision="8"/><logentry revision="7"/></log>'):
            with self.responses(INFO, log):
                with self.assertRaises(HistoryError):
                    self.manager.history("a", limit=1)

    def test_redaction_precedes_utf8_truncation(self):
        self.run_responses(INFO, "中 https://person:SUPER-SECRET@example.test/repo".encode())
        result = self.manager.compare("a", max_text_bytes=23)
        self.assertTrue(result.diff.truncated)
        self.assertNotIn("SUPER", result.diff.text)
        self.assertEqual(result.diff.returned_utf8_bytes, len(result.diff.text.encode()))
        self.assertEqual(result.diff.source_utf8_bytes, len("中 https://person:***@example.test/repo".encode()))

    def test_history_metadata_redacted(self):
        self.run_responses(INFO, b'<log><logentry revision="2"><author>https://u:secret@host</author><msg>svn://u:pw@host/x</msg></logentry></log>')
        result = self.manager.history()
        self.assertEqual(result.entries[0].author, "https://u:***@host")
        self.assertEqual(result.entries[0].message, "svn://u:***@host/x")

    def test_argv_is_read_only_noninteractive_internal_diff(self):
        with patch("svn_client.history._bounded_process", return_value=b"ok") as run:
            self.manager._run(["diff", "--revision", "BASE", "--", "a"], 50)
        self.assertEqual(run.call_args.args[0], ["/trusted/svn", "diff", "--non-interactive", "--no-auth-cache", "--internal-diff", "--revision", "BASE", "--", "a"])
        self.assertEqual(run.call_args.kwargs["stdout_limit"], 50)
        with self.assertRaises(ValueError):
            self.manager._run(["commit", "--", "a"], 50)

    def test_revision_normalization(self):
        self.assertEqual(normalize_revision(" head "), "HEAD")
        self.assertEqual(normalize_revision("0009"), "9")
        self.assertEqual(normalize_revision(0), "0")
        self.assertEqual(normalize_revision("working", working=True), "WORKING")

    def test_nested_switched_and_different_repository_targets_refused(self):
        for field, replacement in (("wc-info/wcroot-abspath", str(self.workspace / "nested")),
                                   ("repository/uuid", "different-repository"),
                                   ("url", "https://example.test/repo/other-branch/a")):
            target = ET.fromstring(self.info_for("a"))
            target.find("entry/" + field).text = replacement
            with self.subTest(field=field), self.responses(ET.tostring(target)) as run:
                with self.assertRaises(ValueError):
                    self.manager.history("a")
                self.assertEqual([call.args[0][0] for call in run.call_args_list], ["info", "info"])

    def test_info_requires_exactly_one_entry_and_identity(self):
        for payload in (b'<info><entry kind="file"/><entry kind="file"/></info>',
                        b'<info><entry kind="file"><url>https://example.test/repo/a</url></entry></info>'):
            with self.responses(payload):
                with self.assertRaises(HistoryError):
                    self.manager.compare("a")

    def test_missing_target_never_retries_workspace_root(self):
        with patch.object(self.manager, "_run", side_effect=HistoryError("missing")) as run:
            with self.assertRaises(HistoryError):
                self.manager.history("missing.txt")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], "missing.txt")

    def test_subdirectory_workspace_allows_real_wcroot_ancestor(self):
        selected = self.workspace / "sub"
        selected.mkdir()
        self.client.working_copy = selected
        root_info = ET.fromstring(self.info_for("."))
        root_info.find("entry/url").text = "https://example.test/repo/sub"
        target_info = ET.fromstring(self.info_for("a"))
        target_info.find("entry/url").text = "https://example.test/repo/sub/a"
        with patch.object(self.manager, "_run", side_effect=[ET.tostring(target_info), ET.tostring(root_info), b"<log/>"]):
            self.assertEqual(self.manager.history("a").path, "a")

    def test_oversized_timeout_is_validation_error_without_process(self):
        self.client.timeout = 10**400
        with patch("svn_client.history._bounded_process") as run:
            with self.assertRaises(ValueError):
                self.manager._run(["info", "--", "."], 20)
            run.assert_not_called()


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cwd = Path(directory.name)

    def run_script(self, script, limit=1024, timeout=4):
        return _bounded_process([sys.executable, "-c", script], cwd=self.cwd,
                                timeout=timeout, stdout_limit=limit)

    def test_small_output_and_exact_limit(self):
        self.assertEqual(self.run_script("import sys; sys.stdout.buffer.write(b'x' * 1024)"), b"x" * 1024)

    def test_oversized_stdout_never_returns_password_fragment(self):
        with self.assertRaisesRegex(HistoryError, "capture limit") as caught:
            self.run_script("import sys; sys.stdout.write('https://u:' + 'SECRET' * 100000 + '@host')")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_oversized_stderr_does_not_deadlock(self):
        with self.assertRaisesRegex(HistoryError, "capture limit"):
            self.run_script("import sys; sys.stderr.buffer.write(b'x' * 1000000); sys.stdout.write('ok')")

    def test_both_streams_drained_then_diagnostics_reject_success(self):
        # Both pipes still drain without deadlock, but zero-exit diagnostics
        # are now fail-closed instead of presenting stdout as a complete read.
        with self.assertRaisesRegex(HistoryError, "diagnostics despite exit code zero"):
            self.run_script("import sys; sys.stderr.buffer.write(b'e' * 60000); sys.stdout.buffer.write(b'o' * 1000)")

    def test_timeout_reports_read_only_and_no_retry(self):
        with self.assertRaisesRegex(HistoryError, "No write was requested"):
            self.run_script("import time; time.sleep(20)", timeout=0.05)

    def test_delayed_reap_has_bounded_wait_and_returns_no_output(self):
        process = Mock(stdout=io.BytesIO(b"partial"), stderr=io.BytesIO())
        process.wait.side_effect = [subprocess.TimeoutExpired("svn", 1), subprocess.TimeoutExpired("svn", 2)]
        process.poll.return_value = None
        process.kill.side_effect = OSError("termination unavailable")
        with patch("svn_client.history.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(HistoryError, "cleanup could not be verified"):
                _bounded_process(["svn", "log"], cwd=self.cwd, timeout=1, stdout_limit=1024)
        self.assertEqual([call.kwargs for call in process.wait.call_args_list], [{"timeout": 1}, {"timeout": 2}])
        process.kill.assert_called_once()

    def test_errors_expose_only_exit_and_svn_codes(self):
        with self.assertRaises(HistoryError) as caught:
            self.run_script("import sys; sys.stderr.write('svn: E170013 https://u:SECRET@host token SECRET'); sys.exit(3)")
        self.assertIn("exit 3, E170013", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_no_files_created_by_reader(self):
        self.run_script("print('read')")
        self.assertEqual(list(self.cwd.iterdir()), [])


class HistoryCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.client = Mock()
        self.manager = Mock()
        self.manager.history.return_value = HistoryResult("a", "HEAD", (LogEntry(4, message="fix"),), True, 3)
        self.manager.compare.return_value = ComparisonResult("a", "BASE", "WORKING", BoundedText("diff", False, 4, 4))

    def invoke(self, args):
        output = io.StringIO()
        with patch("svn_client.cli.SvnClient", return_value=self.client), patch("svn_client.cli.HistoryManager", return_value=self.manager):
            code = main(["--workspace", str(self.workspace), *args], stdout=output, stdin=io.StringIO())
        return code, json.loads(output.getvalue())

    def test_history_structured_cursor(self):
        code, payload = self.invoke(["history", "--path", "a", "--limit", "10", "--start-revision", "8"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "read")
        self.assertEqual(payload["data"]["next_start_revision"], 3)
        self.manager.history.assert_called_once_with("a", limit=10, start_revision="8")
        self.client.command.assert_not_called()

    def test_compare_explicit_endpoints(self):
        code, payload = self.invoke(["compare", "--path", "a", "--from", "4", "--to", "HEAD"])
        self.assertEqual(code, 0)
        self.assertFalse(payload["data"]["binary_payloads_included"])
        self.manager.compare.assert_called_once_with("a", from_revision="4", to_revision="HEAD", max_text_bytes=262144)

    def test_write_flags_and_missing_compare_selection_refused(self):
        for args in (["history", "--yes"], ["compare"], ["compare", "--path", "a", "--dry-run"]):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(args)[0], 2)
        self.manager.history.assert_not_called()
        self.manager.compare.assert_not_called()

    def test_escaping_path_refused(self):
        self.assertEqual(self.invoke(["compare", "--path", "../escape"])[0], 4)
        self.manager.compare.assert_not_called()

    def test_capture_failure_is_structured_read_failure(self):
        self.manager.compare.side_effect = HistoryError("SVN output exceeded the bounded capture limit.")
        code, payload = self.invoke(["compare", "--path", "a"])
        self.assertEqual(code, 6)
        self.assertEqual(payload["outcome"], "failed")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin unavailable")
class RealHistoryTests(unittest.TestCase):
    def test_comparison_keeps_current_path_peg_across_a_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, workspace = root / "repo", root / "wc"
            subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repository.as_uri(), str(workspace)], check=True, capture_output=True)
            def svn(*args):
                return subprocess.run(["svn", *args], cwd=workspace, check=True, capture_output=True).stdout
            (workspace / "old.txt").write_bytes(b"before\n")
            svn("add", "old.txt")
            svn("commit", "-m", "initial")
            svn("move", "old.txt", "new.txt")
            (workspace / "new.txt").write_bytes(b"after\n")
            svn("commit", "-m", "rename")
            manager = HistoryManager(SvnClient(workspace))
            comparison = manager.compare("new.txt", from_revision="1", to_revision="HEAD")
            self.assertIn("-before", comparison.diff.text)
            self.assertIn("+after", comparison.diff.text)
            self.assertEqual(comparison.diff.text,
                             SvnClient._decode(svn("diff", "--internal-diff", "-r", "1:HEAD", "--", "new.txt")))

    def test_path_scope_revisions_working_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, workspace = root / "repo", root / "wc"
            subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
            subprocess.run(["svn", "checkout", repository.as_uri(), str(workspace)], check=True, capture_output=True)
            def svn(*args):
                return subprocess.run(["svn", *args], cwd=workspace, check=True, capture_output=True).stdout
            target = workspace / "中文 @.txt"
            target.write_text("one\n", encoding="utf-8")
            (workspace / "other.txt").write_text("other\n", encoding="utf-8")
            svn("add", "--", "中文 @.txt@", "other.txt")
            svn("commit", "-m", "initial")
            (workspace / "other.txt").write_text("changed\n", encoding="utf-8")
            svn("commit", "-m", "other only")
            target.write_text("two\n", encoding="utf-8")
            svn("commit", "-m", "target only")
            target.write_text("working\n", encoding="utf-8")
            manager = HistoryManager(SvnClient(workspace))
            before_status, before_content = svn("status", "--xml"), target.read_bytes()
            page = manager.history(target.name, limit=1)
            self.assertEqual([entry.revision for entry in page.entries], [3])
            self.assertTrue(page.has_more)
            older = manager.history(target.name, start_revision=page.next_start_revision)
            self.assertEqual([entry.revision for entry in older.entries], [1])
            self.assertIn("+working", manager.compare(target.name).diff.text)
            self.assertIn("+two", manager.compare(target.name, from_revision="1", to_revision="HEAD").diff.text)
            self.assertIn("+one", manager.compare(target.name, from_revision="HEAD", to_revision="1").diff.text)
            self.assertEqual(manager.compare(target.name, from_revision="BASE", to_revision="HEAD").diff.text, "")
            self.assertEqual(ET.canonicalize(svn("status", "--xml").decode("utf-8")),
                             ET.canonicalize(before_status.decode("utf-8")))
            self.assertEqual(target.read_bytes(), before_content)


if __name__ == "__main__":
    unittest.main()
