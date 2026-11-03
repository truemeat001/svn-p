# 只读 SVN 连接诊断

独立模块 `svn_client/connection.py` 检查 SVN 程序、已有工作副本身份，以及明确指定的仓库 URL。它不登录账号、不修改工作副本或仓库、不接受证书、不配置代理，也不自动重试。

**默认只检查本地。** `report.ok=True` 只表示本次请求的步骤成功。未请求远程检查时，不能把它解释为“服务器已连接”。已接入“文件 → 连接诊断”界面和 `python -m svn_client.cli diagnostics`；操作与结果说明见 [连接诊断界面](CONNECTION_GUI.md) 和 [CLI 文档](CLI.md)。报告应区分每个步骤及是否实际请求远程检查。

这里的“本地”表示不发起 SVN 仓库 URL 请求，并不是系统断网模式。用户明确选择的工作副本或可执行程序如果位于 UNC、网络映射盘或其他远程挂载文件系统，解析路径和读取文件本身可能访问那个文件系统；模块不会猜测挂载是否联网。需要完全离线检查时，应选择本机存储上的目录与程序。

## Python 接口

```python
from dataclasses import asdict
from svn_client.connection import ConnectionDiagnostics, validate_probe_url

# 仅检查安装的 SVN 版本。
report = ConnectionDiagnostics().probe()

# 检查版本和本地工作副本身份；不会跟随工作副本中保存的 URL。
diagnostics = ConnectionDiagnostics("C:/Work/Project", timeout=15)
local_report = diagnostics.probe()

# 在界面中让用户明确选择并核对 URL 后，才开启远程请求。
url = validate_probe_url("https://svn.example.com/project/trunk")
remote_report = diagnostics.probe(url=url, check_remote=True)
data = asdict(remote_report)
```

构造函数接受 `working_copy=None`、仅关键字的 `executable=None`、`timeout=15`。`executable` 只能指定用户信任的已安装 SVN 程序，沿用项目的可执行程序发现逻辑；Windows 批处理包装器不支持。模块不会下载或安装 SVN。

`probe(*, url=None, check_remote=False)` 返回不可变 `ConnectionReport`。参数格式错误抛出不包含输入秘密的 `ValueError`；预期的运行失败通过 `steps` 返回，不向调用方回显原始 SVN 异常或进程输出。

报告包括：

- `executable`、`version`：实际选择的程序路径及识别出的版本。
- `working_copy`：所选目录、真实工作副本根目录和本地记录的仓库身份。允许选工作副本子目录，不自动扩大读取目标。
- `remote`：显式 URL 在 HEAD 的仓库身份。只保留 URL、仓库根、UUID、修订号和节点类型，不复制作者、锁令牌、属性或原始 XML。
- `remote_requested`：调用方是否明确要求远程检查。
- `remote_attempted`：是否开始了远程检查阶段；进程可能未启动成功，不能据此断言服务器收到了请求。
- `repository_identity_matches`：本地与远程均读到时比较 UUID。相同 UUID 不证明 URL、分支、本地内容或修订一致；不同 UUID 不会触发 switch/relocate。
- `steps`：每项有 `stage`、`status`（`ok` / `failed` / `skipped`）、`category`、固定说明、数字错误码和可用时的退出码。

版本检查失败会停止后续步骤。若只是本地目录或工作副本读取失败，用户明确指定的独立远程 URL 仍可检查，以区分本地问题与服务器问题。

## 请求与认证边界

- 远程检查必须同时提供明确 `url` 和 `check_remote=True`；不给标志而提供 URL、只给标志不提供 URL都会拒绝。不会自动取本地 `.svn` 中的 URL 发起请求。
- 支持 `http://`、`https://`、`svn://` 和本地 `file://`。拒绝 SSH 外部传输、远程 file 主机、UNC 别名、userinfo（包括只有用户名的形式）、查询、fragment、原始空白、控制字符、反斜线、无效转义、编码后的控制字符和歧义 authority。
- 输入 URL 的路径一律按字面解释，随后附加 `@HEAD`。例如 `.../name@123` 是名为 `name@123` 的路径，不是历史 peg 修订。此诊断不提供历史 URL 语法。
- 本地只读身份检查可读取 `svn+ssh` 元数据，不运行 SSH。它会去掉仅用户名的 userinfo；含密码或不安全结构的元数据拒绝输出。这样的显示 URL 不会自动用于远程检查。
- 远程命令是一个明确 URL 的 `svn info --xml --depth empty`，带 `--non-interactive` 和 `--no-auth-cache`。不读取密码、不传密码参数、不添加认证缓存，不使用 `--trust-server-cert`、证书失败豁免或配置覆盖。
- SVN 可使用既有可信认证配置和凭据缓存。远程请求可能让服务器产生访问日志。用户应先在受信任的 SVN 客户端配置账号和证书；诊断不是登录、凭据修复或安全限制绕过工具。
- 所有命令使用参数数组、关闭 shell、关闭标准输入。没有通用命令入口，没有更新、检出、提交、加锁或其他写命令。

## 分类与保密

依据 SVN / Serf 的数字错误码分类，不依赖英文或中文提示。只从标准错误的诊断行位置取代码，不会因 URL 路径里碰巧出现 `E215004` 就判断为认证失败。

分类包括 `dependency_missing`、`process_error`、`working_copy`、`authentication`、`authorization`、`tls`、`network`、`timeout`、`not_found`、`connection`、`svn_error`、`invalid_response`、`identity_mismatch`。认证与证书等具体代码优先于普通连接包装错误。

这些分类是排查线索，不是确定的根因诊断。通用 session 错误及未识别的系统码不会硬猜成密码错误或断网；部分服务器也可能通过“找不到”隐藏权限不足的路径。本地化的 warning 标签也可能回退为一般错误，不影响普通数字错误行的分类。TLS 错误只提示核对证书与信任配置，不建议绕过校验。

公开结果不含原始 stdout/stderr、任意服务端文本、证书细节、锁令牌或密码。XML 必须为 UTF-8，不接受 DTD/实体声明，身份字段必须完整有效。超过 2 MiB 的 XML 会被拒绝解析；这是**捕获后的解析限制，不是子进程输出或全程内存配额**。

## 超时和验证范围

超时默认 15 秒，可设为大于 0、不超过 120 秒的有限数值。这是**每个 SVN 子进程**的超时，一次报告最多执行版本、本地 info、远程 info 三个进程；不承诺整个报告在同一时限内结束，也不覆盖进程创建或第三方认证设施的所有行为。不自动重试，不后台轮询，不声称超时表示服务器从未收到请求。

`info` 成功只说明当时能读取该目标，不能证明更新、检出、提交、锁操作、hooks 或所有子目录权限都可用。此模块不执行这些写入或递归操作来“测试权限”。

测试命令：`python -m unittest discover -s tests -p test_connection.py -v`。单元测试模拟子进程输出与临时目录，不连接用户仓库。真实集成用例只创建一次性本地 `file://` 仓库；需要 `svn` 和 `svnadmin`。当前环境没有这些程序，故真实 SVN 用例跳过，原生 Windows、网络认证、TLS 与 GUI 也未经过真实执行验收。

依据：[SVN info 文档](https://www.visualsvn.com/support/svnbook/ref/svn/c/info/)、[Apache SVN 错误码](https://github.com/apache/subversion/blob/1.14.x/subversion/include/svn_error_codes.h)、[Apache Serf 错误码](https://github.com/apache/serf/blob/trunk/serf.h)。
