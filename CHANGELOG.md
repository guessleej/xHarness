# Changelog

中文 | English below

## 1.4.2 — 2026-09-30

- 修正 1.4.0 引入的問題：節點綁在對外位址並以 `[auth]` 取代共用 token 時，首頁被 `forbidden host` 擋住，等於開了帳號就進不了 UI。防 DNS rebinding 的 Host 檢查現在只在「完全沒有憑證」的安裝上生效——有 token 或有登入機制時，首頁照常提供（頁面本身不含任何資料，所有 `/api` 路由仍然要求憑證）。
- Fix a 1.4.0 regression: a node bound to a non-loopback address that uses `[auth]` instead of a shared token had its index page refused with `forbidden host`, so enabling accounts made the UI unreachable. The DNS-rebinding Host check now applies only to an install with no credential at all; with a token or sign-in configured the page is served (it holds no data, and every `/api` route still demands a credential).

## 1.4.1 — 2026-09-30

- 服務使用報告的「沙箱」項目改為**實際探測**後端，而不是照設定值回報：`auto` 在沒有可用後端的機器上（常見於 Ubuntu 24.04 停用未授權 user namespaces）會被列為未達建議設定並進入待處理事項，不再顯示「本期無異常」。報告的價值在於說出系統自己的問題，這一項原本說反了。
- 新增 [學校與公部門部署指南](docs/deployment-school.zh.md)：三層架構、模型來源國確認、多使用者節點與 LDAP 設定、桌面版發佈、Ubuntu 24.04 的 user namespaces 問題與三條處理路徑、九項交付前檢查清單、每月例行工作。
- The usage report's sandbox entry now **probes** the backend instead of trusting the configured mode: `auto` on a machine with no working backend (commonly Ubuntu 24.04, which restricts unprivileged user namespaces) is reported as below the recommended setting and raised as an issue, rather than contributing to "no anomalies this period". A report exists to state the system's own problems, and this one had it backwards.
- New [deployment guide for schools and public-sector sites](docs/deployment-school.zh.md): the three-layer architecture, confirming a model's country of origin, multi-user and LDAP configuration, shipping the desktop build, the Ubuntu 24.04 user-namespace problem with three ways out, a nine-item acceptance checklist, and the monthly routine.

## 1.4.0 — 2026-09-30

多使用者與交付治理：一台機器可以同時服務一群人，而且說得出誰用了什麼、花了多少、安不安全。

- **使用者身分（`[auth]`）**：本機帳號（PBKDF2-HMAC-SHA256，雜湊存 0600 檔，密碼永不進設定檔）或學校目錄（`[auth.ldap]`，純標準函式庫的 LDAP simple bind，拒絕空密碼的匿名綁定、DN 樣板防注入、ldaps 預設驗憑證）。連續失敗會暫時鎖定，登入成敗一律寫入 `access-audit.jsonl`。不設這一段時行為與 1.3.x 完全相同。
- **每人獨立的工作階段**：session 記錄改存 `sessions/u/<使用者>/`，對話列表、session 清單、艦隊視圖與用量只回該使用者自己的；管理者才看得到全體。
- **每人配額（`quota_tokens_per_day` / `_per_month`）**：跨任務、跨重啟累計，超額在下一次模型呼叫前硬停。以磁碟上的記錄為真本，涵蓋 CLI、Web、Telegram 與子代理。
- **服務使用報告**：`GET /api/usage-report`（管理者限定）與 `xharness report` 產出六塊——誰在用、花多少、自建 vs 外購、設備有沒有用到、對外串接安不安全、系統健康與治理。`scripts/report/build_usage_report.py` 把它做成 Word（選配 `--pdf`）。報表會揭露系統自己的問題：未歸戶的用量、連不上的節點、零用量的設備、登入失敗。
- **機密防護檢查頁**：`GET /api/security-check`（管理者限定）與 UI 的「管理」分頁，逐項講人話並附修法——設定檔有沒有明文機密、機密有沒有進網址、帳號檔與權杖檔的權限、對外綁定有沒有權杖、沙箱模式、LDAP 是否加密。永遠不顯示機密內容。
- **桌面版可同時當艦隊節點（`[desktop] node = true`）**：同一個視窗多開一個帶權杖的對外埠，使用者用得順、資訊中心看得到，不必二選一。權杖首次啟動自動產生成 0600 檔案。
- **集中遮罩（`redact.py`）**：稽核記錄、錯誤訊息與工作階段記錄統一過一次遮罩，Bearer 權杖、網址查詢字串裡的金鑰、`sk-`／`hf_`／`ghp_` 形狀與連線字串密碼都不會被寫下來。
- CLI：新增 `xharness users [list|add|password|disable|audit]`（密碼用互動輸入，不經命令列）與 `xharness report`。停用帳號是停用不是刪除，歷史用量才歸得了戶。

