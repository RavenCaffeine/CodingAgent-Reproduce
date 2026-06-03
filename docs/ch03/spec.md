# ch03: 工具系统 Spec

## 1. 背景

ch02 让 LLM 能说话，但 LLM 只是文字流；Coding Agent 真正能在仓库里干活靠的是「函数调用」。ch03 在 `mewcode/tools/` 落地 Function Calling 三步循环所需的全部抽象：统一的 `Tool` ABC 基类、`ToolRegistry` 注册中心、可序列化成 Anthropic 与 OpenAI 两种协议的 schema、流式 tool_use 事件类型，以及 6 个核心工具。没有这一章，ch04 Agent Loop 收到工具调用事件后无法把工具名映射到具体执行器，所有后续工具（ToolSearch、AskUserQuestion、Skill、Worktree、Team、SubAgent、MCP wrapper）也无处挂靠。

## 2. 目标

交付 `tools.Tool` ABC 与 `tools.ToolRegistry` 注册中心，统一所有工具的 name / description / params_model / category / execute 五段式契约；交付 `create_default_registry()` 一次性注册 6 个核心工具（ReadFile / WriteFile / EditFile / Bash / Glob / Grep）；以 Pydantic `BaseModel` 直接生成 JSON Schema，省去手写两遍 schema 的负担；交付 deferred 工具协议 + `ToolSearchTool` 做渐进式工具披露。给 ch04 Agent Loop、ch07 MCP、ch11 Skill `allowedTools`、ch13 SubAgent 工具过滤、ch15 Team 等下游使用。

## 3. 功能需求

- F1: 提供统一的工具返回值 `ToolResult(output: str, is_error: bool = False)`，所有工具结果都用同一形状回灌会话。
- F2: 提供 `ToolCategory = Literal["read", "write", "command"]`，用于权限分类与并行批次划分（read-only + `is_concurrency_safe` 工具可并发，write / command 串行）。
- F3: 定义 `Tool` ABC 基类，声明 `name` / `description` / `params_model` / `category` / `execute` 字段与方法，所有内置 / MCP / Skill / Team 工具继承该类。`params_model` 用 Pydantic `BaseModel` 描述参数；`get_schema()` 直接 `model_json_schema()` 出 JSON Schema。
- F4: 提供 `should_defer: bool` 类属性，允许工具声明「初次请求时不进 schema 列表，由 ToolSearch 按需取出」，供 ch07 MCP / ch15 Team / `AskUserQuestion` 等专用工具采用。
- F5: 提供 `ToolRegistry`，支持 `register` / `get` / `list_tools` 三种基础操作，外加 `enable` / `disable` / `is_enabled` 控制启停、`mark_discovered` / `is_discovered` 跟踪 deferred 工具是否已被披露。
- F6: 提供按协议导出工具 schema 的能力：`get_all_schemas(protocol)` 默认输出 Anthropic 形状 `{name, description, input_schema}`，遇到 `protocol == "openai"` 时在边缘转成 `{type: "function", name, description, parameters}`；deferred 且未 discovered 的工具默认不出现在 schema 列表里。
- F7: 提供 deferred 工具的两种查询入口：按名精确选（`search.find_deferred_by_names`，对应 `select:Name1,Name2`）与按关键词搜（`search.search_deferred`，在 name / description 中匹配并打分）；命中后自动 `mark_discovered`。
- F8: 提供 `create_default_registry(file_cache)` 工厂，一次性把 6 个核心工具注册好；TUI 装配阶段直接拿到可用 Registry。
- F9: ReadFile 工具：读文本文件并按行号输出 `<line_no>\t<content>`（1-based）；处理文件不存在 / 路径不是文件两类边界；支持 `offset` / `limit` 切片；命中 `FileCache` 时跳过实际 IO。
- F10: WriteFile 工具：写入指定路径，目录不存在时 `path.parent.mkdir(parents=True, exist_ok=True)` 自动创建中间目录；写完后 `FileCache.invalidate`。
- F11: EditFile 工具：基于 `old_string` 唯一匹配在文件里做一次性替换；处理「文件不存在」「未匹配」「匹配多次」三类边界，命中后 `FileCache.invalidate`。
- F12: Bash 工具：通过 `asyncio.create_subprocess_shell` 启动子进程，捕获 stdout / stderr / 退出码；用 `asyncio.wait_for(timeout)` 控制超时，超时与非零退出区分清楚（is_error 区分）。
- F13: Glob 工具：用 `Path.glob(pattern)` 递归匹配文件名，结果按字典序输出相对路径；跳过 `SKIP_DIRS` 子树；无匹配时返回 `No files matched the pattern.`。
- F14: Grep 工具：`re.compile` + `include` basename glob 过滤 + 逐行扫描，输出 `<rel>:<line_num>:<line>`；遇到非法正则返回结构化错误；跳过 `SKIP_DIRS` 子树；无匹配时返回 `No matches found.`。
- F15: ToolSearch 工具：把 deferred 工具按 `select:` / 关键词两种形态暴露给模型；命中后 `mark_discovered`；未命中时回退到列出全部 deferred 工具名。
- F16: AskUserQuestion 工具（deferred）：把结构化问题经 `asyncio.Future` 交给 TUI 渲染，阻塞等待用户回应；带 5 分钟超时；TUI 通过 `_pending_event` 读取问题并 `set_result` 解阻塞。
- F17: 流式事件类型 `TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd` 集中定义在 `mewcode/tools/base.py`，便于 LLM client 与 Agent Loop 共享类型。
- F18: 提供单工具结果上限常量 `MAX_OUTPUT_CHARS = 10000`，由 ch04 / ch08 在回灌会话前据此截断或落盘，避免单工具撑爆下一轮上下文。

