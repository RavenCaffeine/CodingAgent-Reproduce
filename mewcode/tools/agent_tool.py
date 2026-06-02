"""SubAgent tooling — model selection helpers.

ch02 contributes the model short-name mapping used by SubAgent to swap models
while reusing the same `LLMClient` interface. The full AgentTool lands in a
later chapter; here we expose the client-construction helper it depends on.
"""

from __future__ import annotations

from mewcode.client import LLMClient, create_client
from mewcode.config import ProviderConfig

# Short alias -> concrete model id. Inlined per spec.md F12 / T6.
_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


def _create_client_for_model(
    model_alias: str,
    parent_config: ProviderConfig,
) -> LLMClient:
    """Derive a child `LLMClient` for `model_alias` from the parent config.

    A known alias maps to its concrete model id; anything else is treated as a
    literal model id and passed through. The parent's provider config is cloned
    (name + model overridden) so the child inherits credentials and endpoint.
    """
    model_id = _MODEL_MAP.get(model_alias, model_alias)
    child_config = parent_config.clone(
        name=f"{parent_config.name}:{model_alias}",
        model=model_id,
    )
    return create_client(child_config)
