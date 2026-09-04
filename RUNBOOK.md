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
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
$OutputEncoding = $Utf8NoBom
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = '1'
```

推荐先运行一键手册冒烟。它为每次运行创建唯一的合成目录，完成后删除事实输入，
只在忽略目录保留 JSON 报告。脚本优先使用仓库的 `.venv`，若不存在则使用 PATH 中
已安装的 `context-hub`（例如 GitHub Actions 的干净 Runner）：

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\run_runbook_smoke.ps1
```

若需逐步观察，请在同一终端创建独立的纯合成项目并初始化：

```powershell
$RunId = [Guid]::NewGuid().ToString('N')
$ManualRoot = Join-Path $PWD ".context-hub-test-data\manual-$RunId"
$ContextHubTestData = Join-Path $ManualRoot 'data'
$SyntheticProject = Join-Path $ManualRoot 'project'
New-Item -ItemType Directory -Path (Join-Path $SyntheticProject 'context') -Force | Out-Null
$Padding = (1..80 | ForEach-Object { "synthetic-token-$($_.ToString('D3'))" }) -join ' '
[IO.File]::WriteAllText(
  (Join-Path $SyntheticProject 'AGENTS.md'),
  "# Synthetic Manual Project`n`nmanual-smoke-marker $Padding`n",
  $Utf8NoBom
)
[IO.File]::WriteAllText(
  (Join-Path $SyntheticProject 'context\notes.md'),
  "# Synthetic Notes`n`nOnly generated test data is used here.`n",
  $Utf8NoBom
)

.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData init
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData register-project demo $SyntheticProject
```

重复执行 `init` 是幂等的。普通 `init` 不会关闭此前明确启用的写模式。

## 3. 管理与读取

查看清单、搜索和分页读取：

```powershell
$Manifest = .\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData manifest | ConvertFrom-Json
$Search = .\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData `
  search 'manual-smoke-marker' --project-id demo | ConvertFrom-Json
$Ref = [string]$Search.items[0].ref
$Page = .\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData `
  read $Ref --max-chars 600 | ConvertFrom-Json
while ($Page.truncated) {
  $Page = .\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData `
    read --cursor $Page.next_cursor --max-chars 600 | ConvertFrom-Json
}
```

注册项目只接受项目根目录下明确允许的 `AGENTS.md`、`context/*.md` 或命令行
`--file` 指定的受支持文本文件：

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
  --content 'manual-write-marker' --project-id demo `
  --source-ref 'synthetic:manual-smoke'
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData `
  search 'manual-write-marker' --project-id demo --kind fact
```

要恢复持久只读模式，先断开 MCP 进程，再把数据根目录中 `config.toml` 的
`write_enabled` 改为 `false`，随后运行 `doctor`。不要设置
`CONTEXT_HUB_WRITE_ENABLED`；该环境变量会临时强制显示写工具。

## 5. MCP STDIO 启动

能启动本地 STDIO 服务器的 MCP 客户端需要绝对路径。以下命令根据当前仓库和本轮
合成数据目录生成可复制的 JSON，不依赖客户端展开环境变量：

```powershell
$McpCommand = (Resolve-Path '.\.venv\Scripts\context-hub-mcp.exe').Path
$McpConfig = [ordered]@{
  mcpServers = [ordered]@{
    'context-hub' = [ordered]@{
      command = $McpCommand
      env = [ordered]@{
        CONTEXT_HUB_DATA_DIR = [IO.Path]::GetFullPath($ContextHubTestData)
      }
    }
  }
}
$McpConfig | ConvertTo-Json -Depth 5
```

只读启动的 `tools/list` 必须仅返回 `context_get`。本地官方 SDK 的真实 STDIO
验收由 `tests/test_mcp_contract.py` 和 `scripts/run_multiproject_acceptance.py` 执行。
ChatGPT 产品侧边界见
[`docs/chatgpt-client-status.md`](docs/chatgpt-client-status.md)。

## 6. 诊断、重建与恢复

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData doctor
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData doctor --reindex
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData reindex
$BackupRoot = Join-Path $ManualRoot 'backups'
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData `
  backup --destination $BackupRoot
```

- `doctor` 精确核对 JSONL、注册 Markdown、SQLite 普通表与 FTS 表。
- 索引缺失、损坏或写入返回 `status="persisted", index_state="pending_reindex"` 时，运行
  `doctor --reindex`；JSONL 中已落盘的事件无需重写。
- 备份 ZIP 只包含 `config.toml`、`manifest.json`、`memory/events.jsonl` 和逐文件
  SHA-256 清单，不复制注册项目中的外部 Markdown。
- 恢复时先解压到新的空数据根目录，核对 `backup-manifest.json` 中的哈希，再运行
  `reindex` 和 `doctor`。清单中的外部项目路径必须仍然存在，否则先恢复项目文件。

## 7. 验收卡

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\run_runbook_smoke.ps1
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 scripts\run_multiproject_acceptance.py `
  --output .context-hub-test-data\multiproject-report.json
.\.venv\Scripts\python.exe -X utf8 scripts\run_acceptance.py `
  --output .context-hub-test-data\acceptance-report.json
```

验收时逐项记录：

1. 手册冒烟报告的 `ok` 与 `synthetic_only` 为 `true`，且 `checks` 全部为 `true`。
2. 所有输入都来自测试临时目录，未导入既有记忆。
3. 多项目报告的 `ok` 与 `synthetic_only` 为 `true`，`external_fact_inputs` 为 `0`，
   3 个项目的固定检索集 Top-3 召回率不低于 85%，且 `checks` 全部为 `true`。
4. 单元、并发、故障恢复与真实 SDK STDIO 测试全部通过。
5. 10,000 条合成事件的核心搜索 p50、p95 和预热 MCP p95 达标。
6. 三个独立 STDIO 进程的冷启动样本及其中位数是否达到 1 秒目标；不达标时保留
   全部测量值和根因，不降低门槛。
7. 客户端实测的产品名称、版本、接入方式、`tools/list`、search/read 和哈希结果。
8. 截图或配置存在不能替代真实 MCP 调用；未执行的客户端验收必须标为未验证。
