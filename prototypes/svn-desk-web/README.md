# SVN Desk · Modern Web vertical prototype

独立的、可操作的现代 Web 桌面原型，保留 P4V 式工作区树、pending changelist 文件分组、明确选区、详情、差异和独立操作日志。**不是旧主程序的替换版，不是完成迁移或可发布客户端。**

最新生命周期修复为 HTTP 请求行与全部请求头增加 5 秒总时限，避免慢速连接在未执行 SVN 时长时间拖住原生窗口关闭。已开始的操作仍会等待完成，不会强杀或自动重试。见 [请求头时限修复](docs/HTTP_HEADER_DEADLINE.md)；该修复仍需重新打包及 Windows GUI 验收。

## 立即体验

- 最简单：双击 `SVN-Desk-Web-Demo.html`。这是离线打包的实际应用代码，不是截图；无安装、外网或 SVN 调用。
- 本地服务（Python 3.10+，无第三方 Python 依赖）：

```sh
python3 server.py
# Windows: py -3 server.py
# 然后打开 http://127.0.0.1:8765
```

默认和离线包始终标明“演示数据”；离线归组仅模拟当前页面。启用工作区桥接后明确区分待选工作区、真实只读状态、本地归组已启用和失败状态。默认变更列表初始折叠，选中 `PlayerController.cs` 可以查看内置示例差异、历史。其他文件没有示例差异时会明确说明，不复用虚假内容。

## 已实现

- 搜索文件/变更列表，状态与目录范围筛选；目录只筛选，不会扩大选区。
- 本地 changelist 分组/折叠、文件多选、当前可见项选择、隐藏选中项计数、完整只读审查。
- 可选单文件移动／重命名与带历史复制：不存在目标、默认关闭、预检后一次确认；结构变更须用原版可信客户端提交。
- 明确未版本化普通文件的本地 SVN 添加：独立启用，完整选区预检和一次确认，不递归、不添加父目录、不自动提交。
- 可选本地 changelist 标签移动与移回默认组：启动时明确启用，完整选区预检，再单独确认；归组不触发提交或还原；本地 Shelf 与仓库提交分别显式启用。
- 选中行详情、diff、显式单文件 WORKING/BASE 属性对照、分页文件历史和独立有界操作日志；离线模式仍明确使用示例数据。
- 文件右键菜单与 Shift+F10 / 菜单键：完整选区审查、归组、添加文件、单文件移动／复制、Shelf、提交、仓库锁/解锁，以及焦点文件差异/历史/属性；复用现有预检与确认，打开菜单不执行 SVN。
- F5 刷新、Ctrl/Cmd K 搜索、方向键浏览、空格切换选择、Esc 关闭菜单或弹窗。
- 详情/差异/日志面板显示切换，窄桌面视口布局；**最低布局宽度 520px，非手机产品。**
- 可选本地只读 status、明确单文件 diff / history 桥接，以及授权目录中的工作区发现、选择、专用偏好保存与恢复。历史与非 BASE→WORKING 的修订比较只在显式点击后访问仓库。默认没有 SVN 写能力；可选写入口是经过预检和确认的单文件本地移动／复制、本地文件添加、本地 changelist 标签、本地 Shelf 备份/恢复，以及单独启用、另行确认的明确范围仓库提交和文件锁/解锁。

## 真实工作区流程

Windows 验收发现旧版会把 W155007 + 零退出码误当空状态，配套严格后端已修正，集成提交 `5dbaff4` 的 status 修复已通过后续 Windows 真实 SVN 重测。提交 `9864460` 的 Windows 真实 SVN 验证已确认历史分页等流程，同时发现混合编码差异和 BASE→BASE 兼容问题。本轮修正已完成本地回归，**仍待新的 Windows 真实 SVN 复测**。请先阅读 [status 修正说明](docs/STATUS_DIAGNOSTICS_FIX.md) 、[只读文件工作流说明](docs/READONLY_FILE_WORKFLOWS.md) 与 [混合编码修正说明](docs/DIFF_ENCODING_FIX.md)，更新完整配套源码并重启服务。

```sh
python3 server.py --workspace "/path/to/working-copy-root" \
  --backend-source "/path/to/original-svn-desk"
# 可选 --svn 指定可信、已经安装的 SVN 原生程序。
```

