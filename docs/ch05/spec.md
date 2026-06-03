# ch05: System Prompt 设计 Spec

## 1. 背景

没有 System Prompt，模型并不知道自己叫 MewCode、不知道运行在什么 OS、不知道有哪些工具能用、不知道用户的代码规范，输出会落到「通用 ChatGPT 助手」基线。所有静态规则（语气、安全、工具使用规范）和环境信息必须固化到 System Prompt 才能让模型回答稳定、可预期；动态指令（Plan Mode reminder、Hook 注入、Skill 拉取）则走 user channel 的 `<system-reminder>` 块或 `inject_environment`，避免反复改 System 失效缓存。本章把这条 prompt 拼接管线做出来。

## 2. 目标

对外提供 `mewcode.prompts`：调用者准备好工作目录、（可选）custom_instructions / skill_section / memory_section / hook_prompts，调一次 `build_system_prompt(...)` 拿到能直接喂给 LLM 客户端的纯文本 System Prompt。多个信息来源（角色、行为准则、工具规范、tone、文本输出风格、环境上下文、项目说明、Skill 摘要、Memory）按优先级合并；环境上下文由 `build_environment_context` 单独构造并通过 `ConversationManager.inject_environment` 注入 user channel；动态注入走 `ConversationManager.add_system_reminder` + ch04 主循环。

## 3. 功能需求

- F1: 提供 `environment_section(work_dir)` 构造环境 section，输出工作目录、`platform.system()` / `platform.release()`、`datetime.now().strftime('%Y-%m-%d')` 字段，作为 System Prompt 的 priority=70 段。
- F2: 提供 `build_system_prompt(hook_prompts, coordinator_mode, agent_catalog, custom_instructions, skill_section, memory_section, work_dir)` 主入口，装配 8 个固定 section（Identity / System / DoingTasks / ExecutingActions / UsingTools / ToneStyle / TextOutput / Environment）外加 3 个可选 section（CustomInstructions / Skills / Memory），按优先级排序后拼接。
- F3: `build_system_prompt` 接收 `custom_instructions` / `skill_section` / `memory_section` 三类可选字符串；空字符串不进入最终输出；`hook_prompts` 非空时尾部追加 `# Hook Injected Context` 段。
- F4: 提供 `PromptBuilder` + `PromptSection` 数据类支持自定义扩展：调用者可空 builder 起步、自由 `add(...)` section、指定 priority，最后 `build()` 排序输出；`add` 返回 `self` 支持链式调用。
- F5: 各 section 有固定优先级（Identity=0、System=10、DoingTasks=20、ExecutingActions=30、UsingTools=40、ToneStyle=50、TextOutput=60、Environment=70，CustomInstructions=80、Skills=90、Memory=95），保证最终 prompt 顺序稳定。
- F6: Plan Mode 系统提醒不进入 System Prompt，由 `build_plan_mode_reminder(plan_path, plan_exists, iteration)` 构造，由 ch04 主循环通过 `conversation.add_system_reminder(...)` 注入 user channel，最终包成 `<system-reminder>` user 消息。
- F7: 各 section 文案需保持与终端 Agent 系统提示语义一致：禁用 emoji、优先用专用工具（ReadFile/EditFile/WriteFile/Glob/Grep）、文件路径引用用 `file_path:line_number`、状态报告诚实、对潜在 prompt injection 进行 flag、`<system-reminder>` 与具体 tool 结果无直接关系、tool 调用前别打冒号等关键短语保留。
- F8: 提供 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)` 单独构造 user 通道环境块（工作目录、操作系统、时间，含 skill_catalog / agent_catalog / Active Skills），供 `conversation.inject_environment` 使用，与 System Prompt 中的 Environment section 互补。

## 4. 非功能需求

- N1: System Prompt 内容必须能被 LLM 长缓存命中——只在切 provider / 工作目录 / Skill / Memory 时重建，每轮迭代不重新构建（每轮调一次是当前实现，可后续做缓存）。
- N2: 环境探测在缺失字段时静默降级（Python 的 `platform.system()` 在容器或不识别 OS 时仍返回字符串，不抛异常）。
- N3: 日期字段使用稳定格式（`%Y-%m-%d`），跨进程一致。
- N4: section 之间用恰好两个换行分隔，section 内部用单换行；trim 后为空的 section 不出现在输出里。
- N5: 文案不使用 emoji（除非用户在 ToneStyle section 内显式说明）。

## 5. 设计概要

- 核心数据结构: `PromptSection(name: str, priority: int, content: str)` dataclass（`mewcode/prompts.py:10-15`）、`PromptBuilder._sections: list[PromptSection]`（`mewcode/prompts.py:17-28`），无独立 `EnvironmentContext` 类，环境字段直接由 `environment_section(work_dir)` 渲染。
- 主流程:
 1. Agent.run 启动 → 调 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)` 并通过 `conversation.inject_environment(env_context)` 注入 user channel；
 2. 每轮迭代前调 `build_system_prompt(hook_prompts=..., coordinator_mode=..., agent_catalog=...)` 拼出 system prompt；
 3. system prompt 作为 `system` 参数传给 LLM 客户端（Anthropic / OpenAI 等）；
 4. Plan Mode 下额外调 `build_plan_mode_reminder(plan_path, plan_exists, iteration)` 并 `conversation.add_system_reminder(plan_reminder)`。
 - `build_system_prompt` 内部依次添加 8 固定 section + 3 可选 section，最后 `PromptBuilder.build()` 排序拼接。
- 调用链:
 - `mewcode/agent.py:469` `Agent.run` 主循环每轮迭代调 `build_system_prompt`。
 - `mewcode/agent.py:935` `Agent.run_to_completion` 单轮入口也调 `build_system_prompt`。
 - `mewcode/agent.py:399` 和 `:898` `:918` 三处调 `build_environment_context`（启动、压缩后、run_to_completion）。
 - `mewcode/agent.py:480` Plan Mode 下调 `build_plan_mode_reminder`。
 - `tests/test_agent.py:483/489/495/500` 四个单测覆盖 normal / plan / sparse / environment 四种路径。
- 与其他模块的交互:
 - 依赖 Python 标准库（`platform` / `datetime` / `dataclasses`），coordinator_mode 时动态导入 `mewcode.teams.coordinator`。
 - 被 `mewcode.agent`（构造 prompt、注入 environment、Plan Mode reminder）使用。
 - 输入数据由 `mewcode.memory` / `mewcode.skills` / `mewcode.teams` 等模块准备好后传入。

## 6. Out of Scope

- Coordinator Mode 的 system prompt 替换分支已实现（`build_system_prompt` 内 `coordinator_mode=True` 委托给 `mewcode.teams.coordinator.get_coordinator_system_prompt`），但 coordinator 角色专有规则不在本章 spec 范围内详述。
- 不缓存 section 输出。
- Plan Mode Reentry / Exit 提醒函数 Python 版本未实现（Go 有 `BuildPlanModeReentryReminder` / `BuildPlanModeExitReminder` 死代码，Python 直接跳过这两个函数）。
- 不实现外部 `--system-prompt` / `appendSystemPrompt` CLI 参数。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