Multi-user operation and delivery governance: one machine can serve a group of
people and still answer who used what, what it cost, and whether it is safe.

- **Identity (`[auth]`)**: local accounts (PBKDF2-HMAC-SHA256, hashes in a 0600 file, passwords never in the config) or the site directory (`[auth.ldap]`, an LDAP simple bind written against the standard library only — empty-password anonymous binds refused, DN templates injection-proof, ldaps verified by default). Repeated failures lock an account temporarily; every sign-in and refusal lands in `access-audit.jsonl`. Without this section the behaviour is exactly 1.3.x.
- **Per-user sessions**: transcripts move to `sessions/u/<user>/`; conversation lists, session lists, the fleet view and usage return only that person's own work. Administrators see everyone.
- **Per-user quotas** (`quota_tokens_per_day` / `_per_month`): accumulated across tasks and restarts, hard-stopping before the next model call. The session logs on disk are the source of truth, so CLI, Web, Telegram and subagent traffic all count.
- **Service usage report**: `GET /api/usage-report` (admin only) and `xharness report` produce six blocks — who used it, what it cost, self-hosted vs bought-in, whether the hardware was used, whether outbound integrations are safe, system health and governance. `scripts/report/build_usage_report.py` turns it into a Word document (optional `--pdf`). The report states its own problems: unattributed usage, unreachable nodes, idle hardware, failed sign-ins.
- **Secret-handling self-check**: `GET /api/security-check` (admin only) and the UI's admin tab, each finding in plain language with the fix beside it — inline secrets in the config, secrets in URLs, permissions on the account and token files, an exposed binding without a token, the sandbox mode, LDAP encryption. It never shows a secret's value.
- **The desktop window can be a fleet node** (`[desktop] node = true`): a second, token-protected listener on the same instance, so the app stays usable for the person in front of it and visible to the people responsible for it. The token is generated into a 0600 file on first launch.
- **Central redaction (`redact.py`)**: audit records, error messages and transcripts pass through one masker, so bearer tokens, secrets in URL query strings, `sk-` / `hf_` / `ghp_` shapes and connection-string passwords are never written down.
- CLI: `xharness users [list|add|password|disable|audit]` (passwords typed, never passed as arguments) and `xharness report`. Disabling an account keeps its history attributable; deleting would orphan it.

## 1.3.2 — 2026-09-21

- CI：修 bandit SAST 發現——`desktop.py` 關閉時銷毀可能已關閉的原生視窗那段 `try/except/pass` 補上理由並登錄 SSDLC nosec 清單，不再靜默跳過。
- CI: fix a bandit SAST finding — the try/except/pass around destroying a possibly-already-closed native window on desktop shutdown now carries a justification and is registered in the SSDLC nosec list instead of being silently skipped.

## 1.3.1 — 2026-09-21

- Telegram：按下「允許／拒絕」後把按鈕換成結果文字（之前只有一閃即逝的提示，看起來像沒反應）；工具跑完也回報一行；聊天型訊息（問候、閒聊）直接回答不動工具，不再對「還在嗎」跑 `ls`。`WebApp.create()` 多了 `extra_system`、`AgentOptions.system_suffix`。
- Telegram: after allow / deny the buttons are replaced with the verdict (before, only a transient toast, which looked like nothing happened); tool completion is reported too; chat-style messages (greetings, small talk) are answered directly without tools — no more `ls` in reply to "are you there". `WebApp.create()` gains `extra_system`, `AgentOptions.system_suffix`.

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
