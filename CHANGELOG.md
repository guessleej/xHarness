# Changelog

中文 | English below

## 1.18.0 — 2026-10-29

操作這台電腦：模型可以看畫面、用滑鼠和鍵盤（macOS）。`[tools] desktop_control = true` 開啟，預設關閉。

- **`desktop_look`**：列出前景程式（或指定程式）的視窗與畫面上的按鈕、欄位、選單、文字，每個帶編號、名稱、內容與位置。資料從「輔助使用」介面讀，不靠看圖，純文字模型也能用；只走看得到的部分，長聊天紀錄的視窗也在一兩秒內讀完（原本用 osascript 走完整棵樹要 107 秒）。`screenshot: true` 會另外截圖交給 `[vision] provider` 描述，截圖用完即刪、不留在磁碟。
- **`desktop_act`**：點擊、雙擊、右鍵（用畫面編號或座標）、輸入文字、按鍵組合（如 `cmd+s`）、捲動、把程式叫到前面。做完自動再看一次畫面並回傳，模型看得到動作的結果。文字一律用剪貼簿貼上再還原原本的剪貼簿內容，因為逐鍵輸入會經過輸入法，注音模式下打 abc 會變成「ㄇ」。
- **每次都問人**：看畫面與每個動作都要人按核准，審批模式設成 auto 也一樣；核准卡寫出要點的是哪個元素（例如「click AXButton「傳送」 in LINE」）。沒有人在就不做。`desktop_approval = false` 可改回一般審批策略，機密防護檢查頁會對此亮警告。
- **核准後先把目標程式叫回前景**：在瀏覽器或桌面視窗按核准，那個視窗會跑到前景，按鍵就會打錯地方。現在核准後會先把動作的目標程式叫回前景並檢查，叫不回來就什麼都不送。
- **直接拒絕的動作**：在密碼欄位輸入（請本人輸入），以及清空垃圾桶的按鍵組合。畫面編號在換了前景程式或超過 5 分鐘後失效，要重新看。
- **一次一個操作者**：只有對話的主代理拿得到這兩個工具，子代理與討論參與者沒有（子代理的審批會轉給上層策略，自動模式下可能不問人就放行）；整台電腦一把鎖，同時只會有一個動作。其他代理照樣可以平行處理檔案、指令與網頁。
- 讀畫面與送出輸入在獨立的子行程執行，macOS 介面 API 出錯或某個程式卡住都不會拖垮 xHarness，逾時 30 秒。
- 需要在「系統設定 > 隱私權與安全性 > 輔助使用」開啟執行 xharness 的程式，截圖另需「螢幕錄製」；沒開時工具會說明要去哪裡開。Windows 與 Linux 尚未支援。
- 服務使用報告的功能清單與機密防護檢查頁各加一項。

Use this computer: the model can see the screen and use the mouse and keyboard (macOS). Off by default; `[tools] desktop_control = true` turns it on.

- **`desktop_look`** lists the window and the buttons, fields, menus and text of the app in front (or a named app) as numbered elements with label, value and position, read through the Accessibility API rather than from pixels, so a text-only model can use it. Only what is visible is walked: a window with a long chat history reads in a second or two (walking the whole tree through osascript took 107 seconds). `screenshot: true` adds a screenshot described by `[vision] provider`; the picture is deleted right after.
- **`desktop_act`**: click, double-click, right-click (an element number or a point), type, key combinations (`cmd+s`), scroll, bring an app to the front; then it looks again and returns the new screen. Text is pasted through the clipboard, which is put back afterwards: typed keys go through the input method, and with Zhuyin "abc" arrives as "ㄇ".
- **Asks every time**: every look and every action needs the person's approval, approval mode "auto" included; the card names the element. Nobody there means no. `desktop_approval = false` puts the tools under the normal policy; the security check page warns about it.
- **The target app comes back first**: approving in a browser or the desktop window brings that window to the front, where the keys would land. After approval the app the action is for is brought back and checked; if it will not come, nothing is sent.
- **Refused outright**: typing into a password field, and the key combinations that empty the Trash. Element numbers expire when another app comes to the front or after 5 minutes.
- **One operator at a time**: only a conversation's main agent gets the tools (a child's approvals go to its parent's policy, which may approve silently in auto mode); one lock for the screen. Other agents keep working on files, commands and the web in parallel.
- Screen reading and input run in a separate process with a 30-second timeout, so a failing Accessibility call or a hung app cannot take xHarness down.
- Needs Accessibility for the app that runs xharness, and Screen Recording for screenshots; the tools say where to turn them on. Windows and Linux are not supported yet.
- The usage report's feature list and the security check page each gain an item.

通知頻道：管理者在網頁上設定 Telegram、LINE、Microsoft Teams，任務完成、失敗或等人核准時發訊息。

- **「管理 > 通知頻道」**：新增、編輯、停用、刪除、測試發送。每種頻道附取得金鑰的步驟；存檔時先向該服務驗證（Telegram `getMe`、LINE `bot/info`），卡片上看得到機器人或官方帳號名稱、最近一次送出成功與否（台北時間）。
- **金鑰加密保存**：機器人權杖、LINE 權杖、Teams 網址以 AES-256-GCM 加密存在 `channels.json`（`enc:v1:` 前綴，0600），金鑰另存 `channels.key`（0600），也可掛 `/run/secrets/xharness_channels_key`。任何 API 都不回傳已存的金鑰。需要 `cryptography`，缺少時頁面直接說明修法、不開放新增。新增選用相依 `xharness[secure]`。
- **Telegram 改由網頁管理**：網頁存的機器人優先於設定檔的 `[channels.telegram]`，改完立即重掛、不必重新啟動。沒授權的人傳訊息給機器人，機器人照樣不回，但管理頁會列出他（只記名稱與 chat id，不記內容），按「授權」即可。設定檔裡現有的機器人可一鍵「搬到網頁管理」。
- **網頁任務的核准送到 Telegram**：需要核准時，授權的 chat 收到「允許／拒絕」按鈕，按了就生效。
- **LINE、Teams 只通知**，預設不附回覆內容（只送標題、節點、耗時與網頁連結）；要附內容須逐一勾選。Teams 只接受 Microsoft 的 webhook 主機（logic.azure.com、powerplatform.com、webhook.office.com），表單無法用來打內網位址。
- **事件與門檻**：每個頻道選任務完成、任務失敗、需要核准、其他系統推送（`/api/notify`）；「任務完成」可設最短秒數（預設 30 秒）。從 Telegram 下的任務不會再收到一份重複通知。
- **遮罩補洞**：錯誤訊息與記錄裡的 Telegram 權杖（`/bot123:AA…`）與 Teams 網址的 `sig=` 原本不會被遮，現在會。
- 機密防護檢查頁加四項（頻道金鑰是否加密、兩個檔案的權限、哪些頻道會把內容送出本機）；服務使用報告的對外串接改列「通知頻道」；新增、修改、刪除、測試、授權都寫進存取稽核。

Notification channels: an admin sets up Telegram, LINE and Microsoft Teams on the web page, and a message goes out when a task finishes, fails or waits for an approval.

