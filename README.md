# MewCode

**English** | [中文](README.zh-CN.md)

A terminal AI assistant (Coding Agent) in Python, à la Claude Code. Launch in
the terminal, type a question, and the model's reply streams token by token.
Multi-turn (it remembers the conversation) and **tool-using**: it can read,
write, and edit files, run shell commands, and search the codebase.

## Features

- Interactive terminal chat loop (TUI) with multi-turn memory.
- Streaming output via SSE — tokens print as they arrive, not after the full
  response.
- Three backends behind one unified `LLMClient` interface — switch via config:
  - **Anthropic Claude** (Messages API)
  - **OpenAI** (Responses API)
  - **DeepSeek** V4 (OpenAI Chat Completions API)
- Claude **extended thinking** (adaptive on opus/sonnet-4-6+, fixed-budget
  fallback otherwise); DeepSeek **reasoning** is streamed too. Both rendered
  dimmed.
- **Tool system**: six core tools (`ReadFile` / `WriteFile` / `EditFile` /
  `Bash` / `Glob` / `Grep`) plus `ToolSearch` and `AskUserQuestion`.
- **Agent Loop (ReAct)**: a multi-round loop calls the model, runs the tools it
  asks for, feeds results back, and repeats until it stops asking — with
  read-concurrent / write-serial batching, a max-iterations cap (50), `/plan`
  mode (read-only planning), and Ctrl-C cancellation.
- **Modular system prompt**: the system prompt is assembled from priority-ordered
  sections (identity, safety, tool usage, tone, output style, environment, …);
  stable rules stay in the cacheable prompt while volatile context (environment
  info, plan reminders) is injected through the conversation channel.
- **Permission system (defense in depth)**: a dangerous-command blacklist, a
  filesystem sandbox, glob allow/deny rules, and permission modes gate every
  tool call. Unknown cases ask the user (HITL); the blacklist and sandbox are a
  hard floor that no mode can bypass.
