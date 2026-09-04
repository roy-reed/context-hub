# Context Hub MVP 运行手册

## 1. 边界与前提

- 使用 Python 3.11；项目声明不接受 3.12 及以上版本。
- 默认数据目录是 `%LOCALAPPDATA%\ContextHub`，也可用 `--data-dir` 或
  `CONTEXT_HUB_DATA_DIR` 指向绝对路径。
- 首轮只在临时或 `.context-hub-test-data` 目录使用合成数据。不要注册既有记忆、
  聊天导出、浏览器资料或个人目录。
- JSONL 与明确注册的 Markdown 是事实源；`index/context.sqlite3` 可随时删除重建。
- 默认只读。只有显式启用后，MCP 才会注册 `context_put`。

## 2. 安装与合成数据初始化

在 PowerShell 7 中从仓库根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps

$ContextHubTestData = Join-Path $PWD '.context-hub-test-data\manual'
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData init
```

重复执行 `init` 是幂等的。普通 `init` 不会关闭此前明确启用的写模式。

## 3. 管理与读取

查看清单、搜索和分页读取：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData manifest
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData search '合成检索词'
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData read '<稳定 ref>' --max-chars 600
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData read --cursor '<上一页 next_cursor>' --max-chars 600
```

注册项目只接受项目根目录下明确允许的 `AGENTS.md`、`context/*.md` 或命令行
`--file` 指定的受支持文本文件：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData register-project demo C:\absolute\synthetic-project
```

越界路径、越界符号链接、敏感证书后缀和常见浏览器数据目录会被拒绝。

## 4. 显式写模式

持久启用写工具是安全边界变更；仅在用户明确要求后执行：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData init --enable-writes
```

CLI 写入还要求 `--confirm-write`，并必须给出可追溯的 `--source-ref`：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData put `
  --confirm-write --action append --kind fact `
  --content '合成写入样本' --source-ref 'synthetic:manual-smoke'
```

要恢复持久只读模式，先断开 MCP 进程，再把数据根目录中 `config.toml` 的
`write_enabled` 改为 `false`，随后运行 `doctor`。不要设置
`CONTEXT_HUB_WRITE_ENABLED`；该环境变量会临时强制显示写工具。

## 5. MCP STDIO 启动

能启动本地 STDIO 服务器的 MCP 客户端可使用以下等价配置；占位符必须替换为
绝对路径，不能依赖客户端替你展开 `%LOCALAPPDATA%`：

```json
{
  "mcpServers": {
    "context-hub": {
      "command": "F:\\gptspace\\context-hub\\.venv\\Scripts\\context-hub-mcp.exe",
      "env": {
        "CONTEXT_HUB_DATA_DIR": "C:\\absolute\\synthetic-data-root"
      }
    }
  }
}
```

只读启动的 `tools/list` 必须仅返回 `context_get`。本地官方 SDK 的真实 STDIO
验收由 `tests/test_mcp_contract.py` 执行。ChatGPT 产品侧边界见
[`docs/chatgpt-client-status.md`](docs/chatgpt-client-status.md)。

## 6. 诊断、重建与恢复

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData doctor
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData doctor --reindex
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData reindex
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData backup
```

- `doctor` 精确核对 JSONL、注册 Markdown、SQLite 普通表与 FTS 表。
- 索引缺失、损坏或写入返回 `persisted=true, index_status=pending` 时，运行
  `doctor --reindex`；JSONL 中已落盘的事件无需重写。
- 备份 ZIP 只包含 `config.toml`、`manifest.json`、`memory/events.jsonl` 和逐文件
  SHA-256 清单，不复制注册项目中的外部 Markdown。
- 恢复时先解压到新的空数据根目录，核对 `backup-manifest.json` 中的哈希，再运行
  `reindex` 和 `doctor`。清单中的外部项目路径必须仍然存在，否则先恢复项目文件。

## 7. 验收卡

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\run_acceptance.py `
  --output .context-hub-test-data\acceptance-report.json
```

验收时逐项记录：

1. 所有输入都来自测试临时目录，未导入既有记忆。
2. 单元、并发、故障恢复与真实 SDK STDIO 测试全部通过。
3. 10,000 条合成事件的核心搜索 p50、p95 和预热 MCP p95 达标。
4. STDIO 冷启动是否达到 1 秒目标；不达标时保留测量值和根因，不降低门槛。
5. 客户端实测的产品名称、版本、接入方式、`tools/list`、search/read 和哈希结果。
6. 截图或配置存在不能替代真实 MCP 调用；未执行的客户端验收必须标为未验证。
