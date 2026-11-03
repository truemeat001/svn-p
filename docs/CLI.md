# SVN Desk CLI：给脚本与 AI 的 JSON 接口

CLI 和桌面界面使用同一套后端，但不依赖 tkinter。仅使用 Python 标准库，运行时仍需要可信来源安装的 `svn` 命令行程序；本工具不下载、不安装、不替代 SVN。读取、校验和删除已有本地 Shelf 不要求安装 SVN。

入口：

```console
python -m svn_client.cli --workspace /absolute/path/to/working-copy status
```

Windows 可使用：

```console
py -3 -m svn_client.cli --workspace "C:\Work\Project" status
```

请在 SVN Desk 项目目录运行，或自行将其加入 Python 模块搜索路径。除 `diagnostics` 外，`--workspace` 必须明确提供并指向已有本地目录；建议始终选择相同的工作副本根目录。`diagnostics` 可不带工作副本，仅检查 SVN 安装，也可报告无效工作副本。相对目标路径相对于工作副本，而非终端当前目录。全局参数放在命令之前，命令参数放在对应子命令之后。

## 设计边界

- 一次调用执行一个命令，标准输出仅包含一行 JSON；帮助和可处理的错误也使用 JSON。正常情况下标准错误为空。不使用终端确认、密码提示或图形对话框。
- 所有修改都要求 `--yes`，或用 `--dry-run` 只做预检；两个选项不能同时出现。缺少确认时，在调用后端前返回退出码 `3`。只读命令不接受这两个选项。
- `--yes` 是调用者明确执行这次操作的标记，不是用户授权系统。AI 调用方仍必须核对用户给出的权限、目标和操作范围。
- 不提供通用 SVN 命令执行器、shell 命令、密码参数、账号登录、证书绕过、凭据存储或外部 diff/editor。SVN 使用现有认证缓存，始终带有非交互及不新增认证缓存参数。请提前在可信 SVN 客户端完成正常认证。
- 本地目标不能越过工作副本边界，普通路径参数不接受 URL。分支流程只在专用 URL 参数中接受经过验证的同仓库目录 URL。路径通过参数向量传给 SVN，不进行 shell 展开。更严格的 changelist/Shelf 检查还会拒绝不支持的链接、外部引用和节点状态。
- `log` 和指定仓库版本的 `diff` 可能访问仓库网络。创建 Shelf 只是本地备份，**不会清空工作区、上传或提交**；恢复保留 Shelf。
- 无自动重试、无限循环或后台监控。外部编辑器仍可并发改变文件；CLI 不是隔离沙箱或跨所有进程的事务系统。

## 可用命令

### 只读连接诊断：默认不访问仓库

```console
python -m svn_client.cli diagnostics
python -m svn_client.cli --workspace /wc diagnostics
python -m svn_client.cli diagnostics --check-remote --url https://svn.example/repo/trunk
python -m svn_client.cli --workspace /wc --timeout 10 diagnostics --check-remote --url https://svn.example/repo/trunk
```

- 不带 `--workspace`：默认只检查已安装 SVN 的版本。提供工作副本后，再读取该目录的本地身份；**不会跟随其中保存的仓库 URL**。这里的“本地检查”指不发起仓库请求；用户明确选择的工作副本或程序若位于 SMB/挂载存储，路径解析和读取仍可能访问该文件系统。
- 远程检查必须同时提供 `--check-remote` 和明确的 `--url`。只给其一会在程序检查前拒绝，不从工作副本、历史或其他参数推测目标。显式远程检查也不要求已有工作副本；若提供的本地目录无效，可继续检查用户明确给出的独立 URL。
- 仅支持无 userinfo 的 `http`、`https`、`svn` 和本地 `file` URL。拒绝密码、仅用户名、SSH、远程 file 主机/UNC URL、查询串、片段、控制字符和不安全转义。URL 路径中的 `@123` 当作名称字面内容，不当作修订；实际检查使用 HEAD。
- 命令始终只读，不接受 `--yes`、`--dry-run`、`--force`、密码、信任证书或自动重试选项。不修复认证、不更新或检出、不试提交、不增加凭据缓存。现有 SVN 认证配置可能用于用户明确请求的远程检查，该请求可能出现在服务器日志中。
- 诊断默认每个 SVN 子进程 **15 秒**，可用全局 `--timeout` 指定大于 0、不超过 120 秒的值；其他命令仍默认 120 秒。一次报告最多包含版本、本地 info、远程 info 三步，不是整个报告的总时限，不覆盖所有进程创建或认证设施行为。

