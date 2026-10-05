# Changelog

中文 | English below

## 1.7.2 — 2026-10-05

- 修 1.7.0 的 `extra_body` 根本沒送出去：設定檔載入時 `apply_preset()` 只留 `PROVIDER_KEYS` 白名單裡的欄位，而 `extra_body` 不在名單上，於是 `[providers.X] extra_body = { ... }` 被靜默丟掉，reasoning 模型照樣回空 `content`。補進白名單，並加一個測試鎖住白名單與 `OpenAIAdapter` 的參數一致，下次新增參數忘了登記會直接失敗。
- Fix 1.7.0's `extra_body` never reaching the endpoint: `apply_preset()` keeps only keys in `PROVIDER_KEYS` when the config loads, `extra_body` was not on that list, so `[providers.X] extra_body = { ... }` was silently dropped and reasoning models still came back with empty `content`. It is on the list now, and a test pins the list to `OpenAIAdapter`'s parameters so the next new parameter cannot be forgotten the same way.

## 1.7.1 — 2026-10-01

- 修 1.6.0 的副作用：每人一個工作目錄之後，**操作者放在伺服器啟動目錄的 `AGENTS.md` 就再也沒人讀得到**——而那正是最需要一份共同規範的時候。現在 agent 會先讀伺服器啟動目錄的指示（站台規範），再讀自己工作目錄的（專案慣例），後者可以補充前者。兩邊指到同一個目錄時只讀一次。
- Fix a 1.6.0 side effect: once each person worked in their own directory, an operator's `AGENTS.md` in the server's own working directory stopped reaching anybody — exactly when a shared policy matters most. An agent now reads the server directory's instructions first (site policy), then its own working directory's (project conventions), which refine them. The same directory is never read twice.

## 1.7.0 — 2026-10-01

- **供應端可帶自訂請求欄位**：`[providers.X] extra_body = { ... }` 會併進送往端點的 JSON。**reasoning 模型是這個功能存在的理由**——granite 這類模型若不在請求裡關掉 thinking，答案會跑進 `reasoning_content`，`content` 留空，harness 收到的就是一串空回應；而每個家族關閉的寫法都不同（llama.cpp 吃 `chat_template_kwargs = { enable_thinking = false }`）。adapter 自己擁有的欄位（model、messages、stream、stream_options、tools）不會被覆寫。
- **Per-endpoint request fields**: `[providers.X] extra_body = { ... }` is merged into the JSON sent to the endpoint. **Reasoning models are why this exists** — several of them put the answer in `reasoning_content` and leave `content` empty unless the request turns thinking off, and every family spells that differently (llama.cpp takes `chat_template_kwargs = { enable_thinking = false }`). Fields the adapter owns (model, messages, stream, stream_options, tools) cannot be overwritten.

## 1.6.1 — 2026-09-30

- `GET /api/tools`（側面板的「工具」區）改為依呼叫者的層級過濾：受限帳號不會再看到自己永遠拿不到的工具。清單本來就不該宣傳做不到的事。
- `GET /api/tools` now filters by the caller's tier, so a restricted account no longer sees tools it will never be given. A capability list should not advertise what it cannot do.

## 1.6.0 — 2026-09-30

使用者分層，以及檔案終於能從瀏覽器進來。1.4.0 把對話與用量隔離到人，但**檔案系統還是共用的**——同一台機器上的兩個帳號，一個 `write` 另一個就 `read` 得到。這一版補上。

- **每人一個工作目錄**：`$XHARNESS_HOME/workspaces/u/<帳號>/`，agent 的 cwd 就是它，`read`／`write`／`glob`／`bash` 全都在裡面。隔離是檔案系統層級的，不是查詢時過濾。沒有啟用 `[auth]` 時，agent 照舊使用啟動伺服器的工作目錄。
- **上傳檔案**：輸入框旁的迴紋針，或直接把檔案拖進輸入區。檔案落在自己的 `uploads/`，送出訊息時自動附上路徑告訴 agent；側面板可看清單、用量、重新附加與刪除。`POST /api/files` 走自己寫的 multipart 解析器（`multipart.py`，純標準函式庫，因為 `cgi` 在 Python 3.13 被移除且本專案零相依）。每次上傳寫入存取稽核。
- **上傳的安全邊界**：檔名先消毒再落地（去掉路徑分隔符號與控制字元，同名加序號不覆蓋，落點驗證在 uploads 目錄內）、副檔名白名單（**可執行檔不在內**，上傳的檔案永不執行）、單檔與每人總量上限、0600 權限。
- **使用者分層**：`[auth.roles.<層級>]` 自定層級，可設顯示名稱、是否為管理者、`deny_tools`、該層預設配額。被擋的工具是**不存在**而非被拒絕——它不會出現在模型的工具清單裡，所以受限帳號不會先被答應再失敗。配額優先序：個人 → 帳號記錄 → 層級 → 全站預設。內建的 `admin`／`user` 行為不變。

