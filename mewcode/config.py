"""Provider configuration and token budgeting.

This module is the dependency boundary for `mewcode/client.py`. It defines how
a provider (Anthropic / OpenAI) is configured, how API keys are resolved, and
the output-token budgets the client requests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace

# Hard ceiling the Agent Loop may escalate to when a turn ends on
# `stop_reason == "max_tokens"`. See ProviderConfig.set_max_output_tokens.
MAX_TOKENS_CEILING = 128000

# Default budgets (see N5 in spec.md).
_THINKING_DEFAULT_TOKENS = 64000
_PLAIN_DEFAULT_TOKENS = 8192


class ConfigError(ValueError):
    """Raised when a config file is structurally invalid."""


# Host env vars safe to pass through to MCP stdio child processes. API keys and
# other secrets are deliberately excluded — a child server gets only these plus
# whatever the config explicitly declares (see build_child_env).
_CHILD_ENV_WHITELIST = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SystemRoot",
    "PATHEXT",
    "COMSPEC",
)

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def resolve_env_vars(mapping: dict[str, str]) -> dict[str, str]:
    """Expand ``${VAR}`` / ``$VAR`` inside a mapping's values from os.environ.

    Used for MCP HTTP headers and stdio env so API keys can be referenced as
    ``Authorization: "Bearer ${MCP_TOKEN}"``. Missing variables expand to "".
    """

    def _sub(value: str) -> str:
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1) or m.group(2)
            return os.environ.get(name, "")

        return _ENV_VAR_RE.sub(repl, value)

    return {k: _sub(v) if isinstance(v, str) else v for k, v in mapping.items()}


def build_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal env for an MCP stdio child: whitelist + explicit extras.

    Host secrets (e.g. ANTHROPIC_API_KEY) are NOT inherited — only the
    whitelist below, plus whatever ``extra`` the server config declares.
    """
    env: dict[str, str] = {}
    for key in _CHILD_ENV_WHITELIST:
        if key in os.environ:
            env[key] = os.environ[key]
    if extra:
        env.update(extra)
    return env


