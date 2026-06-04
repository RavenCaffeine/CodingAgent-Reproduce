"""Tests for the MCP client (ch07).

The official `mcp` SDK transports are not exercised here; instead we use fakes
to test our own logic: env resolution, config parsing, the tool wrapper's
schema/param/result handling, and the manager's per-server failure isolation.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from mewcode.config import (
    ConfigError,
    MCPServerConfig,
    build_child_env,
    resolve_env_vars,
)
from mewcode.mcp import manager as manager_mod
from mewcode.mcp.manager import MCPManager
from mewcode.mcp.tool_wrapper import (
    MCPToolWrapper,
    _build_params_model,
    _extract_text,
)
from mewcode.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# resolve_env_vars
# --------------------------------------------------------------------------- #
class TestResolveEnvVars:
    def test_expands_braced_var(self, monkeypatch):
        monkeypatch.setenv("MCP_TOKEN", "xyz")
        out = resolve_env_vars({"Authorization": "Bearer ${MCP_TOKEN}"})
        assert out["Authorization"] == "Bearer xyz"
        assert "${" not in out["Authorization"]

    def test_expands_bare_var(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert resolve_env_vars({"k": "$FOO"})["k"] == "bar"

    def test_missing_var_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert resolve_env_vars({"k": "x${NOPE}y"})["k"] == "xy"


# --------------------------------------------------------------------------- #
# build_child_env
# --------------------------------------------------------------------------- #
class TestBuildChildEnv:
    def test_excludes_host_secrets(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = build_child_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_includes_explicit_extra(self):
        env = build_child_env({"LOG_LEVEL": "info"})
        assert env["LOG_LEVEL"] == "info"


# --------------------------------------------------------------------------- #
# load_config / mcp_servers parsing
# --------------------------------------------------------------------------- #
def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestLoadConfigMCP:
    _BASE = "protocol: openai\nmodel: gpt-4.1\napi_key: x\n"

    def test_parses_stdio_and_http(self, tmp_path):
        from mewcode.config import load_config

        cfg = load_config(
            _write(
                tmp_path,
                self._BASE
                + "mcp_servers:\n"
                + "  fs:\n"
                + "    command: npx\n"
                + "    args: ['-y', 'server']\n"
                + "  remote:\n"
                + "    url: https://example.com/mcp\n",
            )
        )
        by_name = {s.name: s for s in cfg.mcp_servers}
        assert by_name["fs"].is_stdio is True
        assert by_name["fs"].args == ["-y", "server"]
        assert by_name["remote"].is_stdio is False
        assert by_name["remote"].url == "https://example.com/mcp"

    def test_both_command_and_url_errors(self, tmp_path):
        from mewcode.config import load_config

        with pytest.raises(ConfigError, match="cannot have both"):
            load_config(
                _write(
                    tmp_path,
                    self._BASE
                    + "mcp_servers:\n  bad:\n    command: x\n    url: http://y\n",
                )
            )

    def test_neither_command_nor_url_errors(self, tmp_path):
        from mewcode.config import load_config

        with pytest.raises(ConfigError, match="must have either"):
            load_config(
                _write(
                    tmp_path,
                    self._BASE + "mcp_servers:\n  bad:\n    args: ['a']\n",
                )
            )

    def test_no_mcp_servers_is_empty_list(self, tmp_path):
        from mewcode.config import load_config

        assert load_config(_write(tmp_path, self._BASE)).mcp_servers == []


# --------------------------------------------------------------------------- #
# fakes for wrapper / manager
# --------------------------------------------------------------------------- #
def _tool_def(name, schema=None, description="d"):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


class _FakeClient:
    """Stands in for MCPClient; records the call_tool invocation."""

    def __init__(self, config, *, tools=None, fail=False):
        self.config = config
        self.name = config.name
        self._tools = tools or []
        self._fail = fail
        self.is_alive = True
        self.closed = False
        self.last_call = None

    async def connect(self):
        if self._fail:
            raise RuntimeError("boom")

    async def list_tools(self):
        return self._tools

    async def call_tool(self, name, arguments):
        self.last_call = (name, arguments)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"ran {name}")],
            isError=False,
        )

    async def close(self):
        self.closed = True
        self.is_alive = False


# --------------------------------------------------------------------------- #
# MCPToolWrapper
# --------------------------------------------------------------------------- #
class TestMCPToolWrapper:
    def test_name_prefix_and_category(self):
        client = _FakeClient(MCPServerConfig(name="github"))
        w = MCPToolWrapper("github", _tool_def("create_issue"), client)
        assert w.name == "mcp_github_create_issue"
        assert w.category == "command"
        assert w.should_defer is True

    def test_get_schema_returns_raw_input_schema(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        w = MCPToolWrapper(
            "s", _tool_def("t", schema), _FakeClient(MCPServerConfig(name="s"))
        )
        assert w.get_schema()["input_schema"] is w._input_schema

    def test_required_field_is_mandatory(self):
        from pydantic import ValidationError

        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        Model = _build_params_model("mcp_s_t", schema)
        with pytest.raises(ValidationError):
            Model.model_validate({})
        assert Model.model_validate({"q": "hi"}).q == "hi"

    def test_optional_field_defaults_none(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        Model = _build_params_model("mcp_s_t", schema)
        assert Model.model_validate({}).q is None

    @pytest.mark.asyncio
    async def test_execute_round_trips_and_excludes_none(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        client = _FakeClient(MCPServerConfig(name="s"))
        w = MCPToolWrapper("s", _tool_def("search", schema), client)
        result = await w.execute(w.params_model.model_validate({"q": "hi"}))
        assert result.output == "ran search"
        assert result.is_error is False
        assert client.last_call == ("search", {"q": "hi"})  # None dropped


# --------------------------------------------------------------------------- #
# _extract_text
# --------------------------------------------------------------------------- #
class TestExtractText:
    def test_joins_text_blocks(self):
        r = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="a"),
                SimpleNamespace(type="text", text="b"),
            ]
        )
        assert _extract_text(r) == "a\nb"

    def test_empty_content_falls_back(self):
        assert _extract_text(SimpleNamespace(content=[])) == "(no output)"

    def test_image_block_rendered(self):
        r = SimpleNamespace(
            content=[SimpleNamespace(type="image", mimeType="image/png")]
        )
        assert "image/png" in _extract_text(r)


# --------------------------------------------------------------------------- #
# MCPManager — partial failure isolation
# --------------------------------------------------------------------------- #
class TestMCPManagerPartialFailure:
    @pytest.mark.asyncio
    async def test_single_server_failure_does_not_block_others(self, monkeypatch):
        good_tools = [_tool_def("ok_tool")]

        def fake_client_factory(config):
            return _FakeClient(
                config,
                tools=good_tools if config.name == "good" else [],
                fail=config.name == "bad",
            )

        monkeypatch.setattr(manager_mod, "MCPClient", fake_client_factory)

        registry = ToolRegistry()
        mgr = MCPManager()
        mgr.load_configs(
            [
                MCPServerConfig(name="bad", command="x"),
                MCPServerConfig(name="good", command="y"),
            ]
        )
        errors = await mgr.register_all_tools(registry)

        assert any(e.startswith("bad:") for e in errors)
        # the good server's tool still registered despite bad's failure
        assert registry.get("mcp_good_ok_tool").name == "mcp_good_ok_tool"

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_clients(self, monkeypatch):
        clients = []

        def factory(config):
            c = _FakeClient(config, tools=[_tool_def("t")])
            clients.append(c)
            return c

        monkeypatch.setattr(manager_mod, "MCPClient", factory)
        mgr = MCPManager()
        mgr.load_configs([MCPServerConfig(name="s", command="x")])
        await mgr.register_all_tools(ToolRegistry())
        await mgr.shutdown()
        assert clients and all(c.closed for c in clients)


# silence "no event loop" if pytest-asyncio mode differs
os.environ.setdefault("PYTHONASYNCIODEBUG", "0")
