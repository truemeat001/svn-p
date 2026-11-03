# SVN Desk（预览版）

一个参考 P4V 操作布局与工作流的独立 SVN 桌面客户端，使用 Python 标准库、Tk/ttk 原生控件和系统安装的 Subversion 命令行程序。提供桌面操作及面向脚本、AI 的 JSON CLI。

**当前是源码预览版，Windows 打包尚未完成。** 不包含 Python、SVN、安装器或打包好的 `.exe`。旧基线 `2c6b681` 加 Windows 修复已经在原生 Windows 上通过一轮测试，其中包括真实本地 SVN 集成测试；**本 README 对应的最新合并源码包含之后的连接诊断、Shelf 批处理和额外修复，尚未重新完成原生 Windows / 真实 SVN 验证**。两者不能混为一个已验收版本。详细版本范围见下方“测试与验证状态”。请先使用一次性测试仓库，独立备份重要工作。

本项目不隶属于 Perforce 或 Apache Subversion，也不复制 P4V 的服务器语义或全部功能。

## 快速开始

### Windows

1. 安装可信来源的 **Python 3.10 或以上版本**，包含 tkinter；安装可信来源的 **Subversion 命令行客户端**，确保 `svn.exe` 可用。仅安装图形客户端不一定包含命令行组件。
2. 解压完整项目目录，不要只复制 `main.py`。不需要安装第三方 Python 包。
3. 双击 `启动.bat`，或在项目目录运行：

```powershell
py -3 main.py
```

指定已有工作副本，或先看不运行 SVN 的只读示例界面：

```powershell
py -3 main.py "C:\Work\Project"
py -3 main.py --demo
```

依赖自检：

```powershell
py -3 --version
py -3 -m tkinter
svn --version --quiet
```

没有 `py` 启动器时，可使用对应的 `python` 命令。界面可选择已安装的 SVN 可执行文件；本程序不会下载或安装 SVN。真实仓库需要事先通过可信 SVN 客户端配置认证和证书。本工具不接收密码参数、不新增认证缓存、不绕过证书错误。

### Linux / macOS

可在已安装 Python、tkinter 与 Subversion 的环境中使用 `python3 main.py`。部分系统需要单独安装 tkinter；这不是已完成各平台打包和原生交互验证的承诺。

## 已实现的工作流

| 功能 | 桌面界面 | JSON CLI | 主要边界 |
| --- | --- | --- | --- |
| 工作副本管理 | 打开、命名登记、切换、启动恢复、扫描父目录、受限检出 | 工作操作显式指定已有 `--workspace` | 不是服务器连接档案或 P4V workspace 映射 |
| 连接诊断 | “文件 → 连接诊断”，本地检查或明确确认 URL | `diagnostics`，工作副本可选 | 默认不访问仓库；不是登录、认证修复或写权限测试 |
| 浏览与检查 | 工作副本/仓库导航、状态、文件、记录 | `status`、`info`、旧 `log/diff` | 默认状态采用有界管道与增量 XML 解析；仍返回完整结果列表 |
| 历史与版本比较 | 单一路径历史、分页、明确版本端点比较 | `history`、`compare` | 新接口有输出上限；二进制不返回载荷，无三方编辑器 |
| 常用 SVN 操作 | 添加、更新、选中项提交、还原 | 没有对应的通用命令入口 | CLI 提交入口仅为命名 changelist 提交 |
| 原生变更列表 | 新建并归组、移动成员、移回未分组、按列表提交 | `changelist list/assign/remove/commit` | 本地文件标签，不是服务器编号提交单 |
| 本地 Shelf | 创建、检查、恢复、删除 | `shelf create/list/inspect/restore/delete` | 备份后不清空工作区，不上传服务器，不自动合并 |
| 冲突处理 | 筛选、检查、预检、明确确认保留已手工修复的内容 | `conflict list/inspect/resolve` | 仅受限普通文件的内容冲突，唯一方案为 `working` |
| 文件操作 | 右键或“文件与锁”菜单 | `file delete/move/copy` | 同一根内普通已版本化文件，不支持目录或覆盖 |
| 仓库文件锁 | 检查、明确选择加锁/解锁、预检和确认 | `lock inspect/acquire/release` | 不抢锁、不破锁，只释放当前副本真正持有的锁 |
| 切换与合并 | “分支与合并”菜单，完整根范围和双重确认 | `branch switch/merge` | 严格完整干净副本、固定数字修订；合并只产生本地待审查更改 |
| 分支 / 标签计划 | 只读计划窗口，没有创建按钮 | `branch plan-copy` | **只有预检，没有创建执行，不发送仓库 copy/commit** |

