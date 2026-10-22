# xHarness 架構圖（arch-diagram-style：技術論文式分段圖）。用法：python3 architecture.py zh|en 輸出路徑.svg
import sys

sys.path.insert(0, "/Users/jeff/.claude/skills/arch-diagram-style")
from arch_diagram import Diagram, BLUE, GREEN, VIOLET, ORANGE, CYAN, PINK, GREY
from msicons import ms_badge

LANG = sys.argv[1] if len(sys.argv) > 1 else "zh"
OUT = sys.argv[2] if len(sys.argv) > 2 else f"architecture_{LANG}.svg"
T = {
    "zh": {
        "s1": ("入口", "五種進來的方式，同一個核心", "同一套審批與稽核"),
        "s2": ("核心", "一切皆插件，約 40 個內建工具", "零執行期相依"),
        "s3": ("治理與稽核", "每一步都攔得下來、查得到", "預設不對外傳資料"),
        "web": ("Web UI 與艦隊", "xharness web", ["串流逐字稿、審批卡", "代理分頁、討論廳", "多節點合併呈現"]),
        "cli": ("CLI 與 REPL", "xharness", ["一次性任務（headless）", "互動 REPL、斜線指令", "xharness route 試算"]),
        "desk": ("桌面版", "xharness desktop", ["原生視窗，不佔分頁", "loopback 隨機埠", "關窗即停"]),
        "tg": ("Telegram", "手機下任務", ["每個聊天是一段對話", "允許／拒絕按鈕", "只服務白名單聊天"]),
        "acp": ("編輯器", "xharness acp", ["Zed 等 ACP 編輯器", "diff 與許可請求", "讀 Claude Code hooks"]),
        "core": ("Context 核心", "約 150 行，沒有特權", ["服務 provide / get", "事件 on / emit", "中介層 intercept / invoke", "unload 反序回捲"]),
        "loop": ("Agent 迴圈", "模型 → 工具 → 模型", ["計畫模式、目標模式", "自動壓縮、斷路器", "子代理、背景子代理", "工作流程、ralph 分輪"]),
        "tools": ("工具", "每個都是插件", ["檔案與指令：read bash terminal", "代理：subagent discussion", "記憶與對話：memory session_search", "外部：MCP web_search powershell"]),
        "llm": ("模型", "任何 OpenAI 相容端點", ["llama.cpp vLLM Ollama", "自動路由：成本、能力、備援", "硬規則：個資不送外部", "排除指定來源國的模型"]),
        "gate": ("審批與沙箱", "有副作用先問人", ["Seatbelt / bwrap 圈住 bash", "token 與呼叫數煞車", "每人每日每月配額", "登入鎖定、hook 攔截"]),
        "log": ("只增不改的記錄", "每一步都落地", ["session JSONL 逐事件", "記憶 Markdown 與主題頁", "audit.jsonl 稽核", "路由與代理訊息事件"]),
        "out": ("給人看的產出", "數字由程式算", ["服務使用報告 Word / PDF", "機密防護檢查頁", "eval 評測通過率", "艦隊用量趨勢"]),
        "a12": "任務、審批", "a23": "每步寫記錄、過閘門", "model_out": "模型端點",
        "leg": [(BLUE, "入口"), (GREY, "核心"), (GREEN, "模型與迴圈"), (PINK, "工具"), (ORANGE, "閘門"), (VIOLET, "記錄"), (CYAN, "產出")],
    },
    "en": {
        "s1": ("Entrances", "five ways in, one core", "same approval and audit"),
        "s2": ("Core", "everything is a plugin, about 40 built-in tools", "zero runtime dependencies"),
        "s3": ("Governance and audit", "every step can be stopped and traced", "no data leaves by default"),
        "web": ("Web UI and fleet", "xharness web", ["Streamed transcript, approvals", "Agents tab, discussion halls", "Nodes merged in one view"]),
        "cli": ("CLI and REPL", "xharness", ["One-shot task (headless)", "Interactive REPL, / commands", "xharness route dry run"]),
        "desk": ("Desktop", "xharness desktop", ["Native window, no tab", "Random loopback port", "Closing the window stops it"]),
        "tg": ("Telegram", "tasks from a phone", ["Each chat is a conversation", "Allow / deny buttons", "Allow-listed chats only"]),
        "acp": ("Editors", "xharness acp", ["Zed and other ACP editors", "Diffs and permission asks", "Reads Claude Code hooks"]),
        "core": ("Context core", "about 150 lines, no privileges", ["Services provide / get", "Events on / emit", "Middleware intercept / invoke", "unload rolls back in reverse"]),
        "loop": ("Agent loop", "model, tools, model", ["Plan mode, goal mode", "Auto compaction, circuit breaker", "Subagents, background agents", "Workflows, ralph rounds"]),
        "tools": ("Tools", "each one a plugin", ["Files: read bash terminal", "Agents: subagent discussion", "Memory: memory session_search", "Outside: MCP web_search powershell"]),
        "llm": ("Models", "any OpenAI-compatible endpoint", ["llama.cpp vLLM Ollama", "Routing: cost, ability, fallback", "Hard rule: no personal data out", "Excludes chosen origin countries"]),
        "gate": ("Approval and sandbox", "side effects ask first", ["Seatbelt / bwrap around bash", "Token and call budgets", "Per-user daily, monthly quota", "Login lockout, hook veto"]),
        "log": ("Append-only records", "written at every step", ["Session JSONL per event", "Markdown memory and topics", "audit.jsonl", "Route and agent-message events"]),
        "out": ("Output for people", "numbers computed by code", ["Usage report, Word / PDF", "Secret protection check", "Eval pass rates", "Fleet usage trend"]),
        "a12": "tasks, approvals", "a23": "every step: gates, records", "model_out": "model endpoint",
        "leg": [(BLUE, "Entrance"), (GREY, "Core"), (GREEN, "Model and loop"), (PINK, "Tools"), (ORANGE, "Gate"), (VIOLET, "Records"), (CYAN, "Output")],
    },
}[LANG]

