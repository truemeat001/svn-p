"""Read-only GUI workflows tested without a native display or SVN process."""
import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import LogEntry, StatusEntry
from svn_client.history import BoundedText, ComparisonResult, HistoryResult
from svn_client.history_dialogs import ComparisonDialog, normalize_scope_path
from svn_client.history_ui import (HistoryUiMixin, bounded_text, comparison_preview, history_cell,
                                  history_detail, HISTORY_BACK_LIMIT)
from svn_client.ui import SvnDesk


class Variable:
    def __init__(self, value, changed=None):
        self.value, self.changed = value, changed
    def get(self):
        return self.value
    def set(self, value):
        self.value = value
        if self.changed:
            self.changed()


class Harness(HistoryUiMixin):
    def __init__(self):
        self._init_history_state()
        self.client = SimpleNamespace(working_copy=Path('/wc'))
        self.root_path = Path('/wc')
        self._busy = self._closing = self.demo = False
        self.tab = 2
        self._preview_request = 0
        self.logs = []
        self.pending, self.previews, self.idle = [], [], []
        self.tabs = Mock()
        self.tabs.index.side_effect = lambda _: self.tab
        self.history_path_var = Variable('src/a.txt', self._history_query_changed)
        self.history_start_var = Variable('HEAD', self._history_query_changed)
        self.history_limit_var = Variable('100', self._history_query_changed)
        self.history_note_var = Variable('')
        self.status_var = Variable('')
        self.history_prev_button, self.history_next_button = Mock(), Mock()
        self.history = Mock()
        self.history.selection.return_value = ()
        self.changes, self.files = Mock(), Mock()
        self.changes.selection.return_value = ('file',)
        self.files.selection.return_value = ('file',)
        self._change_lookup = {'file': StatusEntry('src/a.txt', 'modified')}
        self._file_lookup = {'file': 'src/a.txt', 'dir': 'src'}
        self._file_kind = {'file': 'file', 'dir': 'dir'}
        self.changelist_candidates = list(self._change_lookup.values())
        self._log_lookup = {}
    def _can_operate(self):
        return not (self._busy or self.demo or self._closing) and self.client is not None
    def _run(self, label, work, done=None, *, on_error=None, preserve_view_on_error=False):
        self.last_preserve_error_view = preserve_view_on_error
        self.pending.append((label, work, done, on_error))
        self._busy = True
    def finish(self, result):
        self._busy = False
        self.pending[-1][2](result)
    def fail(self, error):
        self._busy = False
        self.pending[-1][3](error)
    def _show_preview(self, title, text, **options):
        self._preview_request += 1
        self.previews.append((title, text, options))
    def _render_history(self):
        self._log_lookup = {f'log{i}': entry for i, entry in enumerate(self.logs)}
    def select_tab(self, tab):
        self.tab = tab
    def after_idle(self, callback):
        self.idle.append(callback)


def entry(revision=10, message='message'):
    return LogEntry(revision, 'author', '2026-11-03', message,
                    [{'action': 'M', 'path': '/other-than-selected.txt'}])


def page(path='src/a.txt', start='HEAD', revisions=(10, 6), more=False, next_start=None):
    return HistoryResult(path, start, tuple(entry(rev) for rev in revisions), more, next_start)


def comparison(path='src/a.txt', left='BASE', right='WORKING', text='+new\n-old\n'):
    size = len(text.encode('utf-8'))
    return ComparisonResult(path, left, right, BoundedText(text, False, size, size))