User tiers, and files can finally get in from the browser. 1.4.0 isolated conversations and usage per person but left **the filesystem shared** — one account's `write` was another's `read`. This release closes that.

- **A working directory per person**: `$XHARNESS_HOME/workspaces/u/<user>/` is the agent's cwd, so `read` / `write` / `glob` / `bash` all stay inside it. The isolation is a filesystem boundary, not a query-time filter. Without `[auth]` the agent uses the server's own working directory as before.
- **File uploads**: a paperclip beside the input, or drop files onto the composer. They land in the uploader's own `uploads/`, and their paths are appended to the message so the agent knows where they are; the side panel lists them with their size and lets you re-attach or delete. `POST /api/files` uses a multipart parser written for this (`multipart.py`, standard library only — `cgi` was removed in Python 3.13 and this project has no runtime dependencies). Every upload is written to the access audit.
- **Upload trust boundary**: names are sanitised before anything is written (path separators and control characters stripped, repeats numbered rather than overwritten, the destination verified to be inside the uploads directory), an extension allow-list (**no executables**, and uploads are never executed), per-file and per-person size limits, 0600 permissions.
- **User tiers**: define them under `[auth.roles.<tier>]` with a label, whether they administer, `deny_tools`, and a default quota. A denied tool is **absent** rather than refused — it never reaches the model's tool list, so a restricted account is never promised something that then fails. Quotas resolve person → stored record → tier → site default. The built-in `admin` / `user` behave exactly as before.

## 1.5.1 — 2026-09-30

修 1.5.0 的兩個排版問題，都出在「同一件事有兩個地方在決定」。

- **切到「管理」或「艦隊」時，空狀態的建議卡還留在頁面最上方。** 視圖切換是用 JS 設 `hidden` 屬性做的，但 `.starters` 在樣式表裡有明確的 `display:grid`，`hidden` 蓋不過去。現在哪個視圖在畫面上只由 body 的一個 class 決定，JS 不再逐個元素設 `hidden` 或 inline `display`。
- **「管理」與「艦隊」的標題被拉成置中的大字。** `.hero` 是這兩頁共用的區塊標題樣式，1.5.0 把落地頁的置中、放大與光暈直接加在上面。這些現在限定在落地頁自己的 `#hero`。

Two layout fixes from 1.5.0, both cases of two places deciding the same thing.

- **The starter cards stayed at the top of the admin and fleet views.** View switching set the `hidden` property from JS, but `.starters` carries an explicit `display:grid` in the stylesheet, which wins. Which view is on screen is now decided by a single body class, and JS no longer sets `hidden` or an inline `display` per element.
- **The admin and fleet headings were centred and oversized.** `.hero` is the shared section-heading block those views use, and 1.5.0 put the landing page's centring, larger type and glow directly on it. Those now apply only to the landing page's own `#hero`.

## 1.5.0 — 2026-09-30

Web UI 重新排版。舊版把狀態晶片和導覽按鈕塞在同一列，人一多、晶片一長就擠成三行把按鈕推開；空狀態只有兩行字，中間留一大片白。

- **導覽列改成三段式且永不換行**：左邊 logo，中間橫向導覽（新對話／艦隊／管理／紀錄，目前所在用淡色底標示），右邊使用者選單。模型、沙箱、審批模式與今日配額（含進度條）收進使用者選單，不再佔用工具列。
- **空狀態重做**：置中的標題與說明，下方四張建議任務卡（點一下直接送出），內容在視窗內垂直置中，緩慢漂移的品牌光暈給深色以外的層次。有對話之後這一整區收起，換成一條狀態列顯示目前對話、執行狀態與用量。
- **行動版**：導覽在 375px 下原本重疊，現在第一列放 logo 與使用者頭像、第二列放可橫向捲動的導覽；建議卡改單欄，hero 字級與間距一併調整。
- 尊重 `prefers-reduced-motion`：關閉光暈漂移與卡片進場動畫。

The Web UI has been re-laid out. The old toolbar mixed status chips with navigation buttons, so a longer chip pushed the buttons onto a third row; the empty state was two lines of text above a large blank area.

- **Three-zone toolbar that never wraps**: wordmark, horizontal navigation (new chat / fleet / admin / history, with the current view on a tinted background), and a user menu. Model, sandbox, approval mode and today's quota (with a meter) moved into that menu instead of crowding the toolbar.
- **Rebuilt empty state**: a centred heading and description over four starter cards that send their task on click, vertically centred in the viewport, with a slow brand-coloured glow for depth. Once a conversation starts the whole area collapses into a single status row.
- **Phone layout**: the toolbar used to overlap at 375px; the wordmark and avatar now share the first row and the navigation scrolls sideways on the second. Starter cards go single-column, with hero type and spacing adjusted to match.
- `prefers-reduced-motion` turns off the glow and the card entrance animation.

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
