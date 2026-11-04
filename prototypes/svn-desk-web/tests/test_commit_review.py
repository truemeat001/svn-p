"""Independent adversarial review: synthetic temporary WC and mocked SVN only."""
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import test_commits
from file_reads import StaleRead
from changelist_ops import MutationUncertain
import server


@unittest.skipUnless((test_commits.BACKEND / 'svn_client/history.py').is_file(), 'Paired backend unavailable.')
class IndependentCommitReview(unittest.TestCase):
    def setUp(self):
        self.fixture = test_commits.CommitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_reviewed_backend_opt_in_and_hash_mismatch_preserves_reads(self):
        f = self.fixture
        with patch.object(f.backend, 'discover_svn', return_value='/trusted/svn'):
            default = server.ReadOnlyAdapter(f.root, test_commits.BACKEND)
            enabled = server.ReadOnlyAdapter(f.root, test_commits.BACKEND, enable_commits=True)
            with patch.object(server, 'COMMIT_BACKEND_SHA256', '0' * 64):
                disabled = server.ReadOnlyAdapter(f.root, test_commits.BACKEND, enable_commits=True)
        self.assertIsNone(default._commit_service)
        self.assertIsNotNone(enabled._commit_service)
        self.assertIsNone(disabled._commit_service)
        self.assertIsNotNone(disabled._file_service)
        self.assertIsNotNone(disabled.client)

    def test_repository_uuid_change_with_same_status_and_content_rejects(self):
        f = self.fixture
        plan = f.preflight()
        original = f.info_xml
        def changed(target):
            tree = ET.fromstring(original(target))
            tree.find('entry/repository/uuid').text = 'different-repository'
            return ET.tostring(tree)
        f.manager._run = Mock(side_effect=lambda args, limit: changed(args[-1]))
        with self.assertRaises(StaleRead):
            f.apply(plan)
        f.client.command.assert_not_called()

    def test_property_changes_during_review_reject(self):
        f = self.fixture
        original = f.properties_xml
        calls = 0
        def changing(target):
            nonlocal calls
            calls += 1
            f.props = {'custom': str(calls)}
            return original(target)
        f.service._property_bytes = Mock(side_effect=changing)
        with self.assertRaises(StaleRead):
            f.preflight()
        self.assertIsNone(f.service.plan)
        f.client.command.assert_not_called()

    def test_nonzero_exit_without_diagnostics_never_success_or_retry(self):
        f = self.fixture
        f.client.command.return_value = f.backend.CommandResult((), 1, '', '')
        plan = f.preflight()
        with self.assertRaises(MutationUncertain):
            f.apply(plan)
        with self.assertRaises(StaleRead):
            f.apply(plan)
        f.client.command.assert_called_once()
        f.invalidated.assert_called_once()

    def test_snapshot_refresh_even_identical_rows_invalidates_plan(self):
        f = self.fixture
        plan = f.preflight()
        f.snapshot = ('new-snapshot', f.snapshot[1])
        with self.assertRaises(StaleRead):
            f.apply(plan)
        f.client.command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
