# 冲突检查与显式解决（P0）

这版提供冲突检查和明确确认后的 `svn resolve --accept working`：**只支持保留已手工修复、保存并核对的普通文件当前内容**。属性、混合及树冲突，以及选择 base/mine/theirs 整份版本，均需外部可信 SVN 客户端处理。没有自动合并、三方编辑器、冲突标记编辑或属性编辑器。真实 SVN 和原生 Windows 尚未在本次环境验证；请先在一次性测试仓库使用。

## 安全流程

1. 查看冲突列表，逐个检查类型、工作副本和允许的选项。
2. 独立备份工作文件及所有冲突辅助文件（例如 `.mine`、基础及传入版本文件）。需要手工合并时，用可信编辑器修复、保存并检查工作文件。
3. 运行预检，核对确切路径、解决方式、风险提示和状态指纹。
4. 明确确认后执行。完成后重新读状态，检查文件内容并运行项目测试，再决定是否提交。

**“已解决”只是 SVN 不再阻止该节点提交，不证明内容正确，也不会自动提交。** SVN 的解决命令会清除冲突记录及相关临时文件；即使你单独编辑过某个辅助文件，该文件也可能被删除。因此必须先备份，且不能并行编辑。[SVN Book：resolve](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.resolve.html)

## CLI 示例

在 SVN Desk 项目目录运行；Windows 可将 `python` 换成 `py -3`。

```console
python -m svn_client.cli --workspace /wc conflict list
python -m svn_client.cli --workspace /wc conflict list --path src
python -m svn_client.cli --workspace /wc conflict inspect --path src/a.txt
python -m svn_client.cli --workspace /wc conflict resolve --path src/a.txt --accept working --dry-run
python -m svn_client.cli --workspace /wc conflict resolve --path src/a.txt --accept working --expect-state-sha256 <预检返回的64位小写哈希> --yes
```

- `list` 返回 `data.conflicts`，包括文本、属性和树冲突状态；目录范围的读取可包含其子项。无路径表示当前所选工作区。
- `inspect` 必须指定一个 `--path`，读取该节点以及祖先状态，不递归展开目录。返回 `data.conflict`，其中 `allowed_accepts` 为空时不能用本工具解决，`blocked_reasons` 说明原因。
- `resolve` 必须明确提供路径及 `--accept`，并选择 `--dry-run` 或 `--yes`。缺少确认会在调用后端前拒绝。可重复 `--path`，或用 `--paths-json FILE|-`；单次最多 128 个输入路径。
- 所有目标完整预检后，才会发出一次 `resolve --accept working --depth empty --` 命令。不允许目录目标、递归、自动推荐或隐含“全部”。
- 这些子命令沿用 [CLI JSON v1 契约、退出码及输出上限](CLI.md)。`--yes` 只是这次调用的执行开关，不能替代 AI 调用者应获得的用户授权。

## 唯一启用选项：working

`working` 接受普通文件的当前内容。**必须先手工修复、保存并核对**。本工具计算完整内容 SHA-256 来发现审核后变化，但不做合并正确性判断，不扫描标记来宣称问题已修复。

SVN 的内容冲突状态也可能对应二进制文件，并不证明文件是可读文本。本流程可保留已由用户独立检查的二进制工作文件字节；不会将它解释为文本或猜测编码。

`base`、`mine-full`、`theirs-full` 在此 P0 中均拒绝，因为它们可能使用基础、传入、本地冲突辅助文件，而本版尚未可靠绑定这些实际输入。属性及混合冲突也全部拒绝，因为当前属性值和属性冲突输入尚未完整校验。不要把这些限制理解为自动选择了当前属性或某一侧版本。

