# ch08: 上下文管理 Spec

## 1. 背景

LLM 上下文窗口有上限，但长任务里 tool result（Bash 输出、长文件）很容易在几轮内把窗口顶爆。没有上下文管理就意味着 Agent 跑到一半被 API 退回 `prompt_too_long`，会话失败、上下文丢失、用户得手动重启。

本章用「先廉价救火再花钱总结」两层策略解决：第 1 层不调 LLM，只做本地写盘 + 决策记录；第 2 层在 `last_input_tokens` 过阈值时整段摘要。第 1 层加一个跨轮持久的「替换决策日志」 `ContentReplacementState`，让每个 tool result 的「替换/不替换」决定只做一次、之后字节相同地复读 —— 这是 Anthropic prompt cache 命中所需的前缀稳定性的关键。

## 2. 目标

交付一套两层上下文管理 + 压缩后恢复：

- **Layer 1**：`apply_tool_result_budget(conversation, session_dir, state)` 每轮 agent loop 都跑。读取 `ContentReplacementState` 已记录的决策，对新候选评估「单条超限」和「聚合超限」两条规则；选中的 tool result 写盘换 `<persisted-output>` preview 字符串，决定写入 state；过 `KEEP_RECENT_TURNS` 轮的陈旧 tool result 裁为 `<snipped>` 一段。返回**新的** `ConversationManager`，原 conversation 不动。新决策追加写到 `<session_dir>/replacement_records.jsonl`。
- **Layer 2**：`auto_compact(conversation, client, context_window, session_dir, ...)` 在 `conversation.last_input_tokens >= threshold` 时调 LLM 拼摘要，把整段会话换成 `[摘要]` + 边界消息两条。`CompactCircuitBreaker` 连续失败 `max_failures` 次后熔断不再发请求。
- **Layer 2 后恢复**：`RecoveryState` 跨轮记录每次 ReadFile 的字节快照与每次 Skill 调用的 SOP 文本。`auto_compact` 在拼出摘要、构造新会话之前先调 `build_recovery_attachment(state, tool_schemas)`，把「最近读过的文件 / 已激活的技能 / 当前可用工具 / 收尾提示」四段拼到摘要 user 消息末尾，避免摘要替换后模型瞬间失去工作记忆。

两层在 Agent 主循环里串联：Layer 2 先跑（决定是否整段摘要 + 恢复，需要时**就地** mutate `conversation.history`）→ 写入各种 system reminder → Layer 1 在 `client.stream` 调用前最后一刻跑、把 `api_conv` 喂给 LLM。手动入口 `manual_compact` 给 `/compact` 用，切到 `MANUAL_COMPACT_SAFETY_MARGIN` 更小的安全余量直接走 Layer 2。

Anthropic 客户端在 system / tools 末项 / 最后一条 user message 末尾三处加 `cache_control: {"type": "ephemeral"}` 标记；配合 Layer 1 的字节稳定 replacements，前缀缓存就能命中。

## 3. 功能需求

### 3.1 状态容器与持久化

- F1: `ContentReplacementState` dataclass（`seen_ids: set[str]` + `replacements: dict[str, str]`），以及 `create_replacement_state()` / `clone_replacement_state(src)` 两个工厂。`seen_ids` 收录每个判断过的 `tool_use_id`，`replacements` 仅收录决定「替换」的那些 id 到 preview 字符串。不变量：`replacements.keys() ⊆ seen_ids`。
- F2: `ContentReplacementRecord` dataclass（`tool_use_id`, `replacement`, `kind="tool-result"`），及 JSONL I/O：
  - `append_replacement_records(session_dir, records)`：空切片直接 return；用 `open("a", encoding="utf-8")` 追加，每行一个 JSON 对象。
  - `load_replacement_records(session_dir)`：缺文件返回空列表；逐行 `json.loads`。
- F3: `reconstruct_replacement_state(messages, records, inherited_replacements=None)`：seed `seen_ids` = `{ tr.tool_use_id | for tr in m.tool_results, for m in messages }`；按 `r.kind == "tool-result"` 过滤 records 并命中 candidate 才写入 `replacements`；可选 `inherited_replacements` 做 gap-fill。

