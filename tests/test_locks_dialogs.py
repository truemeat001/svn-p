"""Headless consent/state tests; no native Tk visual acceptance claim."""
import ast
import inspect
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.locks import FileLockInfo, LockPlan
from svn_client.locks_dialogs import (LockConfirmDialog, LockInspectDialog, can_lock, can_unlock, lock_state_text)


def info(path="data/asset.png", **changes):
    value = FileLockInfo(path, "https://repo.test/assets/asset.png", "uuid", 8, 7, False)
    return replace(value, **changes)


class LockDialogTests(unittest.TestCase):
    def test_neither_empty_nor_mixed_selection_is_silently_eligible(self):
        self.assertFalse(can_lock([]))
        self.assertFalse(can_unlock([]))
        owned = info(locked=True, owner="artist", owned_by_working_copy=True, local_token_present=True)
        self.assertFalse(can_lock([info(), owned]))
        self.assertFalse(can_unlock([info(), owned]))
        self.assertTrue(can_lock([info()]))
        self.assertTrue(can_unlock([owned]))

    def test_same_username_without_this_working_copy_ownership_cannot_unlock(self):
        self.assertFalse(can_unlock([info(locked=True, owner="same-user", local_token_present=True)]))
        self.assertFalse(can_lock([info(stale_local_token=True)]))
        self.assertFalse(can_lock([info(working_revision=4, server_last_changed_revision=8)]))

    def test_renderer_uses_allowlisted_fields_not_repr_or_raw_tokens(self):
        value = info(path="目录/a\nnext", owner='user"one', comment="note\nline")
        fake = SimpleNamespace(**value.__dict__, token="SECRET-TOKEN-DO-NOT-PRINT")
        rendered = lock_state_text([fake])
        self.assertNotIn("SECRET-TOKEN", rendered)
        self.assertIn('"目录/a\\nnext"', rendered)
        self.assertIn('"note\\nline"', rendered)
        self.assertNotIn(value.url, rendered)

    @patch("svn_client.locks_dialogs.messagebox.showwarning")
    def test_accept_requires_explicit_eligible_mode(self, warning):
        dialog = Mock(files=(info(),), result=None)
        dialog.mode_var.get.return_value = ""
        LockInspectDialog._accept(dialog)
        self.assertIsNone(dialog.result)
        dialog.destroy.assert_not_called()
        dialog.mode_var.get.return_value = "unlock"
        LockInspectDialog._accept(dialog)
        self.assertIsNone(dialog.result)

    def test_lock_preserves_exact_comment_and_unlock_does_not_transmit_it(self):
        dialog = Mock(files=(info(),), result=None)
        dialog.mode_var.get.return_value = "lock"
        dialog.comment.get.return_value = "  明确备注\n内容  "
        LockInspectDialog._accept(dialog)
        self.assertEqual(dialog.result, ("lock", "  明确备注\n内容  "))
        dialog.files = (info(locked=True, owned_by_working_copy=True),)
        dialog.mode_var.get.return_value = "unlock"
        dialog.comment.reset_mock()
        LockInspectDialog._accept(dialog)
        self.assertEqual(dialog.result, ("unlock", ""))
        dialog.comment.get.assert_not_called()

    @patch("svn_client.locks_dialogs.messagebox.showwarning")
    def test_invalid_comment_refuses_without_close(self, warning):
        for value in ("字" * 1400, "secret\x00value", "\ud800"):
            dialog = Mock(files=(info(),), result=None)
            dialog.mode_var.get.return_value = "lock"
            dialog.comment.get.return_value = value
            LockInspectDialog._accept(dialog)
            self.assertIsNone(dialog.result)
            dialog.destroy.assert_not_called()

    def test_comment_enabled_only_for_valid_lock_mode(self):
        for mode, state in (("", "disabled"), ("unlock", "disabled"), ("lock", "normal")):
            dialog = Mock(files=(info(),))
            dialog.mode_var.get.return_value = mode
            LockInspectDialog._mode_changed(dialog)
            dialog.comment.configure.assert_called_once_with(state=state)

    @patch("svn_client.locks_dialogs.messagebox.showwarning")
    def test_risk_ack_is_required(self, warning):
        dialog = Mock(confirmed=False)
        dialog.risk_var.get.return_value = False
        LockConfirmDialog._confirm(dialog)
        self.assertFalse(dialog.confirmed)
        dialog.destroy.assert_not_called()
        dialog.risk_var.get.return_value = True
        LockConfirmDialog._confirm(dialog)
        self.assertTrue(dialog.confirmed)

    def test_invalid_plan_rejected_before_constructing_native_window(self):
        parent = Mock()
        for value in (LockPlan("force", (info(),), "a" * 64), LockPlan("unlock", (info(),), "a" * 64), LockPlan("lock", (), "a" * 64)):
            with patch("svn_client.locks_dialogs.tk.Toplevel.__init__") as init, self.assertRaises(ValueError):
                LockConfirmDialog(parent, value, "/wc")
            init.assert_not_called()

    def test_callbacks_exist_and_no_default_action_or_enter_execution(self):
        for cls in (LockInspectDialog, LockConfirmDialog):
            source = inspect.getsource(cls)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    self.assertTrue(callable(getattr(cls, node.func.attr, None)), node.func.attr)
                for kw in node.keywords:
                    if kw.arg == "command" and isinstance(kw.value, ast.Attribute) and isinstance(kw.value.value, ast.Name) and kw.value.value.id == "self":
                        self.assertTrue(callable(getattr(cls, kw.value.attr, None)))
            self.assertNotIn('<Return>', source)
            self.assertIn('focus_set()', source)
        self.assertIn('tk.StringVar(value="")', inspect.getsource(LockInspectDialog))
        self.assertIn('tk.BooleanVar(value=False)', inspect.getsource(LockConfirmDialog))


