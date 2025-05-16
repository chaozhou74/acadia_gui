import os
import json
from PyQt5.QtWidgets import QTabWidget, QTextBrowser


def find_yaml_files(path):
    return sorted([f for f in os.listdir(path) if f.lower().endswith(('.yaml', '.yml'))])


class YamlViewer(QTabWidget):
    def __init__(self, highlight_key=None):
        super().__init__()
        self.highlight_key = highlight_key
        self.setStyleSheet("QTextBrowser { font-family: monospace; }")

    def load_yaml_files(self, folder_path):
        self.clear()
        yaml_files = find_yaml_files(folder_path)

        # Load yaml_key_index.json
        index_path = os.path.join(folder_path, "yaml_key_index.json")
        try:
            with open(index_path, "r") as f:
                yaml_key_index = json.load(f)
        except Exception:
            yaml_key_index = {}
        # Build inverse index: {filename: {yaml_key: alias}}
        inverse_index = {}
        for alias, (fname_, key) in yaml_key_index.items():
            inverse_index.setdefault(fname_, {})[key] = alias

        if not yaml_files:
            browser = QTextBrowser()
            browser.setPlainText("No YAML files found")
            self.addTab(browser, "No Config")
            return

        for fname in yaml_files:
            full_path = os.path.join(folder_path, fname)
            try:
                with open(full_path, 'r') as f:
                    lines = f.readlines()
            except Exception as e:
                lines = [f"Failed to read {fname}:\n{e}"]

            sections = self._split_into_sections(lines)

            html_lines = []
            # Insert CSS style block at top
            html_lines.append("<style>.hover-highlight:hover { background-color: #ffffcc; }</style>")

            for key, content_lines in sections.items():
                joined = "".join(content_lines)
                alias = inverse_index.get(fname, {}).get(key)

                if alias:
                    wrapped = (
                        f'<span title="{alias}" class="hover-highlight">{joined}</span>'
                    )
                else:
                    wrapped = f'<span style="color: #999;">{joined}</span>'

                html_lines.append(wrapped)

            html = "<pre>" + "".join(html_lines) + "</pre>"

            browser = QTextBrowser()
            browser.setHtml(html)
            self.addTab(browser, fname)

    def _split_into_sections(self, lines):
        """
        Splits YAML lines into top-level sections by detecting keys with no indentation.
        """
        sections = {}
        current_key = None
        current_lines = []

        for line in lines:
            if line.strip() == "":
                if current_key:
                    current_lines.append(line)
                continue

            indent = len(line) - len(line.lstrip())
            if indent == 0 and ':' in line:
                if current_key:
                    sections[current_key] = current_lines
                current_key = line.strip().split(":")[0]
                current_lines = [line]
            else:
                if current_key:
                    current_lines.append(line)

        if current_key:
            sections[current_key] = current_lines

        return sections

    def clear(self):
        while self.count():
            widget = self.widget(0)
            if widget:
                widget.deleteLater()
            self.removeTab(0)