### 3.2 Layer 1 应用流程

- F4: `apply_tool_result_budget(conversation, session_dir, state)` 返回 `tuple[ConversationManager, list[ContentReplacementRecord]]`，**不修改入参 conversation**。对每条 tool result 按 4 步处理：
  1. id ∈ `state.replacements` → 取出该字符串原样贴入 api_conv（纯查表，无 I/O）。
  2. id ∈ `state.seen_ids`（但不在 replacements）→ 保留原文。
  3. 外部已带 `PERSISTED_TAG` 前缀 → 视为已知决策，写入 state 与 records，作为字面字符串保留。
  4. 其余进 fresh，跑 Pass 1：单条 content 超 `SINGLE_RESULT_CHAR_LIMIT` → `persist_tool_result` + `make_persisted_preview` → 写入 state；剩余 fresh 跑 Pass 2：消息聚合 > `AGGREGATE_CHAR_LIMIT` 时按 content 长度降序挑直到聚合压回上限；未挑中的 fresh 标 seen 冻结为「不替换」。
- F5: spill 文件 `persist_tool_result` 用 `os.open(O_WRONLY | O_CREAT | O_EXCL)` 写到 `<work_dir>/.mewcode/session/tool-results/<tool_use_id>.txt`，`FileExistsError` 静默吞掉（幂等）。
- F6: preview 格式 `make_persisted_preview` 输出 `<persisted-output>\n输出太大（XKB），完整内容已保存到：\n<file_path>\n\n预览（前 2KB）：\n<content[:PREVIEW_CHARS]>\n</persisted-output>`。这个字符串一旦写入 `state.replacements`，后续每轮逐字节复读，不能改格式。
- F7: 通过 `PERSISTED_TAG = "<persisted-output>"` 与 `SNIPPED_TAG = "<snipped>"` 前缀识别已 persist / snipped 内容，避免重复处理。
- F8: Pass 3 陈旧裁剪 `_snip_stale_messages`：在 Pass 1/2 输出的 new history 上跑（不动原 conversation）；超过 `KEEP_RECENT_TURNS` 轮的消息里，超过 `OLD_RESULT_SNIP_CHARS` 字符且未被 PERSISTED/SNIPPED 前缀标记的 tool result 整体替换为 `<snipped>\n(旧结果已裁剪，原始长度 N 字符)\n<前 200 字符>\n… (snipped)`。

### 3.3 Layer 2 摘要

- F9: 阈值计算 `compute_compact_threshold(context_window, manual=False)`，公式 `window - SUMMARY_OUTPUT_RESERVE - (MANUAL_COMPACT_SAFETY_MARGIN if manual else AUTO_COMPACT_SAFETY_MARGIN)`；`should_auto_compact(last_input_tokens, context_window)` 给布尔。
- F10: `auto_compact` 流程：把当前 conversation 全量塞进临时 `ConversationManager` + `SUMMARY_PROMPT` 包装 → 通过 `client.stream(...)` 收 `TextDelta` 拼成完整文本 → `extract_summary` 剥 `<analysis>`、保留 `<summary>` → `build_compact_messages` 构造 `[摘要] + 边界消息` 替换原会话 → `cleanup_tool_results` 清空 session 目录。
- F11: 摘要后处理 `extract_summary` 容错：找到 `<summary>`/`</summary>` 标签对取内部 trim；找不到完整标签对则返回原文整体，绝不丢摘要。
- F12: 摘要 prompt `SUMMARY_PROMPT` 强制九节结构（主要请求、关键概念、文件与代码段、错误与修复、解决过程、用户原话、待办、当前工作、下一步），并明确禁止工具调用、要求先 `<analysis>` 再 `<summary>`。
- F13: 熔断器 `CompactCircuitBreaker(max_failures=3)` 含 `record_failure / record_success / is_open` 三方法；自动模式下 `is_open()` 时 `auto_compact` 直接回错误字符串不发摘要请求。
- F14: PTL 重试：摘要请求自身报 `prompt too long` 时，`_group_messages_by_turn` 把对话按轮分组、丢掉最旧 1/5，最多重试 3 次；耗尽后 `breaker.record_failure()` 并返回错误字符串。
- F15: 手动入口 `manual_compact`：直接调 `auto_compact(..., manual=True)`，跳过 Layer 1 调用，安全余量切到 `MANUAL_COMPACT_SAFETY_MARGIN = 3_000`，对话不为空就压。

