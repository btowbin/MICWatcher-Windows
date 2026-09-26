import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from microscope_watcher import (
    Config,
    EmailConfig,
    Mailer,
    TransferConfig,
    TransferBacklogExceeded,
    Watcher,
    human_bytes,
)


class RecordingMailer(Mailer):
    def __init__(self):
        self.messages = []

    def send(self, subject, body):
        self.messages.append((subject, body))


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "images"
        self.folder.mkdir()
        self.destination = self.root / "network"
        self.time = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        self.mailer = RecordingMailer()
        self.config = Config(
            microscope_name="Scope A",
            watch_folder=self.folder,
            recursive=True,
            check_interval_seconds=60,
            daily_report_interval_hours=24,
            state_file=self.root / "state.json",
            log_file=self.root / "watcher.log",
            email=EmailConfig(
                "smtp.test", 587, "starttls", "", "PASSWORD", "", "a@test", ("b@test",), 5
            ),
            transfer=TransferConfig(False, None, 60, 60, None, 10_000),
        )

    def tearDown(self):
        self.temp.cleanup()

    def clock(self):
        return self.time

    def watcher(self):
        return Watcher(self.config, self.mailer, self.clock)

    def enable_transfer(self, destination=None):
        self.config = replace(
            self.config,
            transfer=TransferConfig(
                True, destination or self.destination, 60, 60, None, 10_000
            ),
        )

    def test_first_check_establishes_baseline_without_warning(self):
        (self.folder / "first.tif").write_bytes(b"image")
        self.watcher().check_once()
        subjects = [subject for subject, _ in self.mailer.messages]
        self.assertFalse(any("WARNING" in subject for subject in subjects))
        self.assertTrue(any("DAILY REPORT" in subject for subject in subjects))

    def test_stall_repeated_warning_and_recovery(self):
        (self.folder / "first.tif").write_bytes(b"image")
        watcher = self.watcher()
        watcher.check_once()
        self.mailer.messages.clear()

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertIn("[WARNING 1/2]", self.mailer.messages[-1][0])

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertEqual(2, sum("WARNING" in subject for subject, _ in self.mailer.messages))

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertEqual(2, sum("WARNING" in subject for subject, _ in self.mailer.messages))

        self.time += timedelta(minutes=1)
        (self.folder / "second.tif").write_bytes(b"image")
        watcher.check_once()
        self.assertIn("[RECOVERED]", self.mailer.messages[-1][0])
        self.assertEqual(0, watcher.state["inactivity_warning_count"])

    def test_does_not_warn_before_a_full_interval(self):
        watcher = self.watcher()
        watcher.check_once()
        self.mailer.messages.clear()
        self.time += timedelta(seconds=30)
        watcher.check_once()
        self.assertFalse(any("WARNING" in subject for subject, _ in self.mailer.messages))

    def test_daily_report_is_not_repeated_before_24_hours(self):
        watcher = self.watcher()
        watcher.check_once()
        self.mailer.messages.clear()
        self.time += timedelta(hours=23)
        (self.folder / "new.tif").write_bytes(b"x")
        watcher.check_once()
        self.assertFalse(any("DAILY REPORT" in subject for subject, _ in self.mailer.messages))

        self.time += timedelta(hours=1)
        watcher.check_once()
        self.assertTrue(any("DAILY REPORT" in subject for subject, _ in self.mailer.messages))

    def test_new_stable_file_is_copied_then_deleted(self):
        self.enable_transfer()
        watcher = self.watcher()
        watcher.check_once()
        source = self.folder / "experiment" / "new.tif"
        source.parent.mkdir()
        source.write_bytes(b"image-data")

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertTrue(source.exists(), "file must remain while stability is established")

        self.time += timedelta(minutes=1)
        watcher.check_once()
        destination = self.destination / "experiment" / "new.tif"
        self.assertFalse(source.exists())
        self.assertEqual(b"image-data", destination.read_bytes())
        self.assertEqual(0, watcher.state["local_file_count"])

    def test_existing_file_at_startup_is_transferred_when_stable(self):
        self.enable_transfer()
        source = self.folder / "already-present.tif"
        source.write_bytes(b"existing-image")
        watcher = self.watcher()
        watcher.check_once()
        self.assertTrue(source.exists())

        self.time += timedelta(minutes=1)
        watcher.check_once(False, True)
        self.assertFalse(source.exists())
        self.assertEqual(
            b"existing-image", (self.destination / "already-present.tif").read_bytes()
        )

    def test_default_batch_has_no_file_count_limit(self):
        self.enable_transfer()
        for index in range(12):
            (self.folder / f"image-{index:02}.tif").write_bytes(str(index).encode())
        watcher = self.watcher()
        watcher.check_once()

        self.time += timedelta(minutes=1)
        watcher.check_once(False, True)
        self.assertFalse(any(self.folder.iterdir()))
        self.assertEqual(12, len(list(self.destination.iterdir())))

    def test_transfer_backlog_safety_stop_preserves_all_files(self):
        self.config = replace(
            self.config,
            transfer=TransferConfig(True, self.destination, 60, 60, None, 2),
        )
        for index in range(3):
            (self.folder / f"backlog-{index}.tif").write_bytes(b"image")
        watcher = self.watcher()

        with self.assertRaises(TransferBacklogExceeded):
            watcher.check_once()

        self.assertEqual(3, len(list(self.folder.iterdir())))
        self.assertFalse(self.destination.exists())
        self.assertEqual(
            1, sum("SAFETY STOP" in subject for subject, _ in self.mailer.messages)
        )
        state = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        self.assertTrue(state["transfer_safety_stopped"])
        self.assertEqual(3, state["transfer_pending_count"])

    def test_transfer_only_checks_preserve_acquisition_activity(self):
        self.enable_transfer()
        watcher = self.watcher()
        watcher.check_once()
        self.mailer.messages.clear()
        source = self.folder / "between-alarm-checks.tif"
        source.write_bytes(b"image")

        self.time += timedelta(minutes=1)
        watcher.check_once(False, True)
        self.time += timedelta(minutes=1)
        watcher.check_once(False, True)
        self.assertFalse(source.exists())

        self.time += timedelta(minutes=1)
        watcher.check_once(True, False)
        self.assertFalse(
            any("no new images" in subject for subject, _ in self.mailer.messages)
        )

    def test_transfer_failure_warns_once_then_reports_recovery(self):
        blocked_destination = self.root / "blocked"
        blocked_destination.write_text("not a directory", encoding="utf-8")
        self.enable_transfer(blocked_destination)
        watcher = self.watcher()
        watcher.check_once()
        self.mailer.messages.clear()
        source = self.folder / "new.tif"
        source.write_bytes(b"image-data")

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertTrue(source.exists())
        self.assertEqual(
            1, sum("TRANSFER WARNING" in subject for subject, _ in self.mailer.messages)
        )

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertEqual(
            1, sum("TRANSFER WARNING" in subject for subject, _ in self.mailer.messages)
        )

        blocked_destination.unlink()
        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertFalse(source.exists())
        self.assertEqual(
            1, sum("TRANSFER RECOVERED" in subject for subject, _ in self.mailer.messages)
        )

    def test_changing_file_is_not_transferred_until_stable(self):
        self.enable_transfer()
        watcher = self.watcher()
        watcher.check_once()
        source = self.folder / "growing.tif"
        source.write_bytes(b"first")

        self.time += timedelta(minutes=1)
        watcher.check_once()
        source.write_bytes(b"first-second")
        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertTrue(source.exists())
        self.assertFalse((self.destination / "growing.tif").exists())

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertFalse(source.exists())
        self.assertEqual(b"first-second", (self.destination / "growing.tif").read_bytes())

    def test_destination_collision_preserves_local_source(self):
        self.enable_transfer()
        self.destination.mkdir()
        destination = self.destination / "collision.tif"
        destination.write_bytes(b"existing")
        watcher = self.watcher()
        watcher.check_once()
        self.mailer.messages.clear()
        source = self.folder / "collision.tif"
        source.write_bytes(b"new-local-data")

        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.time += timedelta(minutes=1)
        watcher.check_once()
        self.assertEqual(b"new-local-data", source.read_bytes())
        self.assertEqual(b"existing", destination.read_bytes())
        self.assertEqual(
            1, sum("TRANSFER WARNING" in subject for subject, _ in self.mailer.messages)
        )

    def test_daily_report_contains_transfer_status(self):
        self.enable_transfer()
        self.watcher().check_once()
        reports = [body for subject, body in self.mailer.messages if "DAILY REPORT" in subject]
        self.assertEqual(1, len(reports))
        self.assertIn("File transfer: OK", reports[0])
        self.assertIn(str(self.destination), reports[0])

    def test_state_is_valid_json(self):
        self.watcher().check_once()
        state = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        self.assertIn("last_check_at", state)

    def test_human_bytes(self):
        self.assertEqual("1.0 GiB", human_bytes(1024**3))


if __name__ == "__main__":
    unittest.main()
