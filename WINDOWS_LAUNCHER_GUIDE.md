# MICWatcher Windows launcher guide

This guide is for microscope operators. The Gmail sender account and all-users launcher should already have been configured once by the lab administrator. Individual Windows users do not install MICWatcher again.

## Start an experiment

1. Start the microscopy experiment normally and confirm that it is saving images.
2. Double-click **MICWatcher** on the Desktop or open **MICWatcher** from the Start menu.
3. Enter a descriptive **Experiment / microscope name**, for example `SQUID 2 - lifespan experiment 2026-09-26`. This name appears in all email subjects, reports, and SMS warnings.
4. Enter the alert email address.
5. If an SMS warning is wanted, select **Send one SMS for each new warning** and enter the recipient's telephone number in international format, for example `+41791234567`. Sending an SMS has a small per-message cost.
6. If Telegram reporting is wanted, select **Send warnings to the configured Telegram channel**. The administrator must configure the bot and channel first.
7. Next to **Local acquisition folder**, click **Browse...**.
8. Navigate to the folder in which the experiment is saving files and click **Select Folder**.

You can alternatively open the local folder in File Explorer, click the address bar or press Ctrl+L, copy the path with Ctrl+C, and paste it into the MICWatcher path field with Ctrl+V.

The local path will normally look similar to:

```text
D:\MicroscopeData\Experiment_2026_09_25
```

Do not select an individual image file. Select the folder containing the experiment files.

## Choose a network destination

If files should be transferred to the lab server:

1. Open File Explorer and connect to the server first.
2. Click the address bar or press Ctrl+L.
3. Enter the server address below and press Enter:

   ```text
   \\izbkingston.unibe.ch\towbin.data
   ```

4. If Windows asks for credentials, sign in with the account that has access to the share.
5. Navigate to the desired destination and create a folder for the microscope or experiment if necessary.
6. In MICWatcher, select **Transfer stable files to a network folder**.
7. Click **Browse...** next to **Transfer destination** and choose the folder.

If the network folder does not appear in the folder browser, open it in File Explorer, copy its UNC path from the address bar, and paste that path directly into MICWatcher.

Use a separate destination for each microscope. The destination cannot be inside the local acquisition folder.

## Complete the experiment settings

The GUI contains:

1. **Experiment / microscope name** — identifies the experiment and microscope in every notification.
2. **Alert email address** — the user who should receive warnings and reports.
3. **Send one SMS for each new warning** — optional; SMS has a small per-message cost.
4. **SMS recipient number** — required only when SMS is enabled; use international format such as `+41791234567`.
5. **Send warnings to the configured Telegram channel** — optional; the administrator configures the bot and channel.
6. **Local acquisition folder** — type, paste, or browse to the experiment folder.
7. **Missing-file check** — 60 minutes by default.
8. **Transfer stable files to a network folder** — select this checkbox when transfer is required.
9. **Transfer destination** — type, paste, or browse to the network folder. There is no default destination.
10. **Transfer check** — 30 minutes by default.

Review the settings, then click **Start monitoring**.

Every click on **Start monitoring** starts a new monitoring run with fresh warning and report state. This also applies after Stop/Start or closing and reopening MICWatcher, even if the experiment name and folders are unchanged. Consequently, restarting during an existing interruption allows warning emails and SMS to be sent again after a full check interval.

## While MICWatcher is running

- Leave the MICWatcher window open.
- The program starts monitoring immediately.
- The status panel shows the latest check, activity, file count, pending transfers, and errors.
- Existing stable files in the local folder are also transferred when transfer is enabled.
- Click **Stop** to end monitoring. If a check or file copy is active, MICWatcher waits for it to finish safely.
- Closing the window while monitoring asks for confirmation before stopping.
- Do not start a second copy of MICWatcher for the same installation.
- Do not log out of the Windows account running MICWatcher; logging out closes the watcher. Locking the computer or switching users without logging out is acceptable.

## Email behavior

- When no file activity is detected for one complete interval, MICWatcher sends the first warning.
- If another complete interval passes without activity, it sends a second warning.
- It then remains silent until file activity resumes and sends one recovery email.
- If a file already exists at the destination, it remains in the local folder and the other files continue copying. MICWatcher sends only one destination-conflict email during that monitoring run, even if later checks find more conflicts. The current conflicting file names appear in the status panel and daily report.
- Another transfer failure generates one warning. Repeated failures remain silent until a successful transfer generates a recovery email.
- Every 24 hours, it sends a report containing file count, disk space, and transfer status.
- If the untransferred backlog exceeds 10,000 files, it sends a safety-stop warning and exits without deleting those files.

## SMS behavior

- SMS is optional and has a small Twilio per-message cost.
- The first missing-file warning for an interruption sends one SMS.
- The first transfer-failure warning sends one SMS.
- The second missing-file email, repeated failures, recoveries, daily reports, and routine status updates do not send SMS.
- SMS contains only a short warning and asks the recipient to read the email for details.
- While using a Twilio trial account, the recipient number must first be verified in the Twilio Console.

## Telegram behavior

- Telegram is optional and has no MICWatcher per-message charge.
- It follows the same first-warning-only policy as SMS: one message for a new missing-file incident and one for a new transfer-failure incident.
- Second warnings, recoveries, daily reports, safety stops, and routine status updates are not sent to Telegram.
- Telegram failures are written to the log and do not stop email, SMS, monitoring, or file transfer.

## Troubleshooting

**The launcher says that the local folder does not exist**

Open the exact folder in File Explorer and copy its path from the address bar again. Do not include quotation marks.

**The launcher says that the network destination does not exist or is not writable**

Open `\\izbkingston.unibe.ch\towbin.data` in File Explorer first, authenticate if requested, and verify that you can create a folder or file in the chosen destination. Then start MICWatcher again.

**A network path stops working during an experiment**

Reconnect it in File Explorer. MICWatcher retains local files when transfer fails and reports recovery after a later successful transfer.

**The application does not open**

Ask the administrator to confirm that Python 3 was installed for all users with Tcl/Tk support. Operational errors are recorded in `C:\ProgramData\MICWatcher\microscope_watcher.log`.

For diagnosis, open PowerShell while signed in as the affected user and run:

```powershell
& "C:\Program Files\Python314\python.exe" "C:\Program Files\MICWatcher\micwatcher_gui.py"
```

Unlike the windowless desktop shortcut, this command displays any startup error. If it opens the GUI, rerun the current MICWatcher installer as administrator to rebuild the shared shortcut with the direct machine-wide Python path.

**MICWatcher says another copy is already running**

Another signed-in Windows account may already be running it. Return to that account and stop its MICWatcher window before starting a new copy.
