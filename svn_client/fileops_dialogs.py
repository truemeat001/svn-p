"""Pure presentation dialogs for exact, explicitly confirmed SVN file operations.

No filesystem reads/writes or SVN calls occur here. The caller preflights in a
worker and executes the displayed plan with its state hash only after consent.
"""
from __future__ import annotations

import json
import ntpath
import re
import tkinter as tk
from tkinter import messagebox, ttk

from .theme import text_options


_NAMES = {"delete": "删除", "move": "移动 / 重命名", "copy": "带历史复制"}
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)
_COMMON = (
    "只处理下列明确路径，不自动扩大范围，也不提交仓库。",
    "当前只支持同一实际工作副本根内的普通已版本化文件。目录、外部引用、跨根、冲突及已有目标不支持。",
    "执行前重新预检并校验状态摘要；新状态、目录依赖或范围变化必须重新审查，不能自动扩展。",
    "预检不是事务或跨进程排他锁。请停止并行编辑或其他 SVN 操作；失败或中断可能已部分完成，先核查，不能盲目重试。",
)
_OPERATION_RISK = {
    "delete": "将移除当前本地文件，并登记待提交删除。只允许内容/属性干净的文件。未提交前 SVN revert 通常只能恢复已提交的 BASE 内容，不能恢复未备份的本地编辑；请先独立备份。",
    "move": "源路径将被移除，目标带历史待添加；当前本地内容/属性保留到目标。仍需另行提交，并一起审查移动两端。变更列表标签不保证转移。",
    "copy": "源文件保留，目标使用当前本地内容/属性带历史待添加。仍需另行提交；变更列表标签不保证复制。",
}
_ACK = {
    "delete": "我理解当前本地文件会被移除；已保护重要内容，并核对全部删除路径。",
    "move": "我已核对全部源和目标，理解需一起审查移动两端并另行提交。",
    "copy": "我已核对源与精确目标，理解这是带历史待添加而不是已提交。",
}


def display_path(value):
    """Keep unusual path characters visible instead of letting them spoof rows."""
    return json.dumps(str(value), ensure_ascii=False)


def validate_destination(value, source):
    """String-only validation. The backend must check all actual path state."""
    if (not isinstance(value, str) or not value or not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("请输入明确的工作副本相对目标文件路径，不含控制字符。")
    if value.startswith(("/", "\\")) or ntpath.splitdrive(value)[0] or ":" in value:
        raise ValueError("目标必须是工作副本内的相对文件路径，不能是绝对路径或 URL。")
    components = value.replace("\\", "/").split("/")
    if any(not part or part in {".", ".."} or part.casefold() == ".svn"
           or part.endswith((" ", ".")) or _RESERVED.match(part) for part in components):
        raise ValueError("目标不能包含空组件、.、..、.svn、保留名称或以空格/句点结尾的组件。")
    normalized = "/".join(components)
    if normalized.casefold() == str(source).replace("\\", "/").casefold():
        raise ValueError("源和目标必须不同；暂不支持只改变大小写。")
    return normalized


def _check_plan(plan):
    """Reject malformed presentation scope before showing a consent dialog."""
    if getattr(plan, "operation", None) not in _NAMES:
        raise ValueError("未知文件操作。")
    paths = tuple(getattr(plan, "paths", ()))
    if not paths or any(not isinstance(path, str) or not path for path in paths) or len(set(paths)) != len(paths):
        raise ValueError("预检必须包含明确且不重复的源路径。")
    target = getattr(plan, "destination", None)
    if plan.operation == "delete":
        if target is not None:
            raise ValueError("删除预检不能包含目标路径。")
    elif len(paths) != 1 or not isinstance(target, str) or not target:
        raise ValueError("移动和复制预检必须有一个源及一个明确目标。")
    expected = paths + ((target,) if target is not None else ())
    if tuple(getattr(plan, "affected_paths", ())) != expected:
        raise ValueError("预检受影响路径与源/目标不一致，请重新预检。")
    if (not isinstance(getattr(plan, "workspace", None), str) or not plan.workspace
            or not isinstance(getattr(plan, "state_sha256", None), str)
            or not re.fullmatch(r"[0-9a-f]{64}", plan.state_sha256)):
        raise ValueError("预检缺少工作副本或有效状态摘要。")
    if getattr(plan, "scope", None) != "regular_files_only" or getattr(plan, "commit_required", None) is not True:
        raise ValueError("此对话框只接受受限的本地文件待提交操作。")


def confirmation_text(plan):
    _check_plan(plan)
    lines = [f"操作：{_NAMES[plan.operation]}", f"工作副本：{display_path(plan.workspace)}", "", "全部源路径："]
    lines.extend(f"  {index}. {display_path(path)}" for index, path in enumerate(plan.paths, 1))
    lines.extend(["", "精确目标：" + (display_path(plan.destination) if plan.destination is not None else "无（删除）"),
                  "", "全部受影响路径："])
    lines.extend(f"  {index}. {display_path(path)}" for index, path in enumerate(plan.affected_paths, 1))
    lines.extend(["", "操作后果：", _OPERATION_RISK[plan.operation], "", "执行边界：", *_COMMON,
                  "", "预检原始提示："])
    lines.extend(str(warning) for warning in plan.warnings)
    lines.extend(["", "已审查状态 SHA-256：", plan.state_sha256,
                  "服务器权限、钩子和远端最新状态未由此预检确认。"])
    return "\n".join(lines)


def _size(dialog, display, width, height):
    width = min(display.px(width), max(320, dialog.winfo_screenwidth() - 40))
    height = min(display.px(height), max(280, dialog.winfo_screenheight() - 80))
    dialog.geometry(f"{width}x{height}")
    dialog.minsize(min(display.px(560), width), min(display.px(380), height))


def _readonly_text(parent, display, text, *, height=12):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True, pady=(8, 12))
    widget = tk.Text(frame, wrap="none", height=height, relief="solid", bd=1,
                     font=display.font(), padx=display.px(8), pady=display.px(7), **text_options())
    vertical = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    horizontal = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
    widget.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    widget.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    widget.insert("1.0", text)
    widget.configure(state="disabled")
    return widget


