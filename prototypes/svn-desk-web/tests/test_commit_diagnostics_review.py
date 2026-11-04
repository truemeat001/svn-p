"""Independent diagnostic safety review; synthetic output and mocked dispatch only."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import commit_ops
from changelist_ops import MutationUncertain
from file_reads import StaleRead
import test_commits


class DiagnosticReview(unittest.TestCase):
    def classify(self, stdout='', stderr='', code=0):
        return commit_ops.classify_commit_output(SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr))

    def assert_completed_warning(self, result):
        self.assertIs(result['commandCompleted'], True)
        self.assertIs(result['automaticRetryAllowed'], False)
        self.assertTrue(result['warnings'])

    def test_reported_revision_is_string_not_javascript_number(self):
        result = self.classify('Committed revision 9007199254740993.\r\n')
        self.assertEqual(result['reportedRevision'], '9007199254740993')
        self.assertEqual(result['outcome'], 'committed')

    def test_stdout_post_commit_hook_warning_keeps_commit_and_hides_secrets(self):
        secret = 'PRIVATE-HOOK-TOKEN-42'
        result = self.classify('Committed revision 42.\nWarning: post-commit hook failed (exit code 1) with output:\n' + secret)
        self.assert_completed_warning(result)
        self.assertEqual(result['outcome'], 'committed')
        self.assertEqual(result['reportedRevision'], '42')
        self.assertIn('post-commit-hook', result['warnings'])
        self.assertNotIn(secret, json.dumps(result))

    def test_stderr_zero_is_completed_with_static_warning(self):
        result = self.classify('Committed revision 42.\n', 'https://user:PRIVATE-PASSWORD@server.test/repo')
        self.assert_completed_warning(result)
        self.assertEqual(result['reportedRevision'], '42')
        self.assertIn('stderr-output', result['warnings'])
        self.assertNotIn('PRIVATE-PASSWORD', json.dumps(result))
        self.assertNotIn('server.test', json.dumps(result))

    def test_localized_empty_and_unknown_output_never_plain_success(self):
        for output in ['', '已提交版本 42。\n', 'PRIVATE TOKEN\n', 'arbitrary Committed revision 42.\n']:
            with self.subTest(output=output):
                result = self.classify(output)
                self.assert_completed_warning(result)
                self.assertEqual(result['outcome'], 'completed-unverified')
                self.assertIsNone(result['reportedRevision'])
                self.assertNotIn('PRIVATE TOKEN', json.dumps(result))

    def test_revision_conflicts_malformed_and_overflow_are_not_trusted(self):
        for output in ['Committed revision 42.\nCommitted revision 43.\n',
                       'Committed revision 42.\nCommitted revision 42.\n',
                       'Committed revision ４２.\n', 'Committed revision 0.\n',
                       'Committed revision 9223372036854775808.\n',
                       'Committed revision ' + '9' * 5000 + '.\n',
                       'SECRET PREFIX\nCommitted revision 42.\n']:
            with self.subTest(output=output[:80]):
                result = self.classify(output)
                self.assert_completed_warning(result)
                self.assertIsNone(result['reportedRevision'])
                self.assertEqual(result['outcome'], 'completed-unverified')

    def test_oversized_success_output_is_bounded_unverified(self):
        for stdout, stderr in [('Committed revision 42.\n' + 'X' * (128 * 1024), ''),
                               ('Committed revision 42.\n', 'X' * (64 * 1024 + 1))]:
            result = self.classify(stdout, stderr)
            self.assert_completed_warning(result)
            self.assertIsNone(result['reportedRevision'])
            self.assertIn('output-limit', result['warnings'])
            self.assertLess(len(json.dumps(result)), 1024)


@unittest.skipUnless((test_commits.BACKEND / 'svn_client/history.py').is_file(), 'Paired backend unavailable.')
class DispatchDiagnosticReview(unittest.TestCase):
    def setUp(self):
        self.fixture = test_commits.CommitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_hook_warning_response_consumes_one_use_plan(self):
        f = self.fixture
        f.client.command.return_value = f.backend.CommandResult((), 0,
            'Committed revision 42.\nWarning: post-commit hook failed\nPRIVATE TOKEN', '')
        plan = f.preflight()
        result = f.apply(plan)
        self.assertTrue(result['commandCompleted'])
        self.assertTrue(result['warnings'])
        self.assertNotIn('PRIVATE TOKEN', json.dumps(result))
        with self.assertRaises(StaleRead):
            f.apply(plan)
        f.client.command.assert_called_once()
        f.invalidated.assert_called_once()

    def test_nonzero_with_commit_marker_stays_uncertain_no_replay(self):
        f = self.fixture
        f.client.command.return_value = f.backend.CommandResult((), 1, 'Committed revision 42.\nPRIVATE TOKEN', '')
        plan = f.preflight()
        with self.assertRaises(MutationUncertain) as caught:
            f.apply(plan)
        self.assertNotIn('PRIVATE TOKEN', str(caught.exception))
        with self.assertRaises(StaleRead):
            f.apply(plan)
        f.client.command.assert_called_once()

    def test_exception_secret_is_not_reflected_and_plan_is_consumed(self):
        f = self.fixture
        f.client.command.side_effect = RuntimeError('https://user:PRIVATE-PASSWORD@server.test')
        plan = f.preflight()
        with self.assertRaises(MutationUncertain) as caught:
            f.apply(plan)
        self.assertNotIn('PRIVATE-PASSWORD', str(caught.exception))
        with self.assertRaises(StaleRead):
            f.apply(plan)
        f.client.command.assert_called_once()


class FollowupStatusDiagnosticReview(unittest.TestCase):
    def test_catalog_workspace_postcheck_retains_warning_and_revokes_capabilities(self):
        import test_commit_bridge
        from server import ReadOnlyAdapter
        fixture = test_commit_bridge.CommitCatalogTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = fixture.adapter
        adapter.workspace = fixture.wc
        adapter._commit_status_unknown = MethodType(ReadOnlyAdapter._commit_status_unknown, adapter)
        adapter.commit_operation.return_value = {'commitResult': {
            'commandCompleted': True, 'outcome': 'committed', 'reportedRevision': '42',
            'automaticRetryAllowed': False, 'warnings': ['post-commit-hook']}}
        fixture.catalog._check = Mock(side_effect=[None, RuntimeError('PRIVATE POSTCHECK DETAIL')])
        result = fixture.catalog.commit_operation('apply', test_commit_bridge.scope(planToken='p', confirmed=True))
        self.assertTrue(result['commitResult']['commandCompleted'])
        self.assertEqual(result['commitResult']['reportedRevision'], '42')
        self.assertEqual(result['commitResult']['warnings'], ['post-commit-hook', 'workspace-changed'])
        self.assertFalse(result['statusRefreshed'])
        self.assertIsNone(result['snapshotToken'])
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['workspaceId'], 'w')
        self.assertEqual(result['selectionVersion'], 1)
        for flag in ['fileReadsEnabled', 'changelistWritesEnabled', 'shelfWritesEnabled', 'commitsEnabled']:
            self.assertFalse(result[flag])
        self.assertNotIn('PRIVATE POSTCHECK DETAIL', json.dumps(result))
        adapter.commit_operation.assert_called_once()
        self.assertFalse(fixture.catalog._busy.locked())

    def test_catalog_does_not_reenable_capabilities_after_failed_status(self):
        import test_commit_bridge
        from server import ReadOnlyAdapter
        fixture = test_commit_bridge.CommitCatalogTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        adapter = fixture.adapter
        adapter.workspace = fixture.wc
        result = {'commandCompleted': True, 'outcome': 'committed', 'reportedRevision': '42',
                  'automaticRetryAllowed': False, 'warnings': []}
        adapter.commit_operation.return_value = ReadOnlyAdapter._commit_status_unknown(adapter, result, 'status-refresh-failed')
        payload = fixture.catalog.commit_operation('apply', test_commit_bridge.scope(planToken='p', confirmed=True))
        self.assertFalse(payload['statusRefreshed'])
        for flag in ['fileReadsEnabled', 'changelistWritesEnabled', 'shelfWritesEnabled', 'commitsEnabled', 'writesEnabled']:
            self.assertFalse(payload[flag])
        self.assertEqual(payload['workspaceId'], 'w')
        self.assertEqual(payload['selectionVersion'], 1)

    def test_status_failure_preserves_unverified_completion_and_prior_warnings(self):
        import test_commit_bridge
        fixture = test_commit_bridge.CommitAdapterTests()
        fixture.setUp()
        adapter = fixture.adapter
        adapter._commit_service.apply.return_value = {
            'commandCompleted': True, 'outcome': 'completed-unverified', 'reportedRevision': None,
            'automaticRetryAllowed': False, 'warnings': ['unrecognized-output']}
        adapter.client.status.side_effect = RuntimeError('PRIVATE STATUS DETAIL')
        result = adapter.commit_operation('apply', test_commit_bridge.scope(planToken='p', confirmed=True))
        self.assertTrue(result['commitResult']['commandCompleted'])
        self.assertEqual(result['commitResult']['outcome'], 'completed-unverified')
        self.assertEqual(result['commitResult']['warnings'], ['unrecognized-output', 'status-refresh-failed'])
        self.assertIsNone(result['snapshotToken'])
        self.assertNotIn('PRIVATE STATUS DETAIL', json.dumps(result))
        adapter._commit_service.apply.assert_called_once()


if __name__ == '__main__':
    unittest.main()
