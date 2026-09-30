# 學校與公部門部署指南

這份文件寫給要把 xHarness 裝進一所學校或一個機關的人：資訊中心的承辦、系統整合商、或是去做交付的工程師。

內容是「怎麼裝、怎麼交、交之前要檢查什麼」。xHarness 本身的功能說明在 [README](../README.md)，安全開發流程在 [SSDLC](ssdlc.md)。

## 先講架構：這不是一台中央伺服器

最常見的誤解是把 xHarness 當成「架一台大家連進來」的平台。它不是。agent 的價值在於它碰得到那台機器上的檔案、專案與算力，所以 **agent 跑在要幹活的那台機器上，hub 只是看板**。

一所學校的標準配置是三層：

| 層 | 放什麼 | 跑在哪 |
|---|---|---|
| 模型層 | `llama-server` 跑自家模型，只開放校內網段 | 機房的 GPU 伺服器，一台 |
| 使用者層 | 老師／學生的 xHarness（桌面版或瀏覽器） | 各自的電腦，或一台共用的多使用者節點 |
| 治理層 | hub 看板、稽核、服務使用報告 | 一台不需要 GPU 的小機器 |

使用者層有兩種做法，依學校的規模與習慣選：

- **每人一份（桌面版）**：發 `xHarness.exe` 或 `xHarness.app` 給老師，雙擊就開，資料留在自己的電腦。隔離靠作業系統帳號，天然歸得到人。要讓資訊中心看得到這些機器，在設定檔加 `[desktop] node = true`。
- **一台共用（多使用者節點）**：一台機器跑 `xharness web`，全校從瀏覽器進來，每個人登入後有自己的對話、自己的 session 目錄與自己的配額。適合電腦教室、或不想在每台機器上安裝東西的學校。

兩種可以並存。

## 模型：來源國先確認

公部門與公立學校的案子**不得採用中國來源的模型**（Qwen／通義千問、DeepSeek、GLM／智譜、Kimi、豆包、MiniMax、Hunyuan 等）。這在選型的第一步就要確認，不是等到驗收才發現。

云碩自研的 **xVITA** 系列可直接用於這類案子。參考配置：

```sh
llama-server -m Gemma-4-31B-xVITA-zhTW-Q6_K.gguf \
  --host 0.0.0.0 --port 8080 \
  --ctx-size 131072 --parallel 4 -ngl 999 \
  --reasoning off --alias xvita-31b
```

`--parallel 4` 搭配 131072 的總 context，等於每個請求 32768 —— 這是機群的下限要求，低於這個值長文件會被截斷。以一張 48GB 的卡估算，可支撐 5 到 10 人同時使用。

## 部署步驟

### 一、多使用者節點

```toml
# ~/.xharness/config.toml
default_provider = "xvita"
approval = "prompt"

[providers.xvita]
base_url = "http://<機房 GPU 機>:8080/v1"
model = "gemma-4-31b-xvita-zhtw"

[telemetry]
cost_per_1k_tokens = 0.06    # 外購同級 API 的參考單價，用於報表對照

[sandbox]
mode = "require"

[auth]
backend = "local"
session_hours = 8
default_quota_tokens_per_day = 200000
default_quota_tokens_per_month = 3000000

[auth.users.itadmin]
role = "admin"
display = "資訊中心"
```

```sh
xharness users add itadmin --role admin      # 密碼是互動輸入的，不會留在指令歷史
xharness users add teacher_chen --display "陳老師"
xharness web --host 0.0.0.0 --port 3080      # 有 [auth] 就不需要共用 token
```

以 systemd user 服務常駐（記得 `loginctl enable-linger <帳號>`，否則登出就停）：

```ini
[Unit]
Description=xHarness node
After=network-online.target

[Service]
Type=simple
Environment=XHARNESS_HOME=/home/xcloud/.xharness
WorkingDirectory=/home/xcloud/xharness-node
ExecStart=/home/xcloud/xharness/.venv/bin/xharness web --host 0.0.0.0 --port 3080 --no-open
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

### 二、接學校的目錄服務

不想另外管一套帳號時，改用學校既有的 AD 或 OpenLDAP。密碼不會留在這台機器上：

```toml
[auth]
backend = "ldap"

