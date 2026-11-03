"""Display-free regression coverage for explicit conflict GUI transitions."""
import ast
import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import StatusEntry, SvnError
from svn_client.conflicts import ConflictError
from svn_client.conflict_dialogs import (ConflictConfirmDialog, ConflictInspectDialog,
    can_keep_working, display_path, inspection_text)
from svn_client.ui import SvnDesk


def inspection(path="src/file.txt", *, props=False, tree=False, blocked=(), kind="file", allowed=("working",)):
    return SimpleNamespace(path=path, kind=kind, text_conflicted=True,
        property_conflicted=props, tree_conflicted=tree,
        status=StatusEntry(path, "conflicted", props="conflicted" if props else "normal", revision=7, tree_conflicted=tree),
        info={}, allowed_accepts=allowed, blocked_reasons=blocked, warnings=())


def plan(paths=("src/file.txt",), *, accept="working", inspections=None):
    return SimpleNamespace(paths=paths, accept=accept,
        inspections=tuple(inspection(path) for path in paths) if inspections is None else inspections,
        state_sha256="a" * 64, warnings=())


def app_context():
    client = object()
    app = Mock(client=client, _closing=False, _busy=False, demo=False)
    app.tabs.index.return_value = 0
    app.changes.selection.return_value = ("file",)
    app._change_lookup = {"file": StatusEntry("src/file.txt", "conflicted")}
    app._group_lookup = {}
    app._conflict_context_matches.return_value = True
    app._can_operate.return_value = True
    app._selected_entries.return_value = list(app._change_lookup.values())
    return app, client


class ConflictDialogTests(unittest.TestCase):
    def test_scope_requires_plain_content_only_working(self):
        self.assertTrue(can_keep_working([inspection()]))
        for entries in ([], [inspection(props=True)], [inspection(tree=True)],
                        [inspection(blocked=("unsafe_ancestor",))], [inspection(kind="dir")],
                        [inspection(allowed=("mine-full",))]):
            self.assertFalse(can_keep_working(entries))

    def test_mixed_selection_does_not_silently_skip_blocked_node(self):
        self.assertFalse(can_keep_working([inspection("a"), inspection("b", tree=True)]))

    def test_paths_quote_control_characters_without_hiding_unicode(self):
        rendered = display_path("目录/a\nnext.txt")
        self.assertNotIn("\n", rendered)
        self.assertIn("\\n", rendered)
        self.assertIn("目录", rendered)

    def test_tree_and_property_limits_are_explicit(self):
        text = inspection_text(inspection(props=True, tree=True, blocked=("unsafe_ancestor",)))
        self.assertIn("不解决属性或混合冲突", text)
        self.assertIn("不解决树冲突", text)
        self.assertIn("祖先节点", text)

    @patch("svn_client.conflict_dialogs.messagebox.showwarning")
    def test_no_mode_or_attestation_cannot_continue(self, warning):
        dialog = Mock(allowed=True, result=None)
        dialog.mode_var.get.return_value = ""
        dialog.ack_var.get.return_value = True
        ConflictInspectDialog._accept(dialog)
        self.assertIsNone(dialog.result)
        dialog.destroy.assert_not_called()
        dialog.mode_var.get.return_value = "working"
        dialog.ack_var.get.return_value = False
        ConflictInspectDialog._accept(dialog)
        dialog.destroy.assert_not_called()

    def test_explicit_working_and_manual_attestation_continue(self):
        dialog = Mock(allowed=True, result=None)
        dialog.mode_var.get.return_value = "working"
        dialog.ack_var.get.return_value = True
        ConflictInspectDialog._accept(dialog)
        self.assertEqual(dialog.result, "working")
        dialog.destroy.assert_called_once_with()

    def test_mode_change_clears_attestation(self):
        dialog = Mock()
        ConflictInspectDialog._mode_changed(dialog)
        dialog.ack_var.set.assert_called_once_with(False)

    @patch("svn_client.conflict_dialogs.messagebox.showwarning")
    def test_final_confirmation_requires_sidecar_risk_acknowledgement(self, warning):
        dialog = Mock(confirmed=False)
        dialog.risk_var.get.return_value = False
        ConflictConfirmDialog._confirm(dialog)
        self.assertFalse(dialog.confirmed)
        dialog.destroy.assert_not_called()
        dialog.risk_var.get.return_value = True
        ConflictConfirmDialog._confirm(dialog)
        self.assertTrue(dialog.confirmed)

    def test_dialog_callbacks_exist_and_no_accept_mode_default(self):
        for cls in (ConflictInspectDialog, ConflictConfirmDialog):
            tree = ast.parse(inspect.getsource(cls))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    self.assertTrue(callable(getattr(cls, node.func.attr, None)), node.func.attr)
        self.assertIn('tk.StringVar(value="")', inspect.getsource(ConflictInspectDialog))


