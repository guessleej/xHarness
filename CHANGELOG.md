# Changelog

中文 | English below

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
