# ChatGPT 客户端接入状态

状态日期：2026-09-08。

## 已确认

- Context Hub 的 MCP STDIO 和 Streamable HTTP 服务器均已通过官方 Python MCP SDK 的
  真实握手、`tools/list` 和 `context_get` search/read 测试。
- 2026-09-08，用户已在 ChatGPT Web 完成临时纯合成连接器的创建和真实调用；服务端访问
  日志与用户确认共同证明 ChatGPT 实际调用了该 MCP，而不只是完成配置或扫描。
- `context_get` 与可选的 `context_put` 现在都在 `tools/list` 中声明紧凑的
  `outputSchema`，成功调用同时返回匹配的 `structuredContent` 和兼容的文本 JSON。
- HTTP 响应带 `context_hub.active=true`、实际 transport、请求 ID 和有界来源摘要，
  可据此区分 Context Hub 命中与普通模型回答。
- 首轮测试数据全部即时生成在临时目录，没有注册或导入既有记忆。
- 本机 ChatGPT Desktop 共用 MCP 配置已注册 `context-hub` STDIO，数据根固定到纯合成
  `desktop-e2e`，并显式设置 `CONTEXT_HUB_WRITE_ENABLED=0`。
- 三批各 5 个独立的新 STDIO 进程均完成
  `initialize → tools/list → manifest → search → read`；稳定 ref、正文 SHA-256、项目范围
  和调用来源提示全部一致。三批全链路中位数分别为 644.357、1039.130、861.566 ms，
  合计 15/15 通过。另行受 v1.1 门槛约束的冷启动加 `tools/list` 中位数为 922.855 ms。

## 当前官方产品边界

OpenAI 当前 MCP 配置文档说明：ChatGPT Desktop、Codex CLI 与 IDE 扩展支持 MCP，并在
同一 Codex 主机上共用配置；Desktop 支持本地 STDIO 和 Streamable HTTP。Desktop 的
设置流程为“设置 → MCP 服务器 → 添加服务器”，保存后重启，并可用 `/mcp` 检查状态。
官方说明见 [Model Context Protocol](https://learn.chatgpt.com/zh-Hans/docs/extend/mcp)。

本地 SDK 验收和共用配置证明服务可启动、可发现、可重复调用，但不能替代 Desktop 界面
真正选择并调用工具。本轮已完成 Web 真实调用和 Desktop 侧自动化基线；Desktop 产品界面
仍保留一次重启后的真实调用供用户确认。网页端通过不能推断 Desktop 端到端通过。

## 2026-09-08 Web 验收结果

- ChatGPT Web 已完成连接器创建、工具发现与纯合成 `context_get` 调用，人工验收通过。
- 固定 marker 为 `chatgpt-http-e2e-7f3a91`，项目为 `desktop-e2e`；读取结果包含虚构状态
  “蓝色纸鸢已完成校准”，完整内容 SHA-256 为
  `2ed2db74bec2c00f220bda0ca851ec1a14e9f2f6c06ced970216f9a0a82deac4`。
- 调用返回 `context_hub.active=true`、`status=invoked`、
  `transport=streamable-http`、唯一请求 ID 和精确项目/来源摘要。
- 网页端同时提示建议补充 `outputSchema`。该提示暴露的是工具结果契约缺口，不影响本次
  调用是否成功；现已修复，并由真实 STDIO、真实 HTTP 会话及公网 HTTPS 端点自动复验。
- 临时端点只承载上述合成数据；复验结束后关闭，历史 URL 不再有效。修复后的网页界面
  是否不再显示建议，可在下次新建临时连接器时顺手确认，不阻塞本轮功能验收。

## 临时纯合成验收

1. Context Hub 仅加载本轮即时生成的合成数据，并在 `127.0.0.1` 启动 HTTP MCP。
2. 使用短时 HTTPS 隧道转发到回环端口；不把密钥、隧道凭据或 URL 固化进仓库。
3. 在 ChatGPT Web 的 Apps 开发者模式中添加 HTTPS `/mcp` URL；临时隧道进程停止后
   原 URL 即失效，不能复用历史验收报告中的 URL。
4. 在 ChatGPT Web 选择该应用，先请求 manifest，再搜索固定 marker 并读取 ref。
5. 核对返回结果含 `context_hub.active=true`、`transport=streamable-http`、预期
   `project_ids`、正文和 SHA-256；完成后删除连接并关闭隧道。

若“Scan Tools”失败，先用 `scripts/verify_http_mcp.py` 检查当前 URL；若扫描已经显示
`context_get`，但“Create”仍失败，则优先检查 ChatGPT 套餐、开发者模式和工作区角色。
Business 仅管理员/所有者可启用和部署；Enterprise/Edu 还可能受 RBAC 限制；Pro 目前
仅能在开发者模式接入 read/fetch MCP。Context Hub 的临时验收端点只暴露只读
`context_get`。

除 Desktop 完全重启、执行 `/mcp` 和发送一条固定验收提示外，其余配置、协议、检索、
分页、哈希、来源提示与重复启动均由自动化验收承担。实际执行结果会写入验收报告；
未完成产品侧调用时必须明确标为“待人工确认”，不得根据配置或 SDK 结果推断通过。
本轮 Web 产品侧调用已由用户确认完成；Desktop 自动化基线通过，UI 真实调用仍待人工
确认。通过前不导入真实记忆。