### 3.4 Anthropic 缓存断点与集成

- F16: `client.py` 在请求构造期间打三处 `cache_control: ephemeral`：
  - `system` 参数包装成 `[{"type":"text","text":system,"cache_control":{"type":"ephemeral"}}]`。
  - `tools` 末项的 schema dict 加 `"cache_control":{"type":"ephemeral"}`（用 `_mark_last_tool_for_cache` 浅拷贝避免污染调用方的工具表）。
  - 最后一条 user message 的末块用 `_mark_last_user_tail_for_cache` 原地打 marker（string content 自动 up-convert 为 block 列表）。
- F17: `Agent.__init__` 把 `self.replacement_state = create_replacement_state()` 初始化为空容器；三处 `apply_tool_result_budget` 调用点（main loop / manual_compact / 另一主循环变体）都传 `self.replacement_state` 并把 new records 写入 transcript。
- F18: Fork 子 Agent 路径 `mewcode/tools/agent_tool.py` 创建 sub_agent 后判断 `p.subagent_type is None`（即真 fork）时 `sub_agent.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)`。

### 3.5 压缩后恢复

- F19: `RecoveryState` 类含 `_files: dict[str, FileReadRecord]` 与 `_skills: dict[str, SkillInvocationRecord]`，用 `threading.Lock` 守护；`record_file_read(path, content)` / `record_skill_invocation(name, body)` 加锁写入并以 `time.time()` 打时间戳，空路径 / 空名字直接 return；`snapshot_files(limit)` / `snapshot_skills()` 复制后按时间戳降序，文件再切到 limit。`Agent.__init__` 把 `self.recovery_state = RecoveryState()` 默认初始化。
- F20: 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` / `_RECOVERY_CHARS_PER_TOKEN = 3.5` 在 `mewcode/context/manager.py` 顶部段定义。`_approx_tokens` 按 3.5 chars/token 折算；`_truncate_by_tokens` 按预算硬切并追加 `\n… (内容已截断)` 标记。
- F21: `build_recovery_attachment(state, tool_schemas)` 渲染四段（顺序：`## 最近读过的文件` → `## 已激活的技能` → `## 可用工具` → `## 提示`）；任一段为空就跳过；全空返回 `""`；技能累计字节超过 `RECOVERY_SKILLS_BUDGET` 时停止追加。`build_compact_messages(summary, attachment="")` 把恢复块用 `\n\n---\n\n` 拼到 `[摘要]` user 消息之后再返回 `[user, assistant]`。
- F22: `auto_compact` 多两个 kwargs `recovery: RecoveryState | None = None`、`tool_schemas: list[Mapping[str, Any]] | None = None`，生成 summary 后调 `build_recovery_attachment` 拿到 attachment，再调 `build_compact_messages(summary, attachment=attachment)`。三处调用点（main loop / `run_to_completion` / `manual_compact`）都传 `recovery=self.recovery_state` 与 `tool_schemas=self.registry.get_all_schemas(self.protocol)`。
- F23: 工具快照：`Agent._snapshot_for_recovery(tc, result)` 在 `_execute_single_tool_direct` 与 `_execute_tool` 两条工具执行路径末尾调用，仅在 `not result.is_error and tc.tool_name == "ReadFile"` 时打开 `file_path` 读取整文件（`encoding="utf-8", errors="replace"`）并写入 `self.recovery_state`；`OSError` 静默吞掉。
- F24: 技能快照：`SkillExecutor.execute_inline / execute_fork` 在 `self.agent.activate_skill` / 创建 fork_conv 之前判断 `getattr(self.agent, "recovery_state", None) is not None` 后调 `self.agent.recovery_state.record_skill_invocation(name, body)`；inline 记录渲染后的 prompt，fork 记录原始 `skill.prompt_body`。

## 4. 非功能需求

