import os
import sys
import shutil
import subprocess
import logging
import json
from pathlib import Path

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtWidgets import QApplication

from acadia_gui import CONFIG_PATH, APP_ID, APP_NAME
from acadia_qmsmt.utils.path_adapter import detect_platform


logger = logging.getLogger(__name__)

def check_wsl_interop():
    """
    Check whether WSL's Windows interop is available, which is need for features like 
    screen capture and file access.
    Returns True if Windows executables (explorer.exe, powershell.exe) can be launched.
    Logs a warning with fix instructions if interop is missing.
    """
    # Only relevant for Linux WSL environments
    if not detect_platform() == "wsl":
        return True  # Not in WSL — nothing to check

    interop_path = "/proc/sys/fs/binfmt_misc/WSLInterop"
    if os.path.exists(interop_path):
        return True

    logger.warning(
        "⚠️  WSL interop is disabled — Windows executables cannot be launched from this session.\n"
        "Features like screen capture and file access may not work.\n"
        "This is an uncommon issue that can occur if WSL did not properly initialize interop support.\n"
        "\nOne-time fix (run inside WSL, no WSL restart required):\n"
        "  sudo mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc || true\n"
        "  echo 1 | sudo tee /proc/sys/fs/binfmt_misc/status >/dev/null\n"
        "  echo ':WSLInterop:M::MZ::/init:PF' | sudo tee /proc/sys/fs/binfmt_misc/register\n"
        "\nLong-term fix (usually not needed, but can probably prevent recurrence):\n"
        "  1) Ensure /etc/wsl.conf contains:\n"
        "       [interop]\n"
        "       enabled = true\n"
        "       appendWindowsPath = true\n"
        "  2) From Windows, run:  wsl --shutdown\n"
    )
    return False


def set_wsl_display_backend():
    """On WSL, use the Wayland Qt backend instead of the default xcb/XWayland.

    Under WSLg, XWayland tears down popup (override-redirect) windows slowly,
    which makes menus and combo-box dropdowns lag noticeably before collapsing.
    The native Wayland backend does not have this problem. No-op off WSL, and
    respects an explicit QT_QPA_PLATFORM if the user already set one.
    """
    if detect_platform() == "wsl":
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")


def _venv_exec_path():
    """Absolute path to this venv's acadia_gui console script.

    Computed from the running interpreter so the launcher works regardless of
    which virtual environment the app was installed into, with no activation.
    """
    cand = Path(sys.executable).with_name(APP_ID)
    if cand.exists():
        return str(cand)
    return shutil.which(APP_ID)


def _render_padded_pixmap(icon, size: int, margin: float):
    """Render the icon centered in a transparent size x size canvas with margin.

    The padding keeps the art off the edges so Windows' on-the-fly downscaling
    to small taskbar dimensions stays legible instead of muddy.
    """
    from PyQt5.QtGui import QPixmap, QPainter

    inner = max(1, round(size * (1 - 2 * margin)))
    src = icon.pixmap(QSize(inner, inner))
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    offset = (size - inner) // 2
    painter.drawPixmap(offset, offset, src)
    painter.end()
    return canvas


def _render_hicolor_pngs(app_id=APP_ID, sizes=(16, 24, 32, 48, 64, 128, 256), margin=0.1):
    """Render padded PNGs into the user XDG icon theme; returns the hicolor root."""
    from acadia_gui.icons import get_icon

    icon = get_icon("app_icon.svg")
    root = Path.home() / ".local/share/icons/hicolor"
    for s in sizes:
        dest = root / f"{s}x{s}/apps/{app_id}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _render_padded_pixmap(icon, s, margin).save(str(dest), "PNG")
    return root


def _write_user_desktop_file(exec_path, app_id=APP_ID):
    """Write a desktop entry (absolute Exec) into the user XDG dir for staging.

    No NoDisplay: WSLg resolves the running window's taskbar icon from this entry,
    and it filters NoDisplay entries out of icon resolution (not just menus), so
    hiding it here would also blank the running-window icon.
    """
    path = Path.home() / ".local/share/applications" / f"{app_id}.desktop"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={exec_path}\n"
        f"Icon={app_id}\n"
        f"StartupWMClass={app_id}\n"
        "Categories=Science;Utility;\n"
    )
    return path


