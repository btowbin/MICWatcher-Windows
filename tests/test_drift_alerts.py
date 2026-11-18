import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from microscope_watcher import (
    Config,
    DriftAlertConfig,
    EmailConfig,
    Mailer,
    SmsConfig,
    TelegramConfig,
    TransferConfig,
    Watcher,
    load_config,
)

LOG_NAME = "strong_AF_drift_alert_log.txt"
START = (
    "2026-11-18 13:48:13 | INFO | START | job | t=0 | monitoring AF drift: level 10 um "
    "for 3 time points, trend 8 um over 6 time points\n"
)


def alert(kind="DRIFT_TREND", group="2", t=5, stamp="2026-11-18 14:48:13"):
    return (
        f"{stamp} | ALERT | {kind} | group {group} | t={t} | focus is drifting "
        "+2.31 um per time point (+11.6 um over 6 time points)\n"
    )


def ok(kind="DRIFT_TREND", group="2", t=9, stamp="2026-11-18 18:48:13"):
    return f"{stamp} | OK | {kind} | group {group} | t={t} | condition is gone\n"


class RecordingMailer(Mailer):
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, subject, body):
        if self.fail:
            raise OSError("mail server down")
        self.messages.append((subject, body))


class DriftAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "images"
        self.folder.mkdir()
        self.log = self.folder / LOG_NAME
        self.time = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
        self.mailer = RecordingMailer()
        self.config = Config(
            microscope_name="Squid1",
            watch_folder=self.folder,
            recursive=True,
            check_interval_seconds=3600,
            daily_report_interval_hours=24,
            state_file=self.root / "state.json",
            log_file=self.root / "watcher.log",
            email=EmailConfig(
                "smtp.test", 587, "starttls", "", "PASSWORD", "", "a@test", ("b@test",), 5
            ),
            sms=SmsConfig(False, "", "TWILIO_TOKEN", "", "", "", 5),
            telegram=TelegramConfig(False, "TELEGRAM_TOKEN", "", "", 5),
            transfer=TransferConfig(False, None, 60, 60, None, 10_000),
            drift_alerts=DriftAlertConfig(enabled=True),
        )

    def tearDown(self):
        self.temp.cleanup()

    def watcher(self):
        return Watcher(self.config, self.mailer, lambda: self.time)

    def append(self, text):
        with self.log.open("a", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def advance(self, **kwargs):
        self.time += timedelta(**kwargs)

    # --- alerts -------------------------------------------------------------
    def test_disabled_does_nothing(self):
        self.config = replace(self.config, drift_alerts=DriftAlertConfig(enabled=False))
        watcher = self.watcher()
        self.append(START + alert())
        watcher.check_drift_alerts()
        self.advance(hours=3)
        watcher.check_drift_alerts()
        self.assertEqual(self.mailer.messages, [])

    def test_alert_is_emailed_once_and_reminders_are_ignored(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()          # no file yet
        self.append(START)
        watcher.check_drift_alerts()
        self.assertEqual(self.mailer.messages, [])
        self.append(alert(t=5))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)
        subject, body = self.mailer.messages[0]
        self.assertEqual(subject, "[FOCUS DRIFT] Squid1: group 2, DRIFT_TREND, t=5")
        self.assertIn("focus is drifting +2.31 um", body)
        self.assertIn("Job started: 2026-11-18 13:48:13", body)
        self.append(alert(t=11, stamp="2026-11-18 20:48:13"))     # reminder
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_different_group_or_kind_sends_a_new_email(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(t=5))
        watcher.check_drift_alerts()
        self.append(alert("DRIFT_LEVEL", "2", 7))
        watcher.check_drift_alerts()
        self.append(alert("DRIFT_TREND", "3", 8))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 3)

    def test_alert_after_ok_is_emailed_again(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(t=5))
        watcher.check_drift_alerts()
        self.append(ok())
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)       # no resolved mail
        self.append(alert(t=20))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 2)

    def test_alerts_in_one_poll_are_one_email(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(group="2") + alert("DRIFT_TILT", "6"))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)
        subject, body = self.mailer.messages[0]
        self.assertIn("2 new alerts (group 2, group 6)", subject)
        self.assertIn("DRIFT_TILT", body)
        self.assertIn("group 2 | DRIFT_TREND", body)     # open alerts list

    def test_new_start_line_in_same_file_reopens_alerts(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(t=5))
        watcher.check_drift_alerts()
        self.append(START.replace("13:48:13", "19:00:00") + alert(t=3))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 2)

    # --- reading the file -----------------------------------------------------
    def test_content_present_at_watcher_start_is_not_resent(self):
        self.append(START + alert(t=5))
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.assertEqual(self.mailer.messages, [])
        self.append(alert("DRIFT_LEVEL", "2", 7))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertIn("DRIFT_LEVEL", self.mailer.messages[0][0])

    def test_file_created_later_is_read_from_its_start(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.advance(minutes=1)
        self.append(START + alert(t=0))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_half_written_last_line_waits_for_its_newline(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        line = alert(t=5)
        self.append(START + line[:40])
        watcher.check_drift_alerts()
        self.assertEqual(self.mailer.messages, [])
        self.append(line[40:])
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_replaced_smaller_file_is_read_again(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(t=5) + alert("DRIFT_LEVEL", "2", 7))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)      # both alerts in one email
        self.log.write_text(START + alert("DRIFT_TILT", "4", 2), encoding="utf-8")
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 2)

    def test_unparsable_lines_and_blank_lines_are_ignored(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append("garbage line\n\n" + alert(t=5))
        with self.assertLogs("microscope_watcher", level="WARNING"):
            watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_failed_email_is_retried_without_losing_the_alert(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(t=5))
        self.mailer.fail = True
        with self.assertLogs("microscope_watcher", level="ERROR"):
            watcher.check_drift_alerts()
        self.mailer.fail = False
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_only_the_watched_folder_is_searched(self):
        sub = self.folder / "experiment"
        sub.mkdir()
        watcher = self.watcher()
        watcher.check_drift_alerts()
        (sub / LOG_NAME).write_text(START + alert(), encoding="utf-8")
        watcher.check_drift_alerts()
        self.advance(hours=2)
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertIn("no autofocus drift alert log found", self.mailer.messages[0][0])

    # --- missing file ---------------------------------------------------------
    def test_missing_file_notice_is_sent_once_after_the_grace_period(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.advance(minutes=59)
        watcher.check_drift_alerts()
        self.assertEqual(self.mailer.messages, [])
        self.advance(minutes=1)
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)
        subject, body = self.mailer.messages[0]
        self.assertIn("no autofocus drift alert log found", subject)
        self.assertIn(LOG_NAME, body)
        self.advance(hours=5)
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_custom_grace_period(self):
        self.config = replace(
            self.config,
            drift_alerts=DriftAlertConfig(enabled=True, missing_file_grace_seconds=600),
        )
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.advance(minutes=10)
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 1)

    def test_file_appearing_after_the_notice_is_still_watched(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.advance(hours=1)
        watcher.check_drift_alerts()
        self.append(START + alert(t=1))
        watcher.check_drift_alerts()
        self.assertEqual(len(self.mailer.messages), 2)

    def test_no_missing_notice_when_the_file_exists(self):
        self.append(START)
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.advance(hours=5)
        watcher.check_drift_alerts()
        self.assertEqual(self.mailer.messages, [])

    def test_state_is_saved_for_the_state_file(self):
        watcher = self.watcher()
        watcher.check_drift_alerts()
        self.append(START + alert(t=5))
        watcher.check_drift_alerts()
        saved = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        self.assertIn("group 2 | DRIFT_TREND", saved["drift_open_alerts"])


class DriftConfigTests(unittest.TestCase):
    def write(self, tmp, drift):
        raw = {
            "microscope_name": "Squid1",
            "watch_folder": str(tmp),
            "email": {
                "smtp_host": "smtp.test",
                "from_address": "a@test",
                "to_addresses": ["b@test"],
            },
        }
        if drift is not None:
            raw["drift_alerts"] = drift
        path = Path(tmp) / "cfg.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_defaults_are_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(self.write(tmp, None))
        self.assertFalse(config.drift_alerts.enabled)
        self.assertEqual(config.drift_alerts.log_filename, LOG_NAME)

    def test_values_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                self.write(
                    tmp,
                    {"enabled": True, "poll_interval_seconds": 30,
                     "missing_file_grace_seconds": 120},
                )
            )
        self.assertTrue(config.drift_alerts.enabled)
        self.assertEqual(config.drift_alerts.poll_interval_seconds, 30)
        self.assertEqual(config.drift_alerts.missing_file_grace_seconds, 120)

    def test_log_filename_must_not_be_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_config(self.write(tmp, {"enabled": True, "log_filename": "sub/x.txt"}))

    def test_poll_interval_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_config(self.write(tmp, {"enabled": True, "poll_interval_seconds": 0}))


if __name__ == "__main__":
    unittest.main()
