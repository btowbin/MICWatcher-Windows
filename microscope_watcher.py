"""Monitor an acquisition folder and email when image production stops."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import smtplib
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Iterable


LOG = logging.getLogger("microscope_watcher")
DEFAULT_CONFIG = "watcher_config.json"
DEFAULT_STATE = "watcher_state.json"
COPY_BUFFER_BYTES = 1024 * 1024


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    security: str
    username: str
    password_env: str
    password: str
    from_address: str
    to_addresses: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class Config:
    microscope_name: str
    watch_folder: Path
    recursive: bool
    check_interval_seconds: float
    daily_report_interval_hours: float
    state_file: Path
    log_file: Path
    email: EmailConfig
    transfer: "TransferConfig"


@dataclass(frozen=True)
class TransferConfig:
    enabled: bool
    destination_folder: Path | None
    check_interval_seconds: float
    stable_for_seconds: float
    max_files_per_check: int | None
    max_untransferred_files: int


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class FolderSnapshot:
    file_count: int
    newest_mtime_ns: int
    newest_file: str | None
    transfer_files: tuple[FileRecord, ...] = ()


class FileStillChanging(Exception):
    """Raised when an acquisition file changes while it is being copied."""


class TransferBacklogExceeded(RuntimeError):
    """Raised after the transfer-backlog safety stop has been reported."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(f"transfer backlog {count:,} exceeds safety limit {limit:,}")


def _require(mapping: dict[str, Any], key: str, location: str) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing required setting: {location}.{key}")
    return mapping[key]


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    email = _require(raw, "email", "config")
    base = path.resolve().parent

    def config_path(value: str) -> Path:
        candidate = Path(os.path.expandvars(os.path.expanduser(value)))
        return candidate if candidate.is_absolute() else base / candidate

    recipients = _require(email, "to_addresses", "email")
    if isinstance(recipients, str):
        recipients = [recipients]
    if not recipients:
        raise ValueError("email.to_addresses must contain at least one address")

    transfer = raw.get("transfer", {})
    transfer_enabled = bool(transfer.get("enabled", False))
    destination_value = str(transfer.get("destination_folder", "")).strip()
    batch_limit_value = transfer.get("max_files_per_check")
    batch_limit = (
        None if batch_limit_value in {None, 0, ""} else int(batch_limit_value)
    )
    result = Config(
        microscope_name=str(raw.get("microscope_name", socket.gethostname())),
        watch_folder=config_path(str(_require(raw, "watch_folder", "config"))),
        recursive=bool(raw.get("recursive", True)),
        check_interval_seconds=float(raw.get("check_interval_seconds", 300)),
        daily_report_interval_hours=float(raw.get("daily_report_interval_hours", 24)),
        state_file=config_path(str(raw.get("state_file", DEFAULT_STATE))),
        log_file=config_path(str(raw.get("log_file", "microscope_watcher.log"))),
        email=EmailConfig(
            smtp_host=str(_require(email, "smtp_host", "email")),
            smtp_port=int(email.get("smtp_port", 587)),
            security=str(email.get("security", "starttls")).lower(),
            username=str(email.get("username", "")),
            password_env=str(email.get("password_env", "MICROSCOPE_WATCHER_SMTP_PASSWORD")),
            password=str(email.get("password", "")),
            from_address=str(_require(email, "from_address", "email")),
            to_addresses=tuple(str(item) for item in recipients),
            timeout_seconds=float(email.get("timeout_seconds", 30)),
        ),
        transfer=TransferConfig(
            enabled=transfer_enabled,
            destination_folder=config_path(destination_value) if destination_value else None,
            check_interval_seconds=float(
                transfer.get("check_interval_seconds", raw.get("check_interval_seconds", 300))
            ),
            stable_for_seconds=float(transfer.get("stable_for_seconds", 60)),
            max_files_per_check=batch_limit,
            max_untransferred_files=int(transfer.get("max_untransferred_files", 10_000)),
        ),
    )
    if result.check_interval_seconds <= 0:
        raise ValueError("check_interval_seconds must be greater than zero")
    if result.daily_report_interval_hours <= 0:
        raise ValueError("daily_report_interval_hours must be greater than zero")
    if result.email.security not in {"starttls", "ssl", "none"}:
        raise ValueError("email.security must be one of: starttls, ssl, none")
    if result.transfer.enabled and result.transfer.destination_folder is None:
        raise ValueError("transfer.destination_folder is required when transfer is enabled")
    if result.transfer.stable_for_seconds < 0:
        raise ValueError("transfer.stable_for_seconds cannot be negative")
    if result.transfer.check_interval_seconds <= 0:
        raise ValueError("transfer.check_interval_seconds must be greater than zero")
    if (
        result.transfer.max_files_per_check is not None
        and result.transfer.max_files_per_check <= 0
    ):
        raise ValueError("transfer.max_files_per_check must be greater than zero")
    if result.transfer.max_untransferred_files <= 0:
        raise ValueError("transfer.max_untransferred_files must be greater than zero")
    if result.transfer.enabled:
        watch = result.watch_folder.resolve()
        destination = result.transfer.destination_folder.resolve()  # type: ignore[union-attr]
        if destination == watch or watch in destination.parents:
            raise ValueError("transfer.destination_folder cannot be inside watch_folder")
    return result


