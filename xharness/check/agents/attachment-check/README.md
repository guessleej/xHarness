# 核銷附件缺漏檢核（attachment-check）

擴充能力的示範：這支 Agent 只有 `agent.toml`、`rules.py`、`tables.schema.toml` 與驗證案例，平臺核心沒有為它改任何一行。接入新情境照這個資料夾複製一份改。

## 輸入

`claim_id`（核銷單號）、`claim_type`（核銷類別）、`applicant`、`claim_date`、`attachments[]`（每份 `kind`）。

## 判定值表

`required_attachments`：核銷類別、單據代碼、單據名稱。

## 規則

| 編號 | 提示 |
|---|---|
| AC-01 | 應附單據缺漏 |
| AC-02 | 核銷類別未設定應附單據（轉人工） |