class FileDestinationDialog(tk.Toplevel):
    def __init__(self, parent, operation, source, workspace):
        if operation not in {"move", "copy"}:
            raise ValueError("目标对话框仅用于移动或复制。")
        super().__init__(parent)
        self.result = None
        self.source = source
        self.display = parent.display
        self.title(_NAMES[operation] + "：选择精确目标")
        self.transient(parent)
        _size(self, self.display, 800, 500)
        body = ttk.Frame(self, padding=self.display.px(18))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=_NAMES[operation], style="Heading.TLabel").pack(anchor="w")
        _readonly_text(body, self.display,
                       f"工作副本：{display_path(workspace)}\n源文件：{display_path(source)}\n\n"
                       + _OPERATION_RISK[operation] + "\n\n"
                       + "填写尚不存在的精确相对文件路径，不能填写现有文件夹。父目录必须已存在且已版本化。\n"
                       + "此处仅收集路径，不读取文件或执行操作；下一步后台预检后仍需明确确认。", height=8)
        ttk.Label(body, text="目标文件路径（相对于当前工作副本）").pack(anchor="w")
        self.destination_var = tk.StringVar(value="")
        ttk.Entry(body, textvariable=self.destination_var).pack(fill="x", pady=(5, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        cancel = ttk.Button(buttons, text="取消", command=self.destroy)
        cancel.pack(side="right")
        self.next_button = ttk.Button(buttons, text="继续预检…", command=self._accept, state="disabled")
        self.next_button.pack(side="right", padx=8)
        self.destination_var.trace_add("write", self._update_button)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _update_button(self, *_):
        try:
            validate_destination(self.destination_var.get(), self.source)
        except ValueError:
            ready = False
        else:
            ready = True
        self.next_button.configure(state="normal" if ready else "disabled")

    def _accept(self):
        try:
            target = validate_destination(self.destination_var.get(), self.source)
        except ValueError as exc:
            messagebox.showwarning("请检查目标路径", str(exc), parent=self)
            return
        self.result = target
        self.destroy()


class FileOperationConfirmDialog(tk.Toplevel):
    def __init__(self, parent, plan):
        text = confirmation_text(plan)
        super().__init__(parent)
        self.confirmed = False
        self.operation = plan.operation
        self.display = parent.display
        self.title("确认" + _NAMES[plan.operation])
        self.transient(parent)
        _size(self, self.display, 880, 670)
        body = ttk.Frame(self, padding=self.display.px(18))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="核对精确范围与操作后果", style="Heading.TLabel").pack(anchor="w")
        _readonly_text(body, self.display, text, height=16)
        self.risk_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text=_ACK[plan.operation], variable=self.risk_var,
                        command=self._update_confirm).pack(anchor="w", pady=(0, 8))
        ttk.Label(body, text="预检不构成事务保证。停止并行编辑；失败后先核查状态，不自动重试。",
                  style="Muted.TLabel", wraplength=self.display.px(810)).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        cancel = ttk.Button(buttons, text="取消", command=self.destroy)
        cancel.pack(side="right")
        self.confirm_button = ttk.Button(buttons, text="确认" + _NAMES[plan.operation] + "上述文件",
                                         command=self._confirm, state="disabled")
        self.confirm_button.pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _update_confirm(self):
        self.confirm_button.configure(state="normal" if self.risk_var.get() else "disabled")

    def _confirm(self):
        if not self.risk_var.get():
            messagebox.showwarning("请明确确认操作后果", _ACK[self.operation], parent=self)
            return
        self.confirmed = True
        self.destroy()
