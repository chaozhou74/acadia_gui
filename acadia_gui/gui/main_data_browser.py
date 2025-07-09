import sys
import logging
import gc
import os
import json

from PyQt5.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QSplitter,
    QTabWidget
)
from PyQt5.QtCore import Qt

from acadia_gui.gui import (LogViewer, InstrumentParamsViewer, YamlViewer,
                            FigureDisplayWidget, FolderTreeWidget, is_datafolder,
                            AppMenuBar, KwargsJsonViewer, GuiLogWindow, GuiLogHandler)
from acadia_gui import THEME_PATH
from acadia_gui.utils import set_qt_scaling, load_user_config
from acadia_gui.icons import ICON_PATH

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
    def __init__(self, root_path:str, client_station=None, theme:str=None, logging_level=logging.DEBUG):
        """
        Main data browser gui layout

        :param root_path: Path to the root data folder.
        :param client_station: Optional. A `instrumentserver.ClientStation` instance used to restore instrument
            states from previously saved configurations in the data browser GUI.
        :param theme: Name of the stylesheet theme to apply. Should correspond to a css file in `acadia_gui/themes`.
        :param logging_level: Logging level for the global logger. It is recommended to set this to the lowest
            level (e.g., DEBUG) and use log handlers within the GUI to filter messages as needed.
        """
        # todo: add code view

        super().__init__()
        self.setWindowTitle("Data Browser")
        self.resize(1600, 1000)

        # --- Menu Bar ---
        self.menu_bar = AppMenuBar(parent=self, apply_theme_callback=self.apply_theme)
        self.setMenuBar(self.menu_bar)

        # --- Main GUI components ---
        self.folder_tree = FolderTreeWidget(root_path, self.on_folder_selected)
        self.figure_display = FigureDisplayWidget()
        self.right_tabs = RightPanelTabs(client_station)

        # --- Right column: image + params ---
        right_splitter = QSplitter(Qt.Horizontal)
        right_splitter.addWidget(self.figure_display)
        right_splitter.addWidget(self.right_tabs)
        right_splitter.setSizes([850, 400])

        # --- Top-level splitter: tree | main view ---
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(self.folder_tree)
        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([250, 1250])

        # ---- log window ------------
        self.log_window = GuiLogWindow(self)
        self.log_window.setVisible(False)
        handler = GuiLogHandler(self.log_window)
        self.log_window.set_handler(handler)  # Link back to allow filter updates
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging_level)

        # --- Main layout ---
        outer_splitter = QSplitter(Qt.Vertical)
        outer_splitter.addWidget(main_splitter)
        outer_splitter.addWidget(self.log_window)
        outer_splitter.setSizes([800, 200])  # Adjust as needed

        central_widget = QWidget()
        central_layout = QVBoxLayout(central_widget)
        central_layout.addWidget(outer_splitter)
        central_widget.setLayout(central_layout)
        self.setCentralWidget(central_widget)


        if theme is None:
            theme = load_user_config().get("theme") or "default"
        
        self.apply_theme(theme)

        # --- Center window on leftmost screen ---
        self.center_on_left_screen()


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
            theme_path = THEME_PATH/ theme_name
            if theme_path.suffix != '.css':
                theme_path = theme_path.with_suffix('.css')
            with open(theme_path, "r") as f:
                qss = f.read()
                # Replace placeholder with an absolute path to your icon folder
                qss = qss.replace("__ICON_PATH__", ICON_PATH)
                self.setStyleSheet(qss)
        except Exception as e:
            logger.error(f"Failed to apply theme {theme_name}: {e}")

        # forward to central matplotlib plot
        self.figure_display.set_theme(theme_name)


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

