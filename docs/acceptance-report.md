# Context Hub MVP 验收报告

验收日期：2026-09-05。

## 结论

- 功能回归：19/19 通过，包括 Unicode 与哈希分页、FTS5/LIKE 分流、版本替换与
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
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：19 个测试全部通过，用时 17.536 秒。覆盖的关键不变量包括：

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
每轮均得到恰好 400 条有效事件，随后完整 19 项回归亦通过。

## 10,000 条性能验收

执行命令：

```powershell
.\.venv\Scripts\python.exe scripts\run_acceptance.py `
  --output .context-hub-test-data\acceptance-report-ci-fix.json
```

| 检查项 | 实测 | 目标 | 结果 |
| --- | ---: | ---: | --- |
| 核心搜索 p50（200 次） | 2.498 ms | <= 50 ms | 通过 |
| 核心搜索 p95（200 次） | 2.975 ms | <= 150 ms | 通过 |
| 预热 MCP 搜索 p95（40 次） | 4.880 ms | <= 500 ms | 通过 |
| STDIO 启动并完成 `tools/list` | 609.309 ms | <= 1000 ms | 通过 |
| `doctor` 精确计数 | 10,000 / 10,000 | 相等 | 通过 |

补充测量：完整重建 10,000 条索引用时 328.513 ms，核心首次搜索 4.470 ms。

使用 Python `-X importtime` 诊断确认，约 1.45 秒主要消耗在 MCP SDK 2.1.1 顶层
便利包对客户端、HTTP、认证和遥测依赖的提前导入。当前 STDIO 专用进程改为只加载
官方协议类型与官方 STDIO 传输所需模块，不修改 site-packages、不降低阈值，也不跳过
initialize；连续五轮冷启动为 549.655–601.723 ms。普通库导入仍走兼容路径并有独立
回归测试。

## 未覆盖与下一验收点

- 当前 OpenAI 官方文档不提供 ChatGPT Desktop 直接启动本地 STDIO MCP 的接入方式，
  因此 ChatGPT Desktop 原生端到端验收尚未完成。
- Secure MCP Tunnel 是当前官方的本地或私有 MCP 接入路径，但会创建外部状态并需要
  组织权限、隧道 ID 和运行时密钥；未经用户另行授权未执行。
- 若继续客户端验收，仍只使用合成数据，并需要在真实 ChatGPT 支持面完成
  `tools/list`、search、稳定 ref 分页 read 与 SHA-256 对照。
