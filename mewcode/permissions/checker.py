"""PermissionChecker — the layered decision entry point (ch06).

Order (defense in depth): plan exemption -> safe command -> dangerous command
-> path sandbox -> rule engine -> mode matrix. The blacklist and sandbox run
BEFORE the mode matrix, so no mode (even bypass) can skip them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mewcode.permissions.dangerous import DangerousCommandDetector, is_safe_command
from mewcode.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from mewcode.permissions.rules import RuleEngine, extract_content
from mewcode.permissions.sandbox import PathSandbox

if TYPE_CHECKING:
    from mewcode.tools.base import Tool

_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion"})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str


class PermissionChecker:
    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path = ""

    def check(self, tool: "Tool", arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)

        # 1. Plan-mode exemptions (before the sandbox).
        if self.mode == PermissionMode.PLAN:
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision("allow", "Plan mode: tool allowed")
            if tool.name in ("WriteFile", "EditFile") and self._is_plan_file(content):
                return Decision("allow", "Plan mode: writing plan file")

        # 2/3. Command class: safe whitelist, then dangerous blacklist (hard floor).
        if tool.category == "command":
            if is_safe_command(content):
                return Decision("allow", "Safe read-only command")
            dangerous, reason = self.detector.detect(content)
            if dangerous:
                return Decision("deny", f"危险命令拦截: {reason}")

        # 4. Path sandbox for file tools (hard floor).
        if tool.category in ("read", "write") and content:
            ok, reason = self.sandbox.check(content)
            if not ok:
                return Decision("deny", f"路径沙箱拦截: {reason}")

        # 5. Rule engine.
        effect = self.rule_engine.evaluate(tool.name, content)
        if effect == "allow":
            return Decision("allow", "权限规则放行")
        if effect == "deny":
            return Decision("deny", "权限规则拒绝")

        # 6. Mode matrix fallback.
        decided = mode_decide(self.mode, tool.category)
        if decided == "allow":
            return Decision("allow", f"权限模式 {self.mode.value} 放行")
        if decided == "deny":
            return Decision("deny", f"权限模式 {self.mode.value} 拒绝")
        return Decision("ask", "需要用户确认")

    def _is_plan_file(self, target_path: str) -> bool:
        if not target_path:
            return False
        norm = target_path.replace("\\", "/")
        if ".mewcode/plans/" in norm:
            return True
        if not self.plan_file_path:
            return False
        if os.path.abspath(target_path) == os.path.abspath(self.plan_file_path):
            return True
        return os.path.basename(target_path) == os.path.basename(self.plan_file_path)
