"""Conservative whole-WC GUI contracts; no real Tk/SVN verification claim."""
import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import StatusEntry
from svn_client.branches import BranchError, BranchOperationError, BranchPlan, BranchResult, CopyPlan
from svn_client.branches_dialogs import (BranchContext, BranchPlanDialog, BranchRequestDialog,
    make_request, plan_text, validate_plan)
from svn_client.branches_ui import BranchUiMixin, result_text, validate_result
from svn_client.ui import SvnDesk


ROOT = 'https://repo.test/project'
SOURCE = ROOT + '/branches/feature'
CONTEXT = BranchContext('/selected-wc', ROOT + '/trunk', 8, 'uuid', ROOT)


def request(operation='switch'):
    opts = {'start_revision': 8} if operation == 'merge' else {}
    if operation == 'copy_plan':
        opts.update(destination_url=ROOT + '/tags/v1', kind='tag', message='Snapshot')
    return make_request(operation, SOURCE, 12, **opts)


def plan(operation='switch', *, conflicts=False):
    if operation == 'copy_plan':
        return CopyPlan('tag', SOURCE, 12, ROOT + '/tags/v1', 'uuid', 14, 'Snapshot', 'a' * 64, ('No execution.',))
    return BranchPlan(operation, CONTEXT.workspace, SOURCE, 12, CONTEXT.url, 8, 'uuid', ROOT,
        8 if operation == 'merge' else None, 'a' * 64,
        ('C    file.txt\n' if conflicts else 'U    file.txt\n') if operation == 'merge' else '',
        conflicts, operation == 'merge', ('No commit.', 'Do not edit concurrently.'))


def result(operation='switch', *, conflicted=False):
    return BranchResult(operation, 'switched' if operation == 'switch' else 'conflicted' if conflicted else 'needs_review',
        CONTEXT.workspace, SOURCE if operation == 'switch' else CONTEXT.url, 12 if operation == 'switch' else 8,
        'output', () if operation == 'switch' else (StatusEntry('file.txt', 'conflicted' if conflicted else 'modified'),),
        ('file.txt',) if conflicted else ())


class Harness(BranchUiMixin):
    def __init__(self):
        self.client = SimpleNamespace(working_copy=Path(CONTEXT.workspace))
        self.root_path = Path(CONTEXT.workspace)
        self.info = dict(url=CONTEXT.url, revision=8, repository_uuid='uuid', repository_root=ROOT)
        self._branch_request = 0
        self._preview_request = 0
        self._busy = self._closing = self.demo = False
        self.pending, self.previews, self.logs = [], [], []
        self.status_var = Mock()
        self.refresh = Mock()
    def _can_operate(self):
        return not (self._busy or self.demo or self._closing) and self.client is not None
    def _run(self, label, work, done=None, *, on_error=None, preserve_view_on_error=False):
        self.pending.append((label, work, done, on_error, preserve_view_on_error))
        self._busy = True
    def _show_preview(self, title, text):
        self._preview_request += 1
        self.previews.append((title, text))
    def _append_log(self, text):
        self.logs.append(text)
    def finish(self, value):
        self._busy = False
        self.pending[-1][2](value)
    def fail(self, error):
        self._busy = False
        self.pending[-1][3](error)


