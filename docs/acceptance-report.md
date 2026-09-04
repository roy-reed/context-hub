# Context Hub MVP 验收报告

验收日期：2026-09-04。

## 结论

- 功能回归：17/17 通过，包括 Unicode 与哈希分页、FTS5/LIKE 分流、版本替换与
  墓碑、允许列表、四进程并发写入、索引故障恢复、备份、只读工具隐藏，以及官方
  Python MCP SDK 的真实 STDIO 握手与调用。
- 性能目标：核心搜索和预热 MCP 搜索通过；STDIO 进程冷启动未达到 1 秒目标。
- 数据边界：所有输入均由测试在临时目录中即时生成；没有访问、注册或导入既有记忆。
- 客户端边界：未修改 ChatGPT Desktop 或 ChatGPT 工作区配置，也未把本地 SDK
  验收冒充为 ChatGPT Desktop 端到端验收。

由于冷启动检查未通过，性能脚本按约定返回退出码 2，整体报告中的 `ok` 为 `false`。
这不表示功能测试失败，而是保留一个明确、可复现的未满足目标。

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

结果：17 个测试全部通过，用时 17.478 秒。覆盖的关键不变量包括：

1. 四个独立进程各追加 100 条，事实源最终恰好有 400 条有效 JSONL 事件。
2. 索引写入失败时事件已经 `flush`、`fsync` 并持久化，随后可用
   `doctor --reindex` 精确恢复。
3. 默认项目范围精确覆盖 `AGENTS.md` 与直接的 `context/*.md`，不递归扩展到嵌套目录。
4. 删除或篡改派生 SQLite 不会损失事实源，重建后稳定引用与未变化内容哈希保持一致。
5. 默认 MCP 只注册 `context_get`；写模式只有显式启用后才持久显示 `context_put`。
6. 官方 SDK 客户端真实启动子进程，完成 initialize、`tools/list` 和
   `context_get` search/read 调用。

完整输出保存在本地忽略目录
`.context-hub-test-data/unittest-final.log`，不会提交合成测试产物。

## 10,000 条性能验收

执行命令：

```powershell
.\.venv\Scripts\python.exe scripts\run_acceptance.py `
  --output .context-hub-test-data\acceptance-report-final.json
```

| 检查项 | 实测 | 目标 | 结果 |
| --- | ---: | ---: | --- |
| 核心搜索 p50（200 次） | 2.967 ms | <= 50 ms | 通过 |
| 核心搜索 p95（200 次） | 3.523 ms | <= 150 ms | 通过 |
| 预热 MCP 搜索 p95（40 次） | 6.547 ms | <= 500 ms | 通过 |
| STDIO 启动并完成 `tools/list` | 1535.456 ms | <= 1000 ms | **未通过** |
| `doctor` 精确计数 | 10,000 / 10,000 | 相等 | 通过 |

补充测量：完整重建 10,000 条索引用时 437.325 ms，核心首次搜索 4.662 ms。

使用 Python `-X importtime` 单独诊断时，导入 `context_hub.mcp_stdio` 约需 1.45 秒，
时间主要落在 MCP SDK 2.1.1 的依赖导入链。当前没有通过降低阈值、跳过 initialize 或
改成非官方轻量协议来掩盖该问题；它作为非功能风险保留，后续可在 SDK 升级或官方
客户端进程复用机制明确后复测。

## 未覆盖与下一验收点

- 当前 OpenAI 官方文档不提供 ChatGPT Desktop 直接启动本地 STDIO MCP 的接入方式，
  因此 ChatGPT Desktop 原生端到端验收尚未完成。
- Secure MCP Tunnel 是当前官方的本地或私有 MCP 接入路径，但会创建外部状态并需要
  组织权限、隧道 ID 和运行时密钥；未经用户另行授权未执行。
- 若继续客户端验收，仍只使用合成数据，并需要在真实 ChatGPT 支持面完成
  `tools/list`、search、稳定 ref 分页 read 与 SHA-256 对照。
