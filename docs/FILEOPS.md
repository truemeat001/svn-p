# 安全的 SVN 文件操作（第一阶段）

本模块提供**单一工作副本内、文件级**的删除、移动/重命名和带历史复制。操作仅改变本地文件和 SVN 待提交状态，**不会提交仓库**。它不是 P4V 的服务器 changelist，也不提供目录/分支复制或跨仓库移动。

入口：`svn_client/fileops.py` 的 `FileOperationManager`、JSON CLI 的 `file` 子命令，以及桌面“文件与锁”菜单和文件右键菜单。桌面完整流程见 [文件与锁 GUI](FILE_OPERATIONS_GUI.md)；当前仅通过无显示回调/构造测试，尚未完成原生窗口验收。

独立对话框位于 `svn_client/fileops_dialogs.py`：`FileDestinationDialog(parent, operation, source, workspace).result` 返回相对目标路径或 `None`；`FileOperationConfirmDialog(parent, plan).confirmed` 默认 `False`。对话框本身不读写文件、不调用 SVN，只收集路径和展示完整预检范围。UI 调用方仍须后台预检、核对来源工作副本与操作范围，并使用已展示的状态摘要执行。

## 支持范围

| 操作 | 支持的输入 | 本地结果 |
| --- | --- | --- |
| `file delete` | 1–128 个显式选择的普通已版本化文件；内容和属性必须干净 | 删除本地文件，并登记待提交删除 |
| `file move` | 一个普通已版本化源文件，以及一个尚不存在的精确目标文件名 | 源路径待删除，目标带历史待添加；同目录移动即重命名 |
| `file copy` | 一个普通已版本化源文件，以及一个尚不存在的精确目标文件名 | 源保持原状，目标使用工作文件内容/属性带历史待添加 |

移动和复制允许源文件的普通内容/属性修改，但拒绝冲突及其他不支持的状态。目标父目录必须已经存在、已版本化且属于同一工作副本。不会自动创建父目录。

删除前会将本地 `BASE` 导出到工作副本外的隔离临时目录，与工作文件逐块计算 SHA-256 后比较。这样不仅依赖 SVN 可能缓存的时间戳状态。导出会按照 SVN 规则处理换行/关键字；如果实际字节不匹配，宁可拒绝删除。预检不修改工作副本，但会使用临时磁盘空间；这不是独立备份。

## 不支持的输入

以下情况直接拒绝，不自动缩小选择或退化成另一种操作：

- 目录、递归删除/复制/移动、工作副本根、`.svn` 管理路径。
- URL、仓库远端操作、跨工作副本/跨仓库操作，以及在工作副本子目录冒充根的调用。
- 嵌套工作副本、externals、switched 路径及父目录、符号链接或 Windows reparse point/junction。
- 文本/属性/tree conflict、SVN 工作副本锁、已有 copy/move 关系、待新增/删除/替换、缺失、未版本化或忽略文件。
- 已存在的目标文件或目录，或虽不在磁盘上但仍有 SVN 元数据的目标。
- 仅大小写不同的改名，及目标目录内大小写不敏感的名称冲突。
- Windows 保留名称、冒号/ADS、以空格或句点结尾的路径组件、控制字符等不安全/不可移植路径。
- 强制覆盖、`--force`、自动建父目录、空选择表示全工作副本、超长命令拆批执行。

本文件操作模块不提供撤销界面、分支管理或合并功能。仓库锁/解锁由独立的 [锁模块及 GUI](LOCKS.md) 提供。文件操作后如需撤销，应先检查 SVN 状态及独立备份，不能假定一次普通 revert 足以安全恢复移动两端。

## CLI 使用

在项目目录运行。以下的工作副本和文件路径只是示例，必须换成自己的路径。全局参数在 `file` 之前。标准输出始终是一个 JSON 对象，不弹出交互询问。

```sh
# 查看精确删除范围、风险和状态摘要，不删除工作文件
python -m svn_client.cli --workspace /path/to/wc file delete --path old.txt --dry-run

# 多文件选择也可用 UTF-8 JSON 数组：例如 ["a.txt", "folder/b.txt"]
python -m svn_client.cli --workspace /path/to/wc file delete --paths-json files.json --dry-run

# 同一工作副本内移动；目标必须是精确的新文件路径
python -m svn_client.cli --workspace /path/to/wc file move --source src/old.txt --destination src/new.txt --dry-run

# 带历史复制；保留源文件，目标待提交
python -m svn_client.cli --workspace /path/to/wc file copy --source src/a.txt --destination src/a-copy.txt --dry-run
```

预检返回 `data.preflight.state_sha256`。审查操作、`paths`、`destination`、`affected_paths` 和 `warnings` 后，使用 `--yes` 明确执行，并把刚审查的摘要传入。例如：

```sh
python -m svn_client.cli --workspace /path/to/wc file move --source src/old.txt --destination src/new.txt --yes --expect-state-sha256 <reviewed-64-character-sha256>
```

上面的 `<reviewed-64-character-sha256>` 是占位符，不要原样执行；替换成预检返回的 64 位小写十六进制摘要。命令行中包含空格或特殊字符的路径需要按使用的 shell 规则加引号；以 `-` 开头的参数值可以写为 `--source=-name.txt`。程序直接传 argv，不调用 shell。

