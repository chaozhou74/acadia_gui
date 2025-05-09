import os
import subprocess
from PyQt5.QtWidgets import (QWidget, QFileSystemModel, QTreeView, QVBoxLayout,
                             QPushButton, QFileDialog, QMenu, QApplication, QHBoxLayout)
from PyQt5.QtCore import Qt, QModelIndex, QDir, QUrl
from PyQt5.QtGui import QIcon, QDesktopServices


from acadia_gui.helpers import detect_platform, to_windows_path
from acadia_gui.icons import ICON_PATH


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

        self.recent_button = QPushButton(QIcon(os.path.join(ICON_PATH, "most_recent_folder.svg")), "")
        self.recent_button.setToolTip("Select most recent data folder")
        self.recent_button.clicked.connect(self.select_most_recent_folder)

        self.sort_mtime_button = QPushButton(QIcon(os.path.join(ICON_PATH, "sort_by_time.svg")), "")
        self.sort_mtime_button.setToolTip("Sort folders by modification time")
        self.sort_mtime_button.clicked.connect(self.sort_by_mtime)
        self.current_sort_order = Qt.DescendingOrder



        button_row = QHBoxLayout()
        button_row.addWidget(self.select_button)
        button_row.addWidget(self.recent_button)
        button_row.addWidget(self.sort_mtime_button)

        layout = QVBoxLayout(self)
        layout.addLayout(button_row)
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

    def select_most_recent_folder(self):
        root_path = self.model.rootPath()
        most_recent_path = None
        latest_mtime = None

        for dirpath, dirnames, filenames in os.walk(root_path):
            if is_datafolder(dirpath):
                mtime = os.path.getmtime(dirpath)
                if most_recent_path is None or mtime > latest_mtime:
                    most_recent_path = dirpath
                    latest_mtime = mtime

        if most_recent_path:
            index = self.model.index(most_recent_path)
            if index.isValid():
                self.tree.setCurrentIndex(index)
                self.tree.scrollTo(index)
                self.on_select_callback(most_recent_path)

    def sort_by_mtime(self):
        self.tree.sortByColumn(3, self.current_sort_order)
        # Toggle the sort order for next time
        self.current_sort_order = (
            Qt.AscendingOrder if self.current_sort_order == Qt.DescendingOrder else Qt.DescendingOrder
        )
