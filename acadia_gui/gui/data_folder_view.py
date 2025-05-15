import os
import subprocess
import shutil
from functools import wraps
from PyQt5.QtWidgets import (QWidget, QFileSystemModel, QTreeView, QVBoxLayout,
                             QPushButton, QFileDialog, QMenu, QApplication, QHBoxLayout, QMessageBox)
from PyQt5.QtCore import Qt, QModelIndex, QDir, QUrl, QSortFilterProxyModel
from PyQt5.QtGui import QIcon, QDesktopServices


from acadia_gui.helpers import detect_platform, to_windows_path
from acadia_gui.icons import ICON_PATH


TRASH_FOLDER_NAME = "Trash"

def is_datafolder(path):
    """
    check if a path is a data folder by looking for "run.py" in the folder
    """
    return os.path.isfile(os.path.join(path, "run.py"))



class DataFolderModel(QFileSystemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.datafolder_icon = QIcon.fromTheme("document-open")
        self.trash_icon = QIcon.fromTheme("user-trash")  # Standard trash can icon

        if self.trash_icon.isNull():
            # fallback to a local icon
            self.trash_icon = QIcon(os.path.join(ICON_PATH, "trash_bin.svg"))  # <- your own icon


    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        path = self.filePath(index)
        base_name = os.path.basename(path)

        if role == Qt.DecorationRole and index.column() == 0:
            if base_name == TRASH_FOLDER_NAME:
                return self.trash_icon
            if is_datafolder(path):
                return self.datafolder_icon

        if role == Qt.ForegroundRole and index.column() == 0:
            if base_name == TRASH_FOLDER_NAME:
                from PyQt5.QtGui import QBrush, QColor
                return QBrush(QColor("#888888"))  # Greyed out color

        return super().data(index, role)

    def hasChildren(self, index: QModelIndex):
        path = self.filePath(index)
        if is_datafolder(path):
            return False  # Don't show expand arrow
        return super().hasChildren(index)

class DataFolderProxyModel(QSortFilterProxyModel):
    """
    For keeping the Trash folder always on top **within each directory**.
    Also fallback to mtime sort for others.
    """
    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        # Only customize if they have the same parent
        if left.parent() != right.parent():
            return super().lessThan(left, right)

        left_name = left.sibling(left.row(), 0).data()
        right_name = right.sibling(right.row(), 0).data()

        # Force Trash on top
        if left_name == TRASH_FOLDER_NAME and right_name != TRASH_FOLDER_NAME:
            return True
        if right_name == TRASH_FOLDER_NAME and left_name != TRASH_FOLDER_NAME:
            return False

        # Otherwise: sort by Date Modified (column 3)
        left_mtime = left.sibling(left.row(), 3).data()
        right_mtime = right.sibling(right.row(), 3).data()

        if left_mtime is None or right_mtime is None:
            return super().lessThan(left, right)

        return left_mtime < right_mtime

def update_explorer(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        ret = func(self, *args, **kwargs)
        self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(self.model.rootPath())))
        return ret
    return wrapper