def iter_files(folder: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        for root, _, names in os.walk(folder):
            for name in names:
                yield Path(root) / name
    else:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)


def scan_folder(
    folder: Path, recursive: bool, collect_after_mtime_ns: int | None = None
) -> FolderSnapshot:
    count = 0
    newest_ns = 0
    newest_file: str | None = None
    transfer_files: list[FileRecord] = []
    for path in iter_files(folder, recursive):
        try:
            stat = path.stat()
        except (FileNotFoundError, PermissionError, OSError) as exc:
            LOG.warning("Could not inspect %s: %s", path, exc)
            continue
        count += 1
        if collect_after_mtime_ns is not None and stat.st_mtime_ns > collect_after_mtime_ns:
            transfer_files.append(FileRecord(path, stat.st_size, stat.st_mtime_ns))
        if stat.st_mtime_ns > newest_ns:
            newest_ns = stat.st_mtime_ns
            newest_file = str(path)
    return FolderSnapshot(count, newest_ns, newest_file, tuple(transfer_files))


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Cannot read state file %s; starting fresh: %s", path, exc)
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class Mailer:
    def __init__(self, config: EmailConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

    def send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.from_address
        message["To"] = ", ".join(self.config.to_addresses)
        message.set_content(body)

        if self.dry_run:
            LOG.info("DRY RUN email\nSubject: %s\nTo: %s\n\n%s", subject, message["To"], body)
            return

        password = self.config.password or os.environ.get(self.config.password_env, "")
        if self.config.username and not password:
            raise RuntimeError(
                "No SMTP password is configured. Set email.password in the configuration "
                f"or define the {self.config.password_env!r} environment variable."
            )

        smtp_type = smtplib.SMTP_SSL if self.config.security == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": self.config.timeout_seconds}
        if self.config.security == "ssl":
            kwargs["context"] = ssl.create_default_context()
        with smtp_type(self.config.smtp_host, self.config.smtp_port, **kwargs) as smtp:
            if self.config.security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if self.config.username:
                smtp.login(self.config.username, password)
            smtp.send_message(message)


class Watcher:
    def __init__(
        self,
        config: Config,
        mailer: Mailer,
        clock: Callable[[], datetime] = now_local,
    ):
        self.config = config
        self.mailer = mailer
        self.clock = clock
        self.state = load_state(config.state_file)
        configured_watch = str(config.watch_folder.resolve())
        previous_watch = self.state.get("configured_watch_folder")
        if previous_watch is not None and previous_watch != configured_watch:
            LOG.info("Watched folder changed; resetting monitor and transfer state")
            self.state = {}
        self.state["configured_watch_folder"] = configured_watch
        configured_destination = (
            str(config.transfer.destination_folder.resolve())
            if config.transfer.destination_folder is not None
            else None
        )
        previous_destination = self.state.get("configured_transfer_destination")
        if previous_destination is not None and previous_destination != configured_destination:
            LOG.info("Transfer destination changed; resetting transfer status")
            for key in (
                "transfer_failed",
                "transfer_failed_at",
                "last_transfer_error",
                "transfer_pending",
                "transfer_pending_count",
            ):
                self.state.pop(key, None)
        self.state["configured_transfer_destination"] = configured_destination

    def _send(self, subject: str, body: str) -> bool:
        try:
            self.mailer.send(subject, body)
            LOG.info("Sent email: %s", subject)
            return True
        except Exception:
            LOG.exception("Failed to send email: %s", subject)
            return False

    def _is_activity(self, snapshot: FolderSnapshot) -> bool:
        old_count = self.state.get("file_count")
        old_mtime = self.state.get("newest_mtime_ns")
        if old_count is None or old_mtime is None:
            return False
        return snapshot.file_count > int(old_count) or snapshot.newest_mtime_ns > int(old_mtime)

    def _copy_then_delete(self, record: FileRecord) -> Path:
        destination_root = self.config.transfer.destination_folder
        if destination_root is None:
            raise RuntimeError("Transfer destination is not configured")
        relative = record.path.relative_to(self.config.watch_folder)
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            raise FileExistsError(f"Destination already exists; local source retained: {destination}")

        partial = destination.with_name(destination.name + ".micwatcher-part")
        try:
            # Copy only file contents. GVFS/FUSE network mounts commonly reject the
            # chmod/utime metadata operations performed by shutil.copy2.
            with record.path.open("rb") as source_stream, partial.open("wb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=COPY_BUFFER_BYTES)
            source_after = record.path.stat()
            partial_after = partial.stat()
            if (
                source_after.st_size != record.size
                or source_after.st_mtime_ns != record.mtime_ns
                or partial_after.st_size != record.size
            ):
                raise FileStillChanging(str(record.path))
            if destination.exists():
                raise FileExistsError(f"Destination appeared during copy: {destination}")
            os.replace(partial, destination)
            if destination.stat().st_size != record.size:
                raise OSError(f"Copied file size verification failed: {destination}")
            record.path.unlink()
            return destination
        except Exception:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not remove partial transfer file %s", partial)
            raise

    def _record_transfer_failure(
        self, observed_at: datetime, record: FileRecord, error: Exception
    ) -> None:
        first_failure = not bool(self.state.get("transfer_failed", False))
        self.state["transfer_failed"] = True
        self.state["last_transfer_error"] = str(error)
        if first_failure:
            self.state["transfer_failed_at"] = observed_at.isoformat()
            subject = f"[TRANSFER WARNING] {self.config.microscope_name}: file transfer failed"
            body = (
                f"Automatic file transfer for {self.config.microscope_name} failed.\n\n"
                f"Source file: {record.path}\n"
                f"Destination folder: {self.config.transfer.destination_folder}\n"
                f"Error: {error}\n"
                f"Failure observed: {format_timestamp(observed_at)}\n\n"
                "No repeated transfer warnings will be sent while this failure persists. "
                "A recovery email will be sent after a file transfers successfully."
            )
            self._send(subject, body)

    def _process_transfers(self, snapshot: FolderSnapshot, observed_at: datetime) -> int:
        if not self.config.transfer.enabled:
            return 0

        old_pending = self.state.get("transfer_pending", {})
        if not isinstance(old_pending, dict):
            old_pending = {}
        records: dict[str, FileRecord] = {}
        pending: dict[str, dict[str, Any]] = {}
        for record in snapshot.transfer_files:
            relative = str(record.path.relative_to(self.config.watch_folder))
            records[relative] = record
            previous = old_pending.get(relative, {})
            unchanged = (
                isinstance(previous, dict)
                and previous.get("size") == record.size
                and previous.get("mtime_ns") == record.mtime_ns
            )
            pending[relative] = {
                "size": record.size,
                "mtime_ns": record.mtime_ns,
                "unchanged_since": (
                    previous.get("unchanged_since") if unchanged else observed_at.isoformat()
                ),
            }

        pending_count = len(pending)
        self.state["transfer_pending"] = pending
        self.state["transfer_pending_count"] = pending_count
        safety_limit = self.config.transfer.max_untransferred_files
        if pending_count > safety_limit:
            self.state["transfer_safety_stopped"] = True
            self.state["transfer_safety_stopped_at"] = observed_at.isoformat()
            subject = (
                f"[SAFETY STOP] {self.config.microscope_name}: "
                "too many untransferred files"
            )
            body = (
                f"MICWatcher stopped for {self.config.microscope_name} because the transfer "
                f"backlog reached {pending_count:,} files, exceeding the safety limit of "
                f"{safety_limit:,}.\n\n"
                f"Local folder: {self.config.watch_folder}\n"
                f"Transfer destination: {self.config.transfer.destination_folder}\n"
                f"Stopped at: {format_timestamp(observed_at)}\n\n"
                "All untransferred files remain in the local folder. Resolve the backlog or "
                "network-drive problem before restarting MICWatcher."
            )
            self._send(subject, body)
            raise TransferBacklogExceeded(pending_count, safety_limit)
        self.state.pop("transfer_safety_stopped", None)
        self.state.pop("transfer_safety_stopped_at", None)

        transferred = 0
        limit = self.config.transfer.max_files_per_check
        for relative in sorted(records):
            if limit is not None and transferred >= limit:
                break
            entry = pending[relative]
            unchanged_since = parse_timestamp(entry.get("unchanged_since"))
            if unchanged_since is None:
                entry["unchanged_since"] = observed_at.isoformat()
                continue
            stable_seconds = (observed_at - unchanged_since).total_seconds()
            if stable_seconds < self.config.transfer.stable_for_seconds:
                continue

            record = records[relative]
            try:
                destination = self._copy_then_delete(record)
            except FileStillChanging:
                LOG.info("Deferred file that changed during transfer: %s", record.path)
                entry["unchanged_since"] = observed_at.isoformat()
                continue
            except Exception as exc:
                LOG.error("Transfer failed for %s: %s", record.path, exc)
                self._record_transfer_failure(observed_at, record, exc)
                break

            transferred += 1
            pending.pop(relative, None)
            self.state["last_transfer_success_at"] = observed_at.isoformat()
            self.state["last_transferred_file"] = str(destination)
            self.state["files_transferred_total"] = int(
                self.state.get("files_transferred_total", 0)
            ) + 1
            LOG.info("Transferred %s to %s and removed the local source", record.path, destination)

            if self.state.get("transfer_failed", False):
                subject = (
                    f"[TRANSFER RECOVERED] {self.config.microscope_name}: "
                    "file transfer working again"
                )
                body = (
                    f"Automatic file transfer for {self.config.microscope_name} is working again.\n\n"
                    f"Transferred to: {destination}\n"
                    f"Recovery observed: {format_timestamp(observed_at)}\n"
                )
                self._send(subject, body)
                self.state["transfer_failed"] = False
                self.state.pop("transfer_failed_at", None)
                self.state.pop("last_transfer_error", None)

        self.state["transfer_pending"] = pending
        self.state["transfer_pending_count"] = len(pending)
        return transferred

    def _transfer_report_text(self) -> str:
        if not self.config.transfer.enabled:
            return "File transfer: DISABLED"
        status = "FAILED" if self.state.get("transfer_failed", False) else "OK"
        lines = [
            f"File transfer: {status}",
            f"Transfer destination: {self.config.transfer.destination_folder}",
            f"Files waiting for stability/transfer: {int(self.state.get('transfer_pending_count', 0)):,}",
            f"Files transferred since setup: {int(self.state.get('files_transferred_total', 0)):,}",
            "Last successful transfer: "
            + format_timestamp(parse_timestamp(self.state.get("last_transfer_success_at"))),
        ]
        if self.state.get("transfer_failed", False):
            lines.append(
                "Transfer failure since: "
                + format_timestamp(parse_timestamp(self.state.get("transfer_failed_at")))
            )
            lines.append(f"Last transfer error: {self.state.get('last_transfer_error', 'unknown')}")
        return "\n".join(lines)

    def check_once(self, check_acquisition: bool = True, check_transfer: bool = True) -> None:
        observed_at = self.clock()
        try:
            snapshot = scan_folder(
                self.config.watch_folder,
                self.config.recursive,
                -1 if self.config.transfer.enabled and check_transfer else None,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            LOG.error("Cannot scan watched folder %s: %s", self.config.watch_folder, exc)
            snapshot = None

        initialized = "last_check_at" in self.state
        direct_activity = snapshot is not None and self._is_activity(snapshot)
        if not check_acquisition and direct_activity and snapshot is not None:
            self.state["activity_since_acquisition_check"] = True
            self.state["interim_activity_at"] = observed_at.isoformat()
            self.state["interim_activity_file"] = snapshot.newest_file
        interim_activity = bool(self.state.get("activity_since_acquisition_check", False))
        activity = direct_activity or (check_acquisition and interim_activity)
        last_activity = parse_timestamp(self.state.get("last_activity_at"))
        inactivity_due = (
            initialized
            and last_activity is not None
            and (observed_at - last_activity).total_seconds()
            >= self.config.check_interval_seconds
        )

        if check_acquisition and snapshot is not None:
            if not initialized or activity:
                activity_at = (
                    parse_timestamp(self.state.get("interim_activity_at"))
                    if interim_activity and not direct_activity
                    else observed_at
                )
                self.state["last_activity_at"] = (activity_at or observed_at).isoformat()
                self.state["last_activity_file"] = (
                    snapshot.newest_file
                    if direct_activity
                    else self.state.get("interim_activity_file")
                )

            was_stalled = bool(self.state.get("stalled", False))
            if initialized and activity and was_stalled:
                subject = f"[RECOVERED] {self.config.microscope_name}: images arriving again"
                body = (
                    f"Image acquisition for {self.config.microscope_name} has resumed.\n\n"
                    f"Folder: {self.config.watch_folder}\n"
                    f"Activity observed: {format_timestamp(observed_at)}\n"
                    f"Newest file: {snapshot.newest_file or 'unknown'}\n"
                    f"Current file count: {snapshot.file_count:,}\n"
                )
                self._send(subject, body)
                self.state["stalled"] = False
                self.state["inactivity_warning_count"] = 0
            elif inactivity_due and not activity:
                warning_count = int(self.state.get("inactivity_warning_count", 0))
                if warning_count < 2:
                    warning_number = warning_count + 1
                    subject = f"[WARNING {warning_number}/2] {self.config.microscope_name}: no new images"
                    body = (
                        f"No new or updated image files were detected for {self.config.microscope_name} "
                        f"during the latest check interval.\n\n"
                        f"Folder: {self.config.watch_folder}\n"
                        f"Last activity observed: {format_timestamp(last_activity)}\n"
                        f"Last active file: {self.state.get('last_activity_file') or 'unknown'}\n"
                        f"Checked at: {format_timestamp(observed_at)}\n"
                        f"Current file count: {snapshot.file_count:,}\n\n"
                        + (
                            "One final warning will be sent after the next interval if acquisition "
                            "does not resume."
                            if warning_number == 1
                            else "No further inactivity warnings will be sent until acquisition resumes."
                        )
                    )
                    self._send(subject, body)
                    self.state["inactivity_warning_count"] = warning_number
                self.state["stalled"] = True

            self.state["file_count"] = snapshot.file_count
            self.state["newest_mtime_ns"] = snapshot.newest_mtime_ns
            self.state["newest_file"] = snapshot.newest_file
            self.state.pop("activity_since_acquisition_check", None)
            self.state.pop("interim_activity_at", None)
            self.state.pop("interim_activity_file", None)
        elif check_acquisition and initialized:
            subject = f"[WARNING] {self.config.microscope_name}: acquisition folder unavailable"
            body = (
                f"The acquisition folder for {self.config.microscope_name} could not be read.\n\n"
                f"Folder: {self.config.watch_folder}\n"
                f"Last activity observed: {format_timestamp(parse_timestamp(self.state.get('last_activity_at')))}\n"
                f"Checked at: {format_timestamp(observed_at)}\n"
            )
            self._send(subject, body)
            self.state["stalled"] = True

        try:
            transferred = (
                self._process_transfers(snapshot, observed_at)
                if check_transfer and snapshot is not None
                else 0
            )
        except TransferBacklogExceeded:
            save_state(self.config.state_file, self.state)
            raise
        report_snapshot = snapshot
        if snapshot is not None and transferred:
            report_snapshot = FolderSnapshot(
                max(0, snapshot.file_count - transferred),
                snapshot.newest_mtime_ns,
                snapshot.newest_file,
                snapshot.transfer_files,
            )
        if report_snapshot is not None:
            self.state["local_file_count"] = report_snapshot.file_count
        if check_acquisition:
            self._maybe_send_daily_report(observed_at, report_snapshot)
            self.state["last_check_at"] = observed_at.isoformat()
        save_state(self.config.state_file, self.state)

    def _maybe_send_daily_report(
        self, observed_at: datetime, snapshot: FolderSnapshot | None
    ) -> None:
        last_report = parse_timestamp(self.state.get("last_report_at"))
        interval = timedelta(hours=self.config.daily_report_interval_hours)
        if last_report is not None and observed_at - last_report < interval:
            return

        try:
            usage = shutil.disk_usage(self.config.watch_folder)
            disk_text = (
                f"Free disk space: {human_bytes(usage.free)}\n"
                f"Total disk space: {human_bytes(usage.total)}\n"
                f"Disk used: {(usage.used / usage.total * 100) if usage.total else 0:.1f}%"
            )
        except OSError as exc:
            disk_text = f"Disk space: unavailable ({exc})"

        count_text = f"{snapshot.file_count:,}" if snapshot is not None else "unavailable"
        status = "STALLED" if self.state.get("stalled") else "OK"
        subject = f"[DAILY REPORT] {self.config.microscope_name}: {status}"
        body = (
            f"24-hour acquisition report for {self.config.microscope_name}\n\n"
            f"Status: {status}\n"
            f"Folder: {self.config.watch_folder}\n"
            f"Number of files: {count_text}\n"
            f"Last activity observed: {format_timestamp(parse_timestamp(self.state.get('last_activity_at')))}\n"
            f"{disk_text}\n"
            f"{self._transfer_report_text()}\n"
            f"Report generated: {format_timestamp(observed_at)}\n"
        )
        if self._send(subject, body):
            self.state["last_report_at"] = observed_at.isoformat()


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError as exc:
        print(f"Warning: cannot open log file {log_file}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG))
    parser.add_argument("--once", action="store_true", help="Perform one check and exit")
    parser.add_argument("--dry-run", action="store_true", help="Log emails instead of sending them")
    parser.add_argument("--test-email", action="store_true", help="Send one test email and exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(config.log_file, args.verbose)
    mailer = Mailer(config.email, dry_run=args.dry_run)

    if args.test_email:
        try:
            mailer.send(
                f"[TEST] {config.microscope_name}: watcher email test",
                f"The email configuration for {config.microscope_name} is working.\n"
                f"Watched folder: {config.watch_folder}\n",
            )
            LOG.info("Test email sent successfully")
            return 0
        except Exception:
            LOG.exception("Test email failed")
            return 1

    watcher = Watcher(config, mailer)
    if args.once:
        try:
            watcher.check_once()
            return 0
        except TransferBacklogExceeded as exc:
            LOG.error("MICWatcher safety stop: %s", exc)
            return 3

    LOG.info(
        "Watching %s every %.1f seconds for %s; transfer check every %.1f seconds",
        config.watch_folder,
        config.check_interval_seconds,
        config.microscope_name,
        config.transfer.check_interval_seconds,
    )
    next_acquisition_check = time.monotonic()
    next_transfer_check = time.monotonic() if config.transfer.enabled else float("inf")
    while True:
        current = time.monotonic()
        acquisition_due = current >= next_acquisition_check
        transfer_due = config.transfer.enabled and current >= next_transfer_check
        try:
            if acquisition_due or transfer_due:
                watcher.check_once(acquisition_due, transfer_due)
        except TransferBacklogExceeded as exc:
            LOG.error("MICWatcher safety stop: %s", exc)
            return 3
        except Exception:
            LOG.exception("Unexpected error during check; watcher will continue")
        current = time.monotonic()
        if acquisition_due:
            next_acquisition_check = current + config.check_interval_seconds
        if transfer_due:
            next_transfer_check = current + config.transfer.check_interval_seconds
        try:
            time.sleep(max(1.0, min(next_acquisition_check, next_transfer_check) - current))
        except KeyboardInterrupt:
            LOG.info("Watcher stopped")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
