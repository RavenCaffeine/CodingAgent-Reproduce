"""TUI app tests — config loading and the streaming REPL loop, offline.

A fake client + monkeypatched input drive the real `MewCodeApp.run` loop so we
verify: streaming text reaches stdout, multi-turn memory is retained, token
usage accumulates, and YAML config (4 core fields + the three API-key supply
modes) loads correctly.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mewcode import app as app_mod
from mewcode.app import MewCodeApp
from mewcode.client import LLMClient
from mewcode.config import ProviderConfig, load_config
from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEnd, TextDelta


class FakeClient(LLMClient):
    def __init__(self, batches: list[list[Any]]) -> None:
        super().__init__()
        self._batches = batches
        self._i = 0

    async def stream(  # type: ignore[override]
        self,
        conversation: ConversationManager,
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        batch = self._batches[self._i]
        self._i += 1
        for e in batch:
            yield e


def _make_app(monkeypatch, batches) -> MewCodeApp:
    cfg = ProviderConfig(
        name="t", protocol="anthropic", model="claude-x", api_key="k"
    )
    monkeypatch.setattr(app_mod, "create_client", lambda c: FakeClient(batches))
    return MewCodeApp(cfg)


def test_config_loads_four_core_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent(
            """
            protocol: openai
            model: gpt-4.1
            base_url: https://example.com/v1
            api_key: sk-test
            thinking: false
            """
        )
    )
    cfg = load_config(p)
    assert cfg.protocol == "openai"
    assert cfg.model == "gpt-4.1"
    assert cfg.base_url == "https://example.com/v1"
    assert cfg.resolve_api_key() == "sk-test"


def test_config_missing_required_field(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("model: only-model\n")
    with pytest.raises(ValueError, match="protocol"):
        load_config(p)


# --- API key: three supply modes (manual / import / explicit) + fallback --- #


def test_api_key_manual_literal(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("protocol: openai\nmodel: gpt-4.1\napi_key: sk-LITERAL\n")
    assert load_config(p).resolve_api_key() == "sk-LITERAL"


def test_api_key_env_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ENVREF")
    p = tmp_path / "c.yaml"
    p.write_text("protocol: openai\nmodel: gpt-4.1\napi_key: ${OPENAI_API_KEY}\n")
    cfg = load_config(p)
    assert cfg.api_key_env == "OPENAI_API_KEY"
    assert cfg.resolve_api_key() == "sk-ENVREF"


def test_api_key_explicit_env_field(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ENVREF")
    p = tmp_path / "c.yaml"
    p.write_text("protocol: openai\nmodel: gpt-4.1\napi_key_env: OPENAI_API_KEY\n")
    assert load_config(p).resolve_api_key() == "sk-ENVREF"


def test_api_key_default_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-DEFAULT")
    p = tmp_path / "c.yaml"
    p.write_text("protocol: anthropic\nmodel: claude-sonnet-4-6\n")
    cfg = load_config(p)
    assert cfg.api_key_env == "ANTHROPIC_API_KEY"
    assert cfg.resolve_api_key() == "sk-DEFAULT"


def test_api_key_manual_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ENVREF")
    p = tmp_path / "c.yaml"
    p.write_text(
        "protocol: openai\nmodel: gpt-4.1\n"
        "api_key: sk-MANUAL\napi_key_env: OPENAI_API_KEY\n"
    )
    assert load_config(p).resolve_api_key() == "sk-MANUAL"


# --- REPL loop ------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repl_streams_and_remembers(monkeypatch, capsys):
    app = _make_app(
        monkeypatch,
        batches=[
            [TextDelta("Hello "), TextDelta("Rick!"),
             StreamEnd("end_turn", input_tokens=8, output_tokens=3)],
            [TextDelta("You said hi."),
             StreamEnd("end_turn", input_tokens=12, output_tokens=4)],
        ],
    )

    inputs = iter(["hi", "what did I say?", "/exit"])

    async def fake_read(prompt: str) -> str:
        return next(inputs)

    monkeypatch.setattr(app, "_read_line", fake_read)
    await app.run()

    out = capsys.readouterr().out
    assert "Hello Rick!" in out
    assert "You said hi." in out

    # ch05 injects an environment <system-reminder> as the first user message;
    # drop it to inspect the real conversational turns.
    msgs = [
        m for m in app.conversation.get_messages()
        if not m.content.startswith("<system-reminder>")
    ]
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert msgs[0].content == "hi"
    assert msgs[1].content == "Hello Rick!"

    assert app.total_input_tokens == 20
    assert app.total_output_tokens == 7
