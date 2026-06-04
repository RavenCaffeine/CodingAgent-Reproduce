# ch06: 权限系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已在 `origin/python` 分支落地。

## T1: 定义决策与模式枚举
- 影响文件: `mewcode/permissions/modes.py:1-31`
- 依赖任务: 无
- 完成标准: `DecisionEffect = Literal["allow", "deny", "ask"]`（modes.py:8）；`PermissionMode(str, Enum)` 六态 `DEFAULT/ACCEPT_EDITS/PLAN/BYPASS/CUSTOM/DONT_ASK`（modes.py:11-17）；`_MODE_MATRIX` 决策表 6×3 全部填齐（modes.py:20-27）；`mode_decide(mode, category)` 直接索引矩阵（modes.py:30-31）。

## T2: 实现 Layer 1 危险命令检测
- 影响文件: `mewcode/permissions/dangerous.py:5-15, 49-56`
- 依赖任务: 无
- 完成标准: `_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]]` 8 条核心模式（`rm -rf /`、`mkfs.`、`dd if=...of=/dev/`、`chmod -R 777 /`、fork bomb `:()\{ :|:& \};:`、`curl|sh`、`wget|sh`、`> /dev/sd`，dangerous.py:5-15）；`DangerousCommandDetector.__init__` 支持 `extra_patterns` 注入；`detect(command)` 用 `pattern.search` 命中即返回 `(True, reason)`，否则 `(False, "")`（dangerous.py:49-56）。

## T3: 实现 Layer 1 安全命令白名单
- 影响文件: `mewcode/permissions/dangerous.py:18-31, 34-44`
- 依赖任务: 无
- 完成标准: `_SAFE_COMMANDS` 列出 50+ 个只读命令前缀，覆盖 `ls / cat / git status / git log / go version / python --version` 等（dangerous.py:18-31）；`is_safe_command(command)` 先 `strip` 检查空字符串，再检查命令中不含 `|` / `;` / `&&` / `>` / `$(` / 反引号，再按精确匹配或 `startswith(safe + " ")` 命中前缀（dangerous.py:34-44）。

## T4: 实现 Layer 2 路径沙箱
- 影响文件: `mewcode/permissions/sandbox.py:7-46`
- 依赖任务: 无
- 完成标准: `PathSandbox.__init__(project_root, extra_allowed=None)` 把 `Path(project_root).resolve()` + `Path(tempfile.gettempdir()).resolve()` + 所有 extra `.resolve()` 后存入 `_allowed_roots`（sandbox.py:8-17）；`check(path)` 先 `expanduser`，相对路径相对 `project_root` 拼接，调 `resolve(strict=True)` 解析 symlink，路径不存在时回退到 `parent.resolve(strict=True) / name`（sandbox.py:23-34）；遍历 `_allowed_roots` 用 `relative_to` 判定，全 miss 返回 `(False, "路径 {path} 超出沙箱范围")`（sandbox.py:36-46）。

## T5: 实现 Layer 3 规则引擎
- 影响文件: `mewcode/permissions/rules.py:1-106`
- 依赖任务: 无
- 完成标准:
 - `Effect = Literal["allow", "deny"]`、`Rule(tool_name, pattern, effect)` 用 `@dataclass(frozen=True)`、`Rule.matches(tool_name, content)` 用 `fnmatch` 做 glob（rules.py:11, 26-35）。
 - `_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")` 在 rules.py:13；`parse_rule(raw, effect)` 解析，非法语法 `raise ValueError`（rules.py:38-42）。
 - `_CONTENT_FIELDS` 6 工具映射 + `extract_content(tool_name, arguments)`（rules.py:15-23, 45-49）。
 - `_load_rules_file(path)` 处理不存在 / YAML 错 / 非列表 / 单条坏规则时静默跳过（rules.py:52-73）。
 - `RuleEngine.__init__` 接收 `user_rules_path / project_rules_path / local_rules_path`（rules.py:76-84）；`_load_tiers` 顺序 user → project → local（rules.py:86-90）；`evaluate(tool_name, content)` 遍历每层用 `reversed(rules)` LIFO 匹配，找到 effect 立即返回（rules.py:92-97）。
 - `append_local_rule(rule)` 自动 `parent.mkdir(parents=True, exist_ok=True)`，读出现有规则 append 后用 `yaml.dump` 全量重写（rules.py:99-106）。

## T6: 实现 Decision 与 Plan 模式豁免
- 影响文件: `mewcode/permissions/checker.py:1-92`
- 依赖任务: T1~T5
- 完成标准:
 - `Decision(effect, reason)` 用 `@dataclass`（checker.py:14-17）；`_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion"})`（checker.py:13）。
 - `PermissionChecker.__init__(detector, sandbox, rule_engine, mode=PermissionMode.DEFAULT)` 初始化 `self.plan_file_path = ""`（checker.py:20-32）。
 - `_is_plan_file(target_path)` 多策略：basename 落在 `.mewcode/plans/` 直放；否则 `os.path.abspath` 双向匹配 / basename 相等（checker.py:82-92）。

