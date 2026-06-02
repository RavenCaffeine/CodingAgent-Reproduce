- 

- ```markdown
  # ch02: 让 AI 开口说话 Tasks
  
  > 任务粒度: 每个任务可在一次会话内完成，可独立交付。
  
  ## T1: 定义 `LLMClient` ABC 与工厂
  
  - 影响文件: `mewcode/client.py`
  - 依赖任务: 无
  - 完成标准: `mewcode/client.py:42-53` 声明 `LLMClient(ABC)`，含抽象 `async def stream(conversation, system, tools) -> AsyncIterator[StreamEvent]` 和非抽象 `set_max_output_tokens`；`mewcode/client.py:296-301` 实现 `create_client(config)` 按 `config.protocol` 分流，未知 protocol `raise ValueError("Unknown protocol: ...")`。
  
  ## T2: 实现流式事件 dataclass union
  
  - 影响文件: `mewcode/tools/base.py`
  - 依赖任务: T1
  - 完成标准: `mewcode/tools/base.py:51-92` 定义 7 个事件 dataclass（`TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd`），最后一行用 `StreamEvent = TextDelta | ThinkingDelta | ... | StreamEnd` 形成 Union 类型，供 `isinstance` 分发。
  
  ## T3: 实现错误分层
  
  - 影响文件: `mewcode/client.py`
  - 依赖任务: T1
  - 完成标准: `mewcode/client.py:24-40` 定义 `LLMError(Exception)`、`AuthenticationError(LLMError)`、`RateLimitError(LLMError)`（含 `retry_after: float | None` 字段与 `__init__` 复写）、`NetworkError(LLMError)`，全部继承自统一基类 `LLMError`，上层只需 `except LLMError`。
  
  ## T4: 实现 `AnthropicClient`
  
  - 影响文件: `mewcode/client.py`
  - 依赖任务: T1, T2, T3
  - 完成标准:
   - `mewcode/client.py:56-63` 实现 `_supports_adaptive_thinking(model)`：扫描 `claude-opus-4-` / `claude-sonnet-4-` 前缀且后续首字符 `>= '6'`；
   - `mewcode/client.py:65-78` 实现 `__init__`：从 `ProviderConfig.resolve_api_key()` 取 key，无 key 时直接抛 `AuthenticationError`；构造 `AsyncAnthropic(api_key, base_url)`；
   - `mewcode/client.py:81-174` 实现 `async def stream`：序列化 `conversation.serialize("anthropic")` → 拼 `kwargs` → 按 `_supports_adaptive_thinking` 分流 thinking → `async with self._client.messages.stream(**kwargs) as stream: async for event in stream` 解析 `content_block_start` / `content_block_delta`（thinking_delta / signature_delta / text_delta / input_json_delta）/ `content_block_stop` / `message_stop`，最后 `await stream.get_final_message()` 拿 usage 与 stop_reason；
   - `mewcode/client.py:176-187` 实现错误分类 `except` 链：`anthropic.AuthenticationError` → `AuthenticationError`；`anthropic.RateLimitError` → `RateLimitError(retry_after=float(retry))`；`anthropic.APIConnectionError` → `NetworkError`；`anthropic.APIStatusError` → `LLMError(API error ({status_code}): ...)`；均 `raise ... from e`。
  
  ## T5: 实现 `OpenAIClient`
  
  - 影响文件: `mewcode/client.py`
  - 依赖任务: T1, T2, T3
  - 完成标准:
   - `mewcode/client.py:190-201` 实现 `__init__`：从 `ProviderConfig.resolve_api_key()` 取 key，无 key 抛 `AuthenticationError`；构造 `AsyncOpenAI(api_key, base_url)`；
   - `mewcode/client.py:205-278` 实现 `async def stream`：序列化 `conversation.serialize("openai")` → 拼 `kwargs` → `await self._client.responses.create(**kwargs)` → `async for event in response_stream` 分发：
   - `response.output_text.delta` → `TextDelta`
   - `response.function_call_arguments.delta` → 首次到达时回写 `tool_name` / `call_id` 并 yield `ToolCallStart`，后续累积 `json_accum` 并 yield `ToolCallDelta`
   - `response.function_call_arguments.done` → `json.loads(json_accum)` 解析后 yield `ToolCallComplete`
   - `response.output_item.added` 当 `item.type == "function_call"` → yield `ToolCallStart`
   - `response.completed` → 从 `event.response.usage` 取 `input_tokens` / `output_tokens` yield `StreamEnd("end_turn", ...)`；
   - `mewcode/client.py:280-293` 实现错误分类 `except` 链：`openai.AuthenticationError` / `RateLimitError` / `APIConnectionError` / `APIStatusError` → 对应 4 类错误，`raise ... from e`。
  
  ## T6: 实现模型短名映射
  
  - 影响文件: `mewcode/tools/agent_tool.py`
  - 依赖任务: T1
  - 完成标准: `mewcode/tools/agent_tool.py:612-637` 实现 `_create_client_for_model(model_alias)`：内联 `model_map = {"haiku": "claude-haiku-4-5-...", "sonnet": "claude-sonnet-4-6-...", "opus": "claude-opus-4-6-..."}`；从父 Agent `self._provider_config` 拷出 `ProviderConfig` 复写 `name` / `model`，调用 `create_client(config)` 返回 `LLMClient` 实例。
  
  ## T7: 实现 `ConversationManager` 与消息 dataclass
  
  - 影响文件: `mewcode/conversation.py`
  - 依赖任务: 无
  - 完成标准:
   - `mewcode/conversation.py:8-34` 定义 `ToolUseBlock` / `ToolResultBlock` / `ThinkingBlock` / `Message` 四个 dataclass，所有字段类型清楚；
   - `mewcode/conversation.py:37-113` 实现 `ConversationManager` dataclass（含 `history`、`env_injected`、`ltm_injected`、`last_input_tokens` 字段）+ 8 个写入方法（`add_user_message` / `add_assistant_message` / `add_system_reminder` / `add_tool_results_message` / `inject_environment` / `inject_long_term_memory` / `replace_history` / `get_messages`）；
   - `mewcode/conversation.py:62-68` 实现 `add_system_reminder` 把 content 包成 `<system-reminder>\n{content}\n</system-reminder>` 作为 user 消息追加；
   - `mewcode/conversation.py:117-189` 实现 `serialize(protocol)` 分发到 `_serialize_anthropic` / `_serialize_openai`：Anthropic 路径处理 thinking_blocks + text + tool_uses 合并到 assistant 消息的 list content，并把连续 user 中带 `<system-reminder>` 的消息合并到上一条；OpenAI 路径把 tool_use 转 `{type: "function_call", name, call_id, arguments}`、tool_result 转 `{type: "function_call_output", call_id, output}`。
  
  ## T8: Mock LLMClient 与 Agent 集成测试
  
  - 影响文件: `tests/test_agent.py`
  - 依赖任务: T4, T5, T7
  - 完成标准:
   - `tests/test_agent.py:36-56` 定义 `MockLLMClient(LLMClient)`，构造时收脚本化 `responses: list[list[StreamEvent]]`，`stream` 方法逐 event yield；
   - `tests/test_agent.py:88-120` `test_single_step_tool_call` 验证 Agent 完整收到 `ToolCallComplete` 并执行；
   - `tests/test_agent.py:292-330` `test_message_splicing` 验证 `serialize("anthropic")` 出 5 条消息（env_context + user + assistant(text+2 tool_use) + user(2 tool_result) + assistant(final)），证明 thinking / tool_use 字段不丢；
   - `tests/test_agent.py:361-397` `test_token_usage_accumulates` 验证 `StreamEnd.input_tokens` / `output_tokens` 被累积到 `agent.total_input_tokens`。
  
  ## T9: 接入主流程
  
  - 影响文件: `mewcode/app.py`、`mewcode/agent.py`、`mewcode/tools/agent_tool.py`、`mewcode/skills/executor.py`
  - 依赖任务: T1-T7
  - 完成标准:
   - `mewcode/app.py:613-617` 在 `_select_provider` 中用 `create_client(provider)` 构造 `self.client`，捕获 `AuthenticationError` 提前提示；
   - `mewcode/app.py:649-659` 把 `client=self.client` 传给 `Agent(...)`；
   - `mewcode/agent.py:503-504` Agent Loop 调用 `self.client.stream(conversation, system=system, tools=tools)`，并交给 `StreamCollector.consume(stream)` 异步消费；
   - `mewcode/agent.py:179-205` `StreamCollector.consume` 消费 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` 七种事件，把 `StreamText` / `ThinkingText` / `ToolUseEvent` 转发到外层 `AgentEvent` 流；
   - `mewcode/agent.py:531-590` 通过 `conversation.add_assistant_message(response.text, tool_uses, thinking_blocks=conv_thinking)` 把 thinking 与 tool 写回历史，保证下一轮能回放 signature；
   - `mewcode/tools/agent_tool.py:485` 在 `_select_llm` 通过 `_create_client_for_model(model_override)` 让 SubAgent 切模型；
   - `mewcode/app.py:1264-1265` TUI 主循环 `try: async for event in self.agent.run(...)` 外层 `except LLMError as e: self._show_error(str(e))` 兜底所有 4 类错误。
  
  ## T10: 端到端验证
  
  - 影响文件: 无（仅运行验证）
  - 依赖任务: T9
  - 完成标准:
   - `python -m compileall mewcode tests` 通过；
   - `pytest tests/test_agent.py -v` 通过：14 个 Agent 集成测试全绿（含 `test_single_step_tool_call` / `test_message_splicing` / `test_token_usage_accumulates`）；
   - `ruff check mewcode/client.py mewcode/conversation.py` 无警告；
   - 在 TUI 中发送任意一句话（`python -m mewcode`），能看到流式文本（`TextDelta`）被逐 token 渲染到对话窗口，证明 `stream()` async generator 与事件渲染端到端打通。
  
  ## 进度
  
  - [x] T1  client.py: LLMClient ABC + create_client 工厂
  - [x] T2  tools/base.py: 7 个事件 dataclass + StreamEvent Union
  - [x] T3  client.py: 4 类错误分层
  - [x] T4  client.py: AnthropicClient（adaptive/fixed thinking + 错误分类）
  - [x] T5  client.py: OpenAIClient（Responses API 五类事件 + 错误分类）
  - [x] T6  tools/agent_tool.py: _create_client_for_model 模型短名映射
  - [x] T7  conversation.py: Message dataclass + ConversationManager + serialize
  - [x] T8  tests/test_agent.py: MockLLMClient + 21 个测试（含 message_splicing / token_usage）
  - [ ] T9  接入主流程（依赖 ch01 的 app.py/agent.py，本轮范围外）
  - [~] T10 端到端验证：compileall / pytest / ruff 全绿；TUI 端到端待 T9
  ```