- N1: Layer 1 必须廉价：纯本地文件 I/O + 字符串改写，不调 LLM；每轮 agent loop 都跑也不能成为瓶颈。
- N2: `apply_tool_result_budget` 不能 mutate 入参 `conversation` —— 通过新建 `Message` / `ToolResultBlock` 实例 + 重组 `new_history` 产出 api_conv。测试用 `test_apply_does_not_mutate_conv` 守住。
- N3: 已决策 id 的复读必须**字节一致**：从 `state.replacements` 拿出来的字符串直接赋给 `decisions[id]`，不重新读盘、不重新格式化。这是 prompt cache 命中的硬约束。
- N4: spill 写盘幂等：用 `O_CREAT | O_EXCL`，同 `tool_use_id` 重复运行写同一份内容，已存在则 `FileExistsError` 静默跳过；spill 文件路径稳定（`<work_dir>/.mewcode/session/tool-results/<tool_use_id>.txt`），不含时间戳。
- N5: Layer 2 期间不能再触发新的 tool call —— 摘要走的是临时 `ConversationManager` + `SUMMARY_PROMPT` 一次性 stream，不绕回 agent 主循环。
- N6: `auto_compact` 替换 conversation 用 `conversation.replace_history(...)` 就地写法，让调用方持有的 `ConversationManager` 引用保持有效。
- N7: 当 `breaker is None`（测试或一次性脚本场景）熔断器禁用，不能崩。
- N8: 阈值用固定常量（`SUMMARY_OUTPUT_RESERVE = 20_000` / `AUTO_COMPACT_SAFETY_MARGIN = 13_000`）而不是百分比，确保 200K / 1M 等不同窗口下 buffer 大小一致。
- N9: 子 Agent fork 的 state 必须是父 state 的**独立深拷贝**：子端 mutate 不影响父端，反向亦然。`set(src)` 和 `dict(src)` 浅拷贝足够（值是字符串和 hash key，不需要 deepcopy）。测试用 `test_clone_independent` 守住。
- N10: `RecoveryState` 必须并发安全：`StreamingExecutor` 用 `asyncio.gather` 并发跑 ReadFile，多个回写可能交错。结构体内 `threading.Lock` 保护两张 map；`record_*` 方法在空路径 / 空名字上直接 return，方便测试与一次性脚本调用。
- N11: 恢复块限额是**硬上限**：5 个文件、单文件 5K token、技能预算 25K token、单技能 5K token。超出预算时静默丢弃（不抛错），保证压缩输出体积可预测——压缩后摘要 + 恢复总长稳定在约 60K token 以内，远低于 `compute_compact_threshold` 阈值。

## 5. 设计概要

- 核心模块结构 (`mewcode/context/manager.py`):
  - 常量段（顶部）：阈值、tag、session 子目录。
  - 状态段：`ContentReplacementState` / `ContentReplacementRecord` / `create_replacement_state` / `clone_replacement_state` / `reconstruct_replacement_state` / `append_replacement_records` / `load_replacement_records` / `REPLACEMENT_RECORDS_FILENAME`。
  - Session 段：`ensure_session_dir` / `cleanup_tool_results`。
  - Layer 1 段：`persist_tool_result` / `make_persisted_preview` / `_count_turns` / `_copy_message_with_results` / `_snip_stale_messages` / `apply_tool_result_budget`。
  - Layer 2 段：`compute_compact_threshold` / `should_auto_compact` / `SUMMARY_PROMPT` / `extract_summary` / `COMPACT_BOUNDARY_MESSAGE` / `build_compact_messages` / `_group_messages_by_turn` / `CompactCircuitBreaker` / `auto_compact`。
  - 恢复段：`RECOVERY_FILE_LIMIT / RECOVERY_TOKENS_PER_FILE / RECOVERY_SKILLS_BUDGET / RECOVERY_TOKENS_PER_SKILL / _RECOVERY_CHARS_PER_TOKEN` 常量 / `FileReadRecord` / `SkillInvocationRecord` dataclass / `RecoveryState` 类 + `record_file_read` / `record_skill_invocation` / `snapshot_files` / `snapshot_skills` / `_approx_tokens` / `_truncate_by_tokens` / `_first_line` / `build_recovery_attachment`。`mewcode/context/__init__.py` re-export 类名 + 工厂 + builder。
