"""Independent adversarial lock review using only existing synthetic fixtures."""
import unittest
from dataclasses import replace
import xml.etree.ElementTree as ET
from unittest.mock import patch
import test_locks as fixture


@unittest.skipUnless((fixture.BACKEND / "svn_client/locks.py").is_file(), "Paired backend unavailable.")
class IndependentLockReview(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.LockTests('test_explicit_remote_preflight_no_mutation_and_private_metadata')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_remote_ambiguous_scope_and_identity_fail_closed(self):
        f = self.fx
        def malformed(raw, case):
            root = ET.fromstring(raw)
            if case == 'entry': root.append(ET.fromstring(ET.tostring(root[0])))
            elif case == 'uuid': ET.SubElement(root.find('entry/repository'), 'uuid').text = f.uuid
            elif case == 'commit': ET.SubElement(root[0], 'commit', revision='5')
            elif case == 'kind': root[0].set('kind', 'dir')
            elif case == 'root': root.tag = 'other'
            return ET.tostring(root)
        for case in ['entry', 'uuid', 'commit', 'kind', 'root']:
            f.bad_info = lambda raw, case=case: malformed(raw, case)
            with self.subTest(case=case), self.assertRaises(Exception): f.preflight()
            self.assertIsNone(f.service.plan)
        self.assertEqual(f.mutations, [])

    def test_reported_success_without_local_token_is_uncertain_and_consumed(self):
        f = self.fx
        plan = f.preflight()
        original = f.command
        def command(args, **kwargs):
            result = original(args, **kwargs)
            f.local[f.names[0]] = ''
            return result
        f.client.command.side_effect = command
        result = f.apply(plan)
        self.assertFalse(result['verified'])
        self.assertEqual(result['outcome'], 'uncertain')
        with self.assertRaises(fixture.StaleRead): f.apply(plan)
        self.assertEqual(len(f.mutations), 1)

    def test_status_refresh_never_queries_remote(self):
        f = self.fx
        with patch.object(f.service.manager, 'inspect', side_effect=AssertionError('silent remote query')):
            payload = f.adapter.snapshot()
        self.assertTrue(payload['locksEnabled'])
        self.assertEqual(f.remote_calls, [])
        self.assertEqual(f.mutations, [])

    def test_invalid_command_return_code_is_uncertain_even_if_state_achieved(self):
        f = self.fx
        plan = f.preflight()
        original = f.command
        def command(args, **kwargs):
            original(args, **kwargs)
            return f.backend.CommandResult((), 1, '', '')
        f.client.command.side_effect = command
        self.assertFalse(f.apply(plan)['verified'])
        self.assertEqual(len(f.mutations), 1)

    def test_root_admin_lock_blocks_preflight_and_later_apply(self):
        f = self.fx
        plan = f.preflight()
        remote_count = len(f.remote_calls)
        f.entries.append(replace(f.backend.StatusEntry('.', 'normal'), wc_locked=True))
        with self.assertRaises(ValueError): f.apply(plan)
        self.assertEqual(len(f.remote_calls), remote_count)
        self.assertEqual(f.mutations, [])
        f.remote_calls.clear()
        with self.assertRaises(ValueError): f.preflight()
        self.assertEqual(f.remote_calls, [])
