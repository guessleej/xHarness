# SSDLC：安全軟體開發生命週期

[English](ssdlc.md) | 中文

本文是 xHarness 的安全骨幹：架構所依據的威脅模型、每個生命週期階段強制執行的管控、
以及我們接受並揭露的殘餘風險。每個功能變更都應通過這些階段；任何動到信任邊界的變更，
必須在同一個 pull request 裡同步更新威脅模型。

## 一、威脅模型

### 資產

| 資產 | 所在位置 |
|---|---|
| 供應端憑證 | 只存在環境變數，由設定檔的 `api_key_env` 指名；絕不進 repo、設定檔或 session 記錄 |
| 使用者的檔案系統 | 經 `read`/`write`/`edit`/`bash` 工具以執行者權限存取 |
| Session 記錄 | `$XHARNESS_HOME/sessions/` 下的 JSONL；可能含檔案內容與指令輸出 |
| 模型端點 | xHarness 唯一會連線的網路目的地 |

### 信任邊界

1. **模型輸出是不受信任的輸入。** Tool call 來自 LLM，而它的 context 可能含有
   對抗性內容（讀到的檔案夾帶提示注入）。因此 agent 把每個 tool call 都當成敵意輸入：
   不明名稱與非法 JSON 變成錯誤結果，有副作用的工具動手前必須通過審批策略。
2. **工具讀回的內容不受信任。** `read`/`grep`/`bash` 回傳的任何東西都可能試圖
   引導模型。xHarness 無法讓模型免疫；它用審批策略與工具輸出上限來限縮爆炸半徑。
3. **設定檔與插件模組受信任。** 它們是使用者自有的程式與設定，權力等同親手執行 Python。
4. **網路邊界就是設定的端點。** `base_url` 必須是 `http` 或 `https`
   （adapter 建構時驗證，`file:` 與自訂 scheme 到不了請求路徑），
   憑證只附加在送往該端點的請求上。

### 威脅與緩解

| 威脅 | 緩解 | 狀態 |
|---|---|---|
| 提示注入驅動破壞性 tool call | 審批策略：有副作用的工具（`bash`、`write`、`edit`）在 `prompt` 模式先問人；互動模式預設 `prompt` | 已緩解；`auto` 下有殘餘風險（見下） |
| 憑證外洩 | 金鑰每次請求時才從環境變數解析；絕不寫入設定、記錄或 session 檔；只以 header 送往已驗證的端點 | 已緩解 |
| 經 `base_url` 的 SSRF / scheme 濫用 | `OpenAIAdapter.__init__` 強制 scheme 白名單（`http`/`https`），附回歸測試 | 已緩解 |
| 密鑰誤入 repo | `xharness.toml` 列入 gitignore；CI 以 gitleaks 掃描每次 push 與 PR | 已緩解 |
| 供應鏈入侵 | 零執行期相依（純標準函式庫）；僅開發用相依由 CI 的 pip-audit 每週與每次 push 稽核 | 已緩解 |
| 惡意或損毀的 session resume 資料 | JSONL 逐行解析；損毀行跳過；內容是資料，永不執行 | 已緩解 |
| Agent 失控（成本 / DoS） | `max_turns` 上限、逐指令逾時、每個工具的輸出大小上限 | 已緩解 |
| 不受信任 repo 的惡意 `AGENTS.md`/`CLAUDE.md` 引導 agent（system prompt 注入） | 指示檔是 repo 內容：載入它是信任決定。有大小上限（24k 字元）、可用 `--no-instructions` / `project_instructions = false` 關閉；有副作用的工具仍受審批策略把關 | 已緩解；不受信任 repo 配 `auto` 模式仍有殘餘風險 |
| 模型把檔案內容外送到端點 | 設計本質：模型必須看到檔案內容才能工作。請把 xHarness 指向你信任的端點（本設計對地端友善） | 接受並揭露 |

### 殘餘風險（接受並揭露）

- **沒有沙箱。** `bash` 以執行者完整權限運作。審批策略是唯一防線；
  用 `--approve auto` 跑不受信任的任務明確不在威脅模型內。檔案系統沙箱在藍圖上。
- **插件就是程式。** 插件模組以完整直譯器權限執行。只載入信任的路徑。
- **Session 記錄是明文。** 它繼承 `$XHARNESS_HOME` 的目錄權限；
  請像保護 shell 歷史一樣保護該目錄。

## 二、生命週期管控

### 階段一：需求與威脅建模

任何新增工具、網路目的地或資料存放的功能，都要說明其信任邊界影響。
邊界有變，本文件在同一個 pull request 內更新。

### 階段二：設計

架構強制的規則：

- 模型輸出只能經工具註冊表進入 harness，絕不經 `eval`/`exec`，
  也不在 `bash` 工具明示的契約之外對模型文字做 shell 內插。
- 審批決定屬於 agent（策略層），永遠不屬於工具。
- 信任邊界上的失敗一律 fail-closed：不明的供應端 scheme、缺少的服務、
  壞掉的插件都拒絕繼續，而不是靜默降級。

### 階段三：實作

- 執行期**只用標準函式庫**。要新增執行期相依，必須在本文件寫下理由並做供應鏈審查。
- Repo 內不放密鑰：憑證是環境變數參照（`api_key_env`），
  工作設定檔 `xharness.toml` 列入 gitignore。
- 邊界輸入驗證：URL scheme 白名單、tool 參數 JSON 驗證、session JSONL 容錯解析、
  輸出上限與逾時。
- 每個 `# nosec` 抑制都要在現場附理由註解，並在本文件登錄
  （現有：`tools/bash.py` 的 `B404`/`B603`/`B607`——執行指令就是該工具的本職；
  `llm.py` 的 `B310`——scheme 已在建構時驗證）。

### 階段四：驗證

每次 push 與 pull request 的 CI 閘門（`.github/workflows/`）：

| 閘門 | 工具 | Workflow |
|---|---|---|
| 單元測試，3 個 Python 版本 | pytest | `ci.yml` |
| SAST | bandit（任何發現即失敗） | `security.yml` |
| 相依稽核 | pip-audit | `security.yml`（另有每週排程） |
| 密鑰掃描，全歷史 | gitleaks | `security.yml` |

會縮減信任邊界覆蓋的 pull request（移除驗證、放寬權限），必須在描述中明說。

### 階段五：發布

- `pyproject.toml` 與 `xharness/__init__.py` 版本號同步調升，打附註 tag。
- 發布 commit 上所有 CI 閘門全綠；沒有未附理由的抑制項。

### 階段六：維運與應變

- 弱點依 [SECURITY.zh.md](../SECURITY.zh.md) 私下回報：
  3 個工作天內回覆確認，確認成立的問題 14 天內提出修補或緩解方案。
- 安全修補進 `main`，並在 release notes 標明受影響版本。

## 三、部署方的安全使用指引

- 互動作業：維持 `approval = "prompt"`。
- Headless 自動化：用專用低權限帳號、在容器或 VM 內執行，工作目錄限縮在任務範圍。
- `base_url` 指向你自營或信任的端點；xHarness 不會連其他任何地方。
- 把 `$XHARNESS_HOME` 視為敏感資料；供應端金鑰照你既有的密鑰管理流程輪替
  （它們只存在環境變數）。
