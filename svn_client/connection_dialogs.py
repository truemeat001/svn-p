"""Explicit local/remote diagnostic controls with no credential entry or persistence."""
from __future__ import annotations

import json
import re
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit
import uuid

from .connection import ConnectionReport, validate_probe_url
from .theme import text_options


SAFE_FAILURE = "检查未能完成。未显示原始错误内容，以免泄露认证信息；请在受信任的 SVN 客户端核查。不会自动重试。"
INVALID_URL = "请输入 http、https、svn 或本机 file URL；不能包含用户名、密码、查询参数、fragment 或控制字符。不支持 SSH。输入不会保存。"
CATEGORY_TEXT = {
    "ok": "该步骤成功。",
    "dependency_missing": "未找到 SVN 程序；请先安装可信命令行客户端，或在文件菜单选择已安装的程序。",
    "dependency_failed": "先前的程序检查失败，此步骤未开始。",
    "process_error": "程序无法启动，或本地目录已不可用；请核对安装与路径。",
    "working_copy": "无法读取工作副本或格式；请在可信 SVN 客户端确认所选目录。",
    "authentication": "现有 SVN 配置无法完成认证。请在可信 SVN 客户端配置账号后再主动检查。",
    "authorization": "服务器报告访问权限问题；请与仓库管理员确认账号权限。",
    "tls": "TLS / 证书校验失败。请核对服务器证书与可信配置；不会忽略、接受或绕过证书错误。",
    "network": "网络或传输失败；请核对地址、连接与代理配置。",
    "timeout": "该 SVN 进程超时。未自动重试；不表示服务器一定没有收到请求。",
    "not_found": "目标或仓库未找到；部分服务器也可能用此结果隐藏权限不足。",
    "connection": "未建立可用的仓库会话；错误码无法可靠区分网络、代理、认证和服务器原因。",
    "svn_error": "SVN 未能完成检查；暂不能识别根因，请另用可信 SVN 客户端核查。",
    "invalid_response": "SVN 返回的身份元数据不完整、过大或不安全，未展示原始内容。",
    "identity_mismatch": "本地与所查 URL 的仓库 UUID 不同；没有切换、重定位或修改副本。",
}


def quoted(value, limit=32768):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("诊断字段不可显示。")
    return json.dumps(value, ensure_ascii=False)


def safe_identity_lines(identity, *, local=False):
    """Validate public metadata again; never render arbitrary process fields."""
    def safe_url(value):
        if local and isinstance(value, str) and value.startswith("svn+ssh://"):
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("诊断 URL 不可显示。")
            validate_probe_url("svn://" + value[len("svn+ssh://"):])
            return value
        return validate_probe_url(value)
    if type(identity.revision) is not int or identity.revision < 0 or identity.kind not in {"file", "dir"}:
        raise ValueError("诊断身份不可显示。")
    canonical_uuid = str(uuid.UUID(identity.repository_uuid))
    return ["URL：" + quoted(safe_url(identity.url), 8192),
            "仓库根：" + quoted(safe_url(identity.repository_root), 8192),
            "UUID：" + canonical_uuid, f"修订：r{identity.revision}；节点：{identity.kind}"]


