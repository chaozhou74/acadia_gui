import sys
from PyQt5.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QSplitter,
    QTabWidget
)
from PyQt5.QtCore import Qt

from acadia_gui.gui import LogViewer, InstrumentParamsViewer, YamlViewer, FigureDisplayWidget, FolderTreeWidget, is_datafolder, AppMenuBar
from acadia_gui import THEME_PATH


class RightPanelTabs(QTabWidget):
    def __init__(self, client_station=None):
        super().__init__()
        self.config_yaml_tab = YamlViewer()
        self.instruments_tab = InstrumentParamsViewer(client_station)
        self.log_tab = LogViewer()

        self.addTab(self.config_yaml_tab, "Config YAMLs")
        self.addTab(self.instruments_tab, "Instruments")
        self.addTab(self.log_tab, "Logs")

    def update_content(self, folder_path):
        self.instruments_tab.load_json(folder_path)
        self.config_yaml_tab.load_yaml_files(folder_path)
        self.log_tab.load_logs(folder_path)


class DataBrowser(QMainWindow):
    def __init__(self, root_path, client_station=None, theme:str="default"):
        # todo: add code view
        # todo: add logger

        super().__init__()
        self.setWindowTitle("Data Browser")
        self.resize(1400, 700)

        # --- Menu Bar ---
        self.menu_bar = AppMenuBar(apply_theme_callback=self.apply_theme)
        self.setMenuBar(self.menu_bar)


        # --- Main GUI components ---
        self.folder_tree = FolderTreeWidget(root_path, self.on_folder_selected)
        self.figure_display = FigureDisplayWidget()
        self.right_tabs = RightPanelTabs(client_station)

        # --- Right column: image + params ---
        right_splitter = QSplitter(Qt.Horizontal)
        right_splitter.addWidget(self.figure_display)
        right_splitter.addWidget(self.right_tabs)
        right_splitter.setSizes([600, 500])

        # --- Top-level splitter: tree | main view ---
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(self.folder_tree)
        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([300, 1100])

        # --- Main layout ---
        central_widget = QWidget()
        central_layout = QVBoxLayout(central_widget)
        central_layout.addWidget(main_splitter)
        central_widget.setLayout(central_layout)
        self.setCentralWidget(central_widget)

        if theme is not None:
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
        if not is_datafolder(folder_path):
            self.figure_display.clear()
            self.right_tabs.instruments_tab.clear()
            self.right_tabs.config_yaml_tab.clear()
            return

        self.figure_display.load_images(folder_path)
        self.right_tabs.update_content(folder_path)

    def apply_theme(self, theme_name):
        try:
            theme_path = THEME_PATH/ theme_name
            if theme_path.suffix != '.css':
                theme_path = theme_path.with_suffix('.css')
            with open(theme_path, "r") as f:
                self.setStyleSheet(f.read())
        except Exception as e:
            print(f"Failed to apply theme {theme_name}: {e}")

        # forward to central matplotlib plot
        self.figure_display.set_theme(theme_name)


if __name__ == "__main__":
    from acadia_gui.examples.instrument_client.ins_client import make_client_station
    station = make_client_station()

    root_path = "/home/chao/Data/LINC_Cooldown_20250416"


    app = QApplication(sys.argv)
    window = DataBrowser(root_path, station)
    window.show()
    sys.exit(app.exec_())



