# Context Hub MVP

本仓库实现 Context Hub v1.1 的本地优先 MVP：以 JSONL/Markdown 为事实源，
SQLite FTS5 为可重建索引，并通过 MCP STDIO 与 Streamable HTTP 暴露最小工具面。

首轮开发和验收只使用仓库内测试生成的合成数据，不导入任何既有记忆或个人资料。

当前代码已经用官方 Python MCP SDK 完成本地真实 STDIO、Streamable HTTP 握手、
`tools/list` 和 `context_get` 调用。ChatGPT 使用可访问的 HTTPS MCP URL，而不是直接
启动本地 STDIO 进程；当前官方创建与测试入口以 ChatGPT Web 为准。产品侧状态、临时
纯合成验收入口和人工确认边界记录在
[`docs/chatgpt-client-status.md`](docs/chatgpt-client-status.md)。

## 快速验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
pwsh -NoLogo -NoProfile -File .\scripts\run_runbook_smoke.ps1
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 scripts\run_multiproject_acceptance.py --output .context-hub-test-data\multiproject-report.json
.\.venv\Scripts\python.exe -X utf8 scripts\run_acceptance.py --output .context-hub-test-data\acceptance-report.json
```

PowerShell 冒烟脚本逐条调用手册公开的 CLI，验证初始化、项目注册、清单、检索、稳定
引用分页、显式写入、诊断、删除派生索引后重建，以及备份后恢复到新目录。它只创建
唯一的合成测试目录，结束后删除该目录并保留忽略的 JSON 报告。

多项目验收会在单个临时目录内生成 3 个彼此隔离的项目，验证固定 Top-3 检索集、
项目与类型过滤、分页哈希、源文件变更/删除、历史版本、无损重建，以及本地真实
STDIO 的 `manifest → search → read`。脚本不读取现有 Context Hub 数据或既有记忆。

安装、只读启动、显式写入、备份恢复和客户端验收步骤见 [`RUNBOOK.md`](RUNBOOK.md)。
Pi / DeepSeek 的 1,000,000 token 任务上限、截断、重试与墙钟保护实测见
[`docs/worker-guard-validation.md`](docs/worker-guard-validation.md)。
