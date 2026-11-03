# 分支、标签、切换与合并后端（实验性）

本模块是 `svn_client.branches.BranchManager` 的保守后端，不代表 P4V 的完整分支集成。当前环境没有 `svn` / `svnadmin`，因此真实仓库、原生 Windows 和网络认证验证尚未完成。模拟传输测试通过不能代替真实 SVN 验收。首次使用请只在一次性测试仓库中进行。

图形入口、明确确认和结果核查见 [分支与合并 GUI 使用说明](BRANCH_GUI.md)。

## 已实现的边界

- **切换工作副本**：显式目录 URL、显式数字修订、完整工作副本根目录。只读预检后，凭同一操作的预检哈希和明确确认执行 `svn switch`，随后检查实际 URL、仓库身份、修订和干净状态。
- **合并一个向前的修订范围**：显式 `start < end`，每次最多 64 个修订，源定位为 `URL@end`，范围为 `start:end`，实际运行 SVN 的 `--dry-run`。确认后重新预检并比较哈希，再执行一次本地合并。保留原生 merge tracking，不自动提交。
- **分支 / 标签创建计划**：只读检查已提交源目录、固定修订、同仓库现有父目录以及目标名称当时不存在，返回 `CopyPlan`。**没有创建执行方法，也不会发送 `svn copy` 或 `commit`。**

不支持自动 reintegrate、全部可合并修订自动同步、反向合并、两个源 URL 合并、部分目录切换、忽略祖先关系、record-only、允许混合修订、自动解决冲突、远程删除分支、修改标签、提交或重试。不要把“计划生成成功”解释为服务器写权限、提交钩子或最终操作成功。

## 为什么分支 / 标签目前只能预检

SVN 的 URL → URL copy 会立即产生仓库提交，不是待提交文件。CLI 使用 `copy_as_child=TRUE`：若目标在检查后被其他人创建为目录，它可能把源再复制到那个目录内，改变原本核对的精确目标。仅检查“不存在”无法关闭这个时间窗口，所以本版不执行该写入。

后续启用前需要验证一种具有精确目标语义的事务接口，例如官方 `svn_client_copy7(..., copy_as_child=FALSE, ...)`，并在真实仓库测试目标竞争、认证失败、权限拒绝、超时已提交、复制历史及返回修订。不能仅追加一次存在性查询后宣称已解决。

标签本质上仍是普通 SVN 目录；不可修改需要仓库端权限或钩子保证。计划不会跟随或固定 `svn:externals`，不包含本地未提交内容，也不检查未来提交授权。

## 工作副本保护

切换、合并和当前复制预检都要求：

1. 打开真正的工作副本根目录，不是其子目录。
2. 所有版本化条目处于同一基础修订、普通计划状态；所有目录深度是 `infinity`。状态和递归 `svn info` 必须描述同一棵树。
3. 没有内容或属性修改、新增、删除、冲突、缺失、阻塞、复制/移动、已切换子树、工作副本管理锁、外部文件、未版本化文件，或被忽略文件。
4. 没有嵌套工作副本、符号链接、重解析点、硬链接文件、`svn:externals` 或 `svn:special`。源树也拒绝这些 SVN 属性。
5. 当前预览限制为 10,000 个版本化条目和 256 MiB 工作文件内容。内容逐个流式计算哈希，预检比较文件指纹、内容、元数据、状态、属性及操作参数。传入源树也会以固定修订递归列出，拒绝已知超限、缺少大小、不安全路径、重复路径或缺少目录父级的内容。仓库原始字节数与本地换行/关键字展开后的大小可能不同，合并也可能扩大目标树，因此事后检查仍可能超限并报告需要人工核对。

这是严格的首版边界，不是 SVN 本身的限制。大资产工作区、外部引用和稀疏工作区请继续使用可信 SVN 客户端。不会自动 update、cleanup、revert、移除文件或扩大范围来让预检通过。

不能仅凭 `svn status` 的 `normal` 判断文件干净：同大小且恢复修改时间的编辑可能命中 SVN 的状态缓存。每次完整工作树检查都会将本地 `BASE` 一次性导出到工作副本之外的独立临时目录，比较完整目录结构和每个文件的字节哈希，并比较当前与 `BASE` 的递归属性。导出由 SVN 处理换行及关键字展开，二进制按原始字节比较；不使用文本 diff 判断。导出完成后再次读取工作文件的内容/指纹、递归元数据、状态和属性；导出期间发生变化会拒绝，不会返回导出前的过期快照。临时目录使用后清理，系统临时路径若位于工作副本内则拒绝。预检不改写工作副本，但需要临时磁盘空间；导出或核查失败即停止，不退回仅信任状态缓存。

哈希用于识别核对之后的状态变化，不是权限凭据、仓库预约或跨进程锁。其他程序仍可能在最后检查后改写工作副本。执行期间不要编辑、编译或运行会写入同一工作副本的其他 SVN 工具。

## Python 调用契约

界面或 CLI 可先调用纯本地 `validate_branch_url(value)` 和 `validate_branch_revision(value)`，分别获得规范化 URL 和整数修订；它们不会启动 SVN 或传输 URL。验证失败抛出 `BranchError`，凭据不会回显。

```python
from svn_client.backend import SvnClient
from svn_client.branches import BranchManager

manager = BranchManager(SvnClient("/absolute/path/to/disposable-working-copy"))

plan = manager.preflight_switch(
    "https://svn.example/repo/branches/feature", revision=123
)
# 向用户展示整个根目录、目标 URL / 修订、所有 warnings 后取得确认。
result = manager.switch(
    plan.source_url, revision=plan.source_revision,
    confirmed=True, expected_state_sha256=plan.state_sha256,
)
```

