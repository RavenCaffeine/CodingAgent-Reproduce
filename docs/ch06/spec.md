# ch06: 权限系统 Spec

## 1. 背景

工具系统（ch03）开放了 Bash 和写文件能力，Agent Loop（ch04）允许模型自主决策调谁。没有权限层，模型一句话就能 `rm -rf /` 或者写到项目目录之外。一个生产级 Coding Agent 的最低安全门槛是“拦住明显危险的操作 + 把不熟悉的操作交给用户决定”。本章把这条防御线做出来：明显错的直接拦、明显对的直接放，剩下的让规则 / 模式 / HITL 来决定。

## 2. 目标

对外提供 `mewcode.permissions.PermissionChecker`：调用者构造好 `DangerousCommandDetector` + `PathSandbox` + `RuleEngine` + `PermissionMode`，对任意 `mewcode.tools.base.Tool` + `arguments` 调一次 `check(...)`，拿回 `Decision(effect, reason)`。Agent Loop 根据这个 `Decision` 决定直接执行 / 直接拒绝 / 走 HITL（产出 `PermissionRequest` 事件，由 TUI 渲染并交还 `PermissionResponse`）。权限模式覆盖 default / acceptEdits / plan / bypassPermissions / custom / dontAsk 六种，TUI 用 Shift+Tab 或 `/mode` 切换；Plan 模式拥有特殊豁免分支。HITL 用户选「Allow Always」时把规则 append 到本地 YAML 规则文件。

## 3. 功能需求

- F1: 提供权限模式枚举 `PermissionMode`（default / acceptEdits / plan / bypassPermissions / custom / dontAsk）与模式 × 工具类别（`read` / `write` / `command`）的决策矩阵 `_MODE_MATRIX`，对外暴露 `mode_decide(mode, category)` 查矩阵。
- F2: Layer 1 危险命令检测：`DangerousCommandDetector.detect(command)` 用硬编码的 8 条正则覆盖 `rm -rf /` 类删除、磁盘格式化、`dd if=...of=/dev/`、`chmod -R 777 /`、fork bomb、`curl | sh` / `wget | sh` 远程执行、`> /dev/sd` 设备写入。命中即返回 `(True, reason)`，调用方据此拒绝。
- F3: Layer 1 安全命令白名单：维护 `_SAFE_COMMANDS` 只读 Bash 命令前缀集合（`ls` / `pwd` / `cat` / `git status` / `git diff` / `go version` / `npm -v` 等）；`is_safe_command(command)` 检查命令前缀且不含 `|` / `;` / `&&` / `>` / `$(` / 反引号时直接放行。
- F4: Layer 2 路径沙箱 `PathSandbox`：构造时把 `project_root` + `tempfile.gettempdir()` + `extra_allowed` 全部 `Path.resolve()` 后存入 `_allowed_roots`；`check(path)` 对入参做 `expanduser` + 绝对化 + `resolve(strict=True)`（解析 symlink）后逐 root 做 `Path.relative_to` 判定；如果路径不存在则回退到对父目录做 `resolve` 再拼接，支持新文件的预检。
- F5: Layer 3 规则引擎 `RuleEngine`：管理 user / project / local 三层 YAML 规则文件，路径优先级 user < project < local（local 覆盖 project 覆盖 user）；单文件内按 LIFO 匹配；`Rule(tool_name, pattern, effect)` 用 `fnmatch` 做 glob 匹配主参数；`evaluate(tool_name, content)` 命中返回 `"allow"` / `"deny"`，未命中返回 `None`；`append_local_rule(rule)` 写回本地规则文件。
- F6: 规则语法 `ToolName(pattern)` 用 `_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")` 解析；`effect` 仅允许 `"allow"` / `"deny"`；YAML 列表结构 `[{rule: ..., effect: ...}, ...]`。
- F7: 内容字段提取 `extract_content(tool_name, arguments)`：`_CONTENT_FIELDS` 表把六个核心工具的「主参数字段」映射出来（Bash → `command`、ReadFile / WriteFile / EditFile → `file_path`、Glob / Grep → `pattern`），未识别工具返回空字符串。
- F8: `PermissionChecker.check(tool, arguments)` 按固定顺序逐层判定：Plan 模式豁免（特殊工具 + plan 文件写入）→ 安全命令直放 → 危险命令直拒 → 路径沙箱 → 规则引擎 → 模式矩阵兜底。Plan 模式下 `Agent` / `ToolSearch` / `AskUserQuestion` 与 plan 文件自身写入直接放行。
- F9: 自学习：HITL 用户选 `PermissionResponse.ALLOW_ALWAYS` 时，Agent 在执行前把当前 `tool_name` + 主参数（超过 60 字符截断 + `*` 通配）包成 `Rule` 调 `rule_engine.append_local_rule(rule)`，写到本地规则文件。

