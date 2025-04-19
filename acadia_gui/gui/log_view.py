import os
from PyQt5.QtWidgets import QTabWidget, QTextBrowser
from PyQt5.QtGui import QTextCharFormat, QColor


log_files = ["remote_main.log", "runtime.log", "remote_stderr.log", "remote_stdout.log"]

def find_log_files(path):
    logs = [f for f in log_files if f in os.listdir(path)]
    return logs


class LogViewer(QTabWidget):
    def __init__(self):
        super().__init__()

    def load_logs(self, folder_path):
        self.clear()
        log_files = find_log_files(folder_path)

        if not log_files:
            browser = QTextBrowser()
            browser.setPlainText("No log files found.")
            self.addTab(browser, "Logs")
            return

        for fname in log_files:
            full_path = os.path.join(folder_path, fname)

            browser = QTextBrowser()
            browser.setReadOnly(True)
            browser.setStyleSheet("QTextBrowser { font-family: monospace; font-size: 10pt; }")

            try:
                with open(full_path, "r") as f:
                    lines = f.readlines()
                self._insert_with_formatting(browser, lines)
            except Exception as e:
                browser.setPlainText(f"Error reading {fname}:\n{e}")

            self.addTab(browser, fname)

    def _insert_with_formatting(self, browser, lines):
        cursor = browser.textCursor()
        for line in lines:
            fmt = QTextCharFormat()

            if "ERROR" in line:
                fmt.setForeground(QColor("#ff5555"))
                fmt.setFontWeight(600)
            elif "WARNING" in line:
                fmt.setForeground(QColor("#ffaa00"))
                fmt.setFontItalic(True)
            # elif "INFO" in line:
            #     fmt.setForeground(QColor("#aaaaaa"))
            # else:
            #     fmt.setForeground(QColor("#000000"))

            cursor.insertText(line, fmt)
