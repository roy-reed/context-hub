# Context Hub MVP

本仓库实现 Context Hub v1.1 的本地优先 MVP：以 JSONL/Markdown 为事实源，
SQLite FTS5 为可重建索引，并通过 MCP STDIO 暴露最小工具面。

首轮开发和验收只使用仓库内测试生成的合成数据，不导入任何既有记忆或个人资料。

当前代码已经用官方 Python MCP SDK 完成本地真实 STDIO 握手、`tools/list` 和
`context_get` 调用。ChatGPT 当前官方产品文档没有提供 Desktop 直接启动本地 STDIO
服务器的接入方式；这一产品边界与可选的 Secure MCP Tunnel 路径记录在
[`docs/chatgpt-client-status.md`](docs/chatgpt-client-status.md)，不得把本地 SDK 测试
表述为 ChatGPT Desktop 端到端通过。

## 快速验证

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\run_multiproject_acceptance.py --output .context-hub-test-data\multiproject-report.json
.\.venv\Scripts\python.exe scripts\run_acceptance.py --output .context-hub-test-data\acceptance-report.json
```

多项目验收会在单个临时目录内生成 3 个彼此隔离的项目，验证固定 Top-3 检索集、
项目与类型过滤、分页哈希、源文件变更/删除、历史版本、无损重建，以及本地真实
STDIO 的 `manifest → search → read`。脚本不读取现有 Context Hub 数据或既有记忆。

安装、只读启动、显式写入、备份恢复和客户端验收步骤见 [`RUNBOOK.md`](RUNBOOK.md)。
