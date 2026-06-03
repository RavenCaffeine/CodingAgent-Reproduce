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
  `Bash` / `Glob` / `Grep`) plus `ToolSearch` and `AskUserQuestion`. Single
  round per turn — the model calls tools once, gets results, then answers (the
  automatic multi-step loop comes next chapter).

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
back, and the model answers based on them. **Single round per turn** — it calls
tools once, then responds (the automatic multi-step loop comes next chapter).

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

## Run

```bash
uv run python -m mewcode            # uses ./config.yaml
uv run python -m mewcode my.yaml    # or an explicit path
```

Type your message; `/exit` quits.

## Tests

```bash
uv run pytest -q
```
