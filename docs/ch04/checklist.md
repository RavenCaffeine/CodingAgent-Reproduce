# ch04: Agent Loop Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] 类 `Agent` 在 `mewcode/agent.py:284`，字段含 `client` / `registry` / `protocol` / `work_dir` / `max_iterations` / `permission_checker` / `permission_mode` / `context_window` / `session_dir` / `compact_breaker` / `instructions_content` / `memory_manager` / `hook_engine` / `coordinator_mode` / `team_name` / `_plan_path_cache`（`grep -n "class Agent:" mewcode/agent.py`）
- [ ] 12 个 AgentEvent 类型 + `PermissionResponse(Enum)` 在 `mewcode/agent.py:55-153`：`StreamText` / `ThinkingText` / `RetryEvent` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `CompactNotification` / `HookEvent` / `PermissionRequest`（`grep -nE "^@dataclass|^class [A-Z]" mewcode/agent.py` 至少返回 12 条）
- [ ] 方法 `Agent.run` 在 `mewcode/agent.py:397` 实现，签名 `async def run(self, conversation) -> AsyncIterator[AgentEvent]`（`grep -n "async def run" mewcode/agent.py`）
- [ ] 常量 `MAX_TOKENS_CEILING=64000` 与 `MAX_OUTPUT_TOKENS_RECOVERIES=3` 在 `mewcode/agent.py:49-50`，`MEMORY_EXTRACTION_INTERVAL=5` 在 agent.py:48（`grep -nE "MAX_TOKENS_CEILING|MAX_OUTPUT_TOKENS_RECOVERIES|MEMORY_EXTRACTION_INTERVAL" mewcode/agent.py`）
- [ ] `StreamCollector.consume` 在 `mewcode/agent.py:178`，处理 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallComplete` / `StreamEnd` 五类事件（`grep -n "isinstance(event," mewcode/agent.py | head`）
- [ ] `partition_tool_calls` 在 `mewcode/agent.py:218`，`ToolBatch` 在 agent.py:213，安全调用合并到同一并发批的逻辑实现完整
- [ ] `StreamingExecutor.submit / collect_results` 在 `mewcode/agent.py:247-280`，使用 `asyncio.create_task` + `asyncio.gather(..., return_exceptions=True)`
- [ ] `_execute_tool` 在 `mewcode/agent.py:788`，处理 unknown tool / disabled / permission deny / permission ask（`PermissionRequest` 带 `asyncio.Future`）/ `ALLOW_ALWAYS` 写规则 5 个分支
- [ ] `_execute_batch_parallel` 在 `mewcode/agent.py:782`，`_execute_single_tool_direct` 在 agent.py:742
- [ ] `_maybe_persist_or_truncate` 在 `mewcode/agent.py:1105`，按 `SINGLE_RESULT_CHAR_LIMIT` / `MAX_OUTPUT_CHARS` 分支
- [ ] `Agent._get_plan_path` 在 `mewcode/agent.py:334`，使用 `_ADJECTIVES`(24) + `_NOUNS`(24) + `MMDD-HHMM` 拼 slug，`_plan_path_cache` 单例
- [ ] `build_plan_mode_reminder` 在 `mewcode/prompts.py:203`，`_REMINDER_INTERVAL=5`，`iteration==1` 给完整 reminder（`grep -n "_REMINDER_INTERVAL" mewcode/prompts.py`）
- [ ] 任务模型与四工具：`TaskCreateTool` / `TaskGetTool` / `TaskListTool` / `TaskUpdateTool` 在 `mewcode/tools/task_create.py`、`task_get.py`、`task_list.py`、`task_update.py`，皆继承 `Tool` 且 `is_concurrency_safe = True`
- [ ] 工具结果回灌：`_infer_file_path` 在 `mewcode/agent.py:381` 按 `file_path → path` 顺序查找

## 2. 接入完整性（杜绝死代码）

- [ ] `grep -n "Agent(" mewcode/app.py` 显示 `mewcode/app.py:649` 构造 Agent 时传入 `client` / `registry` / `protocol` / `work_dir` / `permission_checker` / `context_window` / `instructions_content` / `memory_manager` / `hook_engine`
- [ ] `grep -n "self.agent.run" mewcode/app.py` 至少 1 处（`mewcode/app.py:1085` 的 `async for event in self.agent.run(self.conversation)`）
- [ ] `grep -rn "build_plan_mode_reminder" mewcode/` 至少 2 处调用方：`mewcode/agent.py:475` 与 `tests/test_agent.py`
- [ ] `grep -rn "set_permission_mode\|set_plan_mode" mewcode/` 调用链：`mewcode/commands/handlers/plan.py` → `MewcodeApp.set_plan_mode`（`mewcode/app.py:850`）→ `agent.set_permission_mode(PermissionMode.PLAN)`（`mewcode/agent.py:352`）
- [ ] `grep -rn "TaskCreateTool\|TaskGetTool\|TaskListTool\|TaskUpdateTool" mewcode/` 四个工具在团队注册路径上被引用（团队场景由 `TeamManager` 注册到 Registry）
- [ ] `grep -n "permission_checker" mewcode/app.py` 在 TUI 构造 Agent 时使用（`mewcode/app.py:654`）
- [ ] `Agent.coordinator_mode` 在 TUI 协调器路径上设值，`build_system_prompt` 据此切到 coordinator 系统提示
- [ ] `Agent.hook_engine` 在 `mewcode/app.py:658` 注入 `HookEngine`，主循环 8 个 hook 事件点（session_start / turn_start / pre_send / post_receive / pre_tool_use / post_tool_use / turn_end / session_end）皆有触发
- [ ] `_handle_permission_request` 在 `mewcode/app.py` 监听 `PermissionRequest` 事件，把用户选择 `future.set_result(PermissionResponse.X)` 回填
- [ ] `RetryEvent` 在 `mewcode/app.py:1119` 渲染为 `↻ Retrying: ...` 系统消息

## 3. 编译与测试

- [ ] `python -m compileall mewcode` 通过，无语法 / 导入错误
- [ ] `ruff check mewcode tests` 无 error
- [ ] `pytest tests/test_agent.py -q` 16 个测试用例全部通过：
  - `test_single_step_tool_call`、`test_multi_step_autonomous`、`test_stop_end_turn`
  - `test_stop_max_iterations`、`test_stop_cancel`、`test_stop_consecutive_unknown_tools`
  - `test_message_splicing`、`test_concurrent_batch_execution`、`test_token_usage_accumulates`
  - `test_plan_mode`、`test_plan_mode_denied_tool_returns_error`
  - `test_partition_tool_calls`
  - `test_system_prompt_normal`、`test_system_prompt_plan`、`test_plan_mode_sparse_reminder`、`test_environment_context`

## 4. 端到端验证

- [ ] Textual 入口：用户在输入框敲普通消息后看到 `StreamText` 渲染、最终 `LoopComplete` 终止 —— 调用链 `MewcodeApp.send_user_message → asyncio.create_task(_send_message) → async for event in self.agent.run(self.conversation) → isinstance 分支`（`mewcode/app.py:840 → :1085 → :1099-1230`）
- [ ] Plan Mode：输入 `/plan` 走 `handle_plan` → `set_plan_mode(True)` → `agent.set_permission_mode(PermissionMode.PLAN)`，下一轮看到 plan reminder 注入；输入 `/do` 走 `handle_do` → 恢复 `PermissionMode.DEFAULT`（`mewcode/commands/handlers/plan.py` / `do.py`）
- [ ] HITL 权限：`PermissionRequest` 事件触发时 Textual 渲染权限对话框（`mewcode/permission_dialog.py`），用户选「允许 / 拒绝 / 允许始终」对应 `PermissionResponse.ALLOW` / `DENY` / `ALLOW_ALWAYS`；选 `ALLOW_ALWAYS` 时调 `rule_engine.append_local_rule` 持久化（`mewcode/agent.py:846-851`）
- [ ] max_tokens 升档：模拟 `stop_reason="max_tokens"` 看到 `RetryEvent(reason="max_tokens escalation")` 与 `client.set_max_output_tokens(64000)`；连续 3 次后停止恢复进入下一轮主流程（`mewcode/agent.py:529-559`）
- [ ] 留存证据：验收阶段无截图；如需补，可在 Textual 中输入 `hi` 拍照保存 stream 渲染

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/python/ch04/`）
- [ ] commit 信息标注 `ch04` 与三件套关闭状态（待统一打包提交）