工作副本必须是含 `.svn` 的已有根目录。`--backend-source` 指向可信原版源码根，复用其 `svn_client.backend.SvnClient.status` / `status_stream`。本轮为修复零退出码告警误判，更新了原后端的只读 status 完成检查；原版 Shelf 核心保持不变；新的可选 Web Shelf 适配见下文。默认刷新只执行本地 `status(verbose=False)`；启用本地归组后使用 `status(verbose=True)` 并仅显示变更项与有标签的干净成员，不加 `--show-updates`，不自动执行历史或远程查询。显式文件读取复用原版有界 `HistoryManager`；本地 `info` 验证身份后，按用户点击执行 `diff` 或 `log`。SVN 程序和目录授权范围在启动时固定，网页不能更改授权根、程序路径或执行任意命令；单文件移动／复制只接受该根内受限的相对目标文件路径。

状态结果只包含元数据。当前状态行可以显式读取单文件的有界差异或分页历史；不是任意文件浏览器，不提供整文件内容、目录差异、仓库树或持续连接状态。失败返回错误，清空结果并标记状态未知，绝不回退为示例或“干净”。5 万行上限是返回给 UI 前的拒绝阈值，不是流式 UI 或内存上限。依赖原后端完成有界 XML/超时处理。挂载网络目录的本地文件读取仍可能访问该文件系统。


### 单文件移动／重命名与带历史复制（独立启用）

使用 `--enable-file-ops` 启用工具栏和文件菜单入口。完整选区只能有一个已版本化普通文件，目标为同一工作副本中尚不存在的精确文件路径，父目录必须已版本化。打开窗口不执行 SVN；本地预检后单独确认，计划 120 秒一次有效。没有删除、覆盖、目录、自动建父目录或提交入口。

移动登记源删除和带历史目标添加；复制保留源内容与属性。原版 FileOperationManager 源码不变，通过有界适配执行与复核。失败不重试或回滚，已核实完成事实在后续状态不可用时保留。**本原型提交入口仍拒绝结构变更，需用原版可信 SVN 客户端审查并提交移动两端或复制目标；不保证标签转移。** 本轮仅合成测试，真实 SVN、Windows、浏览器和原生包装尚待验收。详见 [本地文件操作](docs/LOCAL_FILE_OPERATIONS.md)。

### 明确新文件添加到版本控制（独立启用）

使用 `--enable-adds` 显式启用工具栏和右键菜单的添加入口。仅支持 1–100 个明确未版本化普通文件，包含隐藏选区；父目录必须已版本化。预检绑定文件内容摘要和父目录 SVN 身份，随后单独一次确认。只登记本地待添加状态，不递归、不添加父目录、不提交。

本流程明确禁用 auto-props，SVN 仍可能按文件类型设置默认属性；提交前请核对属性和差异。状态/内容改变、关闭窗口或超过 120 秒需重新预检；失败可能已添加部分文件，不自动重试或回滚。已核实结果会在后续状态不可用时保留。

**d01bf15 的后续 Windows 隔离真实 SVN 验证已通过：两版 Python 各 324 通过、10 跳过；真实 SVN 行为 57 项通过，另 1 项命令审计复核通过；Node 84 项通过。GUI 仍未验收。** 该结果只验证 d01bf15，不代表后续属性切片通过。完整边界与验收步骤见 [本地文件添加](docs/LOCAL_ADDS.md)。

### 本地 changelist 分组与移回

```sh
python3 server.py --workspace "/path/to/test-working-copy" \
  --backend-source "/path/to/original-svn-desk" --enable-changelists
```

`--enable-changelists` 必须由本机启动时显式设置。网页不能开启它、改授权目录或调用任意 SVN 命令；仅此标志不启用提交，旧的默认启动方式仍是只读。配套严格 history 后端不可用时归组不可用。

选择最多 100 个明确文件，点击「移动到变更列表」，输入已有或新名称，或选择「移除标签，回到默认变更列表」。对话框包含被筛选/折叠隐藏的选中路径和来源组。点击「预检明确选区」读取本地状态、info 和所选文件的有界内容摘要，成功后仍需单独「确认修改本地标签」。原始内容和摘要均不发送到网页或仓库。名称 1–128 字符，去除前后空白、拒绝控制字符；不持久化空组。

