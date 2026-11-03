"""Strict bounded history completion; real child process, synthetic SVN output."""
from pathlib import Path
import sys
import unittest
from svn_client.history import HISTORY_DIAGNOSTICS_POLICY, HistoryError, _bounded_process


class HistoryDiagnosticsTests(unittest.TestCase):
    def run_fixture(self, stderr, *, code=0, stdout=b'<log/>'):
        source = f'import sys; sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r}); sys.exit({code})'
        return _bounded_process([sys.executable, '-c', source], cwd=Path.cwd(), timeout=5, stdout_limit=1024)

    def test_capability_is_explicit(self):
        self.assertEqual(HISTORY_DIAGNOSTICS_POLICY, 1)

    def test_clean_success_remains_success(self):
        self.assertEqual(self.run_fixture(b''), b'<log/>')

    def test_zero_exit_diagnostics_never_become_empty_success(self):
        for stderr in [b'svn: warning: W155007: not a working copy', '本地化提示'.encode(), b' ', b'custom tool banner\n']:
            with self.subTest(stderr=stderr), self.assertRaises(HistoryError) as caught:
                self.run_fixture(stderr)
            self.assertNotIn(stderr.decode(errors='replace').strip() or 'custom tool banner', str(caught.exception))
            self.assertIsNone(caught.exception.result)

    def test_partial_stdout_and_credentials_never_leak_on_warning(self):
        with self.assertRaises(HistoryError) as caught:
            self.run_fixture(b'https://user:SUPERSECRET@host warning', stdout=b'private diff contents')
        self.assertNotIn('SUPERSECRET', str(caught.exception)); self.assertNotIn('private diff', str(caught.exception))
        self.assertIsNone(caught.exception.result)

    def test_nonzero_still_retains_only_stable_codes(self):
        with self.assertRaises(HistoryError) as caught:
            self.run_fixture(b'E170013 SUPERSECRET', code=1)
        self.assertIn('E170013', str(caught.exception)); self.assertNotIn('SUPERSECRET', str(caught.exception))


if __name__ == '__main__': unittest.main()
