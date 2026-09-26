"""Install MICWatcher once for every user of a Windows computer."""

from __future__ import annotations

import ctypes
import msvcrt
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO


SOURCE_ROOT = Path(__file__).resolve().parent
PROGRAM_ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MICWatcher"
DATA_ROOT = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "MICWatcher"
PUBLIC_DESKTOP = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
COMMON_START_MENU = (
    Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
)
PROGRAM_FILES = (
    "microscope_watcher.py",
    "start_watcher.py",
    "watcher_config.example.json",
    "README.md",
    "WINDOWS_LAUNCHER_GUIDE.md",
)


def batch_quote(value: Path | str) -> str:
    """Quote a value for a batch file and escape environment expansion."""
    return '"' + str(value).replace("%", "%%").replace('"', '""') + '"'


def launcher_contents(
    program_root: Path | None = None, data_root: Path | None = None
) -> str:
    program_root = PROGRAM_ROOT if program_root is None else program_root
    data_root = DATA_ROOT if data_root is None else data_root
    launcher_script = program_root / "start_watcher.py"
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "title MICWatcher\r\n"
        f"set \"MICWATCHER_DATA_DIR={str(data_root).replace('%', '%%')}\"\r\n"
        f"cd /d {batch_quote(program_root)}\r\n"
        "where py.exe >nul 2>nul\r\n"
        "if not errorlevel 1 (\r\n"
        f"  py -3 {batch_quote(launcher_script)}\r\n"
        ") else (\r\n"
        "  where python.exe >nul 2>nul\r\n"
        "  if errorlevel 1 (\r\n"
        "    echo Python 3 is not available for this Windows user.\r\n"
        "    echo Ask the administrator to install Python for all users.\r\n"
        "    pause\r\n"
        "    exit /b 2\r\n"
        "  )\r\n"
        f"  python {batch_quote(launcher_script)}\r\n"
        ")\r\n"
        "set MICWATCHER_EXIT=%ERRORLEVEL%\r\n"
        "if not \"%MICWATCHER_EXIT%\"==\"0\" (\r\n"
        "  echo.\r\n"
        "  echo MICWatcher exited with code %MICWATCHER_EXIT%.\r\n"
        ")\r\n"
        "echo.\r\n"
        "pause\r\n"
        "exit /b %MICWATCHER_EXIT%\r\n"
    )


def is_administrator() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_elevated() -> bool:
    arguments = subprocess.list2cmdline([str(Path(__file__).resolve()), "--elevated"])
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, arguments, str(SOURCE_ROOT), 1
    )
    return result > 32


def acquire_install_lock(lock_path: Path) -> BinaryIO | None:
    """Return a temporary lock, or None when the installed watcher is active."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def install_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".installing")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def install_file_from_text(destination: Path, contents: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".installing")
    temporary.write_text(contents, encoding="utf-8", newline="")
    os.replace(temporary, destination)


def grant_users_modify_access(path: Path) -> None:
    """Allow the language-neutral built-in Users group to update runtime data."""
    subprocess.run(
        [
            "icacls",
            str(path),
            "/grant:r",
            "*S-1-5-32-545:(OI)(CI)M",
            "/T",
            "/C",
            "/Q",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def install_all_users() -> list[Path]:
    install_lock = acquire_install_lock(DATA_ROOT / ".micwatcher.lock")
    if install_lock is None:
        raise RuntimeError("MICWatcher is running. Stop it before installing or updating.")
    try:
        PROGRAM_ROOT.mkdir(parents=True, exist_ok=True)
        DATA_ROOT.mkdir(parents=True, exist_ok=True)

        for name in PROGRAM_FILES:
            source = SOURCE_ROOT / name
            if not source.is_file():
                raise FileNotFoundError(f"Required installation file is missing: {source}")
            install_file(source, PROGRAM_ROOT / name)

        shared_config = DATA_ROOT / "watcher_config.json"
        if not shared_config.exists():
            local_config = SOURCE_ROOT / "watcher_config.json"
            template = SOURCE_ROOT / "watcher_config.example.json"
            install_file(local_config if local_config.is_file() else template, shared_config)

        grant_users_modify_access(DATA_ROOT)

        contents = launcher_contents()
        launchers = [
            PUBLIC_DESKTOP / "MICWatcher.cmd",
            COMMON_START_MENU / "MICWatcher.cmd",
        ]
        for launcher in launchers:
            install_file_from_text(launcher, contents)
        return launchers
    finally:
        install_lock.close()


def main() -> int:
    if os.name != "nt":
        print("This installer is for Windows. Use the Ubuntu MICWatcher repository on Linux.")
        return 2

    if not is_administrator():
        print("Administrator approval is required for the one-time all-users installation.")
        if relaunch_elevated():
            print("Continue in the administrator window opened by Windows.")
            return 0
        print("The administrator request was cancelled or could not be opened.")
        return 2

    try:
        launchers = install_all_users()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}")
        if "--elevated" in sys.argv:
            input("Press Enter to close this window.")
        return 2

    print("\nMICWatcher was installed for all Windows users.")
    print(f"  Program: {PROGRAM_ROOT}")
    print(f"  Shared settings and logs: {DATA_ROOT}")
    print("  Launchers:")
    for launcher in launchers:
        print(f"    {launcher}")
    print("\nPython 3 must be installed for all users, and each user must have network-share access.")
    if "--elevated" in sys.argv:
        input("Press Enter to close this window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