桌面和 CLI 使用共同后端，但可用入口不完全相同。两者均没有通用 SVN 命令输入器；未覆盖的命令请使用可信外部 SVN 客户端。

## 工作区与多个仓库

- “打开…”验证已有工作副本后登记名称和路径；下次正常启动恢复上次成功打开的工作区。启动参数明确指定的路径优先。
- 工作区下拉框切换已登记副本；“管理…”可重命名、重新定位、移除记录、关闭自动恢复。**移除记录不删除、移动或还原工作副本文件。**
- “新建 / 检出…”仅接受空目录或父目录已存在的新目录。支持 `http`、`https`、`svn`、`file` URL；不支持内置 `svn+ssh` 检出，不覆盖、不嵌套到现有工作副本，不自动检出 externals。
- URL 保留 SVN 的 `@修订` 语法，不强制改成 HEAD。检出失败或超时可能留下部分副本，不自动清理或重试；先检查目标，再决定如何继续。
- “扫描父目录…”有深度、条目数、候选数和时间边界。候选逐个通过 SVN 验证，可独立登记和选择；嵌套副本不自动等同于 externals。
- 多仓库指**多个独立工作副本之间切换**。父目录不是联合工作副本，不跨根组合更新、归组、提交、恢复或冲突解决。

登记保存在当前用户的 `.svn-desk/settings.json`，Windows 通常为 `%USERPROFILE%\.svn-desk\settings.json`。保存名称、本地路径、SVN 程序路径和启动偏好，不自动保存仓库 URL 或认证秘密。损坏设置不会静默覆盖；多实例同时修改登记不保证合并。

详见 [工作区与多个 SVN 副本](docs/WORKSPACES.md)、[检出安全边界](docs/CHECKOUT.md)。

## 只读连接诊断：先区分本地与远程

入口为“文件 → 连接诊断（本地 / 显式 URL）…”。**打开窗口不会自动执行检查**；点击“本地检查”才读取 SVN 版本及当前工作副本的本地身份。未打开工作区时只检查已安装 SVN，不会跟随工作副本内保存的仓库 URL。默认本地成功不代表服务器连接或认证成功。

远程 URL 初始为空，同意框未勾选；需自行填写 URL、勾选明确检查，并在默认“否”的确认窗核对完整地址后，才发出一次只读 `svn info`。修改地址或开始一次请求后，同意会清除。不会尝试备用地址、自动重试或后台轮询。

- 远程检查支持无用户名/密码的 `http`、`https`、`svn` 和本机 `file` URL；拒绝 svn+ssh、远程 file/UNC URL、查询串、片段和不安全输入。路径内 `@123` 是字面名称，检查 HEAD，不是历史修订输入。
- 可使用预先配置的 SVN 凭据，服务器可能记录请求；不收集新凭据、不新增认证缓存、不接受/绕过证书、不修改代理，也不执行检出、更新、提交或锁操作测试权限。
- 报告分开显示版本、本地身份、远程请求和错误分类。`remote_requested` 是是否明确请求，`remote_attempted` 只表示远程阶段开始，不证明服务器收到请求。
- UUID 相同不证明分支 URL、修订或内容相同；不同也不会自动 switch/relocate。`info` 成功只证明当时能读取该目标，不证明提交、钩子或全部子目录权限。
- 错误使用固定说明和数字 SVN 错误码，不回显原始 stdout/stderr、任意服务端文本、证书或令牌。分类是排查线索，不是确定根因。
- GUI 不把输入 URL 或报告写入设置/程序日志，不自动导出。截图和 CLI JSON 仍可能含本地路径、仓库身份，分享前应检查。
- 关闭窗口只放弃结果显示，不保证终止已启动进程。默认每个进程超时 15 秒，一次最多三个进程；没有整个报告 15 秒完成的承诺。

“本地检查”表示不发起 SVN 仓库请求，不是强制断网：若所选程序或目录在网络映射盘/挂载存储，读取它仍可能访问该文件系统。需要完全离线时应选择本机存储。底层 2 MiB XML 限制是捕获后的解析上限，不是全程流式内存配额。

详见 [连接诊断模块与安全语义](docs/CONNECTION.md)、[连接诊断 GUI](docs/CONNECTION_GUI.md)。诊断不能替代可信 SVN 客户端的账号/证书配置，也不代表真实网络和 TLS 已验收。

