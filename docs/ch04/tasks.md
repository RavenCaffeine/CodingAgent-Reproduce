# ch04: Agent Loop Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在 `origin/python` 分支落地，逐项标注真实文件 / 类 / 行号。

## T1: 定义 AgentEvent 事件家族（dataclass union）

- 影响文件: `mewcode/agent.py:55-153`
- 依赖任务: 无
- 完成标准: `StreamText` / `ThinkingText` / `RetryEvent` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `CompactNotification` / `HookEvent` 共 11 个 `@dataclass`，加上 `PermissionResponse(Enum)` 三态（`ALLOW` / `DENY` / `ALLOW_ALWAYS`，agent.py:125）和 `PermissionRequest` dataclass（含 `asyncio.Future`）共 12 个事件类型；`AgentEvent = StreamText | ThinkingText | ...` 类型联合在 agent.py:138-153 定义。

## T2: 实现 Agent 类与构造器

- 影响文件: `mewcode/agent.py:284-327`
- 依赖任务: T1
- 完成标准: `Agent.__init__` 接受 `client` / `registry` / `protocol` / `work_dir=".";` / `max_iterations=50` / `permission_checker=None` / `context_window=200_000` / `instructions_content=""` / `memory_manager=None` / `hook_engine=None`；初始化时拉取 `permission_checker.mode` 同步 `permission_mode`、`ensure_session_dir(work_dir)` 准备会话目录、`CompactCircuitBreaker()` 注入压缩熔断、`agent_id = uuid.uuid4().hex[:12]`；附带 `coordinator_mode` / `team_name` / `_team_manager` 三字段挂在团队 / 协调器场景。

## T3: 实现 run 主循环（async generator）

- 影响文件: `mewcode/agent.py:397-716`
- 依赖任务: T1, T2
- 完成标准: `async def run(self, conversation) -> AsyncIterator[AgentEvent]`；进入前注入 environment context + long-term memory；`while True` 跑迭代；每轮先 `_consume_mailbox`，再 `apply_tool_result_budget` + `auto_compact`（CompactEvent → `yield CompactNotification`）；调 `build_system_prompt` 拼系统提示；Plan Mode 时调 `build_plan_mode_reminder` 注入；调 `client.stream` + `StreamCollector.consume` 把流式事件 `yield` 出去；累计 token 后 `yield UsageEvent`；`stop_reason == "max_tokens"` 走 `MAX_TOKENS_CEILING=64000` / `MAX_OUTPUT_TOKENS_RECOVERIES=3` 升档恢复（agent.py:49-50, 529-559）；无工具调用 → `yield LoopComplete` 退出；连续 3 次 unknown → `yield ErrorEvent` 退出；有工具调用 → 按 `partition_tool_calls` 切批执行，最后 `add_tool_results_message` + `yield TurnComplete`。

## T4: 实现 StreamCollector 与 LLMResponse

- 影响文件: `mewcode/agent.py:158-211`
- 依赖任务: T1
- 完成标准: `StreamCollector.consume(stream)` 为 `async generator`；遇 `TextDelta` 追加 `LLMResponse.text` 并 `yield StreamText`；遇 `ThinkingDelta` `yield ThinkingText`；遇 `ThinkingComplete` 累加 `ThinkingBlock(thinking, signature)`；遇 `ToolCallComplete` 累加 `LLMResponse.tool_calls` 并 `yield ToolUseEvent`；遇 `StreamEnd` 写入 `stop_reason` / `input_tokens` / `output_tokens`。

## T5: 实现 partition_tool_calls 工具批次切分

- 影响文件: `mewcode/agent.py:213-234`
- 依赖任务: T2
- 完成标准: `partition_tool_calls(tool_calls, registry) -> list[ToolBatch]`；逐个调用判断 `tool.is_concurrency_safe and registry.is_enabled(name)`；若为安全且上一批 `concurrent=True` 则归入同批，否则新开 `ToolBatch(concurrent=safe, calls=[tc])`；`test_partition_tool_calls`（`tests/test_agent.py`）覆盖 5 个调用→3 批的切分。