`switch` 没有 SVN 原生 dry-run。`BranchPlan.svn_dry_run` 对它是 `False`，`preview_text` 为空。预检读取元数据，不模拟应用、证明共同祖先关系或预测最终冲突。实际命令保留 SVN 默认祖先检查，使用 `--ignore-externals --accept postpone`，不用 `--force`。

```python
plan = manager.preflight_merge(
    "https://svn.example/repo/branches/feature",
    start_revision=123, end_revision=130,
)
# 展示 plan.preview_text、源 / 目标、范围和 warnings，再取得确认。
result = manager.merge(
    plan.source_url,
    start_revision=plan.start_revision, end_revision=plan.source_revision,
    confirmed=True, expected_state_sha256=plan.state_sha256,
)
```

范围语义是 `start` 之后到 `end` 为止的改动；SVN 的已有 mergeinfo 可能排除已合并部分。不是“从第 start 次提交起”。即使预演没有冲突，也不能保证真正合并完整或没有冲突。识别到预演通知中的内容、属性或树冲突标志时，这个保守后端拒绝应用。

合并后的 `BranchResult.outcome` 总是 `needs_review` 或 `conflicted`，不会假装完成提交或证明所有修订均已应用。`changes` 包括普通更改和属性变化，`conflicts` 给出观察到的冲突路径。需要用户核对状态、差异、mergeinfo，运行项目测试，再另行明确提交。

原生 mergeinfo 可能让 SVN 只应用范围里的部分修订。因此不能只检查源终点：预检会逐一读取从 start 到 end 的每个源历史状态，始终使用相同的 `URL@end` 定位修订，拒绝中间状态里出现过的外部引用、特殊节点或超限/不安全树，即使这些内容在 end 前已删除。这个保守检查可能较慢，也会拒绝部分 SVN 本身可以处理的历史；不会偷偷缩小范围或退化为未经检查的合并。实际合并后再次检查本地属性。

`svn merge` 不提供 `--ignore-externals`，所以该参数**不会**被错误地传给它；本模块通过拒绝外部引用树隔离范围。源和目标都必须是相同仓库 UUID、相同根 URL / 接入端点。URL 不接受凭据、查询串、片段、`svn+ssh`、相对仓库别名或路径穿越。URL 中的 `@` 被当作文件名字符，修订必须单独提供，不接受 HEAD、BASE、日期、范围字符串或选项。

```python
plan = manager.preflight_copy(
    "https://svn.example/repo/trunk",
    "https://svn.example/repo/tags/v1",
    revision=123, kind="tag", message="Snapshot v1",
)
assert plan.execution_supported is False
assert plan.would_commit_immediately is True
# 只显示计划；本模块没有 copy/create_branch/create_tag 执行方法。
```

## 失败与结果

- `BranchError`：预检拒绝或读取失败，没有发送实际 switch / merge 应用命令。预演仍是 SVN 的只读 `merge --dry-run`。
- `BranchOperationError`：实际命令已经尝试，或其事后验证失败；`outcome="uncertain"`，`automatic_retry_safe=False`。保留现有结果，不回滚，不重试。先核对状态、URL 和历史，再决定下一步。
- 真实合并产生冲突但完成状态可读时，返回 `outcome="conflicted"`，保留用户可检查的冲突文件；不会调用 resolve、revert 或 commit。
- 使用共同 `SvnClient.command` 的 argv、非交互和不缓存认证设置。错误消息仅保留稳定的 SVN 错误代码，预览/输出的 URL 密码被隐藏。认证仍由可信客户端预先配置。
- `MAX_PREVIEW_BYTES` 限制对调用者返回的文本；通用 `SvnClient.command` 的输出仍会先捕获在内存里，这不是整个子进程输出的流式内存上限。

## 验证

```console
python -m unittest discover -s tests -p test_branches.py -v
python -m unittest discover -s tests -v
```

`test_branches.py` 同时提供模拟传输回归和 `svn` / `svnadmin` 可用时运行的真实集成测试。集成测试仅在临时目录创建独立 `file://` 仓库，覆盖固定修订切换、真实预演和本地合并、分支/标签只读计划、修改/忽略文件保护和不检出 externals。新增回归包含同大小且恢复修改时间的隐蔽编辑、CRLF/关键字展开、二进制、空目录及目录/文件属性保护。不会操作现有用户仓库。

本轮环境中真实集成测试跳过。发布功能前仍需要完成 Linux / Windows 的真实 SVN 1.14 系列测试及网络认证、中文/特殊名称、冲突和中断后的人工核对测试。

## 已核对的 SVN 语义来源

- [Apache Subversion 官方 copy API](https://subversion.apache.org/docs/api/latest/group__Copy.html)：`copy_as_child` 与精确目标语义。
- [Apache Subversion 1.14 CLI copy 源码](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/copy-cmd.c)：CLI 将 `copy_as_child` 传为 `TRUE`。
- [Apache Subversion 官方 merge API](https://subversion.apache.org/docs/api/latest/group__Merge.html)：dry-run、混合修订和 merge tracking。
- [Apache Subversion 1.14 CLI merge 源码](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/merge-cmd.c)：显式范围及 dry-run 路径。
- [SVN Book：copy](https://svnbook.red-bean.com/en/1.7/svn.ref.svn.c.copy.html)、[switch](https://svnbook.red-bean.com/en/1.7/svn.ref.svn.c.switch.html)、[merge](https://svnbook.red-bean.com/en/1.7/svn.ref.svn.c.merge.html)：命令语义参考，标注为 1.7 文档，不以此代替目标版本实测。
