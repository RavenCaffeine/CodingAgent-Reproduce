"""Two-layer conversation model.

Internally we keep a clean `Message` history with structured blocks (thinking /
tool use / tool result). At request time `serialize(protocol)` flattens that
history into the exact request body each provider expects, without dropping any
field (thinking signatures, tool input dicts, tool_result is_error all make the
round trip — N3 in spec.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_REMINDER_OPEN = "<system-reminder>"
_REMINDER_CLOSE = "</system-reminder>"


# --------------------------------------------------------------------------- #
# Message blocks
# --------------------------------------------------------------------------- #


@dataclass
class ThinkingBlock:
    """An Extended Thinking block. `signature` must round-trip to the provider."""

    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    """An assistant tool invocation."""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    """The result of executing a tool, fed back as a user-turn block."""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """One conversation turn with structured blocks."""

    role: str  # "user" | "assistant"
    content: str = ""
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    tool_uses: list[ToolUseBlock] = field(default_factory=list)
    tool_results: list[ToolResultBlock] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Conversation manager
# --------------------------------------------------------------------------- #


@dataclass
class ConversationManager:
    """Owns the message history and serializes it per protocol.

    Single-consumer model — no locking (N4). The Agent Loop serializes all
    appends.
    """

    history: list[Message] = field(default_factory=list)
    env_injected: bool = False
    ltm_injected: bool = False
    last_input_tokens: int = 0

    # --- writers --------------------------------------------------------- #

    def add_user_message(self, content: str) -> None:
        self.history.append(Message(role="user", content=content))

    def add_assistant_message(
        self,
        content: str,
        tool_uses: list[ToolUseBlock] | None = None,
        thinking_blocks: list[ThinkingBlock] | None = None,
    ) -> None:
        self.history.append(
            Message(
                role="assistant",
                content=content,
                tool_uses=tool_uses or [],
                thinking_blocks=thinking_blocks or [],
            )
        )

    def add_system_reminder(self, content: str) -> None:
        """Append a reminder as a user message wrapped in <system-reminder>."""
        wrapped = f"{_REMINDER_OPEN}\n{content}\n{_REMINDER_CLOSE}"
        self.history.append(Message(role="user", content=wrapped))

    def add_tool_results_message(self, results: list[ToolResultBlock]) -> None:
        self.history.append(Message(role="user", tool_results=list(results)))

    def inject_environment(self, context: str) -> None:
        """Idempotent head-insert of environment context."""
        if self.env_injected:
            return
        wrapped = f"{_REMINDER_OPEN}\n{context}\n{_REMINDER_CLOSE}"
        self.history.insert(0, Message(role="user", content=wrapped))
        self.env_injected = True

    def inject_long_term_memory(self, instructions: str, memories: str) -> None:
        """Idempotent head-insert of long-term memory."""
        if self.ltm_injected:
            return
        body = f"{instructions}\n\n{memories}".strip()
        wrapped = f"{_REMINDER_OPEN}\n{body}\n{_REMINDER_CLOSE}"
        self.history.insert(0, Message(role="user", content=wrapped))
        self.ltm_injected = True

    def replace_history(self, messages: list[Message]) -> None:
        """Replace the whole history (used by Compact). Resets inject flags."""
        self.history = list(messages)
        self.env_injected = False
        self.ltm_injected = False

    def get_messages(self) -> list[Message]:
        """Shallow copy of the history."""
        return list(self.history)

    # --- serialization --------------------------------------------------- #

    def serialize(self, protocol: str) -> list[dict[str, Any]]:
        if protocol == "anthropic":
            return self._serialize_anthropic()
        if protocol == "openai":
            return self._serialize_openai()
        if protocol == "deepseek":
            return self._serialize_openai_chat()
        raise ValueError(f"Unknown protocol: {protocol}")

    def _serialize_anthropic(self) -> list[dict[str, Any]]:
        """Flatten to Anthropic Messages format.

        Assistant turns with thinking or tool_use become list-of-blocks.
        Consecutive user reminders are merged into the previous user message so
        user/assistant strictly alternate.
        """
        out: list[dict[str, Any]] = []

        for msg in self.history:
            if msg.role == "assistant":
                out.append(self._anthropic_assistant(msg))
                continue

            # user turn
            blocks = self._anthropic_user_blocks(msg)
            is_reminder = bool(
                msg.content and msg.content.startswith(_REMINDER_OPEN)
            )
            # Merge a reminder into the prior user message if the last emitted
            # message is also a user turn (keeps alternation intact).
            if is_reminder and out and out[-1]["role"] == "user":
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": "user", "content": blocks})

        return out

    @staticmethod
    def _anthropic_assistant(msg: Message) -> dict[str, Any]:
        # Plain text only -> simple string content.
        if not msg.thinking_blocks and not msg.tool_uses:
            return {"role": "assistant", "content": msg.content}

        blocks: list[dict[str, Any]] = []
        for tb in msg.thinking_blocks:
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": tb.thinking,
                    "signature": tb.signature,
                }
            )
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        for tu in msg.tool_uses:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tu.id,
                    "name": tu.name,
                    "input": tu.input,
                }
            )
        return {"role": "assistant", "content": blocks}

    @staticmethod
    def _anthropic_user_blocks(msg: Message) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        for tr in msg.tool_results:
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tr.tool_use_id,
                    "content": tr.content,
                    "is_error": tr.is_error,
                }
            )
        return blocks

    def _serialize_openai(self) -> list[dict[str, Any]]:
        """Flatten to OpenAI Responses `input` items.

        tool_use -> top-level {type: function_call, ...}
        tool_result -> top-level {type: function_call_output, ...}
        """
        out: list[dict[str, Any]] = []

        for msg in self.history:
            if msg.role == "assistant":
                if msg.content:
                    out.append(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": msg.content}
                            ],
                        }
                    )
                for tu in msg.tool_uses:
                    out.append(
                        {
                            "type": "function_call",
                            "name": tu.name,
                            "call_id": tu.id,
                            "arguments": _to_json(tu.input),
                        }
                    )
                continue

            # user turn
            if msg.content:
                out.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": msg.content}],
                    }
                )
            for tr in msg.tool_results:
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": tr.tool_use_id,
                        "output": tr.content,
                    }
                )

        return out


    def _serialize_openai_chat(self) -> list[dict[str, Any]]:
        """Flatten to OpenAI **Chat Completions** messages (DeepSeek uses this).

        Distinct from `_serialize_openai`, which targets the newer Responses
        API. Here each turn is a `{role, content, ...}` message:

          - assistant tool calls -> `tool_calls: [{id, type, function}]`
          - tool results         -> separate `{role: "tool", ...}` messages

        Thinking blocks are not sent back (DeepSeek expects reasoning_content to
        be omitted from history).
        """
        out: list[dict[str, Any]] = []

        for msg in self.history:
            if msg.role == "assistant":
                m: dict[str, Any] = {"role": "assistant"}
                if msg.content:
                    m["content"] = msg.content
                if msg.tool_uses:
                    m["tool_calls"] = [
                        {
                            "id": tu.id,
                            "type": "function",
                            "function": {
                                "name": tu.name,
                                "arguments": _to_json(tu.input),
                            },
                        }
                        for tu in msg.tool_uses
                    ]
                out.append(m)
                continue

            # user turn: tool results become role:"tool" messages
            for tr in msg.tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content,
                    }
                )
            if msg.content:
                out.append({"role": "user", "content": msg.content})

        return out


def _to_json(obj: Any) -> str:
    import json

    return json.dumps(obj or {})
