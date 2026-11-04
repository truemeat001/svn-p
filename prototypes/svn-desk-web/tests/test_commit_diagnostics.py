"""Commit success/warning classifications using synthetic native-output shapes."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from commit_diagnostics import classify_commit_output
from changelist_ops import MutationUncertain
import test_commits


def native(stdout='',stderr='',code=0):
    return SimpleNamespace(stdout=stdout,stderr=stderr,returncode=code)


class CommitDiagnosticsTests(unittest.TestCase):
    def test_reported_windows_postcommit_failure_preserves_existing_revision(self):
        # Shape copied from the sanitized Windows disposable-repository report;
        # no real path, credential or raw user diagnostic is exported here.
        output=('Sending        src\\中文 @.txt\r\nTransmitting file data .done\r\n'
                'Committing transaction...\r\nCommitted revision 3.\r\n\r\n'
                'Warning: post-commit hook failed (exit code 1) with output:\r\n'
                'ISOLATED_POST_COMMIT_FAILURE\r\r\n')
        result=classify_commit_output(native(output))
        self.assertTrue(result['commandCompleted']); self.assertEqual(result['outcome'],'committed')
        self.assertEqual(result['reportedRevision'],'3'); self.assertFalse(result['automaticRetryAllowed'])
        self.assertIn('post-commit-hook',result['warnings']); self.assertIn('additional-output',result['warnings'])
        self.assertNotIn('ISOLATED',repr(result)); self.assertNotIn('中文',repr(result))

    def test_normal_english_progress_has_no_false_warning(self):
        output=('Adding  (bin) data.bin\nDeleting       old.txt\nReplacing      newer.txt\n'
                'Sending        changed.txt\nTransmitting file data ...done\n'
                'Committing transaction...\nCommitted revision 42.\n')
        result=classify_commit_output(native(output))
        self.assertEqual(result['reportedRevision'],'42'); self.assertEqual(result['warnings'],[])

    def test_unknown_or_localized_suffix_preserves_revision_and_warns(self):
        for extra in ['未知的提交后警告','NOTICE custom hook text','Server diagnostic https://user:secret@example.test']:
            with self.subTest(extra=extra):
                result=classify_commit_output(native('Committed revision 8.\n'+extra))
                self.assertEqual(result['outcome'],'committed'); self.assertEqual(result['reportedRevision'],'8')
                self.assertEqual(result['warnings'],['additional-output']); self.assertNotIn(extra,repr(result))

    def test_localized_or_unknown_success_is_not_plain_success(self):
        for text in ['已提交版本 8。','Révision 8 propagée.','All done','','\r\n\t']:
            result=classify_commit_output(native(text))
            self.assertTrue(result['commandCompleted']); self.assertEqual(result['outcome'],'completed-unverified')
            self.assertIsNone(result['reportedRevision']); self.assertIn('unrecognized-output',result['warnings'])
            self.assertFalse(result['automaticRetryAllowed'])

    def test_unknown_prefix_and_control_injection_do_not_forge_reported_revision(self):
        for output in ['hook payload\nCommitted revision 7.','Warning: text\nCommitted revision 7.',
                       '\x1b[2KCommitted revision 7.','prefix\u2028Committed revision 7.','Committed revision 7.\x00']:
            result=classify_commit_output(native(output))
            self.assertIsNone(result['reportedRevision']); self.assertIn('unrecognized-output',result['warnings'])

    def test_failure_even_with_success_marker_remains_uncertain(self):
        for code in [1,-1,2]:
            with self.assertRaises(MutationUncertain): classify_commit_output(native('Committed revision 7.','',code))

    def test_zero_exit_nonstring_diagnostics_are_qualified(self):
        for out,err in [(None,''),('',None),(b'Committed revision 7.','')]:
            result=classify_commit_output(native(out,err))
            self.assertTrue(result['commandCompleted']); self.assertIn('unrecognized-output',result['warnings'])
            self.assertIsNone(result['reportedRevision'])


@unittest.skipUnless((test_commits.BACKEND/'svn_client/history.py').is_file(),'Paired backend unavailable.')
class CommitWarningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_commits.CommitTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)

    def test_hook_warning_consumes_confirmation_and_does_not_retry(self):
        f=self.fixture
        f.client.command.return_value=f.backend.CommandResult((),0,'Committed revision 3.\nWarning: post-commit hook failed (exit code 1) with output:\nPRIVATE HOOK DETAILS','')
        plan=f.preflight(); result=f.apply(plan)
        self.assertEqual(result['reportedRevision'],'3'); self.assertIn('post-commit-hook',result['warnings'])
        self.assertNotIn('PRIVATE',repr(result)); self.assertIsNone(f.service.plan)
        from file_reads import StaleRead
        with self.assertRaises(StaleRead): f.apply(plan)
        f.client.command.assert_called_once(); f.invalidated.assert_called_once()


if __name__=='__main__': unittest.main()
