import psutil
import os
from pathlib import Path
import logging


from PyQt5.QtWidgets import QMenuBar, QAction, QLabel, QApplication, QTextEdit, QWidget, QVBoxLayout, QFrame, QToolButton, QHBoxLayout, QInputDialog, QMessageBox
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QIcon


from acadia_gui import THEME_PATH, CONFIG_PATH
from acadia_gui.icons import get_icon
from acadia_gui.utils import load_user_config, save_user_config

MEM_THRES_MEDIUM = 3 # threshold for medium memory usage, in GB
MEM_THRES_HIGH = 10 # threshold for high memory usage, in GB

logger = logging.getLogger(__name__)

try:
    from pympler import asizeof
except ImportError:
    asizeof = None
    logger.warning("'pympler' module not found; python object memory usage is not shown. "
                   "to enable, do: `pip install pympler`")

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

        self.config = load_user_config()

        # === Menus ===
        self.theme_menu = self.addMenu("Theme")
        self.view_menu = self.addMenu("View")
        self.help_menu = self.addMenu("Help")

        self._load_themes()
        self.set_view_actions()

        # === Memory usage ===
        self.mem_label = QToolButton()
        self.mem_label.setText("Memory Usage: -- GB")
        self.mem_label.clicked.connect(self.toggle_memory_popup)
        self.mem_label.setProperty("class", "hoverFlat")

        if asizeof:
            self.mem_popup = QFrame(None, Qt.Popup | Qt.FramelessWindowHint)
            self.mem_popup.setObjectName("MemoryPopup")
            self.mem_popup.setAttribute(Qt.WA_ShowWithoutActivating)
            self.mem_popup.setFrameShape(QFrame.StyledPanel)
            self.mem_popup.setVisible(False)

            self.mem_textbox = QTextEdit(self.mem_popup)
            self.mem_textbox.setReadOnly(True)
            self.mem_textbox.setMinimumWidth(250)
            self.mem_textbox.setMinimumHeight(250)

            layout = QVBoxLayout(self.mem_popup)
            layout.setContentsMargins(3, 3, 3, 3)
            layout.addWidget(self.mem_textbox)

        self.update_memory_label()
        self.updateGeometry()
        self.repaint()

        self.mem_timer = QTimer()
        self.mem_timer.timeout.connect(self.update_memory_label)
        self.mem_timer.start(1000)

        # === Shortcut Right panel toggle button ===
        self.toggle_right_tabs_button = QToolButton()
        self.toggle_right_tabs_button.setIcon(QIcon.fromTheme(get_icon("collapse_right_tabs.svg")))
        self.toggle_right_tabs_button.setToolTip("Hide right panel")
        self.toggle_right_tabs_button.setCheckable(True)
        self.toggle_right_tabs_button.setChecked(False)
        self.toggle_right_tabs_button.toggled.connect(lambda checked: self.toggle_right_panel(not checked))

        # Add button into the corner widget
        separator_line = QFrame()
        separator_line.setFrameShape(QFrame.VLine)
        separator_line.setFrameShadow(QFrame.Sunken)
        corner_widget = QWidget()
        corner_layout = QHBoxLayout(corner_widget)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(4)
        corner_layout.addWidget(self.mem_label)
        corner_layout.addWidget(separator_line)
        corner_layout.addWidget(self.toggle_right_tabs_button)
        self.setCornerWidget(corner_widget, Qt.TopRightCorner)
        corner_widget.adjustSize()

    def _load_themes(self):
        self.theme_menu.clear()
        theme_dir = Path(THEME_PATH)
        if theme_dir.exists():
            for file in theme_dir.glob("*.css"):
                action = QAction(file.stem, self)
                action.triggered.connect(lambda _, name=file.name: self.set_theme(name))
                self.theme_menu.addAction(action)

    def set_theme(self, theme_name: str, save=True):
        if self.apply_theme_callback:
            self.apply_theme_callback(theme_name)
        if save:
            self.config["theme"] = theme_name
            save_user_config(self.config)

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

        self.mem_label.setText(f"Memory: {mem_gb:.2f} GB")

        if asizeof and self.mem_popup.isVisible():
            self.update_mem_pop_text()

    def get_tab_memory_usage(self):
        if self.parent_window is None:
            return "No parent window found."

        components = {
            "FolderTreeWidget": getattr(self.parent_window, "folder_tree", None),
            "YamlViewer": getattr(self.parent_window.right_tabs, "config_yaml_tab", None),
            "KwargsJsonViewer": getattr(self.parent_window.right_tabs, "kwargs_json_tab", None),
            "InstrumentParamsViewer": getattr(self.parent_window.right_tabs, "instruments_tab", None),
            "LogViewer": getattr(self.parent_window.right_tabs, "log_tab", None),
            "FigureDisplayWidget": getattr(self.parent_window, "figure_display", None),
            "| - LivePlotWidget": getattr(self.parent_window.figure_display, "live_plot", None),
            "\nTotal Tracked Memory": self # cause we gather all the objects here
        }

        result_lines = ["--- Python Object Mem Usage ---\n"]
        for name, obj in components.items():
            if obj is None:
                result_lines.append(f"{name}: (not found)")
                continue

            try:
                size_bytes = asizeof.asizeof(obj)
                size_mb = size_bytes / (1024 ** 2)
                result_lines.append(f"{name}: {size_mb:.2f} MB")
            except Exception as e:
                result_lines.append(f"{name}: (Error: {e})")

        return "\n".join(result_lines)

    def update_mem_pop_text(self):

        # Count number of live QObjects
        num_qobjects = count_qobjects()

        # Count total python object size
        py_mem_msg = self.get_tab_memory_usage()

        message = f"--- Numer of Qt QObjects : {num_qobjects} ---\n\n"
        message += py_mem_msg
        if len(message) > 3000:
            message = message[:3000] + "\n... (truncated)"
        self.mem_textbox.setText(message)


    def toggle_memory_popup(self):
        if not asizeof:
            return

        if self.mem_popup.isVisible():
            self.mem_popup.hide()
        else:
            # Update text
            self.update_mem_pop_text()

            # Position below the mem_label
            global_pos = self.mapToGlobal(self.rect().bottomRight())
            self.mem_popup.move(global_pos.x()-self.mem_popup.sizeHint().width(), global_pos.y())
            self.mem_popup.adjustSize()
            self.mem_popup.show()


    def toggle_log_window(self,checked):
        if self.parent_window:
            self.parent_window.log_window.setVisible(checked)

    def toggle_right_panel(self, checked):
        if self.parent_window:
            self.parent_window.right_tabs.setVisible(checked)
            self.toggle_right_tabs_button.setToolTip("Show right panel" if checked else "Hide right panel")
            self.toggle_right_tabs_button.setChecked(not checked) # also change the button looking when checked in menu
            self.rt_info_action.setChecked(checked)


    def _make_view_action(self, name, toggle_action, init_checked=True):
        action = QAction(name, self)
        action.setCheckable(True)
        action.setChecked(init_checked)
        self.view_menu.addAction(action)
        action.toggled.connect(toggle_action)
        return action

    def set_view_actions(self):
        self.show_log_action = self._make_view_action("GUI Log", self.toggle_log_window, init_checked=False)
        self.rt_info_action = self._make_view_action("Runtime Info", self.toggle_right_panel)
        self.set_scaling_action = QAction("Set GUI Scaling...", self)
        self.set_scaling_action.triggered.connect(self.change_gui_scaling)
        self.view_menu.addAction(self.set_scaling_action)


    def change_gui_scaling(self):
        current_scale = float(self.config.get("scale_factor", "1.0"))
        new_scale, ok = QInputDialog.getDouble(
            self, "Set GUI Scaling Factor",
            "Recommended values:\n1.0 for 1080p\n1.5 for 2K\n2.0 for 4K",
            value=current_scale, min=0.5, max=4.0, decimals=1
        )
        if ok:
            self.config["scale_factor"] = str(new_scale)
            save_user_config(self.config)
            QMessageBox.information(self, "Restart Required", "Restart the application to apply new scaling.\n\n"
                                    f"If the GUI becomes unusable, "
                                    f"manually edit the value of `scale_factor` in `{CONFIG_PATH}`")
