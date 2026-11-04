"""Optional real SVN checkout acceptance using only self-created file:// repos.

Set SVN_DESK_TEST_SVN and SVN_DESK_TEST_SVNADMIN to trusted installed binaries,
otherwise PATH is used. These tests never contact a remote repository or open a
user project. Windows race tests exercise actual OS handles, not mocked flags.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WEB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEB))
from checkout_ops import CheckoutService
from file_reads import history_module
from workspace_bridge import WorkspaceCatalog, trusted_modules

SOURCE = WEB.parents[1]
SVN = os.environ.get('SVN_DESK_TEST_SVN') or shutil.which('svn')
SVNADMIN = os.environ.get('SVN_DESK_TEST_SVNADMIN') or shutil.which('svnadmin')


@unittest.skipUnless(SVN and SVNADMIN, 'Trusted svn and svnadmin required for real temporary-repository checks')
class RealCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repository'
        self.seed = self.root / 'seed'; self.seed.mkdir()
        (self.seed / 'src').mkdir()
        (self.seed / 'src' / '中文 @ file.txt').write_text('fixture content\n', encoding='utf-8')
        (self.seed / 'README.txt').write_text('fixture\n', encoding='utf-8')
        self.native([SVNADMIN, 'create', str(self.repo)])
        self.native([SVN, 'import', '--non-interactive', '--no-auth-cache', '--quiet',
                     '--message=temporary checkout fixture', '--', str(self.seed), self.repo.as_uri()])
        self.backend, _, self.discovery, self.settings = trusted_modules(SOURCE)
        self.history = history_module(SOURCE)
        self.parent = self.root / 'target-parent'; self.parent.mkdir()
        self.dest = self.parent / '新目录 @ checkout'
        self.catalog = WorkspaceCatalog(discovery=self.discovery, settings=self.settings,
                                        adapter_factory=lambda path: self.fail('No automatic workspace selection'))

    def native(self, args):
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False, timeout=30)
        self.assertEqual(result.returncode, 0, 'Temporary repository command failed')
        self.assertEqual(result.stderr, b'')
        return result

    def service(self):
        return CheckoutService(url=self.repo.as_uri(), destination=self.dest, backend=self.backend,
                               history=self.history, catalog=self.catalog, executable=SVN)

    def execute(self, service):
        plan = service.operation('preflight', {})
        return service.operation('apply', {'planToken': plan['planToken'], 'confirmed': True})

    def verify(self, service, result):
        self.assertIs(result['verified'], True)
        self.assertEqual(service.state, 'completed')
        self.assertIsNone(self.catalog.selected)
        self.assertEqual(self.catalog.candidates[result['workspaceId']].path, self.dest)
        self.assertEqual((self.dest / 'src' / '中文 @ file.txt').read_text(encoding='utf-8'), 'fixture content\n')
        self.assertEqual((self.dest / 'README.txt').read_text(encoding='utf-8'), 'fixture\n')
        self.assertTrue((self.dest / '.svn').is_dir())

    def test_new_directory_real_checkout_with_guard(self):
        service = self.service(); self.verify(service, self.execute(service))

    def test_existing_empty_directory_real_checkout_with_guard(self):
        self.dest.mkdir(); service = self.service(); self.verify(service, self.execute(service))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows deny-rename guard acceptance')
    def test_native_windows_launch_race_blocks_target_and_parent_then_real_svn_succeeds(self):
        original = subprocess.Popen; attempts = []
        displaced_target = self.parent / 'displaced-target'
        displaced_parent = self.root / 'displaced-parent'

        def launch(argv, **kwargs):
            if len(argv) > 1 and argv[1] == 'checkout':
                for source, other in [(self.dest, displaced_target), (self.parent, displaced_parent)]:
                    with self.assertRaises(PermissionError) as caught:
                        source.rename(other)
                    self.assertEqual(caught.exception.winerror, 32)
                    self.assertTrue(source.is_dir()); self.assertFalse(other.exists())
                    attempts.append(str(source))
            return original(argv, **kwargs)

        service = self.service()
        with patch('svn_client.history.subprocess.Popen', side_effect=launch):
            self.verify(service, self.execute(service))
        self.assertEqual(attempts, [str(self.dest), str(self.parent)])
        self.assertFalse(displaced_target.exists()); self.assertFalse(displaced_parent.exists())
        # Guard lifetime ends after verification/registration, not before.
        self.dest.rename(displaced_target)
        self.assertTrue((displaced_target / 'README.txt').is_file())

    @unittest.skipUnless(os.name == 'nt', 'Native Windows pre-mkdir ancestor guard acceptance')
    def test_native_windows_parent_cannot_be_replaced_at_mkdir_then_checkout_succeeds(self):
        original = Path.mkdir; attempts = []
        displaced = self.root / 'displaced-parent'

        def mkdir(path, *args, **kwargs):
            if path == self.dest:
                with self.assertRaises(PermissionError) as caught:
                    self.parent.rename(displaced)
                self.assertEqual(caught.exception.winerror, 32)
                self.assertFalse(displaced.exists()); attempts.append(str(path))
            return original(path, *args, **kwargs)

        service = self.service()
        with patch.object(Path, 'mkdir', new=mkdir):
            self.verify(service, self.execute(service))
        self.assertEqual(attempts, [str(self.dest)])
        self.assertFalse(displaced.exists())


if __name__ == '__main__':
    unittest.main()
