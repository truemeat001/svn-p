"""SVN Desk: a native, dependency-free desktop interface for Subversion.

All SVN and filesystem work runs off the Tk thread.  The interface never
submits changes, reverts files, or restores a shelf without an explicit dialog.
"""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import queue
import stat
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote

from .backend import SvnClient, discover_svn
from .display import configure_display, enable_high_dpi
from .shelves import ShelfManager
from .workspaces import (load_settings, normalize_settings, remember_workspace,
                         remove_workspace, rename_workspace, save_settings,
                         startup_workspace, validate_name)


APP_NAME = "SVN Desk"
BG = "#f3f5f8"
NAV = "#18283d"
TEXT = "#26364b"
MUTED = "#718096"
BLUE = "#2468cc"
STATUS_LABELS = {
    "modified": "已修改", "added": "已添加", "deleted": "待删除",
    "unversioned": "未版本化", "missing": "本地缺失", "conflicted": "冲突",
    "replaced": "已替换", "normal": "正常", "ignored": "已忽略",
    "external": "外部引用", "obstructed": "路径阻塞", "incomplete": "不完整",
}


def has_local_changes(entry):
    """An assigned, unchanged file is a list member, not a pending change."""
    return (entry.item not in {"normal", "none", "ignored", "external"}
            or entry.props not in {"normal", "none"}
            or entry.tree_conflicted)


def grouped_entries(entries, query=""):
    """Keep native membership separate from the filtered, visible file rows."""
    groups = {None: []}
    for entry in entries:
        name = getattr(entry, "changelist", None)
        if name is not None or has_local_changes(entry):
            groups.setdefault(name, []).append(entry)
    needle = query.casefold().strip()
    result = []
    names = [None, *sorted((name for name in groups if name is not None), key=str.casefold)]
    for name in names:
        members = sorted(groups[name], key=lambda entry: entry.path.casefold())
        group_matches = needle in (name or "未分组 默认").casefold()
        visible = [entry for entry in members if not needle or group_matches
                   or needle in entry.path.casefold() or needle in status_label(entry).casefold()]
        if visible or not needle:
            result.append((name, members, visible))
    return result


def read_groupable_status(client, root):
    """Read on a worker: include clean labels without showing all clean files."""
    statuses = client.status(verbose=True)
    pending = [entry for entry in statuses if has_local_changes(entry) or getattr(entry, "changelist", None) is not None]
    candidates = []
    for entry in statuses:
        if entry.item in {"unversioned", "ignored", "external", "missing", "obstructed", "incomplete"}:
            continue
        if getattr(entry, "file_external", False) or getattr(entry, "switched", False):
            continue
        path = root / entry.path
        if path.is_dir():
            continue
        try:
            node = path.lstat()
            if stat.S_ISLNK(node.st_mode) or getattr(node, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                continue
        except FileNotFoundError:
            pass
        if entry.item == "deleted":
            # Deleted directories have no local inode. SVN still knows their kind.
            if client.info(entry.path).get("kind") != "file":
                continue
        candidates.append(entry)
    return pending, candidates


def fit_window(window, display, width, height, min_width, min_height):
    """Scale layout once, but keep windows usable on smaller displays."""
    width = min(display.px(width), max(320, window.winfo_screenwidth() - 40))
    height = min(display.px(height), max(280, window.winfo_screenheight() - 80))
    window.geometry(f"{width}x{height}")
    window.minsize(min(display.px(min_width), width), min(display.px(min_height), height))


def status_label(entry):
    """Render a status without losing property-only changes or conflicts."""
    if getattr(entry, "tree_conflicted", False):
        return "树冲突"
    if entry.props == "conflicted":
        return "属性冲突"
    if entry.item == "normal" and entry.props == "modified":
        return "属性修改"
    return STATUS_LABELS.get(entry.item, entry.item)


def read_preview(root: Path, relative: str, limit=512_000):
    """Read a local text file for display, never following a path outside WC."""
    root = root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return "此路径指向工作副本之外，未读取。"
    if target.is_dir():
        return "这是一个文件夹。选择文件以查看内容或差异。"
    if not target.exists():
        return "本地文件不存在。请查看文件状态。"
    if not stat.S_ISREG(target.stat().st_mode):
        return "此路径不是普通文件，无法预览。"
    with target.open("rb") as handle:
        data = handle.read(limit + 1)
    if b"\x00" in data[:8192]:
        return "二进制文件，无法显示文本预览。"
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        text += "\n\n[预览已截断，仅显示前 500 KB]"
    return text


def local_files(root: Path, limit=3000):
    result = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        safe_dirs = []
        for name in sorted(dirs):
            path = Path(folder) / name
            try:
                node = path.lstat()
                reparse = getattr(node, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if name == ".svn" or stat.S_ISLNK(node.st_mode) or reparse or (path / ".svn").exists():
                    continue
                safe_dirs.append(name)
            except OSError:
                continue
        dirs[:] = safe_dirs
        for name in sorted(files):
            path = Path(folder) / name
            try:
                size = path.stat().st_size if not path.is_symlink() else 0
            except OSError:
                size = None
            result.append((path.relative_to(root).as_posix(), size))
            if len(result) >= limit:
                return result, True
    return result, False


def directory_entries(root: Path, directory: Path):
    """List one directory only; never traverse links or other working copies."""
    root = root.resolve()
    directory = Path(os.path.abspath(directory))
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise ValueError("所选目录不在当前工作副本内。") from error
    current = root
    for part in relative.parts:
        if part.casefold() == ".svn":
            raise ValueError("SVN 管理目录不属于文件浏览范围。")
        current /= part
        node = current.lstat()
        if stat.S_ISLNK(node.st_mode) or getattr(node, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("工作副本导航不展开符号链接或重解析点。")
        if (current / ".svn").exists():
            raise ValueError("此目录属于独立工作副本，请单独打开。")
    directory = directory.resolve()
    rows = []
    for path in directory.iterdir():
        if path.name.casefold() == ".svn":
            continue
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                continue
            kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else None
            if kind is None or kind == "dir" and (path / ".svn").exists():
                continue
            rows.append((path.relative_to(root).as_posix(), kind, info.st_size if kind == "file" else None))
        except OSError:
            continue
    return sorted(rows, key=lambda row: (row[1] != "dir", Path(row[0]).name.casefold()))


class CommitDialog(tk.Toplevel):
    def __init__(self, parent, paths, *, changelist=None, statuses=None):
        super().__init__(parent)
        self.display = parent.display
        self.result = None
        self.title("提交到 SVN 仓库")
        self.transient(parent)
        fit_window(self, self.display, 650, 480, 520, 390)
        self.configure(bg=BG)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        heading = f"提交变更列表：{changelist}" if changelist is not None else f"提交 {len(paths)} 个选中路径"
        ttk.Label(body, text=heading, style="Heading.TLabel").pack(anchor="w")
        description = ("包含该列表全部成员（包括被筛选隐藏的文件），只提交其中的修改。成功提交的文件默认移出列表；空列表自动消失。"
                       if changelist is not None else "此操作会将以下路径的更改发送到远程仓库。目录会包含其下的更改。")
        ttk.Label(body, text=description,
                  wraplength=self.display.px(590)).pack(anchor="w", pady=(7, 12))
        listing = tk.Text(body, height=6, relief="flat", font=self.display.mono_font(), bg="#e9eef5")
        listing.pack(fill="x")
        listing.insert("1.0", "\n".join(f"[{statuses[path]}] {path}" if statuses else path for path in paths))
        listing.configure(state="disabled")
        ttk.Label(body, text="提交说明（必填）").pack(anchor="w", pady=(16, 6))
        self.message = tk.Text(body, height=7, wrap="word", relief="solid", bd=1,
                               font=self.display.font(), undo=True)
        self.message.pack(fill="both", expand=True)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确认提交", style="Primary.TButton", command=self._accept).pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.message.focus_set()
        self.wait_window()

    def _accept(self):
        value = self.message.get("1.0", "end").strip()
        if not value:
            messagebox.showwarning("需要提交说明", "请填写本次提交的说明。", parent=self)
            return
        self.result = value
        self.destroy()


class ChangelistDialog(tk.Toplevel):
    """Name a native list and explicitly choose its members in one dialog."""
    def __init__(self, parent, entries, *, selected=(), existing=(), move=False):
        super().__init__(parent)
        self.display = parent.display
        self.result = None
        self.entries = {f"candidate{index}": entry for index, entry in enumerate(entries)}
        self.existing = set(existing)
        self.title("移动到变更列表" if move else "新建 SVN 变更列表")
        self.transient(parent)
        fit_window(self, self.display, 760, 540, 620, 440)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="移动选中文件" if move else "新建变更列表", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="这是本机工作副本中的文件分组，不会提交、复制或还原文件。每个文件只能属于一个列表。",
                  wraplength=self.display.px(700)).pack(anchor="w", pady=(8, 12))
        name_row = ttk.Frame(body)
        name_row.pack(fill="x")
        ttk.Label(name_row, text="列表名称").pack(side="left", padx=(0, 10))
        self.name_var = tk.StringVar()
        name_input = ttk.Combobox(name_row, textvariable=self.name_var, values=list(existing)) if move else ttk.Entry(name_row, textvariable=self.name_var)
        name_input.pack(side="left", fill="x", expand=True)
        ttk.Label(body, text="选择要归组的已版本化文件（Ctrl / Shift 多选）：").pack(anchor="w", pady=(15, 6))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        self.files = ttk.Treeview(frame, columns=("path", "status", "group"), show="headings", selectmode="extended")
        for key, title, width in [("path", "文件路径", 380), ("status", "状态", 90), ("group", "当前列表", 160)]:
            self.files.heading(key, text=title, anchor="w")
            self.files.column(key, width=self.display.px(width), minwidth=self.display.px(70))
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self.files.yview)
        self.files.configure(yscrollcommand=ybar.set)
        self.files.pack(side="left", fill="both", expand=True)
        ybar.pack(side="left", fill="y")
        selected = set(selected)
        for iid, entry in self.entries.items():
            group = getattr(entry, "changelist", None)
            self.files.insert("", "end", iid=iid, values=(entry.path, status_label(entry), group if group is not None else "未分组"))
            if entry.path in selected:
                self.files.selection_add(iid)
        self.count_var = tk.StringVar()
        self.files.bind("<<TreeviewSelect>>", lambda event: self.count_var.set(f"已选择 {len(self.files.selection())} 个文件"))
        self.count_var.set(f"已选择 {len(self.files.selection())} 个文件")
        ttk.Label(body, textvariable=self.count_var, style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(body, text="SVN 不保存空列表；至少选择一个文件。同名即加入已有列表。未版本化文件请先“添加”，目录不能归组。",
                  wraplength=self.display.px(700), style="Muted.TLabel").pack(anchor="w", pady=(6, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确认移动" if move else "创建并归组", style="Primary.TButton", command=self._accept).pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        name_input.focus_set()
        self.wait_window()

    def _accept(self):
        raw_name = self.name_var.get()
        name = raw_name if raw_name in self.existing else raw_name.strip()
        paths = [self.entries[iid].path for iid in self.files.selection()]
        if not name or not paths:
            messagebox.showwarning("需要名称和文件", "请填写列表名称，并至少选择一个已版本化文件。SVN 不保存空变更列表。", parent=self)
            return
        self.result = name, paths
        self.destroy()


class WrapToolbar(ttk.Frame):
    """Keep every action reachable when high-DPI layouts become narrower."""
    def __init__(self, parent, **options):
        super().__init__(parent, **options)
        self.buttons = []
        self.rows = []
        self.last_width = 0
        self.bind("<Configure>", self._flow)

    def add_button(self, button):
        self.buttons.append(button)
        self.last_width = 0
        self.after_idle(self._flow)

    def _flow(self, event=None):
        width = self.winfo_width()
        if width < 2 or width == self.last_width:
            return
        self.last_width = width
        for button in self.buttons:
            button.pack_forget()
        for frame in self.rows:
            frame.pack_forget()
        row, column, used = 0, 0, 0
        for button in self.buttons:
            requested = button.winfo_reqwidth() + 7
            if column and used + requested > width:
                row, column, used = row + 1, 0, 0
            if row >= len(self.rows):
                self.rows.append(ttk.Frame(self))
            if column == 0:
                self.rows[row].pack(fill="x")
            # Independent rows avoid grid's shared-column width expansion.
            button.pack(in_=self.rows[row], side="left", padx=(0, 7), pady=(0, 5))
            button.lift()
            column += 1
            used += requested


class WorkspaceManagerDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        self.display = parent.display
        self.title("本地工作区管理")
        self.transient(parent)
        fit_window(self, self.display, 850, 480, 650, 380)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="已登记的 SVN 工作副本", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="一个工作区对应一个独立工作副本。这里保存名称和路径，不创建 P4V 式服务器工作区。",
                  wraplength=self.display.px(780)).pack(anchor="w", pady=(8, 12))
        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("name", "path", "state"), show="headings", selectmode="browse")
        for key, title, width in (("name", "名称", 170), ("path", "本地目录", 470), ("state", "状态", 120)):
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=self.display.px(width), minwidth=80)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        for record in parent.settings.get("workspaces", []):
            state = "当前打开" if record["id"] == parent._active_workspace_id else "上次打开" if record["id"] == parent.settings.get("last_workspace_id") else "已登记"
            if record["id"] in parent._workspace_errors:
                state = "上次打开失败"
            self.tree.insert("", "end", iid=record["id"], values=(record["name"], record["path"], state))
            if record["id"] == parent._active_workspace_id:
                self.tree.selection_set(record["id"])
        self.restore_var = tk.BooleanVar(value=parent.settings.get("restore_last_workspace", True))
        ttk.Checkbutton(body, text="启动时自动恢复上次成功打开的工作区", variable=self.restore_var).pack(anchor="w", pady=(10, 8))
        buttons = WrapToolbar(body)
        buttons.pack(fill="x")
        for label, action in (("打开 / 切换", "open"), ("重命名", "rename"), ("重新定位目录…", "relink"),
                              ("移除记录…", "remove"), ("添加已有副本…", "add"), ("新建 / 检出…", "checkout"),
                              ("扫描父目录…", "discover"), ("关闭", "close")):
            button = ttk.Button(buttons, text=label, command=lambda a=action: self._accept(a))
            buttons.add_button(button)
        ttk.Label(body, text="移除记录不会删除文件；路径失效时可重新定位。多个副本分别打开和提交，不合并修改范围。",
                  wraplength=self.display.px(780), style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.tree.bind("<Double-1>", lambda event: self._accept("open"))
        self.bind("<Escape>", lambda event: self._accept("close"))
        self.protocol("WM_DELETE_WINDOW", lambda: self._accept("close"))
        self.grab_set()
        self.wait_window()

    def _accept(self, action):
        selection = self.tree.selection()
        if action in {"open", "rename", "relink", "remove"} and not selection:
            messagebox.showinfo("选择工作区", "请先选择一条工作区记录。", parent=self)
            return
        self.result = (action, selection[0] if selection else None, self.restore_var.get())
        self.destroy()


class CheckoutDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        self.display = parent.display
        self.title("新建本地 SVN 工作区")
        self.transient(parent)
        fit_window(self, self.display, 720, 370, 600, 340)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="从仓库检出到新的本地目录", style="Heading.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(body, text="这会下载一个 SVN 工作副本，并保存其名称和本地路径。不是创建服务器上的仓库或工作区。",
                  wraplength=self.display.px(660)).pack(anchor="w", pady=(0, 12))
        self.name_var, self.url_var, self.path_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for label, variable in (("工作区名称", self.name_var), ("仓库 URL", self.url_var), ("本地目录", self.path_var)):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, width=12).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
            if variable is self.path_var:
                ttk.Button(row, text="浏览…", command=self._choose_path).pack(side="left", padx=(6, 0))
        ttk.Label(body, text="目录必须为空或尚不存在，不能位于已有工作副本内。支持 http/https/svn/file，不支持内置 svn+ssh 检出。\nURL 不得含密码、查询参数或片段。@修订按 SVN 语法生效；字面 @ 路径需在末尾再加 @。\n使用已配置的认证，不下载 externals；失败后保留目标目录中的文件。",
                  wraplength=self.display.px(660), style="Muted.TLabel").pack(anchor="w", pady=(12, 15))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="检查并继续…", style="Primary.TButton", command=self._accept).pack(side="right", padx=8)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        self.wait_window()

    def _choose_path(self):
        folder = filedialog.askdirectory(title="选择空目录，或在输入框填写一个新目录", parent=self)
        if folder:
            self.path_var.set(folder)

    def _accept(self):
        try:
            name = validate_name(self.name_var.get())
        except ValueError as error:
            messagebox.showwarning("名称无效", str(error), parent=self)
            return
        url, path = self.url_var.get().strip(), self.path_var.get().strip()
        if not url or not path:
            messagebox.showwarning("信息不完整", "请填写仓库 URL 和本地目录。", parent=self)
            return
        self.result = name, url, path
        self.destroy()


