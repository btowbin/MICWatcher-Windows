"""Install MICWatcher once for every user of a Windows computer."""

from __future__ import annotations

import base64
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
    "micwatcher_gui.py",
    "watcher_config.example.json",
    "README.md",
    "WINDOWS_LAUNCHER_GUIDE.md",
)


def find_gui_python() -> tuple[Path, tuple[str, ...]]:
    """Find a windowless Python entry point available to all user accounts."""
    user_profile_value = os.environ.get("USERPROFILE", "")
    user_profile = Path(user_profile_value).resolve() if user_profile_value else None
    profiles_root = Path(os.environ.get("PUBLIC", r"C:\Users\Public")).resolve().parent

    def shared_path(value: str | Path) -> Path | None:
        candidate = Path(value).resolve()
        if candidate == profiles_root or profiles_root in candidate.parents:
            return None
        if user_profile is not None and (candidate == user_profile or user_profile in candidate.parents):
            return None
        return candidate

    # Prefer the interpreter beside the Python executable running this installer.
    # A shared pyw.exe launcher can select runtimes from per-user registry state,
    # causing a shortcut installed by an administrator to do nothing for domain
    # users. A direct Program Files pythonw.exe path is account-independent.
    sibling = Path(sys.executable).resolve().with_name("pythonw.exe")
    shared_sibling = shared_path(sibling)
    if sibling.is_file() and shared_sibling is not None:
        return shared_sibling, ()

    pythonw = shutil.which("pythonw.exe")
    if pythonw:
        shared_pythonw = shared_path(pythonw)
        if shared_pythonw is not None:
            return shared_pythonw, ()
    launcher = shutil.which("pyw.exe")
    if launcher:
        shared_launcher = shared_path(launcher)
        if shared_launcher is not None:
            return shared_launcher, ("-3",)
    raise RuntimeError(
        "A windowless Python 3 launcher (pyw.exe or pythonw.exe) was not found. "
        "Install Python 3 for all users, including Tcl/Tk support."
    )


def powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(
    destination: Path,
    target: Path,
    arguments: str,
    working_directory: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$shell = New-Object -ComObject WScript.Shell\n"
        f"$shortcut = $shell.CreateShortcut({powershell_literal(destination)})\n"
        f"$shortcut.TargetPath = {powershell_literal(target)}\n"
        f"$shortcut.Arguments = {powershell_literal(arguments)}\n"
        f"$shortcut.WorkingDirectory = {powershell_literal(working_directory)}\n"
        "$shortcut.Description = 'Configure and start MICWatcher'\n"
        f"$shortcut.IconLocation = {powershell_literal(str(target) + ',0')}\n"
        "$shortcut.Save()\n"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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

        gui_python, python_arguments = find_gui_python()
        subprocess.run(
            [str(gui_python), *python_arguments, "-c", "import tkinter"],
            check=True,
        )
        gui_script = PROGRAM_ROOT / "micwatcher_gui.py"
        shortcut_arguments = subprocess.list2cmdline([*python_arguments, str(gui_script)])
        launchers = [
            PUBLIC_DESKTOP / "MICWatcher.lnk",
            COMMON_START_MENU / "MICWatcher.lnk",
        ]
        for launcher in launchers:
            create_shortcut(launcher, gui_python, shortcut_arguments, PROGRAM_ROOT)
        for legacy in (
            PUBLIC_DESKTOP / "MICWatcher.cmd",
            COMMON_START_MENU / "MICWatcher.cmd",
        ):
            legacy.unlink(missing_ok=True)
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
