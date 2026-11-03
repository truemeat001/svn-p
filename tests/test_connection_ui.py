"""Read-only diagnostics GUI contracts; no real Tk/server access required."""
import ast
from contextlib import ExitStack
from dataclasses import replace
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.connection import ConnectionReport, ProbeStep, RepositoryIdentity, WorkingCopyIdentity
from svn_client.connection_dialogs import (ConnectionDialog, INVALID_URL, SAFE_FAILURE,
    remote_confirmation, report_text)
from svn_client.connection_ui import ConnectionUiMixin, DiagnosticContext
from svn_client.ui import SvnDesk


URL = 'https://repo.test/project/trunk'
UUID = '12345678-1234-1234-1234-123456789abc'
REPOSITORY = RepositoryIdentity(URL, 'https://repo.test/project', UUID, 7, 'dir')
CONTEXT = DiagnosticContext(str(Path('/working-copy')), '/trusted/svn', None)


def report(remote=False, *, mismatch=False):
    local = WorkingCopyIdentity('/working-copy', '/working-copy', REPOSITORY)
    steps = [ProbeStep('executable', 'ok', 'ok', 'PRIVATE arbitrary executable text'),
             ProbeStep('working_copy', 'ok', 'ok', 'PRIVATE arbitrary local text')]
    if remote:
        steps.extend([ProbeStep('remote', 'ok', 'ok', 'PRIVATE arbitrary remote text'),
                      ProbeStep('identity', 'failed' if mismatch else 'ok', 'identity_mismatch' if mismatch else 'ok', 'PRIVATE')])
    return ConnectionReport(not mismatch, tuple(steps), '/trusted/svn', '1.14.2', local,
        REPOSITORY if remote else None, remote, remote, not mismatch if remote else None, ('PRIVATE warning',))


class Harness(ConnectionUiMixin):
    def __init__(self, active=True):
        self.client = SimpleNamespace(working_copy=Path('/working-copy'), executable='/trusted/svn') if active else None
        self.settings = {}
        self._busy = self._closing = self.demo = self._manual_open_requested = False
        self.pending = []
    def _run(self, label, work, done=None, **kwargs):
        self.pending.append((label, work, done, kwargs))
        self._busy = True
    def finish(self, value):
        self._busy = False
        self.pending[-1][2](value)


def viewer():
    obj = Mock(closed=False, generation=4)
    obj.begin.return_value = 4
    return obj


class ReportPresentationTests(unittest.TestCase):
    def test_local_success_does_not_claim_server_connected(self):
        text = report_text(report(), remote_requested=False)
        self.assertIn('未请求远程访问', text)
        self.assertIn('本地成功不代表服务器已连接', text)
        self.assertNotIn('PRIVATE', text)

    def test_remote_report_has_head_uuid_and_limited_success_disclaimers(self):
        text = report_text(report(True), remote_requested=True)
        for fragment in ('HEAD', '不能据此断言服务器收到请求', UUID, '不证明 URL、分支、内容或修订一致', '不证明提交'):
            self.assertIn(fragment, text)
        self.assertNotIn('PRIVATE', text)

    def test_different_repository_is_not_treated_as_switch(self):
        text = report_text(report(True, mismatch=True), remote_requested=True)
        self.assertIn('仓库 UUID 不同', text)
        self.assertIn('没有修改本地映射', text)

    def test_remote_requested_but_never_attempted_is_explicit(self):
        data = ConnectionReport(False, (ProbeStep('executable', 'failed', 'dependency_missing', 'PRIVATE'),
            ProbeStep('remote', 'skipped', 'dependency_missing', 'PRIVATE')), remote_requested=True)
        text = report_text(data, remote_requested=True)
        self.assertIn('远程阶段：未开始', text)

    def test_tls_and_numeric_codes_render_without_raw_message(self):
        data = ConnectionReport(False, (ProbeStep('remote', 'failed', 'tls', 'PRIVATE raw certificate', ('230001',), 1),),
            remote_requested=True, remote_attempted=True)
        text = report_text(data, remote_requested=True)
        self.assertIn('不会忽略、接受或绕过证书错误', text)
        self.assertIn('230001', text)
        self.assertNotIn('PRIVATE', text)

    def test_unrecognized_category_has_safe_generic_description(self):
        data = ConnectionReport(False, (ProbeStep('executable', 'failed', 'PRIVATE', 'PRIVATE'),))
        text = report_text(data, remote_requested=False)
        self.assertIn('暂不能识别根因', text)
        self.assertNotIn('PRIVATE', text)

    def test_malformed_metadata_codes_scope_and_version_fail_closed(self):
        good = report()
        bad = (replace(good, remote_requested=True), replace(good, remote_attempted=True),
            replace(good, remote=REPOSITORY), replace(good, version='PRIVATE'),
            replace(good, executable='bad\nPRIVATE'),
            replace(good, steps=(ProbeStep('executable', 'failed', 'tls', '', ('PRIVATE',)),)),
            replace(good, steps=(ProbeStep('PRIVATE', 'failed', 'tls', ''),)),
            replace(good, steps=(ProbeStep('remote', 'ok', 'ok', ''),)),
            replace(good, steps=(ProbeStep('executable', 'failed', 'tls', '', (), 'PRIVATE'),)),
            replace(good, working_copy=replace(good.working_copy, repository=replace(REPOSITORY, url='https://user:PRIVATE@repo.test'))))
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                report_text(value, remote_requested=False)
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_local_ssh_identity_is_display_only_without_userinfo(self):
        local = replace(report().working_copy, repository=replace(REPOSITORY, url='svn+ssh://host/repo', repository_root='svn+ssh://host/repo'))
        self.assertIn('svn+ssh://host/repo', report_text(replace(report(), working_copy=local), remote_requested=False))
        unsafe = replace(local, repository=replace(local.repository, url='svn+ssh://user@host/repo'))
        with self.assertRaises(ValueError):
            report_text(replace(report(), working_copy=unsafe), remote_requested=False)

    def test_oversized_report_never_displays_partial_output(self):
        data = replace(report(), executable='中' * 32768)
        with self.assertRaises(ValueError):
            report_text(data, remote_requested=False)

    def test_confirmation_names_exact_literal_url_cache_log_and_no_write(self):
        text = remote_confirmation(URL + '/name@123')
        for fragment in (URL + '/name@123', 'HEAD', '@123 按字面名称', '既有凭据', '访问', '不会检出、更新、提交', '不绕过 TLS'):
            self.assertIn(fragment, text)
        with self.assertRaises(ValueError) as caught:
            remote_confirmation('https://user:PRIVATE@host/repo')
        self.assertNotIn('PRIVATE', str(caught.exception))


