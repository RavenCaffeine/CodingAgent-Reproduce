# MewCode

[English](README.md) | **中文**

一个用 Python 写的终端 AI 助手（Coding Agent），类似 Claude Code。在终端启动后
输入问题，模型的回复逐字流式打印出来。支持多轮对话（会记住上下文），并且**会用
工具**：能读、写、改文件，执行 shell 命令，检索代码库。

## 功能

- 交互式终端对话循环（TUI），多轮记忆。
- 通过 SSE 流式输出 —— token 边到边打印，而不是等全部生成完。
- 统一的 `LLMClient` 接口下接三种后端，改配置即可切换：
  - **Anthropic Claude**（Messages API）
  - **OpenAI**（Responses API）
  - **DeepSeek** V4（OpenAI Chat Completions API）
- Claude **扩展思考**（opus/sonnet-4-6+ 走 adaptive，其余固定 budget）；DeepSeek
  的 **reasoning** 同样会流式显示。两者均以暗色呈现。
- **工具系统**：六个核心工具（`ReadFile` / `WriteFile` / `EditFile` / `Bash` /
  `Glob` / `Grep`）+ `ToolSearch` + `AskUserQuestion`。每回合单轮——模型调用一次
  工具、拿到结果后作答（自动多步循环留到下一章）。

## 安装（uv）

使用 [uv](https://docs.astral.sh/uv/) 管理环境。依赖写在 `requirements.txt` 里
（`anthropic`、`openai`、`pyyaml`、`pydantic` + 开发工具），方便后续增添。无系统
依赖 —— Glob/Grep 是纯 Python 实现。

```bash
uv venv                              # 创建 .venv

# 激活虚拟环境：
.venv\Scripts\activate               # Windows（PowerShell / cmd）
source .venv/bin/activate            # Linux / macOS

uv pip install -r requirements.txt   # 安装依赖
cp config.example.yaml config.yaml   # 然后按需修改
copy config.example.yaml config.yaml # win
```

后续加依赖：往 `requirements.txt` 追加一行，再执行
`uv pip install -r requirements.txt`。

## 配置（YAML）

四个核心字段 —— `protocol` / `model` / `base_url` / `api_key`，外加可选的
`thinking`。只让一组 provider 处于启用状态，其余整段注释掉。`create_client` 按
`protocol` 路由，所以切换 provider 只改配置、不动代码。三套配置并排见
`config.example.yaml`。

**Anthropic Claude：**

```yaml
protocol: anthropic                  # 走哪家协议
model: claude-sonnet-4-6             # claude-opus-4-6 / claude-sonnet-4-6 / ...
base_url: https://api.anthropic.com  # 可选；用代理/网关时改这里
api_key: ${ANTHROPIC_API_KEY}        # 明文，或 ${环境变量}
thinking: true                       # 扩展思考（仅 Anthropic 有效）
```

**OpenAI（Responses API）：**

```yaml
protocol: openai                     # 走哪家协议
model: gpt-4.1                       # gpt-4.1 / gpt-4o / o4-mini / ...
base_url: https://api.openai.com/v1  # 可选；兼容网关时改这里
api_key: ${OPENAI_API_KEY}           # 明文，或 ${环境变量}
thinking: false                      # OpenAI 忽略此项 —— 仅 Claude 支持
```

**DeepSeek V4（Chat Completions API）：**

```yaml
protocol: deepseek                   # 走哪家协议
model: deepseek-v4-pro               # deepseek-v4-pro / deepseek-v4-flash
base_url: https://api.deepseek.com   # 可选；这是默认值
api_key: ${DEEPSEEK_API_KEY}         # 明文，或 ${环境变量}
thinking: false                      # thinking-mode 模型的 reasoning 会自动流式显示
```

### API key 的三种写法（手填或导入）

1. **手动填入** —— 直接写明文：`api_key: sk-ant-abc123`
2. **环境导入** —— `api_key: ${ANTHROPIC_API_KEY}` 从该环境变量读取。
3. **显式声明** —— `api_key_env: ANTHROPIC_API_KEY`（效果相同）。

三者都不写时，按 protocol 自动回退到约定变量（`ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `DEEPSEEK_API_KEY`），所以 `export` 完直接跑也行。优先级：
**手动填入 > 环境变量**。

运行前设置环境变量：`export ANTHROPIC_API_KEY=sk-ant-...`
（PowerShell：`$env:ANTHROPIC_API_KEY="sk-ant-..."`）。

## 工具

模型可以调用工具来操作你的项目：工具执行后，结果回灌给模型，模型据此作答。
**每回合单轮**——模型调用一次工具就停下作答（自动多步循环留到下一章）。

| 工具 | 作用 |
|------|------|
| `ReadFile` | 读文本文件，按 1 起始行号输出（支持 `offset` / `limit`）。 |
| `WriteFile` | 写文件，自动创建父目录。 |
| `EditFile` | 替换**唯一**匹配的字符串（匹配不到或匹配多次都报错）。 |
| `Bash` | 带超时执行 shell 命令；捕获 stdout/stderr/退出码。 |
| `Glob` | 按 glob 列文件（跳过 `.git`、`node_modules` 等）。 |
| `Grep` | 用正则搜文件内容，返回 `file:line:text`。 |

另有两个：**`ToolSearch`** 对 *deferred*（延迟披露）工具做渐进披露
（`select:Name1,Name2` 或关键词），**`AskUserQuestion`** 让模型向你提结构化问题。
工具 Schema 由 Pydantic 模型自动生成，并按各 Provider 期望的形状导出（Anthropic
的 `input_schema`、OpenAI Responses 的扁平 function、DeepSeek Chat Completions
的嵌套 `function`）。工具失败以结构化错误返回，模型可据此重试而不会崩溃。

## 运行

```bash
uv run python -m mewcode            # 用 ./config.yaml
uv run python -m mewcode my.yaml    # 或指定路径
```

输入消息开始对话；`/exit` 退出。

## 测试

```bash
uv run pytest -q
```