确认绑定当前工作区、选择代际、状态快照、精确文件集合和目标，有效期 120 秒且只使用一次。刷新、编辑目标、改变选区或关闭对话框后需重新预检。执行后通过新的本地状态核对结果；失败/超时可能已改变部分标签，不自动重试或回滚。关闭正在执行的窗口不取消命令，界面先清空旧状态；结果未知时需要明确刷新重新审查。

计划同时绑定所选文件 SHA-256：等长修改并还原 mtime 也须重新预检。每文件最多 32 MiB，每次摘要遍历的选区总量最多 64 MiB，以 64 KiB 块读取并检查累计 10 秒协作式时限；超限拒绝，不回退成仅元数据检查。预检两遍，成功执行共三遍，执行后变化返回结果未知。文件系统阻塞调用无法被该时限中断，摘要也不能使外部 SVN 命令与其他本机进程原子化。**c797d99 的 Windows 验收发现了旧元数据检查漏洞；本轮摘要修复仍待新的 Windows 真实 SVN 复测。**

这只是当前工作副本的 SVN 原生标签，不是服务器编号 CL，不修改工作文件内容，也不触碰提交、还原或 Shelf；Shelf 使用独立入口。空命名组会自然消失。只允许已版本化单文件，拒绝目录、链接、hardlink、reparse point、跨设备、嵌套 WC、external、switched、冲突、缺失及锁定条目。安全边界与完整验证记录见 [本地归组工作流](docs/LOCAL_CHANGELISTS.md)。**当前未完成 Windows 真实 SVN 归组验收，请先在可丢弃测试副本验证。**

### 本地 Shelf 备份与恢复（独立启用）

```sh
python3 server.py --workspace "/path/to/disposable-test-wc" \
  --backend-source "/path/to/paired-original" --enable-shelves
```

选择最多 100 个明确普通文件，完整预览并预检后，单独确认创建本地 Shelf。创建保留原文件，不自动还原或提交。浏览本地 Shelf 后可预检并确认恢复整个明确范围：原版本化文件必须清洁且匹配记录的 BASE，新文件目标必须不存在。Shelf 始终保留，失败可能已部分恢复，没有自动重试或回滚。

复用经校验的原版 Shelf 核心，新建 format 2，兼容上限以内的 format 1/2，不迁移或改写旧备份。此 Web 切片不支持目录、超过 32 MiB 的单文件或超过 64 MiB 的总内容；没有服务器 Shelf 或删除入口。默认使用原版按工作区区分的本地存储，也可显式设置 `--shelf-storage`，位置必须在所有工作副本与授权扫描范围之外。状态、内容、目标或 Shelf 改变需重新预检；确认只用一次，有效期 120 秒。

**ad382ca 的 Windows 真实 SVN 验证已通过 Shelf 核心流程，但发现延迟请求体的 HTTP 拒绝缺陷及测试夹具问题；本轮修复仍待 Windows 复测，浏览器验收尚未完成**。请先用可丢弃副本验证。参见 [HTTP 与 Windows 夹具修复](docs/HTTP_REJECTION_FIX.md)。详细边界、限额及失败语义见 [本地 Shelf 工作流](docs/LOCAL_SHELVES.md)。

### 明确选区 / 完整变更列表提交（独立启用）

```sh
python3 server.py --workspace "/path/to/disposable-test-wc" \
  --backend-source "/path/to/paired-original" --enable-commits
```

仅在本机启动时显式启用且配套后端通过版本核对后可提交。可提交 1–100 个明确选中文件，或单独点击「提交完整变更列表…」审查命名组全部成员（包括干净、筛选隐藏和折叠成员）。完整组缺少任何成员即拒绝；文件选区不会自动扩大为整个组。没有自动 add、目录、移动、还原或提交重试。

必填提交说明（最多 8192 UTF-8 字节），明确选择是否保留标签，先「预检」再单独「确认提交到仓库」。计划绑定工作副本、状态快照、精确成员、内容摘要、属性与 BASE / 仓库元数据、说明和保留标签选项，120 秒内一次有效。变化、刷新、切换或关闭后重新预检。提交使用现有 SVN 配置与凭据，预检只查本地，不保证远端未过期或钩子接受。

