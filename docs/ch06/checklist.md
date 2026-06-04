# ch06: 权限系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 类型别名 `DecisionEffect = Literal["allow", "deny", "ask"]` 在 `mewcode/permissions/modes.py:8`（`grep -n "DecisionEffect" mewcode/permissions/modes.py`）
- [ ] 枚举 `PermissionMode(str, Enum)` 与六态 `DEFAULT/ACCEPT_EDITS/PLAN/BYPASS/CUSTOM/DONT_ASK` 在 `mewcode/permissions/modes.py:11-17`
- [ ] 决策矩阵 `_MODE_MATRIX` 在 `mewcode/permissions/modes.py:20-27`，6×3 共 18 格全填齐
- [ ] `mode_decide(mode, category)` 在 `mewcode/permissions/modes.py:30-31`，直接索引矩阵
- [ ] `_DANGEROUS_PATTERNS` 在 `mewcode/permissions/dangerous.py:5-15` 列出 8 条；`DangerousCommandDetector.detect` 在 `dangerous.py:49-56` 用 `pattern.search`
- [ ] `_SAFE_COMMANDS` 在 `mewcode/permissions/dangerous.py:18-31` 列出 50+ 条；`is_safe_command` 在 `dangerous.py:34-44`，命令中含 `|;&&>$(` 反引号一律拒
- [ ] `PathSandbox.__init__ / check` 在 `mewcode/permissions/sandbox.py:7-46`，默认包含 `tempfile.gettempdir()` 与 `project_root`，`check` 用 `resolve(strict=True)` 解 symlink
- [ ] `Rule(tool_name, pattern, effect)` 用 `@dataclass(frozen=True)` 在 `mewcode/permissions/rules.py:26-35`，`matches` 用 `fnmatch`
- [ ] `_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")` 在 `mewcode/permissions/rules.py:13`，`parse_rule` 在 `rules.py:38-42` 非法语法 `raise ValueError`
- [ ] `_CONTENT_FIELDS` 6 工具映射 + `extract_content` 在 `mewcode/permissions/rules.py:15-23, 45-49`
- [ ] `RuleEngine` + `_load_tiers` + `evaluate` + `append_local_rule` 在 `mewcode/permissions/rules.py:76-106`，单层用 `reversed(rules)` 实现 LIFO
- [ ] `Decision` dataclass + `_PLAN_MODE_ALLOWED_TOOLS = {"Agent", "ToolSearch", "AskUserQuestion"}` 在 `mewcode/permissions/checker.py:13-17`
- [ ] `PermissionChecker.check` 主入口在 `mewcode/permissions/checker.py:34-80`，按 Plan 豁免 → 安全命令 → 危险命令 → 沙箱 → 规则 → 模式 6 步顺序判定
- [ ] `_is_plan_file` 多策略匹配在 `mewcode/permissions/checker.py:82-92`：abspath 相等 / basename 相等 / 路径含 `.mewcode/plans/`
- [ ] Plan 模式豁免分支早于沙箱检查（`checker.py:38-44`）
- [ ] `mewcode/permissions/__init__.py:1-19` 导出 `Decision / DecisionEffect / DangerousCommandDetector / PathSandbox / PermissionChecker / PermissionMode / Rule / RuleEngine / extract_content / mode_decide / parse_rule`

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "PermissionChecker(" mewcode/ --include="*.py"` 至少 1 处真实调用（`mewcode/app.py:623`）
- [ ] `grep -rn "PathSandbox(" mewcode/ --include="*.py"` 至少 1 处（`mewcode/app.py:625`）
- [ ] `grep -rn "RuleEngine(" mewcode/ --include="*.py"` Engine 构造在 App 装配（`mewcode/app.py:626-630`）；测试中多处构造（`tests/test_permissions.py`）
- [ ] `grep -rn "permission_checker.check\b" mewcode/ --include="*.py"` 主流程调用方在 `mewcode/agent.py:815, 1066`（双路径：交互式 + 子 Agent）
- [ ] `grep -rn "append_local_rule" mewcode/ --include="*.py"` 主流程调用方在 `mewcode/agent.py:852`
- [ ] `grep -rn "PermissionMode\." mewcode/ --include="*.py"` 在 `mewcode/app.py:631, 855, 988, 994, 1341, 1346, 1636`、`mewcode/agent.py:305, 330, 354, 1073` 等至少 10 处使用，覆盖创建 / 切换 / 渲染各路径
- [ ] `grep -rn "extract_content" mewcode/ --include="*.py"` 在 `mewcode/agent.py:848` HITL 自学习 + `mewcode/permissions/checker.py:35` 主流程 + `mewcode/permissions/rules.py:45` 定义共 3 处
- [ ] 配置接入：`mewcode/app.py:626-630` 默认配置 `user_rules_path=home/.mewcode/permissions.yaml`、`project_rules_path=work_dir/.mewcode/permissions.yaml`、`local_rules_path=work_dir/.mewcode/permissions.local.yaml`
- [ ] HITL 链路：`PermissionChecker.check` 返回 `effect="ask"` → `mewcode/agent.py:828-852` 通过 `PermissionRequest(tool_name, description, future)` 走 ch04 事件循环 → `mewcode/permission_dialog.py:11-15 InlinePermissionWidget` 渲染 3 选项 → 用户选 `ALLOW_ALWAYS` 时回灌 `append_local_rule`（`agent.py:847-852`）
- [ ] 命令注册：`/mode` / `/plan` / `/do` 命令处理器在 `mewcode/commands/handlers/permission.py`、`plan.py`、`do.py`，用于切换 `PermissionMode`

## 3. 编译与测试
- [ ] `ruff check mewcode/permissions/` 无错误
- [ ] `pytest tests/test_permissions.py -v` 全绿（覆盖 `TestDangerousCommandDetector` / `TestPathSandbox` / `TestRuleEngine` / `TestPermissionMode` / `TestPermissionChecker` 5 个测试类 + `test_e2e_dangerous_command_blocked_loop_continues` / `test_e2e_sandbox_blocks_outside_path` / `test_e2e_rule_allows_git` / `test_e2e_default_mode_write_triggers_ask` / `test_e2e_bypass_mode_allows_all` / `test_e2e_user_denies_operation` 6 个 e2e 异步测试）
- [ ] `mypy mewcode/permissions/` 无类型错误（如配置启用）

## 4. 端到端验证
- [ ] TUI 启动并加载 provider 后 `MewCodeApp._build_agent` 构造 `PermissionChecker`（`mewcode/app.py:623-632`），传入 `Agent(permission_checker=checker, ...)`（`app.py:654`）
- [ ] Plan Mode：`/plan` → `Agent.set_permission_mode(PermissionMode.PLAN)` + Agent loop 给 `permission_checker.plan_file_path = str(self._get_plan_path())`（`agent.py:476-478`）；下一轮 `WriteFile` 调 `check`，非 plan 文件被 Deny
- [ ] HITL：默认模式下让模型写新文件，TUI 弹三选项 `Yes / Yes, and don't ask again for this pattern / No`（`mewcode/permission_dialog.py:11-15`），与 `PermissionResponse.ALLOW / ALLOW_ALWAYS / DENY` 对应
- [ ] 自学习：选 `ALLOW_ALWAYS` 时 `agent.py:847-852` 用 `extract_content` + 截断 60 字符 + `*` 通配生成 `Rule`，append 到 `.mewcode/permissions.local.yaml`
- [ ] 危险命令防御不可绕过：`PermissionMode.BYPASS` 时 `checker.py:51-54` 在模式矩阵之前先 `detector.detect`，让 Agent 跑 `rm -rf /` 仍 Deny（`tests/test_permissions.py:test_bypass_still_blocks_dangerous` 已覆盖）
- [ ] 留存证据: 验收阶段未自动保存日志；如需补，在 `.mewcode/permissions.local.yaml` 中观察 `ALLOW_ALWAYS` 写入的 YAML 列表项 `[{rule: "WriteFile(...)", effect: "allow"}, ...]`

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/python/ch06/`）
- [ ] commit 信息标注 `ch06` 与三件套关闭状态（待统一打包提交）
