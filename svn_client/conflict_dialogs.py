"""Explicit, conservative conflict inspection and confirmation dialogs.

No SVN commands or filesystem writes occur in this module. The first GUI
workflow supports only retaining a manually repaired ordinary working file.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox, ttk

from .theme import text_options


REASONS = {
    "metadata_unavailable": "无法读取必要的 SVN 元数据",
    "different_or_unknown_working_copy": "工作副本根不一致或无法确认",
    "select_working_copy_root": "请先打开实际工作副本根",
    "different_or_unknown_repository": "仓库身份不一致或无法确认",
    "not_conflicted": "该节点已经不是冲突状态，请刷新",
    "tree_conflict_requires_external_resolution": "树冲突需要在可信 SVN 工具中处理结构变化",
    "external_target": "外部引用目标不在此解决范围内",
    "copy_or_move_target": "复制或移动节点暂不支持",
    "switched_target": "切换过的节点暂不支持",
    "working_copy_locked": "工作副本存在管理锁",
    "unsafe_ancestor": "祖先节点存在冲突或不安全结构状态",
    "unsupported_node_status": "节点状态暂不支持",
    "unsupported_property_status": "属性状态暂不支持",
    "unsupported_schedule": "节点计划状态暂不支持",
    "unsupported_filesystem_node": "不是受支持的普通文件",
    "node_kind_changed": "文件类型已改变",
    "filesystem_node_unavailable": "无法读取本地节点",
    "unsupported_node_kind": "节点类型暂不支持",
    "property_conflict_requires_external_resolution": "属性或混合冲突请使用可信 SVN 工具处理",
    "property_conflict_not_supported": "属性或混合冲突请使用可信 SVN 工具处理",
    "property_conflict_requires_manual_external_resolution": "属性或混合冲突请使用可信 SVN 工具处理",
    "ordinary_file_required": "当前只支持普通工作文件的内容冲突",
    "content_fingerprint_unavailable": "无法完成文件内容校验（含超过大小限制或读取期间变化）",
}


def display_path(path):
    """Quote paths so control characters cannot disguise the reviewed scope."""
    return json.dumps(str(path), ensure_ascii=False)


def is_conflicted(entry):
    return entry.item == "conflicted" or entry.props == "conflicted" or entry.tree_conflicted


def can_keep_working(inspections):
    return bool(inspections) and all(
        entry.kind == "file" and entry.text_conflicted
        and not entry.property_conflicted and not entry.tree_conflicted
        and not entry.blocked_reasons and "working" in entry.allowed_accepts
        for entry in inspections)


def inspection_text(entry):
    kinds = []
    if entry.text_conflicted:
        kinds.append("文件内容冲突")
    if entry.property_conflicted:
        kinds.append("属性冲突")
    if entry.tree_conflicted:
        kinds.append("树冲突")
    blocked = "\n".join("• " + REASONS.get(reason, reason) for reason in entry.blocked_reasons)
    if entry.property_conflicted:
        blocked = "• 当前 GUI 不解决属性或混合冲突。\n" + blocked
    if entry.tree_conflicted:
        blocked = "• 当前 GUI 不解决树冲突。\n" + blocked
    return (f"路径：{display_path(entry.path)}\n节点类型：{entry.kind}\n冲突类型：{'、'.join(kinds) or '无冲突'}\n"
            f"基础修订：{entry.status.revision}\n\n"
            + ("限制原因：\n" + blocked if blocked.strip() else "此节点可以继续预检手工修复后的 working 方案。")
            + "\n\n后端提示：\n" + "\n".join(entry.warnings)
            + "\n\n这里只检查状态、内容指纹和允许的操作，不提供三方合并，也不替你判断修复是否正确。")


def _size(dialog, parent, width, height):
    display = parent.display
    width = min(display.px(width), max(320, dialog.winfo_screenwidth() - 40))
    height = min(display.px(height), max(280, dialog.winfo_screenheight() - 80))
    dialog.geometry(f"{width}x{height}")
    dialog.minsize(min(display.px(620), width), min(display.px(440), height))


def _text(parent, display, *, height=6):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, wrap="none", height=height, relief="solid", bd=1,
                   font=display.font(), padx=display.px(8), pady=display.px(7), **text_options())
    ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
    text.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")
    xbar.grid(row=1, column=0, sticky="ew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    return text


class ConflictInspectDialog(tk.Toplevel):
    def __init__(self, parent, inspections):
        super().__init__(parent)
        self.result = None
        self.inspections = tuple(inspections)
        self.allowed = can_keep_working(self.inspections)
        self.display = parent.display
        self.title("检查选中冲突")
        self.transient(parent)
        _size(self, parent, 900, 700)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"检查 {len(self.inspections)} 个明确选中的冲突节点", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="不会自动解决或提交。先在可信编辑器或 SVN 工具中检查、处理并保存工作文件，再明确选择 working。",
                  wraplength=self.display.px(830)).pack(anchor="w", pady=(8, 10))
        scope = _text(body, self.display, height=5)
        for entry in self.inspections:
            state = "可预检" if can_keep_working((entry,)) else "当前流程不支持"
            scope.insert("end", f"[{state}] {display_path(entry.path)}\n")
        scope.configure(state="disabled")
        details = _text(body, self.display, height=7)
        details.insert("1.0", "\n\n".join(inspection_text(entry) for entry in self.inspections))
        details.configure(state="disabled")
        self.mode_var = tk.StringVar(value="")
        self.ack_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(body, text="保留我已手工修复并保存的当前工作文件（working）", value="working",
                        variable=self.mode_var, state="normal" if self.allowed else "disabled").pack(anchor="w", pady=(12, 5))
        ttk.Checkbutton(body, text="我已核对所有选中工作文件的当前内容；如为文本，已手工处理冲突标记。",
                        variable=self.ack_var, state="normal" if self.allowed else "disabled").pack(anchor="w")
        ttk.Label(body, text="当前仅支持受限的普通文件内容冲突（包括二进制文件），单个文件最多 32 MiB。属性、混合、树冲突，以及 base / mine-full / theirs-full 均不在此流程内。\n解决会移除冲突记录和辅助文件，不证明内容正确。请独立保护工作文件和所有冲突辅助文件。",
                  wraplength=self.display.px(830), style="Muted.TLabel").pack(anchor="w", pady=(10, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="关闭 / 取消", command=self.destroy).pack(side="right")
        self.next_button = ttk.Button(buttons, text="继续预检…", style="Primary.TButton", command=self._accept, state="disabled")
        self.next_button.pack(side="right", padx=8)
        self.mode_var.trace_add("write", self._mode_changed)
        self.ack_var.trace_add("write", self._update_button)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        self.wait_window()

    def _mode_changed(self, *_):
        self.ack_var.set(False)
        self._update_button()

    def _update_button(self, *_):
        ready = self.allowed and self.mode_var.get() == "working" and self.ack_var.get()
        self.next_button.configure(state="normal" if ready else "disabled")

    def _accept(self):
        if not (self.allowed and self.mode_var.get() == "working" and self.ack_var.get()):
            messagebox.showwarning("尚未明确确认", "请选择 working 并确认已经手工修复所有选中工作文件。受限节点不能在本流程中解决。", parent=self)
            return
        self.result = "working"
        self.destroy()


class ConflictConfirmDialog(tk.Toplevel):
    def __init__(self, parent, plan, workspace):
        if plan.accept != "working" or not can_keep_working(plan.inspections):
            raise ValueError("此 GUI 只允许受支持节点的手工 working 方案。")
        super().__init__(parent)
        self.confirmed = False
        self.display = parent.display
        self.title("确认标记手工修复为已解决")
        self.transient(parent)
        _size(self, parent, 820, 570)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="确认执行 working 冲突解决", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"当前工作副本：{workspace}\n只处理下列 {len(plan.paths)} 个确切节点，不递归，不提交。",
                  wraplength=self.display.px(750)).pack(anchor="w", pady=(8, 12))
        scope = _text(body, self.display, height=8)
        scope.insert("1.0", "\n".join(display_path(path) for path in plan.paths))
        scope.configure(state="disabled")
        ttk.Label(body, text="保留当前工作文件，移除 SVN 冲突记录及相关辅助文件。\n辅助文件内容不受预检指纹保护，即便被另行编辑过，也可能被移除。\n本工具不证明内容正确，也不会自动清除文本冲突标记。失败或中断可能已处理部分节点，必须先核查，不能盲目重试。",
                  wraplength=self.display.px(750)).pack(anchor="w", pady=(14, 10))
        ttk.Label(body, text="预检会在执行前复核，但不构成排他锁或事务保证。请选择明确执行，或取消回到工作区。",
                  wraplength=self.display.px(750), style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        self.risk_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="我理解冲突辅助文件会被移除，已另行保护其中的重要内容，且不会并行编辑。",
                        variable=self.risk_var, command=self._update_confirm).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        cancel = ttk.Button(buttons, text="取消", command=self.destroy)
        cancel.pack(side="right")
        self.confirm_button = ttk.Button(buttons, text="确认标记上述节点已解决", command=self._confirm, state="disabled")
        self.confirm_button.pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _confirm(self):
        if not self.risk_var.get():
            messagebox.showwarning("请核对辅助文件风险", "请先确认已保护重要内容，并理解冲突辅助文件将被移除。", parent=self)
            return
        self.confirmed = True
        self.destroy()

    def _update_confirm(self):
        self.confirm_button.configure(state="normal" if self.risk_var.get() else "disabled")
