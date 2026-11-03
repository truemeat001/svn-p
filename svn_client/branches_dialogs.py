"""Explicit whole-workspace branch request and review dialogs; no I/O."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import tkinter as tk
from tkinter import messagebox, ttk

from .branches import (MAX_MERGE_REVISIONS, MAX_PREVIEW_BYTES,
                       validate_branch_revision, validate_branch_url)
from .theme import text_options


@dataclass(frozen=True)
class BranchContext:
    workspace: str
    url: str
    revision: int
    repository_uuid: str
    repository_root: str


@dataclass(frozen=True)
class BranchRequest:
    operation: str
    url: str
    revision: int
    start_revision: int | None = None
    destination_url: str | None = None
    kind: str | None = None
    message: str = ""


def quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def make_request(operation, url, revision, *, start_revision=None, destination_url=None, kind=None, message=""):
    if operation not in {"switch", "merge", "copy_plan"}:
        raise ValueError("不支持此分支操作。")
    url = validate_branch_url(url)
    revision = validate_branch_revision(revision)
    if operation == "merge":
        start_revision = validate_branch_revision(start_revision)
        if not 0 <= start_revision < revision or revision - start_revision > MAX_MERGE_REVISIONS:
            raise ValueError("合并必须是明确的向前范围：0 ≤ 起始 < 结束，每次最多 64 个修订。")
    elif start_revision is not None:
        raise ValueError("此操作不接受合并起始修订。")
    if operation == "copy_plan":
        destination_url = validate_branch_url(destination_url)
        if kind not in {"branch", "tag"}:
            raise ValueError("请明确选择 branch 或 tag。")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("请填写拟提交说明；它只进入只读计划，不执行提交。")
        try:
            encoded = message.encode("utf-8")
        except UnicodeError:
            raise ValueError("说明必须是有效 Unicode 文本。") from None
        if len(encoded) > 8192 or any(ord(c) < 32 and c not in "\n\r\t" for c in message):
            raise ValueError("说明最多 8192 UTF-8 字节，不接受不支持的控制字符。")
    elif destination_url is not None or kind is not None or message:
        raise ValueError("切换 / 合并不接受复制目标或提交说明。")
    return BranchRequest(operation, url, revision, start_revision, destination_url, kind, message)


def same_workspace(first, second):
    return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(os.path.normpath(str(second)))


def validate_plan(request, plan, context):
    """Do not show a clickable action for a different or incomplete plan."""
    if not isinstance(plan.state_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", plan.state_sha256):
        raise ValueError("预检缺少有效状态摘要，未提供执行入口。")
    if (plan.source_url != request.url or plan.source_revision != request.revision
            or isinstance(plan.source_revision, bool) or plan.repository_uuid != context.repository_uuid):
        raise ValueError("预检源 URL、固定修订或仓库身份与请求不一致。")
    if request.operation == "copy_plan":
        validate_branch_revision(plan.destination_checked_revision)
        if (plan.kind != request.kind or plan.destination_url != request.destination_url
                or plan.message != request.message or plan.execution_supported is not False
                or plan.would_commit_immediately is not True or plan.local_changes_included is not False):
            raise ValueError("只读复制计划范围不一致或意外宣称可以执行。")
        return
    validate_branch_revision(plan.working_revision)
    if plan.start_revision is not None:
        validate_branch_revision(plan.start_revision)
    if (plan.operation != request.operation or not same_workspace(plan.workspace, context.workspace)
            or plan.scope != "whole_working_copy_root" or plan.commit_performed is not False
            or plan.target_url != context.url or plan.working_revision != context.revision
            or plan.repository_root != context.repository_root or plan.start_revision != request.start_revision
            or plan.svn_dry_run is not (request.operation == "merge")
            or not isinstance(plan.preview_has_conflicts, bool)):
        raise ValueError("工作副本、当前映射、修订、范围或预演状态已改变，请刷新后重新开始。")
    if not isinstance(plan.preview_text, str) or len(plan.preview_text.encode("utf-8", errors="replace")) > MAX_PREVIEW_BYTES:
        raise ValueError("预检输出超过此 GUI 的完整核对上限，未提供执行入口。")
    if request.operation == "switch" and (plan.preview_text or plan.preview_has_conflicts):
        raise ValueError("切换没有 SVN dry-run，拒绝把其他输出当作切换预演。")


def plan_text(request, plan, context):
    validate_plan(request, plan, context)
    common = ["整个工作副本根：" + quoted(context.workspace), "当前映射：" + quoted(context.url),
              f"当前统一基础修订：r{context.revision}", "仓库 UUID：" + quoted(context.repository_uuid)]
    if request.operation == "copy_plan":
        lines = ["只读分支 / 标签计划：不会创建，不存在执行按钮。", *common,
                 "类型：" + request.kind, "已提交源目录：" + quoted(plan.source_url),
                 f"固定源修订：r{plan.source_revision}", "拟建目标：" + quoted(plan.destination_url),
                 f"目标不存在的检查时刻：r{plan.destination_checked_revision}",
                 "拟提交说明（未提交）：" + quoted(plan.message),
                 "URL → URL 复制本会立即提交服务器。由于精确目标竞争风险，本版只显示计划。",
                 "不包含本地未提交内容；标签仍是普通 SVN 目录，不能保证不可修改。"]
    else:
        lines = ["操作范围：整个工作副本根，不是选中的文件、分组或目录子集。", *common]
        if request.operation == "switch":
            lines += ["将切换到：" + quoted(plan.source_url), f"固定目标修订：r{plan.source_revision}",
                      "没有 SVN dry-run。这里只验证元数据、当前干净状态和目标树，不能预先证明祖先关系、最终成功或没有冲突。",
                      "执行会替换整个工作副本的 URL 映射并更改本地文件。不提交仓库。"]
        else:
            lines += ["合并来源：" + quoted(plan.source_url),
                      f"源定位：URL@{plan.source_revision}；向前范围：({plan.start_revision}, {plan.source_revision}]",
                      "即起始修订之后、结束修订为止的改动；mergeinfo 可能排除已合并部分。",
                      "已执行 SVN merge --dry-run。即使预演无冲突，也不能保证实际合并完整或没有冲突。",
                      "实际结果仍是本地修改，包含可能的 svn:mergeinfo。必须另行审查、测试和明确提交。",
                      "预演检测到冲突，当前 GUI 不提供执行入口。" if plan.preview_has_conflicts else "预演未标出冲突，不代表最终无冲突。",
                      "", "完整 SVN dry-run 输出：", plan.preview_text or "（SVN dry-run 没有返回文本，不代表所有修订已应用。）"]
    lines += ["", "完整预检提示：", *plan.warnings, "", "已核对状态 SHA-256：", plan.state_sha256,
              "", "快照不是预约、跨进程锁或事务。执行前会再次检查，但其他程序仍可能修改工作副本。失败或中断可能留下部分结果，不会自动回滚或重试。"]
    return "\n".join(lines)


def _size(dialog, parent, width, height):
    width = min(parent.display.px(width), max(320, dialog.winfo_screenwidth() - 40))
    height = min(parent.display.px(height), max(280, dialog.winfo_screenheight() - 80))
    dialog.geometry(f"{width}x{height}")
    dialog.minsize(min(parent.display.px(650), width), min(parent.display.px(440), height))


def _text(parent, display, *, height=8, editable=False):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, wrap="none", height=height, relief="flat", bd=0,
                   font=display.font(), padx=display.px(8), pady=display.px(7),
                   **text_options("input" if editable else "normal"))
    ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
    text.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")
    xbar.grid(row=1, column=0, sticky="ew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    return text


class BranchRequestDialog(tk.Toplevel):
    def __init__(self, parent, operation, context):
        if operation not in {"switch", "merge", "copy_plan"}:
            raise ValueError("不支持此分支操作。")
        super().__init__(parent)
        self.result = None
        self.operation = operation
        self.display = parent.display
        title = {"switch": "切换整个工作副本", "merge": "合并明确的向前范围", "copy_plan": "分支 / 标签计划（只读）"}[operation]
        self.title(title)
        self.transient(parent)
        _size(self, parent, 900, 690 if operation == "copy_plan" else 610)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="Heading.TLabel").pack(anchor="w")
        summary = _text(body, self.display, height=4)
        summary.insert("1.0", "整个工作副本根：" + quoted(context.workspace) + "\n最近读取的映射：" + quoted(context.url)
                       + f"\n基础修订：r{context.revision}；下面会重新预检。\n不是选中文件或变更列表的操作；分支 / 标签计划不会创建服务器目录。")
        summary.configure(state="disabled")
        fields = ttk.Frame(body)
        fields.pack(fill="x", pady=(12, 7))
        self.url_var = tk.StringVar(value="")
        self.revision_var = tk.StringVar(value="")
        self.start_var = tk.StringVar(value="")
        self.destination_var = tk.StringVar(value="")
        self.kind_var = tk.StringVar(value="")
        items = [("目标目录 URL" if operation == "switch" else "源目录 URL", self.url_var),
                 ("固定结束修订" if operation == "merge" else "固定数字修订", self.revision_var)]
        if operation == "merge":
            items.append(("起始修订（不包含）", self.start_var))
        if operation == "copy_plan":
            items.append(("拟建目录 URL（不会创建）", self.destination_var))
        for row, (label, variable) in enumerate(items):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(fields, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        fields.columnconfigure(1, weight=1)
        if operation == "copy_plan":
            ttk.Label(fields, text="计划类型").grid(row=len(items), column=0, sticky="w", pady=4)
            ttk.Combobox(fields, textvariable=self.kind_var, values=("branch", "tag"), state="readonly").grid(row=len(items), column=1, sticky="ew", pady=4)
            ttk.Label(body, text="拟提交说明：只进入计划，不执行提交；不要填写凭据或秘密。").pack(anchor="w", pady=(5, 4))
            self.message = _text(body, self.display, height=3, editable=True)
        ttk.Label(body, text="只支持明确数字修订，不接受 HEAD / BASE；URL 中 @ 按字面目录名处理。仅同一仓库，拒绝 URL 凭据、svn+ssh、外部引用、稀疏/混合修订、链接及未版本化/忽略文件。\n需要整个副本完全干净。首版最多 10,000 项、256 MiB；合并最多 64 个修订，预检可能逐修订访问服务器，耗时较长。不会自动清理、更新或还原来通过预检。",
                  wraplength=self.display.px(820), style="Muted.TLabel").pack(anchor="w", pady=(10, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        cancel = ttk.Button(buttons, text="取消", command=self.destroy)
        cancel.pack(side="right")
        ttk.Button(buttons, text="只读预检…", command=self._accept).pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _accept(self):
        try:
            options = {}
            if self.operation == "merge":
                options["start_revision"] = self.start_var.get()
            if self.operation == "copy_plan":
                options.update(destination_url=self.destination_var.get(), kind=self.kind_var.get(),
                               message=self.message.get("1.0", "end-1c"))
            request = make_request(self.operation, self.url_var.get(), self.revision_var.get(), **options)
        except (ValueError, TypeError) as error:
            messagebox.showwarning("分支参数不可用", str(error), parent=self)
            return
        self.result = request
        self.destroy()


class BranchPlanDialog(tk.Toplevel):
    def __init__(self, parent, request, plan, context):
        text = plan_text(request, plan, context)  # Refuse malformed plans before opening.
        super().__init__(parent)
        self.confirmed = False
        self.can_apply = request.operation in {"switch", "merge"} and not plan.preview_has_conflicts
        self.display = parent.display
        self.title("只读分支 / 标签计划" if request.operation == "copy_plan" else "核对整个工作副本操作")
        self.transient(parent)
        _size(self, parent, 940, 720)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="只读计划，不会创建分支或标签" if request.operation == "copy_plan" else "核对完整范围与风险",
                  style="Heading.TLabel").pack(anchor="w", pady=(0, 10))
        display = _text(body, self.display, height=16)
        display.insert("1.0", text)
        display.configure(state="disabled")
        self.scope_var = tk.BooleanVar(value=False)
        self.safety_var = tk.BooleanVar(value=False)
        if request.operation != "copy_plan":
            ttk.Checkbutton(body, text="我已核对整个副本根、当前映射、目标 URL 与数字修订 / 范围；不是选中文件的操作。",
                            variable=self.scope_var, command=self._update_button, state="normal" if self.can_apply else "disabled").pack(anchor="w", pady=(12, 5))
            ttk.Checkbutton(body, text="我已独立保护重要数据，不并行编辑 / 编译 / 运行 SVN，理解失败可能部分完成且不会自动回滚。",
                            variable=self.safety_var, command=self._update_button, state="normal" if self.can_apply else "disabled").pack(anchor="w", pady=(0, 10))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        cancel = ttk.Button(buttons, text="关闭 / 取消", command=self.destroy)
        cancel.pack(side="right")
        if request.operation != "copy_plan":
            action = "切换整个工作副本" if request.operation == "switch" else "应用本地合并（不提交）"
            self.apply_button = ttk.Button(buttons, text=action, command=self._confirm, style="Danger.TButton", state="disabled")
            self.apply_button.pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _update_button(self):
        if self.can_apply:
            self.apply_button.configure(state="normal" if self.scope_var.get() and self.safety_var.get() else "disabled")

    def _confirm(self):
        if not self.can_apply or not self.scope_var.get() or not self.safety_var.get():
            messagebox.showwarning("尚未明确确认", "必须核对完整范围并确认风险；只读计划与预测冲突的合并不可执行。", parent=self)
            return
        self.confirmed = True
        self.destroy()