未触发整体输出上限时，报告保留在 `data.report`，包括 `steps`、已识别版本/可执行程序、可选本地和远程仓库身份，以及三个关键字段：

- `remote_requested`：是否明确要求远程检查。
- `remote_attempted`：是否开始远程检查阶段，不证明服务器实际收到请求。
- `repository_identity_matches`：两边身份均可用时比较 UUID；相同不证明分支 URL、本地内容或修订一致，不同不会自动 switch/relocate。

每个步骤有 `stage`、`status`（ok/failed/skipped）、`category`、固定说明、数字 SVN 错误码和可用时的退出码。诊断失败返回 **退出码 10、顶层 `ok: false`、`error.code: diagnostics_failed`**，但仍保留完整 `data.report`；例如缺少 SVN、TLS/认证失败、超时或 UUID 不一致。参数拒绝仍使用既有退出码 2/4；整体 JSON 超限时改为退出码 9 / `output_limit`，报告可能省略，原失败码保留在 `error.details.original_error_code`。不要把未请求远程检查时的 `ok: true` 解释为“服务器已连接”。

`category` 是排查线索，不是确定根因。公开结果不包含原始 stdout/stderr、密码、锁令牌、证书内容或任意服务端文本；XML 有 2 MiB 捕获后解析上限，并不等同于全程流式内存限制。成功 info 仅证明当时可读取这个目标，不能证明提交、锁、检出、钩子或全部子目录权限。详细接口、认证边界和验证限制见 [CONNECTION.md](CONNECTION.md)。

### 读取状态、元数据、历史和差异

```console
python -m svn_client.cli --workspace /wc status
python -m svn_client.cli --workspace /wc status --all --path src
python -m svn_client.cli --workspace /wc info --path src/file.txt
python -m svn_client.cli --workspace /wc log --path src/file.txt --limit 20 --revision HEAD:1
python -m svn_client.cli --workspace /wc diff --path src/file.txt --path docs/readme.txt
```

- `status` 返回 `data.entries`，每项包括路径、内容/属性状态、版本、冲突、移动和变更列表信息。`--all` 也返回正常版本化项。
- `info` 返回 `data.info`。`info` 与 `log` 不给 `--path` 时默认为 `.`。
- `log` 返回 `data.entries`。默认上限 100 条，可设 1–10000 条。
- `diff` 返回 `data.diff` 文本字段对象，另有 `binary_payloads_included: false`。无 `--path` 时查看整个所选工作区；目录目标可能包含其子项。
- `--revision` 支持非负数字、`HEAD`、`BASE`、`COMMITTED`、`PREV`，或两个值组成的范围，如 `BASE:HEAD`。这版接口不接受日期修订语法。
- 差异使用后端解码后的 SVN 文本输出，不是原始字节导出；二进制文件可能只显示 SVN 的提示，不能把空文本解释为二进制内容相同。需要可靠补丁或原始文件导出时，请使用外部 SVN 工具；不要把此接口的截断差异作为补丁应用。

### 有界文件历史与显式版本比较

```console
python -m svn_client.cli --workspace /wc history --path src/file.txt --limit 100
python -m svn_client.cli --workspace /wc history --path src/file.txt --start-revision 125
python -m svn_client.cli --workspace /wc compare --path src/file.txt --from BASE --to WORKING
python -m svn_client.cli --workspace /wc compare --path src/file.txt --from 120 --to HEAD
```

- 新 `history` 命令默认 `HEAD:0`，返回 `path`、`start_revision`、`entries`、`has_more`、`next_start_revision`。有下一页时将游标用于 `--start-revision`，每页 1–1000 项；不更新本地工作副本。未指定路径时为 `.`。筛选提交按路径，提交内 `changed_paths` 包含该次提交全部路径。
- 新 `compare` 命令要求一个明确 `--path`。左侧支持 BASE/HEAD/数字，右侧另支持 WORKING。默认 BASE → WORKING。返回范围、两侧修订和带截断标记的 `diff`；二进制载荷不包含在内。WORKING 仅可在右侧，不伪造反向补丁。
- 两者不会修改工作副本或仓库，拒绝符号链接、管理目录、URL 目标和 svn+ssh 传输。原始进程输出有独立硬上限：历史 XML 4 MiB、diff 8 MiB、诊断 64 KiB；超限明确失败、不返回部分结果。`compare` 的 `--max-text-bytes` 最多 8 MiB，URL 密码遮蔽后才做显示截断。
- 旧 `log` / `diff` 接口未改变，不要把新接口的进程捕获上限套用到旧接口。具体语义、安全边界及真实 SVN / Windows 未验证事项见 [HISTORY.md](HISTORY.md)。