[auth.ldap]
url = "ldaps://ad.school.edu.tw"
user_dn = "{user}@school.edu.tw"        # OpenLDAP 用 "uid={user},ou=people,dc=..."
allow_any = true                        # false 時只有列在 [auth.users] 的人能用
verify_certificate = true
```

一律用 `ldaps://`。學校若是自建 CA，把根憑證裝進這台機器的系統信任區，不要去關 `verify_certificate` —— 關掉之後任何人都能冒充目錄伺服器收走全校的密碼。

### 三、桌面版

給不會用命令列的老師：

```sh
pip install "xharness[desktop]"
python3 packaging/desktop/build.py        # 產出 xHarness.app / .dmg / .exe
```

連同一份預先填好模型端點的 `config.toml` 一起發。要讓資訊中心看得到這些機器，在該檔加上：

```toml
[desktop]
node = true
node_port = 3080
```

權杖會在第一次啟動時自動產生成 0600 的檔案，把它交給 hub，不要交給使用者。

## 沙箱：Ubuntu 24.04 會踩到的一件事

`[sandbox] mode = "require"` 表示沒有可用的沙箱後端就拒絕啟動，這是交付到學校時該用的設定。但 **Ubuntu 24.04 預設停用未授權的 user namespaces**，bwrap 雖然裝著卻探測失敗，服務會直接拒絕建立對話並回報：

```
sandbox required but no working backend: bwrap (installed but probe failed, e.g. user namespaces disabled)
```

三條路，依學校的資安政策選：

1. **開啟 user namespaces**（需要機關同意，這會放寬核心的一道保護）：
   ```sh
   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
   echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/60-userns.conf
   ```
2. **為 xharness 寫一份 AppArmor profile**，只放行這支程式，不動全系統設定。
3. **接受 `mode = "auto"`**，並在交付文件裡寫明這台機器沒有指令沙箱。機密防護檢查頁會把它列成待改善項，不會假裝沒事。

不要在沒有跟機關確認的情況下就去關核心的保護設定。

## 交付前檢查清單

每一項都能當場驗給對方看：

| 項目 | 怎麼驗 | 通過標準 |
|---|---|---|
| 身分 | 開 `/`，未登入時看得到登入頁 | 不能有任何人不登入就下任務 |
| 隔離 | 用兩個老師帳號各建一個對話 | 彼此的對話與 session 清單都看不到對方 |
| 配額 | 管理頁看帳號列表 | 每人的每日上限都設好了 |
| 稽核 | `xharness users audit` | 登入成功與失敗都有記錄，且不含密碼 |
| 資安 | 管理頁「機密防護檢查」 | 必修項為 0；建議項逐條向對方說明 |
| 報告 | 管理頁「服務使用報告」，或 `xharness report` | 六塊都有數字，待處理事項有具體建議 |
| 報表文件 | `python3 scripts/report/build_usage_report.py report.json -o 使用報告.docx` | 產出的 Word 打得開、目錄可點、圖表有資料 |
| 模型來源 | `curl <模型端點>/v1/models` | 不是中國來源的模型 |
| 對外連線 | 管理頁「對外串接」一節 | 除了模型端點之外，沒有預期外的對外連線 |

最後一項是這套系統對學校最有說服力的地方：除了設定檔裡那個模型端點，xHarness 不會把任何資料送到任何地方，沒有匿名 ID、沒有用量回傳。這一點可以請對方自己用防火牆日誌驗證。

## 每月的例行工作

```sh
xharness report --since 30d --json report.json
python3 scripts/report/build_usage_report.py report.json -o "114年10月_使用報告.docx"
```

報告裡的「健康與治理」一章會列出待處理事項。其中最常見的兩項：

- **未歸戶用量**：有人直接用 CLI 跑任務，沒有經過登入。請他們改從瀏覽器操作，或為排程任務建一個專用帳號。
- **零用量的節點**：設備在線但整個月沒人用，通常是沒人知道它可以用，不是設備壞了。

帳號的處理原則只有一條：**離職、畢業、轉單位一律停用，不要刪除**。帳號刪掉之後，歷史用量就歸不了戶，稽核也查不回是誰做的。`xharness users disable <帳號>` 做的就是停用而不是刪除。
