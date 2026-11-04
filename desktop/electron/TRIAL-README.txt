SVN Desk · Electron Windows x64 Proof of Concept

这是额外的 Electron 桌面技术预览，不替换现有 WebView2 版。
双击 SVNDesk-Electron.exe 后选择 SVN 工作副本（只读）或演示。
双击 Start-Demo.cmd 直接进入演示，无须 SVN 或真实项目。
请完整解压全部文件后启动，不要仅复制 EXE。

此包使用 Electron 45.2.2 和已核对 SHA-256 的 5298602 Python bridge。
Web 界面和工作流程复用现有版本，并合入目录计数与键盘选择修复。
仅 app.js/model.js 外置静态资源更新，已冻结 Python bridge 保持不变。
此包不代表已实现 P4V 的全部流程。
写操作默认关闭，只有显式 workspace + --enable-* 选项才开启现有相应功能。
真实工作副本需要另行安装 SVN CLI，此包不安装或更新系统组件。

隔离：每次启动使用新的临时用户数据目录与非持久化页面会话，保留目录但不自动删除。
也可指定 --profile-root C:\SVNDeskBuild\NewProfile：父目录必须存在、在版本库外，目标必须不存在。
页面无 Node.js、preload 或 IPC，启用 Chromium 沙箱、上下文隔离和同源检查。
不自动扫描目录，不打开外部链接，不允许下载、子窗口、权限请求或远程页面。
关闭时先移除工作区页面，再等待已接受的 SVN 操作结束；关闭不等于取消或回滚。
强制结束进程、断电、恶意同用户程序或管理员级修改不属于这些保证。

验证状态：已进行源代码审核、协议/关闭行为单元测试、包清单校验。
本包尚未进行真实 Windows Electron 窗口验收，不能视为 GUI 测试已通过。
未测量 Electron 与 WebView2 的启动时间、滚动延迟或内存对比。
此预览未代码签名，Windows 可能显示发布者/SmartScreen 警告；不要绕过系统安全警告。
如安全策略阻止启动，请保留信息并向维护者报告。

Electron 与 Chromium 许可见 LICENSE、LICENSES.chromium.html。
Python/PyInstaller 及 bridge 依赖许可见 resources\Bridge\Licenses。
本包包含 SHA256-MANIFEST.json，方便独立校验。