### 完整工作副本切换、向前合并与分支/标签计划

这是保守的实验性入口。实际切换或合并必须先读取 `--dry-run` 的 `data.preflight.state_sha256`，核对整个工作副本、URL、固定修订、预览和警告，然后同时提供 `--yes` 与 **该次已核对的** `--expect-state-sha256`。哈希必填，CLI 不会临时生成一个新哈希来代替审核。

```console
python -m svn_client.cli --workspace /wc branch switch --url https://svn.example/repo/branches/feature --revision 123 --dry-run
python -m svn_client.cli --workspace /wc branch switch --url https://svn.example/repo/branches/feature --revision 123 --expect-state-sha256 <已核对的64位小写哈希> --yes
python -m svn_client.cli --workspace /wc branch merge --source-url https://svn.example/repo/branches/feature --start-revision 123 --end-revision 130 --dry-run
python -m svn_client.cli --workspace /wc branch merge --source-url https://svn.example/repo/branches/feature --start-revision 123 --end-revision 130 --expect-state-sha256 <已核对的64位小写哈希> --yes
```

- 只接受显式非负数字修订；不接受 HEAD、BASE、日期或修订范围字符串。合并要求 `start < end`，跨度最多 64 个修订，表示 **start 之后到 end 为止**，仍受 SVN merge tracking 影响。
- `--workspace` 必须是真正的完整工作副本根目录，同一 BASE 修订，全部干净；包括被忽略或未版本化文件也会导致拒绝。不自动清理、更新、还原或扩大范围来使检查通过。
- 当前最多 10,000 个版本化条目和 256 MiB 工作文件内容；拒绝稀疏、嵌套、外部引用、切换子树、链接、已有冲突和锁等不受支持状态。源与目标必须是相同仓库 UUID 和同一根 URL 接入端点。具体限制见 [BRANCHES.md](BRANCHES.md)。
- URL 必须是无凭据的 `http`、`https`、`svn` 或 `file` 目录 URL；不接受 svn+ssh、查询串、片段、路径穿越或相对仓库别名。URL 中的 `@` 按名称字面字符处理，修订单独提供。
- `branch switch --dry-run` 是只读元数据预检，`svn_dry_run: false`，因为 SVN switch 没有原生 dry-run。它不模拟真实应用或预先证明祖先关系、冲突及最终成功。
- `branch merge --dry-run` 调用真实 SVN 的只读 `merge --dry-run`，`svn_dry_run: true`。它会检查范围内每个受限源历史状态，而非只看终点，可能产生多次网络查询。识别到预演冲突时，后端拒绝实际应用。
- `data.preflight.preview` 使用带 `truncated` 和 UTF-8 字节数的文本对象，遵守 `--max-text-bytes`。若截断影响审核，应先提高限额重新读取，不应把片段当作完整预演。后端预览另有 1 MiB 上限，普通 SVN 输出仍先缓冲到内存。
- 执行时后端重新预检并比对调用方传入的旧哈希。状态变化会明确拒绝，不能自动换新哈希后重试。哈希是过期审核检测，不是权限凭据或跨进程锁；执行期间不要同时编辑、构建或运行其他 SVN 工具。

切换验证成功时，顶层 `outcome` 为 `completed`，`data.result.outcome` 为 `switched`。合并的顶层与 `data.result.outcome` 均为 **`needs_review` 或 `conflicted`**，`data.requires_review: true`，并返回 `changes` / `conflicts`。退出码 0 和 `ok: true` 仅代表结果可读取，**不代表已经解决冲突、正确完成所有合并或提交到仓库**。必须核对状态、差异与 mergeinfo，运行项目测试，再另行授权提交。

所有这些命令都明确返回 `commit_performed: false`。实际命令或事后验证失败返回退出码 8 / `operation_uncertain`，保留局部结果，不回滚或自动重试。分支后端明确认定“尚未发送 apply 命令”的拒绝，包括执行前哈希失效，返回退出码 4 / `validation_failed` / `refused`。

