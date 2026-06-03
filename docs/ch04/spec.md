# ch04: Agent Loop Spec

## 1. 背景

LLM 单次回复无法完成完整软件任务，必须把「调模型 → 拿工具调用 → 跑工具 → 把结果回灌」组成 ReAct 循环反复运行，直到模型不再请求工具。没有这层 Agent Loop，工具系统（ch03）与后续模块（ch05~ch15）都失去挂载点；流式 token、思考块、token 配额、用户中断、Plan Mode、HITL 权限请求都只能停留在工具层，无法上浮到 Textual 终端 UI。本章把这条循环、配套事件流、Plan Mode 状态与 max_tokens 升档串到 `mewcode/agent.py` 一个文件内。

## 2. 目标

对外提供 `mewcode.agent.Agent`：调用者构造好 `LLMClient`、`ToolRegistry`、（可选）`PermissionChecker` / `HookEngine` / `MemoryManager` 后，调一次 `async for event in agent.run(conversation)` 即可拿到 `AgentEvent` 异步流；Textual UI 只负责把事件 fan-out 到屏幕，剩下的工具分发、流式拼接、批次并发、Plan Mode reminder 注入、max_tokens 恢复、压缩通知全部由 Agent 在协程内串好。Plan Mode 通过 `PermissionMode.PLAN` 切换；plan 文件路径由 `Agent._get_plan_path` 进程内单例懒加载；团队任务工具由 `mewcode/tools/task_*.py` 提供并通过 `TeamManager` 注册。

## 3. 功能需求

