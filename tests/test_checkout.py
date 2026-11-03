"""Safe checkout boundaries, retained partial results, and optional SVN checks."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from svn_client.backend import (
    SvnClient, SvnError, checkout_working_copy,
    validate_checkout_destination, validate_checkout_url,
)


class CheckoutValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_new_directory_and_existing_empty_directory_are_read_only(self):
        target = self.root / "中文 @ checkout"
        self.assertEqual(validate_checkout_destination(target), target)
        self.assertFalse(target.exists())
        target.mkdir()
        self.assertEqual(validate_checkout_destination(target), target)
        self.assertEqual(list(target.iterdir()), [])

    def test_nonempty_existing_directory_refused_without_deletion(self):
        target = self.root / "existing"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            validate_checkout_destination(target)
        self.assertEqual(marker.read_text(), "keep")

    def test_parent_must_exist_and_every_component_must_be_directory(self):
        with self.assertRaisesRegex(ValueError, "parent directory"):
            validate_checkout_destination(self.root / "missing" / "checkout")
        file = self.root / "file"
        file.write_text("data")
        for target in (file, file / "checkout"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_checkout_destination(target)

    def test_existing_working_copy_and_nested_checkout_refused(self):
        (self.root / ".svn").mkdir()
        for target in (self.root, self.root / "nested"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "nested"):
                validate_checkout_destination(target)

    def test_checkout_cannot_be_ancestor_of_existing_working_copy(self):
        (self.root / "child" / ".svn").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "new or empty"):
            validate_checkout_destination(self.root)

    def test_reserved_administrative_name_refused(self):
        with self.assertRaises(ValueError):
            validate_checkout_destination(self.root / ".svn")

    def test_destination_and_parent_symlinks_refused(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is not supported")
        for target in (link, link / "checkout"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "symbolic links"):
                validate_checkout_destination(target)

    def test_urls_validated_without_network_or_credential_echo(self):
        for url in ("https://example.test/repo", "http://example.test/repo@17",
                    "svn://example.test/repo", "file:///tmp/repo"):
            with self.subTest(url=url):
                self.assertEqual(validate_checkout_url(url), url)
        for url in ("https://user:SECRET@example.test/repo", "https://:SECRET@example.test/repo",
                    "file://user:SECRET@host/repo", " https://example.test/repo", "https://example.test/repo\n",
                    "https://example.test/repo?arg=1", "https://example.test/repo#frag", "ftp://host/repo",
                    "http:local", "file:relative", "https://[broken/repo", "--password"):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                validate_checkout_url(url)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_ssh_requires_trusted_external_checkout_then_registration(self):
        with self.assertRaisesRegex(ValueError, "trusted SVN/SSH client"):
            validate_checkout_url("svn+ssh://user@example.test/repo")


class CheckoutOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "中文 @ checkout"
        self.discovery = patch("svn_client.backend.discover_svn", return_value="/trusted/svn")
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def info_xml(self, wc_root=None):
        root = ET.Element("info")
        entry = ET.SubElement(root, "entry", path=".", kind="dir", revision="1")
        ET.SubElement(entry, "url").text = "https://example.test/repo"
        info = ET.SubElement(entry, "wc-info")
        ET.SubElement(info, "wcroot-abspath").text = str(wc_root or self.target)
        return ET.tostring(root, encoding="utf-8")

    def runner(self, args, **kwargs):
        if args[1] == "checkout":
            self.target.mkdir(exist_ok=True)
            (self.target / ".svn").mkdir()
            (self.target / "retained.txt").write_text("data")
            return subprocess.CompletedProcess(args, 0, b"Checked out revision 1.\n", b"")
        return subprocess.CompletedProcess(args, 0, self.info_xml(), b"")

    def test_checkout_flags_literal_at_destination_and_verification(self):
        with patch("svn_client.backend.subprocess.run", side_effect=self.runner) as run:
            result = checkout_working_copy("https://example.test/repo@17", self.target, timeout=301)
        args, kwargs = run.call_args_list[0]
        self.assertEqual(args[0], ["/trusted/svn", "checkout", "--non-interactive", "--no-auth-cache", "--ignore-externals", "--", "https://example.test/repo@17", str(self.target)])
        self.assertNotIn("--force", args[0])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 301)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][1], "info")
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.target / "retained.txt").read_text(), "data")

    def test_existing_empty_destination_allowed(self):
        self.target.mkdir()
        with patch("svn_client.backend.subprocess.run", side_effect=self.runner):
            checkout_working_copy("https://example.test/repo", self.target)

    def test_timeout_retains_partial_destination_and_never_retries(self):
        def timeout(args, **kwargs):
            self.target.mkdir()
            (self.target / "partial.txt").write_text("partial")
            raise subprocess.TimeoutExpired(args, 1)
        with patch("svn_client.backend.subprocess.run", side_effect=timeout) as run:
            with self.assertRaisesRegex(SvnError, "uncertain"):
                checkout_working_copy("https://example.test/repo", self.target, timeout=1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual((self.target / "partial.txt").read_text(), "partial")

    def test_error_retains_files_and_redacts_credentials(self):
        def failure(args, **kwargs):
            self.target.mkdir()
            (self.target / "partial.txt").write_text("partial")
            return subprocess.CompletedProcess(args, 1, b"", b"https://u:SECRET@host failed")
        with patch("svn_client.backend.subprocess.run", side_effect=failure) as run:
            with self.assertRaisesRegex(SvnError, "retained") as caught:
                checkout_working_copy("https://example.test/repo", self.target)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(run.call_count, 1)
        self.assertTrue((self.target / "partial.txt").exists())

    def test_wrong_working_copy_root_is_not_success_and_is_not_deleted(self):
        def runner(args, **kwargs):
            result = self.runner(args, **kwargs)
            if args[1] == "info":
                result.stdout = self.info_xml(self.root / "wrong-root")
            return result
        with patch("svn_client.backend.subprocess.run", side_effect=runner):
            with self.assertRaisesRegex(SvnError, "verification failed"):
                checkout_working_copy("https://example.test/repo", self.target)
        self.assertTrue((self.target / "retained.txt").exists())

    def test_validation_failures_never_run_a_process(self):
        with patch("svn_client.backend.subprocess.run") as run:
            for timeout in (0, -1, float("inf"), float("nan"), True, "10", 10 ** 400):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    checkout_working_copy("https://example.test/repo", self.target, timeout=timeout)
            with self.assertRaises(ValueError):
                checkout_working_copy("https://u:SECRET@host/repo", self.target)
            with self.assertRaisesRegex(ValueError, "svn\\+ssh"):
                checkout_working_copy("svn+ssh://user@host/repo", self.target)
            run.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_recheck_prevents_destination_changed_before_execution(self):
        original = validate_checkout_destination
        count = 0
        def changing(path):
            nonlocal count
            count += 1
            if count == 2:
                self.target.mkdir()
                (self.target / "keep.txt").write_text("keep")
            return original(path)
        with patch("svn_client.backend.validate_checkout_destination", side_effect=changing), patch("svn_client.backend.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "new or empty"):
                checkout_working_copy("https://example.test/repo", self.target)
            run.assert_not_called()
        self.assertEqual((self.target / "keep.txt").read_text(), "keep")


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin required for integration")
class CheckoutIntegrationTests(unittest.TestCase):
    def test_local_repository_checkout_and_literal_at_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            subprocess.run(["svnadmin", "create", str(repo)], check=True, capture_output=True)
            target = root / "中文 @ checkout"
            result = checkout_working_copy(repo.as_uri(), target)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(Path(SvnClient(target).info()["wc_root"]), target)
            self.assertFalse((root / "中文 @ checkout@").exists())
            with self.assertRaises(ValueError):
                checkout_working_copy(repo.as_uri(), target)


if __name__ == "__main__":
    unittest.main()