## T7: 实现主入口 check
- 影响文件: `mewcode/permissions/checker.py:34-80`
- 依赖任务: T6
- 完成标准: `check(tool, arguments)` 按 spec.md F8 的 6 步顺序执行：
 1. `extract_content` 抽 content（checker.py:35）。
 2. Plan 模式：白名单工具直放 / WriteFile / EditFile 落在 plan 文件直放（checker.py:38-44）。
 3. `tool.category == "command"` 时先 `is_safe_command` 直放（checker.py:47-48）。
 4. `tool.category == "command"` 时 `detector.detect(content)` 直拒（checker.py:51-54）。
 5. `tool.category in ("read", "write")` 且 content 非空 → `sandbox.check` 不通过直拒（checker.py:57-60）。
 6. `rule_engine.evaluate` 命中按 effect 决定（checker.py:63-67）。
 7. `mode_decide(self.mode, tool.category)` 兜底，返回 allow / deny / ask（checker.py:70-77）。
 - 每个分支 `Decision.reason` 写明决策来源：`"Safe read-only command"` / `"危险命令拦截: ..."` / `"路径沙箱拦截: ..."` / `"权限规则放行"` / `"权限规则拒绝"` / `"权限模式 {mode} 放行/拒绝"` / `"需要用户确认"`。

## T8: 接入 Agent Loop 的工具执行
- 影响文件:
 - `mewcode/agent.py:125-135` 定义 `PermissionResponse` 三态 + `PermissionRequest(tool_name, description, future)`。
 - `mewcode/agent.py:292-305` `Agent.__init__` 接收 `permission_checker` 参数。
 - `mewcode/agent.py:352-355` `set_permission_mode(mode)` 同时更新 checker。
 - `mewcode/agent.py:476-478` Plan 模式给 `permission_checker.plan_file_path` 注入实际 plan 路径。
 - `mewcode/agent.py:814-852` `_execute_tool` 调 `checker.check` → deny 返 `ToolResult(is_error=True)` / ask `yield PermissionRequest(...)` 等 future / `ALLOW_ALWAYS` 自动 append_local_rule。
- 依赖任务: T7
- 完成标准: 用户切模式 / Plan 模式注入 / 工具调用前权限检查 / HITL 选 `ALLOW_ALWAYS` 四条主路径全部接到 `PermissionChecker.check` 与 `RuleEngine.append_local_rule`。

## T9: 接入 TUI 装配与模式切换
- 影响文件:
 - `mewcode/app.py:60-64` import `DangerousCommandDetector / PathSandbox / PermissionChecker / RuleEngine`。
 - `mewcode/app.py:623-632` `MewCodeApp._build_agent` 构造 `PermissionChecker`，`RuleEngine` 注入 `user_rules_path=home/.mewcode/permissions.yaml` + `project_rules_path=work_dir/.mewcode/permissions.yaml` + `local_rules_path=work_dir/.mewcode/permissions.local.yaml`。
 - `mewcode/app.py:985-994` `action_cycle_mode` 实现 Shift+Tab 切换。
 - `mewcode/app.py:1341-1346` `/do` 命令 → `PlanChoice.YOLO / MANUAL` 还原模式。
 - `mewcode/permission_dialog.py:11-15` `_PERM_OPTIONS` 三选项分别映射 `ALLOW / ALLOW_ALWAYS / DENY`。
- 依赖任务: T8
- 完成标准: TUI 启动后构造 Checker → Shift+Tab 循环模式 → `/plan` + `/do` 切 Plan ↔ Default/Bypass → `InlinePermissionWidget` 三选项与 `PermissionResponse` 对应。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准:
 - `ruff check mewcode/permissions/` 通过。
 - `pytest tests/test_permissions.py -v` 全绿（覆盖 5 大测试类 + 6 个 e2e 异步测试，共 35+ 用例）。
 - 手动场景:
 1. 默认模式下让 Agent 跑 `Bash(command="rm -rf /")` → `ToolResult.output` 含 `"Permission denied: 危险命令拦截: 递归强制删除根目录"`。
 2. 默认模式下让 Agent `ReadFile(file_path="/etc/passwd")` → 沙箱 Deny，错误信息含 `"沙箱"`。
 3. 默认模式下让 Agent `WriteFile` 到工作目录内 → 触发 `PermissionRequest`；TUI 选 `ALLOW_ALWAYS` → `.mewcode/permissions.local.yaml` 出现 `WriteFile(<path>*)` 规则；下次同路径写不再 Ask。
 4. `/plan` 进入 Plan 模式 → `WriteFile` 写非 plan 文件被 Deny；写 `_plan_path_cache` 指向的 plan 文件被 Allow。
 5. Shift+Tab 切到 `BYPASS` → `rm -rf /` 仍被 Deny（Layer 1 在模式矩阵之前，不可绕过）；普通 `WriteFile` 直接 Allow。

## 进度
- [x] T1 决策 + 模式枚举
- [x] T2 危险命令检测
- [x] T3 安全命令白名单
- [x] T4 路径沙箱
- [x] T5 规则引擎
- [x] T6 Decision + Plan 豁免
- [x] T7 主入口 check
- [x] T8 接入 Agent Loop
- [x] T9 接入 TUI 装配
- [x] T10 端到端验证（ruff + pytest + Agent loop 与 TUI 调用链确认）
