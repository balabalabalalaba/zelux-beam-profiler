"""Native "choose a folder" dialog, shown on the computer that runs the profiler.

The page cannot browse the PC's folders itself, so the server opens the
system dialog: Windows (PowerShell + Windows Forms, built into every Windows
10/11), macOS (AppleScript) or Linux (zenity, if installed). No extra packages.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

_BUSY = threading.Lock()

_PS_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Choose the folder for quick saves'
$d.ShowNewFolderButton = $true
if ($env:BP_INITIAL -and (Test-Path -LiteralPath $env:BP_INITIAL)) { $d.SelectedPath = $env:BP_INITIAL }
$r = $d.ShowDialog($owner)
$owner.Close()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }
"""


class PickerUnavailable(RuntimeError):
    pass


class PickerBusy(RuntimeError):
    pass


def pick_folder(initial: str | None = None) -> str | None:
    """Show the dialog and wait. Returns the chosen folder, or None if cancelled."""
    if not _BUSY.acquire(blocking=False):
        raise PickerBusy("A folder dialog is already open.")
    try:
        return _pick(initial)
    finally:
        _BUSY.release()


def _pick(initial: str | None) -> str | None:
    env = dict(os.environ)
    if sys.platform == "win32":
        exe = shutil.which("powershell.exe") or os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        env["BP_INITIAL"] = initial or ""
        cmd = [exe, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elif sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Choose the folder for quick saves"'
        if initial and os.path.isdir(initial):
            script += ' default location (POSIX file (system attribute "BP_INITIAL"))'
            env["BP_INITIAL"] = initial
        script += ")"
        cmd, flags = ["osascript", "-e", script], 0
    else:
        exe = shutil.which("zenity")
        if not exe:
            raise PickerUnavailable("No folder dialog on this computer; type the folder path instead.")
        cmd = [exe, "--file-selection", "--directory", "--title=Choose the folder for quick saves"]
        if initial:
            cmd.append(f"--filename={initial.rstrip('/')}/")
        flags = 0
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, creationflags=flags, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PickerUnavailable(f"Could not open the folder dialog: {e}") from e
    path = out.stdout.decode("utf-8", errors="replace").strip()
    if out.returncode != 0 or not path:
        return None
    return path.rstrip("/\\") if len(path) > 3 else path
