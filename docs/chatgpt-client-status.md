# ChatGPT 客户端接入状态

状态日期：2026-09-08。

## 已确认

- Context Hub 的 MCP STDIO 和 Streamable HTTP 服务器均已通过官方 Python MCP SDK 的
  真实握手、`tools/list` 和 `context_get` search/read 测试。
- HTTP 响应带 `context_hub.active=true`、实际 transport、请求 ID 和有界来源摘要，
  可据此区分 Context Hub 命中与普通模型回答。
- 首轮测试数据全部即时生成在临时目录，没有注册或导入既有记忆。

## 当前官方产品边界

OpenAI 当前的 ChatGPT 开发者模式与 MCP 应用说明仅明确保证 ChatGPT Web 上的创建、
测试和使用流程，并要求提供可访问的远程 MCP URL；它不支持由 ChatGPT 直接启动本地
STDIO 子进程。Context Hub 因此保留 STDIO 给本地客户端使用，并新增 Streamable HTTP
供 HTTPS 隧道或受控反向代理转发。官方说明见
[Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt)。

因此，本地 SDK 验收只证明协议和适配层可用。应先在 `https://chatgpt.com` 添加 HTTPS
URL 并实际触发 `context_get`；若还要确认 Desktop，则需再检查该应用是否在 Desktop
可见并成功调用。网页端通过不能推断 Desktop 端到端通过。

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

除产品界面中的添加连接、选择工具与确认可见结果外，其余协议、HTTPS、检索、分页、
哈希及关闭清理均由自动化验收承担。实际执行结果会写入验收报告；未完成产品侧调用时
必须明确标为“待人工确认”，不得根据配置截图推断通过。