## 界面与性能边界

- 可调整宽度的左侧工作副本/仓库导航，右侧对象页签，下方详情、文本差异与操作记录。
- 原生蓝灰深色主题、可用中文字体回退、独立的正文/等宽预览字体和 Windows DPI 初始化；状态同时使用文字，不只依赖颜色。
- 工具栏随宽度换行；文件、分组与目录使用不同操作入口，无适当对象时禁用相关动作。
- 工作副本目录展开时后台读取；宽目录分页，每页最多 200 项。文件页提供上一页/下一页，不再只显示全树前 3,000 个文件。
- 选择目录只改变浏览范围，不自动把目录及子树作为修改选区。文件操作和冲突流程显示完整精确范围，确认期间切换工作区或选区会取消旧计划。
- 选择对象立即显示详情；显式差异操作读取内容，过期异步结果不会覆盖新选择。

**尚无大工作区性能保证。** SVN 状态查询仍可能扫描整个工作副本，结果列表仍随条目数增长，待提交树没有行虚拟化；大型列表插入与逐文件 SVN 查询仍可能较慢。没有持续自动监控、拖放归组、GUI 取消按钮或跨进程事务隔离。

默认 `SvnClient.status()` 已改为有界 stdout 队列和增量 XML 解析，不再积累整份原始输出及完整 XML 树。只有 XML 完整、进程结束且退出成功才交付完整列表；超时、取消、解析错误或超限均失败，不用部分状态假装干净。后端可接收 `cancel_event`，本轮没有增加 GUI 取消入口。

状态 stdout 块为 64 KiB、最多排队 4 块，XML 总量上限 512 MiB，条目间数据上限 4 MiB；这些不是整个调用的内存上限。stderr 持续排空但只保留前 64 KiB，截断有标记。其他 SVN 命令仍可能整体缓冲输出，不能把状态路径的改进扩展为全应用内存保证。

主题与 DPI 逻辑有无显示自动测试，旧基线的 Windows 修复副本也启动过演示窗口；但窗口捕获与交互验收未完成，不能宣称外观或操作已通过。Windows 100%–200% 缩放、跨显示器和最新版原生窗口验收仍待完成。系统标题栏、文件选择器或消息框可能保持系统样式；Tk 不保证控件动态跨屏缩放，必要时在目标显示器重启。

详见 [界面设计](docs/UI_DESIGN.md)、[字体与 DPI](docs/DISPLAY.md)、[流式状态与对比测量](docs/STREAMING_STATUS.md)、[其他合成性能基准](docs/PERFORMANCE.md)。已记录的 Linux 10 万条合成状态样本中，Python 峰值追踪分配从约 167.7 MiB 降至 47.6 MiB（约 71.6%）；较小样本并非都提速。该结果不含真实 SVN、GUI、RSS、输入数据或子进程，不是 Windows/真实工程性能承诺，也不证明语言迁移会更快。

## 单一路径历史与版本比较

- 选择一个已版本化文件，点击“文件历史”或右键入口。历史页显示明确相对路径，也可显式输入目录；只有 `.` 或“工作副本根历史”表示根范围。目录历史包含后代，不转化为提交范围。
- 默认从 HEAD 查询相关历史，不更新工作副本。GUI 每页可选 25、50、100、250 项，只保留当前页及有限回退游标；CLI 默认 100 项、每页最多 1000 项，用 `next_start_revision` 继续向前翻页。
- 日志中每次提交的 `changed_paths` 是该次提交全部改动，可能包含查询范围以外路径。GUI 详情有 64 KiB 显示上限并标明截断，不会据此自动扩展比较范围。
- 双击历史修订打开当前路径的 `r(N−1) → rN` 比较；“比较版本…”允许明确左右端点。默认 `BASE → WORKING`，左侧支持 BASE、HEAD、数字，右侧另支持 WORKING。不提供 WORKING 在左侧、日期或任意 peg/URL 编辑器。
- GUI 新比较流程默认显示最多 256 KiB，可选 64/256/1024 KiB；CLI `compare` 可用 `--max-text-bytes` 调整，最多 8 MiB。新接口底层 info/history/diff 也有独立硬上限，超限失败，不把不完整 XML 当作结果。
- 比较只返回内部文本/属性 diff，无二进制载荷、图片比较、可编辑合并或应用补丁。空文本不证明二进制相同，截断文本不是完整补丁。
- 改变路径、页大小、修订或工作区会使旧请求失效；只读结果不自动写文件、update、merge 或重试。新接口拒绝跨根、switched/external、链接及 svn+ssh；旧 `log/diff` 保留既有语义和边界。

