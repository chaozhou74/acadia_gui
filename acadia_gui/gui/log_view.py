import os
from PyQt5.QtWidgets import QTabWidget, QTextBrowser
from PyQt5.QtGui import QTextCharFormat, QColor
from PyQt5.QtCore import QTimer

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
        self.clear()
        self.folder_path = folder_path
        log_files = find_log_files(folder_path)

        if not log_files:
            browser = QTextBrowser()
            browser.setPlainText("No log files found.")
            self.addTab(browser, "Logs")
            return

        for fname in log_files:
            self._add_log_tab(fname)

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
        self.file_mtimes[fname] = os.path.getmtime(full_path)
        self.addTab(browser, fname)

    def _insert_with_formatting(self, browser, lines):
        browser.clear()
        cursor = browser.textCursor()
        for line in lines:
            fmt = QTextCharFormat()

            if "ERROR" in line:
                fmt.setForeground(QColor("#ff5555"))
                fmt.setFontWeight(600)
            elif "WARNING" in line:
                fmt.setForeground(QColor("#ffaa00"))
                fmt.setFontItalic(True)
            elif "DEBUG" in line:
                fmt.setForeground(QColor("#559999"))
            # else:
            #     fmt.setForeground(QColor("#000000"))

            cursor.insertText(line, fmt)

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
