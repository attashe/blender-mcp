"""Install the standalone add-on that belongs to this server."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path


def get_bundled_addon_path() -> Path:
    return Path(__file__).resolve().parent / "bundled" / "addon.py"


def discover_blender_addon_dirs() -> list[Path]:
    override = os.environ.get("BLENDERMCP_ADDONS_DIR") or os.environ.get("BLENDER_USER_ADDONS")
    if override:
        return [Path(override).expanduser()]

    home = Path.home()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        bases = [Path(appdata) / "Blender Foundation" / "Blender"] if appdata else []
    elif sys.platform == "darwin":
        bases = [home / "Library" / "Application Support" / "Blender"]
    else:
        bases = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "blender",
                 home / ".var" / "app" / "org.blender.Blender" / "config" / "blender"]

    versions = []
    for base in bases:
        if not base.is_dir():
            continue
        for folder in base.iterdir():
            parts = folder.name.split(".")
            if len(parts) == 2 and all(part.isascii() and part.isdigit() for part in parts) and folder.is_dir():
                versions.append(((int(parts[0]), int(parts[1])), folder / "scripts" / "addons"))
    return [folder for _, folder in sorted(versions, key=lambda entry: entry[0], reverse=True)]


def install_addon(addons_dir: Path) -> Path:
    source = get_bundled_addon_path()
    target = addons_dir.expanduser() / "blender_mcp_minimal.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.read_bytes() == source.read_bytes():
            return target
        shutil.copy2(target, target.with_suffix(".py.bak"))

    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
        staged_path = Path(staged.name)
    try:
        shutil.copyfile(source, staged_path)
        os.replace(staged_path, target)
    finally:
        staged_path.unlink(missing_ok=True)
    return target


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-for-blender", description="Local Blender MCP server (stdio)")
    parser.add_argument("--host", help="Blender socket host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Blender socket port (default 9876)")
    sub = parser.add_subparsers(dest="command")
    install = sub.add_parser("install-addon", help="Install the bundled Blender add-on")
    install.add_argument("--addons-dir", type=Path, help="Blender scripts/addons directory")
    sub.add_parser("addon-paths", help="List detected Blender scripts/addons directories")
    args = parser.parse_args(argv)
    if args.command is None:
        return -1

    try:
        if args.command == "addon-paths":
            for directory in discover_blender_addon_dirs():
                print(directory)
            return 0
        directory = args.addons_dir or next(iter(discover_blender_addon_dirs()), None)
        if directory is None:
            parser.error("No Blender configuration found. Open Blender once or pass --addons-dir.")
        print(f"Installed {install_addon(directory)}")
        print("Enable Blender MCP (Minimal), then open the BlenderMCP sidebar and start the server.")
        return 0
    except OSError as exc:
        print(f"Add-on installation or discovery failed: {exc}", file=sys.stderr)
        return 1
