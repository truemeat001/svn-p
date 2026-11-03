# 文件历史与版本比较（只读预览）

`HistoryManager` 和 CLI 的 `history` / `compare` 提供按一个本地文件或目录查询的只读流程。不会执行 update、commit、revert、merge，不生成临时补丁文件，也不会写入 SVN 认证缓存。它们不替代备份，亦不等于 P4V 的全部历史/比较功能。

## 图形界面

- 在“待提交更改”或“工作副本文件”中选中**一个已版本化文件行**，点击“文件历史”或右键“查看此文件历史”。组标题、目录行、多个文件或空选择不会自动变成根历史。
- 历史页顶部始终显示明确的相对路径。可以自行输入一个文件或目录；只有显式的 `.` 或“工作副本根历史”按钮表示当前副本根。左侧导航目录的右键入口会明确标为“目录历史（含后代）”。目录读取包含版本化后代，不会传递为提交范围。
- 选择 HEAD、BASE 或非负数字作为起始修订，点击“查询 / 刷新此范围”。GUI 每页可选 25、50、100 或 250 项，每次只保留一页；“更早一页”使用后端游标，“较新一页”最多保留 128 页回退游标，不累积整段日志。
- 改动路径、起始修订或页大小立即清除旧结果。即使把输入改回原值，旧请求也不会复活。读取期间切换工作区、页签或选中其他对象时，旧结果不会覆盖新详情。
- 选中历史修订显示提交说明和该次提交的**全部**更改路径，可能包含查询范围以外的项。详情显示上限 64 KiB，会说明截断；表格单元也限制长度。
- 双击修订或按 Enter，会打开比较对话框，预填当前查询路径的 `r(N−1) → rN`。不会自动比较整次提交涉及的所有路径，也不会扩大到工作副本根。
- “比较版本…”或右键“比较此文件版本”固定当前明确文件；历史页的“比较当前范围的版本…”使用顶部明确路径。对话框显示完整工作副本和相对路径，再选择左右端点。左侧只允许 BASE、HEAD、数字；右侧另允许 WORKING。
- 新比较流程的显示上限为 64、256 或 1024 KiB，默认 256 KiB。结果标明路径、方向、已显示/原文本 UTF-8 字节数和是否截断。空文本结果不代表二进制内容相同；没有二进制载荷、图片比较、可编辑合并或应用补丁功能。
- 此上限针对新“比较版本”与历史修订比较流程。既有批量“查看差异”入口保留原行为，不应将新接口上限推断为所有旧读取路径都已改造。
- 比较和历史全部异步只读，不自动更新、合并、写文件、扩大范围或重试。演示模式只保留示例历史，新查询与比较不会访问真实仓库。
- Windows 将反斜杠视为路径分隔符；在 POSIX 上包含字面反斜杠的文件名会明确拒绝，避免把一个文件名悄悄改成另一条路径。

GUI 测试覆盖真实方法的无显示回调流程、参数、分页、取消、过期结果和 UTF-8 截断。它们不是原生窗口渲染或真实 SVN 验收。

## 历史

```console
python -m svn_client.cli --workspace /path/to/wc history --path src/example.py --limit 100
python -m svn_client.cli --workspace /path/to/wc history --path src/example.py --limit 100 --start-revision 125
```

Windows 将 `python` 换成 `py -3`。默认查询 `HEAD:0`，因此可查看工作副本 BASE 之后已提交的相关历史，但不会更新工作副本。一次最多返回 1000 项，默认 100 项。`entries` 按修订号降序排列；`has_more` 为真时，将 `next_start_revision` 原样用于下页的 `--start-revision`。该游标指向最后一项之前的修订，不是假设每个全库修订都修改过所选文件。结果不承诺跨请求锁定服务器快照。

历史提交筛选以选中路径为范围。`changed_paths` 仍列出该次提交全部受影响路径，并非只有所选文件。目录查询包含该目录的相关历史。SVN 默认沿复制历史追溯，本工具不自行推测改名、不自动扩大到仓库根目录，也不因路径不存在而静默改查另一项。已删除、移动或重建路径的结果受 SVN peg revision 和权限语义约束；不提供任意 URL 或 peg revision 编辑器。

## 显式比较

```console
python -m svn_client.cli --workspace /path/to/wc compare --path src/example.py
python -m svn_client.cli --workspace /path/to/wc compare --path src/example.py --from BASE --to HEAD
python -m svn_client.cli --workspace /path/to/wc compare --path src/example.py --from 120 --to 125
python -m svn_client.cli --workspace /path/to/wc compare --path src/example.py --from HEAD --to WORKING
```