分支与标签创建只有只读计划：

```console
python -m svn_client.cli --workspace /wc branch plan-copy --kind branch --source-url https://svn.example/repo/trunk --destination-url https://svn.example/repo/branches/new-feature --revision 123 --message "Plan new feature"
python -m svn_client.cli --workspace /wc branch plan-copy --kind tag --source-url https://svn.example/repo/trunk --destination-url https://svn.example/repo/tags/v1 --revision 123 --message-file planned-message.txt
```

`plan-copy` 不接受 `--yes`、`--force` 或执行模式，始终是 `dry_run: true` / `outcome: planned`，且 `execution_supported: false`、`repository_unchanged: true`。计划读取已提交源和目标父目录，不包含本地未提交内容。计划说明最多 8192 UTF-8 字节，可用 `--message-file -` 显式读取 stdin；输出只返回说明字节数，不回显全文。

`would_commit_immediately: true` 描述的是将来真正 URL → URL copy 的语义，**不是本命令已提交**。本版没有创建执行入口：SVN CLI 在目标并发出现为目录时可能将源复制进该目录，无法保证已核对的精确目标，因此不发送 copy。标签也只是普通 SVN 目录，并非天然不可修改。完整原因、限制和真实 SVN 待验证项见 [BRANCHES.md](BRANCHES.md)。

### 仓库文件锁

```console
python -m svn_client.cli --workspace /wc lock inspect --path asset.png
python -m svn_client.cli --workspace /wc lock acquire --path asset.png --comment "修改贴图" --dry-run
python -m svn_client.cli --workspace /wc lock acquire --path asset.png --comment "修改贴图" --expect-state-sha256 <64位小写哈希> --yes
python -m svn_client.cli --workspace /wc lock release --path asset.png --dry-run
python -m svn_client.cli --workspace /wc lock release --path asset.png --expect-state-sha256 <64位小写哈希> --yes
```

- 每次明确选择 1–128 个已经提交过的普通本地文件。支持重复 `--path` 或 `--paths-json FILE|-`，不接受隐含工作区范围。目录、switched/结构变化/冲突、外部引用和链接不支持。
- `inspect` 返回当前服务器所有者、备注及本工作副本是否持有有效令牌；不输出原始令牌。相同用户名不等于同一工作副本持有锁。
- `--dry-run` 返回 `data.preflight`，包括 `files`、`comment`、`warnings`、`state_sha256`。它确实访问服务器，但不改变锁。查询逐文件执行，超时按子进程计算。
- `--expect-state-sha256` 可选但推荐；绑定准确文件、操作、备注、仓库/版本及锁状态，不绑定本地文件内容。CLI 无论如何都会把当次预检哈希传给后端执行前复检。
- 不抢锁、不破锁、不强制令牌。只释放本工作副本真正持有的当前锁。加锁备注会发给服务器，请勿包含密码或其他秘密。
- 成功结果已重新读取服务器验证。多文件锁不是原子操作；错误可能部分成功。`operation_uncertain` 的 `observed_locks`（可读取时）提供当前脱敏状态，不表示回滚。先重新查询，不自动重试。

锁会限制其他人的提交，不会禁止本地编辑。`svn:needs-lock` 可能改变本地写权限，正常提交也可能释放锁。支持范围、确认语义和当前真实 SVN 验证限制见 [LOCKS.md](LOCKS.md)。

### 本地原生变更列表

```console
python -m svn_client.cli --workspace /wc changelist list
python -m svn_client.cli --workspace /wc changelist list --name fixes
python -m svn_client.cli --workspace /wc changelist assign --name fixes --path src/a.txt --dry-run
python -m svn_client.cli --workspace /wc changelist assign --name fixes --path src/a.txt --yes
python -m svn_client.cli --workspace /wc changelist remove --path src/a.txt --yes
python -m svn_client.cli --workspace /wc changelist commit --name fixes --message "Fix parser" --dry-run
```

`assign` 可以创建含有成员的列表或移动文件到已有列表；`remove` 只清除所选文件标签，不改变内容。必须明确提供一个或多个**已版本化文件**，不支持目录、空列表或自动添加未版本化文件。`list` 的 `data.changelists` 以列表名称为键，包含修改和干净成员。

