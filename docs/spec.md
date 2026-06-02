- 

- ```markdown
  # ch02: 让 AI 开口说话 Spec
  
  ## 1. 背景
  
  Agent 落地的第一步是让上层（Agent Loop / TUI / SubAgent / Skill / Team）能用同一套接口和 LLM 收发，不必各自面对 SSE 流、Thinking 签名回传、Provider 间消息差异。本章把 LLM 通信、流式响应、Extended Thinking、Token 统计以及两层消息模型封装到 `mewcode/client.py` 与 `mewcode/conversation.py`，是 ch03+ 工具循环与 ch08 Compact 的前置依赖。
  
  ## 2. 目标
  
  交付统一的 `LLMClient` ABC（异步流式接口）和两个内置实现（`AnthropicClient`、`OpenAIClient`），加上 `ConversationManager` 两层消息模型（内部带 thinking / tool use / tool result 的 `Message` dataclass，序列化到具体 Provider 的请求体）。上层（Agent Loop、TUI 装配点、SubAgent、Compact、Skill）拿一个 `LLMClient` 就能跑，不再触碰 SSE 细节。
  
  ## 3. 功能需求
  
  - F1: `LLMClient` 是 `ABC`，暴露唯一 `async def stream(conversation, system, tools) -> AsyncIterator[StreamEvent]` 抽象方法，外加 `set_max_output_tokens(tokens)` 非抽象基类方法。
  - F2: 客户端工厂 `create_client(config: ProviderConfig)` 按 `config.protocol ∈ {anthropic, openai}` 路由到 `AnthropicClient` / `OpenAIClient`，未知 protocol 抛 `ValueError("Unknown protocol: ...")`。
  - F3: 流式事件由 `mewcode/tools/base.py` 集中定义，覆盖五类信号：`TextDelta`、`ThinkingDelta` / `ThinkingComplete`（含签名）、`ToolCallStart` / `ToolCallDelta` / `ToolCallComplete`、`StreamEnd`（含 stop reason 与 input/output tokens）。所有事件以 dataclass + `StreamEvent` Union 类型收口，供 `isinstance` 分发。
  - F4: `AnthropicClient` 基于 `anthropic.AsyncAnthropic` SDK，支持 Extended Thinking 两种模式：`_supports_adaptive_thinking(model)` 命中（claude-opus/sonnet-4- 且 minor ≥ 6）时走 adaptive（`type: enabled` + `budget_tokens: 0`），否则回退到固定 budget（`max_output_tokens - 1`，最小 1024）。模型版本能力判断在客户端内部完成。
  - F5: `OpenAIClient` 基于 OpenAI `responses.create(stream=True)` Responses API（非 Chat Completions），覆盖 `response.output_text.delta`、`response.output_item.added`（function_call）、`response.function_call_arguments.delta/done`、`response.completed` 五类 SDK 事件。
  - F6: 两个客户端的 `stream()` 是 async generator，通过 `async for event in stream` 逐事件 `yield` `StreamEvent` 到调用方；取消由 `asyncio` 协作式 cancel（上层 `agent_task.cancel()` 即可终止）。
  - F7: 错误分类有 4 类：`LLMError`（基类）、`AuthenticationError`、`RateLimitError(retry_after)`、`NetworkError`。各客户端在 `except` 分支把 SDK 异常归类到这 4 类之一，`raise ... from e` 保留异常链；上层只面对统一错误。
  - F8: `Message` dataclass 支持完整字段：`role` / `content` / `thinking_blocks` / `tool_uses` / `tool_results`，每个 block 用独立 dataclass（`ThinkingBlock` / `ToolUseBlock` / `ToolResultBlock`）。
  - F9: `ConversationManager` 提供 `add_user_message` / `add_assistant_message` / `add_tool_results_message` 等写入方法；`get_messages` 返回 list 浅拷贝；`serialize(protocol)` 分发到 `_serialize_anthropic` / `_serialize_openai`，序列化时不丢字段（thinking signature、tool input、tool_result is_error 都要原样回到下一轮请求）。
  - F10: `ConversationManager.add_system_reminder(content)` 把内容包成 `<system-reminder>\n{content}\n</system-reminder>` 作为 user 消息追加；`_serialize_anthropic` 在序列化时把连续 user reminder 合并进上一条 user 消息（避免 user/assistant 不交替）。
  - F11: `ConversationManager.inject_environment(context)` 与 `inject_long_term_memory(instructions, memories)` 提供幂等的 head-insert：用 `env_injected` / `ltm_injected` 标志位避免重复注入，供 ch04 Agent Loop 启动与 ch08 Compact 后重注入。
  - F12: 模型短名映射在 `mewcode/tools/agent_tool.py::_create_client_for_model` 内联实现：`{"haiku", "sonnet", "opus"}` → 具体模型 ID，配合父 Agent 的 `ProviderConfig` 复制出子 client，供 ch13 SubAgent 切模型。
  
  ## 4. 非功能需求
  
  - N1: `stream()` 是 native async generator，事件经 `yield` 直接驱动上层 `async for`，无中间 `asyncio.Queue`，调度成本最小。
  - N2: 上层 cancel（如 TUI ctrl+c）走 `asyncio.CancelledError`，必须在当前事件循环 tick 内退出 `stream()` 协程；SDK 的 `async with messages.stream(...)` 上下文负责连接清理。
  - N3: 序列化层不丢字段：thinking signature / tool input dict / tool_result `is_error` 全部往返保留；assistant 消息有 thinking 或 tool_use 时强制走 list-of-blocks 路径。
  - N4: `ConversationManager` 不加锁——单消费者模型，调用方（Agent Loop）负责串行化追加。
  - N5: `ProviderConfig.get_max_output_tokens()` 在 `thinking=True` 时默认 64000，否则 8192；`set_max_output_tokens(tokens)` 允许 ch04 在 `stop_reason == max_tokens` 时升档到 `MAX_TOKENS_CEILING`。
  
  ## 5. 设计概要
  
  - 核心数据结构:
   - `LLMClient` ABC（含 `stream` + `set_max_output_tokens`）
   - `StreamEvent` Union（7 个事件 dataclass + `StreamEnd`）
   - 4 类错误类型（`LLMError` / `AuthenticationError` / `RateLimitError` / `NetworkError`）
   - `Message` / `ToolUseBlock` / `ToolResultBlock` / `ThinkingBlock` dataclass
   - `ConversationManager` dataclass（私有 history list + `env_injected` / `ltm_injected` 标志）
  - 主流程（每轮 LLM 请求）:
   1. Agent Loop 调 `self.client.stream(conversation, system, tools)` 得到 `AsyncIterator[StreamEvent]`
   2. 客户端 `conversation.serialize(protocol)` 序列化历史为 SDK 入参
   3. `AnthropicClient` 用 `async with self._client.messages.stream(**kwargs) as stream: async for event in stream` 拉流；`OpenAIClient` 用 `await self._client.responses.create(...)` 拿到 `response_stream`，然后 `async for event in response_stream`
   4. 按 SDK 事件类型 `yield` 对应 `StreamEvent` dataclass
   5. 流结束 yield `StreamEnd(stop_reason, input_tokens, output_tokens)`；异常经 `except SDK.XXX` 分支转成 4 类错误后 `raise ... from e`，由上层 `try/except LLMError` 捕获
  - 调用链（模块层级）:
   - TUI 装配 → `create_client(provider)` → 赋给 `MewCodeApp.client` → 传给 `Agent(client=...)`
   - Agent Loop → `client.stream(...)` → `StreamCollector.consume(stream)` → `LLMResponse` → 写回 `ConversationManager`
   - SubAgent（`AgentTool._create_client_for_model`）/ Skill Fork（`SkillExecutor`）复用同一 `LLMClient` 接口
  - 与其他模块的交互:
   - 依赖 `mewcode/config.py`（`ProviderConfig`、`resolve_api_key`、`get_max_output_tokens`）
   - 被 `mewcode/agent.py`、`mewcode/app.py`、`mewcode/tools/agent_tool.py`、`mewcode/skills/executor.py` 直接调用
   - 与 `mewcode/tools/` 解耦：`stream` 只接 `list[dict[str, Any]]` schema，工具注册中心由 `ToolRegistry` 提供
  
  ## 6. Out of Scope
  
  - 多模态输入（image / PDF）请求体构造：`Message.content` 当前仅 `str`，未来章节再扩
  - SDK 静默阻塞的空闲超时兜底：Python 当前依赖 asyncio cancel + SDK 自身超时，不在客户端做 idle watchdog
  - `ContextTooLongError` 与 `context_length_exceeded` 关键字归类：Python 当前在 413 / 400 时只回 `LLMError(status_code, message)`，由上层 Compact 流程兜底
  - OpenAI Responses API 的 reasoning summary / encrypted_content：Python 端暂未实现 reasoning 事件还原，OpenAI 路径无 thinking
  - 自动重试与指数退避：rate limit 的重试在 ch04 Agent Loop 处理，不在 ch02 范围
  - Provider 抽象细分（Bedrock / Vertex / Azure-OpenAI）：当前只支持原生 Anthropic 与原生 OpenAI Responses
  - 模型短名解析的独立模块化：当前内联在 `agent_tool.py::_create_client_for_model`，未来抽出 `model_resolver.py`
  
  ## 7. 完成定义
  
  见 [checklist.md](checklist.md)，所有条目勾上即完成。
  ```