@dataclass
class MCPServerConfig:
    """One external MCP server. Either stdio (command) or HTTP (url), never both.

    Attributes:
        name: Server label; becomes the ``mcp_<name>_<tool>`` prefix.
        command/args/env: stdio transport — child process + its argv + env.
        url/headers: Streamable HTTP transport — endpoint + request headers.
        timeout: Per-request timeout in seconds.
    """

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass
class ProviderConfig:
    """Everything the client layer needs to talk to one provider.

    Attributes:
        name: Human-readable provider name (e.g. "anthropic", "openai", or a
            named profile). Used only for display / cloning.
        protocol: Wire protocol — one of {"anthropic", "openai"}. Routes
            `create_client` to the right implementation.
        model: Concrete model id sent to the provider.
        api_key: Literal key. If None, falls back to `api_key_env`.
        api_key_env: Environment variable name holding the key.
        base_url: Optional custom endpoint (proxies, gateways, self-hosted).
        thinking: Whether Extended Thinking is requested.
        max_output_tokens: Explicit override; when None a default is derived
            from `thinking`.
    """

    name: str
    protocol: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    thinking: bool = False
    max_output_tokens: int | None = None
    extra: dict = field(default_factory=dict)
    # ch07: external MCP servers to connect at startup.
    mcp_servers: list["MCPServerConfig"] = field(default_factory=list)
    # ch08: context window used to size the auto-compaction threshold. Lower it
    # if your effective budget (e.g. a tight rate limit) is smaller than the
    # model's nominal window.
    context_window: int = 200_000

    def resolve_api_key(self) -> str | None:
        """Resolve the API key: literal first, then environment variable."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None

    def get_max_output_tokens(self) -> int:
        """Output-token budget for this turn.

        Explicit `max_output_tokens` wins; otherwise default by thinking mode
        (64000 when thinking, else 8192) — N5 in spec.md.
        """
        if self.max_output_tokens is not None:
            return self.max_output_tokens
        return _THINKING_DEFAULT_TOKENS if self.thinking else _PLAIN_DEFAULT_TOKENS

    def clone(self, **overrides) -> "ProviderConfig":
        """Return a copy with the given fields replaced.

        Used by SubAgent (`_create_client_for_model`) to derive a child config
        from the parent without mutating it.
        """
        return replace(self, **overrides)


_DEFAULT_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Conventional endpoint used when a provider needs one but the config omits it.
_DEFAULT_BASE_URL = {
    "deepseek": "https://api.deepseek.com",
}


def load_config(path: str | os.PathLike) -> ProviderConfig:
    """Load a provider config from a YAML file.

    Four core fields: protocol / model / base_url / api_key. `name`, `thinking`
    and `api_key_env` are optional.

    The API key can be supplied three ways (checked in this order at runtime by
    ``ProviderConfig.resolve_api_key``):

    1. Manual literal — ``api_key: sk-ant-abc123``
    2. Env reference   — ``api_key: ${ANTHROPIC_API_KEY}`` (or ``$VAR``)
    3. Explicit env    — ``api_key_env: ANTHROPIC_API_KEY``

    If none is given, it falls back to the provider's conventional variable
    (ANTHROPIC_API_KEY for anthropic, OPENAI_API_KEY for openai), so plain
    ``export``-then-run works with no key line at all.
    """
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    protocol = data.get("protocol")
    model = data.get("model")
    if not protocol:
        raise ValueError("config: missing required field 'protocol'")
    if not model:
        raise ValueError("config: missing required field 'model'")

    api_key = data.get("api_key")
    api_key_env = data.get("api_key_env")

    # An ${VAR} / $VAR value means "read from this environment variable".
    if isinstance(api_key, str) and api_key.startswith("$"):
        api_key_env = api_key.strip("${} ")
        api_key = None

    # Neither a literal nor an explicit env var -> fall back to the
    # provider's conventional environment variable.
    if not api_key and not api_key_env:
        api_key_env = _DEFAULT_API_KEY_ENV.get(protocol)

    base_url = data.get("base_url") or _DEFAULT_BASE_URL.get(protocol)

    return ProviderConfig(
        name=data.get("name", protocol),
        protocol=protocol,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        base_url=base_url,
        thinking=bool(data.get("thinking", False)),
        max_output_tokens=data.get("max_output_tokens"),
        mcp_servers=_parse_mcp_servers(data.get("mcp_servers")),
        context_window=int(data.get("context_window", 200_000)),
    )


def _parse_mcp_servers(raw: object) -> list[MCPServerConfig]:
    """Deserialize the ``mcp_servers`` mapping into MCPServerConfig list.

    Shape (key = server name)::

        mcp_servers:
          context7:
            command: npx
            args: ["-y", "@upstash/context7-mcp"]
          remote:
            url: https://example.com/mcp
            headers: { Authorization: "Bearer ${MCP_TOKEN}" }

    Each entry must declare exactly one transport: ``command`` (stdio) or
    ``url`` (HTTP) — never both, never neither (raises ConfigError).
    """
    if not raw:
        return []
    if not isinstance(raw, dict):
        raise ConfigError("config: 'mcp_servers' must be a mapping of name -> server")

    servers: list[MCPServerConfig] = []
    for name, entry in raw.items():
        entry = entry or {}
        command = entry.get("command")
        url = entry.get("url")
        if command and url:
            raise ConfigError(
                f"mcp_servers['{name}'] cannot have both 'command' and 'url'"
            )
        if not command and not url:
            raise ConfigError(
                f"mcp_servers['{name}'] must have either 'command' or 'url'"
            )
        servers.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=list(entry.get("args") or []),
                url=url,
                headers=dict(entry.get("headers") or {}),
                env=dict(entry.get("env") or {}),
                timeout=float(entry.get("timeout", 30.0)),
            )
        )
    return servers
