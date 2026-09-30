"""Secret-handling self-check: one page an operator can open to see whether
this install would survive a security review, in plain language, with the
fix next to each finding.

The three rules it checks against:
  1. an environment variable is not a place to keep a secret -- it shows up
     in `docker inspect`, /proc/<pid>/environ and any env dump, so a file
     (0600, or /run/secrets) is the safe form and env is a fallback
  2. secrets travel in an Authorization header, never in a URL, where access
     logs, proxies and error messages would keep a copy
  3. anything the program must read back in clear text is stored encrypted,
     and file permissions are the last line when it is not

It never prints or returns a secret's value: only whether one is in the
wrong place, and what to do about it.
"""

from __future__ import annotations

import os
import re
import stat
from typing import Any

from .config import _find_config_file
from .identity import audit_file, users_file
from .session import harness_home, sessions_dir

PASS, WARN, FAIL = "pass", "warn", "fail"
# Keys whose value is a secret if it appears literally in a config file.
INLINE_SECRET = re.compile(
    r'^\s*(api_key|apikey|password|passwd|secret|token|bot_token|client_secret)\s*=\s*["\']?[^"\'\s#][^"\'\n]*',
    re.IGNORECASE | re.MULTILINE,
)
SECRET_IN_URL = re.compile(r'[?&](api[-_]?key|key|token|access_token|password)=', re.IGNORECASE)
WEAK_VALUES = {"changeme", "password", "secret", "token", "test", "admin", "xharness", "123456"}


def _mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _group_or_world_readable(path: str) -> bool | None:
    mode = _mode(path)
    return None if mode is None else bool(mode & 0o077)


def _item(key: str, title: str, state: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"id": key, "title": title, "state": state, "detail": detail, "fix": fix}


