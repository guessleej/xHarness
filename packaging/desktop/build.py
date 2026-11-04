#!/usr/bin/env python3
"""Build the desktop app with PyInstaller.

macOS: dist/xHarness.app and dist/xHarness-<version>.dmg (icon rendered from the
brand SVG with rsvg-convert + iconutil when they exist, otherwise no icon).
Windows: dist/xHarness/xHarness.exe, and dist/xHarness-<version>-setup.exe when
Inno Setup 6 is installed (iscc on PATH or in its default folder; GitHub's Windows
runners have it). The icon is drawn here, so Windows needs no image tools.
Linux: dist/xHarness/xHarness.

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


def _png(size: int, pixels: bytearray) -> bytes:
    import struct
    import zlib
    rows = b"".join(b"\x00" + bytes(pixels[y * size * 4:(y + 1) * size * 4]) for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def _draw(size: int) -> bytes:
    """The brand icon (ICON_SVG): a red rounded square with a white X, 4x supersampled."""
    over, red, white = 4, (0xBF, 0x18, 0x1F), (0xFF, 0xFF, 0xFF)
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            fill = cover = 0
            for sy in range(over):
                for sx in range(over):
                    u, v = (x + (sx + 0.5) / over) * 64 / size, (y + (sy + 0.5) / over) * 64 / size
                    cx, cy = min(max(u, 14), 50), min(max(v, 14), 50)
                    if (u - cx) ** 2 + (v - cy) ** 2 > 14 ** 2:
                        continue                                      # outside the rounded square
                    cover += 1
                    t = min(max((u - 19 + v - 19) / 52, 0), 1)       # distance to the two strokes (19,19)-(45,45), (45,19)-(19,45)
                    d1 = ((u - 19 - 26 * t) ** 2 + (v - 19 - 26 * t) ** 2) ** 0.5
                    t = min(max((45 - u + v - 19) / 52, 0), 1)
                    d2 = ((u - 45 + 26 * t) ** 2 + (v - 19 - 26 * t) ** 2) ** 0.5
                    fill += min(d1, d2) <= 4.5
            if cover:
                k = fill / cover
                i = (y * size + x) * 4
                pixels[i:i + 4] = bytes([round(red[c] + (white[c] - red[c]) * k) for c in range(3)] + [round(255 * cover / over ** 2)])
    return _png(size, pixels)


def win_icon() -> Path:
    """An .ico holding PNG images at 16-256 px (Windows Vista and later read PNG inside ICO)."""
    import struct
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [_draw(n) for n in sizes]
    offset = 6 + 16 * len(sizes)
    entries, data = b"", b""
    for n, image in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    ico = HERE / "build" / "xHarness.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    ico.write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + entries + data)
    return ico


def win_version_file(ver: str) -> Path:
    """File properties (Details tab): product, version and company."""
    numbers = tuple(int(p) for p in (ver.split("+")[0].split("-")[0].split(".") + ["0"] * 4)[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
  kids=[StringFileInfo([StringTable('040404b0', [
    StringStruct('CompanyName', 'xCloudinfo Corp. Limited'),
    StringStruct('FileDescription', 'xHarness'),
    StringStruct('FileVersion', '{ver}'),
    StringStruct('LegalCopyright', 'xCloudinfo Corp. Limited'),
    StringStruct('ProductName', 'xHarness'),
    StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [0x0404, 1200])])])
"""
    path = HERE / "build" / "version.txt"
    path.write_text(text, encoding="utf-8")
    return path


def iscc() -> str | None:
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "") + r"\Programs"):
        candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
        if base and candidate.exists():
            return str(candidate)
    return None


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
    elif system == "Windows":
        icon = win_icon()
        args += ["--icon", str(icon), "--version-file", str(win_version_file(ver)), "--add-data", f"{icon};."]
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
    elif system == "Windows":
        compiler = iscc()
        if compiler is None:
            print(f"built {DIST / NAME}; no installer: Inno Setup 6 (ISCC.exe) is not installed")
            return 0
        subprocess.run([compiler, "/Qp", f"/DAppVersion={ver}", f"/DSourceDir={DIST / NAME}", f"/DIconFile={win_icon()}",
                        f"/O{DIST}", str(HERE / "xharness.iss")], check=True)
        print(f"built {DIST / NAME} and {DIST / f'{NAME}-{ver}-setup.exe'}")
    else:
        print(f"built {DIST / NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