详见 [历史、比较及 GUI/CLI 参数](docs/HISTORY.md)。此处的有界读取与显示上限**不代表旧批量“查看差异”或全部后端命令都已改造**。

## 创建与提交变更列表

1. 打开工作副本，在“待提交更改”中点击“新建变更列表…”。
2. 填写名称，明确选择已版本化文件后归组。未版本化文件先添加；目录不能加入原生 SVN changelist。
3. 选择文件行后用“移动到列表…”加入其他列表，或用“移回未分组…”清除标签。归组不改变文件内容。
4. 选中命名列表标题，再点击“提交所选变更列表…”。确认页列出全部成员，包括筛选隐藏和正常未修改的已归组文件。
5. 核对范围、填写提交说明并明确确认。成员变化会拒绝；已知父目录或移动配对依赖不满足时，不自动扩大提交范围。

SVN changelist 是**工作副本本地标签**：一个文件只能属于一个列表，不能保存持久空列表，不保存文件内容、不锁文件、不跨电脑同步。最后一个成员移走后列表消失；成功提交文件的标签默认按 SVN 行为移除。CLI 可用 `--keep-changelist` 保留标签。

完整成员哈希只绑定列表名称、工作副本和成员路径，不冻结文件内容；提交时不要并行编辑。预检不证明服务器权限、钩子或最新版本检查一定通过。

## 本地 Shelf：备份，不是服务器搁置

创建 Shelf 保存选中受支持改动的内容和属性，**不还原、不删除、不提交、不上传工作副本**。支持普通文件修改（含二进制）、受限新增/删除、未版本化文件和目录以及 SVN 属性。恢复前验证仓库身份、BASE、实际文件内容/属性、备份校验和及目标不存在等条件，存在预测冲突则拒绝。

- 当前写入格式为 2：按实际节省选择 zlib 压缩或原始字节，单个 Shelf 内相同内容复用 blob。格式 1 仍可读取/恢复，不自动迁移；旧版程序可能无法读取格式 2。
- 文件数据分块捕获、校验和恢复。新格式限制每文件 16 GiB、每 Shelf 总文件内容 64 GiB；临时空间可能需要当前文件约两份数据，不能把压缩当作空间保证。
- 不保存/恢复 changelist 标签；ACL、所有者、完整时间戳等也不保证保存。
- 带历史复制/移动、替换、删除目录、冲突、switched、externals、链接、稀疏选择等不支持。文件操作产生的 copy/move 状态不能因此假定可被 Shelf 完整保存。
- 不保存冲突辅助文件集合；遇到冲突时先自行独立备份，不能依赖 Shelf 替代冲突备份。
- 恢复不合并，不覆盖已有新增目标。成功后保留 Shelf；意外 I/O 或 SVN 失败仍可能部分恢复，不自动破坏性回滚。
- 删除 Shelf 永久删除本机该份备份，不删除工作文件。备份整个 Shelf 目录，而不是只保存 manifest。

### Shelf 元数据批处理

创建和创建预检已经把所选目标的 `info`、当前属性、BASE 属性及末次属性复核合并为有界批次：每批最多 128 个明确目标、目标参数预算 12,000 字节，使用 `--depth empty`，不递归扩大范围。按规范路径匹配 XML，不依赖返回顺序；额外/重复目标、缺失 info 或失败命令的部分 XML 均拒绝。只有整条命令成功，省略的无属性目标才解释为空属性。

这不是跨操作缓存。每次重新读取，源内容、属性、状态、目录结构和变化检测仍保留；每文件 pristine `cat` 和 translated BASE `export` 也未省略，恢复继续独立逐路径验证。先预检再创建仍需两轮完整检查。

同一组 100 个短路径修改文件的回归中，创建从 603 降为 207 次**模拟 SVN 调用**，减少约 65.7%，并核对 manifest、恢复字节和二进制属性一致。**不是耗时减少 65.7%。** 最新同参数合成样本也没有测出创建耗时提速，模拟器没有实际 SVN 进程启动成本；真实 Windows/SVN 收益仍需测量。长路径或更多目标会增加批次数。

详见 [Shelf 格式、批处理与恢复](docs/SHELVES.md)、[合成复测口径](docs/PERFORMANCE.md)。它不是 P4V 的服务器 shelf，也不是 SVN 实验性 `svn shelve` 格式。

## 冲突：先手工修复，再标记 working