发生超时、错误或关闭执行窗口时，仓库可能已接受提交。不会自动重试或回滚；先用可信客户端核对仓库历史，再明确刷新。零退出会保留命令完成事实；识别到唯一有效的 SVN 修订报告时显示该修订。提交后钩子失败、其他诊断或未知输出会明确警告，不能据此重复提交。后续状态刷新失败不会抹去已完成的提交结果。

**b22f584 的 Windows 真实 SVN 重测已确认 XML 顺序修复有效，但发现提交后钩子警告被丢弃。** 本轮保留已完成提交及 SVN 报告的修订，同时明确显示警告、禁止自动重试；原始输出不直接进入网页或操作日志。Linux 回归通过，仍需 Windows 真实 SVN 重测当前修复。浏览器执行验收尚未完成：Chromium 启动被本环境的 socket 限制阻止，本机没有 SVN / svnadmin。请先在自建可丢弃仓库验证，不能据此宣称可发布。完整范围与验证清单见 [安全提交工作流](docs/COMMIT_WORKFLOW.md) 和 [提交诊断与警告](docs/COMMIT_DIAGNOSTICS.md)。

### 仓库文件锁 / 解锁（独立启用）

使用 `--enable-locks` 显式开启，复用原版 LockManager。工具栏和右键菜单只针对 1–100 个明确已提交普通文件，完整包含隐藏选区。打开窗口不会联网；预检明确访问服务器并核对归属，之后另行单次确认。锁定不抢占现有锁，解锁只释放本工作副本匹配的锁；不提供 force。锁可能影响他人提交及 `svn:needs-lock` 文件权限，多文件可能部分完成。

启用后本地状态含干净项以便选择文件，但不自动查询远端。说明最多 4096 UTF-8 字节，仅确认后随锁命令发送到仓库。计划绑定范围、内容/属性/身份和服务器锁状态，120 秒一次有效。已核实完成事实在后续状态刷新失败或关闭窗口时仍保留；未知结果不重试，须在可信原客户端检查后再明确刷新审查。

**本轮只有合成文件/模拟 SVN、模型与控制器回归，真实 SVN、Windows 和浏览器执行仍待验。** 浏览器启动受当前环境 socket 权限限制，未绕过。详见 [文件锁工作流与验收](docs/FILE_LOCKS.md)。

### 文件右键与键盘菜单

右键已选文件会保留完整多选（包含被筛选/折叠隐藏的成员）；右键未选文件会明确改为只选该文件。菜单顶部显示焦点路径、选中总数和隐藏数。差异/历史/属性只针对焦点文件；审查、归组、添加文件、Shelf、提交针对完整选区，提交与锁操作不会扩为整个 changelist。

Shift+F10 或菜单键打开同一菜单。↑/↓、Home/End 浏览可用项，Enter/空格触发，Esc 收起并返回当前文件行；Tab 离开。刷新、筛选、选择/工作区变化、外部点击、失焦或缩放会使菜单失效。不可用功能显示禁用及启用条件；菜单本身不读取仓库或发送写请求。

本轮通过模型与实际控制器/监听器的 DOM-adapter 回归，**不是浏览器或 Windows 验收通过**。Chromium 启动仍被本环境 socket 限制阻止；原生键盘默认行为、布局与 CSP 需运行随附浏览器测试再验收。见 [文件菜单说明与验收](docs/FILE_CONTEXT_MENU.md)。

### 单文件本地属性对照

「属性」标签、文件详情和右键菜单现在可显式读取当前普通文件的 WORKING / BASE 属性。不需要写权限，不自动查询仓库或继承属性；只读取焦点文件。新增文件明确标注没有 BASE，普通文件的 BASE 读取失败不会显示为空。

