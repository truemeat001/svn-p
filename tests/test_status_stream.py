"""Incremental XML and real child-pipe tests; no SVN or repository is required."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import CommandResult, StatusEntry, SvnClient, SvnError
from svn_client.status_stream import (
    CHUNK_BYTES, DIAGNOSTIC_BYTES, MAX_XML_DEPTH, PIPE_QUEUE_CHUNKS,
    StatusXmlParser, _diagnostic, parse_status_chunks, run_status, text_chunks,
)
from test_backend import STATUS_XML


class StatusParserTests(unittest.TestCase):
    def parse(self, xml, size=65536):
        if isinstance(xml, str):
            xml = xml.encode("utf-8")
        return parse_status_chunks((xml[i:i + size] for i in range(0, len(xml), size)), str)

    def test_every_byte_split_preserves_utf8_metadata_and_order(self):
        entries = self.parse(STATUS_XML, 1)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[0], StatusEntry("中文 @ file.txt", "modified", props="normal",
                                                revision=5, last_changed_revision=4,
                                                last_changed_author="甲"))
        self.assertEqual(entries[1].moved_from, "old.txt")
        self.assertTrue(entries[1].copied)
        self.assertTrue(entries[2].conflicted)
        self.assertEqual(entries[-1].changelist, "fixes")

    def test_all_attributes_and_changelist_escaping(self):
        xml = '''<status><changelist name="任务 &amp; 甲"><entry path="a&amp;b">
        <wc-status item="normal" props="modified" revision="not-int" copied="true"
        switched="true" wc-locked="true" file-external="true" moved-from="旧" moved-to="新">
        <commit revision="not-int"><author>A &amp; B</author></commit></wc-status>
        </entry></changelist><target path="."><entry path="without-status"/></target></status>'''
        [entry] = self.parse(xml, 7)
        self.assertEqual(entry.path, "a&b")
        self.assertEqual(entry.changelist, "任务 & 甲")
        self.assertTrue(entry.switched and entry.wc_locked and entry.file_external)
        self.assertEqual(entry.moved_to, "新")
        self.assertEqual(entry.last_changed_author, "A & B")
        self.assertIsNone(entry.revision)
        self.assertIsNone(entry.last_changed_revision)

    def test_clean_named_entries_are_not_discarded(self):
        [entry] = self.parse('<status><changelist name="clean"><entry path="f"><wc-status item="normal"/></entry></changelist></status>')
        self.assertEqual(entry.item, "normal")
        self.assertEqual(entry.changelist, "clean")

    def test_relative_path_callback_is_preserved(self):
        rows = parse_status_chunks([b'<status><target><entry path="/work/f"><wc-status item="modified"/></entry></target></status>'],
                                   lambda path: str(Path(path).relative_to("/work")))
        self.assertEqual(rows[0].path, "f")

    def test_empty_document_is_valid(self):
        self.assertEqual(self.parse("<status/>"), [])

    def test_invalid_documents_fail_without_partial_result(self):
        for xml in ("", "garbage", "<status>", "<status><entry><wc-status/></entry>",
                    "<status/><status/>", "<info/>", "<status><entry></status>"):
            with self.subTest(xml=xml), self.assertRaises(SvnError):
                self.parse(xml, 3)

    def test_dtd_and_entities_rejected_across_chunks(self):
        for xml in (b'<!DOCTYPE status [<!ENTITY x "expanded">]><status/>',
                    '<status/>'.encode('utf-16')):
            with self.subTest(xml=xml), self.assertRaisesRegex(SvnError, "unsupported"):
                self.parse(xml, 1)

    def test_total_output_limit_is_explicit(self):
        parser = StatusXmlParser(str, max_bytes=15)
        parser.feed(b"<status>")
        with self.assertRaisesRegex(SvnError, "output limit"):
            parser.feed(b"</status>")

    def test_oversized_single_record_is_explicit(self):
        parser = StatusXmlParser(str, max_entry_bytes=32)
        parser.feed(b'<status><entry path="')
        with self.assertRaisesRegex(SvnError, "oversized"):
            parser.feed(b"x" * 20)

    def test_excessive_nesting_is_rejected(self):
        with self.assertRaisesRegex(SvnError, "nesting"):
            self.parse("<status>" + "<nested>" * MAX_XML_DEPTH, 7)

    def test_nested_entries_are_rejected(self):
        with self.assertRaisesRegex(SvnError, "nested entries"):
            self.parse('<status><entry><entry><wc-status/></entry></entry></status>')

    def test_completed_elements_do_not_remain_in_parent(self):
        parser = StatusXmlParser(str)
        parser.feed(b'<status><target path=".">')
        record = b'<entry path="f"><wc-status item="normal"/></entry>'
        for _ in range(1000):
            parser.feed(record)
            self.assertEqual(len(parser._stack[-1]), 0)
        parser.feed(b'</target></status>')
        self.assertEqual(len(parser.finish()), 1000)

    def test_parser_cannot_be_reused_after_finish(self):
        parser = StatusXmlParser(str)
        parser.feed(b"<status/>")
        parser.finish()
        with self.assertRaises(ValueError):
            parser.finish()
        with self.assertRaises(ValueError):
            parser.feed(b"anything")

    def test_cancelled_parse_does_not_publish_prefix(self):
        cancelled = threading.Event()
        def chunks():
            yield b'<status><entry path="f"><wc-status item="normal"/></entry>'
            cancelled.set()
            yield b'</status>'
        with self.assertRaisesRegex(SvnError, "cancelled"):
            parse_status_chunks(chunks(), str, cancel_event=cancelled)

    def test_text_adapter_preserves_unicode_with_bounded_chunks(self):
        text = "中文" * CHUNK_BYTES
        chunks = list(text_chunks(text))
        self.assertEqual("".join(chunks), text)
        self.assertLessEqual(max(map(len, chunks)), CHUNK_BYTES)

    def test_diagnostic_prefix_redacts_complete_and_cut_off_passwords(self):
        complete = b'https://user:secret@example.test/repo failed'
        self.assertNotIn("secret", _diagnostic(complete, False, SvnClient._decode))
        incomplete = b'failure https://user:partial-secret'
        result = _diagnostic(incomplete, True, SvnClient._decode)
        self.assertNotIn("partial-secret", result)
        self.assertIn("truncated", result)


class StatusProcessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_python(self, code, **kwargs):
        argv = [sys.executable, "-u", "-c", code]
        return run_status(argv, cwd=self.root, timeout=kwargs.pop("timeout", 10),
                          relative=str, display_args=("synthetic-python-child",),
                          decode=SvnClient._decode, **kwargs)

    def test_real_process_drains_both_pipes_without_full_buffering(self):
        code = '''import sys
sys.stdout.write('<status><target>')
for i in range(6000):
    sys.stderr.write('warning' * 100)
    sys.stdout.write('<entry path="f%d"><wc-status item="modified"/></entry>' % i)
sys.stdout.write('</target></status>')
'''
        rows = self.run_python(code)
        self.assertEqual(len(rows), 6000)
        self.assertEqual(rows[-1].path, "f5999")

    def test_nonzero_exit_discards_valid_entries_and_bounds_redacted_error(self):
        code = '''import sys
sys.stdout.write('<status><entry path="f"><wc-status item="normal"/></entry></status>')
sys.stderr.write('https://user:secret@example.test/repo failed\\n' + 'x' * 200000)
sys.exit(3)
'''
        with self.assertRaises(SvnError) as caught:
            self.run_python(code)
        result = caught.exception.result
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("secret", result.stderr)
        self.assertIn("truncated", result.stderr)
        self.assertLess(len(result.stderr), DIAGNOSTIC_BYTES + 100)

    def test_stderr_error_has_precedence_over_invalid_xml(self):
        with self.assertRaisesRegex(SvnError, "repository unavailable"):
            self.run_python("import sys; print('not XML'); sys.stderr.write('repository unavailable'); sys.exit(1)")

    def test_incomplete_success_is_not_a_status_snapshot(self):
        with self.assertRaisesRegex(SvnError, "invalid XML"):
            self.run_python("print('<status><entry><wc-status/></entry>')")

    def test_timeout_kills_and_reaps_without_retry(self):
        started = time.monotonic()
        real_popen = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch("svn_client.status_stream.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(SvnError, "status read timed out.*No write operation was requested"):
                self.run_python("import time; print('<status>'); time.sleep(30)", timeout=0.15)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertLess(time.monotonic() - started, 3)

    def test_cancel_event_terminates_after_process_started(self):
        event = threading.Event()
        timer = threading.Timer(0.15, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaisesRegex(SvnError, "cancelled"):
            self.run_python("import time; print('<status>'); time.sleep(30)", cancel_event=event)
        self.assertLess(time.monotonic() - started, 3)

    def test_pre_cancel_does_not_launch_process(self):
        event = threading.Event()
        event.set()
        with patch("svn_client.status_stream.subprocess.Popen") as popen:
            with self.assertRaisesRegex(SvnError, "cancelled"):
                self.run_python("", cancel_event=event)
        popen.assert_not_called()

    def test_output_limit_terminates_before_accepting_status(self):
        with self.assertRaisesRegex(SvnError, "output limit"):
            self.run_python("print('<status>' + '<entry><wc-status/></entry>' * 1000 + '</status>')", max_bytes=100)

    def test_output_limit_also_applies_after_malformed_prefix(self):
        with self.assertRaisesRegex(SvnError, "output limit"):
            self.run_python("import sys; sys.stdout.write('invalid' + 'x' * 300000)", max_bytes=100000)

    def test_start_failure_is_wrapped(self):
        with patch("svn_client.status_stream.subprocess.Popen", side_effect=OSError("missing executable")):
            with self.assertRaisesRegex(SvnError, "Could not start SVN"):
                self.run_python("")

    def test_invalid_timeout_does_not_launch(self):
        with patch("svn_client.status_stream.subprocess.Popen") as popen:
            for timeout in (0, -1, True, float("inf"), float("nan"), 10 ** 400):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    self.run_python("", timeout=timeout)
        popen.assert_not_called()

    def test_extreme_backend_timeout_is_rejected_before_lock(self):
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            client = SvnClient(self.root, timeout=10 ** 400)
        client._lock = Mock()
        with patch("svn_client.status_stream.run_status") as run:
            with self.assertRaisesRegex(ValueError, "finite positive"):
                client.status()
        client._lock.acquire.assert_not_called()
        run.assert_not_called()

    def test_keyboard_interrupt_reaps_process(self):
        real_popen = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch("svn_client.status_stream.subprocess.Popen", side_effect=launch), patch(
            "svn_client.status_stream.StatusXmlParser.feed", side_effect=KeyboardInterrupt()
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.run_python("import time; print('<status>'); time.sleep(30)")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)

    def test_default_backend_uses_safe_arguments_and_passes_cancellation(self):
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            client = SvnClient(self.root)
        event = threading.Event()
        with patch("svn_client.status_stream.run_status", return_value=[]) as run:
            client.status(["a@b", "--password"], verbose=True, include_ignored=True, cancel_event=event)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/trusted/svn", "status", "--non-interactive", "--no-auth-cache",
                                "--xml", "--ignore-externals", "--no-ignore", "--verbose", "--", "a@b@", "--password"])
        self.assertIs(run.call_args.kwargs["cancel_event"], event)

    def _contended_status(self, *, cancel, timeout):
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            client = SvnClient(self.root, timeout=timeout)
        event = threading.Event()
        started, finished = threading.Event(), threading.Event()
        errors = []
        original_lock = client._lock
        class ObservedLock:
            def acquire(self, **kwargs):
                started.set()
                return original_lock.acquire(**kwargs)
            def release(self):
                original_lock.release()
        def work():
            try:
                client.status(cancel_event=event)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        original_lock.acquire()
        client._lock = ObservedLock()
        thread = threading.Thread(target=work, daemon=True)
        try:
            with patch("svn_client.status_stream.run_status") as run:
                thread.start()
                self.assertTrue(started.wait(1))
                if cancel:
                    event.set()
                self.assertTrue(finished.wait(1), "Status remained stuck behind its working-copy lock")
                run.assert_not_called()
        finally:
            client._lock.release()
            thread.join(timeout=1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SvnError)
        self.assertRegex(str(errors[0]), "cancelled" if cancel else "timed out waiting")

    def test_status_cancels_while_queued_for_working_copy_lock(self):
        self._contended_status(cancel=True, timeout=30)

    def test_status_times_out_while_queued_for_working_copy_lock(self):
        self._contended_status(cancel=False, timeout=0.1)

    def test_status_lock_remains_reentrant(self):
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            client = SvnClient(self.root)
        with client._lock, patch("svn_client.status_stream.run_status", return_value=[]) as run:
            self.assertEqual(client.status(), [])
        run.assert_called_once()
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], client.timeout)

    def test_process_spawn_is_noninteractive_no_shell_and_unbuffered(self):
        real_popen = subprocess.Popen
        with patch("svn_client.status_stream.subprocess.Popen", wraps=real_popen) as popen:
            self.run_python("print('<status/>')")
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["bufsize"], 0)
        self.assertEqual(kwargs["cwd"], self.root)

    def test_cancelled_backend_does_not_call_injected_transport(self):
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            client = SvnClient(self.root)
        event = threading.Event()
        event.set()
        with patch.object(client, "command") as command:
            with self.assertRaisesRegex(SvnError, "cancelled"):
                client.status(cancel_event=event)
        command.assert_not_called()

    def test_injected_command_transport_remains_supported(self):
        with patch("svn_client.backend.discover_svn", return_value="/trusted/svn"):
            client = SvnClient(self.root)
        client.command = lambda args: CommandResult(tuple(args), 0, STATUS_XML, "")
        with patch("svn_client.status_stream.run_status") as stream:
            self.assertEqual(len(client.status()), 5)
        stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