提交预检返回 `data.preflight`，包括完整成员、确切路径、`members_sha256`、是否保留标签，以及 `server_checked: false`。公共后端预检检查成员状态、已知目录依赖和移动配对；这不是服务器 dry-run，不保证认证、权限、锁、远程最新状态或服务端钩子会通过。

推荐 AI 先展示预检范围，再绑定已核对的成员哈希执行：

```console
python -m svn_client.cli --workspace /wc changelist commit --name fixes --message "Fix parser" --expect-members-sha256 <64位小写哈希> --yes
```

- `--expect-members-sha256` 可选，但推荐提交时使用。哈希绑定所选工作副本的规范绝对路径、列表名称和全部成员路径，不绑定文件内容、状态或提交说明。成员改变会拒绝。
- 不论是否使用哈希，CLI 都把执行前检查到的**全部成员，包括干净成员**传给后端 `expected_paths`。后端执行时重新检查成员，然后使用确切文件目标与 SVN 列表过滤器，避免自动扩大提交范围。
- `--keep-changelist` 保留成功提交文件的分组；未指定时遵循 SVN 的正常行为。当前版本不自动加入父目录依赖来让提交成功。
- 提交说明使用 `--message` 或互斥的 `--message-file FILE`。后者相对终端当前目录读取 UTF-8 文本，`--message-file -` 显式读取标准输入。说明必须非空、不含 NUL，且不超过 65536 UTF-8 字节。
- 输出不回显提交说明或完整 SVN argv，仅返回说明字节数和 SVN 的结果文本。仍应把日志内容视为工作区私有数据，避免发送到未经授权的第三方。

### 本地 Shelf

```console
python -m svn_client.cli --workspace /wc shelf create --name before-refactor --path src/a.txt --dry-run
python -m svn_client.cli --workspace /wc shelf create --name before-refactor --path src/a.txt --yes
python -m svn_client.cli --workspace /wc shelf list
python -m svn_client.cli --workspace /wc shelf inspect <shelf-id>
python -m svn_client.cli --workspace /wc shelf restore <shelf-id> --dry-run
python -m svn_client.cli --workspace /wc shelf restore <shelf-id> --yes
python -m svn_client.cli --workspace /wc shelf delete <shelf-id> --dry-run
python -m svn_client.cli --workspace /wc shelf delete <shelf-id> --yes
```

- `create` 要求明确路径，名称 1–200 个字符且无控制字符。选择目录可能备份其受支持改动子项，预检返回完整 `paths` 和 `entries`；确认前核对展开范围。
- `create --dry-run` 真正检查可备份状态、内容快照、BASE、属性和可移植路径。`restore --dry-run` 校验 blob、仓库身份、基础版本和目标是否干净、不冲突。成功仅代表当时的本地预检通过，实际操作会再检查。
- 只读 Shelf 操作和预检不创建 Shelf 目录、锁、manifest 或 blob，也不改变工作区内容/属性/调度。BASE 校验可能使用随后清理的系统临时文件；底层 SVN 的只读查询也可能更新自己的缓存元数据。
- `inspect` 校验 blob 完整性并返回 `data.shelf`，但**不**检查当前工作区的可恢复性；结果明确包含 `restore_eligibility_checked: false`。要判断是否可恢复，用 `restore --dry-run`。
- `delete` 是永久删除本机备份，不删除工作区内容。CLI 先验证所选 Shelf；损坏或不可读的备份会拒绝删除，请人工检查，而非反复强制删除。
- 默认存储跟随 `ShelfManager` 的用户数据目录。需要明确位置时，在命令前加 `--shelf-store /outside/wc/shelves`，该目录必须位于工作区外。后续调用须使用相同位置。改变 `--workspace`（例如改选子目录）可能对应不同默认存储。
- Shelf 的二进制文件内容保存在 blob 中，不内嵌在 JSON；属性值以 base64 表达。支持和不支持的节点类型见 [SHELVES.md](SHELVES.md)。

## 路径输入与非交互行为

支持多路径的命令为 `status`、`diff`、`changelist assign/remove` 和 `shelf create`。可重复 `--path`，或者使用互斥的 `--paths-json FILE|-`：

```json
["src/中文 @ file.txt", "assets/icon.bin", "docs/read me.txt"]
```

```console
python -m svn_client.cli --workspace /wc status --paths-json paths.json
python -m svn_client.cli --workspace /wc shelf create --name backup --paths-json paths.json --dry-run
```

