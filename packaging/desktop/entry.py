"""PyInstaller entry point: the desktop window, nothing else."""
import sys

from xharness.cli import main

if __name__ == "__main__":
    sys.exit(main(["desktop"]))
