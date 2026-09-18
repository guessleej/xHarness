# 貢獻指南

[English](CONTRIBUTING.md) | 中文

謝謝你的興趣。xHarness 刻意保持小巧，任何變更的門檻是「一個下午仍讀得完整個套件」。

## 基本規則

- **零執行期相依。** 只用標準函式庫。要新增執行期相依，必須在 `docs/ssdlc.zh.md` 寫下理由並做供應鏈審查。
- **一切皆插件。** 新能力一律經 `Plugin.apply(ctx, config)` 掛載；沒有任何東西能走私路進核心。
- **信任邊界要寫下來。** 變更若新增工具、網路目的地或資料存放，同一個 pull request 內更新 `docs/ssdlc.zh.md`（與 `docs/ssdlc.md`）的威脅模型。
- **測試隨變更一起來。** `python -m pytest -q` 必須通過；`bandit -r xharness` 必須乾淨，每個 `# nosec` 都要在現場與 SSDLC 登錄附理由。
- **文件雙語。** 影響使用者的變更同時更新 `README.md`（中文）與 `README.en.md`。
- 程式碼、文件、commit 訊息一律不用 emoji。

## 流程

```sh
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
./.venv/bin/pip install "bandit[toml]" && ./.venv/bin/bandit -r xharness
```

對 `main` 開 pull request；CI 會在 Python 3.11/3.12/3.13 跑測試，外加 bandit、pip-audit、gitleaks。安全問題走 [SECURITY.zh.md](SECURITY.zh.md)，不要開公開 issue。
