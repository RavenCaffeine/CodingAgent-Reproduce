# ch05: System Prompt 设计 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地（`origin/python` 分支）。

## T1: 定义 PromptSection / PromptBuilder 数据结构
- 影响文件: `mewcode/prompts.py:10-28`
- 依赖任务: 无
- 完成标准: `@dataclass class PromptSection` 含 `name: str / priority: int / content: str`（`mewcode/prompts.py:10-15`）；`PromptBuilder.__init__` 维护 `self._sections: list[PromptSection]`（`mewcode/prompts.py:17-19`）；`PromptBuilder.add(section)` 返回 `PromptBuilder` 自身支持链式调用（`mewcode/prompts.py:21-23`）。

## T2: 实现 PromptBuilder.build 排序 + 拼接
- 影响文件: `mewcode/prompts.py:25-28`
- 依赖任务: T1
- 完成标准: `build()` 用 `self._sections.sort(key=lambda s: s.priority)` 按 priority 升序排（`mewcode/prompts.py:26`）；trim 后空 content 不进入 parts；用 `"\n\n".join(parts)` 输出最终文本（`mewcode/prompts.py:27-28`）。

## T3: 实现 environment_section 工厂函数
- 影响文件: `mewcode/prompts.py:147-154`
- 依赖任务: T1
- 完成标准: `environment_section(work_dir: str) -> PromptSection` 把 4 行 markdown 拼成 content（`# Environment` + Working directory + Platform + Date），用 `platform.system()` / `platform.release()` 拿 OS，用 `datetime.now().strftime('%Y-%m-%d')` 拿日期；返回 `PromptSection(name="Environment", priority=70, ...)`。

## T4: 实现 7 个固定文本 section 模块常量
- 影响文件: `mewcode/prompts.py:35-145`
- 依赖任务: T1
- 完成标准:
 - `IDENTITY_SECTION`（priority=0，`prompts.py:35-48`）—— MewCode 身份 + 安全 / URL 不乱造
 - `SYSTEM_SECTION`（priority=10，`prompts.py:50-61`）—— `<system-reminder>` 语义、prompt injection 警告、hook feedback、自动 compact
 - `DOING_TASKS_SECTION`（priority=20，`prompts.py:63-82`）—— 不做未读过的代码、最小修改原则、不写无用注释、报真实结果
 - `EXECUTING_ACTIONS_SECTION`（priority=30，`prompts.py:84-98`）—— 高破坏性操作需 confirm
 - `USING_TOOLS_SECTION`（priority=40，`prompts.py:100-116`）—— ReadFile/EditFile/WriteFile/Glob/Grep 优先 / 并行调用 / Agent / ToolSearch
 - `TONE_STYLE_SECTION`（priority=50，`prompts.py:118-127`）—— 不用 emoji / 简短 / 用 `file_path:line_number` / 工具调用前别打冒号
 - `TEXT_OUTPUT_SECTION`（priority=60，`prompts.py:129-145`）—— 输出文本一句话规划，少注释，end-of-turn summary

## T5: 实现 build_system_prompt 主入口
- 影响文件: `mewcode/prompts.py:233-274`
- 依赖任务: T2, T3, T4
- 完成标准: 签名 `build_system_prompt(hook_prompts, coordinator_mode, agent_catalog, custom_instructions, skill_section, memory_section, work_dir)`（`prompts.py:233-241`）；`coordinator_mode=True` 时委托给 `mewcode.teams.coordinator.get_coordinator_system_prompt`（`prompts.py:242-244`）；否则按 Identity→System→DoingTasks→ExecutingActions→UsingTools→ToneStyle→TextOutput→environment_section 顺序 Add 8 个固定 section（`prompts.py:246-254`）；依次按 `custom_instructions`（priority=80） / `skill_section`（priority=90） / `memory_section`（priority=95）按需 Add（`prompts.py:256-267`）；空字符串不 Add；`hook_prompts` 非空时尾部追加 `# Hook Injected Context\n` + `\n`.join（`prompts.py:271-272`）。

