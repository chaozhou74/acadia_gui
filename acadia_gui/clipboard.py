"""Copy an image to the system clipboard without ever blocking the GUI.

On WSL the Linux clipboard does not reach Windows apps (OneNote, PowerPoint...), so the image is
handed to Windows through ``powershell.exe``. That call used to be a blocking ``subprocess.run``
on the UI thread with no timeout and an inherited stdin. When the GUI is started from Jupyter
(``!acadia_gui``) its stdin is a pseudo-terminal: Windows' console host then sends a cursor-position
query (``ESC[6n``) to that terminal and waits for a reply Jupyter never sends, so the GUI froze
forever. Here PowerShell runs as an asynchronous ``QProcess`` whose stdin is a closed pipe (never a
terminal), with a hard timeout, so the UI never waits on it no matter how the GUI was launched.
"""
import os
import logging
import subprocess
import tempfile

from PyQt5.QtCore import QProcess, QTimer
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

from acadia_qmsmt.utils.path_adapter import detect_platform

logger = logging.getLogger(__name__)

POWERSHELL_TIMEOUT_MS = 15000
_running = set()   # live (QProcess, QTimer, temp path) jobs; keeps them referenced until finished


def _wsl_to_windows_path(path: str) -> str:
    """`wslpath -w` with a timeout and no terminal stdin."""
    result = subprocess.run(["wslpath", "-w", path], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5, check=True)
    return result.stdout.strip()


def _powershell_set_image_script(windows_png_path: str) -> str:
    quoted = windows_png_path.replace("'", "''")   # escape for a single-quoted PowerShell string
    return ("Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            f"$img = [System.Drawing.Image]::FromFile('{quoted}'); "
            "[System.Windows.Forms.Clipboard]::SetImage($img)")


def copy_image_to_clipboard(image: QImage, description: str = "image") -> None:
    """Put `image` on the clipboard. Returns immediately; success/failure is logged."""
    if detect_platform() != "wsl":
        try:
            QApplication.clipboard().setImage(image)
            logger.info(f"Snapshot {description} copied to clipboard!")
        except Exception as e:
            logger.error(f"Failed to copy snapshot to clipboard: {e}", exc_info=True)
        return

    fd, png_path = tempfile.mkstemp(prefix="acadia_snapshot_", suffix=".png")
    os.close(fd)
    try:
        if not image.save(png_path, "PNG"):
            raise OSError(f"could not write {png_path}")
        script = _powershell_set_image_script(_wsl_to_windows_path(png_path))
    except Exception as e:
        logger.error(f"Failed to prepare snapshot for the clipboard: {e}", exc_info=True)
        _remove(png_path)
        return

    proc = QProcess()
    proc.setProcessChannelMode(QProcess.MergedChannels)
    timer = QTimer()
    timer.setSingleShot(True)
    job = (proc, timer, png_path)
    _running.add(job)

    def finish(*_):
        if job not in _running:
            return
        _running.discard(job)
        timer.stop()
        output = bytes(proc.readAll()).decode(errors="replace").strip()
        if proc.exitStatus() == QProcess.NormalExit and proc.exitCode() == 0:
            logger.info(f"Snapshot {description} copied to clipboard!")
        else:
            logger.error(f"Failed to copy snapshot to clipboard (exit code {proc.exitCode()}): {output}")
        _remove(png_path)
        proc.deleteLater()

    def on_error(err):
        if err == QProcess.FailedToStart:
            logger.error("Failed to copy snapshot to clipboard: powershell.exe could not be started "
                         "(is WSL interop enabled?)")
            finish()

    def on_timeout():
        logger.error(f"Copying snapshot to clipboard timed out after {POWERSHELL_TIMEOUT_MS / 1000:.0f} s")
        proc.kill()

    proc.finished.connect(finish)
    proc.errorOccurred.connect(on_error)
    timer.timeout.connect(on_timeout)
    proc.start("powershell.exe", ["-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                                  "-Command", script])
    proc.closeWriteChannel()   # stdin is a closed pipe -- never a terminal
    timer.start(POWERSHELL_TIMEOUT_MS)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