W = 1620
d = Diagram(width=W, height=1320)
X0, XW = 52, 1516


def row(y, hue, items, h, icons):
    """一排等寬卡片；items = [(key, hue)]，每張卡 4 顆（或 3 顆）晶片。"""
    n = len(items)
    gap = 20
    cw = (XW - gap * (n - 1)) // n
    for i, (key, ch, icon) in enumerate(items):
        x = X0 + i * (cw + gap)
        title, sub, chips = T[key]
        d.card(x, y, cw, h, ch, title, sub)
        d.parts.append(ms_badge(icon, x + cw - 40, y + 40, 22, ch.main))
        for j, label in enumerate(chips):
            d.chip(x + 20, y + 86 + j * 44, cw - 40, 34, ch, label, center=False)
    return cw


# 1 入口
d.band(28, 28, 1564, 334, BLUE)
d.section(1, *T["s1"][:2], BLUE, y=66, badge=T["s1"][2])
row(114, BLUE, [("web", BLUE, "Web"), ("cli", BLUE, "Management console"), ("desk", BLUE, "Monitor"),
                ("tg", BLUE, "Phone"), ("acp", BLUE, "Laptop")], 226, None)

# 2 核心
y2 = 418
d.band(28, y2, 1564, 380, GREEN)
d.section(2, *T["s2"][:2], GREEN, y=y2 + 38, badge=T["s2"][2])
row(y2 + 86, GREEN, [("core", GREY, "Settings"), ("loop", GREEN, "Continous Cycle Circle"),
                     ("tools", PINK, "Tool"), ("llm", GREEN, "Server (generic)")], 272, None)

# 3 治理與稽核
y3 = 854
d.band(28, y3, 1564, 380, ORANGE)
d.section(3, *T["s3"][:2], ORANGE, y=y3 + 38, badge=T["s3"][2])
row(y3 + 86, ORANGE, [("gate", ORANGE, "Firewall"), ("log", VIOLET, "Database generic"), ("out", CYAN, "Reports")], 272, None)

# 段與段之間的箭頭（垂直箭頭的膠囊由產生器放在右側）
d.arrow(800, 364, 800, y2 - 2, BLUE, T["a12"])
d.arrow(800, y2 + 382, 800, y3 - 2, GREEN, T["a23"])

d.legend(T["leg"], y=1262)
d.save(OUT, zoom=2)
