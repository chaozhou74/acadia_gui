import sys
import logging
import gc
import os
import json

from PyQt5.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QSplitter, QSizePolicy,
    QTabWidget
)
from PyQt5.QtCore import Qt, QTimer, QPoint, QSize, QRect, QSettings

from acadia_gui.gui import (LogViewer, InstrumentParamsViewer, YamlViewer,
                            FigureDisplayWidget, FolderTreeWidget, is_datafolder,
                            AppMenuBar, KwargsJsonViewer, GuiLogWindow, GuiLogHandler)
from acadia_gui import THEME_PATH
from acadia_gui.utils import set_qt_scaling, load_user_config, get_qt_scaling, update_user_config
from acadia_gui.icons import ICON_PATH, set_icon_color, icon_color_for_theme

logger = logging.getLogger(__name__)


def force_garbage_collect():
    collected = gc.collect()


class RightPanelTabs(QTabWidget):
    def __init__(self, client_station=None):
        super().__init__()
        self.config_yaml_tab = YamlViewer()
        self.instruments_tab = InstrumentParamsViewer(client_station)
        self.log_tab = LogViewer()
        self.kwargs_json_tab = KwargsJsonViewer()

        self.addTab(self.config_yaml_tab, "Config YAMLs")
        self.addTab(self.kwargs_json_tab, "Kwargs")
        self.addTab(self.instruments_tab, "Instruments")
        self.addTab(self.log_tab, "Logs")

    def update_content(self, folder_path):
        self.instruments_tab.load_json(folder_path)
        self.config_yaml_tab.load_yaml_files(folder_path)
        self.log_tab.load_logs(folder_path)
        self.kwargs_json_tab.load_json(folder_path)

    def clear(self):
        self.instruments_tab.clear()
        self.config_yaml_tab.clear()
        self.kwargs_json_tab.clear()
        self.log_tab.clear()