通过“冲突”菜单或文件右键检查确切冲突节点。先用可信编辑器或 SVN 工具修复、保存并核对当前文件，再明确选择 `working`，经过后台预检和风险确认后标记已解决。

仅支持实际工作副本根内普通文件的纯内容冲突，可以是文本或二进制；单次最多 128 个节点，每个内容指纹文件最多 32 MiB。**没有三方编辑器、自动合并、属性/混合/树冲突处理，也不提供 `base`、`mine-full`、`theirs-full`。**

SVN 解决命令可能移除冲突记录及辅助文件；状态摘要包含当前工作文件内容，但不保护辅助文件内容。请提前独立保存重要工作文件和辅助文件。成功只说明冲突状态检查通过，不证明合并内容正确，也不会提交。

详见 [冲突后端与 CLI](docs/CONFLICTS.md)、[冲突 GUI](docs/CONFLICT_GUI.md)。

## 文件删除、移动与带历史复制

在“工作副本文件”或“待提交更改”中选择具体文件，使用右键或“文件与锁”菜单。

- 删除：一次最多 128 个普通已版本化文件，内容/属性必须干净；与工作副本外临时导出的 BASE 比较，防止仅依赖缓存时间戳。
- 移动/重命名、复制：一次一个源文件，允许受支持的本地内容/属性修改；目标必须是不存在的精确文件路径，父目录已经版本化。
- 不支持目录、URL、跨根、覆盖、大小写改名、externals、switched、冲突或已有结构变更，不自动创建父目录。
- 确认页显示全部源、目标、受影响路径、风险和状态摘要；默认取消，人工确认后再复检。执行后检查精确本地结果。

这些操作只登记待提交变化。删除会移除本地文件；移动须一起审查两端并另行提交，changelist 标签不保证转移。最后检查之后仍存在外部进程竞态，预检不是事务；失败后先核查，不自动重试或回滚。

详见 [文件操作后端与 CLI](docs/FILEOPS.md)、[文件与锁 GUI](docs/FILE_OPERATIONS_GUI.md)。

## 仓库文件锁

明确选择已提交的普通文件，使用“仓库锁…”检查服务器所有者、备注及当前工作副本是否真正持有锁；随后可选择加锁或解锁，经预检、范围/备注确认后执行。

- 每次最多 128 个文件，查询逐文件访问服务器；加锁前检查是否落后于服务器。
- 不抢锁、不破锁、不使用 `--force`。同一个用户名在其他副本取得的锁，也不能冒充当前副本持有。
- 锁影响他人提交，但不禁止本地编辑；`svn:needs-lock` 可能改变文件写权限，正常提交通常释放已收集到的锁。
- 加锁备注会发送到服务器，可能被团队看到，不要填写凭据或秘密。界面/JSON 不输出原始锁令牌，SVN 自身仍维护原生令牌。
- 不支持目录、待新增/删除/复制/移动、冲突、外部引用、switched、链接或硬链接。内置锁流程不支持 `svn+ssh`。
- 多文件锁不是原子操作，退出码零也不能单独证明全部成功；后端重新读取服务器验证，部分失败按不确定结果处理。

详见 [锁语义与 CLI](docs/LOCKS.md)。仓库文件锁不是 SVN 工作副本管理锁，本功能不执行 `cleanup`。

## 分支与合并：实验性的完整工作副本流程

顶部“分支与合并”菜单提供以下三个入口。**切换和合并针对当前打开的整个实际工作副本根，不是当前文件选区或 changelist。** 请首先只在一次性测试仓库验证。

1. **切换整个工作副本**：填写同一仓库目录 URL 和明确非负数字修订；先只读预检，再核对完整根、映射、修订、风险和摘要，人工完成两项确认后执行。`svn switch` 没有原生 dry-run，预检不是切换模拟，不保证祖先关系或最终成功。完成后复核 URL、修订和状态，不提交仓库。
2. **合并明确向前范围**：填写源目录 URL 与 `0 ≤ start < end`，最多 64 个修订。范围为 **(start, end]**，来源固定为 `URL@end`；预检实际运行 `svn merge --dry-run`，并检查中间源修订状态。识别到预演冲突即拒绝应用；没有预演冲突也不证明最终无冲突或所有修订均需应用，原生 mergeinfo 可能跳过已合并部分。
3. **分支 / 标签计划**：检查已提交源目录、数字修订、目标和拟提交说明，只展示计划。没有创建按钮、执行 API 或 CLI 开关，**不会发送 `svn copy` 或 `commit`**。URL → URL copy 会立即提交仓库，且目标目录竞争存在精确路径风险，所以当前刻意不执行。标签不可修改仍需服务器策略，不由计划保证。