class DiagnosticWorkflowTests(unittest.TestCase):
    @patch('svn_client.connection_ui.ConnectionDialog')
    def test_dialog_open_does_not_execute_and_prevents_startup_restore(self, dialog):
        app = Harness()
        app.show_connection_diagnostics()
        self.assertTrue(app._manual_open_requested)
        self.assertFalse(app.pending)
        self.assertEqual(dialog.call_args.args[:2], (app, CONTEXT))

    @patch('svn_client.connection_ui.ConnectionDialog')
    @patch('svn_client.connection_ui.messagebox.showinfo')
    def test_demo_busy_closing_never_reads_real_settings_or_constructs_dialog(self, info, dialog):
        for flag in ('demo', '_busy', '_closing'):
            app = Harness()
            setattr(app, flag, True)
            app._diagnostic_context = Mock(side_effect=AssertionError('must not read'))
            app.show_connection_diagnostics()
            app._diagnostic_context.assert_not_called()
        dialog.assert_not_called()

    def test_no_active_working_copy_uses_settings_executable_only(self):
        app = Harness(active=False)
        app.settings['svn_executable'] = '/chosen/svn'
        self.assertEqual(app._diagnostic_context(), DiagnosticContext(None, '/chosen/svn', '/chosen/svn'))

    @patch('svn_client.connection_ui.ConnectionDiagnostics')
    def test_local_probe_passes_no_url_or_remote_flag_and_runs_only_in_worker(self, manager):
        app, view = Harness(), viewer()
        app._run_connection_probe(view, app.client, CONTEXT, None)
        manager.assert_not_called()
        manager.return_value.probe.return_value = report()
        text = app.pending[-1][1]()
        manager.assert_called_once_with(str(Path('/working-copy')), executable='/trusted/svn', timeout=15)
        manager.return_value.probe.assert_called_once_with()
        self.assertTrue(app.pending[-1][3]['preserve_view_on_error'])
        app.finish(text)
        view.finish.assert_called_once_with(text)

    @patch('svn_client.connection_ui.ConnectionDiagnostics')
    def test_remote_probe_requires_exact_valid_url_and_true(self, manager):
        app, view = Harness(), viewer()
        app._run_connection_probe(view, app.client, CONTEXT, URL)
        manager.return_value.probe.return_value = report(True)
        app.pending[-1][1]()
        manager.return_value.probe.assert_called_once_with(url=URL, check_remote=True)
        self.assertNotIn(URL, app.pending[-1][0])
        self.assertEqual(app.settings, {})

    @patch('svn_client.connection_ui.ConnectionDiagnostics')
    def test_invalid_url_does_not_start_or_echo(self, manager):
        app, view = Harness(), viewer()
        app._run_connection_probe(view, app.client, CONTEXT, 'https://user:PRIVATE@host/repo')
        self.assertFalse(app.pending)
        manager.assert_not_called()
        self.assertNotIn('PRIVATE', str(view.finish.call_args))

    def test_stale_client_or_config_prevents_query(self):
        for replacement in ('client', 'settings'):
            app, view = Harness(), viewer()
            old = app.client
            if replacement == 'client':
                app.client = SimpleNamespace(working_copy=Path('/working-copy'), executable='/trusted/svn')
            else:
                app.settings['svn_executable'] = '/new/svn'
            app._run_connection_probe(view, old, CONTEXT, None)
            self.assertFalse(app.pending)
            self.assertTrue(view.finish.call_args.kwargs['stale'])

    def test_closed_or_superseded_view_discards_callback(self):
        for state in ('closed', 'generation'):
            app, view = Harness(), viewer()
            app._run_connection_probe(view, app.client, CONTEXT, None)
            setattr(view, state, True if state == 'closed' else 99)
            app.finish('old report')
            view.finish.assert_not_called()

    def test_changed_workspace_discards_completed_result(self):
        app, view = Harness(), viewer()
        app._run_connection_probe(view, app.client, CONTEXT, None)
        app.client = None
        app.finish('old report PRIVATE')
        self.assertNotIn('PRIVATE', str(view.finish.call_args))
        self.assertTrue(view.finish.call_args.kwargs['stale'])

    @patch('svn_client.connection_ui.ConnectionDiagnostics')
    def test_worker_exception_is_sanitized_before_shared_logger(self, manager):
        app, view = Harness(), viewer()
        app._run_connection_probe(view, app.client, CONTEXT, URL)
        manager.side_effect = RuntimeError('https://user:PRIVATE@host/token')
        with self.assertRaises(RuntimeError) as caught:
            app.pending[-1][1]()
        self.assertEqual(str(caught.exception), SAFE_FAILURE)
        self.assertNotIn('PRIVATE', str(caught.exception))
        app.pending[-1][3]['on_error'](caught.exception)
        view.finish.assert_called_once_with(SAFE_FAILURE)

    def test_display_exception_is_also_safe_for_shared_logger(self):
        app, view = Harness(), viewer()
        app._run_connection_probe(view, app.client, CONTEXT, None)
        view.finish.side_effect = RuntimeError('PRIVATE')
        with self.assertRaises(RuntimeError) as caught:
            app.finish('text')
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_stale_display_or_context_failure_cannot_escape_with_raw_error(self):
        for state in ('stale_display', 'context_check'):
            app, view = Harness(), viewer()
            app._run_connection_probe(view, app.client, CONTEXT, None)
            if state == 'stale_display':
                app.client = None
                view.finish.side_effect = RuntimeError('PRIVATE stale display')
            else:
                app._diagnostic_context = Mock(side_effect=RuntimeError('PRIVATE context'))
            with self.assertRaises(RuntimeError) as caught:
                app.finish('text')
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertIn('结果未保存', str(caught.exception))

    def test_callbacks_and_menu_entry_exist(self):
        for cls in (ConnectionUiMixin, ConnectionDialog):
            source = inspect.getsource(cls)
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self':
                    if node.func.attr == 'request':
                        continue
                    self.assertTrue(callable(getattr(SvnDesk if cls is ConnectionUiMixin else cls, node.func.attr, None)), node.func.attr)
        self.assertIn('command=self.show_connection_diagnostics', inspect.getsource(SvnDesk._make_menu))


