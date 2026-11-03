"""Explicit whole-working-copy branch workflows; preflight then reviewed apply."""
from __future__ import annotations

from tkinter import messagebox

from .branches import (BranchError, BranchManager, BranchOperationError,
                       validate_branch_revision, validate_branch_url)
from .branches_dialogs import (BranchContext, BranchPlanDialog, BranchRequestDialog,
                               plan_text, quoted, same_workspace, validate_plan)


def validate_result(result, request, plan, context):
    """Any failure here is post-attempt, never a pre-command refusal."""
    expected_url = plan.source_url if request.operation == "switch" else plan.target_url
    expected_revision = plan.source_revision if request.operation == "switch" else plan.working_revision
    outcomes = {"switched"} if request.operation == "switch" else {"needs_review", "conflicted"}
    if (result.operation != request.operation or result.outcome not in outcomes
            or not same_workspace(result.workspace, context.workspace)
            or result.url != expected_url or result.working_revision != expected_revision
            or result.commit_performed is not False or result.automatic_retry_safe is not False):
        raise ValueError("返回的操作范围、映射、修订或结果分类不符。")
    if request.operation == "switch" and (result.changes or result.conflicts):
        raise ValueError("切换结果没有确认干净状态。")
    if request.operation == "merge" and bool(result.conflicts) != (result.outcome == "conflicted"):
        raise ValueError("合并冲突结果分类不一致。")


def result_text(result):
    lines = ["整个工作副本根：" + quoted(result.workspace), "当前 URL：" + quoted(result.url),
             f"基础修订：r{result.working_revision}"]
    if result.outcome == "switched":
        lines += ["切换后的 URL、固定修订与干净状态已被后端复核。没有提交仓库。请继续检查项目。"]
    elif result.outcome == "conflicted":
        lines += ["合并返回后观察到冲突，结果保留在本地。没有自动解决、还原或提交。", "观察到的冲突路径：",
                  *(quoted(path) for path in result.conflicts)]
    else:
        lines += ["本地合并已返回，但结果必须人工审查。没有提交仓库，也没有证明所有修订都已应用。"]
    lines += [f"观察到 {len(result.changes)} 个内容或属性变更节点。",
              "请检查状态、差异与 svn:mergeinfo，运行项目测试，再另行决定是否提交。",
              "不自动重试或回滚。", "", "观察到的变更：",
              *(f"[{entry.item}; 属性 {entry.props}] {quoted(entry.path)}" for entry in result.changes),
              "", "SVN 输出：", result.output]
    raw = "\n".join(lines).encode("utf-8", errors="replace")
    limit = 64 * 1024
    text = raw[:limit].decode("utf-8", errors="ignore")
    if len(raw) > limit:
        text += f"\n\n[结果详情预览已截断：{len(text.encode('utf-8')):,}/{len(raw):,} UTF-8 字节；请在刷新后的状态列表继续核查。]"
    return text