- JSON 文件路径相对终端当前目录。UTF-8、无 BOM，最多 1 MiB、100000 个字符串；空数组、空路径、NUL 或越界目标均拒绝。
- 输入文件必须是普通文件；拒绝设备、目录和 FIFO。只在显式使用 `-` 时读标准输入，EOF 之前可能等待调用方；自动化应完整写入并关闭管道。其他操作不读取 stdin。
- 对以连字符开头的字面文件名使用 `--path=-name.txt`、`--path ./-name.txt` 或 JSON 路径输入。`@`、空格和 Unicode 名称不需要自行添加 SVN peg 转义。
- 无路径的只读 `status/diff` 明确代表所选工作区。修改命令不允许缺少路径用作隐含“全部”。

## JSON v1 契约

所有正常可捕获结果都使用同样的顶层字段：

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "changelist.assign",
  "workspace": "/wc",
  "dry_run": true,
  "outcome": "planned",
  "data": {
    "preflight": {"paths": ["src/a.txt"], "content_unchanged": true, "name": "fixes"}
  },
  "error": null,
  "warnings": []
}
```

- `schema_version` 固定为 `1`；调用方应检查版本并允许未来添加字段，不依赖 JSON 键顺序。
- `ok` 表示这次请求的结果可用。`command` 为 `status` 或 `changelist.commit` 等名称；帮助和尚未完成解析的请求可能为 `null`。
- `workspace` 是解析后的绝对路径，解析/确认阶段尚未求值或输出缩减时可为 `null`。
- `outcome`：`read`（只读结果）、`planned`（预检通过）、`completed`（修改调用成功返回）、`needs_review`（合并结果待审核）、`conflicted`（合并后观察到冲突，需人工处理）、`refused`（执行前拒绝）、`failed`（已知只读/预检错误）或 `uncertain`（修改已调用但未成功返回）。
- `data` 为具体命令结果，错误通常为 `null`。`diagnostics_failed` 例外：保留 `data.report` 的完整安全诊断步骤。
- `error` 非空时包含稳定 `code`、面向人的 `message`、`message_truncated` 和可为空的 `details`。脚本应分支判断 `code`，不要解析英文错误句子。
- `warnings` 为字符串数组。Shelf 预检对象内也可能有更具体的 `warnings`。
- Unicode 与控制字符在 JSON 中转义，因此即使 Windows 控制台不是 UTF-8，管道输出仍为 ASCII 安全的合法 UTF-8 JSON。客户端通过 JSON 解析即可还原 Unicode。
- 意外参数错误不回显原始 argv，常见 URL 中的密码也会隐藏。这不是通用秘密扫描器；源文件、属性、日志或服务端输出本身可能含用户数据，调用方仍需妥善处理。

错误示例：

```json
{
  "schema_version": 1,
  "ok": false,
  "command": "changelist.commit",
  "workspace": "/wc",
  "dry_run": false,
  "outcome": "uncertain",
  "data": null,
  "error": {
    "code": "operation_uncertain",
    "message": "A mutation was started and did not finish successfully. Changes may be partial or already complete; inspect status, history, and shelves before retrying.",
    "message_truncated": false,
    "details": {
      "cause": {"text": "SVN timed out", "truncated": false, "source_utf8_bytes": 13, "returned_utf8_bytes": 13},
      "automatic_retry_safe": false
    }
  },
  "warnings": []
}
```

### 输出大小和二进制边界

`--max-text-bytes` 默认 262144，限制每个差异/诊断文本字段的 UTF-8 字节数；输出格式为：

```json
{"text": "...", "truncated": false, "source_utf8_bytes": 123, "returned_utf8_bytes": 123}
```

字节计数针对后端解码及 URL 密码隐藏后的文本；截断不切开 UTF-8 字符。此字段出现在 `data.diff`、分支 `data.preflight.preview` / `data.result.output`、`data.svn.stdout/stderr` 及不确定错误的 `details.cause` 等位置。`truncated: true` 绝不代表完整差异或诊断。

`--max-output-bytes` 默认 4194304，允许 2048–67108864。整个 JSON 超限时不静默省略列表成员，而返回 `output_limit`、退出码 `9`。`details` 包含原始结果的 `original_outcome`、`original_error_code`、所需字节数和 `mutation_attempted`；`outcome` 保留原结果。

**尤其注意：如果超限发生在已完成修改之后，`ok` 为 false 但 `outcome` 仍可为 `completed`。不得为了再次获取输出而重做提交、恢复或删除。** 只读请求可缩小查询或提高限额；已经执行的修改应另行读取状态和历史。

这些可配置限额控制返回的文本/JSON，本身不是底层 SVN 输出总量或全程内存配额。新 `history` / `compare` 另有上述固定进程捕获上限，旧 `log` / `diff` 没有同等保证。大量工作副本或长历史仍应使用目标路径和 `--limit` 缩小范围。

### 退出码

| 代码 | `error.code` | 含义 |
| --- | --- | --- |
| 0 | 无 | 命令/帮助/预检结果可读取；合并仍可能需要审核或解决冲突，必须检查 `outcome` |
| 2 | `usage_error` | 参数、命令或选项格式不正确 |
| 3 | `confirmation_required` | 修改缺少 `--yes` 或 `--dry-run` |
| 4 | `validation_failed` | 路径、输入、成员、状态或恢复冲突检查失败 |
| 5 | `dependency_missing` | 无法初始化系统 SVN 客户端 |
| 6 | `svn_failed` | 只读 SVN 或执行前 SVN 预检失败 |
| 7 | `shelf_failed` | Shelf 读取、完整性或预检失败 |
| 8 | `operation_uncertain` | 修改调用未成功返回，不能认定回滚 |
| 9 | `output_limit` | 完整 JSON 超出配置大小；检查原操作结果 |
| 10 | `diagnostics_failed` | 一个或多个请求的诊断步骤失败/跳过；完整安全报告仍在 `data.report` |
| 70 | `internal_error` | 未预期内部错误，未回显堆栈或内部数据 |
| 74 | `io_error` | 输入/输出 I/O 错误；管道断开时无法返回 JSON |
| 130 | `interrupted` | 修改调用前收到中断 |

除分支后端明确保证尚未发送 apply 命令的 `BranchError` 外，一旦进入修改方法，后续错误会保守报告 `operation_uncertain`，即使后端实际在内部验证阶段拒绝。这避免把已部分完成的操作误判为可安全重试。中断修改同样为 `8`。外部强杀、进程崩溃或输出接收方关闭时可能没有 JSON；调用方必须按未知结果处理，不得以“没收到结果”为“没发生修改”。

`--timeout` 普通命令默认为 120 秒，`diagnostics` 默认为 15 秒（最大 120 秒），是**每个 SVN 子进程**的时限，不是整个 CLI 的时限。一次预检可能调用多个 SVN 子进程。调用方若设置总时限并终止 CLI，同样不能假定已发出的提交或局部恢复自动回滚。

## AI 调用示例

优先使用参数数组和 JSON 解析，不通过 shell 拼接字符串：

```python
import json
import subprocess
import sys

