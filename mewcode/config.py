"""Provider configuration and token budgeting.

This module is the dependency boundary for `mewcode/client.py`. It defines how
a provider (Anthropic / OpenAI) is configured, how API keys are resolved, and
the output-token budgets the client requests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# Hard ceiling the Agent Loop may escalate to when a turn ends on
# `stop_reason == "max_tokens"`. See ProviderConfig.set_max_output_tokens.
MAX_TOKENS_CEILING = 128000

# Default budgets (see N5 in spec.md).
_THINKING_DEFAULT_TOKENS = 64000
_PLAIN_DEFAULT_TOKENS = 8192


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
    )