class BranchUiMixin:
    def _branch_context_snapshot(self):
        if not self.client or not same_workspace(self.root_path, self.client.working_copy):
            raise ValueError("请先打开并刷新实际工作副本根。")
        if not self.info.get("repository_uuid"):
            raise ValueError("缺少已读取的仓库身份，请先刷新或重新打开工作副本。")
        return BranchContext(str(self.client.working_copy), validate_branch_url(str(self.info.get("url", ""))),
                             validate_branch_revision(self.info.get("revision")), str(self.info["repository_uuid"]),
                             validate_branch_url(str(self.info.get("repository_root", ""))))

    def _branch_context_matches(self, client, context, request_id):
        if self._closing or self.client is not client or self._branch_request != request_id:
            return False
        try:
            return self._branch_context_snapshot() == context
        except (ValueError, TypeError):
            return False

    def switch_working_copy(self):
        self._begin_branch_operation("switch")

    def merge_revision_range(self):
        self._begin_branch_operation("merge")

    def preview_branch_copy(self):
        self._begin_branch_operation("copy_plan")

    def _begin_branch_operation(self, operation):
        if not self._can_operate() or operation not in {"switch", "merge", "copy_plan"}:
            return
        try:
            context = self._branch_context_snapshot()
        except (ValueError, TypeError) as error:
            messagebox.showwarning("工作副本上下文不可用", str(error), parent=self)
            return
        self._branch_request += 1
        request_id = self._branch_request
        client = self.client
        preview_request = self._preview_request
        dialog = BranchRequestDialog(self, operation, context)
        request = dialog.result
        if request is None:
            return
        if (request.operation != operation or not self._branch_context_matches(client, context, request_id)
                or self._preview_request != preview_request):
            self.status_var.set("工作区或当前对象已变化，未开始旧分支请求。")
            return
        manager = BranchManager(client)
        label = {"switch": "整个工作副本切换", "merge": "整个工作副本向前合并", "copy_plan": "只读分支 / 标签计划"}[operation]
        self._show_preview(label + "预检中", "整个副本根：" + quoted(context.workspace)
            + "\n当前映射：" + quoted(context.url) + "\n预检可能读取整个本地树并多次访问服务器。没有应用切换 / 合并或创建目录。")
        preview_request = self._preview_request
        def current():
            return self._branch_context_matches(client, context, request_id) and self._preview_request == preview_request
        def preflight():
            if operation == "switch":
                return manager.preflight_switch(request.url, revision=request.revision)
            if operation == "merge":
                return manager.preflight_merge(request.url, start_revision=request.start_revision, end_revision=request.revision)
            return manager.preflight_copy(request.url, request.destination_url, revision=request.revision,
                                          kind=request.kind, message=request.message)
        def failed(error):
            if not current():
                return
            text = ("整个副本根：" + quoted(context.workspace) + "\n\n预检未通过，没有发送实际切换 / 合并应用或服务器复制命令。"
                    "合并预检可能已运行只读 dry-run。不会自动清理、更新或还原来通过检查。\n\n" + str(error))
            self._show_preview(label + "预检被拒绝", text)
            messagebox.showwarning("分支预检未通过", text, parent=self)
            if self.client is client:
                self.refresh(reset_navigation=True)
        def checked(plan):
            if not current():
                if self.client is client and self._branch_request == request_id and not self._closing:
                    self.status_var.set("分支预检结果已因对象或工作区变化而丢弃；没有应用操作。")
                return
            try:
                validate_plan(request, plan, context)
                text = plan_text(request, plan, context)
            except (ValueError, TypeError, AttributeError) as error:
                failed(error)
                return
            self._show_preview(label + "预检（尚未应用）", text)
            reviewed_preview = self._preview_request
            confirmation = BranchPlanDialog(self, request, plan, context)
            # The copy-plan path has no callable mutation branch, even if a
            # faulty dialog falsely claimed confirmation.
            if operation == "copy_plan" or not confirmation.confirmed or plan.preview_has_conflicts:
                return
            if (not self._branch_context_matches(client, context, request_id)
                    or self._preview_request != reviewed_preview):
                self.status_var.set("确认期间工作区或当前对象已变化，未执行切换 / 合并。")
                return
            self._apply_branch_plan(manager, client, context, request_id, request, plan)
        self._run(label + "只读预检（可能较慢）", preflight, checked, on_error=failed, preserve_view_on_error=True)

    def _apply_branch_plan(self, manager, client, context, request_id, request, plan):
        if request.operation not in {"switch", "merge"} or plan.preview_has_conflicts:
            return
        label = "切换整个工作副本" if request.operation == "switch" else "应用本地合并（不提交）"
        preview_request = self._preview_request
        scope = ("整个副本根：" + quoted(context.workspace) + "\n原映射：" + quoted(context.url)
                 + "\n源 / 目标目录：" + quoted(request.url) + f"\n固定修订：r{request.revision}"
                 + (f"；合并范围 ({request.start_revision}, {request.revision}]" if request.operation == "merge" else ""))
        def execute():
            options = dict(confirmed=True, expected_state_sha256=plan.state_sha256)
            if request.operation == "switch":
                result = manager.switch(request.url, revision=request.revision, **options)
            else:
                result = manager.merge(request.url, start_revision=request.start_revision, end_revision=request.revision, **options)
            try:
                validate_result(result, request, plan, context)
            except (ValueError, TypeError, AttributeError) as error:
                raise BranchOperationError("应用命令已返回，但结果范围或状态无法完整核对。保留已有结果，请先检查实际工作副本。") from error
            return result
        def completed(result):
            try:
                text = result_text(result)
                self._append_log(label + "结果：\n" + text)
                if self._branch_context_matches(client, context, request_id) and self._preview_request == preview_request:
                    self._show_preview("合并存在冲突，需要人工处理" if result.outcome == "conflicted" else label + "结果需核查", text)
                if result.outcome == "conflicted":
                    messagebox.showwarning("合并后观察到冲突", scope + f"\n\n观察到 {len(result.conflicts)} 个冲突路径，结果已保留。"
                                           "请检查刷新后的状态和文件，未自动解决或提交。", parent=self)
            finally:
                # A display error must never leave the previous branch's cached
                # mapping or file scope looking valid after a whole-WC attempt.
                if self.client is client and not self._closing:
                    self.refresh(reset_navigation=True)
        def failed(error):
            try:
                refused = isinstance(error, BranchError)
                title = "执行前复核拒绝" if refused else "切换 / 合并结果不确定"
                detail = ("实际应用命令未开始。请重新读取、预检和核对，不自动重试。" if refused else
                          "命令已经尝试或结果无法验证。部分或全部内容可能已改变，不等于回滚。请先检查实际 URL、修订、状态和内容；不会自动重试。")
                text = scope + "\n\n" + detail + "\n\n" + str(error)
                self._append_log(title + "：\n" + text)
                if self._branch_context_matches(client, context, request_id) and self._preview_request == preview_request:
                    self._show_preview(title, text)
                messagebox.showwarning(title, text, parent=self)
            finally:
                if self.client is client and not self._closing:
                    self.refresh(reset_navigation=True)
        self._run(label, execute, completed, on_error=failed, preserve_view_on_error=True)

    def _invalidate_branch_views(self):
        """Invalidate logical scope before touching potentially failing widgets."""
        self._workspace_state_unknown = True
        self.info = {}
        self.entries, self.changelist_candidates, self.all_files, self.shelves = [], [], [], []
        self._repo_url = ""
        self._tree_generation += 1
        for state in (self._workspace_nodes, self._workspace_cache, self._workspace_more,
                      self._repository_nodes, self._repo_entries, self._change_lookup,
                      self._group_lookup, self._file_lookup, self._file_kind):
            state.clear()
        self._workspace_pending.clear()
        self._folder_rows, self._folder_scope, self._folder_offset = None, None, 0
        self.summary_var.set("工作副本状态未知，尚未重新读全；不能据此判断干净或无冲突。请刷新或重新打开并核查。")
        self._clear_workspace_selection()
        for tree in (self.workspace_tree, self.repository_tree, self.repository):
            tree.delete(*tree.get_children())
        self.repo_var.set("整个工作副本操作后正在重新读取映射。刷新失败时请重新打开，不使用旧目录缓存。")
        self._reset_history_state(reset_form=True)
        self._render_all()
