# ch08: 上下文管理 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。每条任务记录实际落地的文件与行号。

## T1: 常量、tag 与 session 助手

- 影响文件: `mewcode/context/manager.py:14-30, 132-145`
- 依赖任务: 无
- 完成标准: `SINGLE_RESULT_CHAR_LIMIT / AGGREGATE_CHAR_LIMIT / PREVIEW_CHARS / KEEP_RECENT_TURNS / OLD_RESULT_SNIP_CHARS / SNIPPED_TAG / SUMMARY_OUTPUT_RESERVE / AUTO_COMPACT_SAFETY_MARGIN / MANUAL_COMPACT_SAFETY_MARGIN / PERSISTED_TAG / SESSION_SUBDIR` 全部定义；`ensure_session_dir(work_dir)` / `cleanup_tool_results(session_dir)` 实现。

## T2: `CompactEvent` / `ContentReplacementState` / `ContentReplacementRecord` dataclass

- 影响文件: `mewcode/context/manager.py:37-58`
- 依赖任务: T1
- 完成标准: `CompactEvent(before_tokens)` 在 `manager.py:37-38` 定义。`ContentReplacementState` 含 `seen_ids: set[str]` + `replacements: dict[str, str]` 两个 field（都用 `default_factory`），`manager.py:46-49` 定义。`ContentReplacementRecord` 含 `tool_use_id` / `replacement` / `kind="tool-result"`，`manager.py:52-56` 定义。

## T3: `create_replacement_state` / `clone_replacement_state`

- 影响文件: `mewcode/context/manager.py:59-68`
- 依赖任务: T2
- 完成标准: `create_replacement_state()` 返回空容器；`clone_replacement_state(src)` 用 `set(src.seen_ids)` 与 `dict(src.replacements)` 浅拷贝，源与拷贝彼此独立；`test_clone_independent` 通过。

## T4: Transcript JSONL `append_replacement_records` / `load_replacement_records`

- 影响文件: `mewcode/context/manager.py:70-104`
- 依赖任务: T2
- 完成标准: `REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"` 在 `manager.py:70` 定义。`append_replacement_records(session_dir, records)`：空切片直接 return；用 `open("a", encoding="utf-8")` 追加，每行一个 `{"kind": ..., "tool_use_id": ..., "replacement": ...}` 对象；`load_replacement_records(session_dir)`：缺文件返回空列表；逐行 `json.loads`。`test_append_and_load_records_roundtrip` 通过。

## T5: `reconstruct_replacement_state`

- 影响文件: `mewcode/context/manager.py:107-127`
- 依赖任务: T2, T4
- 完成标准: 先 seed `seen_ids` = `{ tr.tool_use_id | for tr in m.tool_results, for m in messages }`；按 `r.kind == "tool-result"` 过滤 records 并命中 candidate 才写入 `replacements`；可选 `inherited_replacements` 在 candidate ∩ 未被 records 覆盖时补全；`test_reconstruct_from_records / test_reconstruct_with_inherited_parent` 通过。

## T6: `persist_tool_result` / `make_persisted_preview`

- 影响文件: `mewcode/context/manager.py:148-170`
- 依赖任务: T1
- 完成标准: `persist_tool_result` 用 `os.open(O_WRONLY | O_CREAT | O_EXCL)` 写到 `<session_dir>/<tool_use_id>.txt`，`FileExistsError` 静默吞掉（幂等）。`make_persisted_preview` 输出 `<persisted-output>\n输出太大（XKB），完整内容已保存到：\n<file_path>\n\n预览（前 2KB）：\n<content[:PREVIEW_CHARS]>\n</persisted-output>`。`TestPersistToolResult` / `TestMakePersistedPreview` 通过。

## T7: 辅助 `_count_turns` / `_copy_message_with_results` / `_snip_stale_messages`

- 影响文件: `mewcode/context/manager.py:173-238`
- 依赖任务: T1, T6
- 完成标准:
  - `_count_turns(messages)` 数 `assistant && not tool_uses` 当作一轮。
  - `_copy_message_with_results(msg, new_tool_results)` 产出新 `Message` 实例，共享 `tool_uses` / `thinking_blocks` 引用（不可变结构）。
  - `_snip_stale_messages(history)` 在 new history 上跑（stateless），总轮数 ≤ `KEEP_RECENT_TURNS` 直接 return；超 boundary 的消息里超 `OLD_RESULT_SNIP_CHARS` 字符且未 PERSISTED/SNIPPED 前缀的 tool result 整体替换为 `<snipped>` 头 + 200 字符预览 + `… (snipped)` 尾。

