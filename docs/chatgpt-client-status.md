# ChatGPT 客户端接入状态

状态日期：2026-09-04。

## 已确认

- Context Hub 的 MCP STDIO 服务器已通过官方 Python MCP SDK 的真实子进程握手、
  `tools/list` 和 `context_get` search/read 测试。
- 首轮测试数据全部即时生成在临时目录，没有注册或导入既有记忆。
- 未修改 ChatGPT Desktop、ChatGPT 工作区或任何全局 MCP 配置。

## 当前官方产品边界

OpenAI 的[ChatGPT 开发者模式与完整 MCP 连接器说明](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)
把 ChatGPT 的自定义 MCP 接入描述为远程服务器连接，并明确指出本地 MCP 服务器
不直接受支持。该页面描述的是 ChatGPT Web 的开发者模式与 Apps 管理面，而不是
Desktop 启动本地 STDIO 子进程的配置格式。

OpenAI 的[Secure MCP Tunnels 指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
提供了让 ChatGPT 访问本地或私有 MCP 的官方路径：隧道客户端可以连接 STDIO 或
HTTP 服务器，只建立出站 HTTPS 连接，不要求开放入站端口或公开服务器。

因此，当前不能诚实声称“ChatGPT Desktop 直接本地 STDIO 端到端通过”。本地 SDK
验收证明的是 Context Hub 服务器协议可用，不是 ChatGPT 产品侧能力。

## 若继续接入 ChatGPT

Secure MCP Tunnel 会引入 MVP 原固定边界以外的外部状态，并需要 Platform 组织权限、
隧道 ID、运行时 API key，以及 ChatGPT 工作区的开发者模式权限。创建隧道、保存
密钥、修改 ChatGPT Apps 或工作区均须用户另行明确授权；密钥不得写入仓库、日志或
交付文档。

获得授权后，验收仍必须使用纯合成数据，并至少完成真实 `tools/list`、search、稳定
ref 分页 read 和 SHA-256 对照。若官方 Desktop 后续增加本地 STDIO 支持，再按届时
官方文档补做 Desktop 原生端到端测试。
