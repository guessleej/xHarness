# xHarness 架構

[English](architecture.md) | 中文

給貢獻者的設計說明。用法見 README；本文講設計。

## 核心：Context、Plugin、Harness

`xharness/context.py` 就是整個核心，約 150 行：

- **`Context`** 持有三個註冊表：具名**服務**（`provide`/`get`/`optional`）、**事件監聽**（`on`/`emit`，失敗被攔下並記錄）、以及可攔截呼叫的**中介層鏈**（`intercept`/`invoke`）。
- **`Plugin`** 是一個名字加上 `apply(ctx, config)`。任何東西要進入 context，唯一的路就是套用插件。
- **`Harness`** 依序套用插件。插件套用期間，每筆註冊的 disposer 都收進該插件的 scope；`unload(id)` 反序執行 scope，插件的貢獻剛好完整回捲。`apply` 中途拋錯的插件會立即回捲，什麼都不掛載。

沒有特權核心：模型 adapter、每個工具、session 記錄、agent 的工具接線，掛載方式與使用者插件完全相同。`presets.build_harness()` 只是預設組裝順序，不是特殊機制。

## 接縫

| 接縫 | 類型 | 契約 |
|---|---|---|
| `llm` | 服務 | `stream(messages, tools, on_delta) -> AssistantTurn`。任何符合此形狀的物件都能服務；測試就掛假的。 |
| `tools` | 服務 | `ToolRegistry`：`register`（回傳 disposer）、`list`、`get`。 |
| `session` | 服務（可選） | `SessionLog.append(event)`。不存在就是不落地；agent 用 `ctx.optional` 檢查。 |
| `llm/stream` | 中介層 | 包住每次模型呼叫。payload 是 `{"messages", "tools"}`；terminal 執行真正的請求。快取、記錄、路由、請求改寫都住在這裡。 |
| `agent/task-start`、`agent/task-end`、`plugin/loaded`、`plugin/unloaded` | 事件 | 觀測用；監聽者失敗絕不拖垮宿主。 |

## Agent 迴圈

`Agent.run(task)` 是一個直白的迴圈：附加使用者訊息、經 `llm/stream` 呼叫模型、由註冊表執行 tool calls、附加工具結果、重複直到模型不再呼叫工具或達到 `max_turns`。格式錯誤的 tool call（不明名稱、非法 JSON 參數）會變成錯誤結果回饋給模型而不是當機，讓模型能自我修正。

審批是策略不是工具邏輯：有副作用的工具動手前呼叫 `tool_ctx.approve(summary)`，其意義由 agent 決定（`auto` 通過、`prompt` 問人、`prompt` 但沒有詢問器就拒絕）。工具永遠不直接讀策略。

## Session

`SessionLog` 是 `$XHARNESS_HOME/sessions/` 底下每個 session 一個的只增不改 JSONL 檔。事件是帶 ISO 時間戳的 `message` 與 `tool-result` 記錄。`--resume` 以 `messages_from_events` 重建訊息串；損毀的行會被跳過而不是整個 session 報廢。只增不改代表當機最多丟掉正在寫的那筆事件，永遠不會改寫歷史。

## Adapter

`OpenAIAdapter` 講 OpenAI chat-completions 線上格式（`stream: true`），用標準函式庫（`urllib` + 逐行迭代）解析 SSE。tool-call 增量依 index 累積；伺服器送 `stream_options.include_usage` 時從最後一個 chunk 取得用量。憑證每次請求時才從設定檔指名的環境變數（`api_key_env`）解析，設定檔裡不放任何密鑰。

## 與 DeepSeek Harness 的對照

xHarness 借用 DeepSeek Harness（dsh）的組合思想，並刻意捨棄其規模：

| | dsh | xHarness |
|---|---|---|
| 語言 | TypeScript | Python（3.11+，純標準函式庫） |
| 規模 | 約 56.5 萬行、227 個套件 | 約 1,500 行、1 個套件 |
| 插件執行期 | Cordis（服務、型別化事件、HMR、fiber） | `Context`（服務、事件、中介層、scope 回捲） |
| 組合方式 | profile、bundle、分層 YAML patch | 一個 preset 函式 + TOML 插件清單 |
| 供應端 | adapter 註冊表、型錄、模型探索 | 一個 OpenAI 相容 adapter |
| 沙箱 | bwrap / Landlock / Seatbelt / ACL，fail-closed | 尚無（僅審批策略） |
| 介面 | Web 應用 | CLI |

重點不是對等，而是：架構核心——掛在共享 context 上、效果可回捲的插件；可攔截的模型呼叫接縫；只增不改的 session 記錄——可以塞進一個開發者審計得完的套件裡。對地端部署來說這很重要：每一行會碰網路的程式碼都必須可被審查。

## 已知限制

- `bash` 工具沒有沙箱；審批策略是唯一防線。不要用 `--approve auto` 跑不受信任的任務。
- 一次執行一個供應端；不支援 session 中途換模型。
- 同步單執行緒：一次一個模型呼叫、一個工具。
- `resume` 只重放記錄裡的內容；粒度就是整則訊息。