def run(config: Any, *, bound_host: str | None = None, has_token: bool = False, users: Any = None) -> dict[str, Any]:
    """Every check, against the live configuration. Read-only."""
    items: list[dict[str, str]] = []
    config_path = _find_config_file(None)

    # --- 1. secrets written straight into the config file ----------------
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = ""
        hits = [match.group(1).lower() for match in INLINE_SECRET.finditer(text)]
        # *_env and *_file lines name a source, they do not hold the secret.
        real = [name for name in hits if not re.search(rf"{name}_(env|file)\s*=", text, re.IGNORECASE)]
        if real:
            items.append(_item(
                "config-inline-secret", "設定檔裡有明文機密", FAIL,
                f"設定檔 {config_path} 有 {len(real)} 個欄位直接寫著機密值（欄位名：{', '.join(sorted(set(real)))}）。"
                "設定檔常被複製、進版控、貼進工單，機密一起跟著走。",
                "改用 api_key_env = \"XHARNESS_API_KEY\" 或 token_file = \"~/.xharness/…\"，把值移到 0600 的檔案裡。",
            ))
        else:
            items.append(_item(
                "config-inline-secret", "設定檔裡沒有明文機密", PASS,
                "設定檔只指出機密放在哪個環境變數或檔案，沒有機密本身。", "",
            ))
        exposed = _group_or_world_readable(config_path)
        items.append(_item(
            "config-permissions", "設定檔權限", WARN if exposed else PASS,
            f"{config_path} 目前{'同群組或其他使用者讀得到' if exposed else '只有你讀得到'}。",
            f"chmod 600 {config_path}" if exposed else "",
        ))
    else:
        items.append(_item(
            "config-inline-secret", "找不到設定檔", WARN,
            "目前以環境變數執行。環境變數會出現在 docker inspect 與 /proc/<pid>/environ，任何一次 env dump 都會帶走。",
            "建立 ~/.xharness/config.toml，機密改走 *_file。",
        ))

    # --- 2. secrets in URLs ----------------------------------------------
    base_url = str((getattr(config, "provider", {}) or {}).get("base_url") or "")
    if SECRET_IN_URL.search(base_url):
        items.append(_item(
            "secret-in-url", "模型端點網址裡帶著機密", FAIL,
            "base_url 的查詢字串含有金鑰參數。網址會留在反向代理的 access log、錯誤訊息與瀏覽器歷史裡。",
            "把金鑰改走 api_key_env，讓它只出現在 Authorization 標頭。",
        ))
    else:
        items.append(_item(
            "secret-in-url", "機密只走標頭不進網址", PASS,
            "模型端點網址沒有夾帶金鑰；xHarness 一律把憑證放在 Authorization 標頭。", "",
        ))

    # --- 3. account and token files ---------------------------------------
    path = users_file()
    if os.path.exists(path):
        exposed = _group_or_world_readable(path)
        items.append(_item(
            "users-file", "帳號檔權限", FAIL if exposed else PASS,
            f"{path} {'其他使用者讀得到，裡面是所有人的密碼雜湊' if exposed else '只有擁有者讀得到（0600）'}。",
            f"chmod 600 {path}" if exposed else "",
        ))
    for name, node in ((getattr(config, "fleet", {}) or {}).get("nodes", {}) or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get("token") or node.get("token_value"):
            items.append(_item(
                f"node-token-{name}", f"節點 {name} 的 token 寫在設定檔", FAIL,
                "節點權杖直接寫在設定檔裡，等同把整台機器的操作權附在檔案上。",
                "改用 token_file 指向 0600 的檔案，或 token_env。",
            ))
        token_file = node.get("token_file")
        if token_file:
            resolved = os.path.expanduser(str(token_file))
            exposed = _group_or_world_readable(resolved)
            if exposed is None:
                items.append(_item(
                    f"node-token-{name}", f"節點 {name} 的 token 檔不存在", FAIL,
                    f"設定指向 {resolved}，但這個檔案讀不到，hub 會連不上這個節點。",
                    "重新產生節點 token 並寫入該檔案（chmod 600）。",
                ))
            else:
                items.append(_item(
                    f"node-token-{name}", f"節點 {name} 的 token 檔權限", WARN if exposed else PASS,
                    f"{resolved} {'其他使用者讀得到' if exposed else '權限正確（0600）'}。",
                    f"chmod 600 {resolved}" if exposed else "",
                ))

    telegram = (getattr(config, "channels", {}) or {}).get("telegram") or {}
    if telegram:
        inline = bool(telegram.get("token"))
        items.append(_item(
            "telegram-token", "Telegram bot token 來源", FAIL if inline else PASS,
            "token 直接寫在設定檔裡。" if inline else "token 來自檔案或環境變數，沒有進設定檔。",
            "改用 token_file 或 token_env。" if inline else "",
        ))

    # --- 4. network exposure ----------------------------------------------
    loopback = bound_host in (None, "127.0.0.1", "localhost", "::1", "[::1]")
    if loopback:
        items.append(_item(
            "binding", "服務只綁本機", PASS,
            "目前只在 127.0.0.1 上聽，網路上的其他機器連不到。", "",
        ))
    elif has_token:
        items.append(_item(
            "binding", "對外綁定且需要權杖", PASS,
            f"綁在 {bound_host}，每個 /api 請求都要帶 Bearer 權杖。", "",
        ))
    else:
        items.append(_item(
            "binding", "對外綁定卻沒有權杖", FAIL,
            f"綁在 {bound_host} 而沒有設定權杖，同網段的任何人都能下任務。",
            "加上 --token，或改綁 127.0.0.1。",
        ))

    # --- 5. identity, quota, sandbox ---------------------------------------
    enabled = bool(users and getattr(users, "enabled", False))
    items.append(_item(
        "identity", "使用者身分", PASS if enabled else WARN,
        "已啟用登入，每筆用量與稽核都歸得到人。" if enabled
        else "未啟用 [auth]：所有人共用同一個權杖，稽核記錄記得到做了什麼，記不到是誰做的。",
        "" if enabled else "在設定檔加入 [auth]，再用 xharness users add 建立帳號。",
    ))
    if enabled and getattr(users, "backend", "") == "ldap":
        ldap = (getattr(users, "settings", {}) or {}).get("ldap") or {}
        url = str(ldap.get("url") or "")
        secure = url.startswith("ldaps://")
        verify = bool(ldap.get("verify_certificate", True))
        items.append(_item(
            "ldap-tls", "LDAP 連線加密", PASS if (secure and verify) else FAIL,
            ("以 ldaps 連線並驗證憑證。" if secure and verify
             else "密碼會以未加密或未驗證憑證的方式送到目錄伺服器，同網段可被側錄。"),
            "" if (secure and verify) else "改用 ldaps://，並保持 verify_certificate = true（自建 CA 請把根憑證裝進系統信任區）。",
        ))

    sandbox = str((getattr(config, "sandbox", {}) or {}).get("mode", "auto"))
    items.append(_item(
        "sandbox", "指令沙箱", {"require": PASS, "auto": WARN, "off": FAIL}.get(sandbox, WARN),
        {"require": "沒有沙箱後端就拒絕啟動，保證每個指令都被圈住。",
         "auto": "有後端就圈住，沒有後端會退回無沙箱執行——在未裝 bwrap 的機器上等於沒有隔離。",
         "off": "沙箱關閉，bash 工具可以寫到工作目錄以外的任何地方。"}.get(sandbox, "設定值無法辨識。"),
        "" if sandbox == "require" else '在設定檔寫 [sandbox] mode = "require"。',
    ))

    # --- 6. weak defaults and data at rest ---------------------------------
    weak = [name for name, value in os.environ.items()
            if name.startswith("XHARNESS_") and value.strip().lower() in WEAK_VALUES]
    items.append(_item(
        "weak-values", "預設或弱權杖", FAIL if weak else PASS,
        f"環境變數 {', '.join(weak)} 的值是常見的預設字串，等同沒有保護。" if weak
        else "沒有偵測到預設或明顯過弱的權杖值。",
        "以 python3 -c \"import secrets;print(secrets.token_urlsafe(32))\" 重新產生。" if weak else "",
    ))

    for label, path in (("工作階段記錄", sessions_dir()), ("存取稽核記錄", audit_file()), ("資料目錄", harness_home())):
        exposed = _group_or_world_readable(path)
        if exposed is None:
            continue
        items.append(_item(
            f"perm-{os.path.basename(path) or 'home'}", f"{label}權限", WARN if exposed else PASS,
            f"{path} {'其他使用者讀得到，裡面是完整的對話內容' if exposed else '只有擁有者讀得到'}。",
            f"chmod -R go-rwx {path}" if exposed else "",
        ))

    counts = {state: sum(1 for item in items if item["state"] == state) for state in (PASS, WARN, FAIL)}
    return {
        "items": items,
        "summary": counts,
        "verdict": FAIL if counts[FAIL] else (WARN if counts[WARN] else PASS),
        "note": "本頁只檢查機密放在哪裡、誰讀得到，永遠不顯示機密內容。",
    }
