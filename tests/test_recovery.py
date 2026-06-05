"""Tests for ch08 post-compact recovery (RecoveryState + attachment)."""

from __future__ import annotations

from mewcode.context.manager import (
    RECOVERY_FILE_LIMIT,
    RecoveryState,
    build_recovery_attachment,
)


def test_recovery_attachment_empty_when_nothing_recorded():
    assert build_recovery_attachment(RecoveryState(), None) == ""
    assert build_recovery_attachment(None, None) == ""


def test_recovery_attachment_emits_all_sections():
    state = RecoveryState()
    state.record_file_read("/a/b.py", "print(1)")
    state.record_skill_invocation("pdf", "SOP: how to pdf")
    schemas = [{"name": "ReadFile", "description": "Read a file\nmore"}]
    out = build_recovery_attachment(state, schemas)
    assert "## 最近读过的文件" in out
    assert "- /a/b.py" in out  # path listed (no content embedded)
    assert "## 已激活的技能" in out
    assert "pdf" in out and "SOP" in out  # skills still embed their SOP body
    assert "## 可用工具" in out
    assert "- ReadFile — Read a file" in out  # first line only
    assert out.rstrip().endswith("不要根据本摘要臆造不存在的代码。")  # 提示 last


def test_recovery_files_are_paths_only_not_content():
    # File CONTENT must not be re-embedded (it would defeat the summary).
    state = RecoveryState()
    huge = "SECRET_FILE_BODY " * 5000
    state.record_file_read("/big.txt", huge)
    out = build_recovery_attachment(state, None)
    assert "- /big.txt" in out  # path listed
    assert "SECRET_FILE_BODY" not in out  # content NOT embedded
    assert len(out) < 500  # tiny — just the path + tip


def test_recovery_skips_files_in_recent_tail():
    state = RecoveryState()
    state.record_file_read("/kept.py", "x")
    state.record_file_read("/old.py", "y")
    # /kept.py is still in the kept recent tail -> must be skipped (no dup)
    out = build_recovery_attachment(state, None, skip_paths={"/kept.py"})
    assert "/old.py" in out
    assert "/kept.py" not in out


def test_recovery_file_limit_and_order():
    state = RecoveryState()
    for i in range(RECOVERY_FILE_LIMIT + 3):
        state.record_file_read(f"/f{i}.txt", f"content {i}")
    files = state.snapshot_files(RECOVERY_FILE_LIMIT)
    assert len(files) == RECOVERY_FILE_LIMIT
    # most recent first
    assert files[0].path == f"/f{RECOVERY_FILE_LIMIT + 2}.txt"


def test_recovery_skills_budget():
    state = RecoveryState()
    # many big skills; total exceeds RECOVERY_SKILLS_BUDGET so some are dropped
    for i in range(20):
        state.record_skill_invocation(f"skill{i}", "Y" * 80_000)
    out = build_recovery_attachment(state, None)
    listed = out.count("### skill")
    assert 0 < listed < 20  # budget cut some off
