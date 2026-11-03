"""Bounded, explicit one-path history and comparison GUI orchestration."""
from __future__ import annotations

import json
from tkinter import messagebox, ttk
import tkinter as tk

from .history import HistoryManager, normalize_revision
from .history_dialogs import ComparisonDialog, normalize_scope_path


HISTORY_PAGE_LIMIT = 250
HISTORY_BACK_LIMIT = 128
DETAIL_TEXT_LIMIT = 64 * 1024


def bounded_text(text, limit=DETAIL_TEXT_LIMIT):
    encoded = str(text).encode("utf-8", errors="replace")
    prefix = encoded[:limit].decode("utf-8", errors="ignore")
    return prefix, len(encoded) > limit, len(encoded), len(prefix.encode("utf-8"))


def history_cell(value, limit=240):
    line = str(value).split("\n", 1)[0].replace("\r", " ")
    return line[:limit] + ("…" if len(line) > limit else "")


def comparison_preview(result, limit):
    text, clipped, actual_bytes, shown_bytes = bounded_text(result.diff.text, limit)
    truncated = bool(result.diff.truncated or clipped)
    source_bytes = max(int(result.diff.source_utf8_bytes), actual_bytes)
    warning = (f"编码警告：{result.diff.undecodable_bytes:,} 个不可按 UTF-8 解码的字节、"
               f"{result.diff.escaped_controls:,} 个控制字符已显示为转义。"
               "未猜测本地编码；此显示不能作为精确补丁。\n"
               if result.diff.undecodable_bytes or result.diff.escaped_controls else "")
    heading = (f"范围：{json.dumps(result.path, ensure_ascii=False)}\n"
               f"方向：{result.from_revision} → {result.to_revision}\n"
               f"显示：{shown_bytes:,} / {source_bytes:,} UTF-8 字节（解码、遮蔽后）\n"
               + ("已截断：仅为预览，不能作为完整补丁。\n" if truncated else "本次返回的文本已完整显示。\n")
               + warning
               + "仅文本 / 属性差异，不包含二进制载荷；不是可编辑合并视图。\n\n")
    return heading + (text or "SVN 对这个确切范围和版本组合没有返回文本差异。不能据此断言二进制内容相同。")


def history_detail(entry, path):
    paths = "\n".join(f"{item.get('action', '')}  {json.dumps(item.get('path', ''), ensure_ascii=False)}"
                      for item in entry.changed_paths)
    raw = (f"查询范围：{json.dumps(path, ensure_ascii=False)}\n修订：r{entry.revision}\n"
           f"作者：{entry.author}\n日期：{entry.date}\n\n{entry.message}\n\n"
           "本次提交的全部更改路径（可能包含查询范围以外的路径）：\n" + paths)
    text, truncated, source_bytes, returned_bytes = bounded_text(raw)
    if truncated:
        text += f"\n\n[详情已截断：显示 {returned_bytes:,}/{source_bytes:,} UTF-8 字节]"
    return text + "\n\n双击此修订可显式选择比较当前查询路径的 r(N−1) → rN；不会自动扩大为整次提交差异。"