## T6: 实现 StreamingExecutor 并发收集器

- 影响文件: `mewcode/agent.py:247-280`
- 依赖任务: T2
- 完成标准: `StreamingExecutor.submit(coro)` 用 `asyncio.create_task` 起协程并按 `_order` 编号；`collect_results()` 按编号排序后 `asyncio.gather(..., return_exceptions=True)`，遇 `Exception` 包装成 `_ToolExecResult(is_error=True)` 不中断主流程；供 SubAgent / Teams 在流式阶段就启动工具时复用。

## T7: 实现 _execute_batch_parallel 并发批执行

- 影响文件: `mewcode/agent.py:782-786`
- 依赖任务: T5, T6
- 完成标准: `_execute_batch_parallel(calls)` 对每个 `ToolCallComplete` 调 `_execute_single_tool_direct`，再 `asyncio.gather` 并发；返回 `list[_ToolExecResult]`，主循环负责把每个结果做 `_maybe_persist_or_truncate` 后写入 `tool_results`，同时 `yield ToolResultEvent`。

## T8: 实现 _execute_tool 串行批 / HITL 路径

- 影响文件: `mewcode/agent.py:788-867`
- 依赖任务: T2, T6
- 完成标准: `_execute_tool(tc)` 为 `async generator`，依次处理 unknown tool / disabled / `permission_checker.check` 三态：`deny` → 错误结果；`ask` → `yield PermissionRequest(future=loop.create_future())` 等 UI 把 `future.set_result(...)` 回填；`ALLOW_ALWAYS` 时调 `rule_engine.append_local_rule(Rule(tool, pattern=content[:60]+"*", "allow"))` 持久化；`pydantic.ValidationError` 拿 `Parameter validation error` 结果；其他异常拿 `Tool execution error`；产出 `(ToolResult, elapsed, is_unknown)` 三元组。

## T9: 实现 Hook 前后包夹

- 影响文件: `mewcode/agent.py:371-395`、`mewcode/agent.py:603-685`
- 依赖任务: T8
- 完成标准: `_build_hook_context(event, **kwargs)` 拼 `HookContext`；`_infer_file_path(args)` 取 `file_path` 或 `path`；`_drain_hook_events()` 把 `HookEngine.drain_notifications()` 转 `HookEvent` `yield` 出去；主循环在 `session_start` / `turn_start` / `pre_send` / `post_receive` / `pre_tool_use`（可阻断，返回 `Hook rejected: {reason}` 错误结果）/ `post_tool_use` / `turn_end` / `session_end` 共 8 个事件点插入 hook 执行。

## T10: 实现 plan_path 单例与 Plan Mode reminder

- 影响文件: `mewcode/agent.py:329-355`、`mewcode/prompts.py:158-237`
- 依赖任务: 无
- 完成标准: `Agent._get_plan_path` 用 `random.choice(_ADJECTIVES) + "-" + random.choice(_NOUNS) + "-" + datetime.now().strftime("%m%d-%H%M")` 生成 slug，落到 `work_dir/.mewcode/plans/<slug>.md`，首次调用 `mkdir(parents=True, exist_ok=True)` 并缓存到 `_plan_path_cache`；`build_plan_mode_reminder(plan_path, plan_exists, iteration)`（prompts.py:203）在 `iteration==1` 给完整 reminder，按 `_REMINDER_INTERVAL=5` 周期再发完整版，间隔轮次发 sparse reminder；`Agent.set_permission_mode(mode)` 同时更新 `permission_checker.mode`。

## T11: 实现团队任务四工具