## 4. 非功能需求

- N1: Tool 子类无内部状态约束：核心工具大多用零参 `__init__` 即可注册；带可选注入（FileCache）的工具靠依赖注入实现可测试。
- N2: ToolRegistry 非并发安全——只允许在装配阶段写入，运行期只读；MCP / SubAgent / Skill 等都在 `App._init_after_login` 阶段一次性注册完毕。
- N3: 工具实现不允许依赖上层模块（agent / skills / teams），`mewcode/tools/` 处于底层；只能用 `mewcode.cache.FileCache`（typing-only）做可选注入。
- N4: 工具 `execute` 是 `async` 方法，必须能响应 `asyncio.CancelledError` 并尽快退出；长命令（Bash）依赖 `asyncio.wait_for` 超时机制兜底。
- N5: Schema 形态稳定：以 Anthropic 形状为基础（Pydantic `model_json_schema()` 直接出形），OpenAI 形状在 `ToolRegistry.get_all_schemas` 边缘做形状转换，避免每个 Tool 各写两份 schema。

## 5. 设计概要

- 核心数据结构: `Tool` ABC、`ToolResult` dataclass、`ToolCategory` Literal、`SKIP_DIRS` set、`MAX_OUTPUT_CHARS` int、`ToolRegistry` 类、6 个核心工具子类、`ToolSearchTool`（持有 Registry + Protocol）、`AskUserTool`（持 `_pending_event: AskUserEvent | None`）。
- 主流程（一次工具调用从 LLM 到磁盘）:
 1. Agent Loop 收到 `ToolCallComplete`；
 2. 通过 `ToolRegistry.get(name)` 找到工具，未知 / disabled 工具回灌结构化错误；
 3. 走权限检查（ch06）；
 4. 走 `pre_tool_use` hook（ch12）；
 5. `params = tool.params_model.model_validate(tc.arguments)` 做 Pydantic 校验；
 6. `result = await tool.execute(params)`；
 7. 走 `post_tool_use` hook，结果按 `MAX_OUTPUT_CHARS` 截断后落 tool_result。
- 调用链:
 - 装配: `App._init_after_login` → `create_default_registry(file_cache)` → 追加 `LoadSkill` / `ToolSearchTool` / `AskUserTool` / `EnterWorktreeTool` / `ExitWorktreeTool` / `AgentTool` / `team_create_tool` / `team_delete_tool`；MCP ready 时把 MCP 工具也注册进来。
 - Schema 导出: Agent Loop 每轮取 `registry.get_all_schemas(protocol)` 传给 `LLMClient.stream`。
 - 执行: Agent Loop 内 `_execute_single_tool_direct` 统一通过 `registry.get` + Pydantic 校验 + `await tool.execute` 调用。
 - 并发批次: `partition_tool_calls` 按 `tool.is_concurrency_safe` 把连续的并发安全调用归到同一批。
- 与其他模块的交互:
 - 被依赖: `mewcode/agent.py`（取 schema、查工具、执行）、`mewcode/app.py`（创建并注册）、`mewcode/mcp/manager.py`（注册 MCP 工具 wrapper）、`mewcode/agents/tool_filter.py`（SubAgent 工具过滤复制 Registry）、`mewcode/skills/executor.py`（用 `allowedTools` 拷贝过滤的 Registry）、`mewcode/hooks/`（按 ToolName 覆盖）。
 - 依赖: 仅 Python 标准库（asyncio / pathlib / re）+ Pydantic（用于 params_model 出 JSON Schema），不依赖任何上层模块。

## 6. Out of Scope

- 工具描述自适应（例如 Bash 描述根据当前 sandbox 模式动态生成）：当前所有描述都是类属性常量。
- 文件读取的图片 / PDF / Notebook 解析：本章只支持文本 + 行号输出。
- EditFile 的 `replace_all` 选项：当前要求 `old_string` 唯一。
- Bash 危险命令静态校验：放到 ch06 权限系统。
- Bash 后台任务 / Sandbox 模式 / sed-edit 解析：不在 ch03 范围。
- 工具输出大结果存盘（spillover）：放到 ch08 `mewcode/context/`。
- 细化的工具元数据（isReadOnly / isDestructive / maxResultSizeChars 等）：当前用 `ToolCategory` + `is_concurrency_safe` + 全局 `MAX_OUTPUT_CHARS` 简化表达，细化留给后续章节。
- 协议层的 `cache_control` / `prompt caching`：放到 ch04 / ch08。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