## 4. 非功能需求

- N1: 危险命令模式必须硬编码进 `mewcode/permissions/dangerous.py` 的 `_DANGEROUS_PATTERNS`，不依赖外部下载或环境变量注入，避免被攻击者绕过。
- N2: 路径沙箱必须始终包含项目根 + `tempfile.gettempdir()`；额外路径在 `__init__` 时一次性 `Path.resolve()`，沙箱检查时再 `resolve(strict=True)` 解析符号链接，防止 symlink 换路径逃逸。
- N3: 规则文件解析必须在 YAML 语法错误 / 文件不存在 / 非列表结构 / 单条坏规则时静默跳过，不让单个坏规则导致整套规则失效（`_load_rules_file` 用 `try/except yaml.YAMLError, OSError` 兜底）。
- N4: `PermissionChecker.check` 是无副作用纯函数（除规则文件磁盘读），只读，不修改任何 in-memory 状态。
- N5: Plan 模式的工具豁免与 plan 文件豁免分支必须早于路径沙箱检查，避免 plan 模式下写 plan 文件被沙箱误拦。
- N6: HITL 链路必须是异步事件流：`Agent._execute_tool` 用 `asyncio.Future[PermissionResponse]` + `yield PermissionRequest(...)` 把决策权交给 TUI，TUI `set_result` 后 Agent 才继续，避免阻塞 Agent loop。

## 5. 设计概要

- 核心数据结构:
 - `DecisionEffect = Literal["allow", "deny", "ask"]` 与 `Decision(effect, reason)`（dataclass）。
 - `PermissionMode(str, Enum)` 六态枚举 + `_MODE_MATRIX[mode][category] -> effect`。
 - `PathSandbox`：持有已 `resolve` 的 `_allowed_roots: list[Path]`。
 - `Rule(tool_name, pattern, effect)`（frozen dataclass）+ `RuleEngine(user_path, project_path, local_path)`。
 - `PermissionChecker(detector, sandbox, rule_engine, mode)` + `plan_file_path` 字段。
- 主流程（一次 `check` 调用）:
 - `extract_content(tool.name, arguments)` 抽出主参数。
 - Plan 模式分支：白名单工具或 plan 文件写入 → `Decision("allow", ...)`。
 - 命令类工具：先 `is_safe_command` 直放；再 `detector.detect` 直拒。
 - 读 / 写工具：content 非空时走 `sandbox.check`。
 - 走 `rule_engine.evaluate`，命中按 effect 决定。
 - 落到 `mode_decide(self.mode, tool.category)` 兜底（`"allow"` / `"deny"` / `"ask"`）。
- 调用链:
 - `MewCodeApp._build_agent` 装配 → 构造 `PermissionChecker` + `PathSandbox` + `RuleEngine` → 传给 `Agent`。
 - `Agent._execute_tool` 执行工具前 → `self.permission_checker.check(...)` → `deny` 返回 `ToolResult(is_error=True)` / `ask` `yield PermissionRequest(...)` 走 HITL / `allow` 继续。
 - HITL 选 `ALLOW_ALWAYS` → `extract_content` + 截断 → `rule_engine.append_local_rule(rule)` 写本地。
 - `/plan` 命令切到 `PermissionMode.PLAN` + Agent 自动生成 `_plan_path_cache` 设置 `permission_checker.plan_file_path`；`/do` 通过 `PlanChoice.YOLO` / `PlanChoice.MANUAL` 还原模式。
- 与其他模块的交互:
 - 依赖 `mewcode.tools.base.Tool`（`name`、`category` 字段）。
 - 依赖 `PyYAML` 做规则文件序列化。
 - 被 `mewcode.agent.Agent`、`mewcode.app.MewCodeApp`、`mewcode.permission_dialog.InlinePermissionWidget` 直接使用；子 Agent（`mewcode.agents.fork`）继承父 Agent 的 `permission_checker`。

## 6. Out of Scope

- 不实现 LLM 分类器；本章纯静态规则。
- 不实现 PowerShell 危险命令检测，目前只覆盖 Bash。
- 不持久化 user / project 级别规则文件的写入，只写 local 规则文件。
- 不实现规则文件热重载（每次 `evaluate` 都读盘）。
- 不实现规则解释 UI 或可视化调试器。
- 不实现 Windows ACL / Linux capabilities 等 OS 级沙箱。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。
