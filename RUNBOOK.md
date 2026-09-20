# Context Hub v0.2 运行手册

## 1. 边界与前提

- 使用 Python 3.11；项目声明不接受 3.12 及以上版本。
- 默认数据目录是 `%LOCALAPPDATA%\ContextHub`，也可用 `--data-dir` 或
  `CONTEXT_HUB_DATA_DIR` 指向绝对路径。
- 首轮只在临时或 `.context-hub-test-data` 目录使用合成数据。不要注册既有记忆、
  聊天导出、浏览器资料或个人目录。
- JSONL 与明确注册的 Markdown 是事实源；`index/context.sqlite3` 可随时删除重建。
- 默认只读。只有显式启用后，MCP 才会注册 `context_put`。
- 默认分类为 `synthetic`，遥测关闭；真实来源只有经过逐文件预检和精确快照批准后才可
  进入后续人工注册步骤。

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

## 4. 日常 Loop

日常启动或排障时先读取有界状态，再让协调器选择一个确定性动作：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData status --quick
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData sync
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData loop-check
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData loop-check --apply-safe
```

- `sync` 在事实源未变化且距上次同步少于 300 秒时返回 `throttled`；`--force` 跳过节流，
  `--full` 从事实源完整重建派生索引。
- `status --quick` 只返回计数、freshness、备份年龄、导入闸门和 worker 状态，不返回正文、
  查询词或本地绝对路径。
- `loop-check` 每次只选择一个动作，优先顺序为修复索引、同步、审查真实导入、检查失败
  worker、备份、ready。`--apply-safe` 最多执行一次本地重建、同步或备份，不会批准导入、
  调用外部 worker 或开启写模式。
- 每次 MCP `context_get` 前会执行同一有节流同步，因此外部 Markdown 变化不再依赖手工
  `reindex`；响应中的 `freshness` 和 `sync_action` 会说明本次读取看到的状态。
- `state/loop.json` 只保存有界运行元数据；它不是事实源，也不会进入权威备份。

## 5. 显式写模式

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

## 6. ChatGPT Desktop、MCP STDIO 与 Streamable HTTP

ChatGPT Desktop、Codex CLI 与 IDE 扩展在同一 Codex 主机上共用 MCP 配置。Desktop
可直接启动本地 STDIO 服务器，不需要公网 URL 或隧道。先用绝对路径注册只读、纯合成
数据入口；显式写入 `CONTEXT_HUB_WRITE_ENABLED=0`，避免继承父进程中的意外写开关：

```powershell
$McpCommand = (Resolve-Path '.\.venv\Scripts\context-hub-mcp.exe').Path
$DesktopData = [IO.Path]::GetFullPath($ContextHubTestData)
codex mcp add context-hub `
  --env "CONTEXT_HUB_DATA_DIR=$DesktopData" `
  --env 'CONTEXT_HUB_WRITE_ENABLED=0' `
  -- $McpCommand
codex mcp get context-hub --json
```

若 `context-hub` 已存在且需要换数据目录，先记录 `codex mcp get context-hub --json`，再用
`codex mcp remove context-hub` 删除旧条目并重新添加。注册或更新后完全退出并重启
ChatGPT Desktop，在输入框执行 `/mcp`，确认 `context-hub` 已连接且只显示
`context_get`。

只读启动的 `tools/list` 必须仅返回 `context_get`，并为其声明实际结果形状的
`outputSchema`。本地官方 SDK 的真实 STDIO 验收由 `tests/test_mcp_contract.py`、
`scripts/run_multiproject_acceptance.py` 和重复冷启动脚本执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\verify_desktop_stdio.py `
  --command .\.venv\Scripts\context-hub-mcp.exe `
  --data-dir .\.context-hub-test-data\chatgpt-http-e2e\data `
  --query chatgpt-http-e2e-7f3a91 `
  --project-id desktop-e2e `
  --expected-marker chatgpt-http-e2e-7f3a91 `
  --runs 5 `
  --output .context-hub-test-data\desktop-stdio-report.json
```

Desktop 产品侧只保留一次必要人工验收。在重启后的新对话中发送：

> 只使用 context-hub 的 context_get，按 manifest → search → read 顺序，在
> project_id=desktop-e2e 中搜索 chatgpt-http-e2e-7f3a91，读取首个稳定 ref 的完整内容；
> 最后原样列出 context_hub.active、status、server、transport、operation、request_id、
> source_types、project_ids、freshness、sync_action、classification 和 sha256。不要根据已有
> 对话回答。

预期正文含“蓝色纸鸢已完成校准”，`transport=stdio`、`project_ids=["desktop-e2e"]`，
完整内容 SHA-256 为
`2ed2db74bec2c00f220bda0ca851ec1a14e9f2f6c06ced970216f9a0a82deac4`。若没有
`context_hub.active=true` 和唯一 `request_id`，即使答案碰巧正确也不能判定 Context Hub
生效。

