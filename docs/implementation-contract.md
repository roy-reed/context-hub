# Context Hub MVP v1.1 实现契约

## 固定边界

- 第一目标端仍是 ChatGPT。MVP 先交付客户端无关的本地 MCP STDIO 服务器；截至
  2026-09-04，ChatGPT 官方接入说明未提供 Desktop 直接启动本地 STDIO 服务器的
  路径，因此不能把这一产品能力写成已实现或已验收。
- 首轮数据：仅测试过程中即时生成的合成数据；不得扫描、复制或导入既有记忆、
  用户画像、聊天导出、浏览器资料或其他个人文件。
- 技术栈：Python 3.11 标准库、SQLite FTS5、官方 Python MCP SDK。
- 事实源：`memory/events.jsonl` 与明确注册的 Markdown；SQLite 仅为可删除、
  可完全重建的派生索引。
- 默认只读；写入必须由显式启用的写模式和 `context_put` 调用共同触发。
- MCP 只提供 `context_get`，以及在写模式下才注册的 `context_put`。
- 不引入本地模型、向量库、Web 服务、文件监视器、容器或公网入口。

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

验收证据：

```text
python -m unittest discover -s tests -v
python scripts/run_multiproject_acceptance.py
context-hub doctor --data-dir <synthetic-temp-dir>
context-hub reindex --data-dir <synthetic-temp-dir>
官方 Python MCP SDK 的本地 STDIO tools/list + synthetic search/read smoke test
```

ChatGPT 侧真实验收须使用当时官方支持的接入面。当前可选路径是 Secure MCP Tunnel，
它需要外部账号、权限和运行时密钥，属于另行确认的 L2 操作；未经授权不创建隧道、
不修改 ChatGPT 工作区，也不把本地 SDK 结果替代为 ChatGPT Desktop 结果。
