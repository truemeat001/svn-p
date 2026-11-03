"""Display-free integration of exact UI scope, consent and async operation results."""
import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import StatusEntry, SvnError
from svn_client.fileops import FileOperationError, FileOperationPlan
from svn_client.locks import FileLockInfo, LockError, LockOperationError, LockPlan, LockResult
from svn_client.operation_ui import OperationUiMixin, path_keys, same_scope
from svn_client.ui import SvnDesk


def entry(path="src/a.txt", item="normal", **kw):
    return StatusEntry(path, item, revision=8, **kw)


def lock_info(path="src/a.txt", **kw):
    return replace(FileLockInfo(path, "https://repo.test/src/a.txt", "uuid", 8, 7, False), **kw)


def file_plan(operation="delete", paths=("src/a.txt",), destination=None):
    return FileOperationPlan(operation, "/selected-wc", paths, destination,
        paths + ((destination,) if destination else ()), (), (), "a" * 64, ())


def context_app():
    client = SimpleNamespace(working_copy=Path("/selected-wc"))
    app = Mock(client=client, _closing=False, _busy=False, demo=False)
    app.tabs.index.return_value = 0
    app.changes.selection.return_value = ("selected",)
    app._change_lookup = {"selected": entry()}
    app._group_lookup = {"group": "Feature"}
    app._file_lookup = {"selected": "src/a.txt", "dir": "src"}
    app._file_kind = {"selected": "file", "dir": "dir"}
    app.files.selection.return_value = ("selected",)
    app.changelist_candidates = [entry()]
    app._can_operate.return_value = True
    app._file_action_context.return_value = (0, ("src/a.txt",))
    app._file_action_available.return_value = True
    app._file_context_matches.return_value = True
    app._operation_scope_text.return_value = '工作副本："/selected-wc"\n"src/a.txt"'
    return app, client