ChatGPT Web、跨主机客户端或其他不能启动本地进程的客户端，使用下面的命令在回环地址
启动相同工具面的 Streamable HTTP 适配层；默认拒绝非回环监听，并启用 Host/Origin
校验和 1 MiB 请求体上限：

```powershell
$HttpCommand = (Resolve-Path '.\.venv\Scripts\context-hub-mcp-http.exe').Path
& $HttpCommand --data-dir $ContextHubTestData --host 127.0.0.1 --port 8765 --path /mcp
```

本机也可启用 Bearer 认证。令牌只放在环境变量中，至少 32 个字符；不要把令牌作为命令
参数、写入仓库或验收报告。先在终端 A 输入一个临时令牌并保持服务运行：

```powershell
$env:CONTEXT_HUB_HTTP_BEARER_TOKEN = Read-Host '输入短时随机 Bearer token'
& $HttpCommand --data-dir $ContextHubTestData --host 127.0.0.1 --port 8765 --path /mcp
```

再在终端 B 输入同一个临时令牌并执行验证；验证后停止终端 A 的服务，并在两个终端中
删除该环境变量：

```powershell
$env:CONTEXT_HUB_HTTP_BEARER_TOKEN = Read-Host '再次输入同一短时随机 Bearer token'
.\.venv\Scripts\python.exe -X utf8 scripts\verify_http_mcp.py `
  --url http://127.0.0.1:8765/mcp `
  --query chatgpt-http-e2e-7f3a91 `
  --project-id desktop-e2e `
  --expected-marker chatgpt-http-e2e-7f3a91
Remove-Item Env:CONTEXT_HUB_HTTP_BEARER_TOKEN
```

非回环直连必须同时满足显式公网绑定、Bearer 认证和 TLS：

```powershell
& $HttpCommand --data-dir $ContextHubTestData --host 0.0.0.0 --port 8765 --path /mcp `
  --allow-public-bind --tls-cert C:\path\server.crt --tls-key C:\path\server.key `
  --allowed-host 'mcp.example.com:*' --allowed-origin https://chatgpt.com
```

若 TLS 由同机可信反向代理终止，Context Hub 应继续监听回环地址。只有网络拓扑确实要求
监听非回环地址时，才可用 `--allow-insecure-http` 明确表示“代理后方的明文内网跳”；它不
会关闭 Bearer、Host、Origin 或请求体保护，也不等于允许直接公网明文 HTTP。

ChatGPT Web 接入需要把 `http://127.0.0.1:8765/mcp` 通过受控隧道或反向代理暴露为
可访问的 HTTPS URL，然后在 ChatGPT Web 的 Apps 开发者模式中添加该 URL。Desktop
本机日常使用应优先采用前述 STDIO 配置，避免隧道启动、外部暴露和额外网络延迟。
代理与 Context Hub 在同机时应继续只监听回环地址；若代理保留外部 Host，使用
`--allowed-host` 精确加入该主机名，并以 `--allowed-origin` 精确限定浏览器来源；不得使用
宽泛通配。当前外部入口只允许只读、纯合成数据，完成后关闭
隧道；临时隧道停止后原 URL 即失效，不得复用历史报告中的 URL。完整产品侧步骤和当前
实测状态见
[`docs/chatgpt-client-status.md`](docs/chatgpt-client-status.md)。

每次工具结果都包含 `context_hub` 标记，其中 `active`、`status`、`server`、
`transport`、`operation`、`request_id`、`source_count`、`source_types` 和 `project_ids`
用于判断请求是否确实经过 Context Hub，以及结果来自哪些事实源；`freshness`、
`sync_action` 和 `classification` 用于确认同步与数据分类。标记不暴露正文或本地绝对
路径。成功调用的 `structuredContent` 必须匹配 `tools/list` 中对应的
`outputSchema`；同时保留等价文本 JSON，以兼容尚未消费结构化结果的客户端。

## 7. 诊断、重建与恢复

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData doctor
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData doctor --reindex
.\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData reindex
$BackupRoot = Join-Path $ManualRoot 'backups'
$Backup = .\.venv\Scripts\context-hub.exe --data-dir $ContextHubTestData `
  backup --destination $BackupRoot | ConvertFrom-Json
$RestoreRoot = Join-Path $ManualRoot 'restored'
.\.venv\Scripts\context-hub.exe restore $Backup.backup --destination $RestoreRoot
.\.venv\Scripts\context-hub.exe --data-dir $RestoreRoot doctor
```

- `doctor` 精确核对 JSONL、注册 Markdown、SQLite 普通表与 FTS 表。
- 索引缺失、损坏或写入返回 `status="persisted", index_state="pending_reindex"` 时，运行
  `doctor --reindex`；JSONL 中已落盘的事件无需重写。
