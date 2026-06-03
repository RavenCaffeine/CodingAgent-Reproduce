"""ch05 System Prompt tests — assembly, plan reminders, environment context."""

from __future__ import annotations

from mewcode.prompts import (
    PromptBuilder,
    PromptSection,
    build_environment_context,
    build_plan_mode_reminder,
    build_system_prompt,
)


# --- builder --------------------------------------------------------------- #


def test_builder_sorts_by_priority_and_chains() -> None:
    out = (
        PromptBuilder()
        .add(PromptSection("b", 50, "BBB"))
        .add(PromptSection("a", 10, "AAA"))
        .build()
    )
    assert out == "AAA\n\nBBB"  # priority asc, two-newline join


def test_builder_drops_empty_sections() -> None:
    out = (
        PromptBuilder()
        .add(PromptSection("a", 10, "AAA"))
        .add(PromptSection("blank", 20, "   "))
        .build()
    )
    assert out == "AAA"


# --- system prompt --------------------------------------------------------- #


def test_system_prompt_normal() -> None:
    out = build_system_prompt()
    assert "MewCode" in out
    assert "Plan mode" not in out  # plan goes via reminder, not system prompt
    # key phrases preserved
    assert "Be careful not to introduce security" in out
    assert "<system-reminder>" in out
    assert "Only use emojis if the user explicitly requests it" in out
    assert "file_path:line_number" in out
    assert "Do not use a colon before tool calls" in out


def test_system_prompt_deterministic() -> None:
    assert build_system_prompt(work_dir="/x") == build_system_prompt(work_dir="/x")


def test_system_prompt_optional_sections() -> None:
    out = build_system_prompt(
        custom_instructions="CUSTOM_RULES",
        skill_section="SKILL_BLOB",
        memory_section="MEMORY_BLOB",
        hook_prompts=["HOOK_ONE"],
    )
    assert "CUSTOM_RULES" in out
    assert "SKILL_BLOB" in out
    assert "MEMORY_BLOB" in out
    assert "# Hook Injected Context" in out and "HOOK_ONE" in out
    # empty optionals are dropped
    assert "CustomInstructions" not in build_system_prompt()


# --- plan mode reminders --------------------------------------------------- #


def test_system_prompt_plan() -> None:
    r = build_plan_mode_reminder("/tmp/plan.md", False, 1)
    assert "Plan mode" in r
    assert "MUST NOT" in r
    assert "WriteFile" in r  # file doesn't exist -> create


def test_plan_reminder_existing_file_uses_editfile() -> None:
    r = build_plan_mode_reminder("/tmp/plan.md", True, 1)
    assert "EditFile" in r


def test_plan_mode_sparse_reminder() -> None:
    r = build_plan_mode_reminder("/tmp/plan.md", False, 8)
    assert "Plan mode still active" in r


# --- environment context --------------------------------------------------- #


def test_environment_context() -> None:
    out = build_environment_context(work_dir="/home/user/project")
    assert "/home/user/project" in out
    assert "Operating system" in out
    assert "Current time" in out


def test_environment_section_in_system_prompt() -> None:
    out = build_system_prompt(work_dir="/repo/here")
    assert "# Environment" in out
    assert "/repo/here" in out