argv = [
    sys.executable, "-m", "svn_client.cli",
    "--workspace", "/absolute/path/to/wc",
    "changelist", "commit", "--name", "fixes",
    "--message", "Fix parser", "--dry-run",
]
result = subprocess.run(
    argv, shell=False, stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
)
response = json.loads(result.stdout.decode("ascii"))
if response["schema_version"] != 1:
    raise RuntimeError("Unsupported CLI response version")
if not response["ok"]:
    # 根据 code/outcome 分支；不要自动重试修改。
    raise RuntimeError(response["error"]["code"])
reviewed_paths = response["data"]["preflight"]["paths"]
review_hash = response["data"]["preflight"]["members_sha256"]
# 展示 reviewed_paths 并遵守调用方的授权要求后，另行传 --yes 和 review_hash。
```

帮助同样可机器读取：`python -m svn_client.cli --help`。命令帮助：`python -m svn_client.cli --workspace /wc shelf restore --help`。

## 验证状态

测试命令：`python -m unittest discover -s tests -v`。CLI 测试覆盖 JSON 格式、默认本地诊断/双参数远程选择/失败报告、非交互输入、路径边界、参数注入边界、确认门槛、真正的预检调用、完整成员哈希、分支必填审核哈希与只读复制计划、合并待审核/冲突结果、提交与恢复的不确定结果、二进制/Unicode 输出边界以及进程入口。

真实 SVN 集成测试仅使用一次性本地测试仓库，要求 `svn` 与 `svnadmin`。当前开发环境中两者不可用，因此这部分测试跳过；**没有据此声称真实仓库、Windows 原生运行、认证流程或 GUI 已通过验证**。首次使用请先在可丢弃工作副本中测试，并独立备份重要内容。