def report_text(report, *, remote_requested):
    """Produce an allowlisted, bounded report; messages/warnings are not echoed."""
    if not isinstance(report, ConnectionReport) or type(remote_requested) is not bool:
        raise ValueError("诊断报告类型不可显示。")
    if type(report.remote_requested) is not bool or report.remote_requested != remote_requested:
        raise ValueError("诊断报告与请求不一致。")
    if type(report.remote_attempted) is not bool or (not remote_requested and (report.remote_attempted or report.remote is not None)):
        raise ValueError("本地检查报告出现非预期远程结果。")
    if len(report.steps) > 8 or type(report.ok) is not bool:
        raise ValueError("诊断步骤不可显示。")
    lines = ["仅本地检查（未请求远程访问）" if not remote_requested else "显式 URL 检查（包含本地程序检查）",
             "总体：请求的步骤成功。" if report.ok else "总体：部分步骤失败或未执行，请逐项核对。"]
    if not remote_requested:
        lines.append("本地成功不代表服务器已连接；此报告没有检验服务器。")
    else:
        lines.append("远程阶段：已尝试启动。不能据此断言服务器收到请求。" if report.remote_attempted else "远程阶段：未开始，未尝试访问所填 URL。")
    stages = {"executable": "SVN 程序", "working_copy": "本地工作副本", "remote": "显式 URL", "identity": "仓库 UUID 比较"}
    statuses = {"ok": "成功", "failed": "失败", "skipped": "跳过"}
    for step in report.steps:
        if step.stage not in stages or step.status not in statuses:
            raise ValueError("诊断阶段不可显示。")
        if not remote_requested and step.stage in {"remote", "identity"}:
            raise ValueError("本地检查出现非预期阶段。")
        description = CATEGORY_TEXT.get(step.category, CATEGORY_TEXT["svn_error"])
        lines.extend(["", f"{stages[step.stage]}：{statuses[step.status]}", description])
        if step.error_codes:
            if len(step.error_codes) > 16 or not all(isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code) for code in step.error_codes):
                raise ValueError("诊断错误码不可显示。")
            lines.append("SVN 数字错误码：" + ", ".join(step.error_codes))
        if step.returncode is not None:
            if type(step.returncode) is not int or not -(2 ** 32) <= step.returncode <= 2 ** 32:
                raise ValueError("诊断退出码不可显示。")
            lines.append(f"退出码：{step.returncode}")
    if report.executable:
        lines.extend(["", "SVN 程序路径：" + quoted(report.executable)])
    if report.version:
        if not isinstance(report.version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", report.version) or len(report.version) > 100:
            raise ValueError("诊断版本不可显示。")
        lines.append("SVN 版本：" + report.version)
    if report.working_copy is not None:
        local = report.working_copy
        lines.extend(["", "本地身份（仅本地记录）", "所选目录：" + quoted(local.path),
                      "实际工作副本根：" + quoted(local.working_copy_root), *safe_identity_lines(local.repository, local=True)])
    if report.remote is not None:
        lines.extend(["", "所填 URL 在 HEAD 的身份", *safe_identity_lines(report.remote)])
    if report.repository_identity_matches is not None:
        if type(report.repository_identity_matches) is not bool or report.working_copy is None or report.remote is None:
            raise ValueError("诊断身份比较不可显示。")
        lines.extend(["", "仓库 UUID 相同不证明 URL、分支、内容或修订一致。" if report.repository_identity_matches else "仓库 UUID 不同；本程序没有修改本地映射。"])
    lines.extend(["", "边界", "• 成功只证明所检查目标当时可读取，不证明提交、加锁、检出、hooks 或其他目录权限。",
                  "• 每个 SVN 进程超时 15 秒，最多三个进程；不是整个报告的总时限。",
                  "• 不保存 URL 或报告，不显示原始 stderr / XML / 认证内容；本报告含路径和仓库身份，请谨慎分享截图。",
                  "• 不写认证缓存、不改变证书或代理配置、不自动重试。"])
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > 65536:
        raise ValueError("诊断报告超过显示上限。")
    return text


def remote_confirmation(url):
    target = validate_probe_url(url)
    return ("仅对以下确切 URL 执行一次只读 info（HEAD）：\n\n" + quoted(target, 8192)
            + "\n\n可能使用 SVN 既有凭据，服务器可能记录访问；不输入、传递或保存新凭据。"
              "file:// 检查本机仓库，也需要此次明确同意。\n"
              "URL 路径中的 @123 按字面名称处理，不代表历史版本。\n"
              "不会检出、更新、提交或写仓库，不绕过 TLS / 证书校验。\n\n继续此次检查？")


class ConnectionDialog(tk.Toplevel):
    def __init__(self, parent, context, request):
        super().__init__(parent)
        self.request = request
        self.context = context
        self.closed = False
        self.generation = 0
        self.running = False
        self.title("连接诊断 · 只读")
        self.transient(parent)
        self.display = parent.display
        width = min(self.display.px(860), max(320, self.winfo_screenwidth() - 40))
        height = min(self.display.px(730), max(300, self.winfo_screenheight() - 80))
        self.geometry(f"{width}x{height}")
        self.minsize(min(width, self.display.px(620)), min(height, self.display.px(520)))
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="先检查本地，再明确选择是否访问仓库", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(body, text="打开此窗口不会执行检查。没有工作副本时，本地检查只读取 SVN 版本。无需填写账号或密码。",
                  wraplength=self.display.px(780)).pack(anchor="w", pady=(8, 10))
        self.local_button = ttk.Button(body, text="本地检查（不连接服务器）", command=self._local)
        self.local_button.pack(anchor="w")
        remote = ttk.LabelFrame(body, text="可选：明确 URL 检查", padding=10)
        remote.pack(fill="x", pady=12)
        self.url_var = tk.StringVar(value="")
        self.opt_in_var = tk.BooleanVar(value=False)
        self.url_entry = ttk.Entry(remote, textvariable=self.url_var)
        self.url_entry.pack(fill="x")
        self.opt_in = ttk.Checkbutton(remote, text="我明确选择检查所填 URL；可能使用已有 SVN 凭据并产生访问日志。",
                                      variable=self.opt_in_var, command=self._update_controls)
        self.opt_in.pack(anchor="w", pady=8)
        self.remote_button = ttk.Button(remote, text="核对 URL 并检查…", command=self._remote, state="disabled")
        self.remote_button.pack(anchor="w")
        ttk.Label(remote, text="不支持账号 / 密码 URL、查询参数和 SSH。不会自动采用工作副本 URL，不保存输入。",
                  wraplength=self.display.px(750), style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.url_var.trace_add("write", self._input_changed)
        self.note_var = tk.StringVar(value="尚未检查。")
        ttk.Label(body, textvariable=self.note_var, style="Muted.TLabel", wraplength=self.display.px(780)).pack(fill="x", pady=(0, 8))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        self.output = tk.Text(frame, wrap="none", height=16, relief="flat", font=self.display.font(), **text_options())
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.output.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.output.xview)
        self.output.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.output.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        scope = "当前目录：" + quoted(context.working_copy) if context.working_copy else "未打开工作副本：仅检查 SVN 程序。"
        self.show_text(scope + "\n\n远程检查需填写确切 URL、勾选同意并再次核对。\n结果仅保留在本窗口，不会写入日志或设置。")
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        cancel = ttk.Button(buttons, text="关闭", command=self._close)
        cancel.pack(side="right")
        self.bind("<Escape>", lambda event: self._close())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()
        cancel.focus_set()
        self.wait_window()

    def show_text(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _input_changed(self, *_):
        if not self.running:
            self.generation += 1
            self.opt_in_var.set(False)
        self._update_controls()

    def _update_controls(self):
        available = not (self.running or self.closed)
        self.local_button.configure(state="normal" if available else "disabled")
        self.url_entry.configure(state="normal" if available else "disabled")
        self.opt_in.configure(state="normal" if available else "disabled")
        self.remote_button.configure(state="normal" if available and self.opt_in_var.get() and self.url_var.get() else "disabled")

    def _local(self):
        if not (self.running or self.closed):
            self.request(self, None)

    def _remote(self):
        if self.running or self.closed or self.opt_in_var.get() is not True:
            return
        try:
            url = validate_probe_url(self.url_var.get())
            confirmation = remote_confirmation(url)
        except (ValueError, TypeError):
            # Do not quote invalid input, and clear it if credentials were pasted.
            self.url_var.set("")
            messagebox.showwarning("URL 不可用", INVALID_URL, parent=self)
            return
        generation = self.generation
        if not messagebox.askyesno("确认一次只读 URL 检查", confirmation, default="no", parent=self):
            return
        if self.closed or self.running or self.generation != generation or self.url_var.get() != url or self.opt_in_var.get() is not True:
            return
        self.request(self, url)

    def begin(self):
        self.generation += 1
        self.running = True
        self.opt_in_var.set(False)
        self.note_var.set("正在检查。可关闭窗口；已经开始的进程会继续至完成 / 超时，不会自动重试，结果不会保存。")
        self._update_controls()
        self.show_text("等待此次只读检查结果…")
        return self.generation

    def finish(self, text, *, stale=False):
        self.running = False
        self.note_var.set("上下文已变化，此次结果已丢弃。请关闭后重新打开诊断。" if stale else "本次检查结束。再次访问 URL 仍需重新勾选和确认。")
        self.show_text(text)
        self._update_controls()

    def _close(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self.destroy()