class LockDialogConstructionTests(unittest.TestCase):
    def setUp(self):
        self.parent = Mock()
        self.parent.display.px.side_effect = lambda value: value
        self.buttons = []
        self.modes = []
        self.variables = []
        self.patches = []
        def start(target, **kwargs):
            holder = patch(target, **kwargs)
            self.patches.append(holder)
            return holder.start()
        start("svn_client.locks_dialogs.tk.Toplevel.__init__", return_value=None)
        start("svn_client.locks_dialogs._size")
        self.text = start("svn_client.locks_dialogs._text")
        for name in ("title", "transient", "grab_set", "wait_window", "destroy"):
            start("svn_client.locks_dialogs.tk.Toplevel." + name)
        self.bind = start("svn_client.locks_dialogs.tk.Toplevel.bind")
        for name in ("Frame", "Label", "Checkbutton"):
            start("svn_client.locks_dialogs.ttk." + name)
        def button(*args, **kwargs):
            value = Mock()
            self.buttons.append((kwargs, value))
            return value
        def mode(*args, **kwargs):
            self.modes.append(kwargs)
            return Mock()
        def variable(*args, **kwargs):
            self.variables.append(kwargs)
            return Mock()
        start("svn_client.locks_dialogs.ttk.Button", side_effect=button)
        start("svn_client.locks_dialogs.ttk.Radiobutton", side_effect=mode)
        start("svn_client.locks_dialogs.tk.StringVar", side_effect=variable)
        start("svn_client.locks_dialogs.tk.BooleanVar", side_effect=variable)

    def tearDown(self):
        for holder in reversed(self.patches):
            holder.stop()

    def test_inspect_starts_without_mode_and_cancel_focus(self):
        dialog = LockInspectDialog(self.parent, (info(),), "/wc")
        self.assertIsNone(dialog.result)
        self.assertEqual(self.variables, [{"value": ""}])
        self.assertEqual([value["state"] for value in self.modes], ["normal", "disabled"])
        self.assertEqual(self.buttons[-1][0]["state"], "disabled")
        self.buttons[0][1].focus_set.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.bind.call_args_list], ["<Escape>"])

    def test_mixed_states_offer_no_implicit_subset(self):
        LockInspectDialog(self.parent, (info(), info(locked=True, owned_by_working_copy=True)), "/wc")
        self.assertTrue(all(value["state"] == "disabled" for value in self.modes))

    def test_confirmation_defaults_no_consent_and_disabled_execute(self):
        dialog = LockConfirmDialog(self.parent, LockPlan("lock", (info(),), "a" * 64), "/wc")
        self.assertFalse(dialog.confirmed)
        self.assertEqual(self.variables, [{"value": False}])
        self.assertEqual(self.buttons[-1][0]["state"], "disabled")
        self.buttons[0][1].focus_set.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
