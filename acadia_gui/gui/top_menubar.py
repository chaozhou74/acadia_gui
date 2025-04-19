from PyQt5.QtWidgets import QMenuBar, QAction, QLabel
from PyQt5.QtCore import QTimer, Qt
import psutil
import os
from pathlib import Path

from acadia_gui import THEME_PATH

MEM_THRES_MEDIUM = 2 # threshold for medium memory usage, in GB
MEM_THRES_HIGH = 10 # threshold for high memory usage, in GB

class AppMenuBar(QMenuBar):
    def __init__(self, parent=None, apply_theme_callback=None):
        super().__init__(parent)

        self.apply_theme_callback = apply_theme_callback

        # === Menus ===
        self.theme_menu = self.addMenu("Theme")
        self.help_menu = self.addMenu("Help")

        self.load_themes()

        # === Memory usage ===
        self.mem_label = QLabel("Memory Usage: -- GB")
        self.mem_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setCornerWidget(self.mem_label, Qt.TopRightCorner)

        self.update_memory_label()
        self.updateGeometry()
        self.repaint()

        self.mem_timer = QTimer()
        self.mem_timer.timeout.connect(self.update_memory_label)
        self.mem_timer.start(1000)

    def load_themes(self):
        self.theme_menu.clear()
        theme_dir = Path(THEME_PATH)
        if theme_dir.exists():
            for file in theme_dir.glob("*.css"):
                action = QAction(file.stem, self)
                action.triggered.connect(lambda _, name=file.name: self.apply_theme_callback(name))
                self.theme_menu.addAction(action)

    def update_memory_label(self):
        mem_bytes = psutil.Process(os.getpid()).memory_info().rss
        mem_gb = mem_bytes / (1024 ** 3)

        color = "black"
        style = ""
        if mem_gb > MEM_THRES_HIGH:
            color = "#ff5555"
            style = "font-weight: bold;"
        elif mem_gb > MEM_THRES_MEDIUM:
            color = "orange"

        if mem_gb > MEM_THRES_MEDIUM:
            self.mem_label.setStyleSheet(f"color: {color}; {style}")
        else:
            self.mem_label.setStyleSheet("")

        self.mem_label.setText(f"Memory Usage: {mem_gb:.2f} GB")