class WorkspaceDiscoveryDialog(tk.Toplevel):
    def __init__(self, parent, discovery):
        super().__init__(parent)
        self.result = None
        self.display = parent.display
        self.title("父目录中的工作副本候选")
        self.transient(parent)
        fit_window(self, self.display, 900, 510, 670, 400)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"找到 {len(discovery.working_copies)} 个候选工作副本", style="Heading.TLabel").pack(anchor="w")
        bounds = "扫描范围内已完成" if discovery.complete else "扫描未覆盖全部目录，结果仅为已发现部分"
        ttk.Label(body, text=f"{bounds} · 扫描 {discovery.directories_scanned} 个目录 · 跳过 {discovery.skipped_links} 个链接\n父目录：{discovery.parent}",
                  wraplength=self.display.px(830)).pack(anchor="w", pady=(8, 10))
        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("path", "relation"), show="headings", selectmode="extended")
        self.tree.heading("path", text="候选路径（还需 SVN 验证）", anchor="w")
        self.tree.heading("relation", text="目录关系", anchor="w")
        self.tree.column("path", width=self.display.px(620), minwidth=280)
        self.tree.column("relation", width=self.display.px(200), minwidth=140)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.candidates = {}
        for index, candidate in enumerate(discovery.working_copies):
            iid = f"candidate{index}"
            self.candidates[iid] = candidate.path
            self.tree.insert("", "end", iid=iid, values=(candidate.path, "嵌套候选，来源未判定" if candidate.relation == "nested" else "独立根候选"))
        notes = "每个候选先单独验证再登记。嵌套关系不代表已确认 svn:externals；不会扫描链接或进入 .svn，也不会批量更新或跨副本提交。"
        if discovery.reasons:
            notes += "\n扫描边界：" + "、".join(discovery.reasons)
        if discovery.issues:
            notes += f"\n有 {len(discovery.issues)} 条访问或扫描提示，详见操作记录。"
        ttk.Label(body, text=notes, wraplength=self.display.px(830), style="Muted.TLabel").pack(anchor="w", pady=(10, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        for label, action in (("关闭", "close"), ("验证并登记选中项", "register"), ("打开一个选中项", "open")):
            ttk.Button(buttons, text=label, command=lambda a=action: self._accept(a)).pack(side="right", padx=5)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        self.wait_window()

    def _accept(self, action):
        paths = [self.candidates[iid] for iid in self.tree.selection()]
        if action != "close" and (not paths or action == "open" and len(paths) != 1):
            messagebox.showinfo("选择工作副本", "打开时请选择一个候选；登记时可选择多个。各副本的操作范围始终独立。", parent=self)
            return
        self.result = action, paths
        self.destroy()


class SvnDesk(tk.Tk):
    def __init__(self, working_copy=None, *, demo=False):
        enable_high_dpi()
        super().__init__()
        self.display = configure_display(self)
        self.title(APP_NAME + " · Subversion 工作台")
        fit_window(self, self.display, 1320, 850, 1030, 680)
        self.configure(bg=BG)
        self.demo = demo
        self.client = None
        self.shelf_manager = None
        self.root_path = None
        self.info = {}
        self.entries = []
        self.changelist_candidates = []
        self.logs = []
        self.shelves = []
        self.all_files = []
        self._repo_entries = {}
        self._repo_url = ""
        self._busy = False
        self._closing = False
        self._filter_job = None
        self._queue = queue.Queue()
        self._tree_queue = queue.Queue()
        self._tree_generation = 0
        self._workspace_nodes = {}
        self._workspace_cache = {}
        self._workspace_pending = set()
        self._workspace_more = {}
        self._repository_nodes = {}
        self._folder_rows = None
        self._folder_offset = 0
        self._folder_scope = None
        self._preview_request = 0
        self._controls = []
        self._action_buttons = {}
        self._change_lookup = {}
        self._group_lookup = {}
        self._file_lookup = {}
        self._file_kind = {}
        self._log_lookup = {}
        self._shelf_lookup = {}
        self._settings_file = Path.home() / ".svn-desk" / "settings.json"
        self._settings_warning = None
        self._active_workspace_id = None
        self._workspace_errors = {}
        self._manual_open_requested = False
        self.settings = normalize_settings({}) if demo else self._read_settings()
        self._make_style()
        self._make_menu()
        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_queue)
        self._set_busy(False)
        self._update_workspace_choices()
        if demo:
            self.after(50, self._load_demo)
        elif working_copy:
            self.after(50, lambda: self.open_workspace(working_copy))
        else:
            self.after(50, self._restore_saved_workspace)
        if self._settings_warning:
            self._append_log("本地设置未能读取：" + self._settings_warning)

    def _read_settings(self):
        try:
            return load_settings(self._settings_file)
        except (OSError, ValueError) as error:
            self._settings_warning = str(error)
            return normalize_settings({})

    def _save_settings(self):
        if self.demo:
            return False
        if self._settings_warning and not messagebox.askyesno("重建本地设置？", "原设置文件无法读取。是否用当前工作区登记重建它？\n\n这会替换本地设置文件，不会删除或移动任何工作副本。选择“否”会保留原文件，但本次设置不会持久保存。", default="no", parent=self):
            return False
        try:
            save_settings(self._settings_file, self.settings)
            self._settings_warning = None
            return True
        except (OSError, ValueError) as error:
            self._append_log(f"无法保存本地设置：{error}")
            messagebox.showwarning("工作区设置未能保存", "当前会话可以继续，但下次启动可能无法恢复这些更改。\n\n" + str(error), parent=self)
            return False

    def _make_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        p = self.display.px
        style.configure(".", font=self.display.font(), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("White.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Heading.TLabel", font=self.display.font(15, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TButton", padding=(p(12), p(7)), borderwidth=0, background="#e7ecf3")
        style.map("TButton", background=[("active", "#dbe4f1"), ("disabled", "#edf0f4")],
                  foreground=[("disabled", "#9aa4b1")])
        style.configure("Primary.TButton", foreground="white", background=BLUE)
        style.map("Primary.TButton", background=[("active", "#1d57ae"), ("disabled", "#a8bce0")], foreground=[("disabled", "#f4f6fa")])
        style.configure("Treeview", background="white", fieldbackground="white", foreground=TEXT,
                        rowheight=p(30), borderwidth=0, font=self.display.font())
        style.configure("Treeview.Heading", background="#eaf0f6", foreground="#53647b", padding=(p(8), p(9)), relief="flat")
        style.map("Treeview", background=[("selected", "#e0ecff")], foreground=[("selected", "#174d94")])
        style.configure("TNotebook", tabmargins=(0, 5, 0, 0), borderwidth=0)
        style.configure("TNotebook.Tab", padding=(p(16), p(9)), background="#e9edf4")
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("Horizontal.TProgressbar", troughcolor="#e6ebf2", background=BLUE, borderwidth=0)

    def _make_menu(self):
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="打开工作副本…", accelerator="Ctrl+O", command=self.choose_workspace)
        file_menu.add_command(label="工作区管理…", command=self.manage_workspaces)
        file_menu.add_command(label="新建工作区 / 检出…", command=self.create_workspace)
        file_menu.add_command(label="扫描含多个副本的父目录…", command=self.discover_workspaces)
        file_menu.add_command(label="设置 SVN 程序…", command=self.configure_svn)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._close)
        menu.add_cascade(label="文件", menu=file_menu)
        action_menu = tk.Menu(menu, tearoff=False)
        for label, callback in [("刷新", self.refresh), ("更新工作副本…", self.update_workspace),
                                ("提交选中项…", self.commit_selected), ("添加到版本控制…", self.add_selected),
                                ("还原选中项…", self.revert_selected), ("新建本地 Shelf…", self.create_shelf)]:
            action_menu.add_command(label=label, command=callback)
        menu.add_cascade(label="操作", menu=action_menu)
        changelist_menu = tk.Menu(menu, tearoff=False)
        for label, callback in [("新建变更列表…", self.new_changelist), ("移动选中文件到列表…", self.move_to_changelist),
                                ("移回未分组…", self.remove_from_changelist), ("提交所选变更列表…", self.commit_changelist)]:
            changelist_menu.add_command(label=label, command=callback)
        menu.add_cascade(label="变更列表", menu=changelist_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="使用说明", command=self.show_help)
        help_menu.add_command(label="关于 SVN Desk", command=lambda: messagebox.showinfo(APP_NAME, "SVN Desk 0.1\n\nP4V 风格的独立 Subversion 桌面工作台。\n不隶属于 Perforce 或 Apache Subversion。", parent=self))
        menu.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menu)

    def _button(self, parent, title, callback, *, primary=False, always=False, action=None):
        button = ttk.Button(parent, text=title, command=callback,
                            style="Primary.TButton" if primary else "TButton")
        if isinstance(parent, WrapToolbar):
            parent.add_button(button)
        else:
            button.pack(side="left", padx=(0, 7))
        self._controls.append((button, always))
        if action:
            self._action_buttons[action] = button
        return button

    def _build_ui(self):
        self.main_split = ttk.Panedwindow(self, orient="horizontal")
        self.main_split.pack(fill="both", expand=True)
        sidebar_width = max(180, min(self.display.px(270), self.winfo_screenwidth() // 4))
        sidebar = ttk.Frame(self.main_split, width=sidebar_width, padding=(10, 12))
        sidebar.pack_propagate(False)
        self.main_split.add(sidebar, weight=0)
        ttk.Label(sidebar, text="SVN Desk", font=self.display.font(16, "bold")).pack(anchor="w", pady=(0, 9))
        self.workspace_name = ttk.Label(sidebar, text="尚未打开工作副本", wraplength=sidebar_width - 25,
                                       font=self.display.font(10, "bold"))
        self.workspace_name.pack(fill="x", pady=(0, 10))
        self.nav_buttons = []
        navigator = ttk.Notebook(sidebar)
        navigator.pack(fill="both", expand=True)
        workspace_page, repo_page = ttk.Frame(navigator), ttk.Frame(navigator)
        navigator.add(workspace_page, text="工作副本")
        navigator.add(repo_page, text="仓库")
        self.workspace_tree = self._navigation_tree(workspace_page)
        self.workspace_tree.bind("<<TreeviewOpen>>", self._workspace_open)
        self.workspace_tree.bind("<<TreeviewSelect>>", self._workspace_selected)
        self.workspace_tree.bind("<Double-1>", self._workspace_activated)
        self.workspace_tree.bind("<Return>", lambda event: self._activate_key(self._workspace_activated))
        self.workspace_tree.bind("<Button-3>", self._workspace_menu)
        self.repository_tree = self._navigation_tree(repo_page)
        self.repository_tree.bind("<Double-1>", self._repository_tree_open)
        self.repository_tree.bind("<Return>", lambda event: self._activate_key(self._repository_tree_open))
        self.repository_tree.bind("<<TreeviewOpen>>", self._repository_tree_open)
        ttk.Label(sidebar, text="目录按需加载 · 双击文件查看内容\n工作副本内的链接和独立副本不展开",
                  style="Muted.TLabel", wraplength=sidebar_width - 25).pack(anchor="w", pady=(10, 0))
        content = ttk.Frame(self.main_split, padding=(14, 16, 14, 10))
        self.main_split.add(content, weight=1)
        heading = ttk.Frame(content)
        heading.pack(fill="x")
        self.title_label = ttk.Label(heading, text="待提交更改", style="Heading.TLabel")
        self.title_label.pack(side="left")
        ttk.Label(heading, text="原生 SVN  ·  本地优先", style="Muted.TLabel").pack(side="right")
        workspace_row = ttk.Frame(content)
        workspace_row.pack(fill="x", pady=(12, 0))
        ttk.Label(workspace_row, text="工作区").pack(side="left", padx=(0, 9))
        self.workspace_choice_var = tk.StringVar()
        self.workspace_choice = ttk.Combobox(workspace_row, textvariable=self.workspace_choice_var, state="readonly")
        self.workspace_choice.pack(side="left", fill="x", expand=True)
        self.workspace_choice.bind("<<ComboboxSelected>>", self._switch_workspace_choice)
        manage_button = ttk.Button(workspace_row, text="管理…", command=self.manage_workspaces)
        manage_button.pack(side="left", padx=(8, 0))
        self._controls.append((manage_button, True))
        create_button = ttk.Button(workspace_row, text="新建 / 检出…", command=self.create_workspace)
        create_button.pack(side="left", padx=(8, 0))
        self._controls.append((create_button, True))
        location = ttk.Frame(content)
        location.pack(fill="x", pady=(15, 12))
        self.path_var = tk.StringVar()
        ttk.Label(location, text="本地目录").pack(side="left", padx=(0, 9))
        self.path_entry = ttk.Entry(location, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.path_entry.bind("<Return>", lambda event: self.open_workspace(self.path_var.get()))
        open_button = ttk.Button(location, text="打开…", command=self.choose_workspace)
        open_button.pack(side="left", padx=(8, 0))
        self._controls.append((open_button, True))
        toolbar = WrapToolbar(content)
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "刷新", self.refresh)
        self._button(toolbar, "更新…", self.update_workspace)
        self._button(toolbar, "提交选中项…", self.commit_selected, primary=True, action="commit_files")
        self._button(toolbar, "查看差异", self.preview_changes, action="diff")
        self._button(toolbar, "添加…", self.add_selected, action="add")
        self._button(toolbar, "新建 Shelf…", self.create_shelf, action="shelf")
        self.summary_var = tk.StringVar(value="打开工作副本后，即可查看本地修改。")
        ttk.Label(content, textvariable=self.summary_var, style="Muted.TLabel").pack(anchor="w", pady=(1, 12))
        split = ttk.Panedwindow(content, orient="vertical")
        split.pack(fill="both", expand=True)
        upper = ttk.Frame(split)
        lower = ttk.Frame(split)
        split.add(upper, weight=3)
        split.add(lower, weight=2)
        self.tabs = ttk.Notebook(upper)
        self.tabs.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.pages = []
        for label in ("待提交更改", "工作副本文件", "提交历史", "仓库浏览", "本地 Shelves"):
            frame = ttk.Frame(self.tabs)
            self.tabs.add(frame, text=label)
            self.pages.append(frame)
        list_actions = WrapToolbar(self.pages[0], padding=(10, 9, 10, 3))
        list_actions.pack(fill="x")
        self._button(list_actions, "+ 新建变更列表…", self.new_changelist, primary=True, action="new_list")
        self._button(list_actions, "移动到列表…", self.move_to_changelist, action="move")
        self._button(list_actions, "移回未分组…", self.remove_from_changelist, action="unassign")
        self._button(list_actions, "提交所选列表…", self.commit_changelist, action="commit_group")
        pending_header = ttk.Frame(self.pages[0], padding=(10, 6, 10, 8))
        pending_header.pack(fill="x")
        ttk.Label(pending_header, text="筛选").pack(side="left", padx=(0, 8))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", self._schedule_filter)
        ttk.Entry(pending_header, textvariable=self.filter_var, width=25).pack(side="left")
        ttk.Label(pending_header, text="本地文件分组 · 空列表自动消失 · 不保存文件内容", style="Muted.TLabel").pack(side="right")
        self.changes = self._tree(self.pages[0], [("status", "状态", 95), ("path", "文件路径", 310),
                                                ("props", "属性", 65), ("revision", "基础修订", 80)], tree_column="变更列表 / 文件")
        self.changes.bind("<Double-1>", self._pending_activated)
        self.changes.bind("<Return>", lambda event: self._activate_key(self._pending_activated))
        self.changes.bind("<<TreeviewSelect>>", self._selection_hint)
        self.changes.bind("<Button-3>", self._pending_menu)
        self.changes.tag_configure("group", background="#edf3fb", foreground="#245386")
        self.changes.tag_configure("conflict", foreground="#bb3a35")
        self.changes.tag_configure("added", foreground="#26714c")
        self.changes.tag_configure("missing", foreground="#a66b17")
        file_header = ttk.Frame(self.pages[1], padding=(10, 9))
        file_header.pack(fill="x")
        self.files_note = ttk.Label(file_header, text="工作副本中的本地文件（隐藏 .svn）", style="Muted.TLabel")
        self.files_note.pack(side="left")
        ttk.Button(file_header, text="上一页", command=lambda: self._folder_page(-1)).pack(side="right", padx=3)
        ttk.Button(file_header, text="下一页", command=lambda: self._folder_page(1)).pack(side="right", padx=3)
        self.files = self._tree(self.pages[1], [("path", "文件路径", 470), ("status", "SVN 状态", 110), ("size", "大小", 90)])
        self.files.bind("<Double-1>", lambda event: self.preview_file())
        self.files.bind("<Return>", lambda event: self._activate_key(self.preview_file))
        self.files.bind("<<TreeviewSelect>>", self._file_selection_details)
        self.history = self._tree(self.pages[2], [("revision", "修订", 70), ("author", "作者", 110),
                                                ("date", "日期", 160), ("message", "提交说明", 360)])
        self.history.bind("<<TreeviewSelect>>", self.preview_history)
        self.history.bind("<Double-1>", lambda event: self.preview_revision_diff())
        repo_header = ttk.Frame(self.pages[3], padding=(10, 9))
        repo_header.pack(fill="x")
        ttk.Button(repo_header, text="上一级", command=self.repository_up).pack(side="left", padx=(0, 8))
        self.repo_var = tk.StringVar(value="选择“仓库浏览”加载当前仓库目录。")
        ttk.Label(repo_header, textvariable=self.repo_var, style="Muted.TLabel", wraplength=700).pack(side="left", fill="x", expand=True)
        self.repository = self._tree(self.pages[3], [("name", "名称", 340), ("kind", "类型", 65),
                                                    ("revision", "修订", 70), ("author", "作者", 100), ("size", "大小", 80)])
        self.repository.bind("<Double-1>", self.repository_open)
        self.repository.bind("<Return>", lambda event: self._activate_key(self.repository_open))
        self.repository.bind("<<TreeviewSelect>>", self._repository_selection_details)
        shelf_header = ttk.Frame(self.pages[4], padding=(10, 9))
        shelf_header.pack(fill="x")
        self._button(shelf_header, "恢复选中 Shelf…", self.restore_shelf)
        self._button(shelf_header, "删除 Shelf…", self.delete_shelf)
        ttk.Label(shelf_header, text="仅保存在本机；不会自动还原工作区", style="Muted.TLabel").pack(side="right")
        self.shelf_tree = self._tree(self.pages[4], [("name", "Shelf 名称", 320), ("date", "创建时间", 190),
                                                  ("files", "路径数", 65), ("id", "标识", 130)])
        self.shelf_tree.bind("<<TreeviewSelect>>", self.preview_shelf)
        self.details_tabs = ttk.Notebook(lower)
        self.details_tabs.pack(fill="both", expand=True, pady=(10, 0))
        preview_frame = ttk.Frame(self.details_tabs)
        log_frame = ttk.Frame(self.details_tabs)
        self.details_tabs.add(preview_frame, text="差异 / 详情")
        self.details_tabs.add(log_frame, text="操作记录")
        self.preview_title = ttk.Label(preview_frame, text="选择文件查看差异", style="Muted.TLabel", padding=(10, 8))
        self.preview_title.pack(fill="x")
        self.preview = self._text_area(preview_frame)
        self.preview.tag_configure("add", foreground="#20704a", background="#effaf3")
        self.preview.tag_configure("remove", foreground="#b3423c", background="#fff1ef")
        self.preview.tag_configure("header", foreground="#2766b6", background="#edf4fd")
        self.console = self._text_area(log_frame, background="#1d2b3e", foreground="#c7d7ec")
        footer = ttk.Frame(content)
        footer.pack(fill="x", pady=(11, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=130)
        self.progress.pack(side="right")

    def _navigation_tree(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, show="tree", selectmode="browse")
        tree.column("#0", width=self.display.px(240), minwidth=self.display.px(100))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    @staticmethod
    def _activate_key(callback):
        callback()
        return "break"

    def _reset_workspace_tree(self):
        self._tree_generation += 1
        self._workspace_nodes.clear()
        self._workspace_cache.clear()
        self._workspace_pending.clear()
        self._workspace_more.clear()
        self._folder_rows, self._folder_scope, self._folder_offset = None, None, 0
        self.workspace_tree.delete(*self.workspace_tree.get_children())
        self.workspace_tree.insert("", "end", iid="workspace-root", text=self.root_path.name, open=True)
        self._workspace_nodes["workspace-root"] = (".", "dir")
        self.workspace_tree.insert("workspace-root", "end", text="展开读取目录…")
        self._start_directory_load("workspace-root")
        self.repository_tree.delete(*self.repository_tree.get_children())
        self._repository_nodes.clear()
        url = self.info.get("url")
        if url:
            node = self.repository_tree.insert("", "end", text="当前仓库目录", open=False)
            self._repository_nodes[node] = (url, "dir")
            self.repository_tree.insert(node, "end", text="展开读取仓库…")

    def _demo_directory_rows(self, relative):
        prefix = "" if relative == "." else relative.rstrip("/") + "/"
        rows = {}
        for path, size in self.all_files:
            if not path.startswith(prefix):
                continue
            tail = path[len(prefix):]
            first, separator, _ = tail.partition("/")
            if first:
                rows[prefix + first] = (prefix + first, "dir" if separator else "file", None if separator else size)
        return sorted(rows.values(), key=lambda row: (row[1] != "dir", row[0].casefold()))

    def _start_directory_load(self, node):
        if node not in self._workspace_nodes or node in self._workspace_pending:
            return
        relative, kind = self._workspace_nodes[node]
        if kind != "dir":
            return
        if node in self._workspace_cache:
            self._show_workspace_folder(node)
            return
        self._workspace_pending.add(node)
        generation, root = self._tree_generation, self.root_path
        if self.demo:
            self._tree_queue.put((generation, node, self._demo_directory_rows(relative), None))
            return
        def worker():
            try:
                self._tree_queue.put((generation, node, directory_entries(root, root / relative), None))
            except Exception as error:
                self._tree_queue.put((generation, node, [], error))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_tree_queue(self):
        for _ in range(3):
            try:
                generation, node, rows, error = self._tree_queue.get_nowait()
            except queue.Empty:
                return
            if generation != self._tree_generation or not self.workspace_tree.exists(node):
                continue
            self._workspace_pending.discard(node)
            self._purge_workspace_children(node)
            if error:
                self.workspace_tree.insert(node, "end", text="读取失败，右键重试")
                self._append_log(f"目录读取失败：{error}")
                continue
            self._workspace_cache[node] = rows
            self._append_workspace_page(node, 0)
            if self.workspace_tree.selection() == (node,) or self._folder_scope is None:
                self._show_workspace_folder(node)

    def _purge_workspace_children(self, node):
        children = self.workspace_tree.get_children(node)
        stack = list(children)
        while stack:
            child = stack.pop()
            stack.extend(self.workspace_tree.get_children(child))
            self._workspace_nodes.pop(child, None)
            self._workspace_cache.pop(child, None)
            self._workspace_pending.discard(child)
            self._workspace_more.pop(child, None)
        self.workspace_tree.delete(*children)

    def _clear_workspace_selection(self):
        """A matching path in another working copy is still a new target."""
        self._preview_request += 1
        self.changes.selection_remove(*self.changes.selection())
        self.files.selection_remove(*self.files.selection())
        self._change_lookup.clear()
        self._group_lookup.clear()
        self._file_lookup.clear()
        self._file_kind.clear()

    def _append_workspace_page(self, parent, offset):
        rows = self._workspace_cache[parent]
        existing = {self._workspace_nodes[iid][0] for iid in self.workspace_tree.get_children(parent)
                    if iid in self._workspace_nodes}
        for relative, kind, size in rows[offset:offset + 200]:
            if relative in existing:
                continue
            node = self.workspace_tree.insert(parent, "end", text=Path(relative).name + ("/" if kind == "dir" else ""))
            self._workspace_nodes[node] = (relative, kind)
            if kind == "dir":
                self.workspace_tree.insert(node, "end", text="展开读取目录…")
        if offset + 200 < len(rows):
            more = self.workspace_tree.insert(parent, "end", text=f"加载更多…（{offset + 200}/{len(rows)}）")
            self._workspace_more[more] = (parent, offset + 200)

    def _show_workspace_folder(self, node):
        if node not in self._workspace_cache:
            return
        self._folder_scope = self._workspace_nodes[node][0]
        self._folder_rows = self._workspace_cache[node]
        self._folder_offset = 0
        self._render_files()

    def _workspace_open(self, event=None):
        node = self.workspace_tree.focus()
        if node in self._workspace_nodes and self._workspace_nodes[node][1] == "dir":
            self._start_directory_load(node)

    def _workspace_selected(self, event=None):
        selection = self.workspace_tree.selection()
        if not selection:
            return
        node = selection[0]
        if node not in self._workspace_nodes:
            return
        relative, kind = self._workspace_nodes[node]
        self._preview_request += 1
        if kind == "dir":
            self._start_directory_load(node)
            self.select_tab(1)
            self._show_preview(relative, "已选择工作副本目录。右侧文件页只显示该目录的直接子项。\n\n目录选择仅用于导航，不会自动选择文件或触发递归提交。")
        else:
            self._show_preview(relative, f"工作副本文件：{relative}\n\n双击可读取本地内容。目录树选择不会自动加入提交范围。")

    def _workspace_activated(self, event=None):
        selection = self.workspace_tree.selection()
        if not selection:
            return
        node = selection[0]
        if node in self._workspace_more:
            parent, offset = self._workspace_more.pop(node)
            self.workspace_tree.delete(node)
            self._append_workspace_page(parent, offset)
        elif node in self._workspace_nodes:
            relative, kind = self._workspace_nodes[node]
            if kind == "dir":
                self._start_directory_load(node)
            else:
                self._preview_local_path(relative)

    def _workspace_menu(self, event):
        node = self.workspace_tree.identify_row(event.y)
        if node not in self._workspace_nodes:
            return
        self.workspace_tree.selection_set(node)
        relative, kind = self._workspace_nodes[node]
        menu = tk.Menu(self, tearoff=False)
        if kind == "dir":
            menu.add_command(label="查看目录内容", command=lambda: self._start_directory_load(node))
            menu.add_command(label="刷新此目录", command=lambda: self._refresh_workspace_directory(node))
        else:
            menu.add_command(label="查看本地文件内容", command=lambda: self._preview_local_path(relative))
            menu.add_command(label="在待提交列表中定位", command=lambda: self._locate_pending_file(relative))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _refresh_workspace_directory(self, node):
        if node in self._workspace_pending:
            return
        self._workspace_cache.pop(node, None)
        self._start_directory_load(node)

    def _locate_pending_file(self, relative):
        matches = [iid for iid, entry in self._change_lookup.items() if entry.path == relative]
        self.changes.selection_remove(*self.changes.selection())
        self.select_tab(0)
        if matches:
            self.changes.selection_set(matches[0])
            self.changes.see(matches[0])
        else:
            self.status_var.set("此文件没有待处理更改、未归组，或被当前筛选隐藏。")

    def _repository_tree_open(self, event=None):
        selection = self.repository_tree.selection()
        node = self.repository_tree.focus() or (selection[0] if selection else "")
        if node not in self._repository_nodes:
            return
        url, kind = self._repository_nodes[node]
        if kind == "dir":
            self.select_tab(3)
            self.load_repository(url)
        else:
            self._show_preview(url, "仓库文件为只读导航对象。请在工作副本中修改文件。")

    def _folder_page(self, delta):
        if self._folder_rows is None:
            return
        limit = max(0, (len(self._folder_rows) - 1) // 200 * 200)
        self._folder_offset = max(0, min(limit, self._folder_offset + delta * 200))
        self._render_files()

    def _navigate_workspace_directory(self, relative):
        node = next((iid for iid, data in self._workspace_nodes.items()
                     if data == (relative, "dir") and self.workspace_tree.exists(iid)), None)
        if node is None:
            parent_path = Path(relative).parent.as_posix()
            parent = next((iid for iid, data in self._workspace_nodes.items()
                           if data == (parent_path, "dir") and self.workspace_tree.exists(iid)), "workspace-root")
            node = self.workspace_tree.insert(parent, "end", text=Path(relative).name + "/")
            self._workspace_nodes[node] = (relative, "dir")
            self.workspace_tree.insert(node, "end", text="展开读取目录…")
        self.workspace_tree.selection_set(node)
        self.workspace_tree.focus(node)
        self.workspace_tree.see(node)
        self._start_directory_load(node)

    def _tree(self, parent, columns, *, tree_column=None):
        frame = ttk.Frame(parent, style="White.TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="tree headings" if tree_column else "headings", selectmode="extended")
        if tree_column:
            tree.heading("#0", text=tree_column, anchor="w")
            tree.column("#0", width=self.display.px(245), minwidth=self.display.px(150), stretch=False)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        for name, label, width in columns:
            tree.heading(name, text=label, anchor="w")
            tree.column(name, width=self.display.px(width), minwidth=self.display.px(60), anchor="w", stretch=name in {"path", "message", "name"})
        return tree

    def _text_area(self, parent, background="white", foreground=TEXT):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="none", height=10, bg=background, fg=foreground, relief="flat", bd=0,
                       padx=12, pady=9, font=self.display.mono_font(), insertbackground=foreground, state="disabled")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        return text

    def _bind_shortcuts(self):
        self.bind("<Control-o>", lambda event: self.choose_workspace())
        self.bind("<F5>", lambda event: self.refresh())
        self.bind("<Control-d>", lambda event: self.preview_changes())

    def _set_busy(self, busy, label=""):
        self._busy = busy
        ready = self.client is not None or self.demo
        for button, always in self._controls:
            button.configure(state="normal" if not busy and (always or ready) else "disabled")
        self.path_entry.configure(state="disabled" if busy else "normal")
        self.workspace_choice.configure(state="disabled" if busy else "readonly")
        if busy:
            self.status_var.set(label + "…")
            self.progress.start(12)
        else:
            self.status_var.set("演示模式 · 示例数据，不连接仓库" if self.demo else "就绪")
            self.progress.stop()
        self._update_action_states()

    def _action_available(self, action):
        if self._busy or not (self.client or self.demo):
            return False
        if action == "new_list":
            return bool(self.changelist_candidates)
        if self.tabs.index(self.tabs.select()) != 0:
            return False
        selected = self.changes.selection()
        files = [self._change_lookup[i] for i in selected if i in self._change_lookup]
        groups = [self._group_lookup[i] for i in selected if i in self._group_lookup]
        names = set(groups) | {getattr(entry, "changelist", None) for entry in files}
        if action == "commit_group":
            return len(names) == 1 and None not in names and any(
                getattr(entry, "changelist", None) in names and has_local_changes(entry) for entry in self.entries)
        if not files or groups:
            return False
        if action in {"diff", "shelf"}:
            return True
        if action == "add":
            return all(entry.item == "unversioned" for entry in files)
        if action == "move":
            eligible = {entry.path for entry in self.changelist_candidates}
            return all(entry.path in eligible for entry in files)
        if action == "unassign":
            return any(getattr(entry, "changelist", None) is not None for entry in files)
        if action == "commit_files":
            return any(has_local_changes(entry) for entry in files) and all(
                entry.item not in {"unversioned", "missing", "conflicted", "obstructed", "external", "ignored", "incomplete"}
                and not entry.tree_conflicted and entry.props != "conflicted" for entry in files)
        return False

    def _update_action_states(self):
        for action, button in self._action_buttons.items():
            button.configure(state="normal" if self._action_available(action) else "disabled")

    def _run(self, label, work, done=None, *, mutation=False, on_error=None):
        if self._busy or self._closing:
            self.bell()
            return
        self._set_busy(True, label)
        self._append_log("› " + label)
        def worker():
            try:
                self._queue.put((True, work(), done, label, mutation, on_error))
            except Exception as error:
                self._queue.put((False, error, done, label, mutation, on_error))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        if self._closing:
            return
        try:
            ok, result, done, label, mutation, on_error = self._queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._set_busy(False)
            if ok:
                self._append_log("✓ " + label)
                if done:
                    try:
                        done(result)
                    except Exception as error:
                        self._append_log("显示结果失败：" + str(error))
                        messagebox.showerror("无法显示结果", str(error), parent=self)
            else:
                self._append_log("失败：" + str(result))
                self.details_tabs.select(1)
                if self.root_path:
                    self.path_var.set(str(self.root_path))
                note = "\n\n操作可能已部分执行。将重新读取本地状态，请确认结果后再操作。" if mutation else ""
                if on_error:
                    on_error(result)
                else:
                    messagebox.showerror(label + "失败", str(result) + note, parent=self)
                if mutation and self.client:
                    self.refresh()
        self._drain_tree_queue()
        self.after(90, self._drain_queue)

    def _append_log(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", str(text).rstrip() + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _show_preview(self, title, content, *, diff=False):
        self._preview_request += 1
        self.preview_title.configure(text=title)
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        for line in str(content).splitlines(keepends=True):
            tag = ""
            if diff:
                if line.startswith(("+++", "---", "@@", "Index:", "===")):
                    tag = "header"
                elif line.startswith("+"):
                    tag = "add"
                elif line.startswith("-"):
                    tag = "remove"
            self.preview.insert("end", line, tag)
        self.preview.configure(state="disabled")
        self.details_tabs.select(0)

    def choose_workspace(self):
        if self._busy:
            return
        self._manual_open_requested = True
        folder = filedialog.askdirectory(title="选择 SVN 工作副本", initialdir=str(self.root_path or self.settings.get("last_workspace", Path.home())), parent=self)
        if folder:
            self.open_workspace(folder)

    def _restore_saved_workspace(self):
        if self.demo or self._busy or self.client or self._manual_open_requested:
            return
        record = startup_workspace(self.settings)
        if record:
            self.open_workspace(record["path"], record_id=record["id"], auto_restore=True)
        else:
            warning = "\n\n本地设置读取失败，原文件未修改；请通过工作区管理重新打开副本。" if self._settings_warning else ""
            self._show_preview("欢迎使用 SVN Desk", "打开一个已有工作副本，或点击“新建 / 检出”从仓库下载。\n\n工作副本会登记名称和路径，下次启动自动恢复上次成功打开的工作区。\n\n一个父目录包含多个副本时，请在“工作区管理”中扫描父目录；各副本始终分别打开、更新和提交。\n\n本地工作区登记不是 P4V 服务器工作区，设置中不保存仓库 URL 或认证信息。" + warning)

    def _update_workspace_choices(self):
        records = sorted(self.settings.get("workspaces", []), key=lambda record: (record["name"].casefold(), record["path"]))
        self._workspace_choice_ids = [record["id"] for record in records]
        self.workspace_choice.configure(values=[f"{record['name']}  ·  {record['path']}" for record in records])
        if self._active_workspace_id in self._workspace_choice_ids:
            self.workspace_choice.current(self._workspace_choice_ids.index(self._active_workspace_id))
            active = next(record for record in records if record["id"] == self._active_workspace_id)
            self.workspace_name.configure(text=active["name"])
        else:
            self.workspace_choice_var.set("当前副本未登记" if self.client else "选择已登记工作区…")

    def _switch_workspace_choice(self, event=None):
        if self._busy:
            return
        index = self.workspace_choice.current()
        if 0 <= index < len(self._workspace_choice_ids):
            identifier = self._workspace_choice_ids[index]
            record = next((item for item in self.settings.get("workspaces", []) if item["id"] == identifier), None)
            if record:
                self.open_workspace(record["path"], record_id=identifier)

    def manage_workspaces(self):
        if self._busy:
            return
        self._manual_open_requested = True
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不读取实际副本，也不会修改工作区登记或本地设置。", parent=self)
            return
        dialog = WorkspaceManagerDialog(self)
        if not dialog.result:
            return
        action, identifier, restore = dialog.result
        if self.settings.get("restore_last_workspace", True) != restore:
            self.settings["restore_last_workspace"] = restore
            self._save_settings()
        record = next((item for item in self.settings.get("workspaces", []) if item["id"] == identifier), None)
        if action == "open" and record:
            self.open_workspace(record["path"], record_id=identifier)
        elif action == "rename" and record:
            name = simpledialog.askstring("重命名工作区", "只更改登记名称，不移动目录：", initialvalue=record["name"], parent=self)
            if name is not None:
                try:
                    rename_workspace(self.settings, identifier, name)
                except ValueError as error:
                    messagebox.showwarning("名称无效", str(error), parent=self)
                else:
                    self._save_settings()
                    self._update_workspace_choices()
        elif action == "relink" and record:
            folder = filedialog.askdirectory(title="重新定位已有 SVN 工作副本（不移动文件）", parent=self)
            if folder:
                self.open_workspace(folder, record_id=identifier)
        elif action == "remove" and record:
            if messagebox.askokcancel("只移除工作区记录", f"移除“{record['name']}”的本地登记？\n\n不会删除、移动或还原目录中的任何文件。当前打开的副本可继续使用。", parent=self):
                remove_workspace(self.settings, identifier)
                self._workspace_errors.pop(identifier, None)
                if identifier == self._active_workspace_id:
                    self._active_workspace_id = None
                self._save_settings()
                self._update_workspace_choices()
        elif action == "add":
            self.choose_workspace()
        elif action == "checkout":
            self.create_workspace()
        elif action == "discover":
            self.discover_workspaces()

    def create_workspace(self):
        if self._busy:
            return
        self._manual_open_requested = True
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不会检出文件或保存工作区。", parent=self)
            return
        dialog = CheckoutDialog(self)
        if not dialog.result:
            return
        name, url, destination = dialog.result
        executable = self.settings.get("svn_executable") or None
        def preflight():
            from .backend import validate_checkout_destination, validate_checkout_url
            return validate_checkout_url(url), validate_checkout_destination(destination)
        def checked(data):
            safe_url, path = data
            if not messagebox.askokcancel("确认检出新的工作副本", f"名称：{name}\n仓库：{safe_url}\n本地目录：{path}\n\n按所填 URL 检出；未指定 @修订时使用 HEAD。URL 尾部 @修订按 SVN 语法生效。\n不下载 externals，不保存认证信息。失败可能留下部分工作副本，不会自动删除。是否继续？", parent=self):
                return
            def checkout():
                from .backend import checkout_working_copy
                return checkout_working_copy(safe_url, path, executable=executable)
            def complete(result):
                if result.stdout:
                    self._append_log(result.stdout)
                self.open_workspace(path, workspace_name=name)
            def failed(error):
                messagebox.showerror("检出未完成", "检出结果可能不完整，目标目录中的文件已保留。请先检查结果，不要盲目重试或删除。\n\n" + str(error), parent=self)
                self._show_preview("检出未完成", f"目标目录：{path}\n\n当前工作区未被替换，新目录没有登记为成功的工作区。\n请使用外部 SVN 客户端检查或恢复此目录后，再选择“打开已有副本”。")
            self._run("检出新的工作副本", checkout, complete, on_error=failed)
        self._run("检查检出路径", preflight, checked)

    def discover_workspaces(self, folder=None):
        if self._busy:
            return
        self._manual_open_requested = True
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不扫描实际目录，也不会保存工作区。", parent=self)
            return
        folder = folder or filedialog.askdirectory(title="选择包含多个 SVN 工作副本的父目录", parent=self)
        if not folder:
            return
        def scan():
            from .discovery import discover_working_copies
            return discover_working_copies(folder)
        def found(result):
            for issue in result.issues:
                self._append_log(f"扫描提示 [{issue.code}] {issue.path}: {issue.message}")
            dialog = WorkspaceDiscoveryDialog(self, result)
            if not dialog.result:
                return
            action, paths = dialog.result
            if action == "open":
                self.open_workspace(paths[0])
            elif action == "register":
                self._register_discovered_paths(paths)
        self._run("扫描工作副本候选（有范围限制）", scan, found)

    def _register_discovered_paths(self, paths):
        executable = self.settings.get("svn_executable") or None
        def verify():
            verified, errors = [], []
            program = discover_svn(executable)
            for supplied in paths:
                try:
                    client = SvnClient(supplied, executable=program)
                    info = client.info()
                    root = str(info.get("wc_root") or "")
                    if not root or os.path.normcase(os.path.abspath(root)) != os.path.normcase(os.path.abspath(supplied)):
                        raise ValueError("候选路径不是 SVN 报告的独立工作副本根。")
                    verified.append(root)
                except Exception as error:
                    errors.append((str(supplied), str(error)))
            return verified, errors
        def registered(result):
            verified, errors = result
            new_settings = copy.deepcopy(self.settings)
            for path in verified:
                remember_workspace(new_settings, path, set_last=False)
            if verified:
                self.settings = new_settings
                saved = self._save_settings()
                self._update_workspace_choices()
            else:
                saved = False
            for path, error in errors:
                self._append_log(f"未登记 {path}: {error}")
            self._show_preview("父目录工作区登记结果", f"已验证 {len(verified)} 个独立工作副本，{len(errors)} 个候选未通过。\n"
                               + ("记录已保存。" if saved else "没有新增已保存的记录，请查看操作记录。")
                               + "\n\n可从上方工作区选择器逐个打开。扫描和登记不更新、不合并、不提交这些副本。"
                               + "\n外部引用定义与固定修订未自动导入；选中的嵌套副本仍作为独立范围操作。")
        self._run("逐个验证工作副本根", verify, registered)

    def open_workspace(self, folder, *, workspace_name=None, record_id=None, auto_restore=False):
        if self._busy or not str(folder).strip():
            return
        if self.demo:
            messagebox.showinfo("演示模式", "当前仅展示示例数据。关闭后不带 --demo 参数启动，即可打开真实工作副本。", parent=self)
            return
        if not auto_restore:
            self._manual_open_requested = True
        candidate = Path(folder).expanduser()
        executable = self.settings.get("svn_executable") or None
        def load():
            if not candidate.is_dir():
                raise FileNotFoundError("工作副本目录不存在或不是文件夹。请在工作区管理中重新定位，原记录会保留。")
            client = SvnClient(candidate, executable=discover_svn(executable))
            info = client.info()
            root = Path(info.get("wc_root") or candidate).resolve()
            if root != candidate.resolve():
                client = SvnClient(root, executable=discover_svn(executable))
                info = client.info()
            shelves = ShelfManager(client)
            return client, shelves, root, info, read_groupable_status(client, root), shelves.list()
        def loaded(data):
            new_settings = copy.deepcopy(self.settings)
            record = remember_workspace(new_settings, str(data[2]), name=workspace_name, record_id=record_id)
            self._clear_workspace_selection()
            self.client, self.shelf_manager, self.root_path, self.info, status_data, self.shelves = data
            self.entries, self.changelist_candidates = status_data
            self.all_files = []
            self.path_var.set(str(self.root_path))
            changed = new_settings != self.settings
            self.settings = new_settings
            self._active_workspace_id = record["id"]
            self._workspace_errors.pop(record["id"], None)
            self.workspace_name.configure(text=record["name"])
            if changed:
                self._save_settings()
            self._update_workspace_choices()
            self._repo_url = ""
            self.repo_var.set("选择“仓库浏览”加载当前仓库目录。")
            self.repository.delete(*self.repository.get_children())
            self.logs = []
            self._render_history()
            self._folder_rows = None
            self._render_all()
            self._reset_workspace_tree()
            self._set_busy(False)
            self._show_preview("工作副本信息", self._info_text())
            self.select_tab(0)
        def failed(error):
            if record_id:
                self._workspace_errors[record_id] = str(error)
            self._update_workspace_choices()
            self.path_var.set(str(self.root_path) if self.root_path else "")
            self._show_preview("工作区未打开", f"请求目录：{candidate}\n\n{error}\n\n原有工作区记录与当前打开的副本保持不变。\n可在“管理”中重新定位、打开其他副本，或扫描包含多个独立副本的父目录。\n如果缺少 svn.exe，请在“文件 → 设置 SVN 程序”中配置。")
            if not auto_restore and "E155007" in str(error):
                if messagebox.askyesno("此目录不是一个工作副本", "这个目录可能包含多个独立的 SVN 副本。要扫描此父目录并逐个选择吗？\n\n扫描有深度、数量和时间限制，不修改文件。", parent=self):
                    self.discover_workspaces(candidate)
            else:
                messagebox.showwarning("未能恢复上次工作区" if auto_restore else "无法打开工作区", str(error) + "\n\n记录已保留。可使用“工作区管理”重新定位或打开其他副本。", parent=self)
        self._run("恢复上次工作区" if auto_restore else "打开工作副本", load, loaded, on_error=failed)

    def refresh(self):
        if self.demo:
            self._load_demo()
            return
        if not self.client or self._busy:
            return
        client, shelves, root = self.client, self.shelf_manager, self.root_path
        def load():
            return client.info(), read_groupable_status(client, root), shelves.list()
        def loaded(data):
            self.info, status_data, self.shelves = data
            self.entries, self.changelist_candidates = status_data
            self.logs = []
            self._render_history()
            self._render_all()
            folder_node = next((iid for iid, data in self._workspace_nodes.items()
                                if data == (self._folder_scope, "dir") and self.workspace_tree.exists(iid)), None)
            if folder_node:
                self._refresh_workspace_directory(folder_node)
            current = self.tabs.index(self.tabs.select())
            if current == 2:
                self.load_history()
            elif current == 3:
                self.load_repository(self._repo_url or self.info.get("url"))
        self._run("刷新工作副本", load, loaded)

    def _info_text(self):
        return (f"本地目录：{self.root_path}\n仓库地址：{self.info.get('url', '')}\n"
                f"仓库根目录：{self.info.get('repository_root', '')}\n"
                f"修订：{self.info.get('revision', '')}\n"
                f"最近作者：{self.info.get('last_changed_author', '')}\n\n"
                "提交、还原、更新、Shelf 恢复均需手动确认。\n"
                "SVN 身份验证由已安装的 SVN 客户端管理；本程序不保存密码。")

    def _render_all(self, truncated=False):
        conflicts = sum(e.item == "conflicted" or e.tree_conflicted or e.props == "conflicted" for e in self.entries)
        changed = sum(has_local_changes(entry) for entry in self.entries)
        groups = {getattr(entry, "changelist", None) for entry in self.entries} - {None}
        self.summary_var.set(f"{changed} 个待处理更改  ·  {len(groups)} 个本地变更列表  ·  {conflicts} 个冲突  ·  r{self.info.get('revision', '?')}")
        self._render_changes()
        self._render_files()
        self._render_shelves()
        if self._folder_rows is None:
            self.files_note.configure(text="从左侧工作副本树选择目录 · 目录按需读取，不递归扫描")

    def _render_changes(self):
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
            self._filter_job = None
        selected = {self._change_lookup[i].path for i in self.changes.selection() if i in self._change_lookup}
        selected_groups = {self._group_lookup[i] for i in self.changes.selection() if i in self._group_lookup}
        collapsed = {self._group_lookup[i] for i in self.changes.get_children()
                     if i in self._group_lookup and not self.changes.item(i, "open")}
        self.changes.delete(*self.changes.get_children())
        self._change_lookup = {}
        self._group_lookup = {}
        for index, (name, members, visible) in enumerate(grouped_entries(self.entries, self.filter_var.get())):
            group_id = f"group{index}"
            changed = sum(has_local_changes(entry) for entry in members)
            title = "未分组（默认）" if name is None else f"变更列表：{name}"
            count = f"{changed} 更改 / {len(members)} 成员"
            if len(visible) != len(members):
                count += f" · 显示 {len(visible)}"
            self.changes.insert("", "end", iid=group_id, text=title, open=name not in collapsed,
                                values=("", count, "", ""), tags=("group",))
            self._group_lookup[group_id] = name
            if name in selected_groups:
                self.changes.selection_add(group_id)
            for member_index, entry in enumerate(visible):
                tag = "conflict" if entry.item == "conflicted" or entry.tree_conflicted or entry.props == "conflicted" else entry.item
                iid = f"group{index}-file{member_index}"
                self.changes.insert(group_id, "end", iid=iid, text=Path(entry.path).name,
                                    values=(status_label(entry), entry.path, entry.props if entry.props != "none" else "", entry.revision if entry.revision is not None else "—"), tags=(tag,))
                self._change_lookup[iid] = entry
                if entry.path in selected:
                    self.changes.selection_add(iid)
        self._update_action_states()

    def _schedule_filter(self, *_):
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(180, self._render_changes)

    def _render_files(self):
        self.files.delete(*self.files.get_children())
        self._file_lookup = {}
        self._file_kind = {}
        statuses = {e.path: status_label(e) for e in self.entries}
        if self._folder_rows is None:
            rows = [(path, "file", size) for path, size in self.all_files]
        else:
            rows = self._folder_rows[self._folder_offset:self._folder_offset + 200]
            count = len(self._folder_rows)
            start = self._folder_offset + 1 if count else 0
            self.files_note.configure(text=f"{self._folder_scope}  ·  {start}–{min(self._folder_offset + 200, count)} / {count} 项")
        for index, (path, kind, size) in enumerate(rows):
            iid = f"file{index}"
            self.files.insert("", "end", iid=iid, values=(path + ("/" if kind == "dir" else ""), "文件夹" if kind == "dir" else statuses.get(path, "正常 / 未扫描"), self._size(size)))
            self._file_lookup[iid] = path
            self._file_kind[iid] = kind

    @staticmethod
    def _size(value):
        if value is None:
            return "—"
        value = int(value)
        return f"{value / 1048576:.1f} MB" if value >= 1048576 else f"{value / 1024:.1f} KB" if value >= 1024 else f"{value} B"

    def _render_history(self):
        self.history.delete(*self.history.get_children())
        self._log_lookup = {}
        for index, entry in enumerate(self.logs):
            iid = f"log{index}"
            self.history.insert("", "end", iid=iid, values=(f"r{entry.revision}", entry.author, str(entry.date).replace("T", " ")[:19], entry.message.splitlines()[0] if entry.message else "（无说明）"))
            self._log_lookup[iid] = entry

    def _render_shelves(self):
        self.shelf_tree.delete(*self.shelf_tree.get_children())
        self._shelf_lookup = {}
        for index, shelf in enumerate(self.shelves):
            iid = f"shelf{index}"
            self.shelf_tree.insert("", "end", iid=iid, values=(shelf["name"], str(shelf["created_at"]).replace("T", " ")[:19], shelf.get("entry_count", len(shelf.get("paths", []))), shelf["id"]))
            self._shelf_lookup[iid] = shelf

    def select_tab(self, index):
        self.tabs.select(index)

    def _tab_changed(self, event=None):
        self._preview_request += 1
        current = self.tabs.index(self.tabs.select())
        self.title_label.configure(text=self.tabs.tab(current, "text"))
        for index, button in enumerate(self.nav_buttons):
            button.configure(bg="#29476c" if current == index else NAV, fg="white" if current == index else "#c1cee0")
        self._update_action_states()
        if not self._busy and not self.demo and self.client:
            if current == 2 and not self.logs:
                self.load_history()
            elif current == 3 and not self._repo_url:
                self.load_repository(self.info.get("url"))

    def _selection_hint(self, event=None):
        self._preview_request += 1
        self._update_action_states()
        selection = self.changes.selection()
        if len(selection) == 1 and selection[0] in self._group_lookup:
            self._pending_activated()
        elif selection:
            entries = [self._change_lookup[i] for i in selection if i in self._change_lookup]
            if entries:
                title = entries[0].path if len(entries) == 1 else f"已选择 {len(entries)} 个文件"
                self._show_preview(title + " · BASE ↔ WORKING", "\n".join(f"[{status_label(e)}] {e.path}" for e in entries)
                                   + "\n\n双击文件或点击“查看差异”加载文本差异。")
        if not self._busy:
            groups = [self._group_lookup[i] for i in selection if i in self._group_lookup]
            count = sum(i in self._change_lookup for i in selection)
            if groups:
                self.status_var.set("已选择分组 · 使用“提交所选列表”提交整个命名列表；文件操作需选择文件行")
            else:
                self.status_var.set(f"已选中 {count} 个文件 · 右键移动到列表 · 双击查看差异" if count else "就绪")

    def _pending_activated(self, event=None):
        selection = self.changes.selection()
        if len(selection) == 1 and selection[0] in self._group_lookup:
            name = self._group_lookup[selection[0]]
            members = [entry for entry in self.entries if getattr(entry, "changelist", None) == name]
            title = "未分组（默认）" if name is None else f"变更列表：{name}"
            body = "\n".join(f"[{status_label(entry)}] {entry.path}" for entry in members)
            self._show_preview(title, f"{len(members)} 个成员，{sum(has_local_changes(e) for e in members)} 个有修改。\n\n{body}\n\nSVN 变更列表仅为本机文件分组，不保存文件内容，也不会上传或锁定文件。\n使用“提交所选列表”可提交整个命名列表中的修改，包括被筛选隐藏的成员。\n移动最后一个文件后，空列表会自动消失。")
        elif selection:
            self.preview_changes()

    def _pending_menu(self, event):
        if self._busy:
            return
        row = self.changes.identify_row(event.y)
        if row and row not in self.changes.selection():
            self.changes.selection_set(row)
        elif not row:
            self.changes.selection_remove(*self.changes.selection())
        menu = tk.Menu(self, tearoff=False)
        def item(label, callback, action):
            menu.add_command(label=label, command=callback, state="normal" if self._action_available(action) else "disabled")
        item("新建变更列表…", self.new_changelist, "new_list")
        if row in self._group_lookup:
            item("提交整个所选变更列表…", self.commit_changelist, "commit_group")
            menu.add_command(label="查看列表成员与说明", command=self._pending_activated)
        elif row in self._change_lookup:
            item("移动选中文件到列表…", self.move_to_changelist, "move")
            item("移回未分组…", self.remove_from_changelist, "unassign")
            menu.add_separator()
            item("提交选中文件…", self.commit_selected, "commit_files")
            item("查看文件差异", self.preview_changes, "diff")
            menu.add_command(label="还原选中文件…", command=self.revert_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _selected_entries(self):
        if self.tabs.index(self.tabs.select()) != 0:
            messagebox.showinfo("选择待提交更改", "请在“待提交更改”中选中要操作的路径。可按 Ctrl / Shift 多选。", parent=self)
            self.select_tab(0)
            return []
        return [self._change_lookup[i] for i in self.changes.selection() if i in self._change_lookup]

    def _changelist_names(self):
        return sorted({getattr(entry, "changelist", None) for entry in self.entries} - {None}, key=str.casefold)

    def new_changelist(self):
        if not self._can_operate():
            return
        if not self.changelist_candidates:
            messagebox.showinfo("需要已版本化文件", "SVN 不保存空变更列表。请先在工作副本中添加文件，再创建列表。目录不能归组。", parent=self)
            return
        selected = [self._change_lookup[i].path for i in self.changes.selection() if i in self._change_lookup]
        dialog = ChangelistDialog(self, self.changelist_candidates, selected=selected, existing=self._changelist_names())
        if dialog.result:
            name, paths = dialog.result
            self._run("创建变更列表并归组文件", lambda: self.client.assign_changelist(name, paths), self._after_changelist_change, mutation=True)

    def move_to_changelist(self):
        if not self._can_operate():
            return
        selected = self._selected_entries()
        if not selected:
            messagebox.showinfo("选择文件行", "请先选择要移动的文件行（不是分组标题），再选择目标列表。", parent=self)
            return
        eligible = {entry.path for entry in self.changelist_candidates}
        if any(entry.path not in eligible for entry in selected):
            messagebox.showwarning("只能归组已版本化文件", "选中项包含未版本化文件、目录或暂不能归组的路径。请先添加文件并处理缺失或阻塞状态。", parent=self)
            return
        dialog = ChangelistDialog(self, selected, selected=[entry.path for entry in selected], existing=self._changelist_names(), move=True)
        if dialog.result:
            name, paths = dialog.result
            self._run("移动文件到变更列表", lambda: self.client.assign_changelist(name, paths), self._after_changelist_change, mutation=True)

    def remove_from_changelist(self):
        if not self._can_operate():
            return
        selected = self._selected_entries()
        paths = [entry.path for entry in selected if getattr(entry, "changelist", None) is not None]
        if not paths:
            messagebox.showinfo("选择已归组的文件", "请选中命名列表中的文件行。分组标题不会自动选中其下所有文件。", parent=self)
            return
        if messagebox.askokcancel("移回未分组", f"将 {len(paths)} 个文件移回未分组？\n\n仅移除本地分组标签，文件内容和未提交修改不会改变。空列表会自动消失。\n\n" + "\n".join(paths[:15]), parent=self):
            self._run("移除文件的变更列表标签", lambda: self.client.remove_changelist(paths), self._after_changelist_change, mutation=True)

    def _after_changelist_change(self, result):
        """Label-only operations do not need to rescan every local file."""
        if getattr(result, "stdout", ""):
            self._append_log(result.stdout)
        if getattr(result, "stderr", ""):
            self._append_log(result.stderr)
        client, root = self.client, self.root_path
        def loaded(data):
            self.entries, self.changelist_candidates = data
            self._render_all(len(self.all_files) >= 3000)
        self._run("刷新变更列表", lambda: read_groupable_status(client, root), loaded)

    def commit_changelist(self):
        if not self._can_operate():
            return
        if self.tabs.index(self.tabs.select()) != 0:
            self.select_tab(0)
            messagebox.showinfo("选择变更列表", "请选择一个命名变更列表的标题，然后点击“提交所选列表”。", parent=self)
            return
        names = set()
        for iid in self.changes.selection():
            if iid in self._group_lookup:
                names.add(self._group_lookup[iid])
            elif iid in self._change_lookup:
                names.add(getattr(self._change_lookup[iid], "changelist", None))
        if len(names) != 1 or None in names:
            messagebox.showinfo("请选择一个命名列表", "请选择且只选择一个命名变更列表。未分组文件请使用“提交选中项”。", parent=self)
            return
        name = next(iter(names))
        members = [entry for entry in self.entries if getattr(entry, "changelist", None) == name]
        changed = [entry for entry in members if has_local_changes(entry)]
        if not changed:
            messagebox.showinfo("列表没有待提交修改", "列表成员均未修改，无需提交。文件仍保留在本地变更列表中。", parent=self)
            return
        invalid = [entry.path for entry in changed if entry.item in {"unversioned", "missing", "conflicted", "obstructed", "external", "ignored", "incomplete"} or entry.tree_conflicted or entry.props == "conflicted"]
        if invalid:
            messagebox.showwarning("先处理列表中的问题", "整个列表中存在不能提交的路径（包括被筛选隐藏的文件）：\n\n" + "\n".join(invalid[:15]), parent=self)
            return
        paths = sorted(entry.path for entry in members)
        dialog = CommitDialog(self, paths, changelist=name, statuses={entry.path: status_label(entry) for entry in members})
        if dialog.result:
            self._run(f"提交变更列表：{name}", lambda: self.client.commit_changelist(name, dialog.result, expected_paths=paths), self._after_mutation, mutation=True)

    def _selected_paths(self):
        was_pending = self.tabs.index(self.tabs.select()) == 0
        entries = self._selected_entries()
        if not entries and was_pending:
            messagebox.showinfo("尚未选择文件", "请先选中要操作的路径。", parent=self)
        return [e.path for e in entries]

    def _can_operate(self):
        if self._busy:
            return False
        if self.demo:
            messagebox.showinfo("演示模式", "示例数据仅用于体验界面，不执行 SVN 或文件写入操作。", parent=self)
            return False
        if not self.client:
            messagebox.showinfo("打开工作副本", "请先打开已有的 SVN 工作副本。", parent=self)
            return False
        return True

    def preview_changes(self):
        if self._busy:
            return
        paths = self._selected_paths()
        if not paths:
            return
        if self.demo:
            self._show_preview(paths[0], "Index: " + paths[0] + "\n===================================================================\n--- BASE\n+++ WORKING\n@@ -2,3 +2,4 @@\n-    speed = 4.0\n+    speed = 5.5\n+    acceleration = 0.8\n     return speed\n", diff=True)
            return
        selected = self._selected_entries()
        client, root = self.client, self.root_path
        request = self._preview_request
        def load():
            parts = []
            tracked = [e.path for e in selected if e.item not in {"unversioned", "ignored", "external"}]
            if tracked:
                parts.append(client.diff(tracked))
            for entry in selected:
                if entry.item == "unversioned":
                    parts.append(f"\n未版本化文件：{entry.path}\n" + read_preview(root, entry.path))
            return "\n".join(parts) or "没有可显示的文本差异。二进制文件、目录或部分属性更改可能没有行级差异。"
        self._run("读取差异", load, lambda text: self._show_preview(f"选中 {len(paths)} 个路径 · BASE ↔ WORKING", text, diff=True)
                  if request == self._preview_request else None)

    def preview_file(self):
        if self._busy:
            return
        selected = self.files.selection()
        if not selected:
            return
        path = self._file_lookup[selected[0]]
        if self._file_kind.get(selected[0]) == "dir":
            self._navigate_workspace_directory(path)
            return
        self._preview_local_path(path)

    def _file_selection_details(self, event=None):
        self._preview_request += 1
        selected = self.files.selection()
        if not selected or selected[0] not in self._file_lookup:
            return
        path = self._file_lookup[selected[0]]
        kind = self._file_kind.get(selected[0], "file")
        self._show_preview(path, "双击打开此目录。目录选择仅用于导航，不加入提交范围。" if kind == "dir" else f"工作副本文件：{path}\n\n双击查看本地内容。要查看 SVN 差异，请在待提交更改中选择此文件。")

    def _preview_local_path(self, path):
        if self._busy:
            return
        request = self._preview_request
        if self.demo:
            self._show_preview(path, "# 示例文件预览\n# 在真实工作副本中，这里显示文件内容。\n")
        else:
            self._run("读取本地文件", lambda: read_preview(self.root_path, path),
                      lambda text: self._show_preview(path, text) if request == self._preview_request else None)

    def load_history(self):
        if self.demo or not self.client or self._busy:
            return
        def loaded(entries):
            self.logs = entries
            self._render_history()
            self.status_var.set(f"已加载最近 {len(entries)} 条提交 · 双击查看修订差异")
        self._run("读取最近 100 条提交历史", lambda: self.client.log(limit=100, revision="HEAD:0"), loaded)

    def preview_history(self, event=None):
        self._preview_request += 1
        selected = self.history.selection()
        if not selected:
            return
        entry = self._log_lookup[selected[0]]
        paths = "\n".join(f"{p.get('action', '')}  {p.get('path', '')}" for p in entry.changed_paths)
        self._show_preview(f"修订 r{entry.revision}", f"作者：{entry.author}\n日期：{entry.date}\n\n{entry.message}\n\n更改路径\n{paths}\n\n双击此修订可加载完整差异。")

    def preview_revision_diff(self):
        selected = self.history.selection()
        if not selected or self._busy:
            return
        entry = self._log_lookup[selected[0]]
        request = self._preview_request
        if self.demo:
            self._show_preview(f"r{entry.revision} 差异", "演示模式：真实仓库的修订差异会显示在这里。")
            return
        self._run(f"读取 r{entry.revision} 差异", lambda: self.client.command(["diff", "-c", str(entry.revision), "--", "."]),
                  lambda result: self._show_preview(f"r{entry.revision} 差异", result.stdout or "此路径没有文本差异。", diff=True)
                  if request == self._preview_request else None)

    def load_repository(self, url):
        if not url or not self.client or self.demo or self._busy:
            return
        def loaded(entries):
            self._repo_url = url
            self.repo_var.set(url)
            self.repository.delete(*self.repository.get_children())
            self._repo_entries = {}
            for index, entry in enumerate(entries):
                iid = f"repo{index}"
                self.repository.insert("", "end", iid=iid, values=(entry.name, "文件夹" if entry.kind == "dir" else "文件", entry.revision, entry.author, "—" if entry.kind == "dir" else self._size(entry.size)))
                self._repo_entries[iid] = entry
            parent = next((iid for iid, data in self._repository_nodes.items()
                           if data == (url, "dir") and self.repository_tree.exists(iid)), None)
            if parent is None:
                parent = self.repository_tree.insert("", "end", text=url, open=True)
                self._repository_nodes[parent] = (url, "dir")
            children = self.repository_tree.get_children(parent)
            stack = list(children)
            while stack:
                child = stack.pop()
                stack.extend(self.repository_tree.get_children(child))
                self._repository_nodes.pop(child, None)
            self.repository_tree.delete(*children)
            self.repository_tree.item(parent, open=True)
            for entry in entries:
                child_url = url.rstrip("/") + "/" + quote(entry.name, safe="")
                node = self.repository_tree.insert(parent, "end", text=entry.name + ("/" if entry.kind == "dir" else ""))
                self._repository_nodes[node] = (child_url, entry.kind)
                if entry.kind == "dir":
                    self.repository_tree.insert(node, "end", text="展开读取仓库…")
        self._run("浏览仓库", lambda: self.client.list_entries(target=url), loaded)

    def repository_open(self, event=None):
        selected = self.repository.selection()
        if not selected or self._busy or self.demo:
            return
        entry = self._repo_entries[selected[0]]
        if entry.kind == "dir":
            self.load_repository(self._repo_url.rstrip("/") + "/" + quote(entry.name, safe=""))
        else:
            self._show_preview(entry.name, f"仓库文件：{self._repo_url.rstrip('/')}/{entry.name}\n修订：r{entry.revision}\n作者：{entry.author}\n大小：{self._size(entry.size)}\n\n仓库浏览为只读视图。请在工作副本中打开或修改文件。")

    def _repository_selection_details(self, event=None):
        selected = self.repository.selection()
        if not selected or selected[0] not in self._repo_entries:
            return
        entry = self._repo_entries[selected[0]]
        self._show_preview(entry.name, f"仓库对象：{self._repo_url.rstrip('/')}/{entry.name}\n类型：{'目录' if entry.kind == 'dir' else '文件'}\n修订：r{entry.revision}\n作者：{entry.author}\n\n双击目录可进入。仓库导航不会修改工作副本。")

    def repository_up(self):
        base = str(self.info.get("repository_root", "")).rstrip("/")
        current = self._repo_url.rstrip("/")
        if current and base and current != base and current.startswith(base + "/"):
            self.load_repository(current.rsplit("/", 1)[0])

    def _after_mutation(self, result):
        output = getattr(result, "stdout", "")
        errors = getattr(result, "stderr", "")
        if output:
            self._append_log(output)
        if errors:
            self._append_log(errors)
        self.refresh()

    def update_workspace(self):
        if not self._can_operate():
            return
        if messagebox.askokcancel("更新工作副本", "从仓库更新整个工作副本到 HEAD？\n\n更新会修改本地文件，并可能产生冲突。建议先为重要的未提交修改创建 Shelf。", parent=self):
            self._run("更新工作副本", self.client.update, self._after_mutation, mutation=True)

    def commit_selected(self):
        if not self._can_operate():
            return
        entries = self._selected_entries()
        if not entries:
            messagebox.showinfo("选择提交内容", "请先在“待提交更改”中选择要提交的路径。", parent=self)
            return
        invalid = [e.path for e in entries if e.item in {"unversioned", "missing", "conflicted", "obstructed", "external", "ignored", "incomplete"} or e.tree_conflicted or e.props == "conflicted"]
        if invalid:
            messagebox.showwarning("这些路径暂不能提交", "请先添加未版本化文件或处理缺失、冲突和阻塞状态：\n\n" + "\n".join(invalid[:15]), parent=self)
            return
        paths = [e.path for e in entries]
        dialog = CommitDialog(self, paths)
        if dialog.result:
            self._run("提交选中更改", lambda: self.client.commit(dialog.result, paths=paths), self._after_mutation, mutation=True)

    def add_selected(self):
        if not self._can_operate():
            return
        entries = self._selected_entries()
        if not entries:
            return
        if any(e.item != "unversioned" for e in entries):
            messagebox.showwarning("只添加未版本化路径", "请只选择状态为“未版本化”的文件或文件夹。", parent=self)
            return
        paths = [e.path for e in entries]
        if messagebox.askokcancel("添加到版本控制", f"将 {len(paths)} 个选中路径加入版本控制？\n目录将递归添加文件；本操作不会提交到仓库。", parent=self):
            self._run("添加到版本控制", lambda: self.client.add(paths), self._after_mutation, mutation=True)

    def revert_selected(self):
        if not self._can_operate():
            return
        paths = self._selected_paths()
        if not paths:
            return
        if messagebox.askyesno("丢弃本地更改", f"还原 {len(paths)} 个选中路径？\n\n这会永久丢弃这些路径的未提交修改。选中文件夹会递归还原其下的修改。\n此操作无法撤销；请先创建 Shelf 或其他备份。\n\n" + "\n".join(paths[:10]), icon="warning", default="no", parent=self):
            self._run("还原选中路径", lambda: self.client.revert(paths, recursive=True), self._after_mutation, mutation=True)

    def create_shelf(self):
        if not self._can_operate():
            return
        paths = self._selected_paths()
        if not paths:
            return
        name = simpledialog.askstring("新建本地 Shelf", f"为 {len(paths)} 个选中路径创建本地备份。\n原有修改保留在工作区；不会提交到仓库。\nShelf 不保存变更列表标签，恢复后可能需要重新归组。\n\nShelf 名称：", parent=self)
        if name and name.strip():
            def created(result):
                self._append_log("已创建本地 Shelf：" + str(result.get("name", name)))
                self.refresh()
            self._run("创建本地 Shelf", lambda: self.shelf_manager.create(name.strip(), paths=paths), created)

    def _selected_shelf(self):
        if self.tabs.index(self.tabs.select()) != 4:
            self.select_tab(4)
        selection = self.shelf_tree.selection()
        if not selection:
            messagebox.showinfo("选择 Shelf", "请先选中一个本地 Shelf。", parent=self)
            return None
        return self._shelf_lookup[selection[0]]

    def preview_shelf(self, event=None):
        selection = self.shelf_tree.selection()
        if not selection:
            return
        shelf = self._shelf_lookup[selection[0]]
        paths = shelf.get("paths", [])
        text = f"名称：{shelf['name']}\n创建于：{shelf['created_at']}\n标识：{shelf['id']}\n\n"
        text += "\n".join(str(p) for p in paths)
        text += "\n\n本地 Shelf 是修改的备份，不会自动清空工作区。\nShelf 不保存或恢复变更列表标签，恢复后可能需要重新归组。\n恢复要求目标路径没有本地修改，且 BASE 与创建时匹配；冲突会中止恢复。"
        self._show_preview("本地 Shelf 详情", text)

    def restore_shelf(self):
        if not self._can_operate():
            return
        shelf = self._selected_shelf()
        if shelf and messagebox.askokcancel("恢复本地 Shelf", f"将“{shelf['name']}”恢复到当前工作副本？\n\n目标路径必须干净，BASE 必须与创建时匹配。新增或未版本化文件的目标路径必须不存在。\n发现不兼容状态时会中止。Shelf 本身会保留。\nShelf 不恢复变更列表标签，请按需重新归组。", parent=self):
            self._run("恢复本地 Shelf", lambda: self.shelf_manager.restore(shelf["id"]), self._after_mutation, mutation=True)

    def delete_shelf(self):
        if not self._can_operate():
            return
        shelf = self._selected_shelf()
        if shelf and messagebox.askyesno("永久删除 Shelf", f"永久删除本地 Shelf“{shelf['name']}”？\n\n工作副本不受影响，但此备份无法恢复。", icon="warning", default="no", parent=self):
            self._run("删除本地 Shelf", lambda: self.shelf_manager.delete(shelf["id"]), self._after_mutation, mutation=True)

    def configure_svn(self):
        if self._busy:
            return
        self._manual_open_requested = True
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不会修改本地设置。请正常启动后设置 SVN 程序。", parent=self)
            return
        path = filedialog.askopenfilename(title="选择 SVN 命令行程序（svn.exe）", filetypes=[("SVN 程序", "svn.exe"), ("所有文件", "*")], parent=self)
        if path:
            self.settings["svn_executable"] = path
            if self._save_settings():
                messagebox.showinfo("已保存", "SVN 程序路径已保存。重新打开工作副本后生效。", parent=self)

    def show_help(self):
        self._show_preview("使用说明", "1. 安装 SVN 命令行工具，打开已有工作副本。\n2. 点击“+ 新建变更列表”，输入名称、选择已版本化文件，点击“创建并归组”。\n3. 也可先按 Ctrl / Shift 多选文件，再点击“移动到列表”。已有列表可从下拉框选择。\n4. 选中分组标题，点击“提交所选列表”，核对全部成员并填写提交说明。筛选隐藏的成员也会列入确认。\n5. “移回未分组”只清除分组标签，不还原、不删除文件；未版本化文件请先添加。\n6. 双击文件查看差异，双击分组查看全部成员。文件操作只作用于明确选中的文件行。\n7. 重要修改可以创建本地 Shelf。创建只备份，不清空修改。恢复前请备份现有工作。\n\nSVN 与 P4V 的差别\n• SVN 变更列表仅在本机工作副本保存；不是服务器上的编号提交单。\n• 列表仅支持文件，每个文件只能归属一个列表；目录无法归组。\n• SVN 不保存空列表，移走最后一个文件后列表自动消失。\n• 提交成功后，已提交文件的标签默认移除。干净的已归组文件仍可显示，但不计入待提交修改数。\n• 分组不保存文件内容，也不锁定文件；Shelf 才是本地内容备份。\n• 新增目录等依赖无法只通过文件分组隔离；SVN 拒绝时不会自动扩大提交范围。\n\n快捷键：Ctrl+O 打开工作副本 · F5 刷新 · Ctrl+D 查看选中差异\n\n安全提示\n• 还原会丢弃未提交更改，删除 Shelf 会永久删除备份。\n• 本地 Shelf 不是服务器备份；不会同步到其他电脑。\n• SVN 身份验证由安装的 SVN 客户端处理，本程序不保存密码。\n• 本界面没有通用 SVN 命令输入框。未覆盖的命令请使用外部 SVN 客户端。")

    def _load_demo(self):
        from types import SimpleNamespace
        self.root_path = Path("D:/Projects/OrionGame")
        self.path_var.set(str(self.root_path))
        self.workspace_name.configure(text="OrionGame\n开发工作副本")
        self.workspace_choice_var.set("演示工作区 · OrionGame")
        self.info = {"revision": "248", "url": "https://svn.example.com/OrionGame/trunk", "repository_root": "https://svn.example.com/OrionGame"}
        rows = [("src/player/controller.py", "modified", "248"), ("src/game/session.py", "modified", "246"),
                ("assets/ui/inventory.png", "added", ""), ("docs/release-notes.md", "unversioned", ""),
                ("config/input.json", "modified", "248"), ("tests/test_controller.py", "modified", "247")]
        assignments = {"src/player/controller.py": "输入手感优化", "tests/test_controller.py": "输入手感优化",
                       "assets/ui/inventory.png": "背包界面"}
        self.entries = [SimpleNamespace(path=p, item=s, revision=r, props="none", tree_conflicted=False,
                                        changelist=assignments.get(p)) for p, s, r in rows]
        self.entries.append(SimpleNamespace(path="README.md", item="normal", revision="248", props="none", tree_conflicted=False, changelist="输入手感优化"))
        self.changelist_candidates = [entry for entry in self.entries if entry.item != "unversioned"]
        self.all_files = [(p, 4100 + i * 1280) for i, (p, _, _) in enumerate(rows)] + [("README.md", 1240), ("src/game/scene.py", 6200)]
        self.logs = [SimpleNamespace(revision=248-i, author="developer" if i % 2 == 0 else "teammate",
                                    date=f"2026-11-{3 if i < 2 else 2:02d}T09:{40-i*6:02d}:00Z",
                                    message=message, changed_paths=[{"action": "M", "path": "/trunk/src/game/session.py"}])
                     for i, message in enumerate(["优化游戏启动流程与资源加载", "添加控制器输入测试", "整理 UI 图标资源", "修复场景切换状态"])]
        self.shelves = [{"id": "demo-shelf-01", "name": "控制器手感调整 · 实验 A", "created_at": "2026-11-03T08:32:00Z", "entry_count": 2,
                         "paths": ["src/player/controller.py", "tests/test_controller.py"]}]
        self._render_all()
        self._render_history()
        self._reset_workspace_tree()
        self.changes.selection_set(next(iid for iid, entry in self._change_lookup.items() if entry.path == "src/player/controller.py"))
        self._show_preview("src/player/controller.py  ·  BASE ↔ WORKING", "Index: src/player/controller.py\n===================================================================\n--- src/player/controller.py    (revision 248)\n+++ src/player/controller.py    (working copy)\n@@ -18,6 +18,8 @@\n class PlayerController:\n-    move_speed = 4.0\n+    move_speed = 5.5\n+    acceleration = 0.8\n \n     def update(self, delta):\n-        self.velocity = self.direction * self.move_speed\n+        target = self.direction * self.move_speed\n+        self.velocity = lerp(self.velocity, target, self.acceleration)\n", diff=True)
        self._append_log("演示模式：示例工作副本与数据。未执行 SVN 命令。")
        self._set_busy(False)

    def _close(self):
        if self._busy:
            messagebox.showinfo("操作仍在进行", "请等待当前操作结束后再退出，以免无法确认操作结果。", parent=self)
            return
        self._closing = True
        self.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="SVN Desk · 原生 Subversion 桌面工作台")
    parser.add_argument("working_copy", nargs="?", help="已有 SVN 工作副本的路径")
    parser.add_argument("--demo", action="store_true", help="使用只读示例数据体验界面")
    args = parser.parse_args(argv)
    app = SvnDesk(args.working_copy, demo=args.demo)
    app.mainloop()


if __name__ == "__main__":
    main()
