"""Read-only forms for explicit repository file-lock workflows.

No SVN commands or filesystem operations occur in these dialogs.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox, ttk

from .theme import text_options


def _quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def lock_state_text(files):
    """Render only reviewed public fields, never generic object repr/tokens."""
    sections = []
    for item in files:
        owner = _quoted(item.owner) if item.locked else "无"
        state = "本工作副本持有当前锁" if item.owned_by_working_copy else "本工作副本不持有当前锁"
        if item.stale_local_token:
            state += "；存在过期本地令牌，请先在可信 SVN 工具中核查"
        sections.append(f"路径：{_quoted(item.path)}\n服务器锁：{'有锁' if item.locked else '未锁定'}\n"
                        f"所有者：{owner}\n归属：{state}\n"
                        f"工作修订：{item.working_revision}；服务器最后修改：{item.server_last_changed_revision}\n"
                        f"备注：{_quoted(item.comment)}\n创建：{_quoted(item.created)}\n到期：{_quoted(item.expires)}")
    return "\n\n".join(sections)


def can_lock(files):
    return bool(files) and all(not item.locked and not item.stale_local_token
                               and item.working_revision >= item.server_last_changed_revision for item in files)


def can_unlock(files):
    return bool(files) and all(item.locked and item.owned_by_working_copy for item in files)


def _size(dialog, parent, width, height):
    display = parent.display
    width = min(display.px(width), max(320, dialog.winfo_screenwidth() - 40))
    height = min(display.px(height), max(280, dialog.winfo_screenheight() - 80))
    dialog.geometry(f"{width}x{height}")
    dialog.minsize(min(display.px(630), width), min(display.px(450), height))


def _text(parent, display, *, height=8, kind="normal"):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, wrap="none", height=height, relief="flat", bd=0,
                   font=display.font(), padx=display.px(8), pady=display.px(7), **text_options(kind))
    ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
    text.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")
    xbar.grid(row=1, column=0, sticky="ew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    return text


class LockInspectDialog(tk.Toplevel):
    def __init__(self, parent, files, workspace):
        super().__init__(parent)
        self.result = None
        self.files = tuple(files)
        self.display = parent.display
        self.title("所选文件的仓库锁")
        self.transient(parent)
        _size(self, parent, 850, 720)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"已从服务器检查 {len(self.files)} 个文件", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"工作副本：{workspace}\n这是仓库文件锁，不是工作副本管理锁。查询不会加锁或解锁。",
                  wraplength=self.display.px(780)).pack(anchor="w", pady=(8, 10))
        states = _text(body, self.display)
        states.insert("1.0", lock_state_text(self.files))
        states.configure(state="disabled")
        self.mode_var = tk.StringVar(value="")
        ttk.Radiobutton(body, text="申请所选文件的服务器锁（不会抢锁）", value="lock", variable=self.mode_var,
                        state="normal" if can_lock(self.files) else "disabled").pack(anchor="w", pady=(10, 0))
        ttk.Radiobutton(body, text="释放本工作副本持有的所选文件锁（不会破锁）", value="unlock", variable=self.mode_var,
                        state="normal" if can_unlock(self.files) else "disabled").pack(anchor="w")
        ttk.Label(body, text="加锁备注（可选，将发送给仓库服务器，团队可能可见；不要填凭据或秘密）").pack(anchor="w", pady=(8, 4))
        self.comment = _text(body, self.display, height=3, kind="input")
        self.comment.configure(state="disabled")
        ttk.Label(body, text="同一用户名在其他工作副本中取得的锁也不能在这里释放。混合选择不会跳过受限项。\n锁不阻止本地编辑；svn:needs-lock 可能改变所选文件的可写权限。单次最多 128 个文件，查询逐文件访问服务器。",
                  style="Muted.TLabel", wraplength=self.display.px(780)).pack(anchor="w", pady=(9, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        close = ttk.Button(buttons, text="关闭 / 取消", command=self.destroy)
        close.pack(side="right")
        self.next_button = ttk.Button(buttons, text="继续预检…", command=self._accept, state="disabled")
        self.next_button.pack(side="right", padx=8)
        self.mode_var.trace_add("write", self._mode_changed)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        close.focus_set()
        self.wait_window()

    def _mode_changed(self, *_):
        action = self.mode_var.get()
        ready = can_lock(self.files) if action == "lock" else can_unlock(self.files) if action == "unlock" else False
        self.next_button.configure(state="normal" if ready else "disabled")
        self.comment.configure(state="normal" if action == "lock" and ready else "disabled")

    def _accept(self):
        action = self.mode_var.get()
        if action not in {"lock", "unlock"} or not (can_lock(self.files) if action == "lock" else can_unlock(self.files)):
            messagebox.showwarning("请选择可用操作", "请明确选择加锁或解锁。已被锁定、落后或不属于本工作副本的锁不会被强行处理。", parent=self)
            return
        comment = self.comment.get("1.0", "end-1c") if action == "lock" else ""
        try:
            valid = len(comment.encode("utf-8")) <= 4096
        except UnicodeError:
            valid = False
        if not valid or any(ord(char) < 32 and char not in "\n\r\t" for char in comment):
            messagebox.showwarning("备注不可用", "加锁备注最多 4096 字节 UTF-8，不可含不支持的控制字符。", parent=self)
            return
        self.result = action, comment
        self.destroy()


class LockConfirmDialog(tk.Toplevel):
    def __init__(self, parent, plan, workspace):
        if plan.action not in {"lock", "unlock"} or not (can_lock(plan.files) if plan.action == "lock" else can_unlock(plan.files)):
            raise ValueError("仓库锁计划不满足无强制操作的范围。")
        super().__init__(parent)
        self.confirmed = False
        self.display = parent.display
        action = "加锁" if plan.action == "lock" else "解锁"
        self.title("确认仓库文件" + action)
        self.transient(parent)
        _size(self, parent, 850, 660)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"确认{action} {len(plan.paths)} 个确切文件", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"工作副本：{workspace}\n将向仓库服务器发出{action}命令，不提交文件内容。",
                  wraplength=self.display.px(780)).pack(anchor="w", pady=(8, 10))
        details = _text(body, self.display)
        details.insert("1.0", lock_state_text(plan.files))
        if plan.action == "lock":
            details.insert("end", "\n\n将发送的加锁备注：\n" + _quoted(plan.comment))
        details.insert("end", "\n\n预检提示：\n" + "\n".join(plan.warnings))
        details.configure(state="disabled")
        ttk.Label(body, text="加锁会影响他人提交，解锁会释放占用；不会抢锁、破锁或强制处理。\nsvn:needs-lock 可能改变本地文件写权限。状态仍可能变化，多文件操作不是原子事务，失败也可能已部分完成。请勿并行运行其他 SVN 操作。",
                  wraplength=self.display.px(780)).pack(anchor="w", pady=(12, 9))
        self.risk_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="我已核对全部文件、锁归属与备注，理解服务器影响及可能的部分结果。",
                        variable=self.risk_var, command=self._update_confirm).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        cancel = ttk.Button(buttons, text="取消", command=self.destroy)
        cancel.pack(side="right")
        self.confirm_button = ttk.Button(buttons, text="确认" + action + "上述文件", command=self._confirm, state="disabled")
        self.confirm_button.pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _confirm(self):
        if not self.risk_var.get():
            messagebox.showwarning("请先核对影响", "请先明确确认文件、锁归属、备注及操作风险。", parent=self)
            return
        self.confirmed = True
        self.destroy()

    def _update_confirm(self):
        self.confirm_button.configure(state="normal" if self.risk_var.get() else "disabled")
