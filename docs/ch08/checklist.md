# ch08: 上下文管理 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 1.1 常量与 session 助手

- [ ] `SINGLE_RESULT_CHAR_LIMIT = 5_000` 在 `mewcode/context/manager.py:16` 定义。
- [ ] `AGGREGATE_CHAR_LIMIT = 20_000`、`PREVIEW_CHARS = 2_000`、`KEEP_RECENT_TURNS = 10`、`OLD_RESULT_SNIP_CHARS = 2_000`、`SNIPPED_TAG = "<snipped>"` 在 `manager.py:17-22` 定义。
- [ ] `SUMMARY_OUTPUT_RESERVE = 20_000`、`AUTO_COMPACT_SAFETY_MARGIN = 13_000`、`MANUAL_COMPACT_SAFETY_MARGIN = 3_000`、`PERSISTED_TAG = "<persisted-output>"`、`SESSION_SUBDIR = ".mewcode/session/tool-results"` 在 `manager.py:24-30` 定义。
- [ ] `ensure_session_dir(work_dir) -> Path` 在 `manager.py:132` 实现：创建并返回 `Path("<work_dir>/.mewcode/session/tool-results")`，`mkdir(parents=True, exist_ok=True)`。
- [ ] `cleanup_tool_results(session_dir)` 在 `manager.py:138` 实现：`shutil.rmtree` + 重建空目录。

### 1.2 状态容器与 transcript

- [ ] `@dataclass CompactEvent(before_tokens: int)` 在 `manager.py:37-38` 定义。
- [ ] `@dataclass ContentReplacementState`（`seen_ids: set[str]` + `replacements: dict[str, str]`，都用 `field(default_factory=...)`）在 `manager.py:46-49` 定义。
- [ ] `@dataclass ContentReplacementRecord(tool_use_id, replacement, kind="tool-result")` 在 `manager.py:52-56` 定义。
- [ ] `create_replacement_state()` 在 `manager.py:59-60` 返回空容器；`clone_replacement_state(src)` 在 `manager.py:63-67` 用 `set(src.seen_ids)` + `dict(src.replacements)` 浅拷贝。
- [ ] `REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"` 在 `manager.py:70` 定义。
- [ ] `append_replacement_records(session_dir, records)` 在 `manager.py:73-86` 实现：空切片直接 return；用 `open("a", encoding="utf-8")` 追加；每行一个 JSON 对象（含 `kind / tool_use_id / replacement` 三 key）。
- [ ] `load_replacement_records(session_dir)` 在 `manager.py:88-104` 实现：缺文件返回空列表；逐行 `json.loads`。
- [ ] `reconstruct_replacement_state(messages, records, inherited_replacements=None)` 在 `manager.py:107-127` 实现，包括 candidate-only 过滤与 inheritedReplacements gap-fill。

### 1.3 Layer 1 持久化与决策应用

- [ ] `persist_tool_result(tool_use_id, content, session_dir)` 在 `manager.py:148-156` 实现：`os.open(..., O_CREAT | O_EXCL)`，`FileExistsError` 静默跳过保证幂等。
- [ ] `make_persisted_preview(content, file_path)` 在 `manager.py:159-170` 实现：返回 `<persisted-output>\n输出太大（XKB），完整内容已保存到：\n<file_path>\n\n预览（前 2KB）：\n<前 2_000 字符>\n</persisted-output>`（这个字符串是 byte-stable 的 anchor，不能轻改）。
- [ ] `_count_turns(messages)` / `_copy_message_with_results(msg, new_tool_results)` / `_snip_stale_messages(history)` 在 `manager.py:173-238` 实现。
- [ ] `apply_tool_result_budget(conversation, session_dir, state) -> tuple[ConversationManager, list[ContentReplacementRecord]]` 在 `manager.py:241-348` 实现，**不修改入参 conversation**：
  - 阶段 1 四类分类（replacements 命中复读 / seen_ids 命中冻结原文 / PERSISTED_TAG 前缀冻结作为已知决策 / fresh）。
  - 阶段 2 Pass 1 单条 persist。
  - 阶段 3 Pass 2 聚合超限 + 按 size 降序选 fresh。
  - 阶段 4 剩余 fresh 冻结。
  - 末段 `_copy_message_with_results` + `_snip_stale_messages` + 新 `ConversationManager`。

### 1.4 Layer 2 摘要

