# xHarness 使用者操作流程圖（user-flow-diagram skill 範本改節點與箭頭；樣式與字級不動）
import sys; sys.path.insert(0, "/Users/jeff/.claude/skills/arch-diagram-style")
from arch_diagram import Diagram, BLUE, GREEN, VIOLET, ORANGE, CYAN
from msicons import ms_icon

W, H = 1400, 780
d = Diagram(width=W, height=H)
RED = "#bf181f"; INK = "#16202a"; GR = "#64748b"
BLUEC, GREENC, VIOC, ORC = "#2563eb", "#059669", "#7c3aed", "#ea580c"
T, N, S, A = 25, 20, 18, 20

def node(x, y, icon, color, title, name, size=80, lines=()):
    d.parts.append(ms_icon(icon, x - size / 2, y - size / 2, size, color))
    if title: d.text(x, y - size / 2 - 14, title, size=T, fill=RED, bold=True, anchor="middle")
    if name: d.text(x, y + size / 2 + 26, name, size=N, fill=INK, bold=True, anchor="middle")
    for i, ln in enumerate(lines): d.text(x, y + size / 2 + 50 + i * 21, ln, size=S, fill=GR, anchor="middle")

def arrow(path, color, label="", lx=0, ly=0, anchor="middle"):
    hue = {BLUEC: BLUE, GREENC: GREEN, VIOC: VIOLET, ORC: ORANGE}[color]
    d.arrow(0, 0, 0, 0, hue, path=path)
    for i, ln in enumerate(label.split("\n") if label else []):
        d.text(lx, ly + i * (A + 4), ln, size=A, fill=INK, bold=True, anchor=anchor)

def dashed_box(x, y, w, h, label):
    d.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="none" stroke="#94a3b8" stroke-width="2" stroke-dasharray="10 6"/>')
    d.text(x + w - 14, y + h - 14, label, size=T, fill=RED, bold=True, anchor="end")

# 左：資料進出 → 平台
node(300, 400, "Folder", GREENC, "Workspace", "Project directory", lines=("code, AGENTS.md", "xharness.toml config"))
arrow("M366 400 H 648", GREENC, "read / write files (sandboxed)", 507, 382)
node(300, 600, "Server (generic)", GREENC, "Model endpoint", "OpenAI-compatible", lines=("llama.cpp / vLLM / Ollama", "on-prem, nothing leaves"))
arrow("M366 600 H 660 V 460", GREENC, "streamed reply, tool calls", 513, 582)

# 中：平台
node(700, 400, "Application server", VIOC, "", "", size=100)
d.text(775, 328, "xHarness (local or on-prem node)", size=T, fill=RED, bold=True)
d.text(640, 470, "agent loop + plugins", size=N, fill=INK, bold=True, anchor="end")
d.text(640, 495, "tools · approval · sandbox", size=S, fill=GR, anchor="end")
d.text(640, 518, "brakes · memory · subagents", size=S, fill=GR, anchor="end")

# 上：操作者
dashed_box(430, 20, 520, 236, "Operator")
node(520, 130, "User", BLUEC, "User", "developer", size=72)
arrow("M562 130 H 664", BLUEC, "1. task", 613, 92)
node(710, 130, "Laptop", BLUEC, "Interface", "Web UI / CLI / REPL", size=72)
arrow("M680 200 V 346", BLUEC, "2. task, allow / deny", 662, 296, anchor="end")
arrow("M760 346 V 200", BLUEC, "3. streaming transcript, tool and approval cards", 778, 296, anchor="start")

# 右上：管理者（艦隊 hub）
node(1300, 130, "User -Enterprise", ORC, "Manager (hub)", "", size=72)
arrow("M754 380 H 1280 V 172", ORC, "4. fleet view: state, usage, pending approvals", 1010, 364)
arrow("M1320 172 V 420 H 758", ORC, "5. approve in place, stop an agent", 1010, 406)

# 右下：稽核與對外
node(1300, 600, "Dashboard", CYAN.main, "Audit and trends", "sessions · memory", size=72)
arrow("M754 450 V 600 H 1258", VIOC, "6. audit logs, usage trend, eval reports", 1010, 582)
node(1160, 690, "Connectors", CYAN.main, "MCP / nodes", "external tools, fleet nodes", size=64)
arrow("M700 450 V 690 H 1122", VIOC, "7. MCP tool calls, node proxying", 900, 672)

# 圖例
lx = 40
for hue, lab in [(GREEN, "data in / out"), (BLUE, "operator actions"), (ORANGE, "management and approval"), (VIOLET, "platform output")]:
    d.parts.append(f'<rect x="{lx}" y="{H-42}" width="20" height="20" rx="5" fill="{hue.main}"/>')
    d.text(lx + 30, H - 26, lab, size=A, fill="#334155")
    lx += 30 + int(len(lab) * A * 0.55) + 40
d.save(sys.argv[1] if len(sys.argv) > 1 else "out.svg", zoom=3)
