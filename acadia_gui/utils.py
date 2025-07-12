import os
import json

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from acadia_gui import CONFIG_PATH


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