- F1: `Agent.run(conversation)` 是 `async def ... -> AsyncIterator[AgentEvent]`（`mewcode/agent.py:397`）；调用方用 `async for` 消费事件，循环结束生成器自然终止。
- F2: 每轮迭代先调 `_consume_mailbox` 拉团队消息，再走 `apply_tool_result_budget`（Layer 1 持久化超长结果）与 `auto_compact`（Layer 2 触发压缩），压缩成功时回送 `CompactNotification` 并重注入环境上下文 / 长记忆。
- F3: 通过 `LLMClient.stream(conversation, system, tools)` 拉取 `StreamEvent`，由 `StreamCollector.consume`（`mewcode/agent.py:178`）转成 `StreamText` / `ThinkingText` / `ToolUseEvent`；`ThinkingComplete` 累积进 `LLMResponse.thinking_blocks`；`StreamEnd` 记录 `stop_reason` / `input_tokens` / `output_tokens`。
- F4: 工具调用按 `partition_tool_calls` 切分批次（`mewcode/agent.py:218`）；`is_concurrency_safe=True` 的相邻工具进入同一并发批，剩余工具单独成批；并发批用 `asyncio.gather` 跑，串行批逐个 `_execute_tool` 处理 HITL；本轮结束统一 `add_tool_results_message` 回灌。
- F5: 主循环终止条件：本轮无 `tool_calls` → 追加 assistant 消息并 `yield LoopComplete`；连续 3 次 `consecutive_unknown` → `yield ErrorEvent` 退出；`asyncio.CancelledError` → 协程被取消时自然终止；超过 `max_iterations`（默认 50）→ `yield ErrorEvent`。
- F6: 处理 `stop_reason == "max_tokens"`：首次升档调 `client.set_max_output_tokens(MAX_TOKENS_CEILING)`（64000）并把已生成文本作为 assistant 消息追加，再注入 resume 指令；后续最多 `MAX_OUTPUT_TOKENS_RECOVERIES`（3）次恢复轮；超出仍未完成则继续走主循环逻辑。每次升档 / 恢复都 `yield RetryEvent(reason=...)`。
- F7: 流式异常处理：底层 `LLMClient.stream` 抛错时由调用方协程冒泡（`asyncio.CancelledError` 直接退出）；压缩内部错误 `auto_compact` 返回 `str` 时由主循环 `yield ErrorEvent`；当前实现暂未引入独立的 `ContextTooLongError` / `RateLimitError` 重试分支（与 Go `handleStreamError` 的差异点）。
- F8: 权限交互：`_execute_tool`（`mewcode/agent.py:788`）调 `permission_checker.check`，`deny` → 返回错误结果；`ask` → `yield PermissionRequest`（带 `asyncio.Future`），UI 端 `set_result` 把 `PermissionResponse.ALLOW / DENY / ALLOW_ALWAYS` 回填；`ALLOW_ALWAYS` 时调 `rule_engine.append_local_rule` 写入 `{tool}(content*)` 规则。
- F9: 工具执行包夹 Hooks：执行前走 `hook_engine.run_pre_tool_hooks`（可阻断，返回拒绝即直接当错误结果回灌）；执行后走 `run_hooks("post_tool_use", ctx)`（不阻断）；`_infer_file_path` 从 `args["file_path"]` / `args["path"]` 提取代表性路径供 hook 匹配。
- F10: 工具集动态裁剪：`Agent.coordinator_mode` 字段使 `build_system_prompt` 切到 coordinator 版；`ToolRegistry.is_enabled` 在 `_execute_tool` / `partition_tool_calls` 两处过滤；`registry.get_deferred_tool_names()` 写入 system reminder 让模型按需 `ToolSearch` 加载。
- F11: Plan Mode 文件状态：`Agent._get_plan_path`（`mewcode/agent.py:334`）懒生成单例路径，用 24 词形容词 + 24 词名词 + `MMDD-HHMM` 时间戳拼出可读 slug，落到 `<work_dir>/.mewcode/plans/<slug>.md`；进入 Plan 模式每轮调 `build_plan_mode_reminder(plan_path, plan_exists, iteration)` 注入提醒。
- F12: 团队协作任务工具：`mewcode/tools/task_create.py` / `task_get.py` / `task_list.py` / `task_update.py` 实现 `TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 四个 Tool；持久化交给 `TeamManager.get_task_store()`，支持 `blocks` / `blocked_by` 依赖关系；与 Go 版本的 `internal/todo` 单进程任务不同，Python 版任务以「跨智能体共享任务板」为定位。

## 4. 非功能需求

- N1: 工具并发安全：`Tool.is_concurrency_safe` 字段决定能否进入同一并发批；`partition_tool_calls` 顺序扫描调用并把连续的安全工具聚为一批，写工具与命令工具单独成批，保证串行语义。
- N2: 事件流式产出：`run` 是异步生成器，事件随 `yield` 直接传给消费者，不引入显式队列；UI 端用 `async for` 即可背压式消费，无需手动配 buffer。
- N3: 工具结果回灌前由 `_maybe_persist_or_truncate`（`mewcode/agent.py:1105`）按 `SINGLE_RESULT_CHAR_LIMIT` 决定是否持久化到 session 目录并改成预览，剩余按 `MAX_OUTPUT_CHARS` 截断追加 `… (output truncated)`，防止单工具结果撑爆下一轮上下文。
- N4: 工具参数代表性路径：`_infer_file_path` 只取 `file_path` / `path` 两个 schema 字段（与 Go 的 `file_path → path → pattern → target` 顺序不同，Python 实现更精简，仅用于 hook 匹配）。
- N5: Plan slug 必须可读：`_ADJECTIVES` 24 词 + `_NOUNS` 24 词 + 时间戳，避免纯数字命名，便于人眼区分 `.mewcode/plans/` 下多个历史 plan。

## 5. 设计概要

- 核心数据结构:
  - `Agent`（`mewcode/agent.py:284`）：`client`/`registry`/`protocol`/`work_dir`/`max_iterations`/`permission_checker`/`permission_mode`/`context_window`/`session_dir`/`compact_breaker`/`instructions_content`/`memory_manager`/`hook_engine`/`active_skills`/`coordinator_mode`/`team_name`/`_team_manager`/`_plan_path_cache` 等字段。
  - `AgentEvent` 类型联合（`mewcode/agent.py:138-153`）：`StreamText` / `ThinkingText` / `RetryEvent` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `PermissionRequest` / `CompactNotification` / `HookEvent`，每个都是独立 `@dataclass`。
  - `PermissionResponse(Enum)`：`ALLOW` / `DENY` / `ALLOW_ALWAYS`（`mewcode/agent.py:125`）。
  - `StreamCollector` / `LLMResponse` / `ThinkingBlock`（`mewcode/agent.py:158-211`）：把底层 `StreamEvent` 折叠成一轮完整响应。
  - `ToolBatch` / `partition_tool_calls`（`mewcode/agent.py:213-234`）：把工具调用切成并发批 + 串行批。
  - `StreamingExecutor`（`mewcode/agent.py:247-280`）：保留并发任务编号排序后 gather 收集，目前 `run` 主路径主要走 `_execute_batch_parallel`，`StreamingExecutor` 给 SubAgent / Teams 复用。
- 主流程（一次迭代）:
  1. `iteration += 1`，超过 `max_iterations` 直接 `yield ErrorEvent` 退出；
  2. `_consume_mailbox` 拉团队邮箱消息；
  3. 走 Layer 1 / Layer 2 压缩，压缩后回送 `CompactNotification` 并重注入环境与长记忆；
  4. `plan_mode` 时通过 `build_plan_mode_reminder` 注入 reminder；
  5. 把 hook 拉出的 prompt 段拼到 `build_system_prompt`；
  6. `registry.get_all_schemas(protocol)` 取工具 schema；
  7. `client.stream(...)` 配合 `StreamCollector.consume` 把流式事件转 `AgentEvent`；
  8. 累计 token usage 并 `yield UsageEvent`；
  9. `stop_reason == "max_tokens"` 走升档 + 恢复轮；
  10. 无 `tool_calls` → 追加 assistant 消息、按周期触发记忆抽取、`yield LoopComplete` 退出；
  11. 有 `tool_calls` → 落 assistant 消息、按批次并发 / 串行执行、把 `ToolResultBlock` 收齐回灌、`yield TurnComplete`。
- 调用链:
  - 用户输入 → `MewcodeApp.send_user_message`（`mewcode/app.py:840`）→ `asyncio.create_task(_send_message)` → `agent.run` async for → 各 `isinstance(event, ...)` 分支渲染 Textual widget。
  - `/plan` → `mewcode/commands/handlers/plan.py:handle_plan` → `MewcodeApp.set_plan_mode(True)` → `agent.set_permission_mode(PermissionMode.PLAN)` → 下一轮注入 reminder。
  - `/do` → `mewcode/commands/handlers/do.py:handle_do` → `MewcodeApp.set_plan_mode(False)` → 恢复 `PermissionMode.DEFAULT`。
  - HITL → `_execute_tool` `yield PermissionRequest(future=...)` → UI `_handle_permission_request` 把用户选择 `future.set_result(...)` 回填。
- 与其他模块的交互:
  - 依赖 `mewcode.client`（LLMClient）、`mewcode.conversation`（`ConversationManager` / `ToolUseBlock` / `ToolResultBlock`）、`mewcode.context`（auto_compact / 预算）、`mewcode.permissions`、`mewcode.hooks`、`mewcode.prompts`（plan reminder / system prompt）、`mewcode.memory.auto_memory`、`mewcode.tools`。
  - 被 `mewcode/app.py`（Textual TUI）、`mewcode/agents/fork.py`（SubAgent fork）、`mewcode/teams/inprocess.py`（in-process teammate）调用。

## 6. Out of Scope

- 本章不实现 SubAgent / Fork（属 ch13）；`Agent.run` 只跑一个智能体，多智能体由 `mewcode/agents/fork.py` 单独承担。
- 本章不实现 Worktree 隔离（属 ch14）；Plan 文件直接落 `work_dir/.mewcode/plans`。
- Plan Mode 的 Reentry / Exit Reminder 文本目前仅有 `_PLAN_MODE_FULL_REMINDER` / `_PLAN_MODE_SPARSE_REMINDER` 两种；后续轮次的退出 / 重入提醒文本属未来增强。
- 团队共享任务 `TaskCreate/TaskGet/TaskList/TaskUpdate` 的依赖图渲染（`blocks` / `blocked_by`）不在本章 UI 范围内。
- 除 `max_tokens` 以外的其他 `stop_reason`（`pause_turn` / `refusal`）当前实现未单独分支。
- `ContextTooLongError` / `RateLimitError` 的独立重试路径暂未引入（与 Go 版差异点，留给后续 PR）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
