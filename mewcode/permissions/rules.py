"""Layer 3: glob rule engine over three YAML rule files (ch06)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

Effect = Literal["allow", "deny"]

_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")

# Tool name -> the argument field whose value the pattern matches against.
_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))


@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect

    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name not in (tool_name, "*"):
            return False
        return fnmatch(content, self.pattern)


def parse_rule(raw: str, effect: str) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Invalid rule syntax: {raw!r}")
    if effect not in ("allow", "deny"):
        raise ValueError(f"Invalid effect: {effect!r}")
    return Rule(m.group(1), m.group(2), effect)  # type: ignore[arg-type]


def _load_rules_file(path: Path) -> list[Rule]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(data, list):
        return []
    rules: list[Rule] = []
    for item in data:
        try:
            rules.append(parse_rule(item["rule"], item["effect"]))
        except (KeyError, TypeError, ValueError):
            continue  # skip a single bad rule, keep the rest
    return rules


class RuleEngine:
    """user < project < local precedence; local overrides; LIFO within a file."""

    def __init__(
        self,
        user_rules_path: str | None = None,
        project_rules_path: str | None = None,
        local_rules_path: str | None = None,
    ) -> None:
        self.user_path = Path(user_rules_path) if user_rules_path else None
        self.project_path = Path(project_rules_path) if project_rules_path else None
        self.local_path = Path(local_rules_path) if local_rules_path else None

    def _load_tiers(self) -> list[list[Rule]]:
        # ordered user -> project -> local
        return [
            _load_rules_file(self.user_path) if self.user_path else [],
            _load_rules_file(self.project_path) if self.project_path else [],
            _load_rules_file(self.local_path) if self.local_path else [],
        ]

    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        # local overrides project overrides user -> walk tiers in reverse.
        for tier in reversed(self._load_tiers()):
            for rule in reversed(tier):  # LIFO within a file
                if rule.matches(tool_name, content):
                    return rule.effect
        return None

    def append_local_rule(self, rule: Rule) -> None:
        if self.local_path is None:
            return
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict[str, str]] = []
        if self.local_path.exists():
            try:
                loaded = yaml.safe_load(self.local_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = loaded
            except (yaml.YAMLError, OSError):
                existing = []
        existing.append(
            {"rule": f"{rule.tool_name}({rule.pattern})", "effect": rule.effect}
        )
        self.local_path.write_text(yaml.dump(existing), encoding="utf-8")
