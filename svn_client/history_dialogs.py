"""Pure controls for one-path, read-only revision comparisons."""
from __future__ import annotations

import json
import ntpath
import os
import tkinter as tk
from tkinter import messagebox, ttk

from .history import normalize_revision
from .theme import text_options


def normalize_scope_path(value):
    """A blank selection never means the root. Preserve literal file names."""
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("请输入一个明确的相对路径。只有显式输入 . 才表示当前工作副本根。")
    if "\\" in value and os.name != "nt":
        raise ValueError("当前平台不转换文件名中的反斜杠，以免读取另一条路径。请使用可信 SVN 客户端检查此名称。")
    value = value.replace("\\", "/") if os.name == "nt" else value
    if ntpath.splitdrive(value)[0] or value.startswith("/") or ":" in value:
        raise ValueError("这里只接受当前工作副本内的相对路径，不接受 URL、盘符或绝对路径。")
    parts = value.split("/")
    if any(part in {"..", ""} or part.casefold() == ".svn" for part in parts):
        raise ValueError("路径不能包含上级目录、空路径段或 SVN 管理目录。")
    parts = [part for part in parts if part != "."]
    return "/".join(parts) if parts else "."


class ComparisonDialog(tk.Toplevel):
    def __init__(self, parent, path, *, from_revision="BASE", to_revision="WORKING"):
        super().__init__(parent)
        self.result = None
        self.display = parent.display
        self.title("比较确切路径的两个版本")
        self.transient(parent)
        width = min(self.display.px(760), max(320, self.winfo_screenwidth() - 40))
        height = min(self.display.px(500), max(280, self.winfo_screenheight() - 80))
        self.geometry(f"{width}x{height}")
        self.minsize(min(self.display.px(590), width), min(self.display.px(420), height))
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="显式选择比较方向", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="只读，不更新、不合并、不写入文件。若所选路径是目录，差异包含其版本化后代。",
                  wraplength=self.display.px(690)).pack(anchor="w", pady=(8, 9))
        scope_frame = ttk.Frame(body)
        scope_frame.pack(fill="both", expand=True)
        scope = tk.Text(scope_frame, height=3, wrap="none", font=self.display.font(),
                        relief="flat", **text_options())
        scroll = ttk.Scrollbar(scope_frame, orient="horizontal", command=scope.xview)
        vertical = ttk.Scrollbar(scope_frame, orient="vertical", command=scope.yview)
        scope.configure(xscrollcommand=scroll.set, yscrollcommand=vertical.set)
        scope.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        scroll.grid(row=1, column=0, sticky="ew")
        scope_frame.columnconfigure(0, weight=1)
        scope_frame.rowconfigure(0, weight=1)
        scope.insert("1.0", "工作副本：" + json.dumps(str(parent.root_path), ensure_ascii=False)
                     + "\n确切相对路径：" + json.dumps(path, ensure_ascii=False))
        scope.configure(state="disabled")
        fields = ttk.Frame(body)
        fields.pack(fill="x", pady=(12, 8))
        self.left_var = tk.StringVar(value=str(from_revision))
        self.right_var = tk.StringVar(value=str(to_revision))
        self.limit_var = tk.StringVar(value="256")
        for row, title, variable, values in ((0, "左侧 / 从", self.left_var, ("BASE", "HEAD")),
                                             (1, "右侧 / 到", self.right_var, ("WORKING", "BASE", "HEAD"))):
            ttk.Label(fields, text=title).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Combobox(fields, textvariable=variable, values=values, width=22).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(fields, text="显示上限 KiB").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Combobox(fields, textvariable=self.limit_var, values=("64", "256", "1024"), state="readonly", width=22).grid(row=2, column=1, sticky="ew", pady=4)
        fields.columnconfigure(1, weight=1)
        ttk.Label(body, text="也可输入非负修订号。BASE 为本项基础版本；HEAD 为仓库最新版本；WORKING 为本地内容，仅可放在右侧。\n可能访问服务器。只显示文本 / 属性差异，不含二进制载荷。超过显示上限会明确标记截断，不可当作完整补丁。",
                  wraplength=self.display.px(690), style="Muted.TLabel").pack(anchor="w", pady=(7, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        cancel = ttk.Button(buttons, text="取消", command=self.destroy)
        cancel.pack(side="right")
        ttk.Button(buttons, text="只读查询差异", command=self._accept).pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def _accept(self):
        try:
            left = normalize_revision(self.left_var.get())
            right = normalize_revision(self.right_var.get(), working=True)
            if self.limit_var.get() not in {"64", "256", "1024"}:
                raise ValueError("显示上限只能为 64、256 或 1024 KiB。")
            limit = int(self.limit_var.get()) * 1024
        except ValueError as error:
            messagebox.showwarning("比较参数不可用", str(error), parent=self)
            return
        self.result = left, right, limit
        self.destroy()
