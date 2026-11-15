#!/usr/bin/env python3
"""Build a standalone iTiMO Viewer executable with PyInstaller.

Usage:
    python build_exe.py                 # one-folder build (fast startup)
    python build_exe.py --onefile       # single .exe (slower startup)
    python build_exe.py --console       # keep a console for diagnostics

Output:
    poc/dist/iTiMOViewer/iTiMOViewer.exe          (default)
    poc/dist/iTiMOViewer.exe                      (--onefile)

The app reads config.json, captures/ and recordings/ next to the executable.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICON_DIR = HERE / "icons"
ICON_PNG = ICON_DIR / "app_icon.png"
ICON_ICO = ICON_DIR / "app_icon.ico"


def ensure_icon():
    if not ICON_PNG.exists():
        raise SystemExit("missing icon: %s" % ICON_PNG)
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit(
            "Pillow is required to create the .ico file.\n"
            "  python -m pip install pillow"
        )
    img = Image.open(ICON_PNG).convert("RGBA")
    img.save(
        ICON_ICO,
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    return ICON_ICO


def main():
    ap = argparse.ArgumentParser(description="Build Ear View with PyInstaller")
    ap.add_argument("--onefile", action="store_true",
                    help="build a single executable instead of a folder")
    ap.add_argument("--console", action="store_true",
                    help="keep a console window (default: windowed)")
    ap.add_argument("--name", default="Ear View")
    ap.add_argument("--no-clean", action="store_true")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed.\n"
            "  python -m pip install pyinstaller"
        )

    ico = ensure_icon()
    dist = HERE / "dist"
    work = HERE / "build"
    spec = work

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", args.name,
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(spec),
        "--paths", str(HERE),
        "--add-data", "%s%sicons" % (ICON_DIR, os.pathsep),
        "--hidden-import", "PySide6.QtSvg",
        "--icon", str(ico),
        "--noupx",
        "--log-level", "WARN",
    ]
    if not args.no_clean:
        cmd.append("--clean")
    if not args.console:
        cmd.append("--windowed")
    if args.onefile:
        cmd.append("--onefile")

    cmd.append(str(HERE / "viewer.py"))

    print("Building Ear View")
    print("  mode :", "onefile" if args.onefile else "onefolder")
    print("  icon :", ico)
    print("  out  :", dist)
    print()
    print(" ".join('"%s"' % c if " " in c else c for c in cmd))
    print()
    subprocess.run(cmd, cwd=str(HERE), check=True)

    if args.onefile:
        exe = dist / ("%s.exe" % args.name)
        target_dir = dist
    else:
        target_dir = dist / args.name
        exe = target_dir / ("%s.exe" % args.name)

    if not exe.exists():
        raise SystemExit("build finished but executable was not found: %s" % exe)

    # Seed the config next to the executable.  The app creates it on first
    # rotation/mirror change too, but this makes the location obvious.
    cfg = target_dir / "config.json"
    if not cfg.exists():
        src = HERE / "config.json"
        if src.exists():
            shutil.copy2(src, cfg)

    print()
    print("Built:", exe)
    print("Size : %.1f MB" % (exe.stat().st_size / (1024 * 1024)))
    print("Data :", "embedded" if args.onefile else target_dir)
    print("Run  :", exe)


if __name__ == "__main__":
    main()
