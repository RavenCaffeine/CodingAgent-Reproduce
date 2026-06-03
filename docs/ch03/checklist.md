# ch03: 工具系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] `Tool` ABC 在 `mewcode/tools/base.py:22-45` 定义 `name`/`description`/`params_model`/`category`/`is_concurrency_safe`/`is_system_tool`/`should_defer` 七个类属性以及 `is_read_only` property、`get_schema()` 方法、`@abstractmethod async def execute()`（`git show origin/python:mewcode/tools/base.py | grep -n 'class Tool(ABC)'`）。
- [ ] `ToolResult` 在 `mewcode/tools/base.py:16-19` 以 `@dataclass` 定义 `output: str` + `is_error: bool = False`。
- [ ] `ToolCategory = Literal["read", "write", "command"]` 在 `mewcode/tools/base.py:13`（`grep -n 'ToolCategory' mewcode/tools/base.py`）。
- [ ] `SKIP_DIRS` 在 `mewcode/tools/base.py:9` 列出 `.git/.venv/node_modules/__pycache__/.tox/.mypy_cache` 六项（`grep -n 'SKIP_DIRS' mewcode/tools/base.py`）。
- [ ] `MAX_OUTPUT_CHARS = 10000` 在 `mewcode/tools/base.py:11` 作为全局结果上限。
- [ ] 流式事件 `TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd` 与 `StreamEvent` Union 集中在 `mewcode/tools/base.py:50-92`（`grep -c '^@dataclass' mewcode/tools/base.py` ≥ 8）。
- [ ] `ToolRegistry` 在 `mewcode/tools/__init__.py:11-123` 提供 `register` / `get` / `is_enabled` / `enable` / `disable` / `enable_all` / `mark_discovered` / `is_discovered` / `get_deferred_tool_names` / `search_deferred` / `find_deferred_by_names` / `list_tools` / `get_all_schemas` 共 13 个公开方法（`grep -nE 'def (register|get|is_enabled|enable|disable|enable_all|mark_discovered|is_discovered|get_deferred_tool_names|search_deferred|find_deferred_by_names|list_tools|get_all_schemas)' mewcode/tools/__init__.py`）。
- [ ] `get_all_schemas` 在 protocol == "openai" 时输出 `{type: "function", name, description, parameters}` 形状（`mewcode/tools/__init__.py:113-122`）。
- [ ] `create_default_registry` 在 `mewcode/tools/__init__.py:126-144` 一次性注册 6 个核心工具（`git show origin/python:mewcode/tools/__init__.py | grep -c 'registry.register'` == 6）。
- [ ] `ReadFile` 在 `mewcode/tools/read_file.py:20-51`，`name="ReadFile"`、`category="read"`、`is_concurrency_safe=True`、`offset` 默认 0、`limit` 默认 2000、行号 1-based `<line_no>\t<content>` 输出。
- [ ] `WriteFile` 在 `mewcode/tools/write_file.py:19-37`，`category="write"`、写前 `path.parent.mkdir(parents=True, exist_ok=True)`、成功输出 `Successfully wrote to <path>`。
- [ ] `EditFile` 在 `mewcode/tools/edit_file.py:20-56`，唯一性校验三分支 `count == 0 / 1 / N`，N>1 时报 `found N times, must be unique`。
- [ ] `Bash` 在 `mewcode/tools/bash.py:17-49`，`MAX_TIMEOUT = 600`、`asyncio.create_subprocess_shell` + `asyncio.wait_for(timeout)`、输出含 `STDOUT:` / `STDERR:` 段、超时返回 `Error: command timed out after Ns`、`is_error = (returncode != 0)`。
- [ ] `Glob` 在 `mewcode/tools/glob.py:15-38`，跳过 `SKIP_DIRS`、字典序输出相对路径、无匹配返回 `No files matched the pattern.`。
- [ ] `Grep` 在 `mewcode/tools/grep.py:17-55`，`re.compile` + `include` glob + 跳过 `SKIP_DIRS` + `<rel>:<line_num>:<line>` 输出、无匹配返回 `No matches found.`。
- [ ] `ToolSearchTool` 在 `mewcode/tools/impl/tool_search.py:19-83`，支持 `select:` 前缀与关键词两种查询；命中后逐个 `registry.mark_discovered`；未命中返回 `No matching deferred tools for "..."`。
- [ ] `AskUserTool` 在 `mewcode/tools/ask_user.py:37-75`，`should_defer = True`、`is_system_tool = True`、用 `asyncio.Future` 阻塞、`asyncio.wait_for(timeout=300)` 兜底；超时返回 `User did not respond within 5 minutes`。
- [ ] Tool 的 `params_model` 全用 Pydantic `BaseModel`，`get_schema()` 通过 `params_model.model_json_schema()` 自动出 JSON Schema（`grep -n 'model_json_schema' mewcode/tools/base.py mewcode/tools/impl/tool_search.py`）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `create_default_registry` 在 `mewcode/app.py:535` 与 `tests/test_agent.py:103,143,173,201,239` 中被调用（`git grep -n 'create_default_registry' origin/python -- 'mewcode/**' 'tests/**'` 命中 ≥ 6 处）。
- [ ] `ToolSearchTool` 在 `mewcode/app.py:644-645` 被注册并消费 `provider.protocol`（`git grep -n 'ToolSearchTool(' origin/python -- 'mewcode/**'`）。
- [ ] `AskUserTool` 在 `mewcode/app.py:647` 被注册，并由 `app.py:1163` 的 `_pending_event` 状态机消费（`git grep -n 'AskUserTool\|_pending_event' origin/python -- 'mewcode/app.py'`）。
- [ ] `ToolRegistry.get_all_schemas` 接入点在 `mewcode/agent.py:500` 与 `mewcode/agent.py:940`（`git grep -n 'get_all_schemas' origin/python -- 'mewcode/agent.py'`）。
- [ ] `ToolRegistry.get` 接入点在 `mewcode/agent.py:745` 与 `mewcode/agent.py:791`，`tool.execute` 调用点在 `mewcode/agent.py:767`，证明工具执行接进 Agent Loop。
- [ ] `get_deferred_tool_names` 在 `mewcode/agent.py:491` 与 `mewcode/agent.py:972` 被消费用于拼 system reminder（`git grep -n 'get_deferred_tool_names' origin/python -- 'mewcode/**'`）。
- [ ] `partition_tool_calls` 在 `mewcode/agent.py:218-232` 用 `tool.is_concurrency_safe` 把 ReadFile / Glob / Grep 等只读工具并发分批（`git show origin/python:mewcode/agent.py | sed -n '218,232p'`）。
- [ ] `ToolRegistry` 被 `mewcode/mcp/manager.py:22`、`mewcode/agents/tool_filter.py:121,178,189`、`mewcode/skills/executor.py:31` 等下游模块用作工具容器（`git grep -n 'ToolRegistry()' origin/python -- 'mewcode/**'`）。
- [ ] `StreamEvent` / `ToolCallComplete` / `TextDelta` 等流式事件类型被 `mewcode/agent.py:33-` 与 `mewcode/client.py` 共享（`git grep -n 'from mewcode.tools.base import' origin/python -- 'mewcode/**'`）。