- `--yes` 和 `--dry-run` 互斥。两者均未提供时，返回 `confirmation_required`（退出码 3），不启动操作。
- `--expect-state-sha256` 可用于预检或执行。摘要不匹配会拒绝，不自动确认新范围。
- 即使不提供旧摘要，执行仍会重新预检。建议人和 agent 的两步审查流程始终提供摘要。
- 输入不满足范围或已发生变化，且尚未进入执行时，返回 `validation_failed`（4）。
- 一旦进入修改回调，错误、超时、中断或执行后校验失败统一保守返回 `operation_uncertain`（8）。即使回调自己的再次预检已拒绝，CLI 也可能返回该保守状态。它不证明发生了部分修改，也不能当作“没有修改”。
- 不自动重试。先检查精确源/目标的 `status` 和 `diff`；需要判断仓库结果时再检查 `log`。输出大小限制也不能成为重做操作的理由。
- 成功结果中的 `commit_required: true` 表示还需另行明确提交。`server_checked: false` 表示未检查服务器权限、钩子、远端最新状态或完整提交依赖。

## Python API / UI 接入

```python
from svn_client.backend import SvnClient
from svn_client.fileops import FileOperationManager

files = FileOperationManager(SvnClient("/path/to/wc"))
plan = files.preflight_move("src/old.txt", "src/new.txt")
# 在调用方展示 plan.operation / affected_paths / warnings，并取得明确确认。
# 用户确认后才能执行下一行：
result = files.move(
    "src/old.txt", "src/new.txt",
    confirmed=True,
    expected_state_sha256=plan.state_sha256,
)
```

其他方法：

- `preflight_delete(paths)` / `delete(paths, confirmed=True, expected_state_sha256=...)`
- `preflight_copy(source, destination)` / `copy(source, destination, confirmed=True, expected_state_sha256=...)`

预检返回不可变 `FileOperationPlan` dataclass，包含源文件状态、仓库身份、内容/属性摘要、父目录状态、精确受影响路径和警告。不会把文件内容或属性明文写进计划。调用方应展示精确对象；删除对话框必须说明会移除本地文件且仅登记待提交删除。移动应同时展示源和目标，提示后续提交需要审查两端。

实现使用客户端工作副本锁，防止同一应用的相同根操作互相重叠。执行时会重新预检、比对摘要，再检查路径和目标不存在，最后只调用一次不带 `--force` 的 SVN 修改命令。成功后检查删除源不存在、目标为带历史待新增状态，并核对目标内容/属性摘要。不会自动提交、回滚或扩大选择。

后端 `FileOperationError` 表示修改命令前的安全拒绝。修改命令之后的验证失败，即使最初来自路径校验或文件 I/O，也统一作为 `SvnError` 报告不确定结果，不能向用户宣称“没有执行”。

## 并发和风险边界

预检是快照，**不是文件系统或 SVN 的跨进程事务锁**。外部编辑器、另一个 SVN 客户端或恶意程序仍可能在最后一次检查后改动文件/目录。

尤其是原生 `svn move` 在目标突然变成目录时可能采用“移入目录”的语义。本模块执行前拒绝所有已存在目标，并在执行后校验精确目标；但无法消除检查和 SVN 系统调用之间的竞态。请在操作期间停止编辑，不要同时运行其他 SVN 工具。发现执行后不一致将报告失败/不确定，保留现场，不做猜测性回滚。

状态摘要绑定操作、根路径、精确源/目标、内容摘要、属性摘要、文件身份/时间戳、SVN 元数据和父目录状态。它不能锁住未来状态，也不提供对其他进程的安全隔离。安全拒绝可能发生在无实质内容变化的元数据更新后，此时重新审查即可。

删除可能释放最后一个本地副本；虽然正常已提交文件通常可从 SVN 恢复，仍建议重要数据独立备份。移动/复制的 changelist 标签是否保留由 SVN 行为决定，本模块不承诺转移标签。操作后应重新分组，避免只提交移动的一端。

## 测试与验证

```sh
python -m unittest tests.test_fileops -v
python -m unittest tests.test_fileops_dialogs -v
python -m unittest discover -s tests -v
python -m compileall -q svn_client tests
```

单元测试使用独立临时文件和伪 SVN 响应，覆盖确认门槛、精确多文件范围、脏文件删除、同时间戳内容变化、属性变化、冲突/跨根/链接/目录/已有目标拒绝、状态摘要、`@`/Unicode/前导短横线参数、不确定结果及不自动重试。

真实集成测试仅使用自行创建的临时 `svnadmin` 仓库，不访问用户工作副本；需要同时安装 `svn` 和 `svnadmin`。测试包含待提交删除、`@` 文件名的移动/复制历史、本地修改及属性、换行转换和旧摘要拒绝。当前开发环境没有这两个程序，因此真实 SVN 测试跳过。**不能把单元测试通过等同于真实 SVN、Windows 文件系统或原生 GUI 已验证。**

## SVN 行为依据

- [Apache Subversion 1.14：copy 命令实现](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/copy-cmd.c)
- [Apache Subversion 1.14：move 命令实现](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/move-cmd.c)
- [Apache Subversion 1.14：delete 命令实现](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/delete-cmd.c)
- [Apache Subversion 1.14：属性列表 XML 输出](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/proplist-cmd.c)

值得单独回归的细节：`copy` 的目标会解析 peg revision，含字面 `@` 时需要空 peg 转义；`move` 只处理源 peg，目标为字面路径，不能机械附加 `@`。实现据此分别构造参数，而不是对所有命令复用同一个目标转义函数。
