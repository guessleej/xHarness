# Changelog

中文 | English below

## 1.3.0 — 2026-09-21

- 新增桌面版：`xharness desktop` 把 Web UI 開在原生視窗（pywebview，選配 `xharness[desktop]`），伺服器綁隨機 loopback 埠、關窗即停；沒有設定檔時視窗顯示建立方式。`packaging/desktop/build.py` 以 PyInstaller 打包成 `xHarness.app`＋`.dmg`／`xHarness.exe`。`web.start_server()` 從 `serve()` 抽出供兩者共用。
- Desktop app: `xharness desktop` opens the Web UI in a native window (pywebview, optional `xharness[desktop]`); the server binds a random loopback port and stops when the window closes; with no config the window shows how to create one. `packaging/desktop/build.py` bundles `xHarness.app` + `.dmg` / `xHarness.exe` with PyInstaller. `web.start_server()` is split out of `serve()` for both to share.

## 1.2.0 — 2026-09-21

- 新增瀏覽器工具（`[tools] browser = "<camofox 位址>"`）：`web_search` 在真瀏覽器開 DuckDuckGo 結果頁、回傳標題／網址／摘要（結果頁的跳轉連結已還原成真實網址）；`browser_open` 回傳渲染後可見文字。預設走審批，`browser_approval = false` 可放行。
- Browser tools (`[tools] browser = "<camofox url>"`): `web_search` opens the DuckDuckGo results page in a real browser and returns title / URL / snippet (redirect links unwrapped to the real URL); `browser_open` returns the rendered visible text. Approval-gated by default; `browser_approval = false` waives it.

## 1.1.0 — 2026-09-21

- 新增 Telegram 通道（`[channels.telegram]`）：手機直接對節點下任務、有副作用的工具以「允許／拒絕」按鈕審批、`/new` `/stop` `/usage` `/status` 指令；每個聊天就是一個普通對話，艦隊視圖、煞車、session 記錄全部共用。只服務 `allowed_chats`，其餘不回應；token 只從 `token_file` 或 `token_env` 讀。
- 新增 `POST /api/notify {text, chats?}`：讓平台、排程或其他系統把通知推到已掛上的通道；沒有通道回 503。
- Telegram channel (`[channels.telegram]`): drive a node from your phone, approve mutating tools with allow / deny buttons, `/new` `/stop` `/usage` `/status`; every chat is an ordinary conversation sharing the fleet view, the budget brake and the session log. Only `allowed_chats` are served; the token is read from `token_file` or `token_env` only.
- `POST /api/notify {text, chats?}`: let the platform, a scheduler or any other system push a notification through the mounted channels; 503 when none is mounted.

## 1.0.6 — 2026-09-19

- Web UI：側面板新增「工具」區——列出所有工具（標示有副作用）、沙箱狀態、MCP server 與各自貢獻的工具數；新增 `GET /api/tools`。
- Web UI: a "工具" section in the side panel lists every tool (mutating flagged), the sandbox state, and each MCP server with its tool count; new `GET /api/tools`.

## 1.0.5 — 2026-09-19

- Web：終端只記錄寫入與錯誤（艦隊輪詢的 GET 不再洗版）；分頁在背景時暫停艦隊輪詢。
- Web: the terminal logs writes and errors only (fleet polling GETs no longer flood it); fleet polling pauses while the tab is hidden.

## 1.0.4 — 2026-09-19

- Web UI：加上瀏覽器分頁圖示（伺服器自帶的 SVG，品牌紅底白 X）。
- Web UI: browser tab icon (server-provided SVG favicon).

## 1.0.3 — 2026-09-19

- Web UI：艦隊視圖的「目前對話」卡片改用淡色底標示，不再用彩色描邊。
- Web UI: the current conversation card in the fleet view is marked with a tinted background instead of a colored outline.

## 1.0.2 — 2026-09-19

- Web UI：導覽列新增「對話：…」晶片，「回到對話」按鈕寫出目前對話的標題，艦隊視圖把目前對話的卡片標為「目前對話」——多個對話並存時一眼知道現在在哪一個。
- Web UI: a "對話：…" chip in the top bar, the fleet toggle names the current conversation, and the fleet view marks the current card — with many conversations open it is always clear which one you are in.

## 1.0.1 — 2026-09-19

- Web UI：在艦隊視圖按「新對話」或從面板選對話時，現在會切回逐字稿（之前停留在艦隊、每按一次多一張閒置卡）。
- Web UI: creating a conversation or picking one from the panel while in the fleet view now returns to the transcript (it used to stay on the fleet grid and add an idle card per click).

## 1.0.0 — 2026-09-19

首個正式版。從 0.1（2026-08-22）到 1.0 共 10 個功能版本，全部在地端機群實測。

- **核心**：一切皆插件的 `Context`／`Harness`；OpenAI 相容串流 adapter（llama.cpp／vLLM／Ollama／閘道皆可）；工具 bash／read／write／edit／glob／grep／todo；審批策略；JSONL session 與 `--resume`；headless 與 REPL
- **治理**：探測制沙箱（Seatbelt／bwrap）、telemetry 成本煞車、`security_scan`、MCP client、預設關閉的 webfetch、AGENTS.md／CLAUDE.md 注入、SSDLC 威脅模型與 CI 安全閘門（bandit／pip-audit／gitleaks 全歷史）
- **評測**：`xharness eval` TOML 案例套件、七種檢查、內建 `evals/basic`
- **多 agent**：subagent 與平行 batch（子代理不遞迴、副作用回流父審批）
- **介面**：零相依 Web UI（串流、審批卡、IME 防護、開燈關燈）、艦隊視圖（就地審批／停止）、多節點 hub（token 只在檔案或環境變數、只代理審批與停止）、用量歷史趨勢圖
- **記憶**：一則一檔的可稽核記憶、主題頁、彙整（dry-run 預設）、到期與重驗（只歸檔不刪）
- **供應端**：型錄預設集與 `providers probe`
- **1.0 修正**：system prompt 明講實際模型身分，禁止冒稱 GPT／Claude／Gemini 等他家模型

## 1.0.0 — 2026-09-19 (English)

First stable release; ten feature releases since 0.1 (2026-08-22), all exercised on an on-prem fleet.

- **Core**: everything-is-a-plugin `Context`/`Harness`; OpenAI-compatible streaming adapter; bash/read/write/edit/glob/grep/todo tools; approval policy; JSONL sessions with `--resume`; headless and REPL modes
- **Governance**: probed sandbox (Seatbelt/bwrap), telemetry cost brakes, `security_scan`, MCP client, opt-in webfetch, AGENTS.md/CLAUDE.md injection, SSDLC threat model and CI security gates (bandit, pip-audit, full-history gitleaks)
- **Evaluation**: `xharness eval` TOML suites with seven check kinds and the bundled `evals/basic`
- **Multi-agent**: subagents and parallel batches (no recursion; side effects route through the parent's approval)
- **Interfaces**: zero-dependency Web UI (streaming, approval cards, IME-safe composer, light/dark), fleet view with inline approve/stop, multi-node hub (tokens only in files or env, approvals and stop proxied), usage history trend chart
- **Memory**: auditable one-file-per-fact memory, topic pages, consolidation (dry-run by default), expiry and re-verification (archive, never delete)
- **Providers**: catalog presets and `providers probe`
- **1.0 fix**: the system prompt states the real model identity and forbids claiming to be GPT, Claude, Gemini, or any other vendor's model
