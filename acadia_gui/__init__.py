from pathlib import Path

THEME_PATH = Path(__file__).parent / "themes"
PACKAGE_PATH = Path(__file__).parent
CONFIG_PATH = Path.home() / ".acadia_gui_config.json"

# App identity. APP_ID must match the desktop-file basename (acadia_gui.desktop)
# and the Wayland app_id (QApplication.setDesktopFileName) so WSLg/Wayland can
# resolve the window's taskbar icon; APP_NAME is the human-facing title.
APP_ID = "acadia_gui"
APP_NAME = "Acadia Data Browser"

