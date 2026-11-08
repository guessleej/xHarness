"""PyInstaller entry point: the desktop window, nothing else.

The one exception is `--desktop-backend <module> <json>`: the desktop tools run
their platform backend in a process of its own, and a packaged app has no
separate Python to run it with, so it runs itself. `--sandbox-launch --cwd <dir>
-- <command>` is the same for the Windows command protection (xharness/winlow.py).
"""
import importlib
import sys

from xharness.cli import main
from xharness.tools.desktop_control import BACKENDS

if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "--sandbox-launch":
        from xharness.winlow import main as sandbox_launch
        sys.exit(sandbox_launch(sys.argv[2:]))
    if len(sys.argv) == 4 and sys.argv[1] == "--desktop-backend" and sys.argv[2] in BACKENDS.values():
        sys.argv = [sys.argv[2], sys.argv[3]]
        importlib.import_module(f"xharness.tools.{sys.argv[0]}").main()
        sys.exit(0)
    sys.exit(main(["desktop"]))
