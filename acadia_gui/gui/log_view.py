import os
import re
import logging
from PyQt5.QtWidgets import QTabWidget, QTextBrowser
from PyQt5.QtGui import QTextCharFormat, QColor, QFont
from PyQt5.QtCore import QTimer

QUOTED_PATTERN = re.compile(r"'[^']*'")
FLOAT_PATTERN = re.compile(r"""(?x)(?<!\w)[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?(?![\w.])""")
TIMESTAMP_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\]")

logger = logging.getLogger(__name__)

log_files = ["remote_main.log", "runtime.log", "remote_stderr.log", "remote_stdout.log"]

def find_log_files(path):
    logs = [f for f in log_files if f in os.listdir(path)]
    return logs


class LogViewer(QTabWidget):
    def __init__(self):
        super().__init__()
        self.folder_path = None
        self.file_mtimes = {}
        self.timer = QTimer()
        self.timer.timeout.connect(self.check_for_updates)
        self.timer.start(1000)  # Check every 1 second

    def load_logs(self, folder_path):
        self.timer.stop()
        self.clear()
        self.folder_path = folder_path
        self.file_mtimes = {}

        log_files = find_log_files(folder_path)

        if not log_files:
            browser = QTextBrowser()
            browser.setPlainText("No log files found.")
            self.addTab(browser, "Logs")
        else:
            for fname in log_files:
                self._add_log_tab(fname)

        self.timer.start(1000)

    def _add_log_tab(self, fname):
        full_path = os.path.join(self.folder_path, fname)
        browser = QTextBrowser()
        browser.setReadOnly(True)

        try:
            with open(full_path, "r") as f:
                lines = f.readlines()
            self._insert_with_formatting(browser, lines)
        except Exception as e:
            browser.setPlainText(f"Error reading {fname}:\n{e}")
            logger.error(e, exc_info=True)
        self.file_mtimes[fname] = os.path.getmtime(full_path)
        self.addTab(browser, fname)

    def _insert_with_formatting(self, browser, lines):
        browser.clear()
        cursor = browser.textCursor()
        font = QFont("Courier New", 10)
        browser.setFont(font)

        blocks = []
        current_block = []

        for line in lines:
            if TIMESTAMP_PATTERN.match(line) and current_block:
                blocks.append(current_block)
                current_block = [line]
            else:
                current_block.append(line)
        if current_block:
            blocks.append(current_block)

        for block in blocks:
            block_text = ''.join(block)

            # === Base format per block ===
            base_fmt = QTextCharFormat()

            if "ERROR" in block_text:
                base_fmt.setForeground(QColor("#ff5555"))
                base_fmt.setFontWeight(QFont.Bold)
            elif "WARNING" in block_text:
                base_fmt.setForeground(QColor("#ffaa00"))
                base_fmt.setFontItalic(True)
            elif "DEBUG" in block_text:
                base_fmt.setForeground(QColor("#559999"))

            for line in block:
                tokens = []
                last_index = 0

                # Timestamp
                ts_match = TIMESTAMP_PATTERN.search(line)
                if ts_match:
                    start, end = ts_match.span()
                    if start > last_index:
                        tokens.append((line[last_index:start], base_fmt))
                    ts_fmt = QTextCharFormat()
                    ts_fmt.setForeground(QColor("#777777"))
                    tokens.append((ts_match.group(), ts_fmt))
                    last_index = end

                # Floats and quoted strings
                remaining = line[last_index:]
                rel_index = 0
                for match in sorted(
                        list(QUOTED_PATTERN.finditer(remaining)) + list(FLOAT_PATTERN.finditer(remaining)),
                        key=lambda m: m.start()
                ):
                    if match.start() > rel_index:
                        tokens.append((remaining[rel_index:match.start()], base_fmt))

                    token_text = match.group()
                    token_fmt = QTextCharFormat(base_fmt)
                    if QUOTED_PATTERN.fullmatch(token_text):
                        token_fmt.setForeground(QColor("#c586c0"))
                    elif FLOAT_PATTERN.fullmatch(token_text):
                        token_fmt.setForeground(QColor("#4fc1ff"))

                    tokens.append((token_text, token_fmt))
                    rel_index = match.end()

                if rel_index < len(remaining):
                    tokens.append((remaining[rel_index:], base_fmt))

                for text, fmt in tokens:
                    cursor.insertText(text, fmt)

        browser.moveCursor(cursor.End)

    def reload_tab(self, index):
        fname = self.tabText(index)
        browser = self.widget(index)
        full_path = os.path.join(self.folder_path, fname)

        try:
            with open(full_path, "r") as f:
                lines = f.readlines()
            browser.clear()
            self._insert_with_formatting(browser, lines)
        except Exception as e:
            browser.setPlainText(f"Error reading {fname}:\n{e}")
            logger.error(e, exc_info=True)

        self.file_mtimes[fname] = os.path.getmtime(full_path)

    def check_for_updates(self):
        if not self.folder_path:
            return
        for i in range(self.count()):
            fname = self.tabText(i)
            full_path = os.path.join(self.folder_path, fname)
            try:
                current_mtime = os.path.getmtime(full_path)
                if fname not in self.file_mtimes or self.file_mtimes[fname] != current_mtime:
                    self.file_mtimes[fname] = current_mtime
                    self.reload_tab(i)
            except FileNotFoundError:
                continue

    def clear(self):
        while self.count():
            widget = self.widget(0)
            if widget:
                widget.deleteLater()
            self.removeTab(0)

