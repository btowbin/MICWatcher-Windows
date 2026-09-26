"""Graphical Windows operator interface for MICWatcher."""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, BinaryIO

import start_watcher
from microscope_watcher import (
    Config,
    Mailer,
    SmsSender,
    TransferBacklogExceeded,
    Watcher,
    format_timestamp,
    load_config,
    parse_timestamp,
)


LOG = logging.getLogger("microscope_watcher")


@dataclass(frozen=True)
class OperatorSettings:
    recipient: str
    watch_folder: Path
    acquisition_interval_seconds: float
    transfer_enabled: bool
    destination_folder: Path | None
    transfer_interval_seconds: float
    sms_enabled: bool
    sms_to_number: str
    microscope_name: str


def normalized_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"')))).resolve()


def positive_minutes(value: str, label: str) -> float:
    try:
        minutes = float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number greater than zero.") from exc
    if minutes <= 0:
        raise ValueError(f"{label} must be a number greater than zero.")
    return minutes * 60


def normalize_phone_number(value: str) -> str:
    return re.sub(r"[\s().-]", "", value.strip())


def validate_operator_settings(
    recipient: str,
    watch_folder: str,
    acquisition_minutes: str,
    transfer_enabled: bool,
    destination_folder: str,
    transfer_minutes: str,
    sms_enabled: bool = False,
    sms_to_number: str = "",
    microscope_name: str = "Microscope",
) -> OperatorSettings:
    microscope_name = microscope_name.strip()
    if not microscope_name:
        raise ValueError("Enter an experiment or microscope name.")
    if "\r" in microscope_name or "\n" in microscope_name:
        raise ValueError("The experiment or microscope name must fit on one line.")
    if len(microscope_name) > 100:
        raise ValueError("The experiment or microscope name must be 100 characters or fewer.")

    recipient = recipient.strip()
    if "@" not in recipient or recipient.startswith("@") or recipient.endswith("@"):
        raise ValueError("Enter a valid alert email address.")

    source = normalized_path(watch_folder)
    if not source.is_dir():
        raise ValueError(f"The local acquisition folder does not exist:\n{source}")
    if not os.access(source, os.R_OK):
        raise ValueError(f"The local acquisition folder is not readable:\n{source}")

    acquisition_seconds = positive_minutes(
        acquisition_minutes, "Missing-file check interval"
    )
    destination: Path | None = None
    transfer_seconds = 1800.0
    if transfer_enabled:
        transfer_seconds = positive_minutes(transfer_minutes, "Transfer check interval")
        if not destination_folder.strip():
            raise ValueError("Choose or enter a transfer destination folder.")
        destination = normalized_path(destination_folder)
        if not destination.is_dir():
            raise ValueError(
                "The transfer destination is not connected or does not exist:\n"
                f"{destination}"
            )
        if not os.access(destination, os.W_OK):
            raise ValueError(f"The transfer destination is not writable:\n{destination}")
        if destination == source or source in destination.parents:
            raise ValueError("The transfer destination cannot be inside the acquisition folder.")

    phone_number = normalize_phone_number(sms_to_number)
    if sms_enabled and not re.fullmatch(r"\+[1-9]\d{7,14}", phone_number):
        raise ValueError(
            "Enter the SMS recipient in international format, for example +41791234567."
        )

    return OperatorSettings(
        recipient,
        source,
        acquisition_seconds,
        transfer_enabled,
        destination,
        transfer_seconds,
        sms_enabled,
        phone_number,
        microscope_name,
    )


def apply_operator_settings(
    existing: dict[str, Any], settings: OperatorSettings
) -> dict[str, Any]:
    config = copy.deepcopy(existing)
    email = config.setdefault("email", {})
    transfer = config.setdefault("transfer", {})
    sms = config.setdefault("sms", {})
    config["microscope_name"] = settings.microscope_name
    email["to_addresses"] = [settings.recipient]
    config["watch_folder"] = str(settings.watch_folder)
    config["check_interval_seconds"] = settings.acquisition_interval_seconds
    transfer["enabled"] = settings.transfer_enabled
    transfer["check_interval_seconds"] = settings.transfer_interval_seconds
    transfer["destination_folder"] = (
        str(settings.destination_folder) if settings.destination_folder is not None else ""
    )
    transfer.setdefault("stable_for_seconds", 60)
    if transfer.get("max_files_per_check") == 10:
        transfer["max_files_per_check"] = None
    else:
        transfer.setdefault("max_files_per_check", None)
    transfer.setdefault("max_untransferred_files", 10_000)
    sms["enabled"] = settings.sms_enabled
    sms["to_number"] = settings.sms_to_number if settings.sms_enabled else ""
    return config