def _install_system_wide(exec_path, verbose=False):
    """Copy the desktop entry + icons to /usr/share via one sudo prompt.

    Needed only for the *running window's* taskbar icon, which WSLg resolves from
    a system-wide entry (it ignores user-local ones). Stages the files in the
    user dirs first, then elevates to copy them. Falls back to printing the
    manual commands if sudo is unavailable or the copy fails.
    """
    desktop = _write_user_desktop_file(exec_path)
    hicolor = _render_hicolor_pngs()
    cache = ("gtk-update-icon-cache -f /usr/share/icons/hicolor"
             if shutil.which("gtk-update-icon-cache") else "true")
    script = (
        f"cp '{desktop}' /usr/share/applications/ && "
        f"cp -r '{hicolor}/.' /usr/share/icons/hicolor/ && "
        f"{cache}"
    )
    manual = (
        "To give the running window a crisp icon, run once:\n"
        f"  sudo cp '{desktop}' /usr/share/applications/\n"
        f"  sudo cp -r '{hicolor}/.' /usr/share/icons/hicolor/\n"
        "  sudo gtk-update-icon-cache -f /usr/share/icons/hicolor"
    )
    if not shutil.which("sudo"):
        print(manual)
        return False
    if verbose:
        print("Installing system-wide desktop entry + icons (sudo may prompt)...")
    res = subprocess.run(["sudo", "sh", "-c", script])  # inherit tty for the prompt
    if res.returncode != 0:
        print("\nSystem-wide install did not complete.\n" + manual)
        return False
    if verbose:
        print("System-wide entry installed.")
    return True


SYSTEM_DESKTOP_FILE = Path("/usr/share/applications") / f"{APP_ID}.desktop"
USER_DESKTOP_FILE = Path.home() / ".local/share/applications" / f"{APP_ID}.desktop"


def _install_user_local(exec_path, verbose=False):
    """Install the desktop entry + icons under ~/.local/share (no root).

    Native Linux desktop environments (GNOME on Ubuntu 22+, etc.) scan the
    per-user XDG dirs, so this alone makes the app show up in the application
    menu / dock. Refreshing the desktop and icon caches is best-effort.
    """
    desktop = _write_user_desktop_file(exec_path)
    icons = _render_hicolor_pngs()
    for cmd in (["update-desktop-database", str(desktop.parent)],
                ["gtk-update-icon-cache", "-f", "-t", str(icons)]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, check=False, capture_output=True, timeout=15)
            except Exception as e:
                logger.debug(f"Cache refresh failed ({cmd[0]}): {e}")
    if verbose:
        print(f"Installed desktop entry -> {desktop}")
    return True


def install_desktop_integration(verbose: bool = False):
    """Register the app with the OS so it appears in the application launcher.

    The desktop entry's Exec points at the venv's *absolute* console script so
    it launches without an activated environment, Icon resolves through the XDG
    icon theme, and icons are padded so downscaled taskbar renderings stay
    legible.

    Platform behavior:
      - Native Linux: install under ~/.local/share (no root); the desktop
        environment scans it directly.
      - WSL: install system-wide under /usr/share via one sudo prompt. WSLg
        ignores user-local entries and uses the system-wide one to generate the
        Windows Start Menu shortcut and the running-window taskbar icon.
      - Other (Windows/unknown): no-op.

    Best-effort: prints manual fallback commands if a WSL sudo copy fails, and
    never raises. Must be called after a QApplication exists (icons need it).
    """
    def _say(msg):
        if verbose:
            print(msg)
        logger.info(msg)

    plat = detect_platform()
    if plat not in ("wsl", "linux"):
        if verbose:
            print("Desktop integration is only supported on Linux / WSL.")
        return
    try:
        exec_path = _venv_exec_path()
        if not exec_path:
            logger.warning("Desktop integration skipped: could not locate the "
                           "acadia_gui executable.")
            return

        if plat == "wsl":
            if _install_system_wide(exec_path, verbose=verbose):
                _say("Run `wsl --shutdown` from Windows, then relaunch, so WSLg "
                     "picks up the shortcut and taskbar icon.")
        else:  # native Linux
            _install_user_local(exec_path, verbose=verbose)
            _say("Added to the application menu (log out/in if it doesn't appear "
                 "right away).")
    except Exception as e:
        logger.warning(f"Could not install desktop integration: {e}")


def setup_desktop_on_launch():
    """At GUI launch, keep desktop integration up to date with minimal fuss.

    Native Linux: silently install the (no-sudo) user-local entry once, so the
    app appears in the launcher without the user running anything. WSL: only log
    a hint, since the install needs sudo and can't run silently. No-op on Windows
    or once already set up; never prompts and never raises.
    """
    plat = detect_platform()
    try:
        if plat == "linux":
            if not USER_DESKTOP_FILE.exists():
                install_desktop_integration(verbose=False)
        elif plat == "wsl":
            if not SYSTEM_DESKTOP_FILE.exists():
                logger.info("Tip: run `acadia_gui --install-desktop` once to add the "
                            "Windows Start Menu shortcut and taskbar icon.")
    except Exception:
        pass


def load_user_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}

def save_user_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def set_qt_scaling(scale=None):
    """
    Sets Qt environment variables for UI scaling.
    If no scale is provided, it attempts to load from config.
    """
    if scale is None:
        scale = float(load_user_config().get("scale_factor", 1.0))

    os.environ["QT_SCALE_FACTOR"] = str(scale)
    # These help with fractional scaling support
    if scale > 1:
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
        os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)


def get_qt_scaling():
    try:
        return float(os.environ.get("QT_SCALE_FACTOR", "1.0"))
    except ValueError:
        return 1.0