## T8: Layer 1 `apply_tool_result_budget` Design B 主流程

- 影响文件: `mewcode/context/manager.py:241-348`
- 依赖任务: T2, T6, T7
- 完成标准: 签名 `apply_tool_result_budget(conversation, session_dir, state) -> tuple[ConversationManager, list[ContentReplacementRecord]]`，**不修改入参 conversation**。算法：
  1. 阶段 1: 对每个 tr 分四类——`state.replacements` 命中 → 复读；`state.seen_ids` 命中 → 冻结原文；外部已带 `PERSISTED_TAG` 前缀 → 视为已知决策，写入 state 与 records；其余进 fresh。
  2. 阶段 2 (Pass 1): fresh 中 content 长度 > `SINGLE_RESULT_CHAR_LIMIT` 调 `persist_tool_result` + `make_persisted_preview`，写入 state 与 records。
  3. 阶段 3 (Pass 2): 计算 `total = Σdecisions.values + Σremaining.content`；> `AGGREGATE_CHAR_LIMIT` 时按 content 长度降序挑直到压回上限。
  4. 阶段 4: 未决策的 fresh 全部加进 `state.seen_ids`、`decisions[id] = tr.content`。
  5. 末段: 用 `decisions` 构造新 `[ToolResultBlock]` 保持原顺序 → `_copy_message_with_results` → `_snip_stale_messages` 跑 Pass 3 → 构造新 `ConversationManager` 并复制 `env_injected / ltm_injected / last_input_tokens` flags。
- `test_apply_does_not_mutate_conv / test_first_call_freezes_unreplaced / test_replacement_byte_identical / test_frozen_never_replaced / test_aggregate_only_picks_fresh` 通过。

## T9: 阈值计算 `compute_compact_threshold` / `should_auto_compact`

- 影响文件: `mewcode/context/manager.py:350-358`
- 依赖任务: T1
- 完成标准: `compute_compact_threshold(200_000) == 167_000`、`compute_compact_threshold(200_000, manual=True) == 177_000`、`compute_compact_threshold(128_000) == 95_000`；`should_auto_compact(last_input_tokens, context_window)` 边界精确。`TestComputeCompactThreshold / TestShouldAutoCompact` 通过。

## T10: 摘要 prompt + helpers (`SUMMARY_PROMPT` / `extract_summary` / `COMPACT_BOUNDARY_MESSAGE` / `build_compact_messages` / `_group_messages_by_turn`)

- 影响文件: `mewcode/context/manager.py:360-419`
- 依赖任务: T1
- 完成标准: `SUMMARY_PROMPT` 含九节结构 + 两次禁止工具调用 + 先 `<analysis>` 再 `<summary>` 的要求；`extract_summary` 找到 `<summary>...</summary>` 整对时返回内部 trim，找不到时返回原文整体；`build_compact_messages(summary)` 输出 `[user '[摘要]\n...', assistant COMPACT_BOUNDARY_MESSAGE]` 两条；`_group_messages_by_turn` 按 `assistant && not tool_uses` 切轮。`TestExtractSummary / TestBuildCompactMessages` 通过。

## T11: 熔断器 `CompactCircuitBreaker`

- 影响文件: `mewcode/context/manager.py:421-436`
- 依赖任务: T1
- 完成标准: `@dataclass` 含 `max_failures: int = 3` 默认值与 `consecutive_failures: int = field(init=False, default=0)`；`record_failure / record_success / is_open` 三方法行为正确；`TestCompactCircuitBreaker` 通过。

## T12: Layer 2 `auto_compact`

- 影响文件: `mewcode/context/manager.py:439-end`
- 依赖任务: T9, T10, T11
- 完成标准: 自动模式 `conversation.last_input_tokens < threshold` 返回 `None`；`breaker.is_open()` 返回错误字符串；构造临时 `ConversationManager`（header SUMMARY_PROMPT + 原 history + 结尾再次提醒不要调工具）通过 `client.stream(summary_conv, system=SUMMARY_PROMPT)` 收 `TextDelta` 拼成文本；PTL 重试用 `_group_messages_by_turn` 丢最旧 1/5，最多 3 次；成功调 `conversation.replace_history(build_compact_messages(summary))` + `cleanup_tool_results(session_dir)` + `breaker.record_success()`，返回 `CompactEvent(before_tokens)`；失败 `breaker.record_failure()` 返回错误字符串。

## T13: Anthropic 客户端缓存断点

