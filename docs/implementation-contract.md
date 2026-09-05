# Context Hub MVP v1.1 实现契约

## 固定边界

- 第一目标端仍是 ChatGPT。MVP 同时交付客户端无关的本地 MCP STDIO 与 Streamable
  HTTP 适配层；ChatGPT 通过可访问的 HTTPS MCP URL 接入，不能把本地协议测试替代为
  ChatGPT 产品侧真实调用。
- 首轮数据：仅测试过程中即时生成的合成数据；不得扫描、复制或导入既有记忆、
  用户画像、聊天导出、浏览器资料或其他个人文件。
- 技术栈：Python 3.11 标准库、SQLite FTS5、官方 Python MCP SDK。
- 事实源：`memory/events.jsonl` 与明确注册的 Markdown；SQLite 仅为可删除、
  可完全重建的派生索引。
- 默认只读；写入必须由显式启用的写模式和 `context_put` 调用共同触发。
- MCP 只提供 `context_get`，以及在写模式下才注册的 `context_put`。
- 不引入本地模型、向量库、文件监视器或容器。HTTP 默认只监听回环地址；公网 HTTPS
  只允许作为纯合成、短时且可关闭的客户端验收入口，导入真实记忆前必须使用认证入口。

## 最小评测契约

目标案例：

1. Unicode、emoji、引号、反斜杠、空行和 CRLF 内容可逐字分页读回，SHA-256 一致。
2. 中文三字以上查询走 FTS5 trigram；一至二字查询走有界 LIKE 回退。
3. `supersede` 与 `tombstone` 默认隐藏旧版本，历史模式仍可审计。
4. 删除 SQLite 后重建，事件 ID、内容和哈希保持一致。
5. 四个进程各写入 100 条，最终恰好 400 条且 JSONL 无损坏。
6. 注册项目只索引 `AGENTS.md` 与 `context/*.md`；路径穿越、越界符号链接和
   Windows 大小写变体不能绕过允许列表。
7. 只读 MCP 不注册写工具；写入失败不损坏此前的事实源。
8. 稳定引用和游标可继续读取同一版本；派生索引故障返回“已持久化、待重建”。
9. 三个纯合成项目使用同名章节和各自唯一标记，固定检索集 Top-3 召回率至少 85%，
   项目过滤与类型过滤不得串扰。
10. 本地真实 STDIO 必须在只读工具面完成 `manifest → 项目过滤 search → read`，
    并逐字核对内容、来源与 SHA-256。
11. 本地真实 Streamable HTTP 必须完成 initialize、`tools/list`、search/read，并验证
    Host 防护；每个响应包含 transport、request ID 和有界来源摘要。
12. 备份只含 3 个权威源文件；恢复必须先校验成员、大小、压缩比和 SHA-256，再在新
    目录重建索引，校验失败不得留下目标目录或不完整事实源。

既有正确案例：

- 空仓初始化与重复初始化幂等。
- 无命中搜索返回稳定空结果。
- 普通非历史请求不读取历史版本。
- 适配器失败不改变 JSONL/Markdown 格式。

边界不变量：

- 测试必须使用临时目录；不得把 `%LOCALAPPDATA%\\ContextHub` 或仓库外文件作为
  测试输入。
- JSONL 追加在独占跨进程锁内完成，并在索引事务前 `flush` 与 `fsync`。
- 任何索引均可由事实源独立重建。
- `context_get` 默认响应保持紧凑；正文必须通过稳定引用分页读取。
- 状态与来源提示只公开类型、数量和项目 ID，不新增绝对路径、正文或凭据泄露面。

验收证据：

```text
pwsh -NoLogo -NoProfile -File scripts/run_runbook_smoke.ps1
python -X utf8 -m unittest discover -s tests -v
python -X utf8 scripts/run_multiproject_acceptance.py
python -X utf8 scripts/run_acceptance.py
context-hub --data-dir <synthetic-temp-dir> doctor
context-hub --data-dir <synthetic-temp-dir> reindex
官方 Python MCP SDK 的本地 STDIO tools/list + synthetic search/read smoke test
官方 Python MCP SDK 的本地 Streamable HTTP initialize + tools/list + search/read smoke test
context-hub restore <backup.zip> --destination <new-synthetic-data-dir>
```

用户已授权为纯合成数据创建临时 HTTPS MCP 入口并接入 ChatGPT Desktop。代理、隧道、
产品设置和真实调用分别留存证据；运行时密钥不得写入仓库、日志或报告。只有 ChatGPT
产品界面实际调用并返回预期 `context_hub` 标记，才能记为 Desktop 端到端通过。
