# README 用的功能圖（arch-diagram-style，ARCH_FS_SCALE=1.35 讓縮進 README 後字仍看得清）。
# 用法：python3 features.py zh|en 輸出資料夾
import os
os.environ.setdefault("ARCH_FS_SCALE", "1.35")
import sys

sys.path.insert(0, "/Users/jeff/.claude/skills/arch-diagram-style")
from arch_diagram import Diagram, BLUE, GREEN, VIOLET, ORANGE, AMBER, CYAN, PINK, GREY
from msicons import ms_badge

LANG = sys.argv[1] if len(sys.argv) > 1 else "zh"
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
W, X0, XW = 1400, 52, 1296
HUE = {"blue": BLUE, "green": GREEN, "violet": VIOLET, "orange": ORANGE, "amber": AMBER, "cyan": CYAN, "pink": PINK, "grey": GREY}

# 每張圖：[(色, 段號, 段標, 副標, 徽章, 卡片們)]；卡片 = (色, 圖示, 標題, 副標, [晶片], 實心晶片數)
Z = {
"quickstart": [
 ("blue", 1, "三步上手", "不到一分鐘", "零執行期相依", [
   ("blue", "Import generic", "安裝", "Python 3.11 以上", ["git clone", "pip install -e ."], 0),
   ("green", "Server (generic)", "指向模型", "任何 OpenAI 相容端點", ["XHARNESS_BASE_URL", "XHARNESS_MODEL"], 0),
   ("pink", "Management console", "下任務", "終端機或網頁", ["xharness \"任務\"", "xharness web"], 0)])],
"toolcall": [
 ("orange", 1, "一次工具呼叫的路", "每一步都攔得下來", "有副作用先問人", [
   ("green", "Continous Cycle Circle", "提出", "模型要用工具", ["tool call", "參數驗證"], 0),
   ("grey", "Filter", "過濾", "收起不該看的", ["計畫模式", "角色工具限制", "斷路器"], 0),
   ("orange", "Policy", "審批", "有副作用才問", ["自動通過", "問人", "拒絕"], 1),
   ("orange", "Firewall", "沙箱", "圈住 bash", ["Seatbelt", "bwrap", "可斷網"], 0),
   ("pink", "Tool", "執行", "有上限與逾時", ["輸出上限", "逾時重開"], 0),
   ("violet", "Database generic", "記錄", "只增不改", ["session JSONL", "稽核"], 0)])],
"agents": [
 ("green", 1, "分工", "把事交給別的代理", "每個動作回流審批", [
   ("green", "Continous Cycle Circle", "主代理", "你對話的那個", ["下任務", "收訊息"], 0),
   ("blue", "Workstation client", "子代理", "同步、全新對話", ["subagent", "subagent_batch"], 0),
   ("cyan", "Queue, general", "背景子代理", "唯讀、同時最多 4 個", ["不用等", "訊息送回"], 0),
   ("amber", "Task list or Backlog", "ralph", "分輪接力", ["每輪全新", "進度在檔案"], 0)]),
 ("violet", 2, "對話", "代理之間自己談", "數字逐一對照出處", [
   ("violet", "Message", "討論", "圓桌、辯論、審查", ["逐字紀錄", "結論留分歧"], 0),
   ("pink", "Groups", "組對組", "每組組內先商量", ["組內私下", "代表公開"], 0),
   ("orange", "Internet", "討論廳", "跨對話、跨機器", ["邀請碼", "各組權杖"], 0),
   ("grey", "Phone", "訊息", "list / send / report", ["下一步讀到", "每則進記錄"], 0)])],
"routing": [
 ("orange", 1, "自動路由", "三層，由嚴到寬", "預設關閉", [
   ("blue", "Message", "請求", "每個請求各挑一個", ["個資？", "本機檔案？"], 0),
   ("orange", "Lock, protected", "硬規則", "程式決定，不問模型", ["個資不外送", "排除指定來源國"], 1),
   ("green", "Performance", "能力與成本", "夠強又最便宜", ["簡單／中等／困難", "同分自家優先"], 0),
   ("cyan", "Cluster server", "備援", "連不上就換", ["只在出字前換", "冷卻跳過"], 0)]),
 ("green", 2, "模型", "各標位置與強度", "全被排除就報錯", [
   ("green", "Server (generic)", "本機", "自己的機器", ["強度 1 到 3"], 0),
   ("violet", "Server rack", "私有", "內網或自建", ["強度 1 到 3"], 0),
   ("orange", "Cloud", "外部", "看不到個資與本機內容", ["精簡提示", "不帶 MCP 工具"], 0)])],
"fleet": [
 ("violet", 1, "多節點艦隊", "一頁看完所有機器", "token 只在 hub", [
   ("blue", "Phone", "手機", "Telegram", ["下任務", "允許／拒絕"], 0),
   ("orange", "User -Enterprise", "管理者", "瀏覽器", ["狀態與用量", "就地審批"], 0),
   ("violet", "Management console", "Hub", "任一節點都能當", ["合併呈現", "代理審批與停止"], 0),
   ("green", "Server (generic)", "節點 A", "自己的模型與資料", ["xharness web"], 0),
   ("green", "Server (generic)", "節點 B", "自己的模型與資料", ["xharness web"], 0)])],
"report": [
 ("cyan", 1, "服務使用報告", "給決策者看，數字由程式算", "一行指令出 Word", [
   ("blue", "User group", "誰在用", "人數與頻率", ["每日趨勢", "每人用量"], 0),
   ("amber", "Performance", "花多少", "token 與成本", ["估算一定帶假設", "各模型分開算"], 0),
   ("green", "Server (generic)", "自建 vs 外購", "資料留在哪", ["各模型呼叫次數", "不外送的請求數"], 0)]),
 ("violet", 2, "設備與治理", "報告也列系統自己的問題", "永不顯示金鑰", [
   ("violet", "Device health", "設備用到沒", "算力利用", ["節點狀態", "GPU 使用率"], 0),
   ("orange", "Lock, protected", "串接安不安全", "機密防護檢查", ["機密放在哪", "誰讀得到"], 0),
   ("pink", "Health monitoring", "系統健不健康", "自己的問題也列", ["拒絕、備援次數", "預算煞車次數"], 0)])],
}
E = {
"quickstart": [
 ("blue", 1, "Three steps", "under a minute", "zero runtime dependencies", [
   ("blue", "Import generic", "Install", "Python 3.11 or newer", ["git clone", "pip install -e ."], 0),
   ("green", "Server (generic)", "Point at a model", "any OpenAI-compatible endpoint", ["XHARNESS_BASE_URL", "XHARNESS_MODEL"], 0),
   ("pink", "Management console", "Give a task", "terminal or web", ["xharness \"task\"", "xharness web"], 0)])],
"toolcall": [
 ("orange", 1, "One tool call, step by step", "stoppable at every step", "side effects ask first", [
   ("green", "Continous Cycle Circle", "Propose", "wants a tool", ["tool call", "args checked"], 0),
   ("grey", "Filter", "Filter", "hide the rest", ["plan mode", "role limits", "breaker"], 0),
   ("orange", "Policy", "Approve", "side effects", ["auto", "ask", "deny"], 1),
   ("orange", "Firewall", "Sandbox", "confines bash", ["Seatbelt", "bwrap", "no network"], 0),
   ("pink", "Tool", "Run", "capped, timed", ["output cap", "restart"], 0),
   ("violet", "Database generic", "Record", "append-only", ["session JSONL", "audit"], 0)])],
"agents": [
 ("green", 1, "Division of work", "hand tasks to other agents", "every action goes through approval", [
   ("green", "Continous Cycle Circle", "Main agent", "the one you talk to", ["gives tasks", "reads messages"], 0),
   ("blue", "Workstation client", "Subagent", "fresh chat", ["subagent", "subagent_batch"], 0),
   ("cyan", "Queue, general", "Background", "read-only, max 4", ["no waiting", "answer as message"], 0),
   ("amber", "Task list or Backlog", "ralph", "work in rounds", ["fresh each round", "progress in files"], 0)]),
 ("violet", 2, "Conversation", "agents talk to each other", "every figure checked against a source", [
   ("violet", "Message", "Discussion", "debate, review", ["verbatim record", "disagreement kept"], 0),
   ("pink", "Groups", "Team vs team", "confers first", ["private caucus", "public spokesperson"], 0),
   ("orange", "Internet", "Hall", "across machines", ["invite code", "per-team token"], 0),
   ("grey", "Phone", "Messages", "list / send / report", ["read next step", "each one logged"], 0)])],
"routing": [
 ("orange", 1, "Model routing", "three layers, strictest first", "off by default", [
   ("blue", "Message", "Request", "one model each", ["personal data?", "local files?"], 0),
   ("orange", "Lock, protected", "Hard rules", "code decides", ["no personal data out", "exclude origins"], 1),
   ("green", "Performance", "Ability, cost", "strong enough, cheapest", ["easy / medium / hard", "own hardware first"], 0),
   ("cyan", "Cluster server", "Fallback", "switch if one is down", ["only before output", "cooldown skip"], 0)]),
 ("green", 2, "Models", "each marked place and strength", "none left means an error", [
   ("green", "Server (generic)", "Local", "your own machine", ["strength 1 to 3"], 0),
   ("violet", "Server rack", "Private", "internal or self-built", ["strength 1 to 3"], 0),
   ("orange", "Cloud", "Public", "no personal data", ["minimal prompt", "no MCP tools"], 0)])],
"fleet": [
 ("violet", 1, "Multi-node fleet", "every machine on one page", "tokens stay on the hub", [
   ("blue", "Phone", "Phone", "Telegram", ["give tasks", "allow / deny"], 0),
   ("orange", "User -Enterprise", "Manager", "browser", ["status, usage", "approve in place"], 0),
   ("violet", "Management console", "Hub", "any node", ["merged view", "proxy approvals"], 0),
   ("green", "Server (generic)", "Node A", "own models", ["xharness web"], 0),
   ("green", "Server (generic)", "Node B", "own models", ["xharness web"], 0)])],
"report": [
 ("cyan", 1, "Service usage report", "for decision makers, numbers by code", "one command, one Word file", [
   ("blue", "User group", "Who uses it", "people, frequency", ["daily trend", "usage per person"], 0),
   ("amber", "Performance", "What it costs", "tokens, cost", ["estimates carry assumptions", "split by model"], 0),
   ("green", "Server (generic)", "Own vs bought", "where data stays", ["calls per model", "requests kept in-house"], 0)]),
 ("violet", 2, "Hardware, safety", "the report lists its own problems", "never shows a key", [
   ("violet", "Device health", "Hardware used", "compute utilisation", ["node status", "GPU utilisation"], 0),
   ("orange", "Lock, protected", "Safe to connect", "secret protection check", ["where secrets live", "who can read them"], 0),
   ("pink", "Health monitoring", "System health", "its own problems too", ["refusals, fallbacks", "budget brake hits"], 0)])],
}
SPEC = Z if LANG == "zh" else E