- 影响文件: `mewcode/tools/task_create.py`、`task_get.py`、`task_list.py`、`task_update.py`
- 依赖任务: 无
- 完成标准: 四个 Tool 类（`TaskCreateTool` / `TaskGetTool` / `TaskListTool` / `TaskUpdateTool`）皆继承 `Tool`，定义 `name` / `description` / `params_model` / `category` / `is_concurrency_safe=True`；构造函数注入 `team_manager: TeamManager` 与 `team_name`；`execute` 走 `team_manager.get_task_store(team_name)` 拿 `TaskStore` 后调 `create/get/list_tasks/update`；`TaskUpdate` 校验 `VALID_STATUSES = {"pending","in_progress","completed","blocked"}`；`TaskList` 输出按状态 icon `○◐●✕` 渲染。

## T12: 实现 _maybe_persist_or_truncate 工具结果整形

- 影响文件: `mewcode/agent.py:1105-1117`
- 依赖任务: T2
- 完成标准: 工具输出长度超 `SINGLE_RESULT_CHAR_LIMIT` 时调 `persist_tool_result` 落到 session 目录、返回 `make_persisted_preview`；超 `MAX_OUTPUT_CHARS` 时直接截断追加 `… (output truncated)`；其他情况原样返回。

## T13: 接入主流程（Textual TUI）

- 影响文件: `mewcode/app.py:649`（构造 `Agent`）、`mewcode/app.py:850-855`（`set_plan_mode`）、`mewcode/app.py:1085`（`async for event in agent.run`）、`mewcode/app.py:1099-1230`（事件分发）、`mewcode/commands/handlers/plan.py`、`mewcode/commands/handlers/do.py`
- 依赖任务: T1~T12
- 完成标准: 用户进入聊天后 `MewcodeApp` 构造 `Agent` 并装好 `permission_checker` / `memory_manager` / `hook_engine`；`send_user_message` 调 `asyncio.create_task(self._send_message(text))`；`_send_message` 用 `async for event in self.agent.run(self.conversation)` 消费事件，按 `isinstance` 分别渲染 `StreamText` / `ThinkingText` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `HookEvent` / `CompactNotification` / `ErrorEvent` / `PermissionRequest` / `RetryEvent`；`/plan` 命令切 `PermissionMode.PLAN`，`/do` 切 `PermissionMode.DEFAULT`。

## T14: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T13
- 完成标准:
  - `python -m compileall mewcode` 通过（语法 / 导入正确）。
  - `ruff check mewcode tests` 无 error。
  - `pytest tests/test_agent.py -q` 通过：覆盖 `test_single_step_tool_call`、`test_multi_step_autonomous`、`test_stop_end_turn`、`test_stop_max_iterations`、`test_stop_cancel`、`test_stop_consecutive_unknown_tools`、`test_message_splicing`、`test_concurrent_batch_execution`、`test_token_usage_accumulates`、`test_plan_mode`、`test_plan_mode_denied_tool_returns_error`、`test_partition_tool_calls`、`test_system_prompt_normal`、`test_system_prompt_plan`、`test_plan_mode_sparse_reminder`、`test_environment_context` 共 16 个测试用例（tests/test_agent.py）。
  - 在 Textual 界面输入 `hello` 看到 `StreamText` 流式渲染与 `LoopComplete` 终止；输入 `/plan` 看到 plan reminder 注入并禁止写工具。

## 进度

- [ ] T1 AgentEvent 11 dataclass + Enum + 联合类型
- [ ] T2 Agent.__init__
- [ ] T3 Agent.run 主循环
- [ ] T4 StreamCollector
- [ ] T5 partition_tool_calls
- [ ] T6 StreamingExecutor
- [ ] T7 _execute_batch_parallel
- [ ] T8 _execute_tool（HITL / 权限）
- [ ] T9 Hook 包夹
- [ ] T10 plan_path 单例 + build_plan_mode_reminder
- [ ] T11 TaskCreate/Get/List/Update 四工具
- [ ] T12 _maybe_persist_or_truncate
- [ ] T13 Textual TUI 接入
- [ ] T14 端到端验证（compileall + ruff + pytest + 手工 plan 模式）
