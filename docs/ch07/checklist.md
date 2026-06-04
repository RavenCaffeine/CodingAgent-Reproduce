# ch07: MCP Protocol Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。文件路径基于 `origin/python` 分支。

## 1. 实现完整性

- [ ] 数据结构 `MCPServerConfig` 在 `mewcode/config.py:67-78` 实现，字段含 `name / command / args / url / headers / env`，`is_stdio` property 在第 76-78 行（`git show origin/python:mewcode/config.py | grep -n "class MCPServerConfig"` 命中第 68 行）。
- [ ] 数据结构 `MCPClient` 在 `mewcode/mcp/client.py:17-23` 实现，含 `config / name / _session / _stack / _alive` 五个属性（`git show origin/python:mewcode/mcp/client.py | grep -n "class MCPClient"` 命中第 17 行）。
- [ ] 数据结构 `MCPManager` 在 `mewcode/mcp/manager.py:13-16` 实现，含 `_configs / _clients` 两张 dict（`git show origin/python:mewcode/mcp/manager.py | grep -n "class MCPManager"` 命中第 13 行）。
- [ ] 数据结构 `MCPToolWrapper` 在 `mewcode/mcp/tool_wrapper.py:57-74` 实现，继承 `Tool` 基类，赋值 `name / description / category / should_defer / params_model`（`git show origin/python:mewcode/mcp/tool_wrapper.py | grep -n "class MCPToolWrapper"` 命中第 57 行）。
- [ ] 函数 `MCPClient.connect` 在 `mewcode/mcp/client.py:29-51` 实现，按 `config.is_stdio` 分流到 `_connect_stdio` / `_connect_http`，握手通过 `ClientSession.initialize()`，失败回滚 `AsyncExitStack`。
- [ ] 函数 `MCPClient._connect_stdio` 在 `client.py:53-65` 实现，用 `StdioServerParameters` + `mcp.client.stdio.stdio_client`，env 通过 `build_child_env` 白名单。
- [ ] 函数 `MCPClient._connect_http` 在 `client.py:67-84` 实现，用 `httpx.AsyncClient` + `mcp.client.streamable_http.streamable_http_client`，header 通过 `resolve_env_vars` 展开。
- [ ] 函数 `MCPClient.list_tools` 在 `client.py:86-89` 实现，调 `self._session.list_tools()` 返回 `list[types.Tool]`。
- [ ] 函数 `MCPClient.call_tool` 在 `client.py:91-95` 实现，透传 `CallToolResult`。
- [ ] 函数 `MCPClient._cleanup_stack` 在 `client.py:102-113` 实现，对 anyio `RuntimeError("cancel scope")` 静默吞没（这是 SDK shutdown race 的已知行为）。
- [ ] 函数 `MCPManager.load_configs` 在 `manager.py:18-20` 实现，按 `cfg.name` 灌进 `_configs` dict。
- [ ] 函数 `MCPManager.register_all_tools` 在 `manager.py:22-41` 实现，按 server 维度收集 `errors`，单个失败 `logger.warning` 后 append 不阻塞其他 server；返回 `list[str]`。
- [ ] 函数 `MCPManager.get_client` 在 `manager.py:43-61` 实现，支持 lazy connect 与 `is_alive=False` 时重新实例化客户端。
- [ ] 函数 `MCPManager.shutdown` 在 `manager.py:63-70` 实现，遍历调 `client.close()`，异常仅 `logger.debug` 记录，清空 `_clients`。
- [ ] 函数 `_build_params_model` 在 `tool_wrapper.py:12-26` 实现，用 `pydantic.create_model` 动态生成 `<ToolName>Params`，required 标 `...`、optional 标 `None`。
- [ ] 函数 `_extract_text` 在 `tool_wrapper.py:41-54` 实现，处理 `TextContent / ImageContent / EmbeddedResource`，无 block 回填 `(no output)`。
- [ ] 函数 `MCPToolWrapper.execute` 在 `tool_wrapper.py:87-109` 实现，`is_alive=False` 时 lazy reconnect；失败返回 `ToolResult(output="...", is_error=True)`；透传 `result.isError`。
- [ ] 工具名格式为 `mcp_<server>_<tool>`（`tool_wrapper.py:67` `f"mcp_{server_name}_{tool_def.name}"`）。
- [ ] 边界 `MCPServerConfig` 同时给 `command` 和 `url` 时 `load_config` 抛 `ConfigError`，错误信息包含 `cannot have both`（`pytest tests/test_mcp.py::TestLoadConfigMCP::test_both_command_and_url_errors -v`）。
- [ ] 边界 `MCPServerConfig` 两者都不给时抛 `ConfigError`，包含 `must have either`（`pytest tests/test_mcp.py::TestLoadConfigMCP::test_neither_command_nor_url_errors -v`）。
- [ ] 边界 stdio 子进程 env 通过 `build_child_env` 白名单（`tests/test_mcp.py::TestBuildChildEnv::test_excludes_host_vars` 通过，确认宿主机 `ANTHROPIC_API_KEY` 不被泄漏）。
- [ ] 边界 HTTP header 值的 `${VAR}` 展开走 `resolve_env_vars`（`client.py:71-72` 字典推导式）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `git show origin/python:mewcode/app.py | grep -nE "mcp_|MCPManager|_init_mcp"` 至少 12 处命中（实测含 import、字段、`on_mount` 派任务、`_init_mcp` / `_shutdown_mcp`、发消息 await、system reminder 注入）。
- [ ] 调用入口位于 Textual TUI 的 `mewcode/app.py:810-811`：`if self._mcp_server_configs: self._mcp_init_task = asyncio.create_task(self._init_mcp())`。
- [ ] 工具注册中心已更新：`_init_mcp`（`app.py:1496-1532`）调 `await manager.register_all_tools(self.registry)`，把 wrapper 注入 `ToolRegistry`。
- [ ] System reminder 注入：连接成功后构造 `_mcp_instructions`（`app.py:1515-1532`，含 server 名与工具列表），发消息时若未注入则用 `conversation.add_system_reminder` 写入一次（`app.py:1068-1070`）。
- [ ] 配置项 `mcp_servers` 已从 YAML 反序列化到 `AppConfig.mcp_servers`（`mewcode/config.py:129-139`），`__main__.py:52` 把 `config.mcp_servers` 传给 `MewCodeApp`。
- [ ] 用户输入到本模块的路径可一句话描述: Textual TUI 启动 → 读 `config.yaml.mcp_servers` → `MCPManager().load_configs(...) → register_all_tools(self.registry)` → 工具变成 `mcp_<server>_<tool>` → LLM 把它当普通工具调用 → `MCPToolWrapper.execute` 走 `MCPClient.call_tool` → MCP server 返回 `CallToolResult` → `_extract_text` 拼成字符串。
- [ ] 退出时 `_shutdown_mcp`（`app.py:1534-1544`）取消 `_mcp_init_task` 并 await，再调 `manager.shutdown()` 清理所有 client。