class PresentationTests(unittest.TestCase):
    def test_blank_or_unsafe_path_never_becomes_root(self):
        for value in ('', ' ', '../a', '/abs', 'C:foo', 'https://server/repo', '.SVN/entries', 'a//b', 'a\nb'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_scope_path(value)
        self.assertEqual(normalize_scope_path('.'), '.')
        self.assertEqual(normalize_scope_path('folder/./a @.txt'), 'folder/a @.txt')
        self.assertEqual(normalize_scope_path('folder/ 名字.txt'), 'folder/ 名字.txt')

    def test_posix_backslash_name_never_rewrites_to_another_path(self):
        with patch('svn_client.history_dialogs.os.name', 'posix'):
            with self.assertRaises(ValueError):
                normalize_scope_path(r'a\b.txt')
        with patch('svn_client.history_dialogs.os.name', 'nt'):
            self.assertEqual(normalize_scope_path(r'a\b.txt'), 'a/b.txt')

    def test_utf8_boundaries_and_metadata_are_explicit(self):
        text, cut, total, shown = bounded_text('中文A', 4)
        self.assertEqual(text, '中')
        self.assertTrue(cut)
        self.assertEqual((total, shown), (7, 3))
        value = comparison(text='中' * 50)
        result = comparison_preview(value, 10)
        self.assertIn('已截断', result)
        self.assertIn('BASE → WORKING', result)
        self.assertIn('9 / 150', result)
        self.assertIn('不能作为完整补丁', result)

    def test_backend_truncation_and_empty_diff_are_not_silently_success_equality(self):
        value = comparison()
        value = replace(value, diff=replace(value.diff, truncated=True, source_utf8_bytes=1000))
        self.assertIn('已截断', comparison_preview(value, 64 * 1024))
        text = comparison_preview(comparison(text=''), 64 * 1024)
        self.assertIn('不能据此断言二进制内容相同', text)
        self.assertIn('不包含二进制载荷', text)

    def test_history_detail_does_not_claim_changed_paths_are_only_selected_file(self):
        text = history_detail(entry(), 'src/a.txt')
        self.assertIn('可能包含查询范围以外', text)
        self.assertIn('/other-than-selected.txt', text)
        self.assertIn('不会自动扩大', text)
        self.assertIn('详情已截断', history_detail(entry(message='x' * 100000), 'src/a.txt'))
        self.assertLess(len(history_detail(entry(message='x' * 100000), 'src/a.txt')), 66000)

    def test_table_cell_does_not_render_unbounded_author_or_message(self):
        self.assertEqual(history_cell('a' * 1000, 120), 'a' * 120 + '…')
        self.assertEqual(history_cell('first\nsecond'), 'first')

    @patch('svn_client.history_dialogs.messagebox.showwarning')
    def test_comparison_parameters_reject_working_left_ranges_and_unknown_limits(self, warning):
        for left, right, limit in [('WORKING', 'HEAD', '256'), ('5:9', 'HEAD', '256'), ('BASE', 'PREV', '256'), ('BASE', 'WORKING', '8192')]:
            dialog = Mock(result=None)
            dialog.left_var.get.return_value = left
            dialog.right_var.get.return_value = right
            dialog.limit_var.get.return_value = limit
            ComparisonDialog._accept(dialog)
            self.assertIsNone(dialog.result)
            dialog.destroy.assert_not_called()

    def test_comparison_parameters_accept_explicit_reverse_numeric_and_utf8_limit(self):
        dialog = Mock(result=None)
        dialog.left_var.get.return_value = '12'
        dialog.right_var.get.return_value = '3'
        dialog.limit_var.get.return_value = '64'
        ComparisonDialog._accept(dialog)
        self.assertEqual(dialog.result, ('12', '3', 65536))

    def test_registered_callbacks_and_classic_text_theme_are_wired(self):
        for cls in (HistoryUiMixin, ComparisonDialog):
            source = inspect.getsource(cls)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self':
                    target = SvnDesk if cls is HistoryUiMixin else ComparisonDialog
                    self.assertTrue(callable(getattr(target, node.func.attr, None)), node.func.attr)
            if cls is ComparisonDialog:
                self.assertIn('**text_options()', source)
                self.assertNotIn('<Return>', source)
                self.assertIn('cancel.focus_set()', source)


class ScopeTests(unittest.TestCase):
    def test_file_actions_require_one_file_not_group_directory_or_empty(self):
        app = Harness()
        app.tab = 0
        self.assertEqual(app._selected_history_file(), 'src/a.txt')
        for selection in ((), ('group',), ('file', 'other')):
            app.changes.selection.return_value = selection
            self.assertIsNone(app._selected_history_file())
        app.tab = 1
        self.assertEqual(app._selected_history_file(), 'src/a.txt')
        app.files.selection.return_value = ('dir',)
        self.assertIsNone(app._selected_history_file())

    @patch('svn_client.history_ui.messagebox.showwarning')
    def test_selected_posix_backslash_path_is_rejected_without_query(self, warning):
        app = Harness()
        app.tab = 0
        app._change_lookup = {'file': StatusEntry(r'a\b.txt', 'normal')}
        app.changelist_candidates = list(app._change_lookup.values())
        with patch('svn_client.history_dialogs.os.name', 'posix'):
            app.show_selected_history()
        self.assertFalse(app.pending)
        self.assertFalse(app.idle)

    def test_intentional_tab_switch_is_processed_before_query(self):
        app = Harness()
        app.tab = 1
        app.open_path_history('src/a.txt')
        self.assertEqual(app.tab, 2)
        self.assertFalse(app.pending)
        app._preview_request += 1  # queued NotebookTabChanged
        app.idle[0]()
        self.assertEqual(len(app.pending), 1)
        app.finish(page())
        self.assertTrue(app._history_loaded)

    def test_scheduled_open_drops_on_new_controls_workspace_or_navigation(self):
        for change in ('control', 'workspace', 'tab'):
            app = Harness()
            app.open_path_history('src/a.txt')
            if change == 'control':
                app.history_path_var.set('other.txt')
            elif change == 'workspace':
                app.client = object()
            else:
                app.tab = 1
            app.idle[0]()
            self.assertFalse(app.pending)


    def test_stale_read_error_does_not_switch_details_or_replace_address(self):
        import queue
        for preserve in (True, False):
            app = Mock(_closing=False, root_path=Path('/current'))
            app._queue = queue.Queue()
            failed = Mock()
            app._queue.put((False, ValueError('old read'), None, 'read', False, failed, preserve))
            SvnDesk._drain_queue(app)
            failed.assert_called_once()
            if preserve:
                app.details_tabs.select.assert_not_called()
                app.path_var.set.assert_not_called()
            else:
                app.details_tabs.select.assert_called_once_with(1)
                app.path_var.set.assert_called_once_with('/current')

    def test_demo_never_queries_or_shows_real_comparison(self):
        app = Harness()
        app.demo = True
        with patch('svn_client.history_ui.ComparisonDialog') as dialog:
            app.load_history()
            app.open_path_history('.')
            app.show_comparison('src/a.txt')
        self.assertFalse(app.pending)
        dialog.assert_not_called()


class HistoryReadTests(unittest.TestCase):
    @patch('svn_client.history_ui.HistoryManager')
    def test_queries_exact_path_start_limit_and_replaces_one_page(self, manager):
        app = Harness()
        app.logs = [entry(999)]
        app.load_history()
        app.pending[-1][1]()
        manager.return_value.history.assert_called_once_with('src/a.txt', limit=100, start_revision='HEAD')
        self.assertEqual(app.logs, [])
        app.finish(page(more=True, next_start=5))
        self.assertEqual([value.revision for value in app.logs], [10, 6])
        self.assertEqual(app._history_next, 5)
        self.assertTrue(app._history_loaded)

    def test_change_restore_controls_still_invalidates_old_request(self):
        for name, other in [('history_path_var', 'other'), ('history_start_var', '8'), ('history_limit_var', '25')]:
            app = Harness()
            app.load_history()
            variable = getattr(app, name)
            original = variable.get()
            variable.set(other)
            variable.set(original)
            previews = len(app.previews)
            app.finish(page())
            self.assertEqual(app.logs, [])
            self.assertFalse(app._history_loaded)
            self.assertEqual(len(app.previews), previews)

    def test_new_workspace_tab_or_detail_drops_late_result_and_error(self):
        for change in ('workspace', 'tab', 'details'):
            app = Harness()
            app.load_history()
            if change == 'workspace':
                app.client = object()
            elif change == 'tab':
                app.tab = 1
            else:
                app._show_preview('new', 'selection')
            previews = len(app.previews)
            app.finish(page())
            app.fail(ValueError('old failure'))
            self.assertEqual(app.logs, [])
            self.assertEqual(len(app.previews), previews)
            if change != 'workspace':
                self.assertFalse(app._history_loading)
                self.assertIn('丢弃', app.history_note_var.get())

    def test_own_empty_selection_event_does_not_invalidate_load(self):
        app = Harness()
        app.load_history()
        preview = app._preview_request
        app.preview_history()
        self.assertEqual(app._preview_request, preview)
        app.finish(page())
        self.assertTrue(app._history_loaded)

    def test_empty_page_marks_loaded_without_automatic_retry(self):
        app = Harness()
        app.load_history()
        app.finish(page(revisions=()))
        self.assertTrue(app._history_loaded)
        self.assertFalse(app._history_auto_pending)
        self.assertIsNone(app._history_next)
        self.assertEqual(len(app.pending), 1)

    def test_failure_is_not_rendered_as_empty_or_retried(self):
        app = Harness()
        app.load_history()
        app.fail(ValueError('read rejected'))
        self.assertIn('失败', app.previews[-1][0])
        self.assertIn('不会自动改查根目录', app.previews[-1][1])
        self.assertFalse(app._history_loaded)
        self.assertEqual(len(app.pending), 1)

    def test_mismatched_scope_or_page_is_not_adopted(self):
        for result in (page(path='.'), page(start='2'), page(revisions=tuple(range(150, 0, -1))), page(more=True)):
            app = Harness()
            app.load_history()
            app.finish(result)
            self.assertFalse(app._history_loaded)
            self.assertFalse(app.logs)

    def test_next_and_previous_use_cursor_not_global_revision_guess(self):
        app = Harness()
        app.load_history()
        app.finish(page(more=True, next_start=5))
        app.history_next_page()
        self.assertEqual(app.history_start_var.get(), '5')
        app.finish(page(start='5', revisions=(4, 2)))
        self.assertEqual(app._history_back, ['HEAD'])
        self.assertEqual([value.revision for value in app.logs], [4, 2])
        app.history_previous_page()
        self.assertEqual(app.history_start_var.get(), 'HEAD')
        app.finish(page(more=True, next_start=5))
        self.assertEqual(app._history_back, [])

    def test_cursor_stack_bounded_and_reset_on_workspace(self):
        app = Harness()
        app._history_loaded = True
        app._history_start = '300'
        app._history_next = 299
        app._history_back = [str(value) for value in range(600, 300, -1)]
        app.history_next_page()
        app.finish(page(start='299', revisions=(290,), more=True, next_start=289))
        self.assertEqual(len(app._history_back), HISTORY_BACK_LIMIT)
        app._reset_history_state(reset_form=True)
        self.assertEqual(app._history_back, [])
        self.assertEqual(app.history_path_var.get(), '.')
        self.assertEqual(app.history_start_var.get(), 'HEAD')
        self.assertFalse(app.logs)


class ComparisonReadTests(unittest.TestCase):
    @patch('svn_client.history_ui.ComparisonDialog')
    def test_cancel_and_context_changes_do_not_query(self, dialog):
        app = Harness()
        dialog.return_value.result = None
        app.show_comparison('src/a.txt')
        self.assertFalse(app.pending)
        def changed(*args, **kwargs):
            app._preview_request += 1
            return SimpleNamespace(result=('BASE', 'WORKING', 262144))
        dialog.side_effect = changed
        app.show_comparison('src/a.txt')
        self.assertFalse(app.pending)

    @patch('svn_client.history_ui.ComparisonDialog')
    def test_revision_double_click_uses_only_history_path_not_root_or_commit_paths(self, dialog):
        app = Harness()
        app._history_path = 'src/a.txt'
        app._log_lookup = {'row': entry(10)}
        app.history.selection.return_value = ('row',)
        dialog.return_value.result = None
        app.preview_revision_diff()
        dialog.assert_called_once_with(app, 'src/a.txt', from_revision='9', to_revision='10')

    @patch('svn_client.history_ui.HistoryManager')
    def test_explicit_direction_limit_and_only_compare_api_used(self, manager):
        app = Harness()
        app._read_comparison('src/a.txt', 'HEAD', 'WORKING', 65536)
        app.pending[-1][1]()
        manager.return_value.compare.assert_called_once_with('src/a.txt', from_revision='HEAD', to_revision='WORKING', max_text_bytes=65536)
        app.finish(comparison(left='HEAD'))
        self.assertIn('HEAD → WORKING', app.previews[-1][0])
        self.assertTrue(app.previews[-1][2]['diff'])
        manager.return_value.history.assert_not_called()

    def test_stale_result_and_error_cannot_replace_new_objects(self):
        for change in ('workspace', 'tab', 'preview', 'request'):
            app = Harness()
            app._read_comparison('src/a.txt', 'BASE', 'WORKING', 65536)
            if change == 'workspace': app.client = object()
            if change == 'tab': app.tab = 1
            if change == 'preview': app._show_preview('new', 'object')
            if change == 'request': app._comparison_request += 1
            count = len(app.previews)
            app.finish(comparison())
            app.fail(ValueError('old error'))
            self.assertEqual(len(app.previews), count)

    def test_wrong_scope_direction_or_binary_payload_not_displayed(self):
        for result in (comparison(path='.'), comparison(left='HEAD'), replace(comparison(), binary_payloads_included=True)):
            app = Harness()
            app._read_comparison('src/a.txt', 'BASE', 'WORKING', 65536)
            app.finish(result)
            self.assertIn('失败', app.previews[-1][0])
            self.assertNotIn('+new', app.previews[-1][1])

    @patch('svn_client.history_ui.messagebox.showwarning')
    def test_invalid_left_or_unbounded_display_refuses_before_work(self, warning):
        app = Harness()
        app._read_comparison('src/a.txt', 'WORKING', 'BASE', 65536)
        app._read_comparison('src/a.txt', 'BASE', 'WORKING', 8 * 1024 * 1024)
        self.assertFalse(app.pending)


if __name__ == '__main__':
    unittest.main()
