# ch05: System Prompt 设计 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 数据结构 `@dataclass class PromptSection` 含 `name / priority / content` 三字段在 `mewcode/prompts.py:10-15`（`grep -n "class PromptSection" mewcode/prompts.py` 返回 1 条）
- [ ] 数据结构 `PromptBuilder` 在 `mewcode/prompts.py:17-28`，`__init__` 维护 `_sections: list[PromptSection]`（`mewcode/prompts.py:17-19`），`add` 返回 `PromptBuilder` 支持链式调用（`mewcode/prompts.py:21-23`），`build` 用 `sort(key=lambda s: s.priority)` 并 `"\n\n".join` 输出（`mewcode/prompts.py:25-28`）
- [ ] 函数 `environment_section(work_dir)` 在 `mewcode/prompts.py:147-154`，用 `platform.system()` + `platform.release()` + `datetime.now().strftime('%Y-%m-%d')`，返回 priority=70 的 PromptSection
- [ ] 函数 `build_system_prompt` 在 `mewcode/prompts.py:233-274`，按 8 段固定 + 3 段可选 + 1 段 hook 尾部顺序拼接
- [ ] 7 个固定文本 section 常量：`IDENTITY_SECTION`(prompts.py:35) / `SYSTEM_SECTION`(:50) / `DOING_TASKS_SECTION`(:63) / `EXECUTING_ACTIONS_SECTION`(:84) / `USING_TOOLS_SECTION`(:100) / `TONE_STYLE_SECTION`(:118) / `TEXT_OUTPUT_SECTION`(:129)
- [ ] Priority 数字固定：0/10/20/30/40/50/60/70，对应 7 固定 section + Environment（`grep -n "priority=" mewcode/prompts.py` 返回 ≥10 条覆盖 0/10/20/30/40/50/60/70/80/90/95）
- [ ] 可选 section priority 数字：80 / 90 / 95（CustomInstructions / Skills / Memory，`mewcode/prompts.py:259/264/267`）
- [ ] Plan Mode 动态指令：`build_plan_mode_reminder` 在 `mewcode/prompts.py:203-226`；`_REMINDER_INTERVAL = 5` 在 `mewcode/prompts.py:200`；`_PLAN_MODE_FULL_REMINDER` 在 `:161`；`_PLAN_MODE_SPARSE_REMINDER` 在 `:195`
- [ ] 函数 `build_environment_context` 在 `mewcode/prompts.py:277-304`，参数为 `work_dir, active_skills, skill_catalog, agent_catalog`
- [ ] 关键文本片段保留（输出含）：`IMPORTANT: Be careful not to introduce security` / `<system-reminder>` / `Only use emojis if the user explicitly requests it` / `file_path:line_number` / `Do not use a colon before tool calls`（`grep -n "Be careful not to introduce security\|<system-reminder>\|Only use emojis\|file_path:line_number\|colon before tool" mewcode/prompts.py` 返回 ≥5 条）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `git grep -n "build_system_prompt" origin/python -- '*.py'` 返回 ≥5 处真实调用（`mewcode/agent.py:469`、`mewcode/agent.py:935`、`tests/test_agent.py:483`、`tests/test_teams.py:569`、`tests/test_teams.py:574`、`tests/test_teams.py:581`）
- [ ] `git grep -n "build_environment_context" origin/python -- '*.py'` 返回 ≥5 处（`mewcode/agent.py:399`、`mewcode/agent.py:898`、`mewcode/agent.py:918`、`tests/test_agent.py:500`、`tests/test_skills.py:534`、`tests/test_skills.py:547`）
- [ ] `git grep -n "build_plan_mode_reminder" origin/python -- '*.py'` 返回 ≥3 处（`mewcode/agent.py:480`、`tests/test_agent.py:489`、`tests/test_agent.py:495`）
- [ ] Agent.run 调用链：`Agent.run` 启动 → `build_environment_context` (`mewcode/agent.py:399`) → `conversation.inject_environment` (`mewcode/agent.py:402`) → 每轮 `build_system_prompt` (`mewcode/agent.py:469`)
- [ ] Plan Mode 调用链：每轮迭代 → `mewcode/agent.py:478-484` 判断 `self.plan_mode` → 调 `build_plan_mode_reminder` → `conversation.add_system_reminder(plan_reminder)`
- [ ] Compact 后恢复链：`mewcode/agent.py:897-905` 自动 compact 触发后重新调 `build_environment_context` + `inject_environment` + `inject_long_term_memory`
- [ ] 已记录差异（不在本章 must-fix）:
 - [ ] Python 版本未实现 `BuildPlanModeReentryReminder` / `BuildPlanModeExitReminder`（`git grep -n "reentry\|exit_reminder" origin/python -- 'mewcode/prompts.py'` 返回 0 条）
 - 处理意见: Go 有但未接入 TUI，Python 直接省略；后续 ch+ 接入 `/do` 命令时再补。

## 3. 编译与测试
- [ ] `ruff check mewcode/prompts.py` 通过（无 lint 错误）
- [ ] `pytest tests/test_agent.py::test_system_prompt_normal` 通过；`tests/test_agent.py:482-485` 断言 `build_system_prompt()` 返回字符串含 `"MewCode"` 且不含 `"Plan mode"`
- [ ] `pytest tests/test_agent.py::test_system_prompt_plan` 通过；`tests/test_agent.py:488-491` 断言 `build_plan_mode_reminder("/tmp/plan.md", False, 1)` 含 `"Plan mode"` + `"MUST NOT"`
- [ ] `pytest tests/test_agent.py::test_plan_mode_sparse_reminder` 通过；`tests/test_agent.py:494-496` 断言 iteration=8 时含 `"Plan mode still active"`
- [ ] `pytest tests/test_agent.py::test_environment_context` 通过；`tests/test_agent.py:499-502` 断言含 `/home/user/project` + `"Operating system"` + `"Current time"`
- [ ] `pytest tests/test_teams.py -k "build_system_prompt or coordinator_system_prompt"` 通过；`tests/test_teams.py:568-581` 覆盖 normal / coordinator_mode=True / plan_mode=True 三种 build 路径

## 4. 端到端验证
- [ ] Agent 启动 → `Agent.run` 首次注入 environment（`mewcode/agent.py:399-402`） → 每轮 `build_system_prompt` 一次（`mewcode/agent.py:469`） → system 参数喂给 LLM 客户端（`mewcode/agent.py:935-938` 在 `run_to_completion` 路径同理）
- [ ] Plan Mode 验证：以 `--plan-mode` 启动 Agent → `mewcode/agent.py:478` 进入 plan_mode 分支 → 下一轮在 stream 之前注入 `<system-reminder>` 包裹的 5 阶段 Workflow（`mewcode/agent.py:483-484` + `mewcode/conversation.py` 的 `add_system_reminder`）
- [ ] Compact 恢复验证：触发自动 compact → `mewcode/agent.py:897-905` 重新 inject env + long-term memory → 下一轮 `build_system_prompt` 时上下文完整
- [ ] 留存证据: 在 Agent 输入 `/plan` 后看一次请求 body 中的 user `<system-reminder>` 内容；或在测试运行后通过 `pytest -v` 看到 4 个 ch05 测试 PASSED

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/python/ch05/`）
- [ ] commit 信息标注 `ch05` 与三件套关闭状态（待统一打包提交）
