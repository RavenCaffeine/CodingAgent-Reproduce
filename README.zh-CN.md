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
  `Glob` / `Grep`）+ `ToolSearch` + `AskUserQuestion`。
- **Agent Loop（ReAct）**：多轮循环——调模型、跑它要的工具、回灌结果、再来一轮，
  直到不再请求工具；读类并发、写/命令类串行，最多 50 轮，支持 `/plan` 只读规划模式
  与 Ctrl-C 取消。
- **模块化系统提示词**：system prompt 由按优先级排序的多段拼装（身份、安全、工具
  使用、语气、输出风格、环境……）；稳定规则留在可缓存的提示词里，易变内容（环境
  信息、plan 提醒）走对话通道注入，不让缓存失效。
- **权限系统（纵深防御）**：危险命令黑名单、文件路径沙箱、`ToolName(glob)` 放行/拒绝
  规则、多档权限模式，逐层把关每次工具调用。没明确命中就交给用户决定（HITL）；黑名单
  与沙箱是硬底线，任何档位都绕不过。

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
**Agent Loop** 会按需重复多轮（上限 50），读类工具并发、写/命令类串行。输入
`/plan` 进入只读规划模式、`/do` 恢复执行；Ctrl-C 取消当前回合。

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

## 系统提示词

System prompt 由一组按优先级排序的小段 `PromptSection`（`mewcode/prompts.py`）拼成
——身份、系统上下文、做任务规则、执行动作、工具使用、语气风格、文本输出，最后是
环境段。后续章节往里加段（自定义指令、Skill、记忆）而不动拼装逻辑。保持确定性，
稳定前缀才能被 provider 缓存命中。

易变内容不进稳定前缀，改走**对话通道**：

- **环境信息**（工作目录、OS、时间）启动时经 `inject_environment` 作为首条
  `<system-reminder>` 注入一次，环境变化不会让缓存失效。
- **Plan 模式提醒**按轮注入（首轮完整、之后稀疏一行、每隔几轮再完整），而不是写死
  进 system prompt。

## 权限

每次工具调用执行前都要过一层纵深安全检查（`mewcode/permissions/`）：

1. **危险命令黑名单** —— `rm -rf /`、fork 炸弹、`curl | sh`、磁盘格式化等直接拒绝。
2. **路径沙箱** —— 文件类工具只能落在项目根 + 临时目录内；越界路径（含软链解析后）拒绝。
3. **规则** —— `ToolName(glob)` 的 allow/deny 规则，来自三层 YAML
   （`~/.mewcode/permissions.yaml` < 项目 `.mewcode/permissions.yaml` <
   `.mewcode/permissions.local.yaml`，后者覆盖前者）。
4. **权限模式** —— 落到「模式 × 工具类别」矩阵兜底。

都没决定时 MewCode 会**问你**（`[y] 本次允许`、`[A] 永久允许`、`[n] 拒绝`）。选
*永久允许* 会把一条规则写进 `.mewcode/permissions.local.yaml`，同类调用以后不再问。
**黑名单和沙箱是硬底线**——即便 `bypass` 档也跑不了 `rm -rf /`、逃不出沙箱。

档位：`default`（读放行、写/命令询问）、`acceptEdits`（写放行）、`plan`（只读）、
`bypassPermissions`（除硬底线外全放）、`dontAsk`、`custom`。用 `/mode <名称>` 或直接
命令切换——`/default`、`/acceptEdits`、`/plan`、`/bypassPermissions`、`/dontAsk`、
`/custom`。`/mode`（无参）显示当前档位并列出全部。

## 运行

```bash
uv run python -m mewcode            # 用 ./config.yaml
uv run python -m mewcode my.yaml    # 或指定路径
```

### 会话内命令

| 命令 | 作用 |
|------|------|
| *（直接输入消息）* | 发给 agent；它会流式回复，并可能跨多轮调用工具。 |
| `/plan` | 进入**规划模式**（只读）。写/命令类工具（WriteFile、EditFile、Bash）被拦截，模型只调研并给出计划。 |
| `/do`（或 `/plan off`） | 退出规划模式，恢复正常执行。 |
| `/mode` | 查看当前权限档位并列出全部档位。 |
| `/default` `/acceptEdits` `/plan` `/bypassPermissions` `/dontAsk` `/custom` | 直接切到对应权限档位（也可用 `/mode <名称>`）。 |
| `/exit`（或 `/quit`、`:q`） | 退出 MewCode（打印 token 用量小结）。 |
| `Ctrl-C` | 取消当前回合（流式中或工具执行中），会话不退出；在输入提示符处按则退出程序。 |

## 测试

```bash
uv run pytest -q
```