- 影响文件: `mewcode/client.py:24-68, 138-160`
- 依赖任务: 无
- 完成标准:
  - `_EPHEMERAL = {"type": "ephemeral"}` 常量定义。
  - `_mark_last_user_tail_for_cache(messages)` 倒序找最后一条 user message，对其末块（string content 自动 up-convert 为 block 列表）打 marker。
  - `_mark_last_tool_for_cache(tools)` 返回浅拷贝并给末项加 marker（不污染调用方持有的工具表）。
  - Anthropic `stream` 内：`messages` 构造后调 `_mark_last_user_tail_for_cache(messages)`；`system` 包装成 `[{"type":"text","text":system,"cache_control":_EPHEMERAL}]`；`tools` 经 `_mark_last_tool_for_cache` 处理后赋给 `kwargs["tools"]`。

## T14: Agent 集成

- 影响文件: `mewcode/agent.py:15-27, 314-316, 436-516, 887-918, 960-1003`
- 依赖任务: T8, T12, T13
- 完成标准:
  - import 段加 `ContentReplacementRecord / ContentReplacementState / append_replacement_records / create_replacement_state / load_replacement_records / reconstruct_replacement_state`。
  - `Agent.__init__` 加 `self.replacement_state: ContentReplacementState = create_replacement_state()`（line 316）。
  - 主循环（line 436 附近）：先 `await auto_compact(...)` 处理事件；中间写各种 reminder；在 `client.stream` 调用前一刻：`api_conv, _new_records = apply_tool_result_budget(conversation, self.session_dir, self.replacement_state)` → 非空 `append_replacement_records(self.session_dir, _new_records)` → `self.client.stream(api_conv, ...)`。
  - `manual_compact` 直接走 `auto_compact(..., manual=True)`，不再前置调 `apply_tool_result_budget`（compact 将整段替换 history，前置 apply 的产物会被丢弃）。
  - 另一主循环变体（line 960）：同样把 `apply_tool_result_budget` 移到 `client.stream` 前一刻。

## T15: Fork 状态继承

- 影响文件: `mewcode/tools/agent_tool.py:192-203`
- 依赖任务: T3, T14
- 完成标准: 创建 sub_agent 后判断 `p.subagent_type is None`（即真 fork）时 `from mewcode.context import clone_replacement_state` → `sub_agent.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)`。

## T16: 测试

- 影响文件: `tests/test_context.py`、`tests/test_replacement_state.py`
- 依赖任务: T2–T12
- 完成标准:
  - `tests/test_context.py` 的 `TestApplyToolResultBudget` 4 个 case 更新为 Design B 签名（接 state、判 api_conv、断言 conv 原始内容未变）；其余 `TestPersistToolResult / TestMakePersistedPreview / TestComputeCompactThreshold / TestShouldAutoCompact / TestExtractSummary / TestCompactCircuitBreaker / TestBuildCompactMessages / TestSessionDir` 全部保留并通过。
  - `tests/test_replacement_state.py` 新增 10 个 state-specific case：`test_create_returns_empty / test_clone_independent / test_apply_does_not_mutate_conv / test_first_call_freezes_unreplaced / test_replacement_byte_identical / test_frozen_never_replaced / test_aggregate_only_picks_fresh / test_reconstruct_from_records / test_reconstruct_with_inherited_parent / test_append_and_load_records_roundtrip`。
  - `PYTHONPATH=. pytest tests/test_context.py tests/test_replacement_state.py -v` 全部通过。

## T17: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T14, T15, T16
- 完成标准:
  - `PYTHONPATH=. pytest tests/test_context.py tests/test_replacement_state.py -v` 全部通过（共 33 个用例）。
  - 制造一次 Bash 大输出（> 5000 字符），观察 `.mewcode/session/tool-results/<tool_use_id>.txt` 文件落地；`.mewcode/session/replacement_records.jsonl` 出现对应行；对话历史里相应 tool result 仍为原文（Design B 不 mutate 原 conv），api_conv 视图里是 preview。
  - 制造一次连续多轮长对话使 `last_input_tokens >= 167_000`（200K 窗口）→ 主循环自动触发 Layer 2，事件流出现 `CompactNotification(before_tokens=...)`，对话被替换为 `[摘要] + 边界消息` 两条。
  - 短会话下在 TUI 输入 `/compact`，看到 `当前 token 数 X，无需压缩`。

## T18: `RecoveryState` 与限额常量