## T6: 实现 build_plan_mode_reminder 动态指令
- 影响文件: `mewcode/prompts.py:161-226`
- 依赖任务: 无
- 完成标准: `_PLAN_MODE_FULL_REMINDER`（`prompts.py:161-193`）+ `_PLAN_MODE_SPARSE_REMINDER`（`prompts.py:195-198`）+ `_REMINDER_INTERVAL = 5`（`prompts.py:200`）；`build_plan_mode_reminder(plan_path, plan_exists, iteration)` 根据 `plan_exists` 选择「文件已存在用 EditFile」或「文件不存在用 WriteFile」的 `plan_file_info`（`prompts.py:206-217`）；`iteration == 1` 返回完整版（`prompts.py:219-220`）；否则 `(iteration-1) // 5 % 5 == 0` 时再次发完整版，其余发稀疏版（`prompts.py:222-226`）。

## T7: 实现 build_environment_context 公共 API
- 影响文件: `mewcode/prompts.py:277-304`
- 依赖任务: 无
- 完成标准: `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`（`prompts.py:277-282`）输出 3 行基础信息（Current working directory / Operating system / Current time）+ 可选 agent_catalog + 可选 skill_catalog + 可选 `## Active Skills` 段（含每个 skill 的 `### Skill: name` + SOP）；最后用 `"\n".join(parts)` 输出。

## T8: 接入主流程（Agent.run / run_to_completion）
- 影响文件:
 - `mewcode/agent.py:399-402` `Agent.run` 启动时调 `build_environment_context` + `conversation.inject_environment`
 - `mewcode/agent.py:469-473` `Agent.run` 每轮迭代调 `build_system_prompt`
 - `mewcode/agent.py:480-484` Plan Mode 下调 `build_plan_mode_reminder` 并 `conversation.add_system_reminder`
 - `mewcode/agent.py:898-901` 自动 compact 后重新注入 environment
 - `mewcode/agent.py:918-921` `run_to_completion` 启动时也注入 environment
 - `mewcode/agent.py:935-938` `run_to_completion` 调 `build_system_prompt`
- 依赖任务: T1~T7
- 完成标准: Agent.run 主循环在 ModePlan 下每轮调 `build_plan_mode_reminder` 并写入 `conversation.add_system_reminder`，最终走 user 通道的 `<system-reminder>` 块；compact 触发后重新 inject env 与 long-term memory。

## T9: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T8
- 完成标准:
 - `ruff check mewcode/prompts.py` 无 lint 错误。
 - `pytest tests/test_agent.py -k "system_prompt or plan or environment"`：`tests/test_agent.py:482-503` 四个测试通过——`test_system_prompt_normal`（`build_system_prompt()` 含 "MewCode" 且不含 "Plan mode"）/ `test_system_prompt_plan`（`build_plan_mode_reminder("/tmp/plan.md", False, 1)` 含 "Plan mode" + "MUST NOT"）/ `test_plan_mode_sparse_reminder`（iteration=8 含 "Plan mode still active"）/ `test_environment_context`（含工作目录 + "Operating system" + "Current time"）。
 - `pytest tests/test_teams.py::test_coordinator_system_prompt`：`tests/test_teams.py:568-581` 三个测试覆盖 normal / coordinator_mode / plan_mode 组合。
 - `pytest tests/test_skills.py -k "environment_context"`：`tests/test_skills.py:530-548` 覆盖 active_skills 进入 environment_context 路径。

## 进度
- [ ] T1 PromptSection / PromptBuilder 数据结构
- [ ] T2 PromptBuilder.build 排序拼接
- [ ] T3 environment_section 工厂函数
- [ ] T4 7 个固定文本 section 常量
- [ ] T5 build_system_prompt 主入口
- [ ] T6 build_plan_mode_reminder 动态指令
- [ ] T7 build_environment_context 公共 API
- [ ] T8 Agent.run / run_to_completion 接入
- [ ] T9 端到端验证（ruff + 三组 pytest）
