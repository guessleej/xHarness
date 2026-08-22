# xHarness

[English](README.md) | 中文

xHarness 是云碩科技（xCloudinfo）開發的插件式 AI agent harness。架構取法 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的「一切皆插件」，重新實作為精簡的 Python 套件，可對接**任何 OpenAI 相容端點**——llama.cpp（`llama-server`）、vLLM、Ollama、LiteLLM 或企業閘道。完全適合地端部署：除了你設定的模型端點之外，不會對外傳送任何資料。

## 為什麼做這個

多數 agent harness 綁死單一廠商 API，還拖著龐大的相依樹。xHarness 保留架構核心——工具、模型 adapter、session 記錄、agent 迴圈的組裝全部都是掛在共享 context 上的插件——但把規模控制在一個人一個下午讀得完：約 1,500 行 Python、**零執行期相依**（純標準函式庫，含 SSE 串流client與 TOML 設定讀取），測試跑完只要 0.1 秒。

## 特色

- **一切皆插件。** 服務、事件、工具註冊都掛在共享 `Context` 上；卸載插件時自動回捲它註冊的一切。
- **任何 OpenAI 相容供應端。** SSE 串流含 tool calls、憑證每次請求時才從環境變數解析、可設定 temperature 與 token 上限。
- **內建工具。** `bash`、`read`、`write`、`edit`、`glob`、`grep`、`todo_write`。
- **審批策略。** 有副作用的工具（`bash`、`write`、`edit`）在互動模式會先詢問；`--yes` 或 `approval = "auto"` 可關閉。
- **只增不改的 session 記錄。** 每則訊息與工具結果都以 JSONL 記錄在 `~/.xharness/sessions/`；`--resume <id>` 可接續。
- **兩種執行模式。** headless 一次性（`xharness "任務"`）與互動 REPL。
- **可擴充。** 使用者插件模組可從設定檔加入工具與服務；`llm/stream` 中介層可攔截每次模型呼叫做快取、記錄或路由。
- **零遙測。** 除了你設定的模型端點，xHarness 不對任何地方傳送資料。沒有匿名 id、沒有使用量上傳、沒有任何 phone-home。

## 快速開始

需要 Python 3.11 以上。

```sh
git clone https://github.com/guessleej/xHarness.git
cd xHarness
python3 -m venv .venv && ./.venv/bin/pip install -e .
```

指向任何 OpenAI 相容端點，最快的方式是環境變數：

```sh
export XHARNESS_BASE_URL=http://localhost:8080/v1
export XHARNESS_MODEL=your-model-id
./.venv/bin/xharness "列出這個目錄的檔案並摘要這個專案"
```

或複製 `xharness.example.zh.toml`（中文註解版）為 `xharness.toml` 後編輯：

```toml
default_provider = "local"
approval = "prompt"

[providers.local]
base_url = "http://localhost:8080/v1"
model = "your-model-id"
# api_key_env = "XHARNESS_API_KEY"
```

接著：

```sh
xharness              # 互動 REPL
xharness "任務"       # headless 一次性
xharness sessions     # 列出已存 session
xharness --resume 2026-08-22-ab12cd34   # 接續 session
```

## 內建工具

| 工具 | 有副作用 | 用途 |
|---|---|---|
| `bash` | 是 | 執行 shell 指令；輸出有上限、逾時有界限 |
| `read` | 否 | 讀檔並附行號，大檔可用 offset/limit |
| `write` | 是 | 寫檔，自動建立上層目錄 |
| `edit` | 是 | 精確字串取代；出現多處會拒絕 |
| `glob` | 否 | 以 glob 樣式（`**`、`*`、`?`）找檔案 |
| `grep` | 否 | 以正規表示式搜尋檔案內容 |
| `todo_write` | 否 | 維護多步驟任務的工作清單 |

審批只作用於有副作用的工具。headless 模式預設 `auto`（沒有人盯著管線）；互動模式預設 `prompt`。明確指定 `--approve prompt|auto` 一律優先。

## 寫一個插件

插件就是一個 Python 檔案，模組層級曝露 `plugin`：

```python
# plugins/word_count.py
from xharness import Plugin, Tool, ToolResult, register_tools

def _apply(ctx, config):
    tool = Tool(
        name="word_count",
        description="Count words in a string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        execute=lambda args, tool_ctx: ToolResult(str(len(str(args["text"]).split()))),
    )
    register_tools(ctx, [tool])

plugin = Plugin("word-count", _apply)
```

在 `xharness.toml` 掛載：

```toml
[[plugins]]
id = "word-count"
module = "./plugins/word_count.py"
```

中介層可透過 `llm/stream` 接縫包住每次模型呼叫：

```python
def _apply(ctx, config):
    def logger(payload, next_call):
        print(f"[llm] {len(payload['messages'])} 則訊息, {len(payload['tools'])} 個工具")
        return next_call(payload)
    ctx.intercept("llm/stream", logger)

plugin = Plugin("call-logger", _apply)
```

嵌入自己的程式也是同一組零件：

```python
from xharness import Agent, AgentOptions, build_harness, load_config

harness = build_harness(load_config(), no_session=True)
agent = Agent(harness.ctx, AgentOptions(approval_mode="auto"))
answer = agent.run("pyproject.toml 宣告的測試相依是什麼？")
```

## 架構

架構圖與設計說明（接縫、與 DeepSeek Harness 的對照）見 [docs/architecture.zh.md](docs/architecture.zh.md)。

## 開發

```sh
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
```

## 藍圖

- Web UI（本機伺服器、串流逐字稿、防中文輸入法 Enter 誤送的輸入處理）
- 子代理與平行任務分派
- MCP client 支援
- `bash` 的檔案系統沙箱模式
- 供應端型錄預設集

## 授權

[MIT](LICENSE) — 版權所有 xCloudinfo Corp. Limited（云碩科技股份有限公司）。
