# MICWatcher Windows launcher guide

This guide is for microscope operators. The Gmail sender account and desktop launcher should already have been configured once by the lab administrator.

## Start an experiment

1. Start the microscopy experiment normally and confirm that it is saving images.
2. In File Explorer, open the local folder in which that experiment is saving files.
3. Click the File Explorer address bar or press Ctrl+L.
4. Press Ctrl+C to copy the complete folder path.
5. Double-click **MICWatcher** on the Desktop or open **MICWatcher** from the Start menu.
6. When the launcher asks for the local acquisition folder, right-click in the terminal window to paste the copied path, then press Enter.

The local path will normally look similar to:

```text
D:\MicroscopeData\Experiment_2026_09_25
```

Do not select an individual image file. Select the folder containing the experiment files.

## Choose a network destination

If files should be transferred to the lab server:

1. Open File Explorer.
2. Click the address bar or press Ctrl+L.
3. Enter the server address below and press Enter:

   ```text
   \\izbkingston.unibe.ch\towbin.data
   ```

4. If Windows asks for credentials, sign in with the account that has access to the share.
5. Navigate to the desired destination and create a folder for the microscope or experiment if necessary.
6. Open that destination folder.
7. Click the address bar or press Ctrl+L, then press Ctrl+C to copy its complete path.
8. When the launcher asks for the transfer destination, right-click in the terminal to paste the path and press Enter.

Use a separate destination for each microscope. The destination cannot be inside the local acquisition folder.

## Answer the launcher questions

The launcher asks for:

1. **Alert email address** — the user who should receive warnings and reports.
2. **Local acquisition folder** — paste the path copied from File Explorer.
3. **Missing-file check interval** — press Enter for the 60-minute default, or enter another number of minutes.
4. **Transfer stable files to a network folder** — enter Y or N.
5. If transfer is enabled, **Transfer destination folder** — paste the network path. There is no default.
6. **File-transfer check interval** — press Enter for the 30-minute default, or enter another number of minutes.

Review the summary, then press Enter to start MICWatcher.

## While MICWatcher is running

- Leave the MICWatcher terminal window open.
- The program starts monitoring immediately.
- Existing stable files in the local folder are also transferred when transfer is enabled.
- To stop monitoring, select the terminal window and press Ctrl+C.
- Do not start a second copy of MICWatcher for the same installation.

## Email behavior

- When no file activity is detected for one complete interval, MICWatcher sends the first warning.
- If another complete interval passes without activity, it sends a second warning.
- It then remains silent until file activity resumes and sends one recovery email.
- A transfer failure generates one warning. Repeated failures remain silent until a successful transfer generates a recovery email.
- Every 24 hours, it sends a report containing file count, disk space, and transfer status.
- If the untransferred backlog exceeds 10,000 files, it sends a safety-stop warning and exits without deleting those files.

## Troubleshooting

**The launcher says that the local folder does not exist**

Open the exact folder in File Explorer and copy its path from the address bar again. Do not include quotation marks.

**The launcher says that the network destination does not exist or is not writable**

Open `\\izbkingston.unibe.ch\towbin.data` in File Explorer first, authenticate if requested, and verify that you can create a folder or file in the chosen destination. Then start MICWatcher again.

**A network path stops working during an experiment**

Reconnect it in File Explorer. MICWatcher retains local files when transfer fails and reports recovery after a later successful transfer.

**The terminal closes immediately**

Open `microscope_watcher.log` in the MICWatcher folder. If the installation was moved, run `python install_windows_launcher.py` again so the desktop launcher contains the new path.
