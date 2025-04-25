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
from acadia.runtime import Runtime

KWARGS_JSON_FILE = "kwargs.json"


# todo: this should actually be the base of all json file viewer

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
                data = Runtime._untransform_arg(data)
                self._populate_tree(data)
            except Exception as e:
                self.tree.addTopLevelItem(QTreeWidgetItem(["Error", str(e)]))

    def _populate_tree(self, obj, parent=None):
        """Recursively populate the tree with dictionary/list items."""
        try:
            if parent is None:
                parent = self.tree.invisibleRootItem()

            if isinstance(obj, dict):
                for key, val in obj.items():
                    item = QTreeWidgetItem([str(key)])
                    item.setData(0, Qt.UserRole, val)  # <-- store object here
                    parent.addChild(item)
                    self._populate_tree(val, item)

            elif isinstance(obj, (list, np.ndarray, tuple)):
                parent.setText(1, str(obj))
                parent.setData(0, Qt.UserRole, obj)  # <-- also store array or list here
                for idx, val in enumerate(obj):
                    item = QTreeWidgetItem([f"[{idx}]", self._list_summary(val)])
                    item.setData(0, Qt.UserRole, val)
                    parent.addChild(item)
                    self._populate_tree(val, item)
            else:
                parent.setText(1, str(obj))
                parent.setData(0, Qt.UserRole, obj)

        except Exception as e:
            parent.setText(1, str(e))

    def _list_summary(self, val):
        """Show summary for nested structures."""
        flat = np.asarray(val).flat
        if len(flat) > 1:
            show_val = f"{val.__class__.__name__}; [{flat[0]:.4e}, ... ,{flat[-1]:.4e}]; {np.asarray(val).shape}"
        else:
            show_val = str(val)
        return show_val


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
        obj = selected.data(0, Qt.UserRole)

        # add copy option
        copy_action = menu.addAction("Copy Value")

        # add plot option for ndarray
        if isinstance(obj, np.ndarray):
            plot_action = menu.addAction("Plot ndarray")

        # get action
        action = menu.exec_(self.tree.viewport().mapToGlobal(position))
        if action is None:
            return

        # copy action
        if action == copy_action:
            QApplication.clipboard().setText(str(obj))
            self._flash_item(selected)

        # plot action
        elif action == plot_action:
            try:
                self._plot_array(selected.text(0), obj)
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


    def _plot_array(self, name, arr):
        """Plot NumPy array in a new window."""
        plt.figure("ndarray plot")
        if np.iscomplexobj(arr):
            plt.plot(arr.real, marker='o', linestyle='-', label='Real')
            plt.plot(arr.imag, marker='x', linestyle='--', label='Imag')
            plt.title("Complex ndarray: Real and Imag parts")
            plt.legend()
        else:
            plt.plot(arr, marker='o', linestyle='-')

        plt.title(name)
        plt.xlabel("Index")
        plt.ylabel("Value")
        plt.grid(True)
        plt.show()

    def clear(self):
        """Clear tree and internal data to avoid stale references."""
        self.tree.clear()
        self.json_path = None