import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import start_watcher
import install_windows_launcher


class LauncherTests(unittest.TestCase):
    def test_windows_launcher_opens_terminal(self):
        contents = install_windows_launcher.launcher_contents()
        self.assertIn("@echo off", contents)
        self.assertIn("title MICWatcher", contents)
        self.assertIn("start_watcher.py", contents)
        self.assertIn("MICWATCHER_DATA_DIR", contents)
        self.assertIn("py -3", contents)
        self.assertIn("pause", contents)

    def test_all_users_install_uses_shared_locations_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            program = root / "Program Files" / "MICWatcher"
            data = root / "ProgramData" / "MICWatcher"
            desktop = root / "Public" / "Desktop"
            start_menu = root / "ProgramData" / "Start Menu" / "Programs"
            source.mkdir()
            data.mkdir(parents=True)

            repository = Path(__file__).resolve().parents[1]
            for name in install_windows_launcher.PROGRAM_FILES:
                shutil.copyfile(repository / name, source / name)
            existing_config = data / "watcher_config.json"
            existing_config.write_text('{"preserved": true}\n', encoding="utf-8")

            with (
                patch.object(install_windows_launcher, "SOURCE_ROOT", source),
                patch.object(install_windows_launcher, "PROGRAM_ROOT", program),
                patch.object(install_windows_launcher, "DATA_ROOT", data),
                patch.object(install_windows_launcher, "PUBLIC_DESKTOP", desktop),
                patch.object(install_windows_launcher, "COMMON_START_MENU", start_menu),
                patch.object(install_windows_launcher, "grant_users_modify_access"),
            ):
                launchers = install_windows_launcher.install_all_users()

            self.assertEqual('{"preserved": true}\n', existing_config.read_text(encoding="utf-8"))
            self.assertTrue((program / "start_watcher.py").is_file())
            self.assertEqual(
                [desktop / "MICWatcher.cmd", start_menu / "MICWatcher.cmd"], launchers
            )
            for launcher in launchers:
                contents = launcher.read_text(encoding="utf-8")
                self.assertIn(str(program), contents)
                self.assertIn(str(data), contents)

    def test_instance_lock_prevents_a_second_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / ".micwatcher.lock"
            with patch.object(start_watcher, "INSTANCE_LOCK_PATH", lock_path):
                first = start_watcher.acquire_instance_lock()
                self.assertIsNotNone(first)
                try:
                    self.assertIsNone(start_watcher.acquire_instance_lock())
                finally:
                    assert first is not None
                    first.close()

    def test_configure_updates_operator_fields_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "images"
            destination = root / "network"
            source.mkdir()
            destination.mkdir()
            config_path = root / "watcher_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "microscope_name": "Scope A",
                        "watch_folder": str(source),
                        "recursive": True,
                        "check_interval_seconds": 3600,
                        "daily_report_interval_hours": 24,
                        "state_file": "state.json",
                        "log_file": "watcher.log",
                        "transfer": {
                            "enabled": False,
                            "destination_folder": str(destination),
                            "check_interval_seconds": 300,
                            "stable_for_seconds": 60,
                            "max_files_per_check": 10,
                        },
                        "email": {
                            "smtp_host": "smtp.gmail.com",
                            "smtp_port": 587,
                            "security": "starttls",
                            "username": "watcher@gmail.com",
                            "password": "saved-app-password",
                            "password_env": "",
                            "from_address": "watcher@gmail.com",
                            "to_addresses": ["old@example.org"],
                            "timeout_seconds": 30,
                        },
                    }
                ),
                encoding="utf-8",
            )
            answers = iter(
                [
                    "new@example.org",
                    str(source),
                    "",
                    "y",
                    str(destination),
                    "",
                ]
            )
            with (
                patch.object(start_watcher, "CONFIG_PATH", config_path),
                patch.object(start_watcher, "EXAMPLE_CONFIG_PATH", root / "example.json"),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                self.assertTrue(start_watcher.configure())

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(["new@example.org"], updated["email"]["to_addresses"])
            self.assertEqual("saved-app-password", updated["email"]["password"])
            self.assertEqual(3600, updated["check_interval_seconds"])
            self.assertTrue(updated["transfer"]["enabled"])
            self.assertEqual(1800, updated["transfer"]["check_interval_seconds"])
            self.assertIsNone(updated["transfer"]["max_files_per_check"])
            self.assertEqual(10_000, updated["transfer"]["max_untransferred_files"])
            self.assertEqual(str(destination), updated["transfer"]["destination_folder"])


if __name__ == "__main__":
    unittest.main()
