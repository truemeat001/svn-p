# Windows 检出目录句柄修正

## 发现及影响

Windows 对 `202367d40d82da96f7f3e2cab5df907d7f67c12e` 的两套 Python（3.10 与 3.14.6）测试各运行 565 项：545 通过、3 失败、17 跳过。失败涉及持有原句柄时仍可重命名目录及相关文件保留位置。SVN 1.15.0 的常规真实临时仓库/HTTP 主流程 53/53 通过，不能抵消安全失败。

额外真实竞态回归在启动 SVN 前重命名目标并创建替代目录。SVN 写入替代目录后，后置检查才返回 HTTP 503/uncertain，且没有注册候选。结果保留和不重试正确，但不足以阻止写入未经审查的新目录对象。因此旧 Windows 检出不得视为验收通过，不交付此功能。

## 修正

`checkout_paths._windows_directory` 将访问掩码从 `FILE_READ_ATTRIBUTES`（0x80）改为 `FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES`（0x81），保留 `FILE_SHARE_READ`、`OPEN_EXISTING`、目录/重解析点打开标志和完整句柄生命周期。

仅属性访问不能建立预期的删除/重命名共享排斥。独立原生实验观察到 0x1 与 0x81 会以 WinError 32 拒绝重命名，关闭句柄后允许重命名。本修正采用 0x81，不请求 GENERIC_READ、写入或删除权限；打开失败时关闭已持有句柄并失败关闭，绝不回退到 0x80 或放宽共享模式。

Windows 的访问/共享规则参见 [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)。上述具体差异由原生实验确认，不能仅靠模拟断言 API 参数证明。

Linux 描述符路径锚定、一次确认、固定 URL/目录、失败保留、输出边界及现有操作均不改变。本轮**不增加指定修订功能**：检出仍是执行时 HEAD，本地预检不固定版本；界面已明确说明。

## 必须执行的精确提交重测

新增 `tests/test_checkout_real_integration.py`，只创建自己的临时 file:// 仓库与工作副本，不连接远程仓库或打开用户工程。安装好的可信 `svn` / `svnadmin` 可经 PATH 提供，也可使用仅用于测试的 `SVN_DESK_TEST_SVN` 与 `SVN_DESK_TEST_SVNADMIN` 绝对路径环境变量。

从 `prototypes/svn-desk-web` 运行：

```console
python -m unittest discover -s tests -p "test_checkout*.py" -v
```

Windows 上必须实际通过：

- 新目录和已有空目录真实检出，Unicode/空格/@ 文件与目标。
- 在真实 Popen 之前试图重命名目标及父目录，均被 OS 拒绝；随后真实 SVN 仍成功检出、验证并注册。
- 在创建目标前试图替换父目录，被 OS 拒绝；后续 mkdir/检出仍兼容。
- 完成后句柄已释放，目标可重命名。
- 原三个失败用例仍保留，不能删断言或以缺少权限为由跳过。
- 复跑既有 53 项真实 HTTP 流程及替换目录额外回归，确认替代目录没有仓库文件。
- 两套 Python 的 Web 全量、Node、桌面构建输入、自检继续通过；对精确提交核对源码未被测试改动。

缺少 SVN 或非 Windows 环境时，相关原生测试明确跳过，不计为通过。当前修正只完成源码修复与本机可运行测试；其 Windows 真正兼容性和安全性以重测结果为准。没有重新打包或交付，历史 Electron 装配器仍固定旧桥接，不能混装。