- 备份 ZIP 只包含 `config.toml`、`manifest.json`、`memory/events.jsonl` 和逐文件
  SHA-256 清单，不复制注册项目中的外部 Markdown，也不复制派生的 `state/loop.json`。
- `restore` 只接受不存在的新数据根目录；它在同级临时目录中检查成员集合、路径、类型、
  大小、压缩比和逐文件 SHA-256，再自动重建索引并通过 `doctor` 后原子落位。任何校验
  失败都不会创建目标目录。
- 清单中的注册项目只保存路径，不复制项目 Markdown；外部项目路径必须仍然存在，否则
  先恢复项目文件。
- 恢复后会关闭此前的真实导入批准状态，必须针对当前文件内容重新生成并批准计划。

## 8. 验收卡

```powershell
pwsh -NoLogo -NoProfile -File .\scripts\run_runbook_smoke.ps1
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 scripts\run_loop_acceptance.py `
  --output .context-hub-test-data\loop-acceptance-report.json
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
4. 单元、并发、故障恢复与真实 SDK STDIO/Streamable HTTP 测试全部通过；两个传输层的
   `outputSchema` 一致，调用返回匹配的 `structuredContent`。
5. 10,000 条合成事件的核心搜索 p50、p95 和预热 MCP p95 达标。
6. 三个独立 STDIO 进程的冷启动样本及其中位数是否达到 1 秒目标；不达标时保留
   全部测量值和根因，不降低门槛。
7. 备份只含 3 个权威文件；恢复到新目录后 `doctor` 计数、稳定 ref 与哈希保持一致。
8. 客户端实测的产品名称、版本、接入方式、`tools/list`（含 `outputSchema`）、
   search/read、哈希和 `context_hub.transport` 结果。
9. 截图或配置存在不能替代真实 MCP 调用；未执行的客户端验收必须标为未验证。
10. Loop 报告必须证明同步、单步安全动作、两项目评测、真实导入快照、1,000,000 token
    边界、输出截断元数据、权威备份恢复和默认关闭遥测全部通过。
11. 外部 HTTP 验收必须证明未认证请求被拒绝、带认证的官方 MCP 客户端可完成
    `manifest → search → read`，且非回环启动缺少认证或 TLS/可信代理确认时拒绝启动。

## 9. 真实记忆导入闸门

在以下条件全部满足前，不读取、注册或导入真实记忆：

1. 重复 STDIO 验证至少 5 次全部通过，稳定 ref 与完整内容 SHA-256 一致。
2. ChatGPT Desktop 完全重启后的新对话完成一次真实
   `manifest → search → read`，并返回正确的调用状态与来源提示。
3. `/mcp` 与 `tools/list` 都只暴露 `context_get`；配置中
   `CONTEXT_HUB_WRITE_ENABLED=0`，数据根配置中 `write_enabled=false`。
4. 手册冒烟、完整回归、多项目隔离、备份恢复和重建索引检查全部通过。

闸门通过后，先由用户明确授权真实来源根目录、项目 ID、逐文件允许列表和排除项。在任何
注册前生成不写入状态的逐文件计划：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $NewRealDataRoot import-plan `
  <project-id> <authorized-root> --file AGENTS.md --file context\approved.md `
  --classification real --dry-run
```

人工核对计划中的相对路径、大小、`mtime_ns`、`ctime_ns`、SHA-256 和 `plan_hash` 后，
才可对完全相同的根目录与文件快照批准：

```powershell
.\.venv\Scripts\context-hub.exe --data-dir $NewRealDataRoot approve-import `
  <plan-hash> <project-id> <authorized-root> `
  --file AGENTS.md --file context\approved.md --classification real --confirm-approval
```

`import-plan` 和 `approve-import` 都不会读取正文到报告、复制文件或执行
`register-project`。每次批准以及最终注册前都必须重新生成并核对当前计划；根目录、文件
元数据或内容变化会产生不同的 `plan_hash`，旧计划不能通过重新校验。最终注册仍需用户
针对该精确快照单独授权。获得授权后，再在独立的新数据根中
分阶段注册；不要覆盖当前纯合成根。先备份 Context Hub 权威文件，并另行备份被注册的
外部 Markdown（Context Hub 备份不会复制外部项目文件）；随后执行 `reindex`、`doctor`、
项目隔离查询和敏感路径反向测试。只有验收报告通过后，才把 Desktop MCP 条目切换到真实
数据根；保留旧条目参数和备份作为回滚依据。

Pi / DeepSeek 调度器的独立保护验收、真实提供商与本地故障注入边界见
[`docs/worker-guard-validation.md`](docs/worker-guard-validation.md)。