- **Admin > Notification channels**: add, edit, disable, delete, send a test. Each kind comes with the steps for getting its key; saving checks the key with the service first (Telegram `getMe`, LINE `bot/info`), and the card shows the bot or official account and whether the last message got through (Taipei time).
- **Keys stored encrypted**: bot token, LINE token and Teams URL are AES-256-GCM encrypted in `channels.json` (`enc:v1:` prefix, 0600), with the key in `channels.key` (0600) or mounted at `/run/secrets/xharness_channels_key`. No API returns a stored key. Needs `cryptography`; without it the page says how to fix it and adding is disabled. New optional extra `xharness[secure]`.
- **Telegram managed on the page**: a bot saved there wins over `[channels.telegram]` and is remounted at once, no restart. Someone not yet authorised who messages the bot still gets no reply, but the page lists them (name and chat id, never the text) with an Authorise button. A bot already in the config file moves to the page with one click.
- **Approvals for web tasks go to Telegram** as Allow and Deny buttons in the authorised chats.
- **LINE and Teams only notify**, and by default carry no answer text (title, node, duration and a link); carrying it is a per-channel tick. Teams only accepts Microsoft webhook hosts (logic.azure.com, powerplatform.com, webhook.office.com), so the form cannot be pointed at an internal address.
- **Events and threshold**: each channel picks task finished, task failed, approval needed and pushes from other systems (`/api/notify`); "task finished" has a minimum duration (30 s by default). Tasks given from Telegram are not notified a second time.
- **Masking gap closed**: a Telegram token in a URL (`/bot123:AA…`) and a Teams `sig=` were not masked in errors and logs; now they are.
- The security check page gains four items (channel keys encrypted, both files' permissions, which channels send content off the machine); the usage report lists "notification channels" under outbound connections; saving, editing, deleting, testing and authorising are written to the access audit.

## 1.17.1 — 2026-10-24

討論的四個問題，由 staffKM 用 .70 xVITA 跑知識庫討論時回報。

- **參與者的說明不再算證據**：每位參與者的 brief 和角色說明原本會進入可引用的資料，裡面寫到的數字或文件名稱因此不會被標成沒查證。現在只有提出討論的人給的題目與背景、以及工具查到的內容才算。
- **共識由程式核對**：結論的「共識」各點，若其中的數字或出處只出現在一位參與者的發言裡，會加註「只出現在某某的發言，沒有其他人提到或同意」。原本只靠提示詞要求「兩位以上同意」，地端模型擋不住。
- **審查形式**：作者稿沒有再變動（或只回「無需修訂」）就提早收場，不再白跑剩下的輪數；輸出另附「定稿」，取作者最後一篇真正的稿子，跳過「無需修訂」與篇幅不到最長一篇六成的致謝回覆；審查者的規則加上「查不到作者用的資料是正常的，不算問題」，沒問題只回【通過】。
- **結論保留額度**：參與者用到整場 token 上限的九成就停（保留一成、最多 20,000 給主持人），預算用完也一定有結論。
- 發言會清掉模型漏出的控制記號（`<|channel>model`、`<|im_end|>`）與「（資料來源：無）」「（出處：作者）」這類不是出處的出處。

Four discussion problems, reported by staffKM from knowledge-base discussions on .70 xVITA.

- **A participant's brief is not evidence**: briefs and role instructions used to count as citable material, so a figure or document named there was never flagged as unchecked. Only the person's topic and background, and what tools returned, count now.
- **Consensus checked by code**: a consensus item whose figure or source appears in only one participant's turns is marked as such. Asking for "two or more agree" in the prompt did not hold with local models.
- **Review**: the room ends early once the author's draft stops changing (or the author only says nothing needs changing); the output adds a "final draft", the author's last real draft, skipping "no changes" replies and thank-you notes under six tenths of the longest draft; reviewers are told that not finding the author's data is normal, and to reply only 【通過】 when nothing needs changing.
- **A reserve for the summary**: participants stop at nine tenths of the room's allowance (a tenth, at most 20,000, is kept for the moderator), so a room that runs out still concludes.
- Turns are cleaned of leaked chat-template tokens and of "sources" that name nothing, such as （資料來源：無）.

## 1.17.0 — 2026-10-22

對照 DeepSeek Harness 補齊剩下的工具，並讓辯論角色能自己上網查證。

- **代理之間的訊息**：`list_agents`（看誰在做什麼）、`send_message`（傳話給另一個代理，下一步開始時讀到）、`report`（被啟動的代理把中途發現回報給啟動它的代理）、`interrupt_agent`（停掉自己啟動的代理；主代理可停全部，走審批）。訊息標示來源是代理而不是使用者，單則 4,000 字元、每個代理最多排 20 則，寫進 session（`agent-message`）；討論參與者不會拿到這些工具。
- **背景子代理**：`subagent` 加 `background: true`，父代理不用等，子代理的回報與最後答案以訊息送回。背景子代理一律唯讀（沒有人守在審批畫面前）、同時最多 4 個。
- **ralph**：同一個目標分輪做，每輪全新子代理，只看到目標與上一輪交接，進度記在工作目錄。輪數預設 5、最多 10（`[subagent] ralph_rounds`）；結果為 `complete`／`blocked`／`budget-limited`。
- **`session_search`／`session_read`**：模型自己查這個人過去的對話；只讀自己的 session，不含目前這段，回傳標示為舊內容、不是指令。
- **PowerShell**：`powershell` 工具與 `terminal` 的 `shell: "powershell"`（變數、目錄、載入的模組跨指令保留）；主機上找得到 `pwsh` 才會出現。持續 shell 的指令走私有暫存目錄的檔案而非標準輸入，指令讀輸入只會拿到空串流。與 bash 同一套審批與沙箱。
- **範例角色**：正方、反方、審查員加上 `web_search`、`browser_open`，節點有設瀏覽器時辯論會自己上網查官方公告；沒有就維持依推論論述。已安裝的站台請更新 `.xharness/agents/` 底下的檔案。
- **討論**：參與者把回合全用在查資料、一句話都沒說時（實測 .27：正方、反方各查了三份官方公告，用完 10 回合，整場 0 次發言、96,021 tokens），現在會再叫它一次，這次收走所有工具、依已查到的資料發言；範例角色的回合上限由 10 改為 16。
- 每場討論的 token 上限預設由 80,000 提高到 200,000：參與者開始上網查證後，實測 .27 第 1 輪就用掉 80,000（總計 150,000 完成兩輪前的一場）；要更省可在 `[discussion] max_tokens` 調回。
- `terminal` 只在主機找得到 `pwsh` 時才宣稱支援 PowerShell（實測 .70 沒裝，模型卻回答「有」）。
- 實測：本機 PowerShell 7.5.1 跑通變數、目錄、結束碼、中文、逾時重開；13 項新測試。

Closing the remaining gaps against DeepSeek Harness, and debate roles that can look things up on the web.

- **Messages between agents**: `list_agents`, `send_message` (read at the start of the receiver's next step), `report` (a started agent tells the agent that started it) and `interrupt_agent` (stop an agent you started; the main agent may stop any, with approval). Messages are labelled as coming from an agent, not the person; 4,000 characters each, 20 waiting per agent, logged to the session (`agent-message`); discussion participants do not get these tools.
- **Background subagents**: `subagent` with `background: true` returns at once and the child's reports and final answer come back as messages. Background children are read-only (nobody sits at an approval prompt for them) and capped at 4.
- **ralph**: one objective in rounds, each by a fresh child that sees only the objective and the last handoff, with the working directory holding the progress. 5 rounds by default, at most 10 (`[subagent] ralph_rounds`); result `complete`, `blocked` or `budget-limited`.
- **`session_search` / `session_read`**: the model looks things up in the person's earlier conversations; own sessions only, never the current one, results labelled as old text and not an instruction.
- **PowerShell**: a `powershell` tool and `terminal` with `shell: "powershell"` (variables, directory and modules persist); offered only where `pwsh` is found. The open shell takes commands from a file in a private temp directory rather than standard input. Same approval and sandbox as bash.
- **Example roles**: 正方, 反方 and 審查員 gain `web_search` and `browser_open`, so on a node with a browser configured a debate looks up official notices itself; without one they keep arguing from reasoning. Sites that installed the roles should update the files under `.xharness/agents/`.
- **Discussion**: a participant that spends every turn looking things up and never speaks (seen on .27: each side read three official notices, used all 10 turns, 0 utterances for 96,021 tokens) is now asked once more with every tool taken away to speak from what it found; the example roles' turn limit goes from 10 to 16.
- The per-discussion token limit defaults to 200,000 instead of 80,000: once participants look things up on the web, round 1 alone used 80,000 on .27. Lower it with `[discussion] max_tokens` to spend less.
- `terminal` mentions PowerShell only where `pwsh` is found (on .70, which has none, the model had answered "yes").
- Tested: PowerShell 7.5.1 locally for variables, directory, exit codes, Chinese text and restart after a timeout; 13 new tests.

## 1.16.2 — 2026-10-22

- 討論：辯論題目沒有資料可查時（例如「颱風天是否放假」），雙方原本都回「查不到、無法佐證」就停擺。現在提示詞把「事實」和「論點」分開：數字、日期、規定這類事實仍要查到並註明出處，查不到不准編；論點本身可以從原則、推論、利弊與具體情境說理，標明「依推論」，不需要來源。辯論與組對組裡，若某一輪只說查不到資料、沒有表達立場，主持人提醒一次請它直接論述（新的發言取代舊的）。範例角色「正方」「反方」同步改寫；已安裝的站台請自行更新 `.xharness/agents/` 底下的檔案。

- Discussion: a debate on a topic with nothing to look up (say, whether to close for a typhoon) used to stall with both sides answering "no data, cannot verify". The prompt now separates facts from arguments: figures, dates and rules still need a source and are never invented, while reasoning from principle, trade-offs and cases needs none and is labelled as reasoning. In debate and teams, a turn that only says nothing was found gets one moderator nudge to argue its side (the new turn replaces the old one). The example 正方/反方 roles are rewritten to match; sites that installed them should update the files under `.xharness/agents/`.

## 1.16.1 — 2026-10-22

- 路由：成本與位置都相同時，原本會挑強度較低的模型（沒有省任何東西、只是變差）；現在先選預設模型，再依設定檔順序。成本真的比較低時照樣選便宜的。

- Routing: with equal price and place the router used to pick the weaker model (saving nothing, only lowering quality); it now takes the default first, then config order. A model that really costs less still wins.

## 1.16.0 — 2026-10-22

自動路由：設定檔列了好幾個模型時，每個請求自己挑一個。預設關閉（`[routing] enabled = true`）。

- **三層決定，由嚴格到寬鬆**：(1) 硬規則由程式決定、不問模型——帳號分層沒開放的、來源國在 `deny_origins`（預設 `cn`）的、不支援工具或上下文放不下的排除；**外部模型永遠看不到個資、金鑰與內網位址**（身分證字號、手機、電子郵件、通過檢查碼的信用卡號、API 金鑰、內網 IP、操作者的關鍵字與規則運算式），預設也看不到任何來自本機的內容（讀過的檔案、工具結果、附件、圖片、hook 輸出；`external_sees = "all"` 才放行檔案），碰過本機內容的對話即使歷史被壓縮成摘要也一直算；送外部模型用精簡系統提示、不帶 `mcp__` 工具；全部被排除就報錯說明原因，不改送到被排除的模型。(2) 能力與成本：請求依字詞、長度、工具迴圈長度分簡單／中等／困難，挑成本最低而夠強的、同分自己的機器優先；中等這段可交給自家機器上的小模型（`decider`，只回一個字）判斷。(3) 備援：連不上的模型暫時跳過、換下一個，只在還沒輸出任何字之前才換；有備援待命時，連不上的模型不再等 8 秒重試。
- **手動指定仍受規則管**：`/model 名稱` 指定一個模型，個資與本機檔案的規則照樣有效；`/model auto` 回到自動；`/route` 看上一次的決定、誰被排除、誰連不上；`xharness route "請求"` 不呼叫模型直接試算。
- **看得見、查得到**：每個決定寫進 session（`route`、`route-fallback`，只記類別、不記比對到的內容）；網頁在每一步顯示「選用誰、為什麼」，個資或本機檔案擋下外部模型時明說；使用報告與 Word 月報統計各模型次數、因個資或本機檔案而不送外部的次數、備援次數、拒絕次數；機密防護檢查頁新增三項（外部模型看得到什麼、`where` 與位址不符、中國來源模型有沒有被擋）。
- Tailscale 位址（100.64.0.0/10）現在算自建（原本被當成外部，使用報告的「自建 vs 外購」會算錯）。
- 實測 .70（含一個模擬外部的模型與一個在 .27 的中國來源模型）：「你好」第一選擇連不上，改用備援並在畫面標明（連不上的模型有備援待命時只花 0.02 秒，沒有備援才照舊等 8 秒重試）；含身分證字號的請求只用私有模型、畫面說明原因；沒有個資的困難請求用模擬外部模型，它發出讀檔後檔案內容一回來就改走私有模型；對話碰過檔案後手動指定外部模型被拒絕並說明；中國來源模型被排除。決策小模型（OCR 微調的 e4b）把「整理資料夾」判成困難，不適合當決策模型——換用前先用 `xharness route` 驗過。

Model routing: with several models in the config, each request picks one. Off by default (`[routing] enabled = true`).

- **Three layers, strictest first**: (1) hard rules decided by code, never by a model -- models the account's tier does not offer, whose origin is in `deny_origins` (default `cn`), that cannot call tools or whose context is too small are out; **a public model never sees personal data, keys or internal addresses** (national ID, mobile number, e-mail, checksum-valid card numbers, API keys, internal IPs, the operator's words and regexes), and by default nothing from this machine (files read, tool results, attachments, pictures, hook output; `external_sees = "all"` lets files out); a conversation that touched local material stays marked even after its history is compacted into a summary; public-bound calls carry a minimal system prompt and no `mcp__` tools; with nothing left the request fails with the reason instead of falling back to an excluded model. (2) Capability and cost: easy, medium or hard by words, length and tool-loop length; the cheapest strong enough model wins, own hardware first on a tie; an optional small model on your own hardware (`decider`, one-word answer) settles the medium middle. (3) Fallback: an unreachable model is skipped and the next takes over, only before any text was produced; with a stand-in waiting, a dead model no longer costs the 8-second retry.
- **A manual pick still goes through the rules**: `/model name` pins a model, personal-data and local-file rules still apply; `/model auto` restores routing; `/route` shows the last decision, who was excluded and who is down; `xharness route "request"` rates a request without calling a model.
- **Visible and checkable**: every decision is written to the session (`route`, `route-fallback`: categories only, never the text that matched); the Web UI says which model was picked and why on every step, and says so when personal data or local files kept a public model out; the usage report and Word report count decisions per model, requests kept off public models, fallbacks and refusals; the security check gains three items (what public models can see, `where` disagreeing with the address, China-origin models not blocked).
- Tailscale addresses (100.64.0.0/10) now count as self-hosted (they were counted as external, which skewed the report's self-hosted vs bought-in split).
- Measured on .70 (with a stand-in for a public model and a China-origin model on .27): "你好" with an unreachable first choice moved to the stand-in and said so on screen (a dead model costs 0.02 s when a stand-in waits, still 8 s of retries when none does); a request with a national ID used only private models and explained why; a hard request without personal data used the stand-in public model, which asked to read a file and, as soon as the content came back, the router moved to a private model; after the conversation had touched a file, pinning the public model was refused with an explanation; the China-origin model was excluded. The decider tried (an OCR-tuned e4b) rated "tidy this folder" as hard and is not a good decider -- check yours with `xharness route` first.

## 1.15.0 — 2026-10-21

跨對話、跨機器的組對組：討論廳。

- **一組可以由另一個對話負責**：組對組裡把某一組標成遠端（`{"name": "學務組", "remote": true}`），討論區塊就出現邀請碼（`xhall:` 開頭）與「複製邀請碼」；另一個對話——同一台、別人的、或另一台機器的 xHarness——說「加入討論廳 xhall:…，我們是學務組：反方、審查員」就加入。新工具 `discussion_join`。
- **各組用自己那台的人、資料與模型**：組內討論只留在自己的對話，只有代表的公開發言送到討論廳、再轉給其他組；結論由開討論的一方寫，兩邊都收到。別台的發言標「來自哪台，數字由對方查核」。
- **連線方向永遠是加入的一方找討論廳**（長輪詢），.27 加入 .70 只需要 .27 連得到 .70。
- **討論廳用自己的憑證、不走帳號**：邀請碼只能加入、而且討論一開始就失效；加入後每組拿到自己的權杖，只能讀這一廳的公開紀錄、送自己這組的發言、離開；密鑰只放 Authorization 標頭、constant-time 比對；邀請碼只顯示在畫面上、不寫進紀錄；發言長度、組員數、同時開放的廳數有上限。SSDLC 威脅表新增一列。
- 實測跨機器：.70（gemma-4-31b-xvita）開討論廳、教務組用 招生數據.csv；.27（自己設定的模型）以學務組加入、用只有 .27 才有的 學務資料.csv。8 次發言、約 10 分鐘；學務組逐行引用自己的資料，還抓出檔案裡損益兩平人數算不起來；.70 的紀錄裡沒有任何一句學務組的組內討論，也沒有邀請碼。實測跨對話（同一台兩個分頁）：邀請碼、加入、標示都正常。
- 修：模型把要由別的對話加入的組寫成空成員時，錯誤訊息直接示範 `"remote": true` 的寫法（原本只說名字是空的，模型連錯三次）；結束原因的中文；加入討論廳的步驟名稱。
- 設定：`[discussion] join_minutes`、`turn_minutes`；`[web] public_url`（別台連得到的位址，不設就用瀏覽器連進來的位址）。只有網頁介面能開討論廳，終端機可以加入。

Team against team across conversations and machines: halls.

- **A team can be played by another conversation**: mark a team remote (`{"name": "學務組", "remote": true}`) and the discussion shows an invite code (starting `xhall:`) with a copy button; another conversation -- on this node, someone else's, or xHarness on another machine -- says "join the hall xhall:…, we are 學務組: 反方, 審查員" and joins. New tool `discussion_join`.
- **Every team uses its own machine's people, data and model**: internal talk stays in its own conversation; only spokesperson statements go to the hall, which relays them; the host writes the summary and both sides receive it. Statements from elsewhere are labelled with their origin and "figures checked by them".
- **The joining side always calls the hall** (long polling): .27 joining a hall on .70 needs only .27 to reach .70.
- **Halls use their own credentials, not accounts**: the invite only joins and stops working when the discussion starts; each joined team gets a token that can read that hall's public record, post its own statement and leave; secrets travel only in the Authorization header and are compared in constant time; the invite is shown on screen and never written to the record; statement length, members and open halls are capped. A new row in the SSDLC threat table.
- Measured across machines: .70 (gemma-4-31b-xvita) hosted with 教務組 on 招生數據.csv; .27 (its own configured model) joined as 學務組 with 學務資料.csv, which only .27 has. 8 utterances, about 10 minutes; 學務組 cited its own file line by line and found that the file's break-even figure does not add up; the .70 record holds none of 學務組's internal talk and no invite. Measured across conversations (two tabs on one node): invite, joining and labels all work.
- Fix: when a model writes a team that should join from elsewhere with blank members, the error now shows the `"remote": true` form (it used to say only that a name was empty, and the model failed three times); end reasons in Chinese; the join step's label.
- Settings: `[discussion] join_minutes`, `turn_minutes`; `[web] public_url` (an address other machines can reach; defaults to the one the browser used). Only the Web UI hosts a hall; the terminal can join.

## 1.14.0 — 2026-10-21

組對組討論：一個討論群跟另一個討論群討論。

- **新形式 `teams`（組對組）**：2～4 組、每組 1 人以上（全部最多 8 人）。每一輪各組先在組內討論（其他組看不到），代表最後發言、把組內意見收斂；接著各組代表對所有人公開發言；下一輪的組內討論從聽到別組的公開發言開始。代表預設是組裡第一位，也可以指定；只有一人的組直接公開發言。結論逐組寫出最後立場，並說明有沒有因為哪一組的論點而改變。數字與出處查核、逐字紀錄、token 上限、停止都照舊。
- 網頁上方列出對陣（例：教務組（正方、查資料員）對 學務組（反方、審查員）），每段發言標「某組・組內（其他組看不到）」或「某組・代表發言」，頭像依組別上色、組內發言縮排；匯出的逐字稿同樣標明。
- 直接說就會用：「開一場組對組討論：教務組（正方、查資料員）對學務組（反方、審查員），題目：要不要開夜間班，兩輪」。實測 .70 gemma-4-31b-xvita：模型自己選對形式與分組，12 次發言、26,758 tokens、約 8 分鐘；組內發言不外流；一位成員提到資料裡沒有的「2024」被要求先查後改正。殘留：兩組都把 110 學年的「12 人次」說成「去年」，數字是真的但用錯年份，結論照抄——比對數字擋不住這類誤讀。
- 修：出處寫成《檔名》時沒有去掉書名號，真的有讀的檔案被標成未查證出處。

Team against team: one discussion group talking with another.

- **A new `teams` mode**: 2-4 teams of one or more members (8 at most in all). Each round every team talks internally (the other teams cannot see it), its spokesperson speaking last to pull the team together; then each spokesperson speaks to everyone; the next round's internal talk starts from what the other teams said in public. The spokesperson is the first member unless named; a one-member team simply speaks in public. The summary gives each team's final position and whether another team's argument changed it. Figure and source checks, the verbatim record, token limits and Stop work as before.
- The Web UI shows the line-up (e.g. 教務組（正方、查資料員）對 學務組（反方、審查員）), tags every utterance "team, internal (other teams cannot see)" or "team, spokesperson", colours avatars by team and indents internal talk; the export marks the same.
- Ask in plain words: "開一場組對組討論：教務組（正方、查資料員）對學務組（反方、審查員），題目：要不要開夜間班，兩輪". Measured on .70 with gemma-4-31b-xvita: the model chose the mode and teams itself; 12 utterances, 26,758 tokens, about 8 minutes; no internal talk leaked; a member's unsourced "2024" was sent back and corrected. Residual: both teams called the 110 school year's "12 enquiries" "last year" -- a real figure in the wrong year, which the summary repeated; checking numbers cannot catch that kind of misreading.
- Fix: a source cited as 《file name》 kept its title marks and a file that had been read was flagged as unverified.

## 1.13.5 — 2026-10-21

- **升級後不會再看到舊畫面**：網頁本身原本沒有快取指示，部署新版後已開著的分頁照樣跑舊介面（1.13.4 修好的選單方向鍵，舊分頁裡還是壞的）。現在每次載入都向伺服器確認最新版；分頁開著時伺服器升級了，畫面上方會出現「xHarness 已更新到 x.y.z，重新整理才會用到新版」與「重新整理」按鈕。
- 指令選單每次打開都從第一項開始（原本會停在上次的位置）。
- 實測 Edge：方向鍵一次移一格（↓ 五次、↑ 兩次依序停在 2→6→4），滑鼠停在選單上按 ↓ 也照常移動；模擬伺服器升級後一分鐘內出現更新提示，位置在導覽列下方、不擋輸入框。

- **No stale interface after an upgrade**: the page itself carried no caching directive, so a tab left open across a deployment kept running the old interface (the arrow-key fix of 1.13.4 stayed broken in such a tab). The page now revalidates on every load, and when the server is upgraded under an open tab a banner says so with a reload button.
- The command menu starts at the first item each time it opens (it used to remember the last position).
- Measured in Edge: one row per key press (five ↓ and two ↑ step 2→6→4), arrows still move with the mouse resting over the menu, and after a simulated upgrade the banner appears within a minute, below the navigation bar, clear of the input box.

## 1.13.4 — 2026-10-21

- **指令選單用方向鍵選時跟得上**：輸入 `/` 後按 ↓，原本每按一次就把整個選單重建，清單的捲動位置被拉回頂端，超過第八項以後反白跑到看不見的地方、畫面像卡住。改成方向鍵只移動反白、清單在自己的框內捲動讓反白一直看得到（不捲動整頁）；滑鼠移過去與方向鍵共用同一個反白，不再同時亮兩列。實測 .70：按 ↓ 12 次停在第 13 項 `/feedback`，仍在框內可見。`@檔案` 選單同樣修正。

- **Arrow keys in the command menu keep up**: after typing `/`, every ↓ used to rebuild the whole menu, which reset its scroll to the top, so past the eighth item the highlight sank out of sight and the screen looked stuck. Arrow keys now only move the highlight, and the list scrolls inside its own box to keep it in view (the page does not move); hovering moves the same single highlight instead of lighting a second row. Measured on .70: twelve presses of ↓ land on item 13, `/feedback`, still in view. The `@file` menu is fixed the same way.

## 1.13.3 — 2026-10-21

- **角色名稱可以用中文**：原本只收英數字，所以範例角色只能叫 advocate、skeptic；現在任何文字都行（不能有空白或路徑符號），四個範例改名為 `查資料員`、`審查員`、`正方`、`反方`，首頁「代理討論」的範本改成「讓 正方 和 反方 辯論」。

- **Role names in any script**: names were limited to ASCII letters and digits, which is why the examples were called advocate and skeptic; any letters now work (no spaces or path characters), the four examples are `查資料員`, `審查員`, `正方` and `反方`, and the home page's discussion template reads "讓 正方 和 反方 辯論".

## 1.13.2 — 2026-10-21

- **艦隊頁不再跳動**：頁面每 2.5 秒更新一次，原本每次都把所有卡片與數字方塊整個拆掉重建，卡片的進場動畫（淡入並上移）就每 2.5 秒重播一次，整頁看起來一直在跳；在卡片裡回答代理提問時，打到一半的字也會被清掉。改成依任務比對、只更新有變的卡片，只有執行時間變就只改那個數字，進場動畫只在卡片第一次出現時播；正在輸入的卡片不會被換掉。實測 .70：連續三次更新，卡片元素不變、動畫 0 次、新增節點 0 個。
- **用量趨勢圖是平的**：開了帳號之後每個人的 session 存在自己的目錄，趨勢圖只讀共用目錄，所以總用量有三萬多 tokens 圖卻是 0；現在管理者看到所有人的、一般使用者看到自己的。
- **縱軸刻度**：最大值很小時刻度四捨五入成 1、1、1、0、0；改用 1、2、5 倍數的整數刻度。
- **「代理」數字**：艦隊頁的數字方塊與卡片原本只算子代理，現在連工作流程步驟與討論參與者一起算，卡片列出正在工作的代理名稱與角色。

- **The fleet page no longer jumps**: it refreshes every 2.5 seconds and used to tear down and rebuild every card and tile each time, replaying their entrance animation (fade in, slide up) so the whole page appeared to jump, and wiping a half-typed answer to an agent's question. Cards are now matched by task and only changed ones are redrawn; a new running time only updates that number; the entrance animation plays once; a card being typed in is never replaced. Measured on .70: three refreshes, same elements, 0 animations, 0 nodes added.
- **The usage trend was flat**: with accounts on, each person's sessions live in their own directory and the chart read only the shared one, showing 0 beside a total of over 30,000 tokens; admins now see everyone's and others their own.
- **Axis ticks**: a small maximum rounded its quarter ticks to 1, 1, 1, 0, 0; ticks are now whole 1-2-5 steps.
- **The "agents" count**: the fleet tile and cards counted only subagents; workflow steps and discussion participants count too, and cards list the working agents with their roles.

## 1.13.1 — 2026-10-21

- **找得到代理功能**：首頁「從這裡開始」新增「代理討論」，點下去把範本填進輸入框、選取題目讓你直接改，不會自己送出（原本登入後看不到任何代理相關的入口）。
- **現成角色**：`examples/agents/` 附四個角色：`researcher`（查資料員）、`reviewer`（審查員）、`advocate`（正方）、`skeptic`（反方），複製到站台的 `.xharness/agents/` 就給所有人用。

- **Finding the agents**: the home page's starters gain "代理討論" (agent discussion), which fills a template into the input box and selects the topic for you to type over, without sending it (until now nothing after sign-in pointed at the agent features).
- **Ready-made roles**: `examples/agents/` ships `researcher`, `reviewer`, `advocate` and `skeptic`; copy them into the site's `.xharness/agents/` to give them to everyone.

## 1.13.0 — 2026-10-21

管理 AI agent，並讓 agent 之間互相對話。

- **代理角色**：一個 Markdown 檔定義一個代理（工作說明、可用工具、模型、輪數與 token 上限），格式與 Claude Code 的 subagent 相同，`.claude/agents/` 也讀得到；`subagent`、`subagent_batch`、`workflow` 的步驟與討論參與者都能指定角色。角色只能縮小權限：工具與這個人原本可用的取交集，模型必須是設定檔裡、這個人可以用的。
- **代理管理**：每段對話登記正在工作的代理（子代理、工作流程步驟、討論參與者）。網頁「檢視 → 代理」看每個代理的角色、模型、正在做什麼、token 與呼叫次數，可個別停止、停止整場討論或全部停止；終端機 `/agents`、`/agents stop`。每個代理結束時在 session 留下 `agent-run`，服務使用報告與 Word 月報依角色拆開用量、失敗與被停止次數。
- **討論**：新的 `discussion` 工具讓代理之間對話，圓桌、辯論（自動分正反方、主持人裁決）、審查（寫與審來回到全數通過）、自由（主持人點名）四種形式。每位參與者有自己的對話、角色、工具與模型，只收到上次發言後的新內容；每次發言連同發言者、輪次與查過的工具寫進 session，網頁即時顯示，匯出含完整逐字稿；有輪數、每場 token 與每次發言呼叫數上限；結論分共識、分歧（點名）、待確認。
- **數字與出處要查得到**：每次發言的數字與引用的出處都和討論室實際握有的資料比對（題目、背景、工作說明、所有工具結果）。沒查資料就引用的參與者會被要求先查再重講一次；仍找不到的標成未查證、提醒後面的發言者、不准寫進共識。實測 gemma-4-31b-xvita：沒有這道檢查時，辯論雙方引用了資料檔裡沒有的註冊率，結論還把捏造的「畢業 42-48 人」列為共識；加上後，一位辯手的初稿列了九個資料裡沒有的數字，被點出後真的去讀 招生數據.csv、改用 57、54、49、46、44 人與 95%→73.3%，四段發言零未查證；另一場有人引用不存在的「六年制電資系研討會」（以及《本系開設夜間部評估方案》）並被後兩位照抄，現在會標成未查證出處。殘留風險：資料裡確實有的數字仍可能被用錯地方（例：把 81.7% 的那年也說成 90% 以上），國字數字（四成）不在比對範圍。
- **停止會連同代理一起停**：按下對話的停止，原本只停主代理、子代理會繼續跑，現在所有登記中的代理都會停；討論中按停止，正在跑的那次模型呼叫結束後就不再有人發言（實測約 20 秒收尾並寫出結論），在「先查再講」重問時按下也不會被吃掉。
- 修：從首頁「繼續最近的任務」打開一段仍在執行中的對話，原本會另開一個對話接續同一份 session 記錄、兩邊同時寫入；現在直接回到那段執行中的對話。
- 新指令 `/agents`；新設定段 `[agents]`、`[discussion]`；`Agent` 可指定自己的模型（`AgentOptions.llm`）與 token 上限（`max_tokens`）。

Managing AI agents, and letting agents talk to each other.

- **Agent roles**: one Markdown file defines an agent (instructions, tools, model, turn and token limits), in Claude Code's subagent format, and `.claude/agents/` is read too; `subagent`, `subagent_batch`, workflow steps and discussion participants can all name a role. A role can only narrow: its tools are intersected with what the person may already use, and its model must be configured and allowed for this person.
- **Managing agents**: each conversation registers the agents working for it (children, workflow steps, discussion participants). Review → Agents in the Web UI shows each one's role, model, current activity, tokens and calls, and stops one, a whole discussion, or all of them; `/agents` and `/agents stop` in the terminal. Every agent leaves an `agent-run` record, and the usage report and the Word report break spending, failures and stops down by role.
- **Discussions**: a new `discussion` tool lets agents talk to each other as a round table, a debate (sides assigned, the moderator gives a verdict), a review (write and review until everyone approves) or a free discussion (the moderator picks the speaker). Each participant has its own conversation, role, tools and model and hears only what was said since its last turn; every utterance is logged with speaker, round and tools used, shown live, and exported in full; rounds, tokens per room and calls per utterance are capped; the summary separates agreement, disagreement (by name) and what still needs checking.
- **Figures and sources must be found**: every number and every named source in an utterance is checked against what the room actually holds (topic, background, instructions, all tool results). A participant that quotes without looking is asked to check and speak again; what is still unsourced is marked, pointed out to later speakers, and kept out of the agreed summary. Measured on gemma-4-31b-xvita: without the check both sides of a debate quoted registration rates that were not in the data file and the summary listed an invented "42-48 graduates" as agreed; with it, a speaker whose first draft listed nine figures not in the data was told so, actually read 招生數據.csv and used 57, 54, 49, 46, 44 and 95% → 73.3%, leaving four utterances with nothing unverified; in other rooms an invented "六年制電資系研討會" (and 《本系開設夜間部評估方案》) copied by later speakers is now marked as an unverified source. Residual risk: a figure that is in the data can still be misapplied (calling a year at 81.7% "above 90%"), and numbers written in Chinese characters are not checked.
- **Stop stops the agents too**: the conversation's Stop used to stop only the main agent while children kept running; now every registered agent stops. In a discussion nobody speaks after the model call in flight returns (measured: about 20 seconds to wind down and write the summary), and a Stop pressed during a look-it-up retry is no longer lost.
- Fix: opening a still-running conversation from the home page's recent tasks used to open a second conversation on the same session log, both writing to it; it now returns to the running one.
- New command `/agents`; new config sections `[agents]` and `[discussion]`; an `Agent` can have its own model (`AgentOptions.llm`) and token ceiling (`max_tokens`).

## 1.12.0 — 2026-10-21

對照 DeepSeek Harness 補功能的第三批：生態系。三批到此完成。

- **Hooks**：照 Claude Code 的協定，現成的 hook 腳本不用改。`SessionStart`、`UserPromptSubmit`（補脈絡或擋下）、`PreToolUse`（放行、要求審批、拒絕）、`PostToolUse`（附註）、`Stop`（要求繼續，一個任務最多 3 次）、`PreCompact`（否決壓縮）；exit 2 擋下並把 stderr 給模型；比對器認得 Claude Code 的工具名稱。也支援 Codex 的 `notify`。只從操作者的設定檔載入，專案本身不能註冊 hook。
- **工具斷路器**：同一工具在一個任務裡被拒絕 3 次（hook、審批、計畫模式），或同參數連續呼叫 12 次，就收起到任務結束並告知模型。實測 bash 被 hook 擋下時，granite 原本重試 37 次直到輪數上限，現在 3 次就換方法並說明做不到的部分。
- **ACP**：`xharness acp` 讓 Zed 等支援 Agent Client Protocol 的編輯器直接使用：回覆串流、工具呼叫附種類與狀態、寫檔與編輯附差異、審批變成編輯器的許可提示（允許一次／一律允許／拒絕）、可取消。
- **LSP**：新的 `lsp` 工具（定義、引用、型別說明、診斷、大綱），依副檔名自動啟動已安裝的語言伺服器並保持到對話結束，位置可只給行號與名稱。純標準函式庫的 JSON-RPC 客戶端。實測接 pylsp 每次查詢約 1 秒。
- **提醒**：`schedule_add`／`schedule_list`／`schedule_cancel` 與 `/remind`；時間到以標明為提醒的新請求交給模型，可重複；寫進 session、接續時還原並補送錯過的；只在網頁介面開著對話時送出，終端機裡模型會說做不到。
- **工作流程**：新的 `workflow` 工具，把有相依關係的步驟交給子代理依序執行、可平行，結果往下傳，失敗就跳過依賴它的步驟；檢查未知步驟與循環。
- **新指令**：`/remind`、`/hooks`。
- 修：`glob`／`grep` 搜尋不存在的目錄時回「沒有這個目錄」而不是「沒有符合」，讀檔失敗會提醒用相對路徑，system prompt 要求用相對路徑——實測 granite 打錯一長串絕對路徑後，被「沒有符合」誤導成以為檔案不存在。提醒請模型用提醒文字的語言回覆。

Third of three batches closing the gap with DeepSeek Harness: the ecosystem. The three batches are complete.

- **Hooks**: Claude Code's protocol, so existing hook scripts work unchanged. `SessionStart`, `UserPromptSubmit` (add context or block), `PreToolUse` (allow, ask, deny), `PostToolUse` (note), `Stop` (continue, at most 3 times a task), `PreCompact` (veto); exit 2 blocks with stderr given to the model; matchers know Claude Code's tool names. Codex's `notify` too. Loaded only from the operator's configuration; a repository cannot register a hook.
- **Tool circuit breaker**: a tool refused 3 times in a task (hook, approval policy, plan mode), or called 12 times in a row with the same arguments, is withdrawn for the rest of the task and the model is told. Measured: with bash blocked by a hook granite retried it 37 times until the turn limit; now it changes approach after 3 and says what it could not do.
- **ACP**: `xharness acp` lets editors that speak the Agent Client Protocol, such as Zed, use xHarness directly: streamed replies, tool calls with kind and status, diffs for writes and edits, approvals as the editor's permission prompts (allow once / always / reject), cancellation.
- **LSP**: a new `lsp` tool (definition, references, hover, diagnostics, outline) that starts an installed language server for the file type and keeps it for the conversation; a position can be a line plus a name. A standard-library JSON-RPC client. Measured against pylsp: about one second per query.
- **Reminders**: `schedule_add` / `schedule_list` / `schedule_cancel` and `/remind`; due reminders arrive as requests marked as reminders, optionally repeating; logged, restored on resume and caught up; delivered only while the Web UI holds the conversation, and in the terminal the model says it cannot set one.
- **Workflows**: a new `workflow` tool runs steps with dependencies as subagents in order, in parallel where possible, passing results forward and skipping steps built on a failure; unknown steps and cycles are rejected.
- **New commands**: `/remind`, `/hooks`.
- Fix: `glob` / `grep` under a directory that does not exist now say so instead of "no matches", a failed read suggests a relative path, and the system prompt asks for relative paths -- measured: granite mistyped a long absolute path and, told "no files matched", concluded existing files were gone. Reminders ask for a reply in the reminder's own language.

## 1.11.0 — 2026-10-21

對照 DeepSeek Harness 補功能的第二批：能力上限。

- **計畫模式**（輸入框旁的按鈕或 `/plan`）：會改東西的工具與子代理整組從模型的清單收起，硬要呼叫也會被擋；模型只查看、列出編號計畫，回覆底下的「核准計畫並執行」才關閉計畫模式照做。實測 granite 在計畫裡寫錯了日期的星期，正是這一步要讓人看一眼的原因。
- **目標模式**（`/goal 目標`，`--rounds N` 調輪數，預設 5、上限 20）：模型停下卻沒用新的 `goal_done` 回報完成或卡住時，自動再給一輪，直到完成、卡住、輪數用完或停止；畫面上方有目標橫幅。輪數要明寫，`/goal 2026 年的報表` 不會被當成 2026 輪。
- **背景工作**（`job_start`／`job_status`／`job_wait`／`job_stop`）：長指令丟到背景跑，模型繼續做事；與 bash 同樣走審批與沙箱，同時最多 4 個、最長 2 小時（`[jobs]`），記錄在 `.xharness/jobs/`，對話結束時一併停止，完成的工作在下一則訊息告訴模型。檢視面板新增「背景」頁。
- **終端機**（`terminal`）：一直開著的 shell，`cd`、`export`、啟用的虛擬環境會留到下一次；逾時就重開在原目錄並告知模型變數已重設；每個代理各自一個。
- **@檔案**：輸入框打 `@` 列出工作目錄的檔案，選取的文字檔內容隨訊息送出（只限工作目錄內、單檔 2 萬字、合計 6 萬字）；`@圖片` 當成圖片附上。終端機也支援。
- **看圖**：可以貼上、拖進或附加圖片（每則 4 張、每張 5 MB）。供應端 `vision = true` 直接看；看不懂圖的模型由 `[vision] provider` 指定的模型先轉成文字描述（實測 .70 的 xReadable OCR 把錯誤畫面一字不差轉出，granite 據此給出排查步驟）；都沒有時明白告訴模型它看不到。描述模型不會出現在換模型清單。
- 安全：帶圖片的訊息上限 30 MB，但只在身分驗證通過後才放寬；新的 `nosec` 與信任邊界登記在 SSDLC 文件。

Second of three batches closing the gap with DeepSeek Harness: what the agent can take on.

- **Plan mode** (button next to the composer, or `/plan`): every mutating tool and the subagent tools are withdrawn from the model's list and refused if called anyway; the model looks and proposes a numbered plan, and "approve and run" under the reply turns plan mode off and carries it out. In testing granite got a weekday wrong in its plan, which is what this step is for.
- **Goal mode** (`/goal objective`, `--rounds N`, 5 by default, at most 20): when the model stops without reporting done or blocked through the new `goal_done`, it gets another round, until done, blocked, out of rounds or stopped; a banner shows progress. The round count is explicit, so `/goal 2026 年的報表` is not 2026 rounds.
- **Background jobs** (`job_start` / `job_status` / `job_wait` / `job_stop`): long commands run in the background while the model keeps working; same approval and sandbox as bash, 4 at once and 2 hours each (`[jobs]`), logs in `.xharness/jobs/`, stopped when the conversation ends, announced to the model with the next message once finished. The review pane has a background tab.
- **Terminal** (`terminal`): a shell that stays open, so `cd`, `export` and an activated virtualenv carry over; on a timeout it restarts in the same directory and the model is told variables were reset; one per agent.
- **@files**: `@` in the composer lists working-directory files; the chosen text file goes with the message (inside the working directory only, 20,000 characters each, 60,000 in all); `@picture` attaches an image. Works in the terminal too.
- **Images**: paste, drop or attach (4 per message, 5 MB each). A provider with `vision = true` sees them; for one that cannot, the `[vision] provider` model writes a description first (in testing the xReadable OCR model on .70 transcribed an error screenshot exactly and granite worked from it); with neither, the model is told it cannot see. The describer is not offered as a chat model.
- Security: messages with images may be 30 MB, but only once the caller is authenticated; new `nosec` sites and trust boundaries are registered in the SSDLC document.

## 1.10.0 — 2026-10-21

對照 DeepSeek Harness 補功能的第一批（共三批）：使用者每天會碰到的部分。

- **技能（Skills）**：含 `SKILL.md` 的資料夾就是一個技能，格式與 Claude Code、Codex 相同。從工作目錄的 `.xharness/skills`／`.claude/skills`、伺服器目錄的 `.xharness/skills`、`$XHARNESS_HOME/skills` 與 `[skills] dirs` 找，同名時前者優先；`~/.claude/skills` 要 `claude_home = true` 才讀。system prompt 只放名稱與一句說明，模型用新的 `skill` 工具載入全文與附檔清單。
- **斜線指令**：`/compact` `/model` `/skills` `/title` `/feedback` `/export` `/new` `/stop` `/tools` `/memory` `/usage` `/help`，網頁輸入框打 `/` 會跳出清單（方向鍵、Enter），終端機用同一套。指令由 xHarness 處理、不送給模型，執行紀錄寫進 session。看起來像指令卻不存在的字（例如終端機的 `/stop`）直接回「沒有這個指令」：實測 xVITA 收到 `/stop` 後回報「已修改兩個檔案」，其實一個工具都沒呼叫。
- **模型摘要壓縮**：供應端設 `context_tokens` 後，prompt 超過 `[compaction] threshold`（預設八成）就先請同一個模型把較早的內容整理成摘要；遇到「超出 context」也先摘要、不夠才刪舊工具輸出。切點一定落在使用者訊息，不會拆散工具呼叫與結果；摘要寫進 session，接續時在同一位置重建。
- **換模型**：網頁輸入框的選單或 `/model` 為這段對話換成設定檔裡的另一個供應端；`[web] model_switch` 可關閉，各層級可用 `providers = [...]` 限制。每個任務的用量記錄是哪個模型跑的，服務使用報告的「自建 vs 外購」與模型統計改為逐任務計算。
- **標題、搜尋、匯出**：第一個任務完成後由這段對話的模型寫短標題（`[sessions] auto_title`）；任務清單的搜尋查所有舊對話的提問、回覆與標題；對話可匯出成 Markdown（時間以台北時間呈現）。
- **回覆評價**：每則回覆底下可複製、按讚或倒讚、寫備註。評價寫進 session 但不回到模型；服務使用報告「健康與治理」列出讚、倒讚數與最近的倒讚備註，Word 報告同步呈現。
- 修：編輯內容前後相同時不再顯示「+0 −0」的空預覽；測試裡的假金鑰改為執行時組合，避免機密掃描誤判。

First of three batches closing the gap with DeepSeek Harness: what people touch every day.

- **Skills**: a folder holding `SKILL.md` is a skill, in Claude Code's and Codex's layout. Found in the working directory's `.xharness/skills` / `.claude/skills`, the server directory's `.xharness/skills`, `$XHARNESS_HOME/skills` and `[skills] dirs`, earlier winning on a name clash; `~/.claude/skills` only with `claude_home = true`. The system prompt carries names and one line each; the model loads a body and its file list with the new `skill` tool.
- **Slash commands**: `/compact` `/model` `/skills` `/title` `/feedback` `/export` `/new` `/stop` `/tools` `/memory` `/usage` `/help`, with a picker when `/` is typed in the composer; the terminal uses the same set. Commands are handled by xHarness, never sent to the model, and logged. Something that looks like a command but is not one (such as `/stop` in the terminal) is answered with "no such command": handed `/stop` as a request, xVITA reported editing two files without calling a tool.
- **Model-written compaction**: with `context_tokens` on a provider, a prompt past `[compaction] threshold` (80% by default) has the same model summarise the older part first; a context overflow is also answered by a summary before old tool output is dropped. The cut is always at a person's message, so tool calls stay with their results; the summary is logged and rebuilt on resume.
- **Switching models**: a conversation can move to another configured provider from the composer or with `/model`; `[web] model_switch` turns it off and `providers = [...]` limits a tier. Each task's usage records its model, and the usage report's self-hosted vs bought-in split and model counts are now per task.
- **Titles, search, export**: after the first task the conversation's model writes a short title (`[sessions] auto_title`); the task list searches the requests, replies and titles of every earlier conversation; a conversation exports as Markdown (times in Taipei time).
- **Ratings**: each reply can be copied, rated up or down and given a note. Ratings are logged but never reach the model; the usage report's health block (and the Word report) counts them and lists the latest notes on poor answers.
- Fix: an edit whose old and new text are identical no longer shows an empty "+0 −0" preview; fake keys in tests are assembled at runtime so secret scanners do not flag them.

## 1.9.1 — 2026-10-21

- 修 1.9.0 版面左右不對稱：寬螢幕預設展開任務清單、而且把內容推開，看起來就是固定左側欄；標題列的「新任務」與「檢視」又貼在畫面最左最右，跟置中的內容對不齊，右邊留一大塊空白。現在任務清單與檢視面板都是浮在頁面上的圓角卡片、預設收起，選了任務就收；寬螢幕打開時內容欄在剩下的空間重新置中。標題列、內容、輸入框對齊同一個寬度，收起時左右留白完全相同。
- 還沒送出任何訊息的空任務不再出現在任務清單。

- Fix 1.9.0's lopsided layout: wide screens opened the task list by default and pushed the content aside, which read as a fixed sidebar, and the header's title and review button sat at the window edges while the content was centred. Both the task list and the review pane are now floating rounded cards that start closed and close after a choice; on a wide screen the column re-centres in the space left when one is open. Header, content and composer share one width, with equal margins on both sides.
- A task with nothing sent yet no longer appears in the task list.

## 1.9.0 — 2026-10-20

Web UI 照 Codex 的工作方式重做，並修正一個遮罩漏洞。

- **三塊版面**：左邊可收起的任務清單（等你回應／進行中／這次開啟的／較早的任務，可搜尋，點一下接續；有任務等你許可時頂列顯示數字），中間對話，右邊檢視面板（變更／檔案／工具／記憶）。固定左側欄不用，寬螢幕預設展開、窄螢幕滑出。
- **步驟**：每個工具呼叫一行，例如「讀取 README.md」「修改 notes.md +3 −1」，有進行中、完成、失敗與秒數，點開看參數與結果；一輪做完自動收合，有失敗的保持展開。
- **審批直接看差異**：write／edit 開始時伺服器先算出 unified diff（新事件 `change`），許可卡片裡就是修改前後的對照；檢視面板列出這個任務改過的每個檔案與狀態（等你許可／已套用／已拒絕／沒有套用）。超過 512 KB 的檔案只列名稱，差異最多 400 行，內容一樣過遮罩。預覽失敗不會影響任務。
- `tool_start`／`tool_end`／`approval_request` 帶同一個 `call` 編號，前端不再用工具名稱猜是哪一次呼叫。
- 模型回覆照 markdown 顯示（表格、清單、粗體、程式碼），用 DOM 節點組出來，模型輸出永遠不會變成 HTML。
- 任務進行中送出鈕變成停止鈕；首頁有深色主視覺、進行中／等你回應／近 24 小時用量三格數字、建議任務與「接續最近的任務」；`/api/sessions` 多回第一則提問當預覽。
- 艦隊與管理頁換成同一套外觀，拿掉卡片色條，原生確認框換成自己的對話框，提示改用 toast；深淺色預設跟著系統。
- 修：`scrub()` 只遮到 `sk-proj`／`sk-ant` 這段前綴，後面真正的金鑰原樣留在日誌與錯誤訊息裡；`.env` 寫法（`OPENAI_API_KEY=...`、`DB_PASSWORD="..."`）完全沒遮。現在整把金鑰都遮，大寫環境變數名稱的指派也遮，`max_tokens=16384` 這類一般文字不受影響。

The Web UI is rebuilt around how Codex works, and a masking gap is closed.

- **Three parts**: a collapsible task list on the left (waiting for you / running / opened this session / earlier, searchable, one click to resume; the top bar counts tasks waiting for approval), the conversation in the middle, and a review pane on the right (changes / files / tools / memory). No fixed sidebar: open by default on wide screens, a slide-over on narrow ones.
- **Steps**: one line per tool call, such as "read README.md" or "edit notes.md +3 −1", with running, done, failed and seconds, opening to arguments and result; a finished turn folds its steps away unless one failed.
- **Approvals show the diff**: when a write or edit starts, the server computes a unified diff (new `change` event), so the approval card shows the before-and-after; the review pane lists every file the task changed and its state (waiting / applied / denied / not applied). Files over 512 KB are named only, diffs stop at 400 lines, and contents are masked like every other surface. A failing preview never affects the task.
- `tool_start`, `tool_end` and `approval_request` carry the same `call` number, so the page no longer guesses which call an event belongs to by tool name.
- Replies render as markdown (tables, lists, bold, code), built as DOM nodes; model output never becomes HTML.
- While a task runs the send button becomes stop; the landing has a dark hero, running / waiting / last-24-hours counts, suggested tasks and recent tasks to resume; `/api/sessions` returns each session's first request as a preview.
- Fleet and admin pages share the new look, cards lose their colour bars, native confirm boxes become an in-page dialog, notices become toasts; light and dark follow the system by default.
- Fix: `scrub()` masked only the `sk-proj` / `sk-ant` prefix and left the actual key in logs and error messages, and `.env` lines (`OPENAI_API_KEY=...`, `DB_PASSWORD="..."`) were not masked at all. The whole key is masked now, as are assignments to upper-case environment names; prose such as `max_tokens=16384` is left alone.

## 1.8.0 — 2026-10-15

讓模型問人而不是猜，並參考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 補上長任務的幾個防護。

- **`ask_user` 工具**：模型缺一個只有人知道的事實時，提出 2～4 個選項的問題，等回答再繼續同一個任務。Web UI／桌面版是問題卡（選項按鈕＋可自己打字，有輸入法防護）、艦隊視圖可代答其他節點、Telegram 每個選項一顆按鈕且直接回文字也算、終端機列編號選項。無人值守時立刻告訴模型「沒有人在」，不會卡住；15 分鐘沒人回或任務被停止，模型會被告知沒有回答。子代理拿不到這個工具。
- **system prompt 每次帶今天日期與星期**（含 UTC 偏移），自訂 system prompt 也帶。實測沒有它時 granite 認定今天是 2025-04-21，並把錯的日期寫進檔案。對話開著過夜，換日後第一則訊息會附上新日期。
- **相對日期寫入前先確認**：任務有「下週五」這類日期又要寫進檔案時，先算出確切日期並跟人確認。實測 granite 換算「下週五」8 次錯 8 次（給了日曆也一樣，所以日曆沒留）；加上這條後 8 次裡有 6 次在動手前把算出的日期講出來讓人看，把錯日期直接寫進檔案的從 3 次降到 1 次。這是安全網，不是讓模型算對。
- **先看再問、用同一種字體回答**：要問「哪一份」之前先用工具看工作目錄；繁體中文提問用繁體中文回答。
- **模型請求自動重試**：502／503／504／500／429 與連線被拒，等 2 秒、6 秒各重試一次（照 `Retry-After`，最多 30 秒）。只在回應開始前重試，已串流的字不會重複；400、401、找不到主機不重試。
- **大輸出改存檔**：超過 16,000 字的工具輸出全文存到工作目錄的 `.xharness/spill/`（自帶 `.gitignore`，留最近 20 份），模型只看開頭、結尾與檔名，中間可用 `read` 分段讀。bash 原本 50,000 字就截斷丟棄。
- **context 爆了自救一次**：模型回「超出 context」時，把較舊的工具輸出換成一行說明後重送，保留最近兩筆，原文仍在 session 記錄。
- **重複呼叫提醒**：同工具同參數連續第 3、5、8 次時，在結果後附註「結果不會變」。只提醒，不擋。
- **模型看到的都進記錄**：system prompt（身分、日期、`AGENTS.md`）以 `context` 事件寫進 session，事後查得到模型當時被告知什麼。
- 修：終端機 `/clear` 之後的 system prompt 少了日期、站台與專案指示。
- 問題卡與審批卡拿掉左側色條，改用淡色底。

Letting the model ask instead of guess, plus long-task safeguards modelled on [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).

- **`ask_user` tool**: when the model is missing a fact only the person has, it puts a 2-4 option question and carries on with the same task once answered. Web UI / desktop show a question card (option buttons plus free text, IME-safe); the fleet view answers for any node; Telegram gives one button per option and a plain reply also counts; the terminal lists numbered options. Unattended runs tell the model at once that nobody is there, so nothing blocks; no answer within 15 minutes, or a stopped task, is reported as no answer. Subagents do not get the tool.
- **Today's date and weekday in every system prompt** (with the UTC offset), custom prompts included. Without it granite took today to be 2025-04-21 and wrote that wrong date into a file. A conversation left open overnight gets the new date with its first message of the day.
- **Confirm relative dates before writing them**: when a task gives a date like "next Friday" that will go into a file, work it out and confirm it with the person. Measured: granite resolved "下週五" wrongly in 8 of 8 runs (with or without a calendar, so the calendar was dropped); with this rule it stated its date before acting in 6 of 8 runs, and wrote a wrong date straight into the file in 1 of 8, down from 3. A safety net, not a fix for the arithmetic.
- **Look before asking, answer in the same script**: check the working directory before asking "which one"; answer Traditional Chinese in Traditional Chinese.
- **Model requests retry**: 502/503/504/500/429 and refused connections retry after 2 s and 6 s (`Retry-After` honoured up to 30 s), only before the response starts, so streamed text is never repeated; 400, 401 and unknown hosts fail at once.
- **Large output is saved, not dropped**: tool output over 16,000 characters is saved in full under `.xharness/spill/` (own `.gitignore`, latest 20 kept); the model sees head, tail and file name and can `read` the middle. bash used to cut at 50,000 characters and discard the rest.
- **Recovering once from a full context**: when the model reports the context exceeded, older tool outputs become a one-line note and the request is resent, keeping the latest two; the originals stay in the session log.
- **Repeat-call note**: the 3rd, 5th and 8th identical call in a row gets a note that the result will not change. Advice only.
- **What the model sees is logged**: the system prompt (identity, date, `AGENTS.md`) is written to the session as a `context` event.
- Fix: after `/clear` in the terminal the system prompt lost the date and the site and project instructions.
- Question and approval cards drop the left colour bar for a tinted background.

## 1.7.3 — 2026-10-07

權杖模式的節點（`--token` 或 `XHARNESS_WEB_TOKEN_FILE`，沒有 `[auth]`）從瀏覽器幾乎進不去。

- **存好的權杖用不到，每次重新載入都要再貼一次。** 1.6.0 加上傳時，`loadFiles()` 被放在 `loadToken()` 前面，第一個請求不帶權杖就出去了。現在先讀權杖、再打第一個請求。
- **兩個原生 `prompt()` 疊在一起，取消任何一個就只剩裸的 `HTTP 401`。** 頁面載入時檔案清單與 meta 同時 401，各跳一個框；取消後掉進通用錯誤分支，回應已讀過一次，只剩狀態碼，沒說要什麼、去哪拿。
- **改法：權杖模式也走正式的登入頁**，換成一個「存取權杖」欄位並說明權杖存放在哪、該向誰要。送出前先拿去打 `/api/meta` 驗證，錯的不存。瀏覽器裡存著已失效的權杖（節點輪換過）時會自動清掉並回到登入頁。`prompt()` 整個拿掉。

Token-mode nodes (`--token` or `XHARNESS_WEB_TOKEN_FILE`, no `[auth]`) were close to unusable from a browser.

- **A saved token was never used, so every reload asked again.** When uploads arrived in 1.6.0, `loadFiles()` was placed before `loadToken()`, so the first request went out without a credential. The saved token is now read before anything is sent.
- **Two native `prompt()` boxes stacked, and cancelling either left a bare `HTTP 401`.** On page load the file list and the meta request both failed and each opened a box; cancelling fell through to the generic error branch with the response already consumed, leaving only a status code.
- **Token mode now uses the real sign-in page**, with a single access-token field that says where the token lives and who to ask. The value is checked against `/api/meta` before it is kept, so a wrong one is never stored; a stale saved token (after rotation) is cleared and the page returns to sign-in. `prompt()` is gone.

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