class DialogInteractionTests(unittest.TestCase):
    def remote_dialog(self):
        value = Mock(running=False, closed=False, generation=3)
        value.opt_in_var.get.return_value = True
        value.url_var.get.return_value = URL
        return value

    @patch('svn_client.connection_dialogs.messagebox.askyesno', return_value=False)
    def test_remote_confirmation_cancel_makes_no_request(self, ask):
        obj = self.remote_dialog()
        ConnectionDialog._remote(obj)
        obj.request.assert_not_called()
        self.assertEqual(ask.call_args.kwargs['default'], 'no')

    @patch('svn_client.connection_dialogs.messagebox.askyesno', return_value=True)
    def test_remote_request_only_after_checkbox_and_confirmation(self, ask):
        obj = self.remote_dialog()
        ConnectionDialog._remote(obj)
        obj.request.assert_called_once_with(obj, URL)
        obj.request.reset_mock()
        obj.opt_in_var.get.return_value = False
        ConnectionDialog._remote(obj)
        obj.request.assert_not_called()
        self.assertEqual(ask.call_count, 1)

    @patch('svn_client.connection_dialogs.messagebox.askyesno')
    def test_context_or_consent_change_while_confirmation_open_cancels(self, ask):
        for field in ('generation', 'closed', 'consent', 'url'):
            obj = self.remote_dialog()
            def change(*args, **kwargs):
                if field == 'consent': obj.opt_in_var.get.return_value = False
                elif field == 'url': obj.url_var.get.return_value = URL + '/other'
                else: setattr(obj, field, 4 if field == 'generation' else True)
                return True
            ask.side_effect = change
            ConnectionDialog._remote(obj)
            obj.request.assert_not_called()

    @patch('svn_client.connection_dialogs.messagebox.showwarning')
    def test_invalid_credential_url_is_cleared_and_never_echoed(self, warn):
        obj = self.remote_dialog()
        obj.url_var.get.return_value = 'https://user:PRIVATE@host'
        ConnectionDialog._remote(obj)
        obj.url_var.set.assert_called_once_with('')
        obj.request.assert_not_called()
        self.assertEqual(warn.call_args.args[1], INVALID_URL)
        self.assertNotIn('PRIVATE', str(warn.call_args))

    def test_local_button_ignores_filled_remote_input(self):
        obj = self.remote_dialog()
        ConnectionDialog._local(obj)
        obj.request.assert_called_once_with(obj, None)
        obj.url_var.get.assert_not_called()

    def test_begin_clears_consent_and_close_invalidates_without_cancel_claim(self):
        obj = self.remote_dialog()
        self.assertEqual(ConnectionDialog.begin(obj), 4)
        obj.opt_in_var.set.assert_called_once_with(False)
        self.assertTrue(obj.running)
        self.assertIn('会继续至完成 / 超时', obj.note_var.set.call_args.args[0])
        ConnectionDialog._close(obj)
        self.assertTrue(obj.closed)
        self.assertEqual(obj.generation, 5)
        obj.destroy.assert_called_once()

    def test_editing_url_invalidates_previous_consent(self):
        obj = self.remote_dialog()
        ConnectionDialog._input_changed(obj)
        self.assertEqual(obj.generation, 4)
        obj.opt_in_var.set.assert_called_once_with(False)

    def test_controls_disabled_during_probe(self):
        obj = self.remote_dialog()
        obj.running = True
        ConnectionDialog._update_controls(obj)
        for control in ('local_button', 'remote_button', 'url_entry', 'opt_in'):
            getattr(obj, control).configure.assert_called_once_with(state='disabled')

    def test_constructor_blank_defaults_no_query_both_scrollbars_and_close_focus(self):
        buttons, variables, checks, bars = [], [], [], []
        with ExitStack() as stack:
            def patched(name, **kwargs): return stack.enter_context(patch(name, **kwargs))
            patched('svn_client.connection_dialogs.tk.Toplevel.__init__', return_value=None)
            for name in ('title', 'transient', 'geometry', 'minsize', 'grab_set', 'wait_window', 'protocol'):
                patched('svn_client.connection_dialogs.tk.Toplevel.' + name)
            bindings = patched('svn_client.connection_dialogs.tk.Toplevel.bind')
            patched('svn_client.connection_dialogs.tk.Toplevel.winfo_screenwidth', return_value=1600)
            patched('svn_client.connection_dialogs.tk.Toplevel.winfo_screenheight', return_value=1000)
            for name in ('Frame', 'Label', 'LabelFrame', 'Entry'):
                patched('svn_client.connection_dialogs.ttk.' + name)
            text = patched('svn_client.connection_dialogs.tk.Text')
            def button(*a, **k):
                obj = Mock(); buttons.append((k, obj)); return obj
            def variable(*a, **k):
                variables.append(k); return Mock()
            def check(*a, **k):
                checks.append(k); return Mock()
            def scrollbar(*a, **k):
                bars.append(k); return Mock()
            patched('svn_client.connection_dialogs.ttk.Button', side_effect=button)
            patched('svn_client.connection_dialogs.ttk.Checkbutton', side_effect=check)
            patched('svn_client.connection_dialogs.tk.StringVar', side_effect=variable)
            patched('svn_client.connection_dialogs.tk.BooleanVar', side_effect=variable)
            patched('svn_client.connection_dialogs.ttk.Scrollbar', side_effect=scrollbar)
            parent, request = Mock(), Mock()
            parent.display.px.side_effect = lambda value: value
            obj = ConnectionDialog(parent, CONTEXT, request)
            request.assert_not_called()
            self.assertEqual(variables[:2], [{'value': ''}, {'value': False}])
            self.assertEqual([item['orient'] for item in bars], ['vertical', 'horizontal'])
            self.assertEqual(buttons[-1][0]['text'], '关闭')
            buttons[-1][1].focus_set.assert_called_once_with()
            self.assertEqual(buttons[1][0]['state'], 'disabled')
            self.assertNotIn('<Return>', [call.args[0] for call in bindings.call_args_list])
            self.assertIn('selectbackground', text.call_args.kwargs)
            self.assertFalse(obj.running)


if __name__ == '__main__':
    unittest.main()
