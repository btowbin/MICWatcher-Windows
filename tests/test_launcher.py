import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import start_watcher
import install_windows_launcher
import micwatcher_gui


class LauncherTests(unittest.TestCase):
    def test_powershell_shortcut_values_are_escaped(self):
        self.assertEqual("'C:\\Lab''s MICWatcher'", install_windows_launcher.powershell_literal("C:\\Lab's MICWatcher"))

    def test_all_users_installer_rejects_user_private_python(self):
        with (
            patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\operator"}),
            patch.object(
                install_windows_launcher.shutil,
                "which",
                side_effect=[
                    "C:\\Users\\operator\\AppData\\Local\\Python\\pyw.exe",
                    "C:\\Users\\operator\\AppData\\Local\\Python\\pythonw.exe",
                ],
            ),
            patch.object(
                install_windows_launcher.sys,
                "executable",
                "C:\\Users\\operator\\AppData\\Local\\Python\\python.exe",
            ),
        ):
            with self.assertRaises(RuntimeError):
                install_windows_launcher.find_gui_python()

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
                patch.object(
                    install_windows_launcher,
                    "find_gui_python",
                    return_value=(Path("C:/Python/pythonw.exe"), ()),
                ),
                patch.object(install_windows_launcher.subprocess, "run"),
                patch.object(install_windows_launcher, "create_shortcut") as create_shortcut,
            ):
                launchers = install_windows_launcher.install_all_users()

            self.assertEqual('{"preserved": true}\n', existing_config.read_text(encoding="utf-8"))
            self.assertTrue((program / "start_watcher.py").is_file())
            self.assertEqual(
                [desktop / "MICWatcher.lnk", start_menu / "MICWatcher.lnk"], launchers
            )
            self.assertTrue((program / "micwatcher_gui.py").is_file())
            self.assertEqual(2, create_shortcut.call_count)
            for shortcut_call in create_shortcut.call_args_list:
                self.assertIn(str(program / "micwatcher_gui.py"), shortcut_call.args[2])

    def test_gui_settings_are_validated_and_preserve_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "images"
            destination = root / "network"
            source.mkdir()
            destination.mkdir()
            settings = micwatcher_gui.validate_operator_settings(
                "operator@example.org",
                str(source),
                "60",
                True,
                str(destination),
                "30",
                False,
                "",
                "Experiment 42 / SQUID 2",
            )
            existing = {
                "watch_folder": "old",
                "email": {
                    "username": "watcher@gmail.com",
                    "password": "secret",
                    "to_addresses": ["old@example.org"],
                },
                "transfer": {"max_files_per_check": None},
            }
            updated = micwatcher_gui.apply_operator_settings(existing, settings)
            self.assertEqual("Experiment 42 / SQUID 2", updated["microscope_name"])
            self.assertEqual("secret", updated["email"]["password"])
            self.assertEqual(["operator@example.org"], updated["email"]["to_addresses"])
            self.assertEqual(3600, updated["check_interval_seconds"])
            self.assertEqual(1800, updated["transfer"]["check_interval_seconds"])
            self.assertEqual(str(destination.resolve()), updated["transfer"]["destination_folder"])

    def test_disabled_transfer_does_not_require_transfer_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            settings = micwatcher_gui.validate_operator_settings(
                "operator@example.org", str(source), "60", False, "", "not a number"
            )
            self.assertFalse(settings.transfer_enabled)
            self.assertIsNone(settings.destination_folder)

    def test_sms_number_is_normalized_and_credentials_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = micwatcher_gui.validate_operator_settings(
                "operator@example.org",
                temporary,
                "60",
                False,
                "",
                "30",
                True,
                "+41 79 123 45 67",
            )
            existing = {
                "email": {},
                "transfer": {},
                "sms": {
                    "account_sid": "AC123",
                    "auth_token": "secret",
                    "from_number": "+15017122661",
                },
            }
            updated = micwatcher_gui.apply_operator_settings(existing, settings)
            self.assertTrue(updated["sms"]["enabled"])
            self.assertEqual("+41791234567", updated["sms"]["to_number"])
            self.assertEqual("secret", updated["sms"]["auth_token"])

    def test_enabled_sms_requires_international_phone_number(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "international format"):
                micwatcher_gui.validate_operator_settings(
                    "operator@example.org",
                    temporary,
                    "60",
                    False,
                    "",
                    "30",
                    True,
                    "079 123 45 67",
                )

    def test_experiment_name_is_required_and_must_be_one_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "experiment or microscope name"):
                micwatcher_gui.validate_operator_settings(
                    "operator@example.org",
                    temporary,
                    "60",
                    False,
                    "",
                    "30",
                    False,
                    "",
                    "   ",
                )
            with self.assertRaisesRegex(ValueError, "one line"):
                micwatcher_gui.validate_operator_settings(
                    "operator@example.org",
                    temporary,
                    "60",
                    False,
                    "",
                    "30",
                    False,
                    "",
                    "Scope A\nInjected subject",
                )

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
