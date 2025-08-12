import os
import platform
import logging
import json

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from acadia_gui import CONFIG_PATH
from acadia_qmsmt.helpers.path_adapter import detect_platform


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

        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling) # Just to be safe


def get_qt_scaling():
    try:
        return float(os.environ.get("QT_SCALE_FACTOR", "1.0"))
    except ValueError:
        return 1.0