## 3. 编译与测试

- [ ] `python -m compileall mewcode` 通过，无 SyntaxError。
- [ ] `ruff check mewcode/tools/` 无报错。
- [ ] `pytest tests/test_tool_search.py -q` 通过（`git show origin/python:tests/test_tool_search.py | grep -c '^def test_\|^async def test_'` ≥ 6 个测试用例）。
- [ ] `pytest tests/test_agent.py -q` 通过（其中 `test_single_step_tool_call` / `test_multi_step_autonomous` 用 `create_default_registry()` 验证 ReadFile / WriteFile 接通）。

## 4. 端到端验证

- [ ] 在 TUI 输入 `请读取 /Users/codemelo/mewcode/README.md`，Agent 调用 `ReadFile`，对话区显示带行号的内容如 `1\t# MewCode`（验证 ReadFile 接通）。
- [ ] 在 TUI 输入 `跑 ls -la /tmp`，Agent 调用 `Bash`，对话区显示 `STDOUT:` + 文件列表（验证 Bash 接通）。
- [ ] 在 TUI 输入 `搜代码里所有 async def execute`，Agent 调用 `Grep` 并返回 `<file>:<line>:<line content>` 命中（验证 Grep 接通）。
- [ ] 在 TUI 中触发 `AskUserQuestion`（如要求 Agent 让用户选某选项），TUI 弹出问题对话框，选完答案后 Agent 继续（验证 AskUserTool 通过 `_pending_event` + `asyncio.Future` 接通）。
- [ ] 留存证据: `tests/test_agent.py::test_single_step_tool_call`（line 88-118）、`::test_multi_step_autonomous`（line 122-160）这类用 `create_default_registry()` 装配 + `ReadFile/WriteFile` 端到端的测试通过即说明工具能被 Agent Loop 跑起来。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`/Users/codemelo/mewcode/docs/python/ch03/`）。
- [ ] commit 信息标注 ch03 与三件套关闭状态（如 `docs(python/ch01-03): course spec/tasks/checklist`）。
