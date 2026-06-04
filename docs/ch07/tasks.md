# ch07: MCP Protocol Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章基于 `origin/python` 分支已落地的实现产出，每条任务记录实际文件与行号。

## T1: 定义 `MCPServerConfig` 与 ENV 工具
- 影响文件: `mewcode/config.py:67-78`（dataclass 与 `is_stdio`），`mewcode/config.py:50-64`（`resolve_env_vars` / `build_child_env`）
- 依赖任务: 无
- 完成标准: `MCPServerConfig` 字段含 `name / command / args / url / headers / env`，`is_stdio` 用 `command is not None` 判定；`resolve_env_vars` 把 `${VAR}` 展开成 env value，缺失变量保留占位符；`build_child_env` 仅注入 `PATH` 加白名单 env，不携带宿主机敏感变量。

## T2: 在 `load_config` 中反序列化 `mcp_servers`
- 影响文件: `mewcode/config.py:129-139`（构造 list），`mewcode/validator.py`（校验同时给 command 和 url、两者都缺时抛 `ConfigError`）
- 依赖任务: T1
- 完成标准: YAML 中 `mcp_servers` map（key 为 server name）能正确解析成 `list[MCPServerConfig]`；测试 `tests/test_mcp.py::TestLoadConfigMCP` 全绿，其中包含 stdio、HTTP、both/neither 错误三类。

## T3: 实现单服务器 `MCPClient.connect` 分流
- 影响文件: `mewcode/mcp/client.py:17-65`
- 依赖任务: T1
- 完成标准: `MCPClient.connect`（client.py:29-51）根据 `config.is_stdio` 分别走 `_connect_stdio`（53-65，用 `StdioServerParameters` + `stdio_client`）或 `_connect_http`（67-84，用 `httpx.AsyncClient` + `streamable_http_client`）；连接全部通过 `AsyncExitStack` 管理；连接失败时 `_cleanup_stack` 兜底回滚。

## T4: 实现 `list_tools` / `call_tool` / `close` / `_cleanup_stack`
- 影响文件: `mewcode/mcp/client.py:86-113`
- 依赖任务: T3
- 完成标准: `list_tools`（86-89）调 `self._session.list_tools()` 返回 `list[types.Tool]`；`call_tool`（91-95）透传 `CallToolResult`；`close`（97-100）置 `_alive = False` 并交还 stack；`_cleanup_stack`（102-113）静默吞掉 anyio 的 "cancel scope" `RuntimeError`，其他异常仅打 debug 日志。

## T5: 实现 `MCPToolWrapper` 适配器
- 影响文件: `mewcode/mcp/tool_wrapper.py:57-109`
- 依赖任务: T4
- 完成标准: `MCPToolWrapper.__init__`（58-74）赋值 `self.name = f"mcp_{server_name}_{tool_def.name}"`，`category = "command"`，`should_defer = True`，调 `_build_params_model` 生成 pydantic `BaseModel`；`get_schema`（80-85）直接返回原始 `inputSchema`，不走 pydantic 转换；`execute`（87-109）失败时返回 `ToolResult(output="...", is_error=True)`，并把 `result.isError` 透传。

## T6: 实现 `_build_params_model` 与 `_extract_text`
- 影响文件: `mewcode/mcp/tool_wrapper.py:12-54`
- 依赖任务: T5
- 完成标准: `_build_params_model`（12-26）用 `pydantic.create_model` 动态生成 `<tool_name>Params` 模型，required 字段标 `...`、optional 字段标 `None`；`_json_type_to_python`（29-38）覆盖 string/integer/number/boolean/object/array 六类；`_extract_text`（41-54）把 `TextContent` / `ImageContent` / `EmbeddedResource` 三种 block 类型按规则拼接，无 block 时回填 `(no output)`。

## T7: 实现 `MCPManager` 调度与重连
- 影响文件: `mewcode/mcp/manager.py:13-70`
- 依赖任务: T5, T6
- 完成标准: `load_configs`（18-20）把 `list[MCPServerConfig]` 按 name 灌进 `_configs` dict；`register_all_tools`（22-41）遍历 connect + list_tools + register，单个失败 append 到 `errors` 列表不阻塞；`get_client`（43-61）支持 lazy connect 与 `is_alive=False` 时的重连；`shutdown`（63-70）遍历 `_clients` 调 `close()`，异常仅 debug 记录。

## T8: 暴露 `MCPManager` 出包
- 影响文件: `mewcode/mcp/__init__.py:1-5`
- 依赖任务: T7
- 完成标准: `__init__.py` 通过 `__all__ = ["MCPManager"]` 暴露，调用方写 `from mewcode.mcp import MCPManager` 即可。

## T9: 接入 Textual TUI 启动流程
- 影响文件: `mewcode/app.py:50`（import），`mewcode/app.py:514-525`（构造参数），`mewcode/app.py:537-538`（实例字段），`mewcode/app.py:810-811`（`on_mount` 派任务），`mewcode/app.py:1042-1044`（发消息前 await），`mewcode/app.py:1068-1070`（追加 system reminder），`mewcode/app.py:1496-1532`（`_init_mcp`），`mewcode/app.py:1534-1544`（`_shutdown_mcp`）
- 依赖任务: T8
- 完成标准: TUI 启动时把 `config.mcp_servers` 拷给 `MewCodeApp`，`on_mount` 派 `asyncio.create_task(self._init_mcp())`；`_init_mcp` 实例化 `MCPManager` + `load_configs` + `register_all_tools(self.registry)`，把 server 名与可用工具列表拼成 `_mcp_instructions` 用 `add_system_reminder` 注入；用户发消息时若 task 未完成则 `await self._mcp_init_task`；退出时 `_shutdown_mcp` 取消 task 并调 `manager.shutdown`。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准: `pytest tests/test_mcp.py -v` 全绿；在 `config.yaml` 加入 context7 server（`command: npx, args: [-y, "@upstash/context7-mcp"]`），启动 TUI，提示 LLM 调 `mcp_context7_resolve_library_id`，能看到工具命中并返回结果；TUI 顶部状态条应出现 "Connected to N MCP server(s), M tools registered" 提示。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10（受外部 `npx` / context7 依赖，开发者本机已验证；CI 默认跳过）
