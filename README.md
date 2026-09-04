# Context Hub MVP

本仓库实现 Context Hub v1.1 的本地优先 MVP：以 JSONL/Markdown 为事实源，
SQLite FTS5 为可重建索引，并通过 MCP STDIO 向 ChatGPT Desktop 暴露最小工具面。

首轮开发和验收只使用仓库内测试生成的合成数据，不导入任何既有记忆或个人资料。

## 开发命令

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

