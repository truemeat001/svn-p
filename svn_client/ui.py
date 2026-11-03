"""SVN Desk: a native, dependency-free desktop interface for Subversion.

All SVN and filesystem work runs off the Tk thread.  The interface never
submits changes, reverts files, or restores a shelf without an explicit dialog.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import stat
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote

from .backend import SvnClient, discover_svn
from .shelves import ShelfManager


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


class CommitDialog(tk.Toplevel):
    def __init__(self, parent, paths):
        super().__init__(parent)
        self.result = None
        self.title("提交到 SVN 仓库")
        self.transient(parent)
        self.geometry("650x480")
        self.minsize(520, 390)
        self.configure(bg=BG)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"提交 {len(paths)} 个选中路径", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="此操作会将以下路径的更改发送到远程仓库。目录会包含其下的更改。",
                  wraplength=590).pack(anchor="w", pady=(7, 12))
        listing = tk.Text(body, height=6, relief="flat", font=("Consolas", 10), bg="#e9eef5")
        listing.pack(fill="x")
        listing.insert("1.0", "\n".join(paths))
        listing.configure(state="disabled")
        ttk.Label(body, text="提交说明（必填）").pack(anchor="w", pady=(16, 6))
        self.message = tk.Text(body, height=7, wrap="word", relief="solid", bd=1,
                               font=("Microsoft YaHei UI", 10), undo=True)
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


class SvnDesk(tk.Tk):
    def __init__(self, working_copy=None, *, demo=False):
        super().__init__()
        self.title(APP_NAME + " · Subversion 工作台")
        self.geometry("1320x850")
        self.minsize(1030, 680)
        self.configure(bg=BG)
        self.demo = demo
        self.client = None
        self.shelf_manager = None
        self.root_path = None
        self.info = {}
        self.entries = []
        self.logs = []
        self.shelves = []
        self.all_files = []
        self._repo_entries = {}
        self._repo_url = ""
        self._busy = False
        self._closing = False
        self._queue = queue.Queue()
        self._controls = []
        self._change_lookup = {}
        self._file_lookup = {}
        self._log_lookup = {}
        self._shelf_lookup = {}
        self._settings_file = Path.home() / ".svn-desk" / "settings.json"
        self.settings = self._read_settings()
        self._make_style()
        self._make_menu()
        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_queue)
        self._set_busy(False)
        if demo:
            self.after(50, self._load_demo)
        elif working_copy:
            self.after(50, lambda: self.open_workspace(working_copy))
        else:
            self._show_preview("欢迎使用 SVN Desk", "打开一个已有的 SVN 工作副本开始。\n\n左侧切换工作视图，上方处理常用操作，下方查看差异与操作记录。\n\n本地 Shelf 保存修改的副本，不会自动清空工作区，也不会上传到服务器。\n\n首次使用：安装 SVN 命令行工具，或在“设置”中选择 svn.exe。")

    def _read_settings(self):
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self._append_log(f"无法保存本地设置：{error}")

    def _make_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("White.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TButton", padding=(12, 7), borderwidth=0, background="#e7ecf3")
        style.map("TButton", background=[("active", "#dbe4f1"), ("disabled", "#edf0f4")],
                  foreground=[("disabled", "#9aa4b1")])
        style.configure("Primary.TButton", foreground="white", background=BLUE)
        style.map("Primary.TButton", background=[("active", "#1d57ae"), ("disabled", "#a8bce0")], foreground=[("disabled", "#f4f6fa")])
        style.configure("Treeview", background="white", fieldbackground="white", foreground=TEXT,
                        rowheight=30, borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#eaf0f6", foreground="#53647b", padding=(8, 9), relief="flat")
        style.map("Treeview", background=[("selected", "#e0ecff")], foreground=[("selected", "#174d94")])
        style.configure("TNotebook", tabmargins=(0, 5, 0, 0), borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), background="#e9edf4")
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("Horizontal.TProgressbar", troughcolor="#e6ebf2", background=BLUE, borderwidth=0)

    def _make_menu(self):
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="打开工作副本…", accelerator="Ctrl+O", command=self.choose_workspace)
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
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="使用说明", command=self.show_help)
        help_menu.add_command(label="关于 SVN Desk", command=lambda: messagebox.showinfo(APP_NAME, "SVN Desk 0.1\n\nP4V 风格的独立 Subversion 桌面工作台。\n不隶属于 Perforce 或 Apache Subversion。", parent=self))
        menu.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menu)

    def _button(self, parent, title, callback, *, primary=False, always=False):
        button = ttk.Button(parent, text=title, command=callback,
                            style="Primary.TButton" if primary else "TButton")
        button.pack(side="left", padx=(0, 7))
        self._controls.append((button, always))
        return button

    def _build_ui(self):
        sidebar = tk.Frame(self, bg=NAV, width=218)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="SVN  DESK", bg=NAV, fg="white", anchor="w",
                 font=("Segoe UI", 21, "bold")).pack(fill="x", padx=22, pady=(28, 3))
        tk.Label(sidebar, text="Subversion 工作台", bg=NAV, fg="#9bb2ce", anchor="w",
                 font=("Microsoft YaHei UI", 10)).pack(fill="x", padx=24, pady=(0, 32))
        tk.Label(sidebar, text="工作空间", bg=NAV, fg="#8499b5", anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=24, pady=(0, 9))
        self.workspace_name = tk.Label(sidebar, text="尚未打开工作副本", bg="#223750", fg="#ecf3fc",
                                       wraplength=170, justify="left", anchor="w", padx=14, pady=13,
                                       font=("Microsoft YaHei UI", 11, "bold"))
        self.workspace_name.pack(fill="x", padx=12)
        self.nav_buttons = []
        for index, (label, note) in enumerate([("待提交更改", "PENDING"), ("工作副本文件", "WORKSPACE"),
                                              ("提交历史", "HISTORY"), ("仓库浏览", "REPOSITORY"),
                                              ("本地 Shelves", "SHELVES")]):
            button = tk.Button(sidebar, text=f"{label}   ›", anchor="w", relief="flat", bd=0,
                               bg=NAV, fg="#c1cee0", activebackground="#2a4565", activeforeground="white",
                               padx=23, pady=14, cursor="hand2", font=("Microsoft YaHei UI", 10),
                               command=lambda i=index: self.select_tab(i))
            button.pack(fill="x", pady=(14 if index == 0 else 0, 0))
            self.nav_buttons.append(button)
        tk.Label(sidebar, text="本地 Shelf ≠ 服务器备份\n创建后保留工作区修改", bg=NAV, fg="#8da4c0",
                 justify="left", anchor="w", font=("Microsoft YaHei UI", 9)).pack(side="bottom", fill="x", padx=23, pady=26)
        content = ttk.Frame(self, padding=(22, 18, 22, 10))
        content.pack(side="left", fill="both", expand=True)
        heading = ttk.Frame(content)
        heading.pack(fill="x")
        self.title_label = ttk.Label(heading, text="待提交更改", style="Heading.TLabel")
        self.title_label.pack(side="left")
        ttk.Label(heading, text="原生 SVN  ·  本地优先", style="Muted.TLabel").pack(side="right")
        location = ttk.Frame(content)
        location.pack(fill="x", pady=(15, 12))
        self.path_var = tk.StringVar()
        ttk.Label(location, text="工作副本").pack(side="left", padx=(0, 9))
        self.path_entry = ttk.Entry(location, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.path_entry.bind("<Return>", lambda event: self.open_workspace(self.path_var.get()))
        open_button = ttk.Button(location, text="打开…", command=self.choose_workspace)
        open_button.pack(side="left", padx=(8, 0))
        self._controls.append((open_button, True))
        toolbar = ttk.Frame(content)
        toolbar.pack(fill="x", pady=(0, 10))
        self._button(toolbar, "刷新", self.refresh)
        self._button(toolbar, "更新…", self.update_workspace)
        self._button(toolbar, "提交选中项…", self.commit_selected, primary=True)
        self._button(toolbar, "添加…", self.add_selected)
        self._button(toolbar, "还原…", self.revert_selected)
        self._button(toolbar, "新建 Shelf…", self.create_shelf)
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
        pending_header = ttk.Frame(self.pages[0], padding=(10, 9))
        pending_header.pack(fill="x")
        ttk.Label(pending_header, text="筛选").pack(side="left", padx=(0, 8))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_changes())
        ttk.Entry(pending_header, textvariable=self.filter_var, width=25).pack(side="left")
        ttk.Label(pending_header, text="按 Ctrl / Shift 多选 · 双击查看差异", style="Muted.TLabel").pack(side="right")
        self.changes = self._tree(self.pages[0], [("status", "状态", 105), ("path", "文件路径", 390),
                                                ("props", "属性", 65), ("revision", "基础修订", 80)])
        self.changes.bind("<Double-1>", lambda event: self.preview_changes())
        self.changes.bind("<<TreeviewSelect>>", self._selection_hint)
        self.changes.tag_configure("conflict", foreground="#bb3a35")
        self.changes.tag_configure("added", foreground="#26714c")
        self.changes.tag_configure("missing", foreground="#a66b17")
        file_header = ttk.Frame(self.pages[1], padding=(10, 9))
        file_header.pack(fill="x")
        self.files_note = ttk.Label(file_header, text="工作副本中的本地文件（隐藏 .svn）", style="Muted.TLabel")
        self.files_note.pack(side="left")
        self.files = self._tree(self.pages[1], [("path", "文件路径", 470), ("status", "SVN 状态", 110), ("size", "大小", 90)])
        self.files.bind("<Double-1>", lambda event: self.preview_file())
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

    def _tree(self, parent, columns):
        frame = ttk.Frame(parent, style="White.TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="extended")
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
            tree.column(name, width=width, minwidth=60, anchor="w", stretch=name in {"path", "message", "name"})
        return tree

    def _text_area(self, parent, background="white", foreground=TEXT):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="none", height=10, bg=background, fg=foreground, relief="flat", bd=0,
                       padx=12, pady=9, font=("Consolas", 10), insertbackground=foreground, state="disabled")
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
        if busy:
            self.status_var.set(label + "…")
            self.progress.start(12)
        else:
            self.status_var.set("演示模式 · 示例数据，不连接仓库" if self.demo else "就绪")
            self.progress.stop()

    def _run(self, label, work, done=None, *, mutation=False):
        if self._busy or self._closing:
            self.bell()
            return
        self._set_busy(True, label)
        self._append_log("› " + label)
        def worker():
            try:
                self._queue.put((True, work(), done, label, mutation))
            except Exception as error:
                self._queue.put((False, error, done, label, mutation))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        if self._closing:
            return
        try:
            ok, result, done, label, mutation = self._queue.get_nowait()
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
                messagebox.showerror(label + "失败", str(result) + note, parent=self)
                if mutation and self.client:
                    self.refresh()
        self.after(90, self._drain_queue)

    def _append_log(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", str(text).rstrip() + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _show_preview(self, title, content, *, diff=False):
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
        folder = filedialog.askdirectory(title="选择 SVN 工作副本", initialdir=str(self.root_path or self.settings.get("last_workspace", Path.home())), parent=self)
        if folder:
            self.open_workspace(folder)

    def open_workspace(self, folder):
        if self._busy or not str(folder).strip():
            return
        if self.demo:
            messagebox.showinfo("演示模式", "当前仅展示示例数据。关闭后不带 --demo 参数启动，即可打开真实工作副本。", parent=self)
            return
        candidate = Path(folder).expanduser()
        executable = self.settings.get("svn_executable") or None
        def load():
            client = SvnClient(candidate, executable=discover_svn(executable))
            info = client.info()
            root = Path(info.get("wc_root") or candidate).resolve()
            if root != candidate.resolve():
                client = SvnClient(root, executable=discover_svn(executable))
                info = client.info()
            shelves = ShelfManager(client)
            return client, shelves, root, info, client.status(), shelves.list(), local_files(root)
        def loaded(data):
            self.client, self.shelf_manager, self.root_path, self.info, self.entries, self.shelves, file_data = data
            self.all_files, truncated = file_data
            self.path_var.set(str(self.root_path))
            self.workspace_name.configure(text=self.root_path.name)
            self.settings["last_workspace"] = str(self.root_path)
            self._save_settings()
            self._repo_url = ""
            self.repo_var.set("选择“仓库浏览”加载当前仓库目录。")
            self.repository.delete(*self.repository.get_children())
            self.logs = []
            self._render_history()
            self._render_all(truncated)
            self._set_busy(False)
            self._show_preview("工作副本信息", self._info_text())
            self.select_tab(0)
        self._run("打开工作副本", load, loaded)

    def refresh(self):
        if self.demo:
            self._load_demo()
            return
        if not self.client or self._busy:
            return
        client, shelves, root = self.client, self.shelf_manager, self.root_path
        def load():
            return client.info(), client.status(), shelves.list(), local_files(root)
        def loaded(data):
            self.info, self.entries, self.shelves, file_data = data
            self.all_files, truncated = file_data
            self.logs = []
            self._render_history()
            self._render_all(truncated)
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
        self.summary_var.set(f"{len(self.entries)} 个状态条目  ·  {conflicts} 个冲突  ·  工作副本修订 r{self.info.get('revision', '?')}")
        self._render_changes()
        self._render_files()
        self._render_shelves()
        self.files_note.configure(text=f"本地文件：{len(self.all_files)} 个" + ("（仅显示前 3000 个）" if truncated else "  ·  隐藏 .svn"))

    def _render_changes(self):
        selected = {self._change_lookup[i].path for i in self.changes.selection() if i in self._change_lookup}
        self.changes.delete(*self.changes.get_children())
        self._change_lookup = {}
        needle = self.filter_var.get().casefold()
        for index, entry in enumerate(self.entries):
            label = status_label(entry)
            if needle and needle not in entry.path.casefold() and needle not in label.casefold():
                continue
            tag = "conflict" if entry.item == "conflicted" or entry.tree_conflicted or entry.props == "conflicted" else entry.item
            iid = f"change{index}"
            self.changes.insert("", "end", iid=iid, values=(label, entry.path, entry.props if entry.props != "none" else "", entry.revision or "—"), tags=(tag,))
            self._change_lookup[iid] = entry
            if entry.path in selected:
                self.changes.selection_add(iid)

    def _render_files(self):
        self.files.delete(*self.files.get_children())
        self._file_lookup = {}
        statuses = {e.path: status_label(e) for e in self.entries}
        for index, (path, size) in enumerate(self.all_files):
            iid = f"file{index}"
            self.files.insert("", "end", iid=iid, values=(path, statuses.get(path, "正常 / 未扫描"), self._size(size)))
            self._file_lookup[iid] = path

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
        current = self.tabs.index(self.tabs.select())
        self.title_label.configure(text=self.tabs.tab(current, "text"))
        for index, button in enumerate(self.nav_buttons):
            button.configure(bg="#29476c" if current == index else NAV, fg="white" if current == index else "#c1cee0")
        if not self._busy and not self.demo and self.client:
            if current == 2 and not self.logs:
                self.load_history()
            elif current == 3 and not self._repo_url:
                self.load_repository(self.info.get("url"))

    def _selection_hint(self, event=None):
        if not self._busy:
            count = len(self.changes.selection())
            self.status_var.set(f"已选中 {count} 个路径 · 双击或 Ctrl+D 查看差异" if count else "就绪")

    def _selected_entries(self):
        if self.tabs.index(self.tabs.select()) != 0:
            messagebox.showinfo("选择待提交更改", "请在“待提交更改”中选中要操作的路径。可按 Ctrl / Shift 多选。", parent=self)
            self.select_tab(0)
            return []
        return [self._change_lookup[i] for i in self.changes.selection() if i in self._change_lookup]

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
        def load():
            parts = []
            tracked = [e.path for e in selected if e.item not in {"unversioned", "ignored", "external"}]
            if tracked:
                parts.append(client.diff(tracked))
            for entry in selected:
                if entry.item == "unversioned":
                    parts.append(f"\n未版本化文件：{entry.path}\n" + read_preview(root, entry.path))
            return "\n".join(parts) or "没有可显示的文本差异。二进制文件、目录或部分属性更改可能没有行级差异。"
        self._run("读取差异", load, lambda text: self._show_preview(f"选中 {len(paths)} 个路径的差异", text, diff=True))

    def preview_file(self):
        if self._busy:
            return
        selected = self.files.selection()
        if not selected:
            return
        path = self._file_lookup[selected[0]]
        if self.demo:
            self._show_preview(path, "# 示例文件预览\n# 在真实工作副本中，这里显示文件内容。\n")
        else:
            self._run("读取本地文件", lambda: read_preview(self.root_path, path), lambda text: self._show_preview(path, text))

    def load_history(self):
        if self.demo or not self.client or self._busy:
            return
        def loaded(entries):
            self.logs = entries
            self._render_history()
            self.status_var.set(f"已加载最近 {len(entries)} 条提交 · 双击查看修订差异")
        self._run("读取最近 100 条提交历史", lambda: self.client.log(limit=100, revision="HEAD:0"), loaded)

    def preview_history(self, event=None):
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
        if self.demo:
            self._show_preview(f"r{entry.revision} 差异", "演示模式：真实仓库的修订差异会显示在这里。")
            return
        self._run(f"读取 r{entry.revision} 差异", lambda: self.client.command(["diff", "-c", str(entry.revision), "--", "."]),
                  lambda result: self._show_preview(f"r{entry.revision} 差异", result.stdout or "此路径没有文本差异。", diff=True))

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
        name = simpledialog.askstring("新建本地 Shelf", f"为 {len(paths)} 个选中路径创建本地备份。\n原有修改保留在工作区；不会提交到仓库。\n\nShelf 名称：", parent=self)
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
        text += "\n\n本地 Shelf 是修改的备份，不会自动清空工作区。\n恢复要求目标路径没有本地修改，且 BASE 与创建时匹配；冲突会中止恢复。"
        self._show_preview("本地 Shelf 详情", text)

    def restore_shelf(self):
        if not self._can_operate():
            return
        shelf = self._selected_shelf()
        if shelf and messagebox.askokcancel("恢复本地 Shelf", f"将“{shelf['name']}”恢复到当前工作副本？\n\n目标路径必须干净，BASE 必须与创建时匹配。新增或未版本化文件的目标路径必须不存在。\n发现不兼容状态时会中止。Shelf 本身会保留。", parent=self):
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
        if self.demo:
            messagebox.showinfo("演示模式", "演示模式不会修改本地设置。请正常启动后设置 SVN 程序。", parent=self)
            return
        path = filedialog.askopenfilename(title="选择 SVN 命令行程序（svn.exe）", filetypes=[("SVN 程序", "svn.exe"), ("所有文件", "*")], parent=self)
        if path:
            self.settings["svn_executable"] = path
            self._save_settings()
            messagebox.showinfo("已保存", "SVN 程序路径已保存。重新打开工作副本后生效。", parent=self)

    def show_help(self):
        self._show_preview("使用说明", "1. 安装 SVN 命令行工具，打开已有工作副本。\n2. 在“待提交更改”中按 Ctrl / Shift 多选文件，双击查看差异。\n3. 先添加未版本化文件，再选择需要的更改并提交。提交说明必填。\n4. 重要修改可以创建本地 Shelf。创建只备份，不清空修改。\n5. 恢复 Shelf 前，相关路径须干净且 BASE 匹配。请先备份当前工作。\n6. 提交历史和仓库浏览需要网络连接；刷新本地状态不需要。\n\n快捷键：Ctrl+O 打开工作副本 · F5 刷新 · Ctrl+D 查看选中差异\n\n安全提示\n• 还原会丢弃未提交更改，删除 Shelf 会永久删除备份。\n• 本地 Shelf 不是服务器备份；不会同步到其他电脑。\n• SVN 身份验证由安装的 SVN 客户端处理，本程序不保存密码。\n• 更新可能产生冲突；请在外部编辑器中解决冲突后再提交。")

    def _load_demo(self):
        from types import SimpleNamespace
        self.root_path = Path("D:/Projects/OrionGame")
        self.path_var.set(str(self.root_path))
        self.workspace_name.configure(text="OrionGame\n开发工作副本")
        self.info = {"revision": "248", "url": "https://svn.example.com/OrionGame/trunk", "repository_root": "https://svn.example.com/OrionGame"}
        rows = [("src/player/controller.py", "modified", "248"), ("src/game/session.py", "modified", "246"),
                ("assets/ui/inventory.png", "added", ""), ("docs/release-notes.md", "unversioned", ""),
                ("config/input.json", "modified", "248"), ("tests/test_controller.py", "modified", "247")]
        self.entries = [SimpleNamespace(path=p, item=s, revision=r, props="none", tree_conflicted=False) for p, s, r in rows]
        self.all_files = [(p, 4100 + i * 1280) for i, (p, _, _) in enumerate(rows)] + [("README.md", 1240), ("src/game/scene.py", 6200)]
        self.logs = [SimpleNamespace(revision=248-i, author="developer" if i % 2 == 0 else "teammate",
                                    date=f"2026-11-{3 if i < 2 else 2:02d}T09:{40-i*6:02d}:00Z",
                                    message=message, changed_paths=[{"action": "M", "path": "/trunk/src/game/session.py"}])
                     for i, message in enumerate(["优化游戏启动流程与资源加载", "添加控制器输入测试", "整理 UI 图标资源", "修复场景切换状态"])]
        self.shelves = [{"id": "demo-shelf-01", "name": "控制器手感调整 · 实验 A", "created_at": "2026-11-03T08:32:00Z", "entry_count": 2,
                         "paths": ["src/player/controller.py", "tests/test_controller.py"]}]
        self._render_all()
        self._render_history()
        self.changes.selection_set("change0")
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
