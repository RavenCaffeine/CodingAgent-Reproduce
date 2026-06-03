"""System prompt and Plan Mode reminder construction.

ch04 keeps these minimal — just enough to drive the loop. Richer prompt
assembly (environment context, long-term memory, coordinator variants) lands in
later chapters.
"""

from __future__ import annotations

_BASE_SYSTEM = (
    "You are MewCode, a terminal AI coding assistant. You can read, write, and "
    "edit files, run shell commands, and search the codebase using the provided "
    "tools. Work step by step: call tools to gather information and make changes, "
    "then explain what you did. Prefer precise edits over rewriting whole files."
)

_COORDINATOR_SUFFIX = (
    "\n\nYou are operating in coordinator mode: break the task into steps and "
    "delegate where appropriate, keeping the user informed of the overall plan."
)

# Plan Mode reminders -------------------------------------------------------- #

_PLAN_MODE_FULL_REMINDER = (
    "<system-reminder>\n"
    "PLAN MODE is ON. Investigate and produce a plan — do NOT make changes.\n"
    "Only read-only tools (ReadFile, Glob, Grep) are available; write and "
    "command tools (WriteFile, EditFile, Bash) are blocked.\n"
    "Write your plan to: {plan_path}\n"
    "When the user is ready to execute, they will run /do to leave plan mode.\n"
    "</system-reminder>"
)

_PLAN_MODE_SPARSE_REMINDER = (
    "<system-reminder>\n"
    "Reminder: still in PLAN MODE — read-only tools only, no changes.\n"
    "</system-reminder>"
)

# Cadence: full reminder on iteration 1 and every Nth iteration; sparse between.
_REMINDER_INTERVAL = 5


def build_system_prompt(
    *, plan_mode: bool = False, coordinator_mode: bool = False
) -> str:
    """Build the minimal system prompt for one turn."""
    prompt = _BASE_SYSTEM
    if coordinator_mode:
        prompt += _COORDINATOR_SUFFIX
    if plan_mode:
        prompt += (
            "\n\nPLAN MODE is active: investigate and plan only; do not modify "
            "files or run commands."
        )
    return prompt


def build_plan_mode_reminder(
    plan_path: str, plan_exists: bool, iteration: int
) -> str:
    """Reminder text injected each turn while in Plan Mode.

    Full reminder on the first iteration and every `_REMINDER_INTERVAL`
    iterations; a sparse one-liner in between.
    """
    if iteration == 1 or iteration % _REMINDER_INTERVAL == 0:
        return _PLAN_MODE_FULL_REMINDER.format(plan_path=plan_path)
    return _PLAN_MODE_SPARSE_REMINDER
