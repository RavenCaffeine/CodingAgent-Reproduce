"""Layer 1: dangerous-command blacklist + safe-command whitelist (ch06).

Patterns are hardcoded so they can't be bypassed via env/config injection.
"""

from __future__ import annotations

import re

# (compiled pattern, human reason). search() anywhere in the command.
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*.*\s+/(\s|$)"),
     "递归强制删除根目录"),
    (re.compile(r"\bmkfs\.\w+"), "磁盘格式化"),
    (re.compile(r"\bdd\b.*\bif=.*\bof=/dev/"), "向块设备写入 (dd)"),
    (re.compile(r"\bchmod\s+(-[a-zA-Z]*\s+)*-R\s+777\s+/"), "递归放开根目录权限"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "Fork 炸弹"),
    (re.compile(r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh"), "远程脚本下载即执行 (curl|sh)"),
    (re.compile(r"\bwget\b.*\|\s*(sudo\s+)?(ba)?sh"), "远程脚本下载即执行 (wget|sh)"),
    (re.compile(r">\s*/dev/sd[a-z]"), "向裸磁盘设备写入"),
]

# read-only command prefixes that are always safe (no shell metachars allowed).
_SAFE_COMMANDS: frozenset[str] = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
    "echo", "printf", "date", "whoami", "id", "hostname", "uname", "env",
    "which", "type", "tree", "find", "basename", "dirname", "realpath",
    "git status", "git diff", "git log", "git branch", "git show",
    "git remote", "git config --list", "git rev-parse",
    "go version", "go env", "python --version", "python3 --version",
    "node --version", "node -v", "npm --version", "npm -v", "npm ls",
    "pip --version", "pip list", "uv --version", "ruff --version",
    "pytest --version", "rustc --version", "cargo --version", "java -version",
})

_UNSAFE_CHARS = ("|", ";", "&&", ">", "$(", "`")


def is_safe_command(command: str) -> bool:
    """True if the command is a known read-only prefix with no shell tricks."""
    cmd = command.strip()
    if not cmd:
        return False
    if any(ch in cmd for ch in _UNSAFE_CHARS):
        return False
    for safe in _SAFE_COMMANDS:
        if cmd == safe or cmd.startswith(safe + " "):
            return True
    return False


class DangerousCommandDetector:
    def __init__(
        self, extra_patterns: list[tuple[re.Pattern[str], str]] | None = None
    ) -> None:
        self._patterns = list(_DANGEROUS_PATTERNS) + list(extra_patterns or [])

    def detect(self, command: str) -> tuple[bool, str]:
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""