展示属性新增/移除/修改、空值与二进制摘要；变更按完整原始字节比较。文本有明确截断/遮蔽/控制字符转义提示。刷新、切换或关闭使旧请求失效，失败不会残留旧结果。**3a4cb07 的 Windows 原始完整套件出现 2 失败、2 错误，已定位测试夹具路径及符号链接权限兼容问题。本轮只修正测试，精确补丁仍待 Windows 完整重跑；GUI 未验收。** 先前独立复核中真实 SVN 78 项通过、另 1 项 XML 审计通过，不能替代完整套件。见 [Windows 测试兼容修正](docs/PROPERTY_WINDOWS_TEST_FIX.md)；功能边界见 [文件属性审查](docs/FILE_PROPERTIES.md)。

### 单文件差异与历史

点击「查看差异」后明确选择修订并「读取差异」。BASE→BASE 在调用 SVN 之前明确拒绝，不伪造为空差异；本地修改请选择 BASE→WORKING，历史比较使用明确数字修订。BASE→WORKING 只读本地副本；其他比较和历史会访问当前文件仓库，并使用既有 SVN 配置与凭据，页面提前说明。每页历史最多 50 条，支持前后分页、提交详情和当前文件的 r(N−1)→rN 比较；详情的变更路径属于整次提交，可能超出当前文件范围，但不可点选扩大读取范围。

请求绑定当前工作区 ID、选择代际、成功状态快照令牌和原始状态行 ID。切换、刷新失败、改修订、改焦点、关闭窗口或切换预览会清空旧结果并使旧请求失效。关闭不取消已经启动的 SVN 子进程，服务端仍按有界超时完成；不会自动重试。

只接受明确版本化文件。目录、链接、hardlink、reparse point、跨设备、嵌套 WC、external、switched、管理目录与非法路径被拒绝；缺失/已删文件仍须由 SVN 元数据确认是当前工作区文件。Web diff 固定 `--depth empty`，从不递归展开。预览最多 256 KiB，截断会显示；差异硬捕获上限 8 MiB，历史 4 MiB，历史显示页 512 KiB，超限不返回部分结果。

差异按 UTF-8 解释有效片段，不再对整份混合输出回退到系统代码页。不可按 UTF-8 解码的字节逐个显示为 `\xNN`，控制字符按可见形式转义，并始终提示计数及“不可作为精确补丁”。这不是原始编码检测，也不猜测恢复旧编码文件内容；NUL 或 UTF-16 BOM 会安全拒绝整份结果。详情见 [编码策略与复测范围](docs/DIFF_ENCODING_FIX.md)。

配套 `history.py` 必须同时声明 `DIFF_TEXT_POLICY=1` 与 `HISTORY_DIAGNOSTICS_POLICY=1`：零退出码但有任何 stderr（包括本地化提示、工具 banner 或空白）也拒绝结果。可能拒绝无害提示，这是明确的 fail-closed 兼容策略；不按语言或告警编号猜测成功。旧 history 模块不会禁用可用 status，但文件读取按钮不可用。详见 [范围与验证](docs/READONLY_FILE_WORKFLOWS.md)。

### 扫描、选择和保存

```sh
python3 server.py --scan-parent "/path/to/projects" \
  --backend-source "/path/to/original-svn-desk" \
  --settings-file "/path/outside-projects/web-preferences.json"
```

`--workspace` 和 `--scan-parent` 可重复使用。扫描复用原后端的有界、仅元数据 discovery，跳过符号链接、重解析点和跨设备目录；不读取项目文件或 `.svn` 内容。候选不等于已验证工作副本、仓库身份或 externals 关系。扫描不完整及限制原因会显示。没有候选或验证失败时绝不切换到示例数据。

“打开工作区”只允许从授权候选中选一个。选择成功以一次真实本地 status 成功为条件，之后清空文件选区、搜索和目录范围。选中项与路径在每次读状态前后重新核对；候选消失/被替换会失效。偏好恢复只允许当前启动授权范围中重新发现的候选，不信任偏好文件中的程序路径或其他目录。

`--settings-file` 是明确启用的**本地偏好写入**，不是 SVN 写操作。父目录必须已存在，位置必须在所有授权扫描范围与工作副本之外，不接受链接文件。只保存最近成功选择的本地根和非敏感显示信息，不保存令牌、仓库 URL 或 SVN 程序路径。损坏文件不静默覆盖；保存失败会提示，当前有效状态仍可使用。未指定时不保存。

