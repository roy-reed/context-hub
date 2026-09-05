# ChatGPT 客户端接入状态

状态日期：2026-09-05。

## 已确认

- Context Hub 的 MCP STDIO 和 Streamable HTTP 服务器均已通过官方 Python MCP SDK 的
  真实握手、`tools/list` 和 `context_get` search/read 测试。
- HTTP 响应带 `context_hub.active=true`、实际 transport、请求 ID 和有界来源摘要，
  可据此区分 Context Hub 命中与普通模型回答。
- 首轮测试数据全部即时生成在临时目录，没有注册或导入既有记忆。

## 当前官方产品边界

OpenAI 的 ChatGPT 开发者模式与 MCP 连接器说明把自定义 MCP 接入描述为一个可访问的
远程 MCP URL，而不是由 Desktop 直接启动本地 STDIO 子进程。Context Hub 因此保留
STDIO 给本地客户端使用，并新增 Streamable HTTP 供 HTTPS 隧道或受控反向代理转发。

因此，本地 SDK 验收只证明协议和适配层可用。只有在 ChatGPT 产品界面添加 HTTPS URL
后实际触发 `context_get`，并看到预期来源标记，才算 Desktop 端到端通过。

## 临时纯合成验收

1. Context Hub 仅加载本轮即时生成的合成数据，并在 `127.0.0.1` 启动 HTTP MCP。
2. 使用短时 HTTPS 隧道转发到回环端口；不把密钥、隧道凭据或 URL 固化进仓库。
3. 在 ChatGPT 的 Apps/Connectors 开发者模式中添加 HTTPS `/mcp` URL。
4. 在 ChatGPT Desktop 选择该连接，先请求 manifest，再搜索固定 marker 并读取 ref。
5. 核对返回结果含 `context_hub.active=true`、`transport=streamable-http`、预期
   `project_ids`、正文和 SHA-256；完成后删除连接并关闭隧道。

除产品界面中的添加连接、选择工具与确认可见结果外，其余协议、HTTPS、检索、分页、
哈希及关闭清理均由自动化验收承担。实际执行结果会写入验收报告；未完成产品侧调用时
必须明确标为“待人工确认”，不得根据配置截图推断通过。
