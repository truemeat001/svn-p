# 可复现性能基准

本目录的性能数据来自**合成数据 + 当前 Python 实现**，不是 Windows 实机、真实 SVN 工作副本、服务器或 GUI 跑分。不能据此声称“换成 C++/Rust 后 SVN 提交会更快”，也不能把测试耗时当作真实备份、提交、更新或恢复的承诺。

## 文件与运行

- `benchmarks/benchmark_client.py`：只依赖标准库的测试程序，需要当前项目要求的 Python 与 tkinter；不会创建 GUI 窗口。
- `benchmarks/test_benchmark.py`：验证测试程序自身的混合状态、扫描排除、备份恢复哈希、压缩/raw 回退、请求计数与参数限制。
- `benchmarks/results-linux-20261103.json`：本次 Linux 合成实测原始样本、参数、源码 SHA-256、内存口径与限制。

在项目根目录运行。程序只在自动创建的临时目录中生成、修改、恢复、删除自己的测试数据，不接收真实工作副本参数，不执行 `svn`，不访问网络。JSON 输出使用独占创建，已有文件不会被覆盖。

### Windows PowerShell / 命令提示符

先做小规模验证：

```powershell
py -3 -X utf8 -m unittest discover -s benchmarks -p "test_*.py" -v
py -3 -X utf8 benchmarks\benchmark_client.py --status-sizes 100 1000 --scan-files 300 --small-files 5 --large-mib 1 --repeats 2 --output benchmarks\results-windows-smoke-01.json
```

与本次标准样本一致的负载：

```powershell
py -3 -X utf8 benchmarks\benchmark_client.py --status-sizes 1000 10000 100000 --scan-files 10000 --small-files 100 --large-mib 16 --repeats 3 --output benchmarks\results-windows-standard-01.json
```

扩大文件扫描与资产大小：

```powershell
py -3 -X utf8 benchmarks\benchmark_client.py --status-sizes 1000 10000 100000 --scan-files 100000 --small-files 1000 --large-mib 128 --repeats 5 --output benchmarks\results-windows-large-01.json
```

如需选择测试磁盘，增加 `--temp-parent D:\BenchTemp`，该目录须已存在；其中只会新建并清理一个独立测试子目录。生产代码验证 BASE 时另有系统临时文件，因此并不是所有 I/O 都会落在指定磁盘。先检查可用磁盘空间；压缩判断期间原始与压缩候选可能同时存在。

Linux / macOS 将 `py -3 -X utf8` 替换为 `python3`，路径分隔符换成 `/`。本次执行的是 Linux，以上 Windows 命令是复现步骤，**没有在 Windows 上运行过**。

每次使用新输出文件名。源码正在修改时不要比较结果；若结束时源码哈希改变，程序仍保存结果但返回退出码 `2`，并标记 `source_changed_during_run`。应在稳定源码上重跑。不要把正常开发中的这种状态当成客户端性能失败。

## 测了什么

### 状态解析与分组

直接调用实际 `SvnClient.status(verbose=True)`，仅把命令返回值替换为预先生成的 XML。数据包含中文、空格、`@` 路径、clean 与 changed changelist 成员、属性修改与树冲突：每 100 项中 80 normal、12 modified、3 added、2 deleted、1 missing、1 conflicted、1 unversioned。

另外调用实际 `grouped_entries`，分别测不筛选与筛选。XML、原始条目集合与数据生成不计入测量；解析时创建的 XML 树和结果对象计入。分组结果不是 Tk 控件，没有测量 Treeview 插入、绘制、滚动或选择响应。

### 文件浏览与刷新候选

生成每目录最多 1,000 个、每个 24 字节的小文件。测试实际 `directory_entries` 单目录读取，以及保留的 `local_files` 全量和 3,000 条限制扫描。设置 `.svn` 与嵌套工作副本标记，并断言它们的文件不会被递归扫描返回。

**当前界面采用按需展开的 `directory_entries`，刷新已不再调用全量 `local_files`。** 全量递归扫描数据仅作为算法成本对照，不代表当前每次打开或刷新窗口的成本。

另测实际 `read_groupable_status`：使用与磁盘文件对应的 80% 正常、20% 修改状态，包含解析、逐路径文件类型检查和候选整理。SVN 状态响应仍为模拟；没有测量真实 `svn status` 遍历或命令启动。

### 本地 Shelf

通过 `SyntheticShelfManager` **仅替换 `_svn` 边界**：info/status/proplist 返回确定的 XML，cat 读取合成 BASE，export 拷贝合成 BASE。生产代码的 preflight、压缩、哈希、临时文件、fsync、manifest、完整性检查、恢复与保留备份均真实执行。恢复后逐文件计算哈希，确认与原始测试数据一致。

- `small-modified`：100 个 4 KiB 的已版本化修改文件。基础版本和修改内容不同；属性为空。
- `large-compressible`：一个 16 MiB、可高度压缩的未版本化文件。
- `large-high-entropy`：一个 16 MiB、难压缩的未版本化文件，验证 raw 回退。由固定种子的 1 MiB 伪随机块重复组成，不代表所有真实二进制格式。

每种负载分别测 create preflight、create、仅一个备份的 list、restore preflight、restore。重置目标、生成数据、清理备份与最后的额外哈希断言在计时外。生产代码自身的安全校验在计时内。