## 3. 编译与测试

- [ ] `ruff check mewcode/mcp/` 无报错（章节交付前已执行）。
- [ ] `mypy mewcode/mcp/` 类型检查通过（若项目启用 mypy）。
- [ ] `pytest tests/test_mcp.py -v` 全绿，至少 14 个测试（`TestResolveEnvVars`、`TestBuildChildEnv`、`TestLoadConfigMCP`、`TestMCPToolWrapper`、`TestExtractText`、`TestMCPManagerPartialFailure` 六组）。
- [ ] `pytest tests/test_mcp.py::TestMCPManagerPartialFailure -v` 单跑通过，验证单 server 失败不阻塞其他 server。

## 4. 端到端验证

- [ ] 在 `config.yaml` 添加 context7 server（`command: npx, args: ["-y", "@upstash/context7-mcp"]`），启动 `python -m mewcode`，观察日志出现 `MCP server 'context7' connected` 与 `Registered MCP tool: mcp_context7_resolve_library_id` 类条目。
- [ ] TUI 状态条 / 系统消息出现 `Connected to 1 MCP server(s), N tools registered`（`app.py:1512-1514`）。
- [ ] 在 TUI 中提示 LLM 调 context7 工具（例：`use mcp_context7_resolve_library_id for "next.js"`），模型返回结果而非 `Tool not found`。
- [ ] 留存证据: `tests/test_mcp.py` 包含 `TestMCPManagerPartialFailure::test_single_server_failure_does_not_block_others`，可重复运行。

## 5. 文档

- [ ] `docs/ch07/spec.md` / `tasks.md` / `checklist.md` 三件套齐全且最新。
- [ ] commit 信息标注 `ch07` 与三件套关闭状态（验收阶段产物，待用户审阅后随后续 commit 一并打标）。
