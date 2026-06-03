# ch03: 工具系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `Tool` ABC 与 `ToolResult` / `ToolCategory`
- 影响文件: `mewcode/tools/base.py`
- 依赖任务: 无
- 完成标准:
 - `mewcode/tools/base.py:9` 定义 `SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}`；
 - `mewcode/tools/base.py:11` 定义 `MAX_OUTPUT_CHARS = 10000`；
 - `mewcode/tools/base.py:13` 定义 `ToolCategory = Literal["read", "write", "command"]`；
 - `mewcode/tools/base.py:16-19` 定义 `@dataclass ToolResult(output: str, is_error: bool = False)`；
 - `mewcode/tools/base.py:22-45` 定义 `Tool(ABC)`：类属性 `name`/`description`/`params_model`/`category`/`is_concurrency_safe`/`is_system_tool`/`should_defer`，`is_read_only` property、`get_schema()` 方法、`@abstractmethod async def execute()`。

## T2: 定义流式事件类型
- 影响文件: `mewcode/tools/base.py`
- 依赖任务: T1
- 完成标准: `mewcode/tools/base.py:50-92` 定义 7 个 dataclass：`TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd`，以及 `StreamEvent` Union 别名。`ToolCallComplete` 必含 `tool_id` / `tool_name` / `arguments: dict[str, Any]`。

## T3: 实现 `ToolRegistry` 与 schema 转换
- 影响文件: `mewcode/tools/__init__.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/__init__.py:11-39` 实现 `ToolRegistry.__init__` + `register` / `get` / `is_enabled` / `enable` / `disable` / `enable_all` / `mark_discovered` / `is_discovered`；
 - `mewcode/tools/__init__.py:41-48` 实现 `get_deferred_tool_names`：返回 `should_defer=True` 且未 discovered 且未 disabled 的工具名；
 - `mewcode/tools/__init__.py:50-79` 实现 `search_deferred(query, max_results, protocol)`：在 name / description 中按词打分（`name in name_lower` +10，`name in desc_lower` +5，分词 +3 / +1），按分数倒序裁剪 max_results；
 - `mewcode/tools/__init__.py:81-101` 实现 `find_deferred_by_names(names, protocol)`：仅返回 deferred 工具的 schema；
 - `mewcode/tools/__init__.py:103-104` 实现 `list_tools`；
 - `mewcode/tools/__init__.py:106-123` 实现 `get_all_schemas(protocol)`：跳过 disabled 与未 discovered 的 deferred，protocol=="openai" 时输出 `{type: "function", name, description, parameters}`。

## T4: 实现 ReadFile 工具
- 影响文件: `mewcode/tools/read_file.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/read_file.py:14-17` 定义 `Params(file_path, offset=0, limit=2000)`；
 - `mewcode/tools/read_file.py:20-51` 实现 `ReadFile`：`name="ReadFile"`、`category="read"`、`is_concurrency_safe=True`；处理文件不存在 / 不是文件两类错误；`offset` / `limit` 切片后输出 `f"{i + offset + 1}\t{line}"`；如注入了 `FileCache` 走缓存。

## T5: 实现 WriteFile 工具
- 影响文件: `mewcode/tools/write_file.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/write_file.py:14-16` 定义 `Params(file_path, content)`；
 - `mewcode/tools/write_file.py:19-37` 实现 `WriteFile`：`category="write"`；写前 `path.parent.mkdir(parents=True, exist_ok=True)`；写入后 `FileCache.invalidate`；成功输出 `Successfully wrote to <path>`。

## T6: 实现 EditFile 工具
- 影响文件: `mewcode/tools/edit_file.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/edit_file.py:14-17` 定义 `Params(file_path, old_string, new_string)`；
 - `mewcode/tools/edit_file.py:20-56` 实现 `EditFile`：`category="write"`；唯一性校验三分支：`count == 0` → `old_string not found`，`count > 1` → `found N times, must be unique`，`count == 1` → `content.replace(..., 1)` 写回；命中 `FileCache.invalidate`。

## T7: 实现 Bash 工具
- 影响文件: `mewcode/tools/bash.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/bash.py:9` 定义 `MAX_TIMEOUT = 600`；
 - `mewcode/tools/bash.py:12-14` 定义 `Params(command, timeout=120)`；
 - `mewcode/tools/bash.py:17-49` 实现 `Bash`：`category="command"`；`asyncio.create_subprocess_shell` + `asyncio.wait_for(timeout=min(params.timeout, MAX_TIMEOUT))`；输出含 `STDOUT:` / `STDERR:` 两段或 `(no output)`；超时输出 `Error: command timed out after Ns`；`is_error = (returncode != 0)`。

## T8: 实现 Glob 工具
- 影响文件: `mewcode/tools/glob.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/glob.py:10-12` 定义 `Params(pattern, path=".")`；
 - `mewcode/tools/glob.py:15-38` 实现 `Glob`：`is_concurrency_safe=True`；`base.glob(params.pattern)` + 过滤 `SKIP_DIRS` + 仅文件 + 字典序输出相对路径；空结果输出 `No files matched the pattern.`。

