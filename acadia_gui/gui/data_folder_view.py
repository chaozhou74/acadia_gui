import os
import subprocess
from PyQt5.QtWidgets import (QWidget, QFileSystemModel, QTreeView, QVBoxLayout,
                             QPushButton, QFileDialog, QMenu, QApplication)
from PyQt5.QtCore import Qt, QModelIndex, QDir, QUrl
from PyQt5.QtGui import QIcon, QDesktopServices

from acadia_gui.helpers import detect_platform, to_windows_path


def is_datafolder(path):
    """
    check if a path is a data folder by looking for "run.py" in the folder
    """
    return os.path.isfile(os.path.join(path, "run.py"))


class DataFolderModel(QFileSystemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.datafolder_icon = QIcon.fromTheme("document-open")  # or a custom icon

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if role == Qt.DecorationRole and index.column() == 0:
            path = self.filePath(index)
            if is_datafolder(path):
                return self.datafolder_icon
        return super().data(index, role)

    def hasChildren(self, index: QModelIndex):
        path = self.filePath(index)
        if is_datafolder(path):
            return False  # Don't show expand arrow
        return super().hasChildren(index)


class FolderTreeWidget(QWidget):
    def __init__(self, root_path, on_select_callback):
        super().__init__()
        self.on_select_callback = on_select_callback

        self.model = DataFolderModel()
        self.model.setRootPath(root_path)
        self.model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)

        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIndex(self.model.index(root_path))
        self.tree.clicked.connect(self.folder_selected)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(3, Qt.DescendingOrder)  # Column 3 = "Date Modified"

        self.tree.hideColumn(1)  # Size
        self.tree.hideColumn(2)  # Type
        self.tree.hideColumn(3)  # Time Modified

        self.select_button = QPushButton("Select Root Folder")
        self.select_button.clicked.connect(self.select_new_root)

        layout = QVBoxLayout(self)
        layout.addWidget(self.select_button)
        layout.addWidget(self.tree)
        self.setLayout(layout)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_context_menu)
        self.model.directoryLoaded.connect(lambda _: self.tree.sortByColumn(3, Qt.DescendingOrder))

    def folder_selected(self, index: QModelIndex):
        path = self.model.filePath(index)
        self.on_select_callback(path)

    def select_new_root(self):
        new_root = QFileDialog.getExistingDirectory(self, "Select Root Directory", os.getcwd())
        if new_root:
            self.model.setRootPath(new_root)
            self.tree.setRootIndex(self.model.index(new_root))

    def open_context_menu(self, position):
        index = self.tree.indexAt(position)
        if not index.isValid():
            return

        path = self.model.filePath(index)

        menu = QMenu()
        open_folder_action = menu.addAction("Open in File Explorer")
        copy_path_action = menu.addAction("Copy Path")
        action = menu.exec_(self.tree.viewport().mapToGlobal(position))

        if action == open_folder_action:
            if detect_platform() == "wsl":
                try:
                    win_path = to_windows_path(path)
                    subprocess.Popen(["explorer.exe", win_path])
                except Exception as e:
                    print(f"Failed to open path in Explorer: {e}")
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        elif action == copy_path_action:
            clipboard = QApplication.clipboard()
            clipboard.setText(path)