class DataBrowser(QMainWindow):
    def __init__(self, root_path: str, client_station=None, theme: str = None, logging_level=logging.DEBUG):
        """
        Main data browser gui layout

        :param root_path: Path to the root data folder.
        :param client_station: Optional. A `instrumentserver.ClientStation` instance used to restore instrument
            states from previously saved configurations in the data browser GUI.
        :param theme: Name of the stylesheet theme to apply. Should correspond to a css file in `acadia_gui/themes`.
        :param logging_level: Logging level for the global logger. It is recommended to set this to the lowest
            level (e.g., DEBUG) and use log handlers within the GUI to filter messages as needed.
        """

        super().__init__()
        self.setWindowTitle("Data Browser")
        self.resize(1600, 1000)

        # Resolve the theme up front so widget icons are recolored as they're
        # built (get_icon reads the active icon color at construction time).
        if theme is None:
            theme = load_user_config().get("theme") or "default"
        set_icon_color(icon_color_for_theme(theme))

        # --- Menu Bar ---
        self.menu_bar = AppMenuBar(parent=self, apply_theme_callback=self.apply_theme)
        self.setMenuBar(self.menu_bar)

        # --- Main GUI components ---
        self.folder_tree = FolderTreeWidget(root_path, self.on_folder_selected)
        self.figure_display = FigureDisplayWidget()
        self.right_tabs = RightPanelTabs(client_station)

        # --- Right column: image + params ---
        self.right_splitter = QSplitter(Qt.Horizontal)
        self.right_splitter.addWidget(self.figure_display)
        self.right_splitter.addWidget(self.right_tabs)
        # size debugging
        # self.figure_display.setStyleSheet("border: 2px solid red;")
        # self.right_tabs.setStyleSheet("border: 2px solid green;")
        # self.right_splitter.setStyleSheet("border: 2px dashed blue;")

        # --- Top-level splitter: tree | main view ---
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.folder_tree)
        self.main_splitter.addWidget(self.right_splitter)

        # ---- log window ------------
        self.log_window = GuiLogWindow(self)
        self.log_window.setVisible(False)
        handler = GuiLogHandler(self.log_window)
        self.log_window.set_handler(handler)  # Link back to allow filter updates
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging_level)

        # --- Main layout ---
        self.outer_splitter = QSplitter(Qt.Vertical)
        self.outer_splitter.addWidget(self.main_splitter)
        self.outer_splitter.addWidget(self.log_window)

        central_widget = QWidget()
        central_layout = QVBoxLayout(central_widget)
        central_layout.addWidget(self.outer_splitter)
        central_widget.setLayout(central_layout)
        self.setCentralWidget(central_widget)

        self.apply_theme(theme)

        # --- Center window on leftmost screen ---
        self.load_ui_settings()
        # self.center_on_left_screen()

    def center_on_left_screen(self):
        screens = QApplication.screens()
        left_screen = min(screens, key=lambda s: s.geometry().x())
        geometry = left_screen.availableGeometry()

        x = geometry.x() + (geometry.width() - self.width()) // 2
        y = geometry.y() + (geometry.height() - self.height()) // 2
        self.move(x, y)

    def on_folder_selected(self, folder_path):
        self.figure_display.clear()
        self.right_tabs.clear()

        if not is_datafolder(folder_path):
            return

        force_garbage_collect()
        self.figure_display.load_images(folder_path)
        self.right_tabs.update_content(folder_path)

    def apply_theme(self, theme_name):
        try:
            theme_path = THEME_PATH / theme_name
            if theme_path.suffix != '.css':
                theme_path = theme_path.with_suffix('.css')
            with open(theme_path, "r") as f:
                qss = f.read()
                # Replace placeholder with an absolute path to your icon folder
                qss = qss.replace("__ICON_PATH__", ICON_PATH)
                # Apply at the application level so the stylesheet also reaches
                # top-level popup windows (e.g. QComboBox dropdown containers),
                # which do not inherit a stylesheet set on an ancestor widget.
                app = QApplication.instance()
                if app is not None:
                    app.setStyleSheet(qss)
                else:
                    self.setStyleSheet(qss)
        except Exception as e:
            logger.error(f"Failed to apply theme {theme_name}: {e}")

        # recolor `currentColor` icons for the new theme and refresh those already
        # shown (the plot toolbar icons are refreshed via set_theme below)
        set_icon_color(icon_color_for_theme(theme_name))
        self.menu_bar.reload_icons()
        self.folder_tree.reload_icons()

        # forward to central matplotlib plot
        self.figure_display.set_theme(theme_name)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.adjust_splitter_sizes)

    def adjust_splitter_sizes(self):
        folder_width = 250
        right_main_width = self.width() - folder_width

        self.main_splitter.setSizes([folder_width, right_main_width])
        self.right_splitter.setSizes([int(0.7 * right_main_width), int(0.3 * right_main_width)])
        self.outer_splitter.setSizes([int(self.height() * 0.8), int(self.height() * 0.2)])

    def _ensure_on_screen(self):
        """If the saved geometry is off-screen (monitor unplugged etc), recenter."""
        frame = self.frameGeometry()          # QRect in global coords
        screens = QApplication.screens()
        if not screens:
            return
        # If the window’s top-left isn’t contained in any screen, recenter.
        top_left = frame.topLeft()
        on_any = any(scr.geometry().contains(top_left) for scr in screens)
        if not on_any:
            self.center_on_left_screen()

    def load_ui_settings(self):
        s = QSettings("acadia", "DataBrowser")
        # --- window frame ---
        geo = s.value("main/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.center_on_left_screen()

        state = s.value("main/windowState")
        if state is not None:
            self.restoreState(state)

        # --- splitters ---
        outer = s.value("splitters/outer")
        if outer is not None:
            self.outer_splitter.restoreState(outer)

        main = s.value("splitters/main")
        if main is not None:
            self.main_splitter.restoreState(main)

        right = s.value("splitters/right")
        if right is not None:
            self.right_splitter.restoreState(right)

        # --- log window ---
        log_geo = s.value("log/geometry")
        if log_geo is not None:
            self.log_window.restoreGeometry(log_geo)
        visible = s.value("log/visible")
        if visible is not None:
            is_visible = visible == "true" or visible is True
            self.log_window.setVisible(is_visible)
            self.menu_bar.show_log_action.setChecked(is_visible)

        # Ensure the window is actually on a connected screen
        self._ensure_on_screen()

    def save_ui_settings(self):
        s = QSettings("acadia", "DataBrowser")
        s.setValue("main/geometry", self.saveGeometry())
        s.setValue("main/windowState", self.saveState())

        s.setValue("splitters/outer", self.outer_splitter.saveState())
        s.setValue("splitters/main", self.main_splitter.saveState())
        s.setValue("splitters/right", self.right_splitter.saveState())

        s.setValue("log/geometry", self.log_window.saveGeometry())
        s.setValue("log/visible", self.log_window.isVisible())

        # remember the data root so the next launch reopens it (see resolve_startup_root)
        update_user_config(last_root=self.folder_tree.root_path)

    def closeEvent(self, event):
        try:
            self.save_ui_settings()
        finally:
            super().closeEvent(event)


if __name__ == "__main__":
    # example code for starting the main data browser gui app window

    from acadia_gui.examples.instrument_client.ins_client import make_client_station
    # inst_station = make_client_station() # for setting instrument parameters from gui
    inst_station = None

    root_path = "/home/chao/Data"
    # root_path = "/home/rsl/Data"
    set_qt_scaling()
    app = QApplication(sys.argv)
    window = DataBrowser(root_path, inst_station)
    window.show()
    sys.exit(app.exec_())