- [ ] `compute_compact_threshold(context_window, manual=False)` 在 `manager.py:350-353` 实现，公式 `window - SUMMARY_OUTPUT_RESERVE - (3_000 if manual else 13_000)`。
- [ ] `should_auto_compact(last_input_tokens, context_window)` 在 `manager.py:356-358` 实现。
- [ ] `SUMMARY_PROMPT` 在 `manager.py:360-379` 定义，包含九节结构 + 两次禁止工具调用 + 先 `<analysis>` 再 `<summary>` 的指令。
- [ ] `extract_summary(llm_output)` 在 `manager.py:382-387` 实现：找 `<summary>` / `</summary>` 标签对取内部 trim，找不到则返回原文。
- [ ] `COMPACT_BOUNDARY_MESSAGE` 在 `manager.py:390-393` 定义；`build_compact_messages(summary)` 在 `manager.py:396-400` 实现。
- [ ] `_group_messages_by_turn(messages)` 在 `manager.py:403-413` 实现。
- [ ] `@dataclass CompactCircuitBreaker(max_failures=3)` 在 `manager.py:421-436` 实现，含 `record_failure / record_success / is_open` 三方法。
- [ ] `async auto_compact(conversation, client, context_window, session_dir, protocol="anthropic", manual=False, breaker=None)` 在 `manager.py:439-end` 实现，覆盖阈值判断、熔断、PTL 重试（最多 3 次，每次丢 1/5 最旧轮）、`extract_summary` + `replace_history` + `cleanup_tool_results` 全流程。
- [ ] 边界处理 `breaker is None` 时不调用 `record_failure / record_success`（多处显式 `if breaker is not None`）。
- [ ] 边界处理 `extract_summary` 中 `<summary>` 或 `</summary>` 缺失时返回原文，不抛错。

### 1.5 Anthropic 缓存断点

- [ ] `_EPHEMERAL = {"type": "ephemeral"}` 在 `mewcode/client.py:24` 定义。
- [ ] `_mark_last_user_tail_for_cache(messages)` 在 `client.py:27-52` 实现：倒序找最后一条 user message，对其末块（string content 自动 up-convert 成 block 列表）打 marker。
- [ ] `_mark_last_tool_for_cache(tools)` 在 `client.py:55-68` 实现：返回浅拷贝并给末项加 marker。
- [ ] `AnthropicLLMClient.stream` 在请求构造期间打三处 cache marker（`client.py:138-160`）：`messages` 构造后调 `_mark_last_user_tail_for_cache(messages)`；`system` 包装成 `[{"type":"text","text":system,"cache_control":_EPHEMERAL}]`；`tools` 经 `_mark_last_tool_for_cache` 处理后赋给 `kwargs["tools"]`。

### 1.6 `RecoveryState` 与恢复块（同样在 `mewcode/context/manager.py`）

- [ ] 顶部 import 新增 `threading` 与 `time`。
- [ ] 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` / `_RECOVERY_CHARS_PER_TOKEN = 3.5` 在「Post-compact recovery state」段定义。
- [ ] `@dataclass FileReadRecord` / `@dataclass SkillInvocationRecord` 含 `timestamp: float` 字段。
- [ ] `class RecoveryState` 用 `threading.Lock` 守护 `_files` / `_skills` 两张 dict；`record_file_read(path, content)` / `record_skill_invocation(name, body)` 空路径直接 return，加锁写入并以 `time.time()` 打时间戳。
- [ ] `snapshot_files(limit)` / `snapshot_skills()` 复制后按 timestamp 倒序，文件再切到 limit。
- [ ] `_approx_tokens` / `_truncate_by_tokens` / `_first_line` 三个辅助；`_truncate_by_tokens` 超额时按 byte 上限切并追加 `\n… (内容已截断)`。
- [ ] `build_recovery_attachment(state, tool_schemas)` 依次输出 `## 最近读过的文件 / ## 已激活的技能 / ## 可用工具 / ## 提示`；空 state + 空 schemas 时返回 `""`；技能预算超 `RECOVERY_SKILLS_BUDGET` 时停止追加。
- [ ] `build_compact_messages(summary, attachment="")` 把 `attachment` 用 `\n\n---\n\n` 拼到 `[摘要]\n{summary}` user 消息之后。
- [ ] `auto_compact` 多两个 kwargs `recovery: RecoveryState | None = None`、`tool_schemas: list[Mapping[str, Any]] | None = None`，在 `extract_summary` 后调 `build_recovery_attachment` 再传给 `build_compact_messages`。
- [ ] `mewcode/context/__init__.py` re-export `RecoveryState / FileReadRecord / SkillInvocationRecord / build_recovery_attachment`。

### 1.7 Agent / Skill 接入

