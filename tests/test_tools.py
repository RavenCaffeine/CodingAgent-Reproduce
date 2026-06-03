"""ch03 tool tests — six core tools, registry, and schema export."""

from __future__ import annotations

import pytest

from mewcode.cache import FileCache
from mewcode.tools import create_default_registry
from mewcode.tools.bash import Bash
from mewcode.tools.edit_file import EditFile
from mewcode.tools.glob import Glob
from mewcode.tools.grep import Grep
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile

CORE = {"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep"}


# --- registry -------------------------------------------------------------- #


def test_default_registry_has_six_core_tools() -> None:
    r = create_default_registry()
    assert {t.name for t in r.list_tools()} == CORE


def test_get_unknown_tool_raises() -> None:
    r = create_default_registry()
    with pytest.raises(KeyError, match="Unknown tool: Nope"):
        r.get("Nope")


def test_schema_shapes_per_protocol() -> None:
    r = create_default_registry()
    a = r.get_all_schemas("anthropic")[0]
    assert set(a) == {"name", "description", "input_schema"}
    o = r.get_all_schemas("openai")[0]
    assert o["type"] == "function" and "parameters" in o
    d = r.get_all_schemas("deepseek")[0]
    assert d["type"] == "function" and set(d["function"]) == {
        "name", "description", "parameters"
    }


# --- ReadFile / WriteFile -------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_file_numbers_lines(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    res = await ReadFile().execute(ReadFile.params_model(file_path=str(f)))
    assert res.output == "1\talpha\n2\tbeta\n3\tgamma"
    assert res.is_error is False


@pytest.mark.asyncio
async def test_read_file_missing_is_error(tmp_path) -> None:
    res = await ReadFile().execute(
        ReadFile.params_model(file_path=str(tmp_path / "nope"))
    )
    assert res.is_error is True


@pytest.mark.asyncio
async def test_read_file_offset_limit(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\nl4\n")
    res = await ReadFile().execute(
        ReadFile.params_model(file_path=str(f), offset=1, limit=2)
    )
    assert res.output == "2\tl2\n3\tl3"


@pytest.mark.asyncio
async def test_write_then_read_roundtrip(tmp_path) -> None:
    cache = FileCache()
    target = tmp_path / "sub" / "deep" / "out.txt"
    w = await WriteFile(cache).execute(
        WriteFile.params_model(file_path=str(target), content="hi\nthere")
    )
    assert "Successfully wrote" in w.output
    assert target.read_text() == "hi\nthere"


# --- EditFile -------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_edit_unique_match(tmp_path) -> None:
    f = tmp_path / "c.txt"
    f.write_text("hello world")
    res = await EditFile().execute(
        EditFile.params_model(file_path=str(f), old_string="world", new_string="there")
    )
    assert res.is_error is False
    assert f.read_text() == "hello there"


@pytest.mark.asyncio
async def test_edit_no_match_is_error_and_no_write(tmp_path) -> None:
    f = tmp_path / "c.txt"
    f.write_text("abc")
    res = await EditFile().execute(
        EditFile.params_model(file_path=str(f), old_string="zzz", new_string="x")
    )
    assert res.is_error is True
    assert "not found" in res.output
    assert f.read_text() == "abc"


@pytest.mark.asyncio
async def test_edit_multiple_match_is_error_and_no_write(tmp_path) -> None:
    f = tmp_path / "c.txt"
    f.write_text("x x x")
    res = await EditFile().execute(
        EditFile.params_model(file_path=str(f), old_string="x", new_string="y")
    )
    assert res.is_error is True
    assert "found 3 times, must be unique" in res.output
    assert f.read_text() == "x x x"


# --- Bash ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_bash_stdout_and_exit() -> None:
    res = await Bash().execute(Bash.params_model(command="echo hello"))
    assert "STDOUT:" in res.output and "hello" in res.output
    assert res.is_error is False


@pytest.mark.asyncio
async def test_bash_nonzero_is_error() -> None:
    res = await Bash().execute(Bash.params_model(command="exit 3"))
    assert res.is_error is True


@pytest.mark.asyncio
async def test_bash_timeout() -> None:
    res = await Bash().execute(Bash.params_model(command="sleep 5", timeout=1))
    assert res.is_error is True
    assert "timed out" in res.output


# --- Glob / Grep ----------------------------------------------------------- #


@pytest.mark.asyncio
async def test_glob_lists_and_skips(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("y")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("z")
    res = await Glob().execute(
        Glob.params_model(pattern="**/*.py", path=str(tmp_path))
    )
    assert res.output == "a.py\nb.py"  # sorted, pycache skipped


@pytest.mark.asyncio
async def test_glob_no_match(tmp_path) -> None:
    res = await Glob().execute(
        Glob.params_model(pattern="**/*.rs", path=str(tmp_path))
    )
    assert res.output == "No files matched the pattern."


@pytest.mark.asyncio
async def test_grep_finds_with_location(tmp_path) -> None:
    (tmp_path / "x.py").write_text("import os\nasync def execute():\n    pass\n")
    res = await Grep().execute(
        Grep.params_model(pattern="async def", path=str(tmp_path), include="*.py")
    )
    assert "x.py:2:async def execute():" in res.output


@pytest.mark.asyncio
async def test_grep_invalid_regex_is_error(tmp_path) -> None:
    res = await Grep().execute(Grep.params_model(pattern="(", path=str(tmp_path)))
    assert res.is_error is True


@pytest.mark.asyncio
async def test_grep_no_match(tmp_path) -> None:
    (tmp_path / "x.py").write_text("nothing here")
    res = await Grep().execute(
        Grep.params_model(pattern="zzzz", path=str(tmp_path))
    )
    assert res.output == "No matches found."
