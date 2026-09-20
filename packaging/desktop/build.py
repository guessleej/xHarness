#!/usr/bin/env python3
"""Build the desktop app with PyInstaller.

macOS: dist/xHarness.app and dist/xHarness-<version>.dmg (icon rendered from the
brand SVG with rsvg-convert + iconutil when they exist, otherwise no icon).
Windows: dist/xHarness/xHarness.exe. Linux: dist/xHarness/xHarness.

Run inside a venv that has  pip install -e ".[build]" .
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DIST = ROOT / "dist"
NAME = "xHarness"
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#bf181f"/>'
    '<path d="M19 19 L45 45 M45 19 L19 45" stroke="#ffffff" stroke-width="9" stroke-linecap="round"/>'
    "</svg>"
)


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from xharness import __version__

    return __version__


def mac_icon() -> Path | None:
    """SVG -> PNG set -> .icns; skipped quietly when the tools are missing."""
    if not (shutil.which("rsvg-convert") and shutil.which("iconutil")):
        return None
    iconset = HERE / "build" / "xHarness.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    svg = HERE / "build" / "icon.svg"
    svg.write_text(ICON_SVG)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(["rsvg-convert", "-w", str(px), "-h", str(px), "-o", str(iconset / name), str(svg)], check=True)
    icns = HERE / "build" / "xHarness.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    return icns


def main() -> int:
    ver = version()
    system = platform.system()
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", NAME, "--windowed",
        "--distpath", str(DIST), "--workpath", str(HERE / "build" / "work"), "--specpath", str(HERE / "build"),
        "--collect-submodules", "xharness",
    ]
    if system == "Darwin":
        icon = mac_icon()
        if icon:
            args += ["--icon", str(icon)]
        args += ["--osx-bundle-identifier", "tw.com.xcloudai.xharness"]
    args.append(str(HERE / "entry.py"))
    env = dict(os.environ)
    if system == "Darwin" and "DEVELOPER_DIR" not in env and Path("/Library/Developer/CommandLineTools/usr/bin/lipo").exists():
        # PyInstaller shells out to lipo; the Command Line Tools copy works without accepting the Xcode licence
        env["DEVELOPER_DIR"] = "/Library/Developer/CommandLineTools"
    subprocess.run(args, check=True, cwd=ROOT, env=env)

    if system == "Darwin":
        app = DIST / f"{NAME}.app"
        plist = app / "Contents" / "Info.plist"
        # version strings for Finder / Get Info
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", f"Set :CFBundleShortVersionString {ver}", str(plist)], check=False)
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", f"Set :CFBundleVersion {ver}", str(plist)], check=False)
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Add :NSHighResolutionCapable bool true", str(plist)], check=False)
        dmg = DIST / f"{NAME}-{ver}.dmg"
        dmg.unlink(missing_ok=True)
        staging = HERE / "build" / "dmg"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.copytree(app, staging / app.name, symlinks=True)
        os.symlink("/Applications", staging / "Applications")
        subprocess.run(["hdiutil", "create", "-quiet", "-volname", f"{NAME} {ver}", "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(dmg)], check=True)
        print(f"built {app} and {dmg}")
    else:
        print(f"built {DIST / NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