- [ ] `Agent.__init__` 新增 `self.recovery_state: RecoveryState = RecoveryState()`。
- [ ] 三处 `auto_compact` 调用点（`run` 主循环 / `manual_compact` / `run_to_completion`）都传 `recovery=self.recovery_state` 与 `tool_schemas=self.registry.get_all_schemas(self.protocol)`。
- [ ] 新增 `Agent._snapshot_for_recovery(tc, result)` 方法：`not result.is_error and tc.tool_name == "ReadFile"` 时打开 `file_path` 读 utf-8（errors="replace"）并写入 `self.recovery_state`；`OSError` 静默吞掉。
- [ ] `Agent._execute_single_tool_direct` 与 `Agent._execute_tool` 在 `tool.execute(params)` 之后调 `self._snapshot_for_recovery(tc, result)`。
- [ ] `SkillExecutor.execute_inline` 在 `self.agent.activate_skill(...)` 之后调 `record_skill_invocation(skill.name, prompt)`；`execute_fork` 在 `prompt = substitute_arguments(...)` 后调 `record_skill_invocation(skill.name, skill.prompt_body)`，两处都用 `getattr(self.agent, "recovery_state", None) is not None` 保护。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "from mewcode.context" mewcode --include="*.py" | grep -v "context/"` 至少 1 处导入：
  - `mewcode/agent.py:15-27`（导入 `CompactCircuitBreaker / CompactEvent / ContentReplacementRecord / ContentReplacementState / append_replacement_records / apply_tool_result_budget / auto_compact / create_replacement_state / ensure_session_dir / load_replacement_records / reconstruct_replacement_state`）。
- [ ] `grep -rn "apply_tool_result_budget\|auto_compact\|manual_compact\|clone_replacement_state" mewcode --include="*.py"` 命中：
  - `mewcode/agent.py:316`（`Agent.__init__` 初始化 `replacement_state`）
  - `mewcode/agent.py:510`（主循环调 `apply_tool_result_budget`）
  - `mewcode/agent.py:998`（另一主循环变体调 `apply_tool_result_budget`）
  - `mewcode/agent.py` 中 `manual_compact` 调 `auto_compact(..., manual=True)`
  - `mewcode/commands/handlers/compact.py`（`/compact` 命令调 `ctx.agent.manual_compact`）
  - `mewcode/tools/agent_tool.py:200-203`（fork 调 `clone_replacement_state` 注入子 Agent）
- [ ] `grep -rn "RecoveryState\b\|recovery_state" mewcode --include="*.py"` 命中：
  - `mewcode/context/manager.py` 定义。
  - `mewcode/context/__init__.py` re-export。
  - `mewcode/agent.py`：`Agent.__init__` 初始化 + 三处 `auto_compact` kwarg + `_snapshot_for_recovery` 方法 + 两处 `_execute_*` 调用。
  - `mewcode/skills/executor.py`：`execute_inline` / `execute_fork` 各一处 `record_skill_invocation`。
- [ ] 调用入口位于 `Agent` 主循环（`agent.py:510` 在 `Agent.run` 的 `while iteration <= self.max_iterations` 循环内、`client.stream` 调用之前）。
- [ ] 命令注册中心已更新: `COMPACT_COMMAND` 在 `mewcode/commands/handlers/__init__.py` 导出，由 registry 注册到 `/compact` + 别名 `/c`。
- [ ] 用户输入到本模块的路径可一句话描述:
  - 自动: agent 主循环新一轮 → `auto_compact` 阈值判断 → 写入 reminder → `apply_tool_result_budget` 产出 api_conv → `client.stream(api_conv, ...)`。
  - 手动: 用户在 TUI 输入 `/compact` → `handle_compact` → `Agent.manual_compact` → `auto_compact(..., manual=True)` → 回传 `CompactNotification | ErrorEvent`。
  - Fork: 父 Agent 调 Agent 工具触发 fork → `agent_tool.py` 创建 sub_agent → 注入父 state 的 `clone_replacement_state` → 子 Agent 用克隆状态独立演化。
- [ ] **死代码核查**：所有公开符号都在被引用：`ContentReplacementState / ContentReplacementRecord / create_replacement_state / clone_replacement_state / reconstruct_replacement_state / append_replacement_records / load_replacement_records / apply_tool_result_budget / auto_compact / compute_compact_threshold / should_auto_compact / extract_summary / build_compact_messages / make_persisted_preview / persist_tool_result / CompactCircuitBreaker / CompactEvent / ensure_session_dir / cleanup_tool_results` 全部在 `mewcode/context/__init__.py` 导出且被外部模块或测试引用。

## 3. 编译与测试

- [ ] `ruff check mewcode/context mewcode/commands/handlers/compact.py mewcode/client.py mewcode/agent.py mewcode/tools/agent_tool.py` 通过。
- [ ] `PYTHONPATH=. pytest tests/test_context.py tests/test_replacement_state.py tests/test_recovery.py -v` 全部通过：
  - `TestPersistToolResult / TestMakePersistedPreview / TestApplyToolResultBudget`（4 case，已更新为 Design B）/ `TestComputeCompactThreshold / TestShouldAutoCompact / TestExtractSummary / TestCompactCircuitBreaker / TestBuildCompactMessages / TestSessionDir`（已有）。
  - `test_create_returns_empty / test_clone_independent / test_apply_does_not_mutate_conv / test_first_call_freezes_unreplaced / test_replacement_byte_identical / test_frozen_never_replaced / test_aggregate_only_picks_fresh / test_reconstruct_from_records / test_reconstruct_with_inherited_parent / test_append_and_load_records_roundtrip`（10 个新增）。
  - `test_recovery_attachment_empty_when_nothing_recorded / test_recovery_attachment_emits_all_sections / test_recovery_file_limit_and_order / test_recovery_truncates_per_file / test_recovery_skills_budget`（5 个恢复测试）。
- [ ] `PYTHONPATH=. pytest tests/ -v` 整套未被本章引入新的失败用例（`test_plan_mode_denied_tool_returns_error` 已知预存 hang，可 `--deselect`）。

## 4. 端到端验证

- [ ] Layer 1 字节稳定性：制造一轮内并行调 5 个 Bash、每个吐 4.5K 字符的会话（总 22.5K，触发 Pass 2）；`apply_tool_result_budget` 返回的 `api_conv` 里其中一条 tool_result content 为 `<persisted-output>` 包裹的 preview；下一轮再调一次，同一 `tool_use_id` 的 content 与上一轮完全相等（state.replacements 复读）。
- [ ] Layer 1 不 mutate 原 conv：`test_apply_does_not_mutate_conv` 守住；调 `apply_tool_result_budget` 前后 `conversation.history` 各 `tool_result.content` 完全相等。
- [ ] Layer 1 frozen 不再替换：`test_frozen_never_replaced` 验证「第一轮未替换的 id 在后续轮即使聚合超限也不被选中」。
- [ ] Layer 2 触发：制造长对话使 `last_input_tokens >= 167_000`（200K 窗口）→ 主循环自动触发 Layer 2，事件流出现 `CompactNotification(before_tokens=...)`，对话被替换为 `[摘要] + 边界消息` 两条。
- [ ] Layer 2 熔断：人为让 `auto_compact` 连续失败 3 次后第 4 次直接返回错误字符串不发请求。
- [ ] Spill 落盘：长 Bash 输出后 `<work_dir>/.mewcode/session/tool-results/` 目录下出现以 `<tool_use_id>.txt` 命名的文件。
- [ ] Transcript 落盘：`<work_dir>/.mewcode/session/replacement_records.jsonl` 出现新条目，`jq .` 可解析。
- [ ] Fork 隔离：fork 出去的子 Agent 修改自己 state 的 seen_ids / replacements 不影响父 Agent。
- [ ] 短会话下在 TUI 输入 `/compact`，看到 `当前 token 数 X，无需压缩`（input_tokens < 5000 分支）。
- [ ] 长会话下在 TUI 输入 `/compact`，看到 `上下文已压缩（压缩前 X tokens）`（`CompactNotification` 渲染）。
- [ ] 恢复块文件段：先 ReadFile 两个不同路径再触发 `/compact`，摘要消息出现 `## 最近读过的文件` 段、两个 `### <绝对路径>` 子段，每段内容用 ``` 包住。
- [ ] 恢复块技能段：先 `/<skill-name>` 激活一个 skill 再 `/compact`，摘要消息出现 `## 已激活的技能` 段并包含 skill 名 + SOP 片段。
- [ ] 恢复块工具段：摘要消息出现 `## 可用工具` 段，并把当前 registry 里的工具按 `- 名字 — 描述首行` 列出。
- [ ] 恢复块收尾提示：摘要消息以 `## 提示` 段收尾。
- [ ] 限额硬上限：人造 6+ 个 ReadFile 后压缩，恢复块只列最近 5 个；任一 5K token 以上的文件出现 `… (内容已截断)` 标记。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `docs/ch08/`）。
- [ ] 跨分支设计文档存在：`docs/extras/content-replacement-state.md` 描述 ContentReplacementState 三分支统一设计与 Design B（不 mutate）契约。
- [ ] commit 信息标注 `ch08` 与三件套关闭状态。