多窗口共享一个服务的当前工作区；偏好文件不提供跨进程合并。路径前后检查降低替换风险，但不会把路径型 SVN 后端变成可抵御恶意本机进程的沙箱。

## 安全边界

- 仅监听 `127.0.0.1`；精确 Host、Origin / Fetch Metadata、每次启动临时 session token、无 CORS、no-store、CSP、iframe 禁止。
- 只允许明确静态资源、`/api/config`、`/api/status`、`/api/workspaces` 的 GET，以及严格 JSON 的 `/api/workspace-selection`、`/api/file-diff`、`/api/file-history`、`/api/file-properties` POST；独立启用的文件添加仅提供 `/api/add-preflight` 与 `/api/add-apply`，接受明确未版本化文件选区和单次确认，不递归、不添加父目录或提交；另有显式启用后的 `/api/changelist-preflight` 与 `/api/changelist-apply`，以及独立启用的 `/api/shelves-list`、`/api/shelf-preflight` 与 `/api/shelf-apply`。选择请求只接受候选 ID，文件请求只接受工作区身份、快照令牌、状态行 ID 与受限修订；本地归组只接受受限选区及一次性确认令牌。独立启用的提交仅提供 `/api/commit-preflight` 与 `/api/commit-apply`，接受明确范围与单次确认。独立启用的仓库锁仅提供 `/api/lock-preflight` 与 `/api/lock-apply`，接受明确选区和一次确认，预检会访问服务器；没有 force/窃锁/破锁入口。独立启用的文件操作仅提供 `/api/file-op-preflight` 与 `/api/file-op-apply`，单文件移动／复制仍须精确目标预检及一次确认。无通用命令、任意文件读取、上传、单独文件删除、还原、Shelf 删除或服务器 Shelf 入口。
- 原始 SVN 错误输出不会返回网页或记录到日志；动态路径/标签按文本转义。
- 这是本机应用桥接，不是对不可信本机进程的安全沙箱，不应暴露到局域网或互联网。
- Python 标准库服务用于原型，不是生产 HTTP 部署方案。

## 验证

```sh
npm test
# 或分别运行：
node --test tests/model.test.js
python3 -m unittest discover -s tests -p 'test_*.py'
python3 build-standalone.py
# 新工作区集成测试需要可信原版源码：
SVN_DESK_BACKEND_SOURCE=/path/to/original-svn-desk npm test
# 可选真实只读测试，需已安装 SVN 和明确测试副本：
SVN_DESK_TEST_WORKSPACE=/path/to/test-wc SVN_DESK_BACKEND_SOURCE=/path/to/original-svn-desk npm test
```

浏览器测试方式及当前验证状态见 `docs/VALIDATION.md`。必须区分模型/HTTP 回归、Linux Chromium 的实际交互、真实 SVN 和 Windows 原生包装。

## Tauri / Rust

`src-tauri/` 提供 **未编译的最小离线演示壳**：打包同一 `web/`，无 command、shell、filesystem、HTTP 插件或能力授权。它不会启动 Python bridge，也不宣称是已完成的 Rust SVN 后端。

需要独立安装官方 Rust 工具链、Tauri 平台依赖和 `@tauri-apps/cli` 后运行 `npm run tauri dev`。本环境没有 Cargo / Rust / WebKitGTK 4.1，**没有编译或执行该壳，没有 Windows EXE/MSI，没有性能、体积或原生缩放结论**。没有安装依赖，也没有 lockfile；打包验证前应生成并审核锁文件。

下一轮建议先固定一个 Windows WebView2 / Tauri 原生壳验收，然后逐个接入既有安全服务，不为换 UI 重写或弱化 SVN 安全前置条件。

官方资料：[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/)、[capabilities](https://v2.tauri.app/security/capabilities/)、[configuration](https://v2.tauri.app/reference/config/)。

## 固定目标检出（源码预览）

新增「检出…」：启动时成对传入 `--checkout-url` 与 `--checkout-destination`，界面本地预检后单次确认，跳过 externals，保留所有失败/部分结果；核实后加入候选但不自动切换。默认关闭。详见 [检出流程、保护和验证限制](docs/CHECKOUT_WORKFLOW.md)。旧试用包与历史 Electron 装配器不包含本能力，需新桥接和 Windows 验收。
