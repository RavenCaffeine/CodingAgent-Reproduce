- [ ] 

- ```markdown
  # ch02: 让 AI 开口说话 Checklist
  
  > 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。
  
  ## 1. 实现完整性
  
  - [ ] `LLMClient` ABC 在 `mewcode/client.py:42-53` 实现，含 `@abstractmethod async def stream(conversation, system, tools) -> AsyncIterator[StreamEvent]` 和 `set_max_output_tokens(tokens)`（`grep -n "class LLMClient" mewcode/client.py`）。
  - [ ] `create_client` 在 `mewcode/client.py:296-301` 按 `config.protocol ∈ {anthropic, openai}` 分流，未知 protocol `raise ValueError(f"Unknown protocol: {config.protocol}")`（`grep -n "create_client\|Unknown protocol" mewcode/client.py`）。
  - [ ] 7 个流式事件 dataclass 在 `mewcode/tools/base.py:51-90` 齐全（`TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd`），`mewcode/tools/base.py:92` 定义 `StreamEvent = TextDelta | ThinkingDelta | ThinkingComplete | ToolCallStart | ToolCallDelta | ToolCallComplete | StreamEnd` Union（`grep -n "StreamEvent =" mewcode/tools/base.py`）。
  - [ ] 4 类错误 `LLMError` / `AuthenticationError` / `RateLimitError(retry_after)` / `NetworkError` 在 `mewcode/client.py:24-40` 齐全，全部继承 `LLMError`（`grep -n "class.*Error" mewcode/client.py | head -5`）。
  - [ ] `_supports_adaptive_thinking` 在 `mewcode/client.py:56-63` 严格按 `claude-opus-4-` / `claude-sonnet-4-` 前缀且后续首字符 `isdigit() and int(c) >= 6` 判定。
  - [ ] `AnthropicClient.stream` 在 `mewcode/client.py:81-174` 实现：
   - [ ] `async with self._client.messages.stream(**kwargs) as stream` 拉流（`mewcode/client.py:118`）；
   - [ ] thinking adaptive 模式设 `{"type": "enabled", "budget_tokens": 0}`（`mewcode/client.py:103`）；
   - [ ] thinking 回退模式设 `{"type": "enabled", "budget_tokens": max(max_output_tokens - 1, 1024)}`（`mewcode/client.py:105-107`）；
   - [ ] `content_block_start` 分别识别 `thinking` / `tool_use`（`mewcode/client.py:120-133`）；
   - [ ] `content_block_delta` 分别识别 `text_delta` / `thinking_delta` / `signature_delta` / `input_json_delta`（`mewcode/client.py:134-146`）；
   - [ ] `content_block_stop` 时若在 thinking 中则 yield `ThinkingComplete(thinking, signature)`，若在 tool 中则 yield `ToolCallComplete(tool_id, tool_name, arguments)`（`mewcode/client.py:147-164`）；
   - [ ] `await stream.get_final_message()` 取 usage / stop_reason 后 yield `StreamEnd`（`mewcode/client.py:168-173`）。
  - [ ] `AnthropicClient` 错误分类 `except` 链在 `mewcode/client.py:176-187` 覆盖 `AuthenticationError` / `RateLimitError`（取 `e.response.headers["retry-after"]`）/ `APIConnectionError` / `APIStatusError`，全部 `raise ... from e`。
  - [ ] `OpenAIClient.stream` 在 `mewcode/client.py:205-278` 处理 `response.output_text.delta`、`response.function_call_arguments.delta/done`、`response.output_item.added`（function_call）、`response.completed` 五类事件。
  - [ ] `OpenAIClient` 错误分类 `except` 链在 `mewcode/client.py:280-293` 覆盖 4 类错误 + `raise ... from e`。
  - [ ] 模型短名映射在 `mewcode/tools/agent_tool.py:612-637` 内联实现，含 `{"haiku", "sonnet", "opus"}` → 具体模型 ID（`grep -n "model_map\|haiku\|sonnet" mewcode/tools/agent_tool.py`）。
  - [ ] `Message` dataclass 在 `mewcode/conversation.py:28-34` 定义，含 `role` / `content` / `tool_uses` / `tool_results` / `thinking_blocks` 字段。
  - [ ] `ConversationManager` 8 个方法（`add_user_message` / `add_assistant_message` / `add_system_reminder` / `add_tool_results_message` / `inject_environment` / `inject_long_term_memory` / `replace_history` / `get_messages`）在 `mewcode/conversation.py:44-115` 齐全。
  - [ ] `add_system_reminder` 在 `mewcode/conversation.py:62-68` 用 f-string 包裹 `<system-reminder>\n{content}\n</system-reminder>`（`grep -n "system-reminder" mewcode/conversation.py`）。
  - [ ] `_serialize_anthropic` 在 `mewcode/conversation.py:122-165` 合并同角色连续 user reminder 消息以维持 user/assistant 交替（`grep -n "is_reminder\|startswith" mewcode/conversation.py`）。
  - [ ] `_serialize_openai` 在 `mewcode/conversation.py:167-189` 把 `tool_uses` 拆成顶层 `{type: "function_call", name, call_id, arguments}` 项、`tool_results` 拆成 `{type: "function_call_output", call_id, output}` 项。
  
  ## 2. 接入完整性（必查，杜绝死代码）
  
  - [ ] `create_client` 至少 2 个非测试调用方（`grep -rn "create_client" --include="*.py" mewcode/ | grep -v test_` 命中 `mewcode/app.py:616`、`mewcode/tools/agent_tool.py:635`）。
  - [ ] `ConversationManager()` 至少 5 个非测试调用方（`grep -rn "ConversationManager()" --include="*.py" mewcode/ | grep -v test_` 命中 `mewcode/app.py`、`mewcode/agents/fork.py`、`mewcode/skills/executor.py`、Compact 流程等）。
  - [ ] `mewcode/agent.py:503-504` 实际调用 `self.client.stream(conversation, system=system, tools=tools)`，证明 `LLMClient` 接到 Agent Loop（`grep -n "client.stream" mewcode/agent.py`）。
  - [ ] `mewcode/agent.py:181-205` `StreamCollector.consume` 用 `isinstance` 消费 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` 七种事件，无未处理事件类型遗漏（`grep -n "isinstance(event" mewcode/agent.py`）。
  - [ ] `mewcode/agent.py:531-590` 通过 `conversation.add_assistant_message(response.text, tool_uses, thinking_blocks=conv_thinking)` 把 thinking 与 tool 写回历史，保证下一轮能回放 signature。
  - [ ] `_create_client_for_model` 在 `mewcode/tools/agent_tool.py:485` 被 `_select_llm` 装配时使用（`grep -n "_create_client_for_model" mewcode/tools/agent_tool.py`）。
  - [ ] `LLMError` 在 `mewcode/app.py:1264-1265` 的主流式 `try` 块 `except LLMError as e: self._show_error(str(e))` 中被消费，统一兜底（`grep -n "except LLMError" mewcode/app.py`）。
  
  ## 3. 编译与测试
  
  - [ ] `python -m compileall mewcode tests` 通过。
  - [ ] `pytest tests/test_agent.py -v` 通过：14 个 Agent 集成测试全绿（`pytest tests/test_agent.py::test_single_step_tool_call tests/test_agent.py::test_message_splicing tests/test_agent.py::test_token_usage_accumulates -v`）。
  - [ ] `ruff check mewcode/client.py mewcode/conversation.py mewcode/tools/base.py` 无警告。
  - [ ] `mypy mewcode/client.py mewcode/conversation.py` 无 type error（如项目启用 mypy 时执行）。
  
  ## 4. 端到端验证
  
  - [ ] TUI 启动后（`python -m mewcode`）发送 `hello`，对话窗口逐 token 渲染流式回复——证明 `TextDelta` 通道接到 `mewcode/app.py:1100-1118` 的事件渲染。
  - [ ] 模型为 `claude-sonnet-4-6`（或更新）时，`config.yaml` 设 `thinking: true` 后能在对话区看到 thinking 文本流（`ThinkingDelta` → spinner / `_thinking_label` 渲染），证明 adaptive thinking 接通（`grep -n "ThinkingText" mewcode/app.py`）。
  - [ ] 提供故意失败的 API key 后 TUI 显示 `Invalid API key: ...`（`AuthenticationError` 路径走 `mewcode/app.py:617` 与 `:1264-1265`），证明错误分类生效。
  - [ ] 在 `tests/test_agent.py::test_message_splicing` 输出中能看到 `assert len(msgs) == 5` 通过，证明 `serialize("anthropic")` 把 thinking / tool_use / tool_result 字段往返保留（`pytest tests/test_agent.py::test_message_splicing -v`）。
  
  ## 5. 文档
  
  - [ ] spec.md / tasks.md / checklist.md 三件套齐全（`ls /Users/codemelo/mewcode/docs/python/ch02/`）。
  ```
