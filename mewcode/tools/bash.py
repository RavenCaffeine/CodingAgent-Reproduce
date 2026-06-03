"""Bash tool — run a shell command with a timeout."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

MAX_TIMEOUT = 600


class Params(BaseModel):
    command: str = Field(description="Shell command to run.")
    timeout: int = Field(default=120, description="Timeout in seconds.")


class Bash(Tool):
    name = "Bash"
    description = "Run a shell command and capture stdout, stderr, and exit code."
    params_model = Params
    category = "command"

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        timeout = min(params.timeout, MAX_TIMEOUT)
        proc = await asyncio.create_subprocess_shell(
            params.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                f"Error: command timed out after {timeout}s", is_error=True
            )

        stdout = stdout_b.decode("utf-8", errors="replace").rstrip()
        stderr = stderr_b.decode("utf-8", errors="replace").rstrip()

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        output = "\n\n".join(parts) if parts else "(no output)"

        return ToolResult(output, is_error=(proc.returncode != 0))