- `BASE`：工作副本中该项的基础版本，未包含本地编辑。混合版本工作副本中不同项可有不同 BASE。
- `WORKING`：当前本地工作内容。仅支持右侧 `--to WORKING`，不是直接传给 SVN 的修订关键字；实现使用单修订 `svn diff -r N` 语义。
- `HEAD`：仓库最新修订。查询时可能联网，且服务器可在之后继续变化。
- 数字：非负修订号，包括 0。左、右均支持，支持数字修订的反向比较。

默认是 `BASE → WORKING`。左侧支持 BASE、HEAD、数字；右侧另支持 WORKING。不会伪造 `WORKING → 版本` 的反向补丁，选择此方向会明确拒绝。需要查看本地修改时将 WORKING 放在右侧。该接口不接受日期、任意 SVN 选项、修订范围字符串、COMMITTED 或 PREV；旧 CLI `log` / `diff` 接口仍保留其现有语义。

`diff` 是 SVN 内部生成的文本/属性差异。目录会比较其版本化后代，不会隐式升级为整个工作副本。二进制文件的提示可能出现，但不包含二进制载荷，也没有二进制预览、图片对比或可编辑合并功能。读取期间其他程序仍能改变文件，结果不是文件系统原子快照。

## 输出、安全与失败

- 必须是选中工作副本内的单一路径。路径外逸、`.svn` 管理目录、符号链接和 Windows 重解析点被拒绝。逐项检查所选工作区与目标的实际工作副本根、仓库 UUID 和相对路径对应的 URL，拒绝悄悄跨入嵌套副本、其他仓库或指向另一处的 switched / external 目标。需要查看它们时请显式打开对应工作区。文件名中的空格、中文、开头 `-` 和字面 `@` 保留为目标，不当作选项或自定义 peg revision。
- 允许 http、https、svn 和 file 仓库。不会执行 svn+ssh 的外部 SSH 助手。此限制只针对新接口；不代表项目其他功能均有相同限制。认证必须预先用可信 SVN 客户端配置，不请求密码、不接受 URL 密码、不跳过证书验证。
- 读取前用有界的本地 `svn info` 确认目标和所选工作区，每次必须只返回一个有效条目。目标缺失或不支持时明确失败，绝不会改查 `.`。SVN 进程使用参数数组，关闭交互和认证缓存，比较强制内部 diff。
- 两条输出管道并发读取。单次 info 最多 256 KiB，历史 XML 最多 4 MiB，diff 原始输出最多 8 MiB；每条命令 stderr 最多 64 KiB。超过上限终止读取并失败，不展示不完整 XML 或被截断的诊断。每个 SVN 进程有独立超时，一次请求含一至两次本地 info 和一次 log/diff。
- 错误只展示退出状态和稳定的 SVN 错误码，不回显原始诊断、argv 或认证信息。完整成功文本先遮蔽 URL 中的密码，再按 UTF-8 截断，因此不会因展示截断而泄漏半个 URL 密码。
- `compare` 默认展示 256 KiB，可用全局 `--max-text-bytes` 调整至最多 8 MiB。`diff.truncated`、`source_utf8_bytes`、`returned_utf8_bytes` 明确标记展示范围，字节数基于解码并遮蔽后的 UTF-8 文本。截断结果不能当作完整补丁应用。整个 JSON 仍受 `--max-output-bytes` 约束。
- 这不是内容秘密扫描器。用户请求查看的源文件、提交消息本身可能包含秘密，请勿把结果转发给不应接收它的人。
- 无自动重试、回滚、更新或其他写入补救。失败不代表路径无历史或两个版本相同。
- 超时后终止与等待清理也有时间边界；若无法确认 SVN 进程已退出，明确报告清理未验证，不返回部分内容。此时请先检查仍在运行的 SVN 读取进程，不会自动再启动一份。

## 验证范围

```console
python -m unittest discover -s tests -p test_history.py -v
python -m unittest discover -s tests -v
```

单元测试覆盖路径和选项隔离、peg 转义、分页、版本组合、二进制边界、UTF-8 截断前遮蔽、错误脱敏、超时和 stdout/stderr 上限。管道测试实际启动本机 Python 测试进程，不是实际 SVN。可选集成测试使用临时本地 SVN 仓库检查历史筛选、版本差异和前后状态未改变；只有安装 `svn` 与 `svnadmin` 时才执行。

本次环境没有 `svn` / `svnadmin`，真实 SVN 集成测试跳过；原生 Windows、真实认证/服务器和 Tk 图形界面未验证。不得把替身或 Python 管道测试通过描述成真实 SVN / Windows 已通过。

参考 SVN 官方手册：[svn log](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.log.html)、[svn diff](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.diff.html)。各发行版支持细节应以安装版本为准。
