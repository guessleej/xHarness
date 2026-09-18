# xHarness

中文 | [English](README.en.md)

[![CI](https://github.com/guessleej/xHarness/actions/workflows/ci.yml/badge.svg)](https://github.com/guessleej/xHarness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-red.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

xHarness 是云碩科技（xCloudinfo）開發的插件式 AI agent harness：治理優先、零相依的精簡 Python 套件，「一切皆插件」，可對接**任何 OpenAI 相容端點**——llama.cpp（`llama-server`）、vLLM、Ollama、LiteLLM 或企業閘道。完全適合地端部署：除了你設定的模型端點之外，不會對外傳送任何資料。

## 為什麼做這個

多數 agent harness 綁死單一廠商 API，還拖著龐大的相依樹。xHarness 保留架構核心——工具、模型 adapter、session 記錄、agent 迴圈的組裝全部都是掛在共享 context 上的插件——但把規模控制在一個人一個下午讀得完：約 1,900 行 Python、**零執行期相依**（純標準函式庫，含 SSE 串流client、TOML 設定讀取與 MCP client），測試跑完不到一秒。

## 特色

- **一切皆插件。** 服務、事件、工具註冊都掛在共享 `Context` 上；卸載插件時自動回捲它註冊的一切。
- **任何 OpenAI 相容供應端。** SSE 串流含 tool calls、憑證每次請求時才從環境變數解析、可設定 temperature 與 token 上限。
- **內建工具。** `bash`、`read`、`write`、`edit`、`glob`、`grep`、`todo_write`，另有選配的 `webfetch`。
- **沙箱隔離。** `bash` 自動用作業系統現成機制圈住：macOS 走 `sandbox-exec`（Seatbelt）、Linux 走 `bwrap`（bubblewrap）——寫入限制在工作目錄與暫存目錄，`allow_network = false` 可一併斷網；`mode = "require"` 沒有後端就拒絕啟動。
- **MCP client。** `[mcp.servers.*]` 設定 stdio MCP server，其工具以 `mcp__{server}__{tool}` 名稱進工具清單；沒有標 `readOnlyHint` 的一律視為有副作用、走審批策略。
- **審批策略。** 有副作用的工具（`bash`、`write`、`edit`、`webfetch`、MCP 工具）在互動模式會先詢問；`--yes` 或 `approval = "auto"` 可關閉。
- **專案指示檔。** 工作目錄的 `AGENTS.md`（或 `CLAUDE.md`）會自動讀入 system prompt，agent 進到哪個 repo 就遵守哪個 repo 的慣例；`--no-instructions` 或 `project_instructions = false` 可關閉。
- **成本煞車（telemetry）。** `llm/stream` 中介層統計每次模型呼叫的 token、工具呼叫數與延遲；設定 `max_total_tokens` / `max_llm_calls` 超額即硬停整個任務，用量摘要同步寫入 session 記錄，REPL 用 `/usage` 查。
- **Eval 子系統。** `xharness eval <目錄>` 對任何模型跑評測套件：每個案例在乾淨的暫存工作區執行任務，用確定性檢查（檔案內容、回答、指令結果、有沒有真的呼叫工具）加可選的 LLM 評審計分，輸出通過率、每案 token 與耗時，可寫 JSONL。內建 `evals/basic` 六個案例，直接量化「這顆模型會不會用工具、守不守規矩」。
- **子代理（subagent）。** `subagent` 把一個有界的子任務交給全新的子代理、`subagent_batch` 平行分派多個獨立任務；子代理共用工具與模型、不能再生子代理，每個有副作用的動作都回流父代理的審批策略。
- **Web UI 與艦隊視圖。** `xharness web` 起本機介面：串流逐字稿、工具卡片、審批按鈕、對話與歷史 session 清單、記憶清單、用量晶片、開燈關燈；「艦隊」視圖一頁看完所有對話與子代理的狀態、用量、等待中的許可，可就地審批或**停止**任何一個 agent。預設只綁 127.0.0.1，對外綁定必須帶 `--token`。
- **多節點艦隊。** 每台機器跑自己的 `xharness web` 當節點，任一台在設定檔列出節點就成為 hub：艦隊視圖把本機與所有節點的對話合併呈現，允許／拒絕／停止透過 hub 代理到節點；節點 token 只存在 hub 的環境變數，瀏覽器永遠碰不到。`xharness fleet` 在終端機看整個艦隊。
- **供應端型錄預設集。** `preset = "ollama"` 一行就接上 llama.cpp／Ollama／vLLM／LM Studio／LiteLLM 或 OpenAI／OpenRouter／Groq／Mistral／Together；`xharness providers probe` 探測每個端點並列出它提供的模型。
- **記憶層。** 跨 session 的持久記憶，一則事實一個 Markdown 檔（`~/.xharness/memory/`），自動產生 `MEMORY.md` 索引；模型每次呼叫都看到索引（名稱＋一句描述），需要才 `memory_read` 全文；`memory_write`/`memory_delete` 走審批，每次異動寫入 `audit.jsonl`（哪個 agent、哪個 session）。`xharness memory` 讓人直接檢查 agent 到底記得什麼。
- **只增不改的 session 記錄。** 每則訊息與工具結果都以 JSONL 記錄在 `~/.xharness/sessions/`；`--resume <id>` 可接續。
- **兩種執行模式。** headless 一次性（`xharness "任務"`）與互動 REPL。
- **可擴充。** 使用者插件模組可從設定檔加入工具與服務；`llm/stream` 中介層可攔截每次模型呼叫做快取、記錄或路由。
- **零遙測。** 除了你設定的模型端點（以及你自己選擇啟用的 `webfetch` 與 MCP server），xHarness 不對任何地方傳送資料。沒有匿名 id、沒有使用量上傳、沒有任何 phone-home。

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
| `subagent` | 是 | 把子任務交給全新的子代理，回傳它的最終答案 |
| `subagent_batch` | 是 | 平行跑多個獨立子任務，逐一標題回傳 |
| `memory_write` | 是 | 存一則跨 session 的記憶（名稱、一句描述、內容） |
| `memory_read` / `memory_search` / `memory_list` | 否 | 讀全文／關鍵字搜尋／列索引 |
| `memory_delete` | 是 | 刪除錯誤或過時的記憶 |
| `security_scan` | 否 | 對目錄跑 bandit / pip-audit / gitleaks（未安裝的略過） |
| `webfetch` | 是 | 抓取 http(s) 網頁並轉成可讀文字；**預設關閉**（`[tools] webfetch = true` 才開）——開了它才會有模型端點以外的對外連線，且每次抓取都走審批 |

審批只作用於有副作用的工具。headless 模式預設 `auto`（沒有人盯著管線）；互動模式預設 `prompt`。明確指定 `--approve prompt|auto` 一律優先。

## 沙箱

`bash` 工具的指令會在偵測到後端時自動圈住（macOS `sandbox-exec` / Linux `bwrap`）：根目錄唯讀、只有工作目錄與暫存目錄可寫。REPL 啟動列會顯示目前沙箱狀態。

```toml
[sandbox]
mode = "auto"          # auto（預設）| require（沒有後端就拒絕啟動）| off
allow_network = true   # false 時 bash 內的指令一併斷網
```

`auto` 在沒有後端的環境（例如未裝 bwrap 的容器）會退回無沙箱執行——要保證圈住就用 `require`。

## MCP servers

任何 stdio MCP server 都能把工具掛進來：

```toml
[mcp.servers.fs]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
# env = { KEY = "value" }
# timeout_seconds = 60
```

工具以 `mcp__fs__read_file` 這類名稱出現在 `/tools` 清單。沒有宣告 `readOnlyHint` 的 MCP 工具一律視為有副作用、受審批策略管；起不來的 server 會被略過並警告，不會拖垮整個 harness。

## Web UI 與艦隊視圖

```sh
xharness web                          # http://127.0.0.1:3080，自動開瀏覽器
xharness web --port 8090 --no-open
xharness web --host 0.0.0.0 --token 一串長隨機字串   # 對外綁定必須帶 token
```

介面是一個零相依的單檔頁面：頂部玻璃導覽列顯示模型、用量與狀態；逐字稿即時串流，工具呼叫收成可展開的卡片；有副作用的動作出現「允許／拒絕」卡片（`-y` 可改全自動）；右側滑出面板列出進行中的對話、磁碟上的 session（可接續）與目前的記憶。輸入框有中文輸入法 Enter 三重防護（組字中不會誤送）。

**艦隊視圖**（導覽列「艦隊」）：每個對話一張卡——狀態（執行中／等待許可／閒置）、任務預覽、tokens／呼叫／工具數、執行秒數、正在跑的子代理——上方是總覽數字；等待中的許可可以直接在卡片上允許或拒絕，執行中的 agent 可以按「停止」：它會在下一次模型呼叫前停下、待決的許可一律視為拒絕（進行中的那一次模型請求無法中斷，回來就停）。

安全設計：預設只綁 loopback、拒絕非 loopback 的 `Host`（防 DNS rebinding）、所有 POST 需自訂標頭（防跨站表單）、token 只走 `Authorization` 標頭、SSE 用 60 秒一次性票證。細節見 [docs/ssdlc.zh.md](docs/ssdlc.zh.md)。

## 子代理（subagent）

模型可以把子問題委派出去，不讓細節塞爆自己的 context：

```toml
[subagent]
max_workers = 4   # subagent_batch 的平行上限
max_turns = 20    # 每個子代理的回合上限
```

子代理與父代理共用工具、模型、telemetry 與 session 記錄（事件標記 `agent` 名稱，`--resume` 只重建主線）；子代理看不到 `subagent` 工具，所以不會無限遞迴；它的每個有副作用動作都經父代理的審批策略——父代理是 `prompt` 模式就仍會問你。

## 多節點艦隊

節點端（每台要被監看的機器）——非 loopback 綁定必須有 token，token 不必出現在命令列：

```sh
XHARNESS_WEB_TOKEN_FILE=/run/secrets/xharness-node xharness web --host 0.0.0.0 --port 3080 --no-open
```

Hub 端（任一台）——在 `xharness.toml` 列出節點，token 以環境變數名稱參照：

```toml
[fleet]
name = "hub-office"                 # 本機在艦隊視圖的顯示名稱（預設主機名）

[fleet.nodes.farm]
url = "http://10.0.0.5:3080"
token_env = "XHARNESS_NODE_FARM_TOKEN"
# timeout_seconds = 3
```

```sh
xharness fleet                      # 終端機：每個節點 up/DOWN、延遲、對話數、執行中、等待許可、tokens
xharness web                        # 艦隊視圖多出「節點：farm」區段，卡片可就地允許／拒絕／停止
```

信任模型：hub 只代理兩種動作（審批、停止）到節點，路徑白名單、對話 id 驗證；節點被 hub 詢問時不會再去輪詢自己的節點（避免遞迴）；「在節點開啟」會另開該節點的 UI（它有自己的認證）。LAN 內走 http 可接受，跨網段請在節點前放 TLS 反向代理。

## 供應端型錄預設集（providers）

```toml
[providers.local]
preset = "ollama"          # 只填預設值：base_url 與 api_key_env；model 永遠由你決定
model = "your-model"

[providers.cloud]
preset = "groq"            # hosted 預設集會把提示送出機器，屬選擇性啟用
model = "llama-3.3-70b-versatile"
# base_url / api_key_env 明寫就覆蓋預設集
```

```sh
xharness providers presets   # 列出內建預設集（local / hosted、base_url、金鑰環境變數）
xharness providers probe     # 對設定檔裡每個供應端 GET /models，列出可用模型
xharness providers probe cloud
```

內建：`llama-cpp`、`ollama`、`vllm`、`lmstudio`、`litellm`（本機）；`openai`、`openrouter`、`groq`、`mistral`、`together`（hosted）。

## 記憶層（memory）

記憶是給人看的：`~/.xharness/memory/<名稱>.md`，一則事實一個檔，前段是 name／description／kind／updated，後面是內容；`MEMORY.md` 是自動產生的索引。模型每次呼叫都在 system prompt 看到索引（有大小上限，超過會提示用 `memory_search`），只有它主動 `memory_read` 才載入全文——記憶不會悄悄塞爆 context。

```sh
xharness memory                 # 列索引：agent 記得什麼
xharness memory show fav-editor # 看一則全文
xharness memory search 部署     # 關鍵字搜尋
xharness memory audit           # 誰在哪個 session 寫／刪了什麼
```

```toml
[memory]
# enabled = true
# dir = "/path/to/memory"    # 預設 ~/.xharness/memory；指到專案目錄就變成專案記憶
# max_index_chars = 6000
```

治理：寫入與刪除都是有副作用的工具（互動模式會問你）；每次異動記錄 agent 名稱與 session 到 `audit.jsonl`；記憶是模型寫的內容、會回到未來的 prompt，所以它跟 AGENTS.md 一樣是信任決定——用 `xharness memory audit` 定期看，不對就 `memory_delete` 或直接刪檔。Web UI 的側面板也列出目前的記憶。

## 評測模型（eval）

```sh
xharness eval evals/basic                       # 內建六案例：寫檔、改檔、搜尋、bash、多步驟、指示遵循
xharness eval evals/basic --repeat 3 --json out.jsonl
xharness eval my-cases/ --model 另一顆模型      # 同一套案例換模型比較
```

案例是一個 TOML 檔：`prompt` 給任務、`[setup]` 預先放檔案、`[[checks]]` 逐條計分，全部通過才算過：

```toml
prompt = "把 config.ini 裡的 port = 8080 改成 9090，其他不要動。"
max_turns = 8

[setup]
"config.ini" = "[server]\nport = 8080\nworkers = 4\n"

[[checks]]
kind = "file_contains"
path = "config.ini"
pattern = "port = 9090"

[[checks]]
kind = "file_contains"
path = "config.ini"
pattern = "port = 8080"
absent = true
```

檢查種類：`answer_contains`、`answer_regex`、`file_exists`、`file_contains`、`command`（在工作區跑指令，exit 0 為過）、`tool_called`（模型有沒有真的呼叫某工具）、`judge`（用同一顆模型依 `rubric` 評 PASS/FAIL）。任一種加 `absent = true` 反向。每個案例都用全新 harness 執行，token 與耗時逐案獨立；`--repeat N` 量測穩定度。

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

架構圖與設計說明（接縫、與大型 harness 的對照）見 [docs/architecture.zh.md](docs/architecture.zh.md)。

## 開發

```sh
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
```

## 藍圖

- 記憶層的主題彙整（把零散記憶整理成主題頁）
- 艦隊視圖的歷史趨勢（每節點用量隨時間）

## 安全

威脅模型、生命週期管控與 CI 安全閘門見 [docs/ssdlc.zh.md](docs/ssdlc.zh.md)；弱點回報見 [SECURITY.zh.md](SECURITY.zh.md)。

## 授權

[MIT](LICENSE) — 版權所有 xCloudinfo Corp. Limited（云碩科技股份有限公司）。
