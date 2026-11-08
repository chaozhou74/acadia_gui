import logging
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton, QHBoxLayout, QCheckBox
from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtGui import QTextCharFormat, QColor, QFont

# level of the messages about instrument parameters loaded from a run, shown in their own color
INSTRUMENT_LOG_LEVEL = 25
logging.addLevelName(INSTRUMENT_LOG_LEVEL, "INSTRUMENT")

class LogSignalEmitter(QObject):
    new_log = pyqtSignal(tuple)  # (msg: str, levelno: int, runs: [(text, style)] or None)

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
        self.allowed_levels = {logging.DEBUG, logging.INFO, INSTRUMENT_LOG_LEVEL, logging.WARNING,
                               logging.ERROR, logging.CRITICAL}

    def emit(self, record):
        if record.levelno not in self.allowed_levels:
            return
        msg = self.format(record)
        # a record can style parts of its message: extra={"gui_runs": [(text, style), ...]}, where
        # style is a dict with optional "italic", "bold" (bools) and "color"
        runs = getattr(record, "gui_runs", None)
        if runs:
            head, _, tail = msg.partition(record.getMessage())
            runs = [(head, {}), *runs, (tail, {})]
        self.emitter.new_log.emit((msg, record.levelno, runs))

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
        self.checkbox_instrument = QCheckBox("Instrument")
        # Default to all checked except for debug
        for cb in (self.checkbox_debug, self.checkbox_info, self.checkbox_warning, self.checkbox_error,
                   self.checkbox_instrument):
            cb.setChecked(True)
            cb.stateChanged.connect(self.update_filter)
        self.checkbox_debug.setChecked(False)

        control_col = QVBoxLayout()
        control_col.addWidget(self.checkbox_debug)
        control_col.addWidget(self.checkbox_info)
        control_col.addWidget(self.checkbox_warning)
        control_col.addWidget(self.checkbox_error)
        control_col.addWidget(self.checkbox_instrument)

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
        self.update_filter()


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
            if self.checkbox_instrument.isChecked():
                levels.add(INSTRUMENT_LOG_LEVEL)
            self.handler.set_allowed_levels(levels)


    def append_log(self, msg_level_tuple):
        text, levelno, runs = msg_level_tuple
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
        elif levelno == INSTRUMENT_LOG_LEVEL:
            fmt.setForeground(QColor("#9467d6"))
        elif (levelno >= logging.DEBUG) and (levelno < logging.INFO):
            fmt.setForeground(QColor("#559999"))

        cursor.movePosition(cursor.End)
        for part, style in runs or [(text, {})]:
            part_fmt = QTextCharFormat(fmt)
            if style.get("italic"):
                part_fmt.setFontItalic(True)
            if style.get("bold"):
                part_fmt.setFontWeight(QFont.Bold)
            if style.get("color"):
                part_fmt.setForeground(QColor(style["color"]))
            cursor.insertText(part, part_fmt)
        cursor.insertText("\n", fmt)
        self.log_box.setTextCursor(cursor)
        self.log_box.ensureCursorVisible()