- 影响文件: `mewcode/context/manager.py:1-20, 410-510`
- 依赖任务: T1
- 完成标准:
  - 顶部 import 新增 `import threading`、`import time`。
  - 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` / `_RECOVERY_CHARS_PER_TOKEN = 3.5` 在「Post-compact recovery state」段定义。
  - `@dataclass FileReadRecord(path, content, timestamp)` 与 `@dataclass SkillInvocationRecord(name, body, timestamp)` 定义。
  - `class RecoveryState` 用 `threading.Lock` 守护 `_files` / `_skills`；`record_file_read(path, content)` / `record_skill_invocation(name, body)` 空路径直接 return，加锁写入并以 `time.time()` 打时间戳。
  - `snapshot_files(limit) / snapshot_skills()` 复制后按 timestamp 倒序，文件再切到 limit。

## T19: `build_recovery_attachment` + `build_compact_messages` 扩展

- 影响文件: `mewcode/context/manager.py:512-620`
- 依赖任务: T18
- 完成标准:
  - `_approx_tokens(s)` 按 `len / 3.5` 折算；`_truncate_by_tokens(s, budget)` 超额时按 byte 上限切并追加 `\n… (内容已截断)`；`_first_line(s)` 返回第一行非空文本。
  - `build_recovery_attachment(state, tool_schemas)` 按顺序输出 `## 最近读过的文件 / ## 已激活的技能 / ## 可用工具 / ## 提示`；空 state + 空 schemas 时返回 `""`；技能预算超 `RECOVERY_SKILLS_BUDGET` 时 break。
  - `build_compact_messages(summary, attachment="")` 把 `attachment` 用 `\n\n---\n\n` 拼到 `[摘要]\n{summary}` user 消息之后，返回 `[user, assistant(COMPACT_BOUNDARY_MESSAGE)]`。
  - `tests/test_recovery.py` 5 个测试通过：`test_recovery_attachment_empty_when_nothing_recorded / test_recovery_attachment_emits_all_sections / test_recovery_file_limit_and_order / test_recovery_truncates_per_file / test_recovery_skills_budget`。

## T20: `auto_compact` / Agent / Skill 集成

- 影响文件: `mewcode/context/manager.py:622-660`、`mewcode/context/__init__.py`、`mewcode/agent.py:295-330, 460-475, 920-930, 1000-1010, 850-870`、`mewcode/skills/executor.py:58-95`
- 依赖任务: T18, T19, T10
- 完成标准:
  - `mewcode/context/__init__.py` re-export `RecoveryState / FileReadRecord / SkillInvocationRecord / build_recovery_attachment`。
  - `auto_compact` 多两个 kwargs `recovery: RecoveryState | None = None`、`tool_schemas: list[Mapping[str, Any]] | None = None`，在生成 summary 后调 `build_recovery_attachment` 拿 attachment 再传给 `build_compact_messages(summary, attachment=attachment)`。
  - `Agent.__init__` 新增 `self.recovery_state: RecoveryState = RecoveryState()`。
  - 三处 `auto_compact` 调用点（`Agent.run` 主循环 / `manual_compact` / `run_to_completion`）都传 `recovery=self.recovery_state` 与 `tool_schemas=self.registry.get_all_schemas(self.protocol)`。
  - 新增 `Agent._snapshot_for_recovery(tc, result)` 方法（位于 `_extract_memories` 之前），仅当 `not result.is_error and tc.tool_name == "ReadFile"` 时打开 `file_path` 读 utf-8（errors="replace"）并写入 `self.recovery_state`；`OSError` 静默吞掉。
  - `Agent._execute_single_tool_direct` 与 `Agent._execute_tool` 在 `tool.execute(params)` 之后各加一行 `self._snapshot_for_recovery(tc, result)`。
  - `SkillExecutor.execute_inline` 在 `self.agent.activate_skill(...)` 之后调 `self.agent.recovery_state.record_skill_invocation(skill.name, prompt)`；`execute_fork` 在 `prompt = substitute_arguments(...)` 后立刻调 `self.agent.recovery_state.record_skill_invocation(skill.name, skill.prompt_body)`，两处都用 `getattr(self.agent, "recovery_state", None) is not None` 保护。

## T21: 端到端验证（恢复部分）

- 影响文件: 无
- 依赖任务: T18, T19, T20
- 完成标准:
  - `PYTHONPATH=. pytest tests/test_recovery.py -v` 5 个测试通过。
  - 制造一次连续 ReadFile 6 个文件 + 触发 `/compact` 的会话，摘要消息出现 `## 最近读过的文件` 段并只列最近 5 个；任一 5K token 以上的文件出现 `… (内容已截断)` 标记。
  - 制造一次 `/<skill-name>` 激活技能后再 `/compact` 的会话，摘要消息出现 `## 已激活的技能` 段并包含 skill 名 + SOP 片段。
  - 摘要消息以 `## 提示` 段收尾，强调若需要原文请重新读文件而不是靠摘要猜。

## 进度

- T1-T21（含「压缩后恢复」相关 T18-T21）