class OperationScopeTests(unittest.TestCase):
    def test_same_scope_supports_windows_separators_but_not_partial_or_empty(self):
        self.assertTrue(same_scope(("src/a.txt",), ("src\\a.txt",)))
        self.assertFalse(same_scope((), ()))
        self.assertFalse(same_scope(("a", "b"), ("a",)))
        self.assertFalse(same_scope(("a", "a"), ("a",)))

    def test_exact_file_rows_only_across_pending_and_files_tabs(self):
        app, _ = context_app()
        self.assertEqual(OperationUiMixin._file_action_context(app), (0, ("src/a.txt",)))
        app.changes.selection.return_value = ("selected", "group")
        self.assertIsNone(OperationUiMixin._file_action_context(app))
        app.tabs.index.return_value = 1
        self.assertEqual(OperationUiMixin._file_action_context(app), (1, ("src/a.txt",)))
        app.files.selection.return_value = ("selected", "dir")
        self.assertIsNone(OperationUiMixin._file_action_context(app))
        app.tabs.index.return_value = 3
        self.assertIsNone(OperationUiMixin._file_action_context(app))

    def test_workspace_tab_selection_or_closed_window_changes_invalidate_context(self):
        app, client = context_app()
        context = (0, ("src/a.txt",))
        self.assertTrue(OperationUiMixin._file_context_matches(app, client, context))
        self.assertFalse(OperationUiMixin._file_context_matches(app, object(), context))
        app._file_action_context.return_value = (1, context[1])
        self.assertFalse(OperationUiMixin._file_context_matches(app, client, context))
        app._file_action_context.return_value = (0, ("other",))
        self.assertFalse(OperationUiMixin._file_context_matches(app, client, context))
        app._closing = True
        self.assertFalse(OperationUiMixin._file_context_matches(app, client, context))

    def test_demo_busy_and_missing_candidate_disable_operations(self):
        app, _ = context_app()
        self.assertTrue(OperationUiMixin._file_action_available(app, "file_delete"))
        for key in ("demo", "_busy"):
            setattr(app, key, True)
            self.assertFalse(OperationUiMixin._file_action_available(app, "file_delete"))
            setattr(app, key, False)
        app.changelist_candidates = []
        self.assertFalse(OperationUiMixin._file_action_available(app, "file_lock"))

    def test_dirty_delete_disallowed_and_unsafe_states_refuse_entire_selection(self):
        app, _ = context_app()
        for value in (entry(item="modified"), entry(props="modified")):
            app.changelist_candidates = [value]
            self.assertFalse(OperationUiMixin._file_action_available(app, "file_delete"))
            self.assertTrue(OperationUiMixin._file_action_available(app, "file_move"))
        for value in (entry(item="added"), entry(props="conflicted"), entry(file_external=True), entry(copied=True), entry(switched=True), entry(wc_locked=True)):
            app.changelist_candidates = [value]
            self.assertFalse(OperationUiMixin._file_action_available(app, "file_lock"))
            self.assertFalse(OperationUiMixin._file_action_available(app, "file_copy"))

    def test_multi_source_move_and_excessive_batch_disabled(self):
        app, _ = context_app()
        app._file_action_context.return_value = (0, ("a", "b"))
        app.changelist_candidates = [entry("a"), entry("b")]
        self.assertFalse(OperationUiMixin._file_action_available(app, "file_move"))
        self.assertTrue(OperationUiMixin._file_action_available(app, "file_delete"))
        app._file_action_context.return_value = (0, tuple(str(i) for i in range(129)))
        self.assertFalse(OperationUiMixin._file_action_available(app, "file_delete"))

    def test_callbacks_in_mixin_resolve_on_real_window_class(self):
        tree = ast.parse(inspect.getsource(OperationUiMixin))
        targets = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" and callable(getattr(OperationUiMixin, node.attr, None)):
                targets.add(node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                targets.add(node.func.attr)
        self.assertTrue(targets)
        self.assertEqual([name for name in targets if not callable(getattr(SvnDesk, name, None))], [])


class FileOperationFlowTests(unittest.TestCase):
    def start(self, operation="delete", destination=None):
        app, client = context_app()
        with patch("svn_client.fileops.FileOperationManager") as manager, patch("svn_client.fileops_dialogs.FileDestinationDialog") as dialog:
            dialog.return_value.result = destination
            OperationUiMixin._begin_file_operation(app, operation)
        return app, client, manager.return_value

    def test_cancel_destination_never_preflights_or_modifies(self):
        app, _, manager = self.start("move", None)
        app._run.assert_not_called()
        manager.move.assert_not_called()

    @patch("svn_client.fileops_dialogs.FileOperationConfirmDialog")
    def test_final_cancel_never_schedules_mutation(self, dialog):
        app, _, manager = self.start()
        dialog.return_value.confirmed = False
        app._run.call_args.args[2](file_plan())
        app._run_reviewed_operation.assert_not_called()
        manager.delete.assert_not_called()

    @patch("svn_client.fileops_dialogs.FileOperationConfirmDialog")
    def test_exact_plan_token_and_targets_pass_only_after_confirmation(self, dialog):
        for operation, destination in (("delete", None), ("move", "src/new.txt"), ("copy", "src/new.txt")):
            app, _, manager = self.start(operation, destination)
            dialog.return_value.confirmed = True
            app._run.call_args.args[1]()
            app._run.call_args.args[2](file_plan(operation, destination=destination))
            app._run_reviewed_operation.call_args.args[1]()
            if operation == "delete":
                manager.delete.assert_called_once_with(("src/a.txt",), confirmed=True, expected_state_sha256="a" * 64)
            else:
                getattr(manager, operation).assert_called_once_with("src/a.txt", "src/new.txt", confirmed=True, expected_state_sha256="a" * 64)

    @patch("svn_client.operation_ui.messagebox.showerror")
    @patch("svn_client.fileops_dialogs.FileOperationConfirmDialog")
    def test_mismatched_plan_scope_operation_destination_or_workspace_rejected(self, dialog, error):
        good = file_plan()
        bad_plans = (replace(good, operation="move"), replace(good, paths=("other",)),
            replace(good, affected_paths=("other",)), replace(good, destination="unexpected"),
            replace(good, workspace="/another-wc"), replace(good, scope="directory"))
        for value in bad_plans:
            app, _, _ = self.start()
            app._run.call_args.args[2](value)
            app._run_reviewed_operation.assert_not_called()
        dialog.assert_not_called()

    @patch("svn_client.fileops_dialogs.FileOperationConfirmDialog")
    def test_context_changed_during_preflight_or_confirmation_never_executes(self, dialog):
        for responses in ([False], [True, False]):
            app, _, manager = self.start()
            dialog.return_value.confirmed = True
            app._file_context_matches.side_effect = responses
            app._run.call_args.args[2](file_plan())
            app._run_reviewed_operation.assert_not_called()
            manager.delete.assert_not_called()

    def test_demo_or_missing_scope_never_opens_work(self):
        app, _ = context_app()
        app._can_operate.return_value = False
        OperationUiMixin._begin_file_operation(app, "delete")
        app._run.assert_not_called()


class LockOperationFlowTests(unittest.TestCase):
    def start(self, action="lock", comment="备注"):
        app, client = context_app()
        manager = Mock()
        OperationUiMixin._preflight_lock_operation(app, manager, client, (0, ("src/a.txt",)), action, comment)
        return app, client, manager, app._run.call_args.args[2]

    @patch("svn_client.operation_ui.LockInspectDialog")
    @patch("svn_client.locks.LockManager")
    def test_inspection_cancel_and_stale_context_do_not_preflight(self, manager, dialog):
        app, client = context_app()
        OperationUiMixin.inspect_locks(app)
        inspected = app._run.call_args.args[2]
        dialog.return_value.result = None
        inspected((lock_info(),))
        app._preflight_lock_operation.assert_not_called()
        dialog.reset_mock()
        app._file_context_matches.return_value = False
        inspected((lock_info(),))
        dialog.assert_not_called()

    @patch("svn_client.operation_ui.messagebox.showerror")
    @patch("svn_client.operation_ui.LockInspectDialog")
    @patch("svn_client.locks.LockManager")
    def test_partial_inspection_is_not_silently_actionable(self, manager, dialog, error):
        app, client = context_app()
        OperationUiMixin.inspect_locks(app)
        app._run.call_args.args[2]((lock_info("other"),))
        dialog.assert_not_called()

    @patch("svn_client.operation_ui.LockConfirmDialog")
    def test_confirmation_cancel_and_changed_context_never_schedule_write(self, dialog):
        app, _, manager, checked = self.start()
        dialog.return_value.confirmed = False
        checked(LockPlan("lock", (lock_info(),), "a" * 64, "备注"))
        app._run_reviewed_operation.assert_not_called()
        dialog.return_value.confirmed = True
        app._file_context_matches.side_effect = [True, False]
        checked(LockPlan("lock", (lock_info(),), "a" * 64, "备注"))
        app._run_reviewed_operation.assert_not_called()
        manager.lock.assert_not_called()

    @patch("svn_client.operation_ui.LockConfirmDialog")
    def test_exact_action_comment_and_token_used_for_lock_and_unlock(self, dialog):
        for action, comment, value in (("lock", "备注", lock_info()), ("unlock", "", lock_info(locked=True, owned_by_working_copy=True))):
            app, _, manager, checked = self.start(action, comment)
            dialog.return_value.confirmed = True
            plan = LockPlan(action, (value,), "a" * 64, comment)
            checked(plan)
            getattr(manager, action).return_value = LockResult(action, (value,))
            app._run_reviewed_operation.call_args.args[1]()
            if action == "lock":
                manager.lock.assert_called_once_with(("src/a.txt",), "备注", confirmed=True, expected_state="a" * 64)
            else:
                manager.unlock.assert_called_once_with(("src/a.txt",), confirmed=True, expected_state="a" * 64)

    @patch("svn_client.operation_ui.messagebox.showerror")
    @patch("svn_client.operation_ui.LockConfirmDialog")
    def test_changed_comment_scope_action_or_ownership_rejected(self, dialog, error):
        good = LockPlan("lock", (lock_info(),), "a" * 64, "备注")
        for value in (replace(good, comment="different"), replace(good, action="unlock"),
                      replace(good, files=(lock_info("other"),)), replace(good, files=(lock_info(locked=True),))):
            app, _, _, checked = self.start()
            checked(value)
            app._run_reviewed_operation.assert_not_called()
        dialog.assert_not_called()

    @patch("svn_client.operation_ui.LockConfirmDialog")
    def test_unverified_or_partial_result_is_uncertain_not_success(self, dialog):
        app, _, manager, checked = self.start()
        dialog.return_value.confirmed = True
        checked(LockPlan("lock", (lock_info(),), "a" * 64, "备注"))
        manager.lock.return_value = LockResult("lock", (), verified=True)
        with self.assertRaises(LockOperationError):
            app._run_reviewed_operation.call_args.args[1]()


class OutcomeHandlingTests(unittest.TestCase):
    def start(self):
        app, client = context_app()
        context = (0, ("src/a.txt",))
        OperationUiMixin._run_reviewed_operation(app, "operation", Mock(), client, context,
            '/selected-wc\n"src/a.txt"', "verified", lambda err: isinstance(err, FileOperationError))
        return app, client

    @patch("svn_client.operation_ui.messagebox.showwarning")
    def test_uncertain_error_refreshes_once_and_never_retries(self, warning):
        app, _ = self.start()
        app._run.call_args.kwargs["on_error"](SvnError("timeout"))
        self.assertEqual(app._run.call_count, 1)
        app.refresh.assert_called_once_with()
        self.assertIn("可能已经改变", warning.call_args.args[1])
        self.assertIn("/selected-wc", warning.call_args.args[1])

    @patch("svn_client.operation_ui.messagebox.showwarning")
    def test_precommand_refusal_is_distinct_and_does_not_replace_new_details(self, warning):
        app, _ = self.start()
        app.client = object()
        app._file_context_matches.return_value = False
        app._run.call_args.kwargs["on_error"](FileOperationError("stale"))
        self.assertIn("执行前被拒绝", warning.call_args.args[1])
        app.refresh.assert_not_called()
        app._show_preview.assert_not_called()

    def test_completion_does_not_refresh_other_workspace_or_replace_new_selection(self):
        app, _ = self.start()
        app.client = object()
        app._file_context_matches.return_value = False
        app._run.call_args.args[2](SimpleNamespace(stdout="", stderr=""))
        app.refresh.assert_not_called()
        app._show_preview.assert_not_called()
        app._append_log.assert_called_once()

    @patch("svn_client.operation_ui.messagebox.showwarning")
    def test_partial_lock_error_displays_only_public_observed_state(self, warning):
        app, _ = self.start()
        error = LockOperationError("partial", (lock_info(locked=True, owner="artist"),))
        app._run.call_args.kwargs["on_error"](error)
        self.assertIn("随后读取的服务器状态", warning.call_args.args[1])
        self.assertIn("artist", warning.call_args.args[1])
        self.assertEqual(app._run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