- 主流程（每轮 agent loop）:
  - 主循环开头：`compact_result = await auto_compact(conversation, client, context_window, session_dir, ..., recovery=self.recovery_state, tool_schemas=self.registry.get_all_schemas(self.protocol))` 内部按阈值决定是否真做摘要；成功时 yield `CompactNotification` + 重新 `inject_environment` / `inject_long_term_memory`。
  - 各种 system reminder 写入 conversation。
  - 在 `client.stream` 调用前一刻：`api_conv, new_records = apply_tool_result_budget(conversation, self.session_dir, self.replacement_state)` → `append_replacement_records(self.session_dir, new_records)` → `client.stream(api_conv, ...)`。
- 主流程（工具调用快照）:
  - `Agent._execute_single_tool_direct` / `Agent._execute_tool` 在 `tool.execute(params)` 之后调 `self._snapshot_for_recovery(tc, result)`；命中 ReadFile + 非错误时按原路径打开文件读字节写入 `self.recovery_state`。
- 主流程（Skill 调用快照）:
  - 用户输入 `/<skill-name>` → 命令分发到 `SkillExecutor.execute_inline` 或 `execute_fork` → 在改 `self.agent.activate_skill` / 起 fork_conv 之前先 `self.agent.recovery_state.record_skill_invocation(...)`。
- 主流程（手动 `/compact`）:
  - 用户输入 `/compact` → `COMPACT_COMMAND.handler = handle_compact` → 读 `ctx.ui.get_token_count()`，<5000 直接提示无需压缩；否则调 `ctx.agent.manual_compact(ctx.conversation)`。
  - `Agent.manual_compact` 直接调 `auto_compact(..., manual=True)`，拿到 `CompactEvent` 包成 `CompactNotification`，否则返回 `ErrorEvent`。
- 主流程（fork 子 Agent）:
  - `AgentTool.execute` 触发 fork → 创建 sub_agent → 当 `p.subagent_type is None` 时 `sub_agent.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)` → 子 Agent 用克隆状态独立演化。
- Anthropic 客户端缓存断点（`mewcode/client.py`）:
  - `_mark_last_user_tail_for_cache(messages)` 给最后一条 user message 末块打 marker（string content 自动 up-convert 成 block 列表）。
  - `_mark_last_tool_for_cache(tools)` 浅拷贝 tools，给末项加 marker。
  - system 参数包装成 block 列表带 marker。
- 与其他模块的交互:
  - 依赖 `mewcode.conversation`（`ConversationManager / Message / ToolResultBlock / ToolUseBlock` 与 `inject_environment / inject_long_term_memory / replace_history / serialize`）。
  - 依赖 `mewcode.tools.base`（`TextDelta / StreamEnd / StreamEvent` 收摘要 stream 事件）。
  - 被 `mewcode.agent.Agent`（主循环 + `manual_compact` + 另一主循环变体）、`mewcode.commands.handlers.compact`（`/compact` 命令）、`mewcode.tools.agent_tool`（fork clone）调用。

## 6. Out of Scope

- 跨进程 / 跨会话的压缩缓存。
- Micro-compact 与分段压缩：本仓库一次直接全量摘要，不做 partial / per-segment 压缩。
- 持久化的 `RecoveryState`：进程退出后状态丢失，不做磁盘落盘。下一次启动靠用户自然触发 ReadFile / Skill 调用重新填充。
- Session memory compaction：与 ch09 记忆系统配合，本章不做。
- 用真实 tokenizer 替代「LLM 返回的 `last_input_tokens`」作为阈值输入。
- 反应式 ContextTooLong 拦截重试：Python 版未实现，预防 + 手动两条路径已覆盖主要场景。
- 完整 resume 流程：transcript records 已落盘且 `reconstruct_replacement_state` 可用，但 resume 主流程不在本章范围。
- Pass 3 陈旧裁剪的边界穿越漂移：从「未裁剪」到「裁剪」那一轮前缀变了，会导致一次 cache miss；接受为已知 trade-off。
- 配置化阈值：所有阈值是模块常量，调整需改源码。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