CHIP_H, CHIP_STEP, TOP = 40, 50, 100
NOARROW = {"report"}


def build(name, bands):
    # 先算每條 band 的高度
    heights = []
    for hue, num, title, sub, badge, cards in bands:
        n = max(len(c[4]) for c in cards)
        card_h = TOP + n * CHIP_STEP + 14
        heights.append((card_h, 86 + card_h + 22))
    total = 28 + sum(h for _, h in heights) + 24 * (len(bands) - 1) + 28
    d = Diagram(width=W, height=total)
    y = 28
    for (hue, num, title, sub, badge, cards), (card_h, band_h) in zip(bands, heights):
        h = HUE[hue]
        d.band(28, y, W - 56, band_h, h)
        d.section(num, title, sub, h, y=y + 38, badge=badge, x=70, rule_to=W - 52)
        n = len(cards)
        gap = 44 if n > 1 else 0
        cw = (XW - gap * (n - 1)) // n
        cy = y + 86
        for i, (ch, icon, ctitle, csub, chips, solid) in enumerate(cards):
            x = X0 + i * (cw + gap)
            c = HUE[ch]
            d.card(x, cy, cw, card_h, c, ctitle, csub)
            if cw >= 260:
                d.parts.append(ms_badge(icon, x + cw - 38, cy + 44, 20, c.main))
            for j, label in enumerate(chips):
                d.chip(x + 16, cy + TOP + j * CHIP_STEP, cw - 32, CHIP_H, c, label, solid=(j < solid), center=True)
            if i < n - 1 and name not in NOARROW:
                my = cy + 44
                d.arrow(x + cw + 4, my, x + cw + gap - 4, my, c)
        y += band_h + 24
    suffix = "" if LANG == "zh" else "_en"
    d.save(os.path.join(OUT, f"xharness_{name}{suffix}.svg"), zoom=2)


for name, bands in SPEC.items():
    build(name, bands)