树冲突在 P0 中始终拒绝解决，需要在可信 SVN 客户端中先理解移动、删除、替换等结构变化。Subversion 的树冲突处理可能涉及历史查找和其他节点变化，不适合冒充“保留我的文件”这样简单的动作。[Apache Subversion：冲突解决器说明](https://subversion.apache.org/docs/release-notes/1.10.html#conflict-resolver)

## 边界与复核

- 拒绝 URL、越界路径、`.svn` 管理路径、控制字符、Windows 保留名/流及其他不受支持的跨平台路径形式、符号链接和 Windows 重解析点，以及不受支持的缺失/替换/复制/移动/切换/外部引用或锁定状态。
- 目标及祖先存在树冲突或结构异常时拒绝。每个目标的工作副本根和仓库 UUID 必须与当前客户端一致。嵌套或外部工作副本必须独立打开、独立检查和操作，不跨仓库拼接提交或解决。
- 解决冲突时，`--workspace` 必须是实际工作副本根，而不是其中的子目录，确保能检查完整祖先范围。子目录客户端仍可检查，但会提示 `select_working_copy_root`。
- 预检不修改文件、属性或冲突状态，也不创建备份，不直接编辑 SVN 管理数据。它会只读读取选中工作文件，单个文件最多 32 MiB，并仅返回摘要，不返回内容；超过上限或无法安全读取时拒绝解决。
- `state_sha256` 绑定路径、解决方式、SVN 元数据、祖先状态、文件标识/大小/时间戳，以及当前工作文件的内容 SHA-256。**它不绑定冲突辅助文件内容或属性值，也不是整个冲突状态快照或事务锁。** 辅助文件仍可能被 SVN 删除，因此先备份所有相关文件。其他程序仍可能在最后检查与 SVN 执行之间修改工作区；操作期间不要并行编辑或运行其他 SVN 工具。
- 内容读取前后检查普通文件类型、文件身份及状态。支持的平台使用逐层目录描述符及 `O_NOFOLLOW`；其他平台使用路径组件检查和打开后身份核对。不提供对恶意并发文件系统修改的跨进程隔离保证。
- 即使调用者没有传 `--expect-state-sha256`，CLI 也将本次预检指纹传入执行阶段，再检查一次。若需要绑定先前已审核的预检，请显式传入该值。
- 中断、超时或错误可能意味着批次中的部分节点已解决。CLI 返回 `operation_uncertain`，不会自动重试；先读 `status` / `conflict inspect` 并检查内容。
- SVN 返回成功后，后端会重新检查每个确切节点的非递归状态。仍有冲突或无法核实时也按不确定结果处理，不重试。冲突标志已清除仍不等于内容语义正确。
- 本模块不解析或打开 `.mine`、`.rN`、`.prej` 等冲突辅助文件，不假定这些名字或数量固定；因此不能声称指纹保护了这些文件的变化。需要三方内容比较或保留某一侧原始版本时，请使用外部可信工具。

## 后端调用

`ConflictManager(client)` 使用该 `SvnClient` 的串行锁和可信 SVN 可执行程序。

```python
manager = ConflictManager(client)
inspection = manager.inspect("src/a.txt")
plan = manager.preflight_resolve(["src/a.txt"], "working")
# 调用方在显示 plan 后，取得这次路径及选项的明确确认。
result = manager.resolve(
    plan.paths,
    plan.accept,
    confirmed=True,
    expected_state_sha256=plan.state_sha256,
)
```

`confirmed` 必须是布尔值 `True`，不会把任意非空字符串或数字当成确认。底层命令仍是非交互参数向量，不启用 shell，不启动外部编辑器或合并器。

## 测试

```console
python -m unittest discover -s tests -p test_conflicts.py -v
```

测试覆盖确认门槛、树冲突及祖先阻断、属性/混合冲突和 sidecar 版本选择拒绝、工作副本/仓库边界、链接、精确 argv、内容变化、同元数据情况下内容变化、读取期间变化、文件大小上限、JSON 结果和失败不重试。真实仓库测试在同时存在 `svn` 和 `svnadmin` 时才运行；本次环境缺少二者，该测试跳过，不能把模拟测试通过写成真实 SVN 验证通过。
