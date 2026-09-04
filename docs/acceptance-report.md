# Context Hub MVP 验收报告

验收日期：2026-09-05。

## 结论

- 功能回归：25/25 通过，包括 Unicode 与哈希分页、FTS5/LIKE 分流、版本替换与
  墓碑、允许列表、四进程并发写入、索引故障恢复、备份、只读工具隐藏，以及官方
  Python MCP SDK 客户端的真实 STDIO 握手与调用。
- 性能目标：核心搜索、预热 MCP 搜索和 STDIO 进程冷启动全部通过。
- 数据边界：所有输入均由测试在临时目录中即时生成；没有访问、注册或导入既有记忆。
- 客户端边界：未修改 ChatGPT Desktop 或 ChatGPT 工作区配置，也未把本地 SDK
  验收冒充为 ChatGPT Desktop 端到端验收。

性能脚本返回退出码 0，整体报告中的 `ok` 为 `true`；所有检查均使用即时生成的
合成数据。

## 环境

- Windows 10.0.26100
- Python 3.11.0
- SQLite 3.38.4，启用 FTS5 trigram
- MCP Python SDK 2.1.1

## 功能回归

执行命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

结果：25 个测试全部通过，用时 18.920 秒。覆盖的关键不变量包括：

1. 四个独立进程各追加 100 条，事实源最终恰好有 400 条有效 JSONL 事件。
2. 索引写入失败时事件已经 `flush`、`fsync` 并持久化，随后可用
   `doctor --reindex` 精确恢复。
3. 默认项目范围精确覆盖 `AGENTS.md` 与直接的 `context/*.md`，不递归扩展到嵌套目录。
4. 删除或篡改派生 SQLite 不会损失事实源，重建后稳定引用与未变化内容哈希保持一致。
5. 默认 MCP 只注册 `context_get`；写模式只有显式启用后才持久显示 `context_put`。
6. 官方 SDK 客户端真实启动子进程，完成 initialize、`tools/list` 和
   `context_get` search 调用；错误工具参数返回结构化错误且不导致进程崩溃。
7. 作为库导入 `context_hub.mcp_stdio` 不会替换或破坏官方 `mcp` 公共包。

合成测试产物仅允许写入本地忽略目录 `.context-hub-test-data/`，不会提交到仓库。

首次公开仓库 CI 在 GitHub 托管的 Windows Runner 上暴露出旧的 15 秒锁等待窗口不足：
慢速、受争用的磁盘把四个进程的持久化临界区串行时间拉长，三个写入进程因等待
`events.lock` 超时而失败。事实源 JSONL 仍逐事件执行 `flush` 和 `fsync`；派生且可重建的
SQLite 索引改用 WAL + `synchronous=NORMAL`，避免每条事件重复执行一次完整磁盘同步，
锁等待上限同步提高到有界的 120 秒。修复后，四进程各写 100 条的回归连续执行三轮，
每轮均得到恰好 400 条有效事件，随后完整回归亦通过。

## 运行手册 CLI 对齐验收

执行命令：

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\run_runbook_smoke.ps1 `
  -Output .context-hub-test-data\runbook-smoke-report-manual-alignment.json
```

结果：退出码 0，`ok=true`、`synthetic_only=true`、`external_fact_inputs=0`，全部
62 项检查为 `true`，用时 5344.786 ms。脚本逐条调用手册公开的 CLI，覆盖初始化与
幂等、默认允许列表、清单、项目检索、9 页稳定 ref/cursor 分页及 SHA-256、显式写入、
类型过滤、诊断、删除派生 SQLite 后精确重建、稳定 ref/哈希以及权威事实源备份。

本轮首次执行还复现了 Windows PowerShell 旧代码页破坏 CLI Unicode JSON 的问题；
手册和脚本现已显式设置控制台输入、输出及 Python 为 UTF-8，修正后整条链路通过。
脚本只在 `.context-hub-test-data/` 下创建唯一目录，验证后删除该目录并保留 JSON 报告；
`doctor` 精确返回 1 个项目、1 个事件和 2 个章节，备份只含 3 个权威文件。