## T9: 实现 Grep 工具
- 影响文件: `mewcode/tools/grep.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/grep.py:11-14` 定义 `Params(pattern, path=".", include="")`；
 - `mewcode/tools/grep.py:17-55` 实现 `Grep`：`is_concurrency_safe=True`；`re.compile` 捕获 `re.error`；`include` 拼成 `**/<include>` glob；逐行 `regex.search` 后输出 `f"{rel}:{line_num}:{line}"`；跳过 `SKIP_DIRS` 与无法读取的文件；空结果输出 `No matches found.`。

## T10: 实现 ToolSearch 工具与 deferred 协议
- 影响文件: `mewcode/tools/impl/__init__.py`、`mewcode/tools/impl/tool_search.py`
- 依赖任务: T3
- 完成标准:
 - `mewcode/tools/impl/tool_search.py:14-16` 定义 `ToolSearchParams(query, max_results=5)`；
 - `mewcode/tools/impl/tool_search.py:19-46` 定义 `ToolSearchTool`：持有 `registry` / `protocol`；自定义 `get_schema()` 以 strip title；`should_defer = False`（自身从不 defer）；
 - `mewcode/tools/impl/tool_search.py:48-80` 实现 `execute`：`select:` 前缀走 `find_deferred_by_names`，否则走 `search_deferred`；未命中返回 `No matching deferred tools for "<q>". Available: <names>`；命中后逐个 `registry.mark_discovered(s["name"])`，输出 `Found N tool(s)...` + JSON 序列化的 schema。

## T11: 实现 AskUserQuestion 工具
- 影响文件: `mewcode/tools/ask_user.py`
- 依赖任务: T1
- 完成标准:
 - `mewcode/tools/ask_user.py:11-18` 定义 `QuestionItem(type, name, message, options)`；
 - `mewcode/tools/ask_user.py:21-24` 定义 `AskUserParams(questions: list[QuestionItem])`；
 - `mewcode/tools/ask_user.py:27-34` 定义 `AskUserEvent(questions, future)`；
 - `mewcode/tools/ask_user.py:37-75` 实现 `AskUserTool`：`should_defer = True`、`is_system_tool = True`；`execute` 创建 `asyncio.Future`、写 `self._pending_event`、`asyncio.wait_for(future, timeout=300)`；超时返回 `User did not respond within 5 minutes`；最终输出 `{q.name}: {answer}` 多行。

## T12: 拼装 `create_default_registry`
- 影响文件: `mewcode/tools/__init__.py`
- 依赖任务: T4, T5, T6, T7, T8, T9
- 完成标准: `mewcode/tools/__init__.py:126-144` 实现 `create_default_registry(file_cache=None) -> ToolRegistry`：在函数体内 lazy import 6 个工具类，逐个 `registry.register(...)`，ReadFile / WriteFile / EditFile 传入 `file_cache`。

## T13: 接入主流程
- 影响文件: `mewcode/app.py`、`mewcode/agent.py`
- 依赖任务: T10, T11, T12
- 完成标准:
 - `mewcode/app.py:77` `from mewcode.tools import ToolRegistry, create_default_registry`；
 - `mewcode/app.py:535` `self.registry: ToolRegistry = create_default_registry(file_cache=self.file_cache)`；
 - `mewcode/app.py:644-645` `self.registry.register(ToolSearchTool(self.registry, protocol=provider.protocol))`；
 - `mewcode/app.py:647` `self.registry.register(AskUserTool())`；
 - `mewcode/agent.py:33` `from mewcode.tools import ToolRegistry`；
 - `mewcode/agent.py:218-232` `partition_tool_calls` 用 `tool.is_concurrency_safe` 分批；
 - `mewcode/agent.py:500` `tools = self.registry.get_all_schemas(self.protocol)` 取 schema；
 - `mewcode/agent.py:491` `deferred_names = self.registry.get_deferred_tool_names()` 拼 system reminder；
 - `mewcode/agent.py:745` `tool = self.registry.get(tc.tool_name)`；
 - `mewcode/agent.py:767` `params = tool.params_model.model_validate(tc.arguments)` + `result = await tool.execute(params)`。

## T14: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T13
- 完成标准:
 - `python -m compileall mewcode` 通过；
 - `ruff check mewcode/tools/` 无报错；
 - `pytest tests/test_tool_search.py -q` 全部通过；
 - `pytest tests/test_agent.py::test_single_step_tool_call tests/test_agent.py::test_multi_step_autonomous -q` 通过（用 `create_default_registry()` + MockLLMClient 跑 ReadFile/WriteFile 端到端）；
 - 在 TUI 输入 `请读取 README.md 并告诉我前 5 行`，Agent 会调用 `ReadFile`，对话区返回带行号的文本（如 `1\t# MewCode`）；
 - 在 TUI 输入 `跑一下 ls -la`，Agent 会调用 `Bash`，对话区输出含 `STDOUT:` 段；
 - 留存证据: 任一后续章节（ch04-ch15）能正常工作本身就说明工具系统接通。

## 进度
- [x] T1
- [x] T2
- [x] T3
- [x] T4
- [x] T5
- [x] T6
- [x] T7
- [x] T8
- [x] T9
- [x] T10
- [x] T11
- [x] T12
- [x] T13
- [x] T14
