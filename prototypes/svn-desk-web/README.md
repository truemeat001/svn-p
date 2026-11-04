# SVN Desk · Modern Web vertical prototype

独立的、可操作的现代 Web 桌面原型，保留 P4V 式工作区树、pending changelist 文件分组、明确选区、详情、差异和独立操作日志。**不是旧主程序的替换版，不是完成迁移或可发布客户端。**

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
- 可选本地 changelist 标签移动与移回默认组：启动时明确启用，完整选区预检，再单独确认；归组不触发提交或还原；本地 Shelf 与仓库提交分别显式启用。
- 选中行详情、diff / properties、分页文件历史和独立有界操作日志；离线模式仍明确使用示例数据。
- 文件右键菜单与 Shift+F10 / 菜单键：完整选区审查、归组、Shelf、提交，以及焦点文件差异/历史；复用现有预检与确认，打开菜单不执行 SVN。
- F5 刷新、Ctrl/Cmd K 搜索、方向键浏览、空格切换选择、Esc 关闭菜单或弹窗。
- 详情/差异/日志面板显示切换，窄桌面视口布局；**最低布局宽度 520px，非手机产品。**
- 可选本地只读 status、明确单文件 diff / history 桥接，以及授权目录中的工作区发现、选择、专用偏好保存与恢复。历史与非 BASE→WORKING 的修订比较只在显式点击后访问仓库。默认没有 SVN 写能力；可选写入口是经过预检和确认的本地 changelist 标签、本地 Shelf 备份/恢复，以及单独启用、另行确认的明确范围仓库提交。

## 真实工作区流程

Windows 验收发现旧版会把 W155007 + 零退出码误当空状态，配套严格后端已修正，集成提交 `5dbaff4` 的 status 修复已通过后续 Windows 真实 SVN 重测。提交 `9864460` 的 Windows 真实 SVN 验证已确认历史分页等流程，同时发现混合编码差异和 BASE→BASE 兼容问题。本轮修正已完成本地回归，**仍待新的 Windows 真实 SVN 复测**。请先阅读 [status 修正说明](docs/STATUS_DIAGNOSTICS_FIX.md) 、[只读文件工作流说明](docs/READONLY_FILE_WORKFLOWS.md) 与 [混合编码修正说明](docs/DIFF_ENCODING_FIX.md)，更新完整配套源码并重启服务。

```sh
python3 server.py --workspace "/path/to/working-copy-root" \
  --backend-source "/path/to/original-svn-desk"
# 可选 --svn 指定可信、已经安装的 SVN 原生程序。
```

工作副本必须是含 `.svn` 的已有根目录。`--backend-source` 指向可信原版源码根，复用其 `svn_client.backend.SvnClient.status` / `status_stream`。本轮为修复零退出码告警误判，更新了原后端的只读 status 完成检查；原版 Shelf 核心保持不变；新的可选 Web Shelf 适配见下文。默认刷新只执行本地 `status(verbose=False)`；启用本地归组后使用 `status(verbose=True)` 并仅显示变更项与有标签的干净成员，不加 `--show-updates`，不自动执行历史或远程查询。显式文件读取复用原版有界 `HistoryManager`；本地 `info` 验证身份后，按用户点击执行 `diff` 或 `log`。SVN 程序和目录授权范围在启动时固定，网页不能输入路径或任意命令。

状态结果只包含元数据。当前状态行可以显式读取单文件的有界差异或分页历史；不是任意文件浏览器，不提供整文件内容、目录差异、仓库树或持续连接状态。失败返回错误，清空结果并标记状态未知，绝不回退为示例或“干净”。5 万行上限是返回给 UI 前的拒绝阈值，不是流式 UI 或内存上限。依赖原后端完成有界 XML/超时处理。挂载网络目录的本地文件读取仍可能访问该文件系统。


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

### 文件右键与键盘菜单

右键已选文件会保留完整多选（包含被筛选/折叠隐藏的成员）；右键未选文件会明确改为只选该文件。菜单顶部显示焦点路径、选中总数和隐藏数。差异/历史只针对焦点文件；审查、归组、Shelf、提交针对完整选区，提交不会扩为整个 changelist。

Shift+F10 或菜单键打开同一菜单。↑/↓、Home/End 浏览可用项，Enter/空格触发，Esc 收起并返回当前文件行；Tab 离开。刷新、筛选、选择/工作区变化、外部点击、失焦或缩放会使菜单失效。不可用功能显示禁用及启用条件；菜单本身不读取仓库或发送写请求。

本轮通过模型与实际控制器/监听器的 DOM-adapter 回归，**不是浏览器或 Windows 验收通过**。Chromium 启动仍被本环境 socket 限制阻止；原生键盘默认行为、布局与 CSP 需运行随附浏览器测试再验收。见 [文件菜单说明与验收](docs/FILE_CONTEXT_MENU.md)。

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
- 只允许明确静态资源、`/api/config`、`/api/status`、`/api/workspaces` 的 GET，以及严格 JSON 的 `/api/workspace-selection`、`/api/file-diff`、`/api/file-history` POST；另有显式启用后的 `/api/changelist-preflight` 与 `/api/changelist-apply`，以及独立启用的 `/api/shelves-list`、`/api/shelf-preflight` 与 `/api/shelf-apply`。选择请求只接受候选 ID，文件请求只接受工作区身份、快照令牌、状态行 ID 与受限修订；本地归组只接受受限选区及一次性确认令牌。独立启用的提交仅提供 `/api/commit-preflight` 与 `/api/commit-apply`，接受明确范围与单次确认。无通用命令、任意文件读取、上传、还原、Shelf 删除或服务器 Shelf 入口。
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