class RequestAndPlanTests(unittest.TestCase):
    def test_numeric_revision_and_forward_range_are_required(self):
        for revision in ('HEAD', 'BASE', '8:12', -1, True, ''):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                make_request('switch', SOURCE, revision)
        for start, end in ((12, 8), (8, 8), (0, 65)):
            with self.assertRaises(ValueError):
                make_request('merge', SOURCE, end, start_revision=start)
        self.assertEqual(make_request('merge', SOURCE, 64, start_revision=0).start_revision, 0)

    def test_url_credentials_rejected_before_backend_request(self):
        for url in ('https://user:secret@repo.test/p', 'https://user@repo.test/p', 'svn+ssh://host/repo', 'https://repo.test/a?secret=x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                make_request('switch', url, 12)
        self.assertIn('%40', make_request('switch', ROOT + '/name@12', 12).url)

    def test_copy_plan_requires_explicit_kind_and_nonempty_message(self):
        for kind, message in (('', 'message'), ('tag', ''), ('branch', 'x' * 8193), ('tag', 'bad\x00')):
            with self.assertRaises(ValueError):
                make_request('copy_plan', SOURCE, 12, destination_url=ROOT + '/tags/v1', kind=kind, message=message)

    def test_switch_is_metadata_only_and_merge_has_real_dry_run_text(self):
        switch = plan_text(request(), plan(), CONTEXT)
        self.assertIn('没有 SVN dry-run', switch)
        self.assertIn('整个工作副本根', switch)
        merge = plan_text(request('merge'), plan('merge'), CONTEXT)
        self.assertIn('完整 SVN dry-run 输出', merge)
        self.assertIn('(8, 12]', merge)
        self.assertIn('U    file.txt', merge)
        self.assertIn('不代表最终无冲突', merge)

    def test_copy_plan_is_explicitly_non_executable_and_not_immutable_tag(self):
        text = plan_text(request('copy_plan'), plan('copy_plan'), CONTEXT)
        self.assertIn('不会创建，不存在执行按钮', text)
        self.assertIn('立即提交服务器', text)
        self.assertIn('不能保证不可修改', text)

    def test_mismatched_plan_scope_mapping_revision_hash_or_dryrun_is_refused(self):
        good = plan()
        values = (replace(good, operation='merge'), replace(good, workspace='/different'),
            replace(good, source_url=ROOT + '/other'), replace(good, source_revision=13),
            replace(good, target_url=ROOT + '/other'), replace(good, working_revision=9),
            replace(good, repository_uuid='other'), replace(good, repository_root='https://other.test'),
            replace(good, scope='selected_files'), replace(good, state_sha256='bad'),
            replace(good, svn_dry_run=True), replace(good, preview_text='fake dry-run'),
            replace(good, commit_performed=True), replace(good, start_revision=8))
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_plan(request(), value, CONTEXT)

    def test_predicted_merge_conflicts_are_visible_but_plan_can_be_reviewed(self):
        text = plan_text(request('merge'), plan('merge', conflicts=True), CONTEXT)
        self.assertIn('不提供执行入口', text)
        self.assertIn('C    file.txt', text)

    def test_copy_plan_never_accepts_execution_supported_true(self):
        good = plan('copy_plan')
        for value in (replace(good, execution_supported=True), replace(good, destination_url=ROOT + '/other'),
                      replace(good, kind='branch'), replace(good, message='different'), replace(good, would_commit_immediately=False)):
            with self.assertRaises(ValueError):
                validate_plan(request('copy_plan'), value, CONTEXT)

    def test_confirm_requires_both_checkboxes_and_supported_apply(self):
        for can_apply, scope, safety in ((False, True, True), (True, False, True), (True, True, False)):
            dialog = Mock(can_apply=can_apply, confirmed=False)
            dialog.scope_var.get.return_value, dialog.safety_var.get.return_value = scope, safety
            with patch('svn_client.branches_dialogs.messagebox.showwarning'):
                BranchPlanDialog._confirm(dialog)
            self.assertFalse(dialog.confirmed)
            dialog.destroy.assert_not_called()
        dialog = Mock(can_apply=True, confirmed=False)
        dialog.scope_var.get.return_value = dialog.safety_var.get.return_value = True
        BranchPlanDialog._confirm(dialog)
        self.assertTrue(dialog.confirmed)

    def test_registered_callbacks_exist_and_text_widgets_themed(self):
        for cls in (BranchUiMixin, BranchRequestDialog, BranchPlanDialog):
            source = inspect.getsource(cls)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self':
                    self.assertTrue(callable(getattr(SvnDesk if cls is BranchUiMixin else cls, node.func.attr, None)), node.func.attr)
            if cls is not BranchUiMixin:
                self.assertNotIn('<Return>', source)
                self.assertIn('cancel.focus_set()', source)


class BranchWorkflowTests(unittest.TestCase):
    def start(self, operation='switch'):
        app = Harness()
        manager = Mock()
        with patch('svn_client.branches_ui.BranchRequestDialog') as dialog, patch('svn_client.branches_ui.BranchManager', return_value=manager):
            dialog.return_value.result = request(operation)
            app._begin_branch_operation(operation)
        return app, manager

    @patch('svn_client.branches_ui.BranchRequestDialog')
    def test_cancel_does_not_preflight(self, dialog):
        app = Harness()
        dialog.return_value.result = None
        app.switch_working_copy()
        self.assertFalse(app.pending)

    @patch('svn_client.branches_ui.BranchRequestDialog')
    def test_demo_and_busy_do_not_open_dialog_or_query(self, dialog):
        for key in ('demo', '_busy'):
            app = Harness()
            setattr(app, key, True)
            app.switch_working_copy()
            app.preview_branch_copy()
            self.assertFalse(app.pending)
        dialog.assert_not_called()

    @patch('svn_client.branches_ui.messagebox.showwarning')
    @patch('svn_client.branches_ui.BranchRequestDialog')
    def test_missing_context_or_root_mismatch_refuses_before_form(self, dialog, warning):
        app = Harness()
        app.info = {}
        app.switch_working_copy()
        app = Harness()
        app.root_path = Path('/other')
        app.switch_working_copy()
        dialog.assert_not_called()

    def test_context_change_while_entering_form_cancels(self):
        app = Harness()
        def changed(*args):
            app.info['url'] = ROOT + '/other'
            return SimpleNamespace(result=request())
        with patch('svn_client.branches_ui.BranchRequestDialog', side_effect=changed):
            app.switch_working_copy()
        self.assertFalse(app.pending)

    def test_preflight_uses_exact_normalized_request_and_no_mutation(self):
        for operation in ('switch', 'merge', 'copy_plan'):
            app, manager = self.start(operation)
            app.pending[-1][1]()
            if operation == 'switch':
                manager.preflight_switch.assert_called_once_with(SOURCE, revision=12)
            elif operation == 'merge':
                manager.preflight_merge.assert_called_once_with(SOURCE, start_revision=8, end_revision=12)
            else:
                manager.preflight_copy.assert_called_once_with(SOURCE, ROOT + '/tags/v1', revision=12, kind='tag', message='Snapshot')
            manager.switch.assert_not_called()
            manager.merge.assert_not_called()

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_stale_workspace_mapping_selection_or_generation_discards_preflight(self, dialog):
        for change in ('client', 'mapping', 'preview', 'generation'):
            app, manager = self.start()
            if change == 'client': app.client = object()
            if change == 'mapping': app.info['url'] = ROOT + '/changed'
            if change == 'preview': app._preview_request += 1
            if change == 'generation': app._branch_request += 1
            app.finish(plan())
            self.assertEqual(len(app.pending), 1)
            manager.switch.assert_not_called()
        dialog.assert_not_called()

    @patch('svn_client.branches_ui.messagebox.showwarning')
    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_mismatched_plan_is_refused_and_refreshes_caches(self, dialog, warning):
        app, manager = self.start()
        app.finish(replace(plan(), target_url=ROOT + '/changed'))
        dialog.assert_not_called()
        app.refresh.assert_called_once_with(reset_navigation=True)
        self.assertEqual(len(app.pending), 1)

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_final_cancel_or_new_selection_never_schedules_write(self, dialog):
        app, manager = self.start()
        dialog.return_value.confirmed = False
        app.finish(plan())
        self.assertEqual(len(app.pending), 1)
        app, manager = self.start()
        def changed(*args):
            app._preview_request += 1
            return SimpleNamespace(confirmed=True)
        dialog.side_effect = changed
        app.finish(plan())
        self.assertEqual(len(app.pending), 1)

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_readonly_copy_plan_and_predicted_conflict_never_apply_even_bad_dialog(self, dialog):
        dialog.return_value.confirmed = True
        for operation, value in (('copy_plan', plan('copy_plan')), ('merge', plan('merge', conflicts=True))):
            app, manager = self.start(operation)
            app.finish(value)
            self.assertEqual(len(app.pending), 1)
            manager.switch.assert_not_called()
            manager.merge.assert_not_called()

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_execute_passes_original_request_and_exact_reviewed_hash(self, dialog):
        dialog.return_value.confirmed = True
        for operation in ('switch', 'merge'):
            app, manager = self.start(operation)
            app.finish(plan(operation))
            self.assertEqual(len(app.pending), 2)
            getattr(manager, operation).return_value = result(operation)
            app.pending[-1][1]()
            if operation == 'switch':
                manager.switch.assert_called_once_with(SOURCE, revision=12, confirmed=True, expected_state_sha256='a' * 64)
            else:
                manager.merge.assert_called_once_with(SOURCE, start_revision=8, end_revision=12, confirmed=True, expected_state_sha256='a' * 64)

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_unexpected_post_result_becomes_uncertain_not_precommand_refusal(self, dialog):
        app, manager = self.start()
        dialog.return_value.confirmed = True
        app.finish(plan())
        manager.switch.return_value = replace(result(), url=ROOT + '/wrong')
        with self.assertRaises(BranchOperationError):
            app.pending[-1][1]()

    @patch('svn_client.branches_ui.messagebox.showwarning')
    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_uncertain_error_refreshes_whole_namespace_without_retry(self, dialog, warning):
        app, manager = self.start()
        dialog.return_value.confirmed = True
        app.finish(plan())
        app.fail(BranchOperationError('timeout'))
        app.refresh.assert_called_once_with(reset_navigation=True)
        self.assertEqual(len(app.pending), 2)
        self.assertIn('部分或全部内容可能已改变', warning.call_args.args[1])
        self.assertIn('/selected-wc', warning.call_args.args[1])

    @patch('svn_client.branches_ui.messagebox.showwarning')
    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_refusal_does_not_claim_mutation_or_refresh_different_workspace(self, dialog, warning):
        app, manager = self.start()
        dialog.return_value.confirmed = True
        app.finish(plan())
        app.client = object()
        previews = len(app.previews)
        app.fail(BranchError('state changed'))
        app.refresh.assert_not_called()
        self.assertEqual(len(app.previews), previews)
        self.assertIn('实际应用命令未开始', warning.call_args.args[1])

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_result_render_failure_still_invalidates_and_refreshes_same_workspace(self, dialog):
        app, manager = self.start()
        dialog.return_value.confirmed = True
        app.finish(plan())
        with patch('svn_client.branches_ui.result_text', side_effect=ValueError('render error')):
            with self.assertRaises(ValueError):
                app.finish(result())
        app.refresh.assert_called_once_with(reset_navigation=True)
        self.assertEqual(len(app.pending), 2)

    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_uncertain_notification_failure_still_refreshes_without_retry(self, dialog):
        app, manager = self.start()
        dialog.return_value.confirmed = True
        app.finish(plan())
        with patch('svn_client.branches_ui.messagebox.showwarning', side_effect=RuntimeError('window error')):
            with self.assertRaises(RuntimeError):
                app.fail(BranchOperationError('partial'))
        app.refresh.assert_called_once_with(reset_navigation=True)
        self.assertEqual(len(app.pending), 2)

    @patch('svn_client.branches_ui.messagebox.showwarning')
    @patch('svn_client.branches_ui.BranchPlanDialog')
    def test_merge_conflicts_remain_needs_human_review_and_no_commit(self, dialog, warning):
        app, manager = self.start('merge')
        dialog.return_value.confirmed = True
        app.finish(plan('merge'))
        app.finish(result('merge', conflicted=True))
        self.assertIn('冲突', app.previews[-1][0])
        self.assertIn('没有自动解决、还原或提交', app.previews[-1][1])
        app.refresh.assert_called_once_with(reset_navigation=True)
        manager.commit.assert_not_called()
        manager.resolve.assert_not_called()
        manager.revert.assert_not_called()


class ResultAndCacheTests(unittest.TestCase):
    def test_merge_never_claims_complete_or_verified_submission(self):
        text = result_text(result('merge'))
        self.assertIn('必须人工审查', text)
        self.assertIn('没有证明所有修订都已应用', text)
        self.assertIn('svn:mergeinfo', text)
        self.assertIn('预览已截断', result_text(replace(result('merge'), output='x' * 100000)))

    def test_postconditions_validate_mapping_revision_scope_and_outcome(self):
        good = result()
        for bad in (replace(good, outcome='completed'), replace(good, workspace='/other'),
                    replace(good, working_revision=11), replace(good, commit_performed=True),
                    replace(good, automatic_retry_safe=True), replace(good, conflicts=('file.txt',))):
            with self.assertRaises(ValueError):
                validate_result(bad, request(), plan(), CONTEXT)

    def test_attempt_invalidates_old_namespace_and_history_before_refresh(self):
        app = Mock(_tree_generation=8)
        for name in ('_workspace_nodes', '_workspace_cache', '_workspace_more', '_repository_nodes', '_repo_entries'):
            setattr(app, name, {'old': 'stale'})
        app._workspace_pending = {'old'}
        for tree in (app.workspace_tree, app.repository_tree, app.repository):
            tree.get_children.return_value = ('old',)
        BranchUiMixin._invalidate_branch_views(app)
        self.assertEqual(app._tree_generation, 9)
        self.assertEqual(app.info, {})
        self.assertTrue(app._workspace_state_unknown)
        self.assertEqual(app.entries, [])
        self.assertEqual(app.changelist_candidates, [])
        self.assertEqual(app._workspace_cache, {})
        self.assertFalse(app._workspace_pending)
        self.assertIsNone(app._folder_scope)
        self.assertEqual(app._repo_url, '')
        app._reset_history_state.assert_called_once_with(reset_form=True)
        app._clear_workspace_selection.assert_called_once_with()




    def test_failed_callback_exception_does_not_stop_polling_or_repeat_mutation(self):
        import queue
        app = Mock(_closing=False, root_path=Path('/selected-wc'))
        app._queue = queue.Queue()
        failed = Mock(side_effect=RuntimeError('messagebox failed after queuing refresh'))
        app._queue.put((False, BranchOperationError('uncertain'), None, 'merge', False, failed, True))
        SvnDesk._drain_queue(app)
        failed.assert_called_once()
        app._log_callback_failure.assert_called_once()
        app.after.assert_called_once_with(90, app._drain_queue)
        app._drain_tree_queue.assert_called_once()
        app.refresh.assert_not_called()

    def test_done_and_notification_failure_still_rearms_without_further_modal(self):
        import queue
        app = Mock(_closing=False)
        app._queue = queue.Queue()
        done = Mock(side_effect=ValueError('render failed after queuing refresh'))
        app._queue.put((True, result(), done, 'switch', False, None, True))
        with patch('svn_client.ui.messagebox.showerror', side_effect=RuntimeError('notification failed')) as modal:
            SvnDesk._drain_queue(app)
        app.after.assert_called_once_with(90, app._drain_queue)
        self.assertEqual(app._log_callback_failure.call_count, 2)
        modal.assert_called_once()

    def test_callback_failure_logger_tolerates_unavailable_widget(self):
        app = Mock()
        app._append_log.side_effect = RuntimeError('text widget unavailable')
        SvnDesk._log_callback_failure(app, 'callback', ValueError('display'))
        app._append_log.assert_called_once()

    def test_widget_failure_still_clears_logical_old_targets_and_marks_unknown(self):
        app = Mock(_tree_generation=8)
        for name in ('_workspace_nodes', '_workspace_cache', '_workspace_more', '_repository_nodes', '_repo_entries',
                     '_change_lookup', '_group_lookup', '_file_lookup', '_file_kind'):
            setattr(app, name, {'old': 'stale'})
        app._workspace_pending = {'old'}
        app._clear_workspace_selection.side_effect = RuntimeError('widget unavailable')
        with self.assertRaises(RuntimeError):
            BranchUiMixin._invalidate_branch_views(app)
        self.assertTrue(app._workspace_state_unknown)
        self.assertEqual(app.info, {})
        self.assertEqual(app._change_lookup, {})
        self.assertEqual(app._workspace_cache, {})
        self.assertIn('状态未知', app.summary_var.set.call_args.args[0])

    def test_unknown_state_never_renders_as_zero_clean_after_read_failure(self):
        app = Mock(_workspace_state_unknown=True, entries=[], info={}, _folder_rows=None)
        SvnDesk._render_all(app)
        summary = app.summary_var.set.call_args.args[0]
        self.assertIn('状态未知', summary)
        self.assertIn('不能据此判断干净', summary)
        self.assertNotIn('0 个冲突', summary)
        # A failed read never calls the loaded callback that clears the flag.
        app.demo = app._busy = False
        app.client = object()
        SvnDesk.refresh(app, reset_navigation=True)
        self.assertTrue(app._workspace_state_unknown)

    def test_refresh_rebuilds_navigation_only_after_new_metadata_and_never_other_client(self):
        app = Mock(demo=False, _busy=False)
        client = app.client
        SvnDesk.refresh(app, reset_navigation=True)
        app._invalidate_branch_views.assert_called_once_with()
        loaded = app._run.call_args.args[2]
        loaded(({'url': SOURCE}, ([], []), []))
        self.assertEqual(app.info, {'url': SOURCE})
        self.assertFalse(app._workspace_state_unknown)
        app._reset_workspace_tree.assert_called_once_with()
        app._refresh_workspace_directory.assert_not_called()
        app.load_history.assert_not_called()
        app.load_repository.assert_not_called()
        app._reset_workspace_tree.reset_mock()
        app.client = object()
        loaded(({'url': ROOT + '/wrong'}, ([], []), []))
        self.assertEqual(app.info, {'url': SOURCE})
        app._reset_workspace_tree.assert_not_called()


class BranchDialogConstructionTests(unittest.TestCase):
    def setUp(self):
        self.parent = Mock()
        self.parent.display.px.side_effect = lambda value: value
        self.buttons, self.variables, self.checks, self.patches = [], [], [], []
        def start(target, **kwargs):
            holder = patch(target, **kwargs)
            self.patches.append(holder)
            return holder.start()
        start('svn_client.branches_dialogs.tk.Toplevel.__init__', return_value=None)
        start('svn_client.branches_dialogs._size')
        start('svn_client.branches_dialogs._text')
        for name in ('title', 'transient', 'grab_set', 'wait_window', 'destroy'):
            start('svn_client.branches_dialogs.tk.Toplevel.' + name)
        self.bind = start('svn_client.branches_dialogs.tk.Toplevel.bind')
        for name in ('Frame', 'Label', 'Entry', 'Combobox'):
            start('svn_client.branches_dialogs.ttk.' + name)
        def button(*args, **kwargs):
            obj = Mock()
            self.buttons.append((kwargs, obj))
            return obj
        def variable(*args, **kwargs):
            self.variables.append(kwargs)
            return Mock()
        def check(*args, **kwargs):
            self.checks.append(kwargs)
            return Mock()
        start('svn_client.branches_dialogs.ttk.Button', side_effect=button)
        start('svn_client.branches_dialogs.ttk.Checkbutton', side_effect=check)
        start('svn_client.branches_dialogs.tk.StringVar', side_effect=variable)
        start('svn_client.branches_dialogs.tk.BooleanVar', side_effect=variable)

    def tearDown(self):
        for holder in reversed(self.patches):
            holder.stop()

    def test_request_fields_are_blank_and_only_readonly_preflight_offered(self):
        value = BranchRequestDialog(self.parent, 'switch', CONTEXT)
        self.assertIsNone(value.result)
        self.assertTrue(all(item == {'value': ''} for item in self.variables))
        self.assertEqual([item['text'] for item, _ in self.buttons], ['取消', '只读预检…'])
        self.buttons[0][1].focus_set.assert_called_once_with()

    def test_copy_plan_has_only_close_and_no_execution_widget(self):
        value = BranchPlanDialog(self.parent, request('copy_plan'), plan('copy_plan'), CONTEXT)
        self.assertFalse(value.confirmed)
        self.assertFalse(value.can_apply)
        self.assertEqual(len(self.buttons), 1)
        self.assertEqual(self.buttons[0][0]['text'], '关闭 / 取消')
        self.assertFalse(self.checks)
        self.assertFalse(hasattr(value, 'apply_button'))

    def test_switch_requires_two_unchecked_consents_and_cancel_focus(self):
        value = BranchPlanDialog(self.parent, request(), plan(), CONTEXT)
        self.assertFalse(value.confirmed)
        self.assertTrue(value.can_apply)
        self.assertEqual(self.variables, [{'value': False}, {'value': False}])
        self.assertEqual(len(self.checks), 2)
        self.assertEqual(self.buttons[-1][0]['state'], 'disabled')
        self.buttons[0][1].focus_set.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.bind.call_args_list], ['<Escape>'])

    def test_predicted_conflicts_disable_consent_and_application(self):
        value = BranchPlanDialog(self.parent, request('merge'), plan('merge', conflicts=True), CONTEXT)
        self.assertFalse(value.can_apply)
        self.assertTrue(all(item['state'] == 'disabled' for item in self.checks))
        self.assertEqual(self.buttons[-1][0]['state'], 'disabled')


if __name__ == '__main__':
    unittest.main()
