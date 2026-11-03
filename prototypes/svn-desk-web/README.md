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

界面始终标明“演示数据”。默认变更列表初始折叠，选中 `PlayerController.cs` 可以查看内置示例差异、历史。其他文件没有示例差异时会明确说明，不复用虚假内容。

## 已实现

- 搜索文件/变更列表，状态与目录范围筛选；目录只筛选，不会扩大选区。
- 本地 changelist 分组/折叠、文件多选、当前可见项选择、隐藏选中项计数、完整只读审查。
- 选中行详情、示例 diff / properties、示例历史弹窗、独立有界操作日志。
- F5 刷新、Ctrl/Cmd K 搜索、方向键浏览、空格切换选择、Esc 关闭弹窗。
- 详情/差异/日志面板显示切换，窄桌面视口布局；**最低布局宽度 520px，非手机产品。**
- 可选本地只读 status 桥接。没有远程仓库调用，没有写 API。

## 真实只读 status（接入代码，不等于本轮实测通过）

```sh
python3 server.py --workspace "/path/to/working-copy-root" \
  --backend-source "/path/to/original-svn-desk"
# 可选 --svn 指定可信、已经安装的 SVN 原生程序。
```

工作副本必须是含 `.svn` 的已有根目录。`--backend-source` 指向可信原版源码根，复用其 `svn_client.backend.SvnClient.status` / `status_stream`，**不复制、不修改原安全实现或 Shelf**。此原型只执行本地 `status(verbose=False)`，既不运行远程 info/log/list，也不加 `--show-updates`。工作副本根和程序在启动时固定，网页不能改路径或输入任意命令。

结果只包含状态元数据；真实差异、文件内容、历史、仓库树、修订身份和连接状态没有接入。失败返回错误，清空结果并标记状态未知，绝不回退为示例或“干净”。5 万行上限是返回给 UI 前的拒绝阈值，不是流式 UI 或内存上限。依赖原后端完成有界 XML/超时处理。挂载网络目录的本地文件读取仍可能访问该文件系统。

## 安全边界

- 仅监听 `127.0.0.1`；精确 Host、Origin / Fetch Metadata、每次启动临时 session token、无 CORS、no-store、CSP、iframe 禁止。
- 只允许明确静态资源和 `/api/config`、`/api/status` 两个 GET 端点。无通用命令、任意文件读取、上传、提交、还原、Shelf 或归组入口。
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
```

浏览器测试方式及当前验证状态见 `docs/VALIDATION.md`。必须区分模型/HTTP 回归、Linux Chromium 的实际交互、真实 SVN 和 Windows 原生包装。

## Tauri / Rust

`src-tauri/` 提供 **未编译的最小离线演示壳**：打包同一 `web/`，无 command、shell、filesystem、HTTP 插件或能力授权。它不会启动 Python bridge，也不宣称是已完成的 Rust SVN 后端。

需要独立安装官方 Rust 工具链、Tauri 平台依赖和 `@tauri-apps/cli` 后运行 `npm run tauri dev`。本环境没有 Cargo / Rust / WebKitGTK 4.1，**没有编译或执行该壳，没有 Windows EXE/MSI，没有性能、体积或原生缩放结论**。没有安装依赖，也没有 lockfile；打包验证前应生成并审核锁文件。

下一轮建议先固定一个 Windows WebView2 / Tauri 原生壳验收，然后逐个接入既有安全服务，不为换 UI 重写或弱化 SVN 安全前置条件。

官方资料：[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/)、[capabilities](https://v2.tauri.app/security/capabilities/)、[configuration](https://v2.tauri.app/reference/config/)。