def validate_candidate_config(config: dict[str, Any]) -> Config:
    email = config.get("email", {})
    username = str(email.get("username", ""))
    password = str(email.get("password", ""))
    password_env = str(email.get("password_env", ""))
    if username.startswith("your-") or "PASTE_GOOGLE_APP_PASSWORD" in password:
        raise ValueError(
            "The sender Gmail account has not been configured. Ask the administrator "
            "to edit C:\\ProgramData\\MICWatcher\\watcher_config.json."
        )
    if username and not password and not os.environ.get(password_env, ""):
        raise ValueError("The sender Gmail app password is not configured.")

    sms = config.get("sms", {})
    if sms.get("enabled", False):
        account_sid = str(sms.get("account_sid", ""))
        auth_token = str(sms.get("auth_token", ""))
        auth_token_env = str(sms.get("auth_token_env", ""))
        from_number = str(sms.get("from_number", ""))
        if not account_sid or account_sid.startswith("PASTE_"):
            raise ValueError(
                "Twilio has not been configured. Ask the administrator to set "
                "sms.account_sid in C:\\ProgramData\\MICWatcher\\watcher_config.json."
            )
        if not auth_token and not os.environ.get(auth_token_env, ""):
            raise ValueError("The Twilio Auth Token is not configured.")
        if not from_number.startswith("+"):
            raise ValueError("The Twilio sender phone number is not configured.")

    validation_path = start_watcher.CONFIG_PATH.with_name("watcher_config.validation.tmp")
    validation_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        return load_config(validation_path)
    finally:
        validation_path.unlink(missing_ok=True)


def archive_previous_state(config: Config, old_watch_folder: Path) -> None:
    if config.watch_folder.resolve() == old_watch_folder.resolve():
        return
    if config.state_file.exists():
        previous = config.state_file.with_suffix(config.state_file.suffix + ".previous")
        os.replace(config.state_file, previous)


def configure_file_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    resolved = log_file.resolve()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            if Path(handler.baseFilename).resolve() == resolved:
                return
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


class MonitorWorker(threading.Thread):
    def __init__(
        self,
        config: Config,
        events: "queue.Queue[tuple[str, Any]]",
        stop_event: threading.Event,
    ):
        super().__init__(name="MICWatcher monitor", daemon=True)
        self.config = config
        self.events = events
        self.stop_event = stop_event

    def emit_state(self, watcher: Watcher) -> None:
        self.events.put(("state", dict(watcher.state)))

    def run(self) -> None:
        exit_message = "Monitoring stopped."
        try:
            watcher = Watcher(
                self.config,
                Mailer(self.config.email),
                sms_sender=SmsSender(self.config.sms),
            )
            next_acquisition = time.monotonic()
            next_transfer = time.monotonic() if self.config.transfer.enabled else float("inf")
            self.events.put(("started", None))
            while not self.stop_event.is_set():
                current = time.monotonic()
                acquisition_due = current >= next_acquisition
                transfer_due = self.config.transfer.enabled and current >= next_transfer
                if acquisition_due or transfer_due:
                    try:
                        watcher.check_once(acquisition_due, transfer_due)
                        self.emit_state(watcher)
                    except TransferBacklogExceeded as exc:
                        LOG.error("MICWatcher safety stop: %s", exc)
                        exit_message = f"Safety stop: {exc}"
                        self.events.put(("fatal", exit_message))
                        break
                    except Exception as exc:
                        LOG.exception("Unexpected error during check; watcher will continue")
                        self.events.put(("warning", f"Check failed; MICWatcher will retry: {exc}"))

                current = time.monotonic()
                if acquisition_due:
                    next_acquisition = current + self.config.check_interval_seconds
                if transfer_due:
                    next_transfer = current + self.config.transfer.check_interval_seconds
                wait_seconds = max(0.25, min(next_acquisition, next_transfer) - current)
                self.stop_event.wait(wait_seconds)
        except Exception as exc:
            LOG.exception("MICWatcher could not start")
            exit_message = f"MICWatcher could not start: {exc}"
            self.events.put(("fatal", exit_message))
        finally:
            LOG.info("Watcher stopped")
            self.events.put(("stopped", exit_message))


class MICWatcherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MICWatcher")
        self.root.minsize(720, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.worker: MonitorWorker | None = None
        self.stop_event: threading.Event | None = None
        self.instance_lock: BinaryIO | None = None
        self.close_when_stopped = False
        self.form_widgets: list[tk.Widget] = []

        config = start_watcher.load_editable_config()
        email = config.get("email", {})
        recipients = email.get("to_addresses") or [""]
        recipient = recipients[0] if isinstance(recipients, list) else str(recipients)
        transfer = config.get("transfer", {})
        sms = config.get("sms", {})

        self.recipient = tk.StringVar(value=recipient)
        self.microscope_name = tk.StringVar(
            value=str(config.get("microscope_name", "Microscope"))
        )
        self.watch_folder = tk.StringVar(value=str(config.get("watch_folder", "")))
        self.acquisition_minutes = tk.StringVar(value="60")
        self.sms_enabled = tk.BooleanVar(value=bool(sms.get("enabled", False)))
        self.sms_to_number = tk.StringVar(value=str(sms.get("to_number", "")))
        self.transfer_enabled = tk.BooleanVar(value=bool(transfer.get("enabled", False)))
        self.destination_folder = tk.StringVar(value="")
        self.transfer_minutes = tk.StringVar(value="30")
        self.status = tk.StringVar(value="Stopped")
        self.status_detail = tk.StringVar(value="Configure the experiment, then click Start monitoring.")
        self.activity_detail = tk.StringVar(value="No checks performed in this session.")
        self.transfer_detail = tk.StringVar(value="Transfer status will appear here.")

        self.build_ui()
        self.update_sms_controls()
        self.update_transfer_controls()
        self.root.after(250, self.process_events)

    def add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse_command: Any,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        button = ttk.Button(parent, text="Browse...", command=browse_command)
        button.grid(row=row, column=2, padx=(8, 0), pady=6)
        self.form_widgets.extend([entry, button])

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text="MICWatcher", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text="Monitor microscope acquisition and optionally transfer stable files.",
        ).grid(row=1, column=0, sticky="w", pady=(0, 14))

        form = ttk.LabelFrame(outer, text="Experiment settings", padding=12)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Experiment / microscope name").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=6
        )
        microscope_entry = ttk.Entry(form, textvariable=self.microscope_name)
        microscope_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)
        self.form_widgets.append(microscope_entry)

        ttk.Label(form, text="Alert email address").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=6
        )
        recipient_entry = ttk.Entry(form, textvariable=self.recipient)
        recipient_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)
        self.form_widgets.append(recipient_entry)

        sms_check = ttk.Checkbutton(
            form,
            text="Send one SMS for each new warning (small per-message cost)",
            variable=self.sms_enabled,
            command=self.update_sms_controls,
        )
        sms_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 4))
        self.form_widgets.append(sms_check)

        ttk.Label(form, text="SMS recipient number").grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=6
        )
        self.sms_number_entry = ttk.Entry(form, textvariable=self.sms_to_number)
        self.sms_number_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)
        self.form_widgets.append(self.sms_number_entry)

        self.add_path_row(
            form, 4, "Local acquisition folder", self.watch_folder, self.browse_source
        )

        ttk.Label(form, text="Missing-file check").grid(
            row=5, column=0, sticky="w", padx=(0, 10), pady=6
        )
        acquisition_entry = ttk.Entry(form, textvariable=self.acquisition_minutes, width=12)
        acquisition_entry.grid(row=5, column=1, sticky="w", pady=6)
        ttk.Label(form, text="minutes").grid(row=5, column=1, sticky="w", padx=(90, 0))
        self.form_widgets.append(acquisition_entry)

        transfer_check = ttk.Checkbutton(
            form,
            text="Transfer stable files to a network folder",
            variable=self.transfer_enabled,
            command=self.update_transfer_controls,
        )
        transfer_check.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self.form_widgets.append(transfer_check)

        ttk.Label(form, text="Transfer destination").grid(
            row=7, column=0, sticky="w", padx=(0, 10), pady=6
        )
        self.destination_entry = ttk.Entry(form, textvariable=self.destination_folder)
        self.destination_entry.grid(row=7, column=1, sticky="ew", pady=6)
        self.destination_button = ttk.Button(
            form, text="Browse...", command=self.browse_destination
        )
        self.destination_button.grid(row=7, column=2, padx=(8, 0), pady=6)
        self.form_widgets.extend([self.destination_entry, self.destination_button])

        ttk.Label(form, text="Transfer check").grid(
            row=8, column=0, sticky="w", padx=(0, 10), pady=6
        )
        self.transfer_entry = ttk.Entry(form, textvariable=self.transfer_minutes, width=12)
        self.transfer_entry.grid(row=8, column=1, sticky="w", pady=6)
        self.transfer_unit = ttk.Label(form, text="minutes")
        self.transfer_unit.grid(row=8, column=1, sticky="w", padx=(90, 0))
        self.form_widgets.append(self.transfer_entry)

        status_frame = ttk.LabelFrame(outer, text="Status", padding=12)
        status_frame.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        outer.rowconfigure(3, weight=1)
        ttk.Label(status_frame, textvariable=self.status, font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(status_frame, textvariable=self.status_detail, wraplength=650).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Separator(status_frame).grid(row=2, column=0, sticky="ew", pady=10)
        ttk.Label(status_frame, textvariable=self.activity_detail, wraplength=650).grid(
            row=3, column=0, sticky="w"
        )
        ttk.Label(status_frame, textvariable=self.transfer_detail, wraplength=650).grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )
        status_frame.columnconfigure(0, weight=1)

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, sticky="e", pady=(14, 0))
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_monitoring)
        self.stop_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button.state(["disabled"])
        self.start_button = ttk.Button(
            buttons, text="Start monitoring", command=self.start_monitoring
        )
        self.start_button.grid(row=0, column=1)

    def browse(self, variable: tk.StringVar, title: str) -> None:
        initial = variable.get().strip()
        if not initial or not Path(initial).is_dir():
            initial = str(Path.home())
        selected = filedialog.askdirectory(parent=self.root, title=title, initialdir=initial)
        if selected:
            variable.set(selected)

    def browse_source(self) -> None:
        self.browse(self.watch_folder, "Choose the local acquisition folder")

    def browse_destination(self) -> None:
        self.browse(self.destination_folder, "Choose the network transfer destination")

    def update_transfer_controls(self) -> None:
        enabled = self.transfer_enabled.get() and self.worker is None
        state = "normal" if enabled else "disabled"
        self.destination_entry.configure(state=state)
        self.destination_button.configure(state=state)
        self.transfer_entry.configure(state=state)
        self.transfer_unit.configure(state=state)

    def update_sms_controls(self) -> None:
        enabled = self.sms_enabled.get() and self.worker is None
        self.sms_number_entry.configure(state="normal" if enabled else "disabled")

    def set_form_enabled(self, enabled: bool) -> None:
        for widget in self.form_widgets:
            try:
                widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass
        if enabled:
            self.update_sms_controls()
            self.update_transfer_controls()

    def start_monitoring(self) -> None:
        if self.worker is not None:
            return
        try:
            settings = validate_operator_settings(
                self.recipient.get(),
                self.watch_folder.get(),
                self.acquisition_minutes.get(),
                self.transfer_enabled.get(),
                self.destination_folder.get(),
                self.transfer_minutes.get(),
                self.sms_enabled.get(),
                self.sms_to_number.get(),
                self.microscope_name.get(),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot start MICWatcher", str(exc), parent=self.root)
            return

        instance_lock = start_watcher.acquire_instance_lock()
        if instance_lock is None:
            messagebox.showerror(
                "MICWatcher is already running",
                "Another Windows user or session is already running MICWatcher.",
                parent=self.root,
            )
            return

        try:
            existing = start_watcher.load_editable_config()
            old_watch = normalized_path(str(existing.get("watch_folder", Path.home())))
            candidate = apply_operator_settings(existing, settings)
            config = validate_candidate_config(candidate)
            archive_previous_state(config, old_watch)
            start_watcher.save_config(candidate)
            config = load_config(start_watcher.CONFIG_PATH)
            configure_file_logging(config.log_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            instance_lock.close()
            messagebox.showerror("Cannot start MICWatcher", str(exc), parent=self.root)
            return

        self.instance_lock = instance_lock
        self.stop_event = threading.Event()
        self.worker = MonitorWorker(config, self.events, self.stop_event)
        self.set_form_enabled(False)
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.status.set("Starting...")
        self.status_detail.set("Preparing the first acquisition and transfer check.")
        self.worker.start()

    def stop_monitoring(self) -> None:
        if self.worker is None or self.stop_event is None:
            return
        self.stop_event.set()
        self.stop_button.state(["disabled"])
        self.status.set("Stopping...")
        self.status_detail.set(
            "Waiting for the current check or file copy to finish safely."
        )

    def show_state(self, state: dict[str, Any]) -> None:
        last_check = format_timestamp(parse_timestamp(state.get("last_check_at")))
        last_activity = format_timestamp(parse_timestamp(state.get("last_activity_at")))
        count = int(state.get("local_file_count", state.get("file_count", 0)))
        acquisition = "WARNING: no new files" if state.get("stalled") else "OK"
        self.activity_detail.set(
            f"Acquisition: {acquisition}   |   Files: {count:,}\n"
            f"Last check: {last_check}   |   Last activity: {last_activity}"
        )
        if not self.transfer_enabled.get():
            self.transfer_detail.set("File transfer: disabled")
            return
        transfer_status = "FAILED" if state.get("transfer_failed") else "OK"
        pending = int(state.get("transfer_pending_count", 0))
        total = int(state.get("files_transferred_total", 0))
        text = (
            f"File transfer: {transfer_status}   |   Waiting: {pending:,}   |   "
            f"Transferred: {total:,}"
        )
        if state.get("last_transfer_error"):
            text += f"\nLast error: {state['last_transfer_error']}"
        self.transfer_detail.set(text)

    def finish_worker(self, message: str) -> None:
        self.worker = None
        self.stop_event = None
        if self.instance_lock is not None:
            self.instance_lock.close()
            self.instance_lock = None
        self.status.set("Stopped")
        self.status_detail.set(message)
        self.start_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        self.set_form_enabled(True)
        if self.close_when_stopped:
            self.root.destroy()

    def process_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "started":
                    self.status.set("Monitoring")
                    self.status_detail.set(
                        "MICWatcher is running. Keep this Windows session signed in."
                    )
                elif event == "state":
                    self.show_state(payload)
                elif event == "warning":
                    self.status_detail.set(str(payload))
                elif event == "fatal":
                    self.status.set("Safety stop")
                    self.status_detail.set(str(payload))
                    messagebox.showerror("MICWatcher safety stop", str(payload), parent=self.root)
                elif event == "stopped":
                    self.finish_worker(str(payload))
                    if self.close_when_stopped:
                        return
        except queue.Empty:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(250, self.process_events)
        except tk.TclError:
            pass

    def on_close(self) -> None:
        if self.worker is None:
            if self.instance_lock is not None:
                self.instance_lock.close()
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "Stop MICWatcher?",
            "MICWatcher is still monitoring. Stop it and close the window?",
            parent=self.root,
        ):
            return
        self.close_when_stopped = True
        self.stop_monitoring()


def main() -> int:
    try:
        root = tk.Tk()
        MICWatcherApp(root)
        root.mainloop()
        return 0
    except (OSError, json.JSONDecodeError, tk.TclError) as exc:
        try:
            messagebox.showerror("MICWatcher could not start", str(exc))
        except tk.TclError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
