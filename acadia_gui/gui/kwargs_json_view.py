import os
import json
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem,
    QMenu, QApplication
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence, QColor, QBrush
import numpy as np
import matplotlib.pyplot as plt
import binascii

KWARGS_JSON_FILE = "kwargs.json"


# todo: use runtime._untransform_arg at the beginning

class KwargsJsonViewer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.layout = QVBoxLayout(self)
        self.setLayout(self.layout)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Key", "Value"])
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)

        self.layout.addWidget(QLabel(KWARGS_JSON_FILE))
        self.layout.addWidget(self.tree)

        self.json_path = None

    def load_json(self, folder_path):
        """Load `kwargs.json` from the provided folder path and populate the tree."""
        self.tree.clear()
        self.json_path = os.path.join(folder_path, KWARGS_JSON_FILE)
        if not os.path.exists(self.json_path):
            self.tree.addTopLevelItem(QTreeWidgetItem(["Error", f"{KWARGS_JSON_FILE} not found"]))
            return

        with open(self.json_path, "r") as f:
            try:
                data = json.load(f)
                self._populate_tree(data)
            except Exception as e:
                self.tree.addTopLevelItem(QTreeWidgetItem(["Error", str(e)]))

    def _populate_tree(self, obj, parent=None):
        """Recursively populate the tree with dictionary/list items."""
        if parent is None:
            parent = self.tree.invisibleRootItem()

        if isinstance(obj, dict):
            for key, val in obj.items():
                item = QTreeWidgetItem([str(key), self._summary(val)])
                parent.addChild(item)
                self._populate_tree(val, item)
        elif isinstance(obj, list):
            for idx, val in enumerate(obj):
                item = QTreeWidgetItem([f"[{idx}]", self._summary(val)])
                parent.addChild(item)
                self._populate_tree(val, item)

    def _summary(self, val):
        """Show summary for nested structures."""
        if isinstance(val, dict):
            return f"dict ({len(val)} keys)"
        elif isinstance(val, list):
            return f"list ({len(val)} items)"
        return str(val)

    def keyPressEvent(self, event):
        """Ctrl+C copies the value column and flashes the row."""
        if event.matches(QKeySequence.Copy):
            selected = self.tree.currentItem()
            if selected:
                value = selected.text(1)
                QApplication.clipboard().setText(value)
                self._flash_item(selected)
        else:
            super().keyPressEvent(event)


    def show_context_menu(self, position):
        """Show right-click menu for copying or plotting if ndarray."""
        selected = self.tree.itemAt(position)
        if not selected:
            return

        menu = QMenu()
        value = selected.text(1)

        copy_action = menu.addAction("Copy Value")

        plot_action = None
        if value.startswith("ndarray;"):
            plot_action = menu.addAction("Plot ndarray")

        action = menu.exec_(self.tree.viewport().mapToGlobal(position))

        if action == copy_action:
            QApplication.clipboard().setText(value)
            self._flash_item(selected)

        elif action == plot_action:
            try:
                arr = self._parse_ndarray(value)
                if arr is not None:
                    self._plot_array(arr)
            except Exception as e:
                print(f"Failed to plot ndarray: {e}")

    def _flash_item(self, item):
        """Flash the value cell background by temporarily deselecting and reselecting."""
        col = 1
        flash_bg = QBrush(QColor("#A5D6A7"))  # Light green
        original_bg = item.background(col)

        # Temporarily deselect so flash is visible
        self.tree.setCurrentItem(None)
        item.setBackground(col, flash_bg)

        def restore():
            item.setBackground(col, original_bg)
            self.tree.setCurrentItem(item)  # Restore selection

        QTimer.singleShot(200, restore)


    def _parse_ndarray(self, value_str):
        """Decode ndarray;... to a NumPy array (assumes float32 little-endian)."""
        from acadia.runtime import Runtime
        arr =  Runtime._untransform_arg(value_str)
        print(arr)
        return arr


    def _plot_array(self, arr):
        """Plot NumPy array in a new window."""
        plt.figure("ndarray plot")
        plt.plot(arr, marker='o', linestyle='-')
        plt.title("ndarray content")
        plt.xlabel("Index")
        plt.ylabel("Value")
        plt.grid(True)
        plt.show()

