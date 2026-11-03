"""Scoped GUI orchestration for local file operations and repository locks.

All I/O runs in SvnDesk._run workers. Only explicit file rows in the pending
or working-copy files tab may enter these flows. Navigation trees and group
headers never imply operation targets.
"""
from __future__ import annotations

import os
from tkinter import messagebox

from .conflict_dialogs import display_path
from .locks_dialogs import LockConfirmDialog, LockInspectDialog, can_lock, can_unlock, lock_state_text


def path_keys(paths):
    return {os.path.normcase(os.path.normpath(str(path).replace("\\", "/"))) for path in paths}


def same_scope(first, second):
    return bool(first) and len(first) == len(second) and path_keys(first) == path_keys(second)


class OperationUiMixin:
    def _file_action_context(self):
        """Main-thread selection snapshot only, with no disk or SVN reads."""
        tab = self.tabs.index(self.tabs.select())
        if tab == 0:
            selected = self.changes.selection()
            if not selected or any(row not in self._change_lookup for row in selected):
                return None
            paths = tuple(self._change_lookup[row].path for row in selected)
        elif tab == 1:
            selected = self.files.selection()
            if not selected or any(row not in self._file_lookup or self._file_kind.get(row) != "file" for row in selected):
                return None
            paths = tuple(self._file_lookup[row] for row in selected)
        else:
            return None
        return tab, tuple(sorted(set(paths)))

    def _file_context_matches(self, client, context):
        if self._closing or self.client is not client:
            return False
        current = self._file_action_context()
        return bool(current) and current[0] == context[0] and same_scope(current[1], context[1])

    def _file_action_available(self, action):
        if self._busy or self.demo or not self.client:
            return False
        context = self._file_action_context()
        if not context:
            return False
        _, paths = context
        if len(paths) > 128 or action in {"file_move", "file_copy"} and len(paths) != 1:
            return False
        candidates = {entry.path: entry for entry in self.changelist_candidates}
        if any(path not in candidates for path in paths):
            return False
        entries = [candidates[path] for path in paths]
        if any(entry.item not in {"normal", "modified"} or entry.conflicted
               or entry.props not in {"normal", "modified", "none"}
               or entry.copied or entry.moved_from or entry.moved_to
               or entry.file_external or entry.switched or entry.wc_locked for entry in entries):
            return False
        if action == "file_delete":
            return all(entry.item == "normal" and entry.props in {"none", "normal"} for entry in entries)
        return action in {"file_move", "file_copy", "file_lock"}

    def _file_action_menu(self, menu):
        for label, callback in (("查看此文件历史", self.show_selected_history), ("比较此文件版本…", self.compare_selected_file)):
            menu.add_command(label=label, command=callback, state="normal" if self._history_file_action_available() else "disabled")
        menu.add_separator()
        for label, callback, action in (
                ("移动 / 重命名文件…", self.move_file, "file_move"),
                ("复制文件（保留历史）…", self.copy_file, "file_copy"),
                ("删除已版本化文件…", self.delete_files, "file_delete"),
                ("检查仓库文件锁…", self.inspect_locks, "file_lock")):
            menu.add_command(label=label, command=callback,
                             state="normal" if self._file_action_available(action) else "disabled")

    def _files_context_menu(self, event):
        if self._busy:
            return
        import tkinter as tk
        row = self.files.identify_row(event.y)
        if row and row not in self.files.selection():
            self.files.selection_set(row)
        elif not row:
            self.files.selection_remove(*self.files.selection())
        menu = tk.Menu(self, tearoff=False)
        if row:
            menu.add_command(label="打开目录 / 查看内容", command=self.preview_file)
            menu.add_separator()
        self._file_action_menu(menu)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _operation_scope_text(self, client, paths):
        return "工作副本：" + display_path(client.working_copy) + "\n" + "\n".join(display_path(path) for path in paths)

    def _operation_preflight_failed(self, label, client, context, error):
        scope = self._operation_scope_text(client, context[1])
        text = scope + "\n\n没有发出修改命令。\n\n" + str(error)
        self._append_log(label + "：\n" + text)
        if self._file_context_matches(client, context):
            self._show_preview(label, text)
        messagebox.showwarning(label, text, parent=self)
        if self.client is client:
            self.refresh()

    def _run_reviewed_operation(self, label, work, client, context, scope_text, summary, is_refused, render=None):
        """No implicit retries; stale callbacks never replace new selections."""
        def completed(result):
            stdout, stderr = getattr(result, "stdout", ""), getattr(result, "stderr", "")
            if stdout:
                self._append_log(stdout)
            if stderr:
                self._append_log(stderr)
            details = render(result) if render else ""
            text = scope_text + "\n\n" + summary + ("\n\n" + details if details else "")
            self._append_log(label + "：\n" + text)
            if self._file_context_matches(client, context):
                self._show_preview(label, text)
            if self.client is client:
                self.refresh()
        def failed(error):
            refused = is_refused(error)
            title = label + ("被拒绝" if refused else "结果需要核查")
            detail = ("本次在修改命令执行前被拒绝，请重新预检和确认。" if refused else
                      "部分或全部目标可能已经改变。失败不等于回滚，程序不会自动重试。请先核查实际状态。")
            observed = getattr(error, "observed", ())
            text = scope_text + "\n\n" + detail + "\n\n" + str(error)
            if observed:
                text += "\n\n随后读取的服务器状态：\n" + lock_state_text(observed)
            self._append_log(title + "：\n" + text)
            if self._file_context_matches(client, context):
                self._show_preview(title, text)
            messagebox.showwarning(title, text, parent=self)
            if self.client is client:
                self.refresh()
        self._run(label, work, completed, on_error=failed)

    def delete_files(self):
        self._begin_file_operation("delete")

    def move_file(self):
        self._begin_file_operation("move")

    def copy_file(self):
        self._begin_file_operation("copy")

    def _begin_file_operation(self, operation):
        if not self._can_operate():
            return
        if operation not in {"delete", "move", "copy"}:
            return
        context = self._file_action_context()
        if not context or not self._file_action_available("file_" + operation):
            messagebox.showinfo("请选择受支持的文件行", "请在待提交更改或工作副本文件页选择普通已版本化文件，不选目录或分组。\n\n移动 / 复制限一个文件；删除只允许内容和属性均未修改的文件。冲突、外部引用及已有结构变化需先处理。", parent=self)
            return
        from .fileops import FileOperationManager, FileOperationError
        from .fileops_dialogs import FileDestinationDialog, FileOperationConfirmDialog
        client, paths = self.client, context[1]
        manager = FileOperationManager(client)
        destination = None
        if operation != "delete":
            dialog = FileDestinationDialog(self, operation, paths[0], str(client.working_copy))
            if dialog.result is None:
                return
            destination = dialog.result
            if not self._file_context_matches(client, context):
                self.status_var.set("工作区或选区已变化，未预检文件操作。")
                return
        names = {"delete": "删除文件", "move": "移动 / 重命名文件", "copy": "复制文件（保留历史）"}
        label = names[operation]
        def preflight():
            return manager.preflight_delete(paths) if operation == "delete" else getattr(manager, "preflight_" + operation)(paths[0], destination)
        def checked(plan):
            if not self._file_context_matches(client, context):
                self.status_var.set("预检期间工作区或选区已变化，未执行文件操作。")
                return
            affected = (*paths, destination) if destination is not None else paths
            expected_destination = None if destination is None else next(iter(path_keys((destination,))))
            actual_destination = None if plan.destination is None else next(iter(path_keys((plan.destination,))))
            if (plan.operation != operation or not same_scope(plan.paths, paths)
                    or expected_destination != actual_destination
                    or not same_scope(plan.affected_paths, affected)
                    or path_keys((plan.workspace,)) != path_keys((str(client.working_copy),))
                    or plan.scope != "regular_files_only"):
                messagebox.showerror("文件操作方案不匹配", "预检范围、目标或工作副本与选中内容不符，未执行操作。", parent=self)
                return
            dialog = FileOperationConfirmDialog(self, plan)
            if not dialog.confirmed:
                return
            if not self._file_context_matches(client, context):
                self.status_var.set("确认期间工作区或选区已变化，未执行文件操作。")
                return
            def execute():
                options = dict(confirmed=True, expected_state_sha256=plan.state_sha256)
                return manager.delete(plan.paths, **options) if operation == "delete" else getattr(manager, operation)(plan.paths[0], plan.destination, **options)
            summary = "已完成本地 SVN 计划操作并复核目标；没有提交仓库。请检查待提交状态和内容，再决定提交。"
            if operation == "move":
                summary += "\n移动的源删除和目标新增应一起审查、提交。不会自动按变更列表补齐或扩大范围。"
            scope = self._operation_scope_text(client, plan.affected_paths)
            self._run_reviewed_operation(label, execute, client, context, scope, summary,
                                         lambda error: isinstance(error, FileOperationError))
        self._run("预检" + label, preflight, checked,
                  on_error=lambda error: self._operation_preflight_failed("文件操作预检未通过", client, context, error))

    def inspect_locks(self):
        if not self._can_operate():
            return
        context = self._file_action_context()
        if not context or not self._file_action_available("file_lock"):
            messagebox.showinfo("选择可检查仓库锁的文件", "请在待提交更改或工作副本文件页选择最多 128 个已提交的普通文件。目录、分组、冲突、外部引用及待添加/删除/复制/移动节点不在此流程内。", parent=self)
            return
        from .locks import LockManager
        client, paths = self.client, context[1]
        manager = LockManager(client)
        def inspected(files):
            if not self._file_context_matches(client, context):
                self.status_var.set("查询期间工作区或选区已变化，未继续仓库锁操作。")
                return
            if not same_scope(tuple(item.path for item in files), paths):
                messagebox.showerror("锁检查范围不匹配", "服务器查询没有返回完整的所选文件范围，未继续操作。", parent=self)
                return
            self._show_preview("当前服务器文件锁", lock_state_text(files))
            dialog = LockInspectDialog(self, files, str(client.working_copy))
            if dialog.result is None:
                return
            action, comment = dialog.result
            if action not in {"lock", "unlock"} or not (can_lock(files) if action == "lock" else can_unlock(files)):
                return
            if not self._file_context_matches(client, context):
                self.status_var.set("检查期间工作区或选区已变化，未继续预检。")
                return
            self._preflight_lock_operation(manager, client, context, action, comment)
        self._run("只读查询所选仓库文件锁", lambda: tuple(manager.inspect(paths)), inspected,
                  on_error=lambda error: self._operation_preflight_failed("仓库锁检查未通过", client, context, error))

    def _preflight_lock_operation(self, manager, client, context, action, comment):
        from .locks import LockError, LockOperationError
        paths = context[1]
        label = "仓库文件加锁" if action == "lock" else "仓库文件解锁"
        def checked(plan):
            if not self._file_context_matches(client, context):
                self.status_var.set("预检期间工作区或选区已变化，未执行仓库锁操作。")
                return
            valid = can_lock(plan.files) if action == "lock" else can_unlock(plan.files)
            if plan.action != action or not same_scope(plan.paths, paths) or plan.comment != comment or not valid:
                messagebox.showerror("仓库锁方案不匹配", "预检的动作、文件、备注或锁归属发生变化，未执行操作。", parent=self)
                return
            dialog = LockConfirmDialog(self, plan, str(client.working_copy))
            if not dialog.confirmed:
                return
            if not self._file_context_matches(client, context):
                self.status_var.set("确认期间工作区或选区已变化，未执行仓库锁操作。")
                return
            def execute():
                options = dict(confirmed=True, expected_state=plan.state_sha256)
                result = manager.lock(plan.paths, plan.comment, **options) if action == "lock" else manager.unlock(plan.paths, **options)
                if not result.verified or result.action != action or not same_scope(tuple(item.path for item in result.files), plan.paths):
                    raise LockOperationError("返回结果不能完整验证所选文件。操作可能已经执行，请重新只读检查锁状态。")
                return result
            self._run_reviewed_operation(label, execute, client, context,
                self._operation_scope_text(client, plan.paths),
                "命令完成后已重新读取并验证服务器锁状态。没有提交文件内容；服务器状态之后仍可能变化。",
                lambda error: isinstance(error, LockError) and not isinstance(error, LockOperationError),
                render=lambda result: lock_state_text(result.files))
        self._run("预检" + label,
                  lambda: manager.preflight_lock(paths, comment) if action == "lock" else manager.preflight_unlock(paths),
                  checked, on_error=lambda error: self._operation_preflight_failed("仓库锁预检未通过", client, context, error))
