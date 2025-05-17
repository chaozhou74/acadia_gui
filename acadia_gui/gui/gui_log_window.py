import logging
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout, QCheckBox
from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtGui import QTextCharFormat, QColor

class LogSignalEmitter(QObject):
    new_log = pyqtSignal(tuple)  # (msg: str, levelno: int)

class GuiLogHandler(logging.Handler):
    def __init__(self, log_window):
        super().__init__()
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s (%(filename)s:%(lineno)d)",
            datefmt="%y/%m/%d-%H:%M:%S"
        )
        self.setFormatter(formatter)
        self.log_window = log_window
        self.emitter = LogSignalEmitter()
        self.emitter.new_log.connect(self.log_window.append_log)
        self.allowed_levels = {logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL}

    def emit(self, record):
        if record.levelno not in self.allowed_levels:
            return
        msg = self.format(record)
        self.emitter.new_log.emit((msg, record.levelno))

    def set_allowed_levels(self, levels):
        self.allowed_levels = levels


class GuiLogWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Live Log Output")
        self.handler: GuiLogHandler = None  # will be set externally

        layout = QHBoxLayout(self)
        self.log_box = QTextEdit(self)
        self.log_box.setReadOnly(True)


        # Filter checkboxes
        self.checkbox_debug = QCheckBox("Debug")
        self.checkbox_info = QCheckBox("Info")
        self.checkbox_warning = QCheckBox("Warning")
        self.checkbox_error = QCheckBox("Error")
        # Default to all checked except for debug
        for cb in (self.checkbox_debug, self.checkbox_info, self.checkbox_warning, self.checkbox_error):
            cb.setChecked(True)
            cb.stateChanged.connect(self.update_filter)
        self.checkbox_debug.setChecked(False)

        control_col = QVBoxLayout()
        control_col.addWidget(self.checkbox_debug)
        control_col.addWidget(self.checkbox_info)
        control_col.addWidget(self.checkbox_warning)
        control_col.addWidget(self.checkbox_error)

        # clear button
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.log_box.clear)
        control_col.addWidget(clear_button)

        control_widgets = QWidget()
        control_widgets.setLayout(control_col)

        layout.addWidget(self.log_box)
        layout.addWidget(control_widgets)

    def set_handler(self, handler: GuiLogHandler):
        self.handler = handler


    def update_filter(self):
        if self.handler:
            levels = set()
            if self.checkbox_debug.isChecked():
                levels.add(logging.DEBUG)
            if self.checkbox_info.isChecked():
                levels.add(logging.INFO)
            if self.checkbox_warning.isChecked():
                levels.add(logging.WARNING)
            if self.checkbox_error.isChecked():
                levels.add(logging.ERROR)
                levels.add(logging.CRITICAL)  # always show CRITICAL if error is on
            self.handler.set_allowed_levels(levels)


    def append_log(self, msg_level_tuple):
        text, levelno = msg_level_tuple
        cursor = self.log_box.textCursor()
        fmt = QTextCharFormat()

        if levelno >= logging.CRITICAL:
            fmt.setForeground(QColor("#ff2e2e"))
            fmt.setFontWeight(800)
        elif levelno >= logging.ERROR:
            fmt.setForeground(QColor("#ff5555"))
            fmt.setFontWeight(600)
        elif levelno >= logging.WARNING:
            fmt.setForeground(QColor("#ffaa00"))
            fmt.setFontItalic(True)
        elif (levelno >= logging.DEBUG) and (levelno < logging.INFO):
            fmt.setForeground(QColor("#559999"))

        cursor.movePosition(cursor.End)
        cursor.insertText(text + "\n", fmt)
        self.log_box.setTextCursor(cursor)
        self.log_box.ensureCursorVisible()

