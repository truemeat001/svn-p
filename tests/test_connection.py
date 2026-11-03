"""Connection probes never contact non-test repositories in this suite."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from svn_client.backend import SvnError
from svn_client.connection import ConnectionDiagnostics, MAX_XML_BYTES, validate_probe_url


REPO_UUID = "12345678-1234-1234-1234-123456789abc"
URL = "https://example.invalid/repository/trunk"


def xml_info(*, url=URL, root="https://example.invalid/repository", repository_uuid=REPO_UUID,
             wcroot=None, revision="7", kind="dir", token="opaquelocktoken:PRIVATE"):
    info = ET.Element("info")
    entry = ET.SubElement(info, "entry", kind=kind, revision=revision, path=".")
    ET.SubElement(entry, "url").text = url
    repository = ET.SubElement(entry, "repository")
    ET.SubElement(repository, "root").text = root
    ET.SubElement(repository, "uuid").text = repository_uuid
    if wcroot is not None:
        ET.SubElement(ET.SubElement(entry, "wc-info"), "wcroot-abspath").text = str(wcroot)
    ET.SubElement(ET.SubElement(entry, "lock"), "token").text = token
    ET.SubElement(ET.SubElement(entry, "commit"), "author").text = "PRIVATE-AUTHOR"
    return ET.tostring(info, encoding="utf-8", xml_declaration=True)


def completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class UrlValidationTests(unittest.TestCase):
    def test_safe_literal_urls_preserved(self):
        values = [URL, "https://host:8443/repo/a%20b@123", "https://[::1]/repo",
                  "svn://host/repo/中文", "file:///tmp/repo", "file://localhost/C:/repo"]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(validate_probe_url(value), value)

    def test_unsafe_or_ambiguous_urls_fail_without_echo(self):
        values = [None, "", "not-a-url", "svn+ssh://host/repo", "ssh://host/repo",
                  "https://user@host/repo", "https://user:PRIVATE@host/repo", "https://:PRIVATE@host/repo",
                  "https://user%40host/repo", "https://host:bad/repo", "https://host:99999/repo",
                  "https://host:/repo", "https://[broken/repo", "https:///repo", "https://host/repo?",
                  "https://host/repo#", "https://host/repo?token=PRIVATE", "https://host/repo#PRIVATE",
                  "https://host/repo\\file", "https://host/repo%5cfile", "https://host/%0aPRIVATE",
                  "https://host/%00", "https://host/%7f", "https://host/%ff", "https://host/%zz",
                  " https://host/repo", "https://host/a b", "https://host/\n", "https://host/\ud800",
                  "file://remote-server/repo", "file://localhost:80/repo", "file:////host/repo",
                  "file:///%2fhost/repo", "file://localhost//host/repo", "file://", "a" * 8193]
        for value in values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError) as caught:
                    validate_probe_url(value)
                self.assertNotIn("PRIVATE", str(caught.exception))


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.wc = Path(directory.name)
        self.executable = str(self.wc / "svn.exe")
        finder = patch("svn_client.connection.discover_svn", return_value=self.executable)
        runner = patch("svn_client.connection.subprocess.run")
        self.find = finder.start()
        self.run = runner.start()
        self.addCleanup(finder.stop)
        self.addCleanup(runner.stop)
        def default(argv, **kwargs):
            if argv[1] == "--version":
                return completed(b"1.14.2\n")
            return completed(xml_info(wcroot=self.wc if argv[-1] == "." else None))
        self.run.side_effect = default

    def probe(self, **kwargs):
        return ConnectionDiagnostics(self.wc).probe(**kwargs)

    def test_default_checks_version_and_local_identity_without_remote(self):
        result = self.probe()
        self.assertTrue(result.ok)
        self.assertEqual(result.version, "1.14.2")
        self.assertEqual(result.working_copy.repository.repository_uuid, REPO_UUID)
        self.assertEqual(result.working_copy.working_copy_root, str(self.wc))
        self.assertFalse(result.remote_requested)
        self.assertFalse(result.remote_attempted)
        self.assertIsNone(result.remote)
        self.assertEqual([call.args[0][-1] for call in self.run.call_args_list], ["--quiet", "."])

    def test_executable_only_requires_no_working_copy(self):
        result = ConnectionDiagnostics().probe()
        self.assertTrue(result.ok)
        self.assertIsNone(result.working_copy)
        self.run.assert_called_once()

    def test_explicit_remote_only_uses_literal_head_url_and_safe_flags(self):
        value = "https://host/repo/literal@123"
        result = ConnectionDiagnostics(executable=self.executable, timeout=3.5).probe(url=value, check_remote=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.remote_attempted)
        args, kwargs = self.run.call_args_list[-1].args[0], self.run.call_args_list[-1].kwargs
        self.assertEqual(args, [self.executable, "info", "--non-interactive", "--no-auth-cache", "--xml", "--depth", "empty", "--", value + "@HEAD"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 3.5)
        self.assertIsNone(kwargs["cwd"])
        self.assertNotIn("--trust-server-cert", args)
        self.assertNotIn("--config-option", args)
        self.find.assert_called_once_with(self.executable)

    def test_local_and_remote_match_is_uuid_only(self):
        result = self.probe(url=URL, check_remote=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.repository_identity_matches)
        self.assertIn("does not prove identical paths", result.steps[-1].message)
        self.assertEqual(self.run.call_count, 3)

    def test_different_repo_uuid_reported_no_mutation(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(xml_info(wcroot=self.wc)),
                               completed(xml_info(repository_uuid="00000000-0000-0000-0000-000000000000"))]
        result = self.probe(url=URL, check_remote=True)
        self.assertFalse(result.ok)
        self.assertFalse(result.repository_identity_matches)
        self.assertEqual(result.steps[-1].category, "identity_mismatch")
        self.assertEqual(self.run.call_count, 3)

    def test_remote_flag_requires_explicit_url_and_bool(self):
        for args in ({"url": URL}, {"check_remote": True}, {"url": URL, "check_remote": 1}, {"check_remote": None}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.probe(**args)
        self.find.assert_not_called()
        self.run.assert_not_called()

    def test_unsafe_url_refused_before_any_executable_read(self):
        with self.assertRaises(ValueError):
            self.probe(url="https://user:PRIVATE@host/repo", check_remote=True)
        self.find.assert_not_called()
        self.run.assert_not_called()

    def test_invalid_timeout_fail_before_reads(self):
        for value in (False, True, 0, -1, 121, float("nan"), float("inf"), "5", None, 10**400):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                ConnectionDiagnostics(timeout=value)
        self.find.assert_not_called()
        self.run.assert_not_called()

    def test_missing_executable_returns_skipped_remote_without_raw_error(self):
        self.find.side_effect = SvnError("PRIVATE cannot start")
        result = self.probe(url=URL, check_remote=True)
        self.assertFalse(result.ok)
        self.assertFalse(result.remote_attempted)
        self.assertEqual(result.steps[0].category, "dependency_missing")
        self.assertEqual(result.steps[-1].status, "skipped")
        self.assertNotIn("PRIVATE", repr(result))
        self.run.assert_not_called()

    def test_bad_executable_version_blocks_later_stages(self):
        for value in (b"other program PRIVATE", b"https://user:PRIVATE@host/repo", b"\xff", b"9" * 1000):
            self.run.reset_mock()
            self.run.side_effect = [completed(value)]
            result = self.probe(url=URL, check_remote=True)
            self.assertEqual(result.steps[0].category, "invalid_response")
            self.assertEqual(result.version, "")
            self.assertFalse(result.remote_attempted)
            self.assertNotIn("PRIVATE", repr(result))
            self.run.assert_called_once()

    def test_version_timeout_does_not_retry_or_probe_remote(self):
        self.run.side_effect = subprocess.TimeoutExpired("PRIVATE-argv", 1, output=b"PRIVATE")
        result = self.probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[0].category, "timeout")
        self.assertFalse(result.remote_attempted)
        self.assertNotIn("PRIVATE", repr(result))
        self.run.assert_called_once()

    def test_start_error_is_generic(self):
        self.run.side_effect = OSError("PRIVATE path")
        result = self.probe()
        self.assertEqual(result.steps[0].category, "process_error")
        self.assertNotIn("PRIVATE", repr(result))

    def test_remote_timeout_is_read_failure_no_retry(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(xml_info(wcroot=self.wc)),
                               subprocess.TimeoutExpired("PRIVATE", 15, stderr=b"secret")]
        result = self.probe(url=URL, check_remote=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.remote_attempted)
        self.assertEqual(result.steps[-1].category, "timeout")
        self.assertEqual(self.run.call_count, 3)
        self.assertIsNotNone(result.working_copy)

    def test_invalid_working_copy_does_not_infer_remote(self):
        for path in (self.wc / "absent", "https://host/repo", "", "bad\0path"):
            self.run.reset_mock()
            result = ConnectionDiagnostics(path).probe()
            self.assertFalse(result.ok)
            self.assertEqual(result.steps[-1].category, "working_copy")
            self.run.assert_called_once()

    def test_explicit_remote_can_run_after_local_path_validation_failure(self):
        result = ConnectionDiagnostics(self.wc / "absent").probe(url=URL, check_remote=True)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.remote)
        self.assertTrue(result.remote_attempted)
        self.assertEqual(self.run.call_count, 2)

    def test_local_info_failure_does_not_block_explicit_independent_url(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(stderr=b"svn: E155007: bad", returncode=1), completed(xml_info())]
        result = self.probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[1].category, "working_copy")
        self.assertTrue(result.remote_attempted)
        self.assertIsNotNone(result.remote)
        self.assertIsNone(result.repository_identity_matches)

    def test_numeric_classification_is_localization_independent_and_safe(self):
        cases = {"E215004": "authentication", "E120194": "authentication", "E210007": "authentication",
                 "E170001": "authorization", "E175013": "authorization", "E220001": "authorization",
                 "E120170": "tls", "E120171": "tls", "E230001": "tls", "E240006": "tls",
                 "E175012": "timeout", "E120112": "timeout", "E210002": "network", "E120108": "network",
                 "E155007": "working_copy", "E160013": "not_found", "E170013": "connection", "E175002": "connection",
                 "E999999": "svn_error"}
        for code, category in cases.items():
            with self.subTest(code=code):
                self.run.side_effect = [completed(b"1.14.2"), completed(stderr=("svn: " + code + ": 失败 PRIVATE https://u:SECRET@host opaquelocktoken:TOKEN").encode(), returncode=1)]
                result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
                self.assertEqual(result.steps[-1].category, category)
                self.assertEqual(result.steps[-1].error_codes, (code[1:],))
                self.assertEqual(result.steps[-1].returncode, 1)
                public = json.dumps(asdict(result))
                for secret in ("PRIVATE", "SECRET", "TOKEN"):
                    self.assertNotIn(secret, public)

    def test_specific_tls_and_auth_codes_override_generic_session_wrapper(self):
        for code, category in ((b"E120171", "tls"), (b"E215004", "authentication")):
            self.run.side_effect = [completed(b"1.14.2"), completed(stderr=b"svn: E170013: wrapper\nsvn: " + code + b": detail", returncode=1)]
            result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
            self.assertEqual(result.steps[-1].category, category)

    def test_unknown_text_is_not_guessed_as_network(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(stderr=b"authentication timeout certificate PRIVATE", returncode=1)]
        result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[-1].category, "svn_error")
        self.assertNotIn("PRIVATE", repr(result))

    def test_error_looking_url_or_stdout_does_not_override_actual_diagnostic(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(
            stdout=b"svn: E120171: unrelated text", stderr=b"svn: E170013: https://host/E215004:detail", returncode=1)]
        result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[-1].category, "connection")
        self.assertEqual(result.steps[-1].error_codes, ("170013",))

    def test_warning_code_prefix_is_supported(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(stderr=b"svn: warning: W155010: missing", returncode=1)]
        result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[-1].category, "working_copy")

    def test_public_identity_never_copies_lock_or_author_fields(self):
        result = self.probe(url=URL, check_remote=True)
        public = json.dumps(asdict(result))
        self.assertNotIn("PRIVATE", public)
        self.assertNotIn("opaquelocktoken", public)

    def test_local_ssh_metadata_is_read_without_connecting_or_exposing_userinfo(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(xml_info(
            url="svn+ssh://PRIVATE@host/repo/trunk", root="svn+ssh://PRIVATE@host/repo", wcroot=self.wc))]
        result = self.probe()
        self.assertTrue(result.ok)
        self.assertEqual(result.working_copy.repository.url, "svn+ssh://host/repo/trunk")
        self.assertFalse(result.remote_attempted)
        self.assertNotIn("PRIVATE", repr(result))
        self.assertEqual(self.run.call_count, 2)

    def test_remote_ssh_metadata_is_not_treated_as_supported_transport(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(xml_info(url="svn+ssh://host/repo"))]
        result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[-1].category, "invalid_response")
        self.assertIsNone(result.remote)

    def test_bad_identity_response_fails_closed(self):
        responses = [b"not XML PRIVATE", b"<info/>", b"<info><entry/><entry/></info>",
                     xml_info(url="https://u:PRIVATE@host/repo"), xml_info(root="https://host/repo?PRIVATE"),
                     xml_info(repository_uuid="PRIVATE"), xml_info(revision="-1"), xml_info(revision="9" * 10000),
                     xml_info(kind="unknown"), b'<!DOCTYPE info [<!ENTITY x "PRIVATE">]><info>&x;</info>',
                     '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE info [<!ENTITY x "PRIVATE">]><info>&x;</info>'.encode("utf-16"),
                     xml_info().decode().encode("utf-16-le"),
                     b'<?xml version="1.0" encoding="PRIVATE"?><info/>']
        for data in responses:
            with self.subTest(data=data[:40]):
                self.run.side_effect = [completed(b"1.14.2"), completed(data)]
                result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
                self.assertEqual(result.steps[-1].category, "invalid_response")
                self.assertIsNone(result.remote)
                self.assertNotIn("PRIVATE", repr(result))

    def test_local_wc_root_must_be_absolute_ancestor_and_directory(self):
        for data in (xml_info(), xml_info(wcroot="relative"), xml_info(wcroot=self.wc / "other"),
                     xml_info(wcroot=self.wc, kind="file")):
            self.run.side_effect = [completed(b"1.14.2"), completed(data)]
            result = self.probe()
            self.assertEqual(result.steps[-1].category, "invalid_response")

    def test_selected_subdirectory_reports_actual_root_without_widening_target(self):
        child = self.wc / "child"
        child.mkdir()
        result = ConnectionDiagnostics(child).probe()
        self.assertTrue(result.ok)
        self.assertEqual(result.working_copy.path, str(child))
        self.assertEqual(result.working_copy.working_copy_root, str(self.wc))
        self.assertEqual(self.run.call_args.kwargs["cwd"], child)
        self.assertEqual(self.run.call_args.args[0][-1], ".")

    def test_oversized_xml_not_parsed_or_reported(self):
        self.run.side_effect = [completed(b"1.14.2"), completed(b"PRIVATE" + b"x" * MAX_XML_BYTES)]
        result = ConnectionDiagnostics().probe(url=URL, check_remote=True)
        self.assertEqual(result.steps[-1].category, "invalid_response")
        self.assertNotIn("PRIVATE", repr(result))


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin required for temporary local integration")
class RealConnectionTests(unittest.TestCase):
    def test_only_temporary_file_repository_is_contacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, wc = root / "repo", root / "wc"
            subprocess.run(["svnadmin", "create", str(repository)], check=True, capture_output=True)
            subprocess.run(["svn", "checkout", "--non-interactive", "--no-auth-cache", repository.as_uri(), str(wc)], check=True, capture_output=True)
            result = ConnectionDiagnostics(wc).probe(url=repository.as_uri(), check_remote=True)
            self.assertTrue(result.ok, result)
            self.assertTrue(result.repository_identity_matches)
            self.assertEqual(result.remote.revision, 0)


if __name__ == "__main__":
    unittest.main()