三个入口均要求严格完整干净副本：统一 BASE 修订、完整深度、最多 10,000 个版本化条目和 256 MiB 工作文件；没有本地修改、未版本化/忽略文件、稀疏目录、externals、switched 子树、链接、硬链接、嵌套副本等不支持情况。源树同样有属性/大小/路径检查。**这些是本实现首版限制，不是 SVN 本身限制。** 不会自动删除、update、cleanup 或 revert 来让预检通过。

URL 拒绝凭据、查询串、片段、svn+ssh 和路径穿越；`@` 视为字面名称，数字修订单独填写。这里的 URL 规则与检出页保留 `@修订` 的规则不同。预检可能全树读取及逐修订联网，不能把每进程超时当作整个操作总时限。

切换/合并执行必须绑定已审查的同一状态摘要；CLI 的 `--yes` 还必须同时提供 `--expect-state-sha256`，不会拿新预检摘要替换旧确认。状态摘要不是跨进程锁或事务；执行时停止编辑、编译和其他 SVN 修改。

实际合并只留下本地待审查内容/属性变化，可能包含 `svn:mergeinfo` 或冲突，返回 `needs_review` / `conflicted`，不是“已经合并提交成功”。请检查状态、差异、mergeinfo，运行项目测试，再另行决定提交。遇到错误不自动解决、回滚或重试；GUI 重新读取前明确显示状态未知，不把“0 项”当作干净证据。

详见 [分支与合并后端范围](docs/BRANCHES.md)、[分支 GUI 确认流程](docs/BRANCH_GUI.md)。反向合并、双 URL 合并、自动 reintegrate、record-only、部分目录 switch、混合修订和远端分支删除等仍未提供。

## 脚本与 AI：一次调用，一个 JSON 对象

CLI 不需要启动 GUI，也不依赖 tkinter。请在项目目录运行；工作操作将 `/wc` 换成已有工作副本根，Windows 可将 `python` 换成 `py -3`。除 `diagnostics` 外，所有命令仍要求明确 `--workspace`；诊断可不带工作副本。

```console
python -m svn_client.cli diagnostics
python -m svn_client.cli --workspace /wc diagnostics
python -m svn_client.cli diagnostics --check-remote --url https://svn.example/repo/trunk
python -m svn_client.cli --workspace /wc --timeout 10 diagnostics --check-remote --url https://svn.example/repo/trunk
python -m svn_client.cli --workspace /wc status
python -m svn_client.cli --workspace /wc info --path src/a.txt
python -m svn_client.cli --workspace /wc log --path src/a.txt --limit 20 --revision HEAD:1
python -m svn_client.cli --workspace /wc diff --path src/a.txt
python -m svn_client.cli --workspace /wc history --path src/a.txt --limit 100 --start-revision HEAD
python -m svn_client.cli --workspace /wc history --path src/a.txt --limit 100 --start-revision 125
python -m svn_client.cli --workspace /wc compare --path src/a.txt --from BASE --to WORKING
python -m svn_client.cli --workspace /wc compare --path src/a.txt --from 120 --to 125
python -m svn_client.cli --workspace /wc changelist list
python -m svn_client.cli --workspace /wc changelist assign --name fixes --path src/a.txt --dry-run
python -m svn_client.cli --workspace /wc changelist commit --name fixes --message "Fix parser" --dry-run
python -m svn_client.cli --workspace /wc shelf create --name before-change --path src/a.txt --dry-run
python -m svn_client.cli --workspace /wc shelf list
python -m svn_client.cli --workspace /wc conflict inspect --path src/a.txt
python -m svn_client.cli --workspace /wc conflict resolve --path src/a.txt --accept working --dry-run
python -m svn_client.cli --workspace /wc file move --source src/a.txt --destination src/renamed.txt --dry-run
python -m svn_client.cli --workspace /wc file copy --source src/a.txt --destination src/copied.txt --dry-run
python -m svn_client.cli --workspace /wc file delete --path src/old.txt --dry-run
python -m svn_client.cli --workspace /wc lock inspect --path assets/model.bin
python -m svn_client.cli --workspace /wc lock acquire --path assets/model.bin --comment "修改模型" --dry-run
python -m svn_client.cli --workspace /wc lock release --path assets/model.bin --dry-run
python -m svn_client.cli --workspace /wc branch switch --url https://svn.example/repo/branches/feature --revision 125 --dry-run
python -m svn_client.cli --workspace /wc branch merge --source-url https://svn.example/repo/branches/feature --start-revision 120 --end-revision 125 --dry-run
python -m svn_client.cli --workspace /wc branch plan-copy --kind tag --source-url https://svn.example/repo/trunk --destination-url https://svn.example/repo/tags/v1 --revision 125 --message "Plan v1 snapshot"
```

