# MICWatcher for Windows

MICWatcher monitors long-running microscope acquisitions on Windows. It checks whether new or updated files continue to appear, emails at most two warnings during an interruption, reports when acquisition resumes, and sends a daily disk-space and file-count report.

It can optionally move stable files to a network folder. Each file is copied and verified before its local copy is deleted. Transfer failures generate one warning, followed by silence until a successful transfer produces a recovery email.

This is the Windows edition of [MICWatcher](https://github.com/btowbin/MICWatcher). For Ubuntu microscope computers, use the original repository.

## Requirements

- Windows 10 or Windows 11
- Python 3.10 or newer
- Read access to the microscope acquisition folder
- Write access to the transfer destination, if transfer is enabled
- A Gmail account with 2-Step Verification and a Google app password

MICWatcher has no third-party Python dependencies. Administrator rights are not required when Python and Git are already available to the Windows user.

## Install

Open PowerShell and run:

```powershell
cd $HOME
git clone https://github.com/btowbin/MICWatcher-Windows.git
cd MICWatcher-Windows
Copy-Item watcher_config.example.json watcher_config.json
notepad watcher_config.json
```

In `watcher_config.json`, set these once for that microscope:

- `microscope_name`: a recognizable microscope name.
- `email.username`: the Gmail address used to send alerts.
- `email.password`: the 16-character Google app password, without spaces.
- `email.from_address`: the same Gmail address.

The launcher asks for the experiment-specific recipient, local folder, intervals, and optional transfer destination each time it starts. `watcher_config.json` is excluded from Git and is not overwritten by updates.

## Install the desktop launcher

Run once from the repository folder:

```powershell
python install_windows_launcher.py
```

This creates **MICWatcher.cmd** on the current user's Desktop and in the Start menu. Double-click it to configure and start monitoring. The launcher opens a terminal window; leave it open while the experiment is running and press Ctrl+C to stop.

The default missing-file interval is 60 minutes. The default transfer interval is 30 minutes. There is intentionally no default transfer destination.

See [WINDOWS_LAUNCHER_GUIDE.md](WINDOWS_LAUNCHER_GUIDE.md) for the complete operator workflow and instructions for copying local and network folder paths from File Explorer.

## Network transfer

In File Explorer, enter this UNC address in the address bar:

```text
\\izbkingston.unibe.ch\towbin.data
```

Navigate to or create the experiment's destination folder, click the address bar, and copy the complete path. Paste that path into the launcher. A mapped drive path such as `Z:\MicroscopeData\Experiment1` also works, but it must remain connected for the whole experiment.

Files present when MICWatcher starts are included. A file must remain unchanged for `stable_for_seconds` before it is transferred. The destination preserves the directory structure relative to the watched folder.

If a copy or local deletion fails, the source is retained. One transfer-failure email is sent, repeated failures remain silent, and a recovery email is sent after a later successful transfer. If more than 10,000 files remain untransferred, MICWatcher sends a safety-stop email and exits without deleting them.

## Test the configuration

Print email messages without sending them:

```powershell
python microscope_watcher.py --once --dry-run
```

Send a real test email:

```powershell
python microscope_watcher.py --test-email
```

## Operation

- At most two inactivity warnings are sent for one interruption.
- No further inactivity warnings are sent until files appear again; recovery produces one email.
- The daily report includes file count, disk space, and transfer status.
- `microscope_watcher.log` records operation and errors.
- `watcher_state.json` preserves alert and report state across restarts.
- Existing stable files are transferred when transfer is enabled.
- There is no default limit on the number of files transferred per check.

## Update

The desktop launcher does not need to be reinstalled after an ordinary update:

```powershell
cd $HOME\MICWatcher-Windows
git pull
```

Run the installer again only if the repository or Python installation has moved.

## Run tests

```powershell
python -m unittest discover -s tests -v
```
