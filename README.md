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

## Setup (uv)

Uses [uv](https://docs.astral.sh/uv/) for the environment. Dependencies live in
`requirements.txt` (`anthropic`, `openai`, `pyyaml`, `pydantic` + dev tools) so
new ones are easy to add. No system dependencies — Glob/Grep are pure Python.

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

## Config (YAML)

Four core fields — `protocol` / `model` / `base_url` / `api_key` — plus an
optional `thinking`. Keep exactly one provider block active; comment the others
out. `create_client` routes by `protocol`, so switching provider is config-only
(no code change). See `config.example.yaml` for all three blocks side by side.

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
| `/exit` (or `/quit`, `:q`) | Quit MewCode (prints a token-usage summary). |
| `Ctrl-C` | Cancel the current turn (mid-stream or mid-tool); the session stays alive. Press it at the prompt to quit. |

## Tests

```bash
uv run pytest -q
```