## 三项目纯合成验收

执行命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\run_multiproject_acceptance.py `
  --output .context-hub-test-data\multiproject-report.json
```

结果：退出码 0，`ok=true`、`synthetic_only=true`、`external_fact_inputs=0`，全部
23 项检查为 `true`。验收脚本只在一个临时目录内即时生成 3 个项目、6 个 Markdown
事实源和 5 个 JSONL 事件；删除探针结束后索引中保留 11 个章节。

- 12 个固定检索案例全部 Top-1 命中，Top-3 召回率为 100%，同时覆盖中文 trigram、
  英文 trigram、单字 LIKE 和事件类型过滤。
- 6 个错误项目反向探针均为空，3 个同名章节查询只返回指定项目，未发生跨项目串扰。
- 分页拼接、字符数和 SHA-256 逐字一致；源文件编辑使旧 ref 失效，删除使索引项消失，
  并产生预期的 1 条缺失来源警告。
- 删除 SQLite 后，4 个抽样对象的 ref、内容、哈希、来源和位置完全一致；`doctor`
  精确返回 3 个项目、5 个事件、11 个章节。
- 官方 Python MCP SDK 启动本地只读 STDIO 进程，完成
  `manifest → cobalt 项目 search → read`，工具面、内容、来源和哈希全部匹配；该轮
  全链路用时 1014.237 ms。

## 10,000 条性能验收

执行命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\run_acceptance.py `
  --output .context-hub-test-data\acceptance-report-cold-median.json
```

| 检查项 | 实测 | 目标 | 结果 |
| --- | ---: | ---: | --- |
| 核心搜索 p50（200 次） | 3.631 ms | <= 50 ms | 通过 |
| 核心搜索 p95（200 次） | 5.037 ms | <= 150 ms | 通过 |
| 预热 MCP 搜索 p95（40 次） | 6.569 ms | <= 500 ms | 通过 |
| STDIO 启动并完成 `tools/list`（3 次中位数） | 768.602 ms | <= 1000 ms | 通过 |
| `doctor` 精确计数 | 10,000 / 10,000 | 相等 | 通过 |

补充测量：三次独立的 STDIO 冷启动为 789.202、753.177、768.602 ms；完整重建
10,000 条索引用时 467.302 ms，核心首次搜索 6.578 ms。

使用 Python `-X importtime` 诊断确认，约 1.45 秒主要消耗在 MCP SDK 2.1.1 顶层
便利包对客户端、HTTP、认证和遥测依赖的提前导入。当前 STDIO 专用进程改为只加载
官方协议类型与官方 STDIO 传输所需模块，不修改 site-packages、不降低阈值，也不跳过
initialize；连续五轮冷启动为 549.655–601.723 ms。普通库导入仍走兼容路径并有独立
回归测试。

GitHub 共享 Runner 曾在同一提交的首次执行中同时出现重建索引 4.2 秒和单次 MCP
冷启动 2.0 秒的 I/O 尖峰；原提交重跑后分别恢复为 500.894 ms 和 475.101 ms。为避免
单次宿主机抖动造成错误失败，当前验收启动三个全新的 STDIO 进程，以中位数对照原有
1000 ms 门槛，并在报告中保留全部样本。新增回归分别证明：一个异常高值不会误判，而
三个样本中多数超过门槛时仍会失败；阈值本身没有降低。

## 未覆盖与下一验收点

- 当前 OpenAI 官方文档不提供 ChatGPT Desktop 直接启动本地 STDIO MCP 的接入方式，
  因此 ChatGPT Desktop 原生端到端验收尚未完成。
- Secure MCP Tunnel 是当前官方的本地或私有 MCP 接入路径，但会创建外部状态并需要
  组织权限、隧道 ID 和运行时密钥；未经用户另行授权未执行。
- 若继续客户端验收，仍只使用合成数据，并需要在真实 ChatGPT 支持面完成
  `tools/list`、search、稳定 ref 分页 read 与 SHA-256 对照。
