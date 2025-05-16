import psutil
import os
from pathlib import Path
try:
    from pympler import asizeof
except ImportError:
    asizeof = None


from PyQt5.QtWidgets import QMenuBar, QAction, QLabel, QApplication, QTextEdit, QWidget, QVBoxLayout, QFrame
from PyQt5.QtCore import QTimer, Qt


from acadia_gui import THEME_PATH

MEM_THRES_MEDIUM = 2 # threshold for medium memory usage, in GB
MEM_THRES_HIGH = 10 # threshold for high memory usage, in GB


def count_qobjects():
    app = QApplication.instance()
    if app is None:
        return 0

    def recurse(obj):
        total = 1  # Count self
        for child in obj.children():
            total += recurse(child)
        return total

    total = recurse(app)
    return total


class AppMenuBar(QMenuBar):
    def __init__(self, parent=None, apply_theme_callback=None):
        super().__init__(parent)
        self.apply_theme_callback = apply_theme_callback
        self.parent_window = parent

        # === Menus ===
        self.theme_menu = self.addMenu("Theme")
        self.help_menu = self.addMenu("Help")

        self.load_themes()

        # === Memory usage ===
        self.mem_label = QLabel("Memory Usage: -- GB")
        self.mem_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mem_label.mousePressEvent = self.toggle_memory_popup
        self.setCornerWidget(self.mem_label, Qt.TopRightCorner)

        if asizeof:
            self.mem_popup = QFrame(None, Qt.Popup | Qt.FramelessWindowHint)
            self.mem_popup.setAttribute(Qt.WA_ShowWithoutActivating)
            self.mem_popup.setFrameShape(QFrame.StyledPanel)
            self.mem_popup.setVisible(False)

            self.mem_textbox = QTextEdit(self.mem_popup)
            self.mem_textbox.setReadOnly(True)
            self.mem_textbox.setMinimumWidth(220)
            self.mem_textbox.setMinimumHeight(140)

            layout = QVBoxLayout(self.mem_popup)
            layout.setContentsMargins(3, 3, 3, 3)
            layout.addWidget(self.mem_textbox)

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

        # Count number of live QObjects
        num_qobjects = count_qobjects() # todo: this should only show in the detailed text box once we done debugging

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

        self.mem_label.setText(f"Memory: {mem_gb:.2f} GB | {num_qobjects} QObjects")

        if self.mem_popup.isVisible():
            self.update_mem_pop_text()

    def get_tab_memory_usage(self):
        if self.parent_window is None:
            return "No parent window found."

        components = {
            "LogViewer": getattr(self.parent_window.right_tabs, "log_tab", None),
            "InstrumentParamsViewer": getattr(self.parent_window.right_tabs, "instruments_tab", None),
            "YamlViewer": getattr(self.parent_window.right_tabs, "config_yaml_tab", None),
            "KwargsJsonViewer": getattr(self.parent_window.right_tabs, "kwargs_json_tab", None),
            "FigureDisplayWidget": getattr(self.parent_window, "figure_display", None),
            "FolderTreeWidget": getattr(self.parent_window, "folder_tree", None),
            "LivePlotWidget": getattr(self.parent_window.figure_display, "live_plot", None),  # If applicable
            "AppMenuBar": self,
        }

        result_lines = ["--- Python Object Mem Usage ---\n"]
        total_mem = 0
        for name, obj in components.items():
            if obj is not None:
                size_bytes = asizeof.asizeof(obj)
                size_mb = size_bytes / (1024 ** 2)
                result_lines.append(f"{name}: {size_mb:.2f} MB")
                total_mem += size_mb
            else:
                result_lines.append(f"{name}: (not found)")

        result_lines.append(f"\nTotal Tracked Memory: {total_mem:.2f} MB")
        return "\n".join(result_lines)


    def update_mem_pop_text(self):
        message = self.get_tab_memory_usage()
        if len(message) > 3000:
            message = message[:3000] + "\n... (truncated)"
        self.mem_textbox.setText(message)

    def toggle_memory_popup(self, event):
        if not asizeof:
            return

        if self.mem_popup.isVisible():
            self.mem_popup.hide()
        else:
            # Update text
            self.update_mem_pop_text()

            # Position below the mem_label
            global_pos = self.mem_label.mapToGlobal(self.mem_label.rect().bottomLeft())
            self.mem_popup.move(global_pos)
            self.mem_popup.adjustSize()
            self.mem_popup.show()