示例中的 URL、修订和文件名仅为语法示例，必须换成明确选定的实际对象。`branch plan-copy` 天生只读，不接受 `--yes`，也无需 `--dry-run`。

- 全局选项在命令之前，子命令选项在其后。支持多目标的命令可重复 `--path` 或使用 `--paths-json FILE|-`；只有显式 `-` 才从标准输入读取。
- `diagnostics` 默认只读本地信息。远程检查必须同时有 `--check-remote` 和明确 `--url`，不给其一就拒绝，不从工作副本猜测地址。不接受 `--yes`、`--dry-run`、密码或证书绕过选项。
- 修改命令必须 `--yes`，或用互斥的 `--dry-run` 预检。预检可以读取工作文件、访问服务器或使用清理后的临时文件，但不执行所请求的工作副本/锁/Shelf 修改。
- 先审查返回的完整范围、风险及摘要，再明确授权执行。changelist 提交使用 `--expect-members-sha256`；文件、冲突、锁以及分支切换/合并使用 `--expect-state-sha256`。各摘要保护内容不同，不能互换；**branch switch/merge 的执行强制要求旧预检摘要和 `--yes`**。
- 若分支合并预检的 `preview.truncated` 为真，不要基于不完整文本确认；以足够的 `--max-text-bytes` 重新只读预检并核对完整计划。计划生成、预演完成和服务器写入成功是不同阶段。
- `--yes` 是调用方的执行开关，不是用户授权系统。AI 仍须遵守用户授予的操作、数据和范围权限。
- 帮助也输出 JSON。检查 `schema_version`、`ok`、`outcome` 和 `error.code`，不要只凭退出码或消息文字决定重试。新合并流程即使调用返回成功，也会用 `needs_review` / `conflicted` 明确要求人工审查；没有提交仓库。
- `operation_uncertain`（8）表示修改结果不能确定，不能假定回滚。`output_limit`（9）也可能发生在操作已完成后，不得为了取得完整输出重新提交、恢复或删除。
- 诊断失败返回 `diagnostics_failed`（10）、`ok: false`，但正常输出大小内仍保留 `data.report` 的步骤、类别及安全信息。缺少 SVN、本地目录问题、认证/TLS/超时或身份不一致不应被误读为“报告未生成”。未请求远程检查时，`ok: true` 也不表示服务器已连接。
- 文本字段默认最多 262144 UTF-8 字节，JSON 默认最多 4194304 字节；存在明确截断/超限标志。文本 diff 不含二进制 payload，也不是可靠补丁导出。
- `--timeout` 普通命令默认 120 秒；诊断默认 15 秒，可设置大于 0、最多 120 秒。均按每个 SVN 子进程计算，不是整个多阶段请求的总时限，也不覆盖所有进程创建/认证设施行为。无自动重试。

参数和 JSON v1 契约见 [CLI 文档](docs/CLI.md)，各工作流详见 [连接诊断](docs/CONNECTION.md)、[历史与比较](docs/HISTORY.md)、[冲突](docs/CONFLICTS.md)、[文件操作](docs/FILEOPS.md)、[锁](docs/LOCKS.md)、[分支与合并](docs/BRANCHES.md)。读取、检查和删除已有本地 Shelf 可不安装 SVN；`diagnostics` 可报告 SVN 缺失，而不是要求先成功初始化客户端。真正需要 SVN 的操作仍必须满足依赖。

## 安全使用原则

1. **先备份，再使用一次性副本验证。** 还原会丢弃未提交更改，删除 Shelf 永久删除备份；“预检通过”不是备份。
2. **明确核对目标。** 文件/冲突/锁流程不自动扩大范围；分支 switch/merge 明确针对整个工作副本，一般更新、还原及目录 Shelf 选择也应仔细核对递归范围。
3. **不要并行修改同一工作副本。** 本应用的串行锁不能阻止外部编辑器、其他 SVN 客户端或另一个进程。
4. **失败不等于回滚。** 超时、中断、网络错误、部分写入或后续校验失败后，先读状态、内容、历史或锁状态，再决定下一步。
5. **保管日志和 JSON。** 路径、源代码、提交记录、属性和服务器备注可能包含私有数据；不要发送到未授权第三方。

