"""Deterministic demo of the two context-management layers (ch08).

Run it directly — no API key, no huge inputs needed:

    python scripts/demo_compaction.py

It exercises:
  * Layer 1 (apply_tool_result_budget): spill a large tool result to disk and
    show the <persisted-output> preview that replaces it in the prompt.
  * Layer 2 (auto_compact): summarize the OLDER part of a long conversation
    while keeping the recent tail verbatim, using a fake summary client.

To watch the layers fire on SMALLER inputs (e.g. while clicking around the real
app), temporarily lower the module constants in mewcode/context/manager.py:
  Layer 1:  SINGLE_RESULT_CHAR_LIMIT = 500   AGGREGATE_CHAR_LIMIT = 2_000
  Layer 2:  KEEP_RECENT_TOKENS = 500   MIN_KEEP_MESSAGES = 2   KEEP_MAX_TOKENS = 2_000
and set a small context_window in config.yaml (e.g. context_window: 5000 ->
auto threshold 2500). Then any `ls -la` spills, and a few turns auto-compact.
"""

from __future__ import annotations

import asyncio
import tempfile

from mewcode.context.manager import (
    AGGREGATE_CHAR_LIMIT,
    KEEP_MAX_TOKENS,
    KEEP_RECENT_TOKENS,
    SINGLE_RESULT_CHAR_LIMIT,
    apply_tool_result_budget,
    auto_compact,
    compute_compact_threshold,
    create_replacement_state,
    ensure_session_dir,
    _split_keep_recent,
)
from mewcode.conversation import ConversationManager, Message, ToolResultBlock
from mewcode.tools.base import TextDelta


def demo_layer1() -> None:
    print("=" * 70)
    print(f"Layer 1  (single>{SINGLE_RESULT_CHAR_LIMIT:,} chars or "
          f"aggregate>{AGGREGATE_CHAR_LIMIT:,} chars -> spill to disk)")
    print("=" * 70)
    session_dir = ensure_session_dir(tempfile.mkdtemp())
    state = create_replacement_state()

    big = "LINE of fake tool output\n" * 3000  # ~75 KB > 50 KB single limit
    conv = ConversationManager()
    conv.history.append(
        Message(role="user", tool_results=[ToolResultBlock("bash-1", big)])
    )
    print(f"  original tool result: {len(big):,} chars")

    api_conv, records = apply_tool_result_budget(conv, session_dir, state)
    new = api_conv.history[0].tool_results[0].content
    print(f"  spilled file:  {session_dir / 'bash-1.txt'}  "
          f"({(session_dir / 'bash-1.txt').exists()})")
    print(f"  prompt now sees ({len(new)} chars):")
    for line in new.splitlines()[:5]:
        print("    | " + line)
    print("    | …")
    print(f"  original conversation untouched: "
          f"{conv.history[0].tool_results[0].content == big}")
    print(f"  records written: {len(records)}\n")


async def demo_layer2() -> None:
    print("=" * 70)
    print(f"Layer 2  (keep recent ~{KEEP_RECENT_TOKENS:,} tokens verbatim, "
          f"cap {KEEP_MAX_TOKENS:,}; summarize the rest)")
    print("=" * 70)
    session_dir = ensure_session_dir(tempfile.mkdtemp())

    conv = ConversationManager()
    body = "alpha beta gamma delta epsilon " * 200
    for i in range(40):
        conv.add_user_message(f"用户问题 {i}")
        conv.add_assistant_message(f"{body} 回答 {i}")
    conv.last_input_tokens = 180_000  # pretend the last request was this big

    window = 200_000
    print(f"  context_window={window:,}  auto threshold="
          f"{compute_compact_threshold(window):,}  "
          f"last_input_tokens={conv.last_input_tokens:,}")
    print(f"  conversation: {len(conv.history)} messages")
    old, recent = _split_keep_recent(conv.history)
    print(f"  split -> {len(old)} old (summarized) + {len(recent)} recent (kept)")

    class FakeClient:
        async def stream(self, conversation, system, tools):
            yield TextDelta("<analysis>x</analysis>"
                            "<summary>梳理了 MewCode 的代码结构与压缩流程。</summary>")

    evt = await auto_compact(conv, FakeClient(), window, session_dir)
    print(f"  compacted: before={evt.before_tokens:,} tokens")
    print(f"  new history: {len(conv.history)} messages")
    print(f"    [0] {conv.history[0].content[:24]!r} …")
    print(f"    [1] role={conv.history[1].role} (boundary)")
    print(f"    newest turn kept verbatim: "
          f"{any('回答 39' in m.content for m in conv.history)}")
    print(f"    oldest turn folded into summary: "
          f"{not any(m.content == '用户问题 0' for m in conv.history)}\n")


def main() -> None:
    demo_layer1()
    asyncio.run(demo_layer2())


if __name__ == "__main__":
    main()