记录 `simulated_svn_calls`，代表当前算法的调用数量，**不是实际进程启动次数或真实 SVN 耗时**。模拟过程不含认证、网络、SVN 数据库、工作副本锁、属性变更、EOL/关键字转换。不得用这种恢复成功替代真实 SVN 集成测试。

大文件负载为未版本化文件，不经过 `svn cat BASE`。当前版本化 BASE 的 cat 路径仍把返回值整体缓存在内存中，所以不能把大文件测试的低峰值扩展到所有版本化资产。

## 统计与内存口径

- 每项先一次不计时预热，再重复计时。保存每次样本、median/min/max，不从三个样本宣称 p95。
- 内存另跑一次 `tracemalloc`，不会把追踪开销混入正常耗时。
- `peak_traced_mib` 只表示测量窗口内 Python 可追踪分配，不是进程 RSS、Windows Working Set、所有原生库分配、操作系统页缓存或子进程内存。也不包含已生成的输入 XML、条目、文件和其他基线对象。
- 测量前显式 GC，计时中保留自动 GC。缓存未清空，数据刚生成且执行过预热，因此属于暖缓存结果。
- 不测 GUI 首屏、动画帧率、真实用户输入、冷启动、安装体积、Windows Defender 扫描、网络盘或机械盘性能。
- JSON 中 `source_sha256` 是实现文件指纹；不同指纹、Python、硬件、文件系统和后台负载的样本不能当作严格的语言或版本对比。

## 本次结果

原始数据：[results-linux-20261103.json](../benchmarks/results-linux-20261103.json)。环境为 Linux x86_64 / Python 3.12.14，程序报告 9 个逻辑 CPU；处理器型号接口返回空值。每项 1 次预热、3 次计时，另一次内存追踪；`source_changed_during_run=false`。

| 状态条目数 | 解析中位数 | 解析峰值追踪分配 | 分组并筛选中位数 |
| --- | ---: | ---: | ---: |
| 1,000 | 11.6 ms | 1.7 MiB | 0.3 ms |
| 10,000 | 115.2 ms | 17.6 MiB | 3.4 ms |
| 100,000 | 1,534.4 ms | 167.7 MiB | 40.2 ms |

10,000 文件的单目录浏览（目录内 1,000 个文件）为 **26.4 ms**；当前刷新状态解析与文件候选检查合计 **187.7 ms**，峰值追踪分配 **11.5 MiB**，返回 2,000 个修改项与 10,000 个候选。作为对照，保留的全量递归扫描为 **335.3 ms**，3,000 项限制扫描为 **101.2 ms**；二者不能互称完整的同等工作量。

| Shelf 合成负载 | 创建 | 恢复 | 创建峰值追踪分配 | 实际 blob 字节数 |
| --- | ---: | ---: | ---: | ---: |
| 100 × 4 KiB 已版本化修改 | 78.9 ms | 92.4 ms | 1.5 MiB | 6,040 |
| 16 MiB 可压缩未版本化文件 | 96.9 ms | 51.4 ms | 3.2 MiB | 40,786 |
| 16 MiB 高熵未版本化文件 | 440.3 ms | 32.1 ms | 3.6 MiB | 16,777,216 |

所有恢复均验证了内容哈希；可压缩数据使用 zlib，高熵数据回退 raw。压缩比仅反映此合成数据，不能推断真实项目资产的节省比例。

100 个修改文件的创建调用了 **603 次模拟 SVN 边界**：101 次 info、2 次 status、300 次 proplist、100 次 cat、100 次 export。真实程序对应的命令启动与 SVN 工作没有计入这些时间。创建 preflight 也需要这些读取；若先预检再创建会再执行一次。不能将这里的 78.9 ms 作为真实仓库的 100 文件备份耗时。

这些结果支持的结论是：状态 XML 与条目构建仍随条目数增长；单目录按需浏览避免了全工作副本枚举；流式大文件未版本化备份没有在本次测试中分配整个 16 MiB 文件的 Python 缓冲区；逐文件 SVN 元数据调用仍值得优化。它们**不证明 Windows 界面已经流畅、真实仓库速度或某种语言一定更快**。

## 后续性能验收

1. 在同一台 Windows 机器的固定电源模式、本地 SSD、同一 SVN 版本上复现；同时记录 CPU、内存、文件系统、杀毒状态和是否冷缓存。
2. 真实 SVN 测试分开记录原生命令时间与客户端额外开销，不能将网络、服务器端 hook 或仓库操作误记为 Python UI 开销。
3. UI 要单测首次可见行时间、滚动和选择响应、快速重复筛选、目录展开、旧任务结果丢弃、任务取消与高 DPI。建议以 100 ms 以内的交互响应作为设计目标，而非已经达到的承诺。
4. 工作副本逐级扩大到 1 万、10 万、100 万条目；大文件同时覆盖已版本化和未版本化。真实 Shelf 测试补齐属性、CRLF、关键词、冲突、并发修改、损坏压缩数据、部分失败与恢复后内容校验。
5. 优先减少逐文件 SVN 调用、重复解析和同步 UI 重建。迁移语言或 UI 框架后必须运行同样负载及安全回归，再比较；不能用删掉验证、忽略文件或截断结果来换取“更快”。