class HistoryUiMixin:
    def _init_history_state(self):
        self._history_request = 0
        self._history_form_write = False
        self._history_loaded = False
        self._history_loading = False
        self._history_auto_pending = True
        self._history_path = "."
        self._history_start = "HEAD"
        self._history_next = None
        self._history_back = []
        self._comparison_request = 0

    def _build_history_controls(self, parent, toolbar_class):
        scope = ttk.Frame(parent, padding=(10, 9, 10, 3))
        scope.pack(fill="x")
        ttk.Label(scope, text="明确相对路径").pack(side="left", padx=(0, 8))
        self.history_path_var = tk.StringVar(value=".")
        self.history_start_var = tk.StringVar(value="HEAD")
        self.history_limit_var = tk.StringVar(value="100")
        self.history_note_var = tk.StringVar(value=". 明确表示当前工作副本根；目录查询包含后代，不会更新文件。")
        self.history_path_entry = ttk.Entry(scope, textvariable=self.history_path_var)
        self.history_path_entry.pack(side="left", fill="x", expand=True)
        self.history_path_entry.bind("<Return>", lambda event: self.load_history())
        ttk.Button(scope, text="工作副本根历史", command=self.open_root_history).pack(side="left", padx=(8, 0))
        options = ttk.Frame(parent, padding=(10, 5, 10, 3))
        options.pack(fill="x")
        ttk.Label(options, text="起始修订").pack(side="left", padx=(0, 8))
        ttk.Combobox(options, textvariable=self.history_start_var, values=("HEAD", "BASE"), width=12).pack(side="left")
        ttk.Label(options, text="每页").pack(side="left", padx=(16, 8))
        ttk.Combobox(options, textvariable=self.history_limit_var, values=("25", "50", "100", "250"), state="readonly", width=6).pack(side="left")
        ttk.Label(options, text="HEAD / BASE / 非负数字；每次仅保留一页", style="Muted.TLabel").pack(side="left", padx=12)
        actions = toolbar_class(parent, padding=(10, 5, 10, 0))
        actions.pack(fill="x")
        self._button(actions, "查询 / 刷新此范围", self.load_history)
        self.history_prev_button = self._button(actions, "较新一页", self.history_previous_page)
        self.history_next_button = self._button(actions, "更早一页", self.history_next_page)
        self._button(actions, "比较当前范围的版本…", self.compare_history_scope)
        ttk.Label(parent, textvariable=self.history_note_var, style="Muted.TLabel", padding=(10, 4, 10, 8),
                  wraplength=self.display.px(720)).pack(fill="x")
        for variable in (self.history_path_var, self.history_start_var, self.history_limit_var):
            variable.trace_add("write", self._history_query_changed)

    def _set_history_form(self, path=None, start=None, limit=None):
        self._history_form_write = True
        try:
            if path is not None:
                self.history_path_var.set(path)
            if start is not None:
                self.history_start_var.set(str(start))
            if limit is not None:
                self.history_limit_var.set(str(limit))
        finally:
            self._history_form_write = False

    def _reset_history_state(self, *, reset_form=False):
        self._history_request += 1
        self._comparison_request += 1
        self._history_loaded = False
        self._history_loading = False
        self._history_auto_pending = True
        self._history_next = None
        self._history_back = []
        self.logs = []
        if reset_form:
            self._history_path, self._history_start = ".", "HEAD"
            self._set_history_form(path=".", start="HEAD", limit=100)
        self._render_history()
        self.history_note_var.set("历史未读取；目录范围包含其后代。点击查询明确读取当前范围。")
        self._update_history_controls()

    def _history_query_changed(self, *_):
        if self._history_form_write:
            return
        # Generation changes even if a user later restores identical text.
        self._history_request += 1
        self._comparison_request += 1
        self._history_auto_pending = False
        self._history_loaded = False
        self._history_loading = False
        self._history_next = None
        self._history_back = []
        self.logs = []
        self._render_history()
        self.history_note_var.set("参数已改变，旧结果已清除。点击查询读取新范围；空路径不代表根目录。")
        if self.tabs.index(self.tabs.select()) == 2:
            self._show_preview("历史查询参数已改变", "尚未读取新范围，请点击“查询 / 刷新此范围”。")
        self._update_history_controls()

    def _update_history_controls(self):
        if not hasattr(self, "history_prev_button"):
            return
        enabled = not self._busy and not self.demo and self.client and self._history_loaded
        self.history_prev_button.configure(state="normal" if enabled and self._history_back else "disabled")
        self.history_next_button.configure(state="normal" if enabled and self._history_next is not None else "disabled")

    def _selected_history_file(self):
        """Only a single explicit file row; no group or root fallback."""
        tab = self.tabs.index(self.tabs.select())
        known_files = {entry.path for entry in self.changelist_candidates}
        if tab == 0:
            rows = self.changes.selection()
            if len(rows) != 1 or rows[0] not in self._change_lookup:
                return None
            entry = self._change_lookup[rows[0]]
            if entry.path not in known_files or entry.item in {"unversioned", "ignored", "external"}:
                return None
            return entry.path
        if tab == 1:
            rows = self.files.selection()
            if len(rows) != 1 or self._file_kind.get(rows[0]) != "file":
                return None
            path = self._file_lookup.get(rows[0])
            return path if path in known_files else None
        return None

    def _history_file_action_available(self):
        return bool(not self._busy and not self.demo and self.client and self._selected_history_file() is not None)

    def show_selected_history(self):
        path = self._selected_history_file()
        if path is None:
            messagebox.showinfo("请选择一个文件", "请在待提交更改或工作副本文件页明确选择一个已版本化文件行。组标题、空选择和多个文件不会自动变成工作副本根。", parent=self)
            return
        self.open_path_history(path)

    def compare_selected_file(self):
        path = self._selected_history_file()
        if path is None:
            messagebox.showinfo("请选择一个文件", "请明确选择一个已版本化文件行。需要比较整个目录时，请在历史页明确输入该相对目录。", parent=self)
            return
        self.show_comparison(path)

    def open_root_history(self):
        self.open_path_history(".")

    def open_path_history(self, path):
        if not self._can_operate():
            return
        try:
            path = normalize_scope_path(path)
        except ValueError as error:
            messagebox.showwarning("历史路径不可用", str(error), parent=self)
            return
        self._history_request += 1
        self._history_auto_pending = False
        self._history_back = []
        self._history_loaded = False
        self._set_history_form(path=path, start="HEAD")
        scheduled = self._history_request
        client = self.client
        self.select_tab(2)
        # NotebookTabChanged is queued by Tk. Start after that event so the
        # intentional navigation cannot invalidate this request's preview.
        def start():
            if (not self._closing and self.client is client and scheduled == self._history_request
                    and self.tabs.index(self.tabs.select()) == 2):
                self.load_history()
        self.after_idle(start)

    def load_history(self):
        if not self._can_operate():
            return
        self._load_history_page(back=[])

    def history_next_page(self):
        if not self._can_operate() or not self._history_loaded or self._history_next is None:
            return
        back = [*self._history_back, self._history_start][-HISTORY_BACK_LIMIT:]
        self._load_history_page(start=self._history_next, back=back)

    def history_previous_page(self):
        if not self._can_operate() or not self._history_loaded or not self._history_back:
            return
        self._load_history_page(start=self._history_back[-1], back=self._history_back[:-1])

    def _history_signature(self):
        return self.history_path_var.get(), self.history_start_var.get(), self.history_limit_var.get()

    def _load_history_page(self, *, start=None, back=()):
        try:
            path = normalize_scope_path(self.history_path_var.get())
            start = normalize_revision(self.history_start_var.get() if start is None else start)
            limit = int(self.history_limit_var.get())
            if not 1 <= limit <= HISTORY_PAGE_LIMIT:
                raise ValueError(f"历史页大小必须在 1–{HISTORY_PAGE_LIMIT} 之间。")
        except (ValueError, TypeError) as error:
            messagebox.showwarning("历史参数不可用", str(error), parent=self)
            return
        self._set_history_form(path=path, start=start)
        self._history_request += 1
        request = self._history_request
        client, manager = self.client, HistoryManager(self.client)
        signature = self._history_signature()
        self._history_auto_pending = False
        self._history_loaded = False
        self._history_loading = False
        self._history_next = None
        self._history_loading = True
        self.logs = []
        self._render_history()
        self._show_preview("正在读取明确范围的历史", f"路径：{json.dumps(path, ensure_ascii=False)}\n从 {start} 向旧修订查询，最多 {limit} 项。\n目录包含后代；不会更新或修改文件。")
        preview_request = self._preview_request
        self.history_note_var.set("正在读取；改变参数或选择其他对象会使旧请求失效。")
        def current():
            return (not self._closing and self.client is client and request == self._history_request
                    and signature == self._history_signature() and self.tabs.index(self.tabs.select()) == 2
                    and preview_request == self._preview_request)
        def discard():
            if not self._closing and self.client is client and request == self._history_request:
                self._history_loading = False
                self.history_note_var.set("本次历史结果已因导航或选择变化而丢弃。需要时请重新点击查询。")
                self._update_history_controls()
        def loaded(result):
            if not current():
                discard()
                return
            if (normalize_scope_path(result.path) != path or str(result.start_revision) != start
                    or len(result.entries) > limit
                    or (result.has_more and (result.next_start_revision is None or not result.entries))):
                failed(ValueError("返回的历史范围或分页信息与本次请求不一致，没有采用结果。"))
                return
            self.logs = list(result.entries)
            self._history_path, self._history_start = path, start
            self._history_next = result.next_start_revision if result.has_more else None
            self._history_back = list(back)[-HISTORY_BACK_LIMIT:]
            self._history_loaded = True
            self._history_loading = False
            self._render_history()
            end = "还有更早记录" if result.has_more else "本次可见历史已到末尾"
            self.history_note_var.set(f"范围 {json.dumps(path, ensure_ascii=False)} · 起始 {start} · 本页 {len(self.logs)} 项 · {end}。仅保留一页；最多回退 128 页。")
            self._update_history_controls()
            self._show_preview("路径历史已读取", f"查询范围：{json.dumps(path, ensure_ascii=False)}\n本页 {len(self.logs)} 条提交，{end}。\n\n选中修订查看提交说明；更改路径列表属于整次提交，可能包含范围以外路径。\n双击修订可比较当前路径的前后版本，不会扩大为整个工作副本。")
        def failed(error):
            if not current():
                discard()
                return
            self._history_loading = False
            self.history_note_var.set("历史读取失败，没有把失败解释成无历史；请检查后明确重试。")
            self._show_preview("历史读取失败（只读）", f"路径：{json.dumps(path, ensure_ascii=False)}\n起始：{start}\n\n{error}\n\n没有写入或更新，也不会自动改查根目录。")
            self._update_history_controls()
        self._run("读取路径历史", lambda: manager.history(path, limit=limit, start_revision=start), loaded, on_error=failed, preserve_view_on_error=True)

    def preview_history(self, event=None):
        selected = self.history.selection()
        if not selected and self._history_loading:
            return
        self._preview_request += 1
        if len(selected) != 1 or selected[0] not in self._log_lookup:
            return
        entry = self._log_lookup[selected[0]]
        self._show_preview(f"{self._history_path} · 修订 r{entry.revision}", history_detail(entry, self._history_path))

    def preview_revision_diff(self):
        selected = self.history.selection()
        if len(selected) != 1 or selected[0] not in self._log_lookup or self._busy:
            return
        revision = int(self._log_lookup[selected[0]].revision)
        self.show_comparison(self._history_path, from_revision=str(max(0, revision - 1)), to_revision=str(revision))

    def compare_history_scope(self):
        try:
            path = normalize_scope_path(self.history_path_var.get())
        except ValueError as error:
            messagebox.showwarning("比较路径不可用", str(error), parent=self)
            return
        self.show_comparison(path)

    def show_comparison(self, path, *, from_revision="BASE", to_revision="WORKING"):
        if not self._can_operate():
            return
        try:
            path = normalize_scope_path(path)
        except ValueError as error:
            messagebox.showwarning("比较路径不可用", str(error), parent=self)
            return
        self._comparison_request += 1
        request = self._comparison_request
        client = self.client
        tab = self.tabs.index(self.tabs.select())
        preview_request = self._preview_request
        dialog = ComparisonDialog(self, path, from_revision=from_revision, to_revision=to_revision)
        if dialog.result is None:
            return
        if (self.client is not client or self._closing or request != self._comparison_request
                or tab != self.tabs.index(self.tabs.select()) or preview_request != self._preview_request):
            self.status_var.set("对象或工作区已改变，未读取旧对象的比较。")
            return
        left, right, limit = dialog.result
        self._read_comparison(path, left, right, limit)

    def _read_comparison(self, path, left, right, limit):
        try:
            path = normalize_scope_path(path)
            left = normalize_revision(left)
            right = normalize_revision(right, working=True)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit not in {64 * 1024, 256 * 1024, 1024 * 1024}:
                raise ValueError("比较显示上限不可用。")
        except ValueError as error:
            messagebox.showwarning("比较参数不可用", str(error), parent=self)
            return
        self._comparison_request += 1
        request = self._comparison_request
        client, manager = self.client, HistoryManager(self.client)
        tab = self.tabs.index(self.tabs.select())
        self._show_preview("正在读取明确版本差异", f"路径：{json.dumps(path, ensure_ascii=False)}\n方向：{left} → {right}\n只读；显示最多 {limit // 1024} KiB。")
        preview_request = self._preview_request
        def current():
            return (not self._closing and self.client is client and request == self._comparison_request
                    and tab == self.tabs.index(self.tabs.select()) and preview_request == self._preview_request)
        def loaded(result):
            if not current():
                return
            if (normalize_scope_path(result.path) != path or result.from_revision != left or result.to_revision != right
                    or result.binary_payloads_included):
                failed(ValueError("比较返回的路径、方向或载荷与本次请求不符，没有显示结果。"))
                return
            self._show_preview(f"{path} · {left} → {right}", comparison_preview(result, limit), diff=True)
        def failed(error):
            if current():
                self._show_preview("比较读取失败（只读）", f"路径：{json.dumps(path, ensure_ascii=False)}\n方向：{left} → {right}\n\n{error}\n\n没有更新、合并或写入；失败不代表两个版本相同，不会自动扩大范围或重试。")
        self._run("读取路径版本比较", lambda: manager.compare(path, from_revision=left, to_revision=right, max_text_bytes=limit), loaded, on_error=failed, preserve_view_on_error=True)