class FolderTreeWidget(QWidget):
    def __init__(self, root_path, on_select_callback):
        super().__init__()
        self.root_path = root_path
        self.on_select_callback = on_select_callback

        self.model = DataFolderModel()
        self.model.setRootPath(root_path)
        self.model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)

        self.proxy_model = DataFolderProxyModel()
        self.proxy_model.setSourceModel(self.model)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy_model)
        self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(root_path)))
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
        # self.model.directoryLoaded.connect(lambda _: self.tree.sortByColumn(3, Qt.DescendingOrder))


    def source_path_from_proxy_index(self, proxy_index: QModelIndex) -> str:
        source_index = self.proxy_model.mapToSource(proxy_index)
        return self.model.filePath(source_index)

    def folder_selected(self, index: QModelIndex):
        path = self.source_path_from_proxy_index(index)
        self.on_select_callback(path)

    def select_new_root(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        new_root = QFileDialog.getExistingDirectory(self, "Select Root Directory", self.root_path, options=options)
        self.set_root_path(new_root)

    def set_root_path(self, root_path):
        if os.path.isdir(root_path):
            self.model.setRootPath(root_path)
            self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(root_path)))
            self.root_path = root_path

    def open_context_menu(self, position):
        index = self.tree.indexAt(position)
        if not index.isValid():
            return

        path = self.source_path_from_proxy_index(index)
        menu = self.get_context_actions(path)
        action = menu.exec_(self.tree.viewport().mapToGlobal(position))

        if action:
            self.handle_context_action(action.text(), path)

    def get_context_actions(self, path):
        parent_dir = os.path.dirname(path)
        is_in_trash = os.path.basename(parent_dir) == TRASH_FOLDER_NAME
        is_trash = os.path.basename(path) == TRASH_FOLDER_NAME

        menu = QMenu()
        menu.addAction("Open in File Explorer")
        menu.addAction("Copy Path")

        if is_in_trash:
            menu.addAction("Restore")
        elif is_trash:
            menu.addAction("Empty")
        else:
            menu.addAction("Trash")

        if not is_datafolder(path):
            menu.addAction("Set as Root")

        return menu

    def handle_context_action(self, action_text, path):
        if action_text == "Open in File Explorer":
            self.open_in_file_explorer(path)

        elif action_text == "Copy Path":
            QApplication.clipboard().setText(path)

        elif action_text == "Trash":
            self.handle_trash(path)

        elif action_text == "Restore":
            self.handle_restore(path)

        elif action_text == "Empty":
            self.handle_empty_trash(path)

        elif action_text == "Set as Root":
            self.set_root_path(path)


    def open_in_file_explorer(self, path):
        if detect_platform() == "wsl":
            try:
                win_path = to_windows_path(path)
                subprocess.Popen(["explorer.exe", win_path])
            except Exception as e:
                print(f"Failed to open path in Explorer: {e}")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @update_explorer
    def handle_trash(self, path):
        parent_dir = os.path.dirname(path)
        trash_dir = os.path.join(parent_dir, TRASH_FOLDER_NAME)
        os.makedirs(trash_dir, exist_ok=True)
        try:
            shutil.move(path, trash_dir)
            print(f"Moved {path} to {trash_dir}")
        except Exception as e:
            print(f"Failed to move {path} to trash: {e}")

    @update_explorer
    def handle_restore(self, path):
        parent_dir = os.path.dirname(path)  # trash/
        original_dir = os.path.dirname(parent_dir)  # where it came from
        base_name = os.path.basename(path)
        restore_path = os.path.join(original_dir, base_name)

        if os.path.exists(restore_path):
            QMessageBox.warning(self, "Restore Failed",
                                f"A folder named '{base_name}' already exists in the original location.")
            return

        try:
            shutil.move(path, restore_path)
            print(f"Restored {path} to {restore_path}")
        except Exception as e:
            print(f"Failed to restore {path}: {e}")

    @update_explorer
    def handle_empty_trash(self, path):
        if not os.path.isdir(path):
            print(f"Path {path} is not a directory")
            return

        # Count how many folders inside trash
        folders = [name for name in os.listdir(path) if os.path.isdir(os.path.join(path, name))]
        num_folders = len(folders)

        reply = QMessageBox.question(
            self,
            "Empty Trash",
            f"Are you sure you want to permanently delete {num_folders} data folders from trash?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            try:
                shutil.rmtree(path)
                print(f"Deleted trash folder {path}")
            except Exception as e:
                print(f"Failed to delete trash folder {path}: {e}")

    def select_most_recent_folder(self):
        # todo: the sorting can be optimized, kind of slow currently
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
            source_index = self.model.index(most_recent_path)
            proxy_index = self.proxy_model.mapFromSource(source_index)
            if proxy_index.isValid():
                self.tree.setCurrentIndex(proxy_index)
                self.tree.scrollTo(proxy_index)
                self.on_select_callback(most_recent_path)

    def sort_by_mtime(self):
        self.tree.sortByColumn(3, self.current_sort_order)
        # Toggle the sort order for next time
        self.current_sort_order = (
            Qt.AscendingOrder if self.current_sort_order == Qt.DescendingOrder else Qt.DescendingOrder
        )