## 测试与验证状态

在项目目录运行：

```console
python -m unittest discover -s tests -v
python -m unittest discover -s benchmarks -p "test_*.py" -v
python -m compileall -q svn_client tests benchmarks
```

自动测试覆盖后端参数与路径边界、设置/扫描/检出、连接诊断、原生 changelist、Shelf 数据完整性/压缩/批处理/恢复、流式状态、历史/比较、冲突、文件操作、锁、严格分支流程、JSON CLI、样式、DPI 和无显示 GUI 回调/确认流程。管道测试实际运行本地 Python 测试进程，验证有界读取、stdout/stderr、超时、取消和进程回收，**不是运行真实 SVN**。真实集成测试只创建自己的临时仓库，要求同时安装 `svn` 与 `svnadmin`；缺少时跳过。

### 按源码版本区分验证结果

**2026-11-03 已完成的原生 Windows 验证，不是最新合并版：**

- 对象：提交 `2c6b681d001217af2ae5b25e0d894a777326b51f`（简称 `2c6b681`）加对应 Windows 修复；不是这个原始提交未经修复就全部通过。
- 环境：Windows 11 x64 build 22631、Python 3.14.6、SVN / svnadmin 1.15.0、Tk 8.6.15。
- 完整测试：运行 **812 项，790 项通过、22 项跳过，0 失败、0 错误**。其中 **33 项真实 SVN 测试通过**，它们已包含在 790 项中，不另加总；另有 12 项基准工具测试通过。
- 真实 SVN 只访问自行创建的临时 `file://` 仓库，不连接用户服务器，不操作用户现有工作副本。22 项跳过主要涉及不可用的符号链接权限、FIFO/POSIX 能力；跳过不等于通过。
- 该版本**不包含后续连接诊断和 Shelf 元数据批处理**，也不覆盖最新合并后的新增改动。验证产生的是源码修复与证据，不是安装器、打包 `.exe` 或 Release。
- 演示窗口和菜单已启动；应用窗口捕获为空黑帧，独立 Tk 对照窗口也出现同样问题，因此没有完成视觉/交互验收。不能把启动成功写成界面显示正常、DPI 合格或可操作性已验证。

**当前最新合并源码：**

- 已将 Windows 文件身份/时间语义、中文参数与提交说明、检出目录及历史/差异路径等相关修复按上下文合入，保留连接诊断和 Shelf 批处理；另有最新集成修正。它不是上述旧基线测试目录的原样拷贝。
- 合并后的 Linux 回归与编译检查已有通过记录，但该环境没有 `svn` / `svnadmin`，对应真实 SVN 测试跳过。Linux 模拟和管道测试不能代替原生 Windows 复测。
- **最新组合尚未重新运行原生 Windows / 真实 SVN 测试。790/22 和 33 项真实 SVN 的结果只属于前述旧基线加 Windows 修复，不属于当前合并版。** 最新测试总数应以对冻结源码重新运行的日志为准，不沿用旧数。

实际远程认证、TLS、服务器权限/hooks、大仓库、安装/升级流程、最新版原生 GUI 和 DPI 均未完成端到端验收。无显示构造测试不等于窗口正确渲染，合成压缩/性能数据不等于真实工程表现。请在目标机器和 SVN 版本上重新运行，并人工检查实际界面。

## 仍未覆盖与后续方向

- 可靠精确目标的服务器分支/标签创建执行；目前只有只读计划。
- 更宽范围的 switch/merge、图形化合并跟踪、反向/双源合并和目录级结构操作。
- 三方合并编辑器、属性/树冲突流程及可靠绑定冲突辅助版本。
- 持久服务器连接档案、账号登录/凭据管理、认证或代理自动修复；当前只有显式只读诊断。
- externals 定义与固定修订管理。
- P4V 式服务器编号 changelist、可共享服务器 shelf、review 集成、拖放归组和跨根事务。
- 大列表虚拟化、减少逐文件 SVN 调用、GUI 取消/进度、剩余命令的有界输出，以及最新组合的原生 Windows / 真实 SVN 复测和大仓库性能验收。
- 安装器、打包 `.exe`、签名、升级流程及完整无障碍/高 DPI 交互验收。

后续能力应在精确范围、确认、失败后可核查和回归测试完整的前提下逐步增加，不用自动扩大操作范围来掩盖不支持的 SVN 语义。
