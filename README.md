# MewCode

**English** | [中文](README.zh-CN.md)

A terminal AI assistant (Coding Agent) in Python, à la Claude Code. This step
delivers **pure streaming chat**: launch in the terminal, type a question, and
the model's reply prints token by token. Multi-turn — it remembers the
conversation.

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
- No tool use / file editing yet — chat only.

## Setup (uv)

Uses [uv](https://docs.astral.sh/uv/) for the environment. Dependencies live in
`requirements.txt` so new ones are easy to add.

```bash
uv venv                              # create .venv
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