- **MCP client**: connects external [Model Context Protocol](https://modelcontextprotocol.io)
  servers at startup (stdio or Streamable HTTP) and exposes their tools as
  `mcp_<server>_<tool>`, alongside the built-ins — and they pass the same
  permission checks.
- **Context management**: two layers keep long sessions inside the window —
  oversized tool results spill to disk (replaced by a stable preview + path),
  and when history nears the limit the whole conversation is summarized, with
  recently-read files re-attached so the model doesn't lose its place. `/compact`
  triggers it manually.

## Setup (uv)

Uses [uv](https://docs.astral.sh/uv/) for the environment. Dependencies live in
`requirements.txt` (`anthropic`, `openai`, `pyyaml`, `pydantic`, `mcp`, `httpx`,
`tiktoken` + dev tools) so new ones are easy to add. No system dependencies —
Glob/Grep are pure Python (MCP stdio servers may need their own runtime, e.g.
`npx`).

```bash
uv venv                              # create .venv

# activate it:
.venv\Scripts\activate              # Windows (PowerShell / cmd)
source .venv/bin/activate            # Linux / macOS

uv pip install -r requirements.txt   # install deps
cp config.example.yaml config.yaml   # then edit it
```

Add a dependency later: append it to `requirements.txt`, then re-run
`uv pip install -r requirements.txt`.

At runtime MewCode writes working state under `.mewcode/` in the project (plan
files, permission rules, and the context-management spill directory
`.mewcode/session/tool-results/` plus `replacement_records.jsonl`). The whole
`.mewcode/` tree is gitignored — nothing there needs to be committed.

## Config (YAML)

Four core fields — `protocol` / `model` / `base_url` / `api_key` — plus an
optional `thinking`. Keep exactly one provider block active; comment the others
out. `create_client` routes by `protocol`, so switching provider is config-only
(no code change). See `config.example.yaml` for all three blocks side by side.
An optional `mcp_servers` block connects external MCP servers — see
[MCP servers](#mcp-servers) below.

**Anthropic Claude:**

```yaml
protocol: anthropic                  # which protocol
model: claude-sonnet-4-6             # claude-opus-4-6 / claude-sonnet-4-6 / ...
base_url: https://api.anthropic.com  # optional; for proxies/gateways
api_key: ${ANTHROPIC_API_KEY}        # literal, or ${ENV_VAR}
thinking: true                       # extended thinking (Anthropic only)
```

**OpenAI (Responses API):**

```yaml
protocol: openai                     # which protocol
model: gpt-4.1                       # gpt-4.1 / gpt-4o / o4-mini / ...
base_url: https://api.openai.com/v1  # optional; for compatible gateways
api_key: ${OPENAI_API_KEY}           # literal, or ${ENV_VAR}
thinking: false                      # OpenAI ignores this — Claude-only feature
```

**DeepSeek V4 (Chat Completions API):**

```yaml
protocol: deepseek                   # which protocol
model: deepseek-v4-pro               # deepseek-v4-pro / deepseek-v4-flash
base_url: https://api.deepseek.com   # optional; this is the default
api_key: ${DEEPSEEK_API_KEY}         # literal, or ${ENV_VAR}
thinking: false                      # reasoning streams automatically on
                                     # thinking-mode models
```

### API key — three ways (manual or import)

1. **Manual literal** — paste the key straight in:
   `api_key: sk-ant-abc123`
2. **Env import** — `api_key: ${ANTHROPIC_API_KEY}` reads from that variable.
3. **Explicit env field** — `api_key_env: ANTHROPIC_API_KEY` (same effect).

If you omit the key entirely, it falls back to the provider's conventional
variable (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`), so a
plain `export`-then-run works with no key line. A manual literal always wins
over an environment variable.

Set an env var before running:
`export ANTHROPIC_API_KEY=sk-ant-...` (PowerShell:
`$env:ANTHROPIC_API_KEY="sk-ant-..."`).

## Tools

The model can call tools to act on your project: they execute, results are fed
back, and the model answers. The **Agent Loop** repeats this for as many rounds
as needed (capped at 50), running read-only tools concurrently and write/command
tools serially. Type `/plan` for read-only planning mode and `/do` to resume
execution; Ctrl-C cancels the current turn.

| Tool | What it does |
|------|--------------|
| `ReadFile` | Read a text file with 1-based line numbers (`offset` / `limit`). |
| `WriteFile` | Write a file, creating parent directories. |
| `EditFile` | Replace a **unique** occurrence of a string (errors if missing or ambiguous). |
| `Bash` | Run a shell command with a timeout; captures stdout/stderr/exit code. |
| `Glob` | List files matching a glob (skips `.git`, `node_modules`, …). |
| `Grep` | Search file contents by regex; returns `file:line:text`. |

Two extras: **`ToolSearch`** does progressive disclosure of *deferred* tools
(`select:Name1,Name2` or keywords), and **`AskUserQuestion`** lets the model ask
structured questions. Tool schemas are generated from Pydantic models and
exported in the shape each provider expects (Anthropic `input_schema`, OpenAI
Responses flat function, DeepSeek Chat-Completions nested `function`). Tool
failures come back as structured errors so the model can retry instead of
crashing.

## System prompt

The system prompt is built from small, priority-ordered `PromptSection`s
(`mewcode/prompts.py`) — identity, system context, doing-tasks rules, executing
actions, tool usage, tone/style, text output, then environment. Later chapters
add sections (custom instructions, skills, memory) without touching the assembly
logic. Keeping it deterministic means the stable prefix can be cached by the
provider.

Volatile context stays out of that stable prefix and rides the **conversation
channel** instead:

- **Environment context** (working directory, OS, time) is injected once as the
  first `<system-reminder>` message via `inject_environment`, so changing
  environment doesn't invalidate the cached prompt.
- **Plan-mode reminders** are injected per turn (full on the first turn, then a
  sparse one-liner, full again every few turns) rather than baked into the
  system prompt.

## Permissions

Every tool call passes a layered safety check (`mewcode/permissions/`) before it
runs:

1. **Dangerous-command blacklist** — `rm -rf /`, fork bombs, `curl | sh`, disk
   formatting, etc. are denied outright.
2. **Path sandbox** — file tools are confined to the project root + temp dir;
   out-of-sandbox paths (resolved through symlinks) are denied.
3. **Rules** — `ToolName(glob)` allow/deny rules from three YAML files
   (`~/.mewcode/permissions.yaml` < project `.mewcode/permissions.yaml` <
   `.mewcode/permissions.local.yaml`; later overrides earlier).
4. **Permission mode** — falls back to a mode × tool-category matrix.

When nothing decides, MewCode **asks you** (`[y] allow once`, `[A] allow
always`, `[n] deny`). Choosing *allow always* writes a rule into
`.mewcode/permissions.local.yaml` so the same call isn't asked again. **The
blacklist and sandbox are a hard floor** — even `bypass` mode can't run
`rm -rf /` or escape the sandbox.

Modes: `default` (read allow, write/command ask), `acceptEdits` (writes allow),
`plan` (read-only), `bypassPermissions` (allow all but the hard floor),
`dontAsk`, `custom`. Switch with `/mode <name>` or a direct command —
`/default`, `/acceptEdits`, `/plan`, `/bypassPermissions`, `/dontAsk`, `/custom`.
`/mode` with no argument shows the current mode and lists them all.

## MCP servers

MewCode can connect external [MCP](https://modelcontextprotocol.io) servers and
expose their tools to the model. Declare them under `mcp_servers` in your
`config.yaml` (key = server name); each server uses **exactly one** transport —
`command` (stdio subprocess) or `url` (Streamable HTTP). `headers` and `env`
values support `${VAR}` expansion.

```yaml
mcp_servers:
  context7:                       # stdio: local subprocess
    command: npx
    args: ["-y", "@upstash/context7-mcp"]
    env:                          # optional; passed to the child process
      LOG_LEVEL: info
  remote:                         # http: Streamable HTTP
    url: https://example.com/mcp
    headers:
      Authorization: "Bearer ${MCP_TOKEN}"
    timeout: 30                   # optional, seconds (default 30)
```

| Field | Transport | Meaning |
|-------|-----------|---------|
| `command` | stdio | Executable to launch the server (mutually exclusive with `url`). |
| `args` | stdio | Argument list for `command`. |
| `env` | stdio | Extra env for the child (merged onto a safe whitelist). |
| `url` | http | Streamable HTTP endpoint (mutually exclusive with `command`). |
| `headers` | http | Request headers; values support `${VAR}`. |
| `timeout` | both | Per-request timeout in seconds (default 30). |

At startup MewCode connects every server concurrently, registers each tool as
`mcp_<server>_<tool>`, and prints `Connected to N MCP server(s), M tools
registered`. A server that fails to connect only logs a warning and is skipped —
the rest still load. Discovered tools are deferred, so the model finds them via
`ToolSearch` and then calls them like any built-in (they go through the same
permission checks). stdio child processes inherit only a small env whitelist plus
the `env` you declare — host secrets like `ANTHROPIC_API_KEY` are never passed
through.

## Context management

Long sessions blow past the model's context window, mostly via tool results.
MewCode handles this in two layers, run before each API request:

**Layer 1 (prevention, no LLM).** Before every request, any tool result over
~50 000 characters is written to `.mewcode/session/tool-results/<id>.txt` and
replaced in the prompt by a stable `<persisted-output>` preview + the file path
(the model re-reads the file with `ReadFile` if it needs the full content). A
per-message aggregate cap (~200 000 chars) spills the largest results first, and
results in turns older than the last 10 are snipped to a short preview. Each
replace/keep decision is logged once and re-read byte-for-byte on later turns, so
Anthropic's prompt cache keeps hitting. The original conversation is never
mutated — only the copy sent to the API.

**Layer 2 (fallback, LLM).** Before each request the prompt size is estimated
with `tiktoken`; when the larger of that estimate and the last reported usage
crosses `window − 33 000` (≈167 K on a 200 K window, floored at half the window),
the **older** part of the conversation is summarized into a nine-section
structured summary while the **recent tail is kept verbatim** (the last
~10 000 tokens / ≥5 messages, capped at 40 000), so precise recent context isn't
lost. New history becomes `summary + boundary + recent originals`. Estimating up
front means compaction fires *before* an oversized request is sent, not only
after usage is reported. The **paths** of recently-read files, activated skill
SOPs, and the available tool list are re-attached after the summary so the model
keeps its bearings (file contents are not re-embedded — that would defeat the
summary; the model re-reads with `ReadFile` if needed). Repeated summary failures
trip a circuit breaker (3 in a row) that stops auto-triggering.

The window defaults to 200 K but is configurable per provider via
`context_window` in `config.yaml` — lower it when your effective budget (e.g. a
tight per-minute rate limit) is smaller than the model's nominal window, so
compaction kicks in earlier.

**Rate limits.** A `429` rate-limit response is different from a context
overflow: MewCode honors the provider's `retry-after` (or backs off
exponentially) and retries the request up to 3 times instead of failing the turn.

Both layers surface in the terminal as you work: each tool call prints its
primary argument (`Bash: find . -name "*.go"`, `ReadFile: mewcode/agent.py`), a
spill shows `◇ spilled N tool result(s) to disk (~X chars freed)`, and a
compaction shows `◇ Compacted: <before> → <after> estimated tokens`.

Type `/compact` (or `/c`) to compact manually; on a short session it just reports
the current token count and does nothing.

## Run

```bash
uv run python -m mewcode            # uses ./config.yaml
uv run python -m mewcode my.yaml    # or an explicit path
```

### In-session commands

| Command | Effect |
|---------|--------|
| *(type a message)* | Send it to the agent; it streams the reply and may run tools over multiple rounds. |
| `/plan` | Enter **plan mode** — read-only. Write/command tools (WriteFile, EditFile, Bash) are blocked; the model investigates and proposes a plan. |
| `/do` (or `/plan off`) | Leave plan mode and resume normal execution. |
| `/mode` | Show the current permission mode and list all modes. |
| `/default` `/acceptEdits` `/plan` `/bypassPermissions` `/dontAsk` `/custom` | Switch directly to that permission mode (or use `/mode <name>`). |
| `/compact` (or `/c`) | Summarize the conversation now to free up context (no-op on a short session). |
| `/exit` (or `/quit`, `:q`) | Quit MewCode (prints a token-usage summary). |
| `Ctrl-C` | Cancel the current turn (mid-stream or mid-tool); the session stays alive. Press it at the prompt to quit. |

## Tests

```bash
uv run pytest -q
```
