"""Install per-user Windows desktop and Start menu launchers for MICWatcher."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LAUNCHER_SCRIPT = ROOT / "start_watcher.py"


def batch_quote(path: Path) -> str:
    """Quote a path for a batch file and escape environment expansion."""
    return '"' + str(path).replace("%", "%%").replace('"', '""') + '"'


def launcher_contents() -> str:
    python = Path(sys.executable).resolve()
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "title MICWatcher\r\n"
        f"cd /d {batch_quote(ROOT)}\r\n"
        f"{batch_quote(python)} {batch_quote(LAUNCHER_SCRIPT)}\r\n"
        "set MICWATCHER_EXIT=%ERRORLEVEL%\r\n"
        "if not \"%MICWATCHER_EXIT%\"==\"0\" (\r\n"
        "  echo.\r\n"
        "  echo MICWatcher exited with code %MICWATCHER_EXIT%.\r\n"
        ")\r\n"
        "echo.\r\n"
        "pause\r\n"
        "exit /b %MICWATCHER_EXIT%\r\n"
    )


def desktop_directory() -> Path:
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        candidate = Path(onedrive) / "Desktop"
        if candidate.is_dir():
            return candidate
    return Path.home() / "Desktop"


def start_menu_directory() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def install_launcher(destination: Path, contents: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".cmd.tmp")
    temporary.write_text(contents, encoding="utf-8", newline="")
    os.replace(temporary, destination)


def main() -> int:
    if os.name != "nt":
        print("This installer is for Windows. Use the Ubuntu MICWatcher repository on Linux.")
        return 2
    if not LAUNCHER_SCRIPT.is_file():
        print(f"Launcher script not found: {LAUNCHER_SCRIPT}")
        return 2

    contents = launcher_contents()
    installed: list[Path] = []

    desktop = desktop_directory()
    if desktop.is_dir():
        desktop_launcher = desktop / "MICWatcher.cmd"
        install_launcher(desktop_launcher, contents)
        installed.append(desktop_launcher)

    start_menu = start_menu_directory()
    if start_menu is not None and start_menu.is_dir():
        menu_launcher = start_menu / "MICWatcher.cmd"
        install_launcher(menu_launcher, contents)
        installed.append(menu_launcher)

    if not installed:
        fallback = ROOT / "MICWatcher.cmd"
        install_launcher(fallback, contents)
        installed.append(fallback)

    print("MICWatcher launcher installed:")
    for path in installed:
        print(f"  {path}")
    print("Double-click MICWatcher to configure and start monitoring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