class ConflictContextTests(unittest.TestCase):
    def test_matches_exact_selection_in_same_workspace(self):
        app, client = app_context()
        self.assertTrue(SvnDesk._conflict_context_matches(app, client, ("src/./file.txt",)))
        self.assertFalse(SvnDesk._conflict_context_matches(app, client, ("other.txt",)))

    def test_other_workspace_tab_group_or_closed_window_refuses(self):
        app, client = app_context()
        self.assertFalse(SvnDesk._conflict_context_matches(app, object(), ("src/file.txt",)))
        app.tabs.index.return_value = 1
        self.assertFalse(SvnDesk._conflict_context_matches(app, client, ("src/file.txt",)))
        app.tabs.index.return_value = 0
        app._group_lookup = {"group": "Task"}
        app.changes.selection.return_value = ("file", "group")
        self.assertFalse(SvnDesk._conflict_context_matches(app, client, ("src/file.txt",)))
        app._closing = True
        self.assertFalse(SvnDesk._conflict_context_matches(app, client, ("src/file.txt",)))

    @patch("svn_client.ui.messagebox.showinfo")
    def test_regular_files_or_group_headers_cannot_enter_flow(self, info):
        app, client = app_context()
        app._selected_entries.return_value = [StatusEntry("normal", "normal")]
        SvnDesk.inspect_conflicts(app)
        app._run.assert_not_called()
        app._selected_entries.return_value = [StatusEntry("conflicted", "conflicted")]
        app.changes.selection.return_value = ("file", "group")
        app._group_lookup = {"group": "Task"}
        SvnDesk.inspect_conflicts(app)
        app._run.assert_not_called()

    @patch("svn_client.ui.ConflictInspectDialog")
    @patch("svn_client.conflicts.ConflictManager")
    def test_stale_inspection_does_not_open_dialog(self, manager, dialog):
        app, client = app_context()
        SvnDesk.inspect_conflicts(app)
        completed = app._run.call_args.args[2]
        app._conflict_context_matches.return_value = False
        completed((inspection(),))
        dialog.assert_not_called()
        app._preflight_conflict_resolution.assert_not_called()

    @patch("svn_client.ui.ConflictInspectDialog")
    @patch("svn_client.conflicts.ConflictManager")
    def test_cancelled_or_unsupported_inspection_never_preflights_resolution(self, manager, dialog):
        app, client = app_context()
        SvnDesk.inspect_conflicts(app)
        completed = app._run.call_args.args[2]
        dialog.return_value.result = None
        completed((inspection(),))
        app._preflight_conflict_resolution.assert_not_called()
        dialog.return_value.result = "working"
        completed((inspection(tree=True),))
        app._preflight_conflict_resolution.assert_not_called()


class ConflictExecutionTests(unittest.TestCase):
    def start(self):
        app, client = app_context()
        manager = Mock(working_copy=Path(tempfile.gettempdir()) / "selected-wc")
        SvnDesk._preflight_conflict_resolution(app, manager, client, ("src/file.txt",))
        return app, client, manager, app._run.call_args.args[2]

    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_cancel_at_final_confirmation_never_resolves(self, dialog):
        app, client, manager, checked = self.start()
        dialog.return_value.confirmed = False
        checked(plan())
        self.assertEqual(app._run.call_count, 1)
        manager.resolve.assert_not_called()

    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_exact_plan_token_passed_only_after_confirmation(self, dialog):
        app, client, manager, checked = self.start()
        dialog.return_value.confirmed = True
        checked(plan())
        self.assertEqual(app._run.call_count, 2)
        app._run.call_args.args[1]()
        manager.resolve.assert_called_once_with(("src/file.txt",), "working", confirmed=True, expected_state_sha256="a" * 64)

    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_changed_context_after_confirmation_cancels_mutation(self, dialog):
        app, client, manager, checked = self.start()
        dialog.return_value.confirmed = True
        app._conflict_context_matches.side_effect = [True, False]
        checked(plan())
        self.assertEqual(app._run.call_count, 1)
        manager.resolve.assert_not_called()

    @patch("svn_client.ui.messagebox.showerror")
    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_mismatched_scope_mode_or_unsupported_plan_rejected(self, dialog, error):
        for bad_plan in (plan(("other",)), plan(accept="theirs-full"), plan(inspections=(inspection(props=True),))):
            app, client, manager, checked = self.start()
            checked(bad_plan)
            manager.resolve.assert_not_called()
            self.assertEqual(app._run.call_count, 1)
        dialog.assert_not_called()

    @patch("svn_client.ui.messagebox.showwarning")
    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_uncertain_failure_refreshes_once_without_retry(self, dialog, warning):
        app, client, manager, checked = self.start()
        dialog.return_value.confirmed = True
        checked(plan())
        failed = app._run.call_args.kwargs["on_error"]
        failed(SvnError("timeout"))
        self.assertEqual(app._run.call_count, 2)
        app.refresh.assert_called_once_with()
        self.assertIn("可能已处理", warning.call_args.args[1])
        self.assertIn(str(manager.working_copy), warning.call_args.args[1])
        manager.resolve.assert_not_called()

    @patch("svn_client.ui.messagebox.showwarning")
    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_precommand_refusal_and_changed_workspace_do_not_refresh_new_root(self, dialog, warning):
        app, client, manager, checked = self.start()
        dialog.return_value.confirmed = True
        checked(plan())
        failed = app._run.call_args.kwargs["on_error"]
        app.client = object()
        failed(ConflictError("state changed"))
        app.refresh.assert_not_called()
        self.assertIn("执行前被拒绝", warning.call_args.args[1])

    @patch("svn_client.ui.ConflictConfirmDialog")
    def test_completed_old_scope_does_not_replace_new_selection_details(self, dialog):
        app, client, manager, checked = self.start()
        dialog.return_value.confirmed = True
        checked(plan())
        completed = app._run.call_args.args[2]
        app._show_preview.reset_mock()
        app._conflict_context_matches.return_value = False
        completed(SimpleNamespace(stdout="Resolved", stderr=""))
        app._show_preview.assert_not_called()
        app.refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
