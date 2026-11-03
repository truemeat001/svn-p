"""Async connection diagnostics: isolated viewer, captured context, safe errors."""
from __future__ import annotations

from dataclasses import dataclass
from tkinter import messagebox

from .connection import ConnectionDiagnostics, validate_probe_url
from .connection_dialogs import ConnectionDialog, SAFE_FAILURE, report_text


@dataclass(frozen=True)
class DiagnosticContext:
    working_copy: str | None
    executable: str | None
    configured_executable: str | None


class ConnectionUiMixin:
    def _diagnostic_context(self):
        configured = self.settings.get("svn_executable") or None
        client = self.client
        working_copy = str(client.working_copy) if client is not None else None
        executable = str(client.executable) if client is not None else configured
        return DiagnosticContext(working_copy, executable, configured)

    def show_connection_diagnostics(self):
        if self._closing or self._busy:
            return
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不会读取真实 SVN 配置、目录或连接服务器。请正常启动后使用连接诊断。", parent=self)
            return
        # A modal's nested event loop must not start scheduled auto-restoration.
        self._manual_open_requested = True
        client, context = self.client, self._diagnostic_context()
        ConnectionDialog(self, context, lambda viewer, url: self._run_connection_probe(viewer, client, context, url))

    def _run_connection_probe(self, viewer, client, context, url):
        if self._closing or self._busy or self.demo or viewer.closed:
            return
        def context_matches():
            return not self._closing and self.client is client and self._diagnostic_context() == context
        if not context_matches():
            viewer.finish("工作副本或 SVN 配置已变化。未开始检查。", stale=True)
            return
        try:
            target = validate_probe_url(url) if url is not None else None
        except (ValueError, TypeError):
            viewer.finish(SAFE_FAILURE)
            return
        token = viewer.begin()
        remote = target is not None
        def work():
            # This wrapper guarantees that the shared operation logger cannot
            # receive credentials through an unexpected exception or payload.
            try:
                diagnostics = ConnectionDiagnostics(context.working_copy, executable=context.executable, timeout=15)
                report = diagnostics.probe(url=target, check_remote=True) if remote else diagnostics.probe()
                return report_text(report, remote_requested=remote)
            except Exception:
                raise RuntimeError(SAFE_FAILURE) from None
        def done(text):
            try:
                if viewer.closed or viewer.generation != token:
                    return
                if not context_matches():
                    viewer.finish("工作副本或 SVN 配置已变化。未将旧诊断应用到当前上下文。", stale=True)
                    return
                viewer.finish(text)
            except Exception:
                # Stale-result presentation and context checks need the same
                # boundary as normal rendering before shared queue logging.
                raise RuntimeError("诊断结果窗口不可用，结果未保存。") from None
        def failed(_error):
            done(SAFE_FAILURE)
        self._run("生成连接诊断（显式 URL）" if remote else "生成连接诊断（仅本地）",
                  work, done, on_error=failed, preserve_view_on_error=True)
