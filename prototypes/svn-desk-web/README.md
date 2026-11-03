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

默认和离线包始终标明“演示数据”；启用工作区桥接后明确区分待选工作区、真实只读状态和失败状态。默认变更列表初始折叠，选中 `PlayerController.cs` 可以查看内置示例差异、历史。其他文件没有示例差异时会明确说明，不复用虚假内容。

## 已实现

- 搜索文件/变更列表，状态与目录范围筛选；目录只筛选，不会扩大选区。
- 本地 changelist 分组/折叠、文件多选、当前可见项选择、隐藏选中项计数、完整只读审查。
- 选中行详情、示例 diff / properties、示例历史弹窗、独立有界操作日志。
- F5 刷新、Ctrl/Cmd K 搜索、方向键浏览、空格切换选择、Esc 关闭弹窗。
- 详情/差异/日志面板显示切换，窄桌面视口布局；**最低布局宽度 520px，非手机产品。**
- 可选本地只读 status 桥接，以及授权目录中的工作区发现、选择、专用偏好保存与恢复。没有远程仓库调用或 SVN 写 API。

## 真实工作区流程（等待修正后的 Windows 重测）

Windows 验收发现旧版会把 W155007 + 零退出码误当空状态，现已修正并要求配套严格后端。请先阅读 [修正与重测说明](docs/STATUS_DIAGNOSTICS_FIX.md)，更新后重启服务。此环境尚无 SVN，不能把本地合成回归当作 Windows 修复已实测。

```sh
python3 server.py --workspace "/path/to/working-copy-root" \
  --backend-source "/path/to/original-svn-desk"
# 可选 --svn 指定可信、已经安装的 SVN 原生程序。
```

工作副本必须是含 `.svn` 的已有根目录。`--backend-source` 指向可信原版源码根，复用其 `svn_client.backend.SvnClient.status` / `status_stream`。本轮为修复零退出码告警误判，更新了原后端的只读 status 完成检查；其他安全实现和 Shelf 未改。此原型只执行本地 `status(verbose=False)`，既不运行远程 info/log/list，也不加 `--show-updates`。SVN 程序和目录授权范围在启动时固定，网页不能输入路径或任意命令。

结果只包含状态元数据；真实差异、文件内容、历史、仓库树、修订身份和连接状态没有接入。失败返回错误，清空结果并标记状态未知，绝不回退为示例或“干净”。5 万行上限是返回给 UI 前的拒绝阈值，不是流式 UI 或内存上限。依赖原后端完成有界 XML/超时处理。挂载网络目录的本地文件读取仍可能访问该文件系统。


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
- 只允许明确静态资源、`/api/config`、`/api/status`、`/api/workspaces` 的 GET，以及严格 JSON 的 `/api/workspace-selection` POST。POST 只接受服务器发出的候选 ID，执行本地 status 并可选保存偏好，不执行 SVN 写入。无通用命令、任意文件读取、上传、提交、还原、Shelf 或归组入口。
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
