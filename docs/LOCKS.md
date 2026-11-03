# SVN 仓库文件锁

本模块处理服务器上的文件锁，用来限制其他人提交同一文件。它不是工作副本管理锁，也不会执行 `svn cleanup`。锁不会阻止其他人在本地编辑文件，不能代替团队沟通。

## 当前范围

- 查询所选文件当前的服务器锁、所有者、备注、创建/到期时间。
- 对本工作副本内明确选中的、已经提交过的普通文件加锁。
- 只解锁本工作副本持有有效当前令牌的锁。相同用户名在另一工作副本中取得的锁也不会被释放。
- 支持内容或属性已修改但没有冲突的文件。加锁前会检查文件是否已落后于服务器。
- 不抢锁、不破锁、不传入自定义令牌，不使用 `--force`，不自动重试或回滚。

目录、未版本化文件、待添加/删除/替换/移动/复制的文件、冲突、缺失/阻塞文件、switched 文件、externals、嵌套工作副本、符号链接、junction 和硬链接均不在本阶段支持范围。父目录处于 switched、冲突或待提交的结构变化状态时也会拒绝操作。硬链接被拒绝，因为 `svn:needs-lock` 可能让 SVN 改变本地文件写权限，影响同一文件的其他路径。

每次最多选择 128 个文件。服务器查询逐文件执行，大批选择仍可能较慢；各 SVN 进程使用客户端配置的超时，而不是承诺整个批次在该时限内完成。

支持 `http://`、`https://`、`svn://`、`file://` 仓库。不支持 `svn+ssh://` 的外部 SSH 交互。使用已经配置好的可信 SVN 客户端认证；不接受 URL 密码，也不保存新的认证信息。

## 确认、验证与失败

预检是只读的，但会访问服务器。它先核对全部本地文件，再从 XML 读取当前服务器信息。确认后会重新预检，并可用 `state_sha256` 检查确认期间锁状态、目标或备注是否变化。

执行前应让用户确认准确文件、操作、锁所有者信息与加锁备注。`confirmed=True` 是调用方已经取得确认的声明，不是自动授权。

SVN 多文件加锁/解锁不是原子操作。即使退出码为零，也可能只有部分文件操作成功。因此模块不会根据命令输出猜测成功，而是在操作后再次读取服务器状态。验证失败、超时或部分成功返回 `LockOperationError`，其中 `outcome="uncertain"`、`automatic_retry_safe=False`，并在能够读取时附上脱敏的 `observed` 状态。必须先检查当前锁状态，不能直接重试。

SVN 自身仍会维护工作副本的原生锁令牌。本模块只在内存中比较令牌，不在公开计划、结果、错误文本或自建文件中输出/保存原始令牌，也不会把令牌作为命令参数提交。服务器钩子和权限最终决定操作是否允许。

成功提交文件通常会释放 SVN 收集到的锁；本模块不会改变已有提交命令的这一默认行为。`svn:needs-lock` 存在时，加锁/解锁可能分别使本地文件可写/只读。

## Python 接口

```python
from svn_client.backend import SvnClient
from svn_client.locks import LockManager

manager = LockManager(SvnClient(workspace))
owners = manager.inspect(["asset.png"])
plan = manager.preflight_lock(["asset.png"], "修改贴图")

# 调用方先展示 plan.files、plan.comment、plan.warnings 并取得用户确认。
result = manager.lock(
    plan.paths, plan.comment,
    confirmed=True, expected_state=plan.state_sha256,
)

plan = manager.preflight_unlock(["asset.png"])
# 解锁也需要单独明确确认。
result = manager.unlock(
    plan.paths, confirmed=True, expected_state=plan.state_sha256,
)
```

`FileLockInfo` 包含服务器所有者与本工作副本是否真正持有该锁，不只比较用户名。`LockResult.verified=True` 表示操作后的状态已经重新读取并符合目标；不是对未来状态的保证。操作期间避免外部编辑器或其他 SVN 程序同时修改该工作副本。

## JSON CLI

```console
python -m svn_client.cli --workspace /wc lock inspect --path asset.png
python -m svn_client.cli --workspace /wc lock acquire --path asset.png --comment "修改贴图" --dry-run
python -m svn_client.cli --workspace /wc lock acquire --path asset.png --comment "修改贴图" --expect-state-sha256 <已确认的64位小写哈希> --yes
python -m svn_client.cli --workspace /wc lock release --path asset.png --dry-run
python -m svn_client.cli --workspace /wc lock release --path asset.png --expect-state-sha256 <已确认的64位小写哈希> --yes
```

每个命令都要求明确 `--path`（可重复），或 `--paths-json FILE|-`。不会把未提供路径解释为整个工作区。`inspect` 返回 `data.files`；预检返回 `data.preflight`，其中包含 `files`、`comment`、`warnings` 和 `state_sha256`。`--dry-run` 不改变锁，但仍会逐文件访问仓库。

调用方应展示预检文件和备注，再把同一个哈希传给 `--expect-state-sha256`。该哈希绑定所选路径、仓库/版本/锁元数据、操作与备注，也在内存中绑定当前令牌；不绑定文件内容。哈希不包含可逆令牌。即使省略这个可选参数，CLI 也会把刚刚预检的哈希传给执行方法，在实际调用 SVN 前再次核对。这个选项不能取代调用方自己的用户授权。

`--yes` 表示调用方确认本次服务器修改；不能与 `--dry-run` 同用。CLI 不提供抢锁、破锁、强制令牌或密码参数。成功返回 `outcome="completed"` 和 `data.result.verified=true`；失败或部分结果会保守返回退出码 `8`、`operation_uncertain` 与 `automatic_retry_safe=false`。能够重新读取时，`error.details.observed_locks` 包含脱敏状态。先用 `lock inspect` 核对当前状态，不要自动重复修改。进入修改方法后即使二次预检拒绝，CLI 也保守报告不确定。

## 验证与资料

单元测试使用模拟 SVN XML 和临时普通文件，覆盖所有权、确认绑定、部分成功、异常、路径与令牌脱敏。CLI 测试覆盖参数确认、只读预检、哈希绑定、JSON 结果与输出失败后不重试。真实集成测试只创建临时 `file://` 仓库；没有 `svn`/`svnadmin` 时跳过。当前环境未验证真实 SVN 服务或原生 Windows 执行。GUI 接入由对应模块单独完成。

- [SVN 文件锁及令牌语义](https://svnbook.red-bean.com/en/1.7/svn.advanced.locking.html)
- [svn lock](https://www.visualsvn.com/support/svnbook/ref/svn/c/lock/)
- [svn unlock](https://www.visualsvn.com/support/svnbook/ref/svn/c/unlock/)
- [Apache Subversion 1.14 info XML schema](https://github.com/apache/subversion/blob/1.14.x/subversion/svn/schema/info.rnc)
