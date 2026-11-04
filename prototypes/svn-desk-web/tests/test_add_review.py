"""Independent add review regressions; fixture-only SVN responses."""
from dataclasses import replace
from unittest.mock import patch
import unittest
import test_adds as fixtures
from changelist_ops import MutationUncertain
from file_reads import StaleRead


class AddReviewTests(fixtures.AddTests):
    # Keep only independent tests in this subclass's unittest discovery.
    def test_review_root_is_checked_even_for_nested_files(self):
        original = self.entries[0]
        for change in ({'item': 'added'}, {'switched': True}, {'wc_locked': True},
                       {'file_external': True}, {'copied': True}, {'moved_from': 'old'}):
            with self.subTest(change=change):
                self.entries[0] = replace(original, **change)
                with self.assertRaises(ValueError):
                    self.preflight()
        self.client.command.assert_not_called()

    def test_review_expiry_during_apply_validation_never_writes(self):
        plan = self.preflight()
        original = self.service._verify
        def expire(*args, **kwargs):
            result = original(*args, **kwargs)
            # apply has consumed the service capability, but owns this plan.
            internal_plan['expires'] = 0
            return result
        internal_plan = self.service.plan
        with patch.object(self.service, '_verify', side_effect=expire):
            with self.assertRaises(StaleRead):
                self.apply(plan)
        self.client.command.assert_not_called()
        self.invalidate.assert_not_called()

    def test_review_post_add_workspace_failure_is_uncertain_and_one_use(self):
        plan = self.preflight()
        self.after_command = lambda: setattr(self.check, 'side_effect', ValueError('root changed'))
        with self.assertRaises(MutationUncertain):
            self.apply(plan)
        with self.assertRaises(StaleRead):
            self.apply(plan)
        self.assertEqual(self.client.command.call_count, 1)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(AddReviewTests(name) for name in
                              loader.getTestCaseNames(AddReviewTests)
                              if name.startswith('test_review_'))
