import os
import subprocess
import shutil
from functools import wraps
import logging
from threading import Event
import time
from PyQt5.QtWidgets import (QWidget, QFileSystemModel, QTreeView, QVBoxLayout, QSizePolicy, QLineEdit, QLabel,
                             QPushButton, QFileDialog, QMenu, QApplication, QHBoxLayout, QMessageBox)
from PyQt5.QtCore import Qt, QModelIndex, QDir, QUrl, QSortFilterProxyModel, QObject, pyqtSignal, QThread, QTimer
from PyQt5.QtGui import QIcon, QDesktopServices, QColor, QBrush


from acadia_qmsmt.utils.path_adapter import detect_platform, to_windows_path
from acadia_gui.icons import get_icon
from acadia_gui.utils import load_user_config, update_user_config


TRASH_FOLDER_NAME = "Trash"
DATAFOLDER_INDICATOR_FILE = "run.py" # indicates an Acadia data folder

logger = logging.getLogger(__name__)

def is_datafolder(path):
    """
    check if a path is a data folder by looking for "run.py" in the folder
    """
    return os.path.isfile(os.path.join(path, DATAFOLDER_INDICATOR_FILE))

class DataFolderModel(QFileSystemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.datafolder_icon = QIcon.fromTheme("document-open")
        self.trash_icon = QIcon.fromTheme("user-trash")  # Standard trash can icon

        if self.trash_icon.isNull():
            # fallback to a local icon
            self.trash_icon = get_icon("trash_bin.svg")


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

        left_name = (left.sibling(left.row(), 0).data() or "")
        right_name = (right.sibling(right.row(), 0).data() or "")

        # Force Trash on top
        if left_name == TRASH_FOLDER_NAME and right_name != TRASH_FOLDER_NAME:
            return True
        if right_name == TRASH_FOLDER_NAME and left_name != TRASH_FOLDER_NAME:
            return False

        # Check which column is being sorted
        sort_column = self.sortColumn()
        if sort_column == 0:
            return left_name.lower() < right_name.lower()  # Case-insensitive sort

        elif sort_column == 3:
            # Sort by Date Modified (column 3)
            left_mtime = self.sourceModel().fileInfo(left).lastModified()
            right_mtime = self.sourceModel().fileInfo(right).lastModified()

            if left_mtime is None or right_mtime is None:
                return super().lessThan(left, right)

            return left_mtime < right_mtime

        return super().lessThan(left, right)


def update_explorer_after_trash(func):
    @wraps(func)
    def wrapper(self, path):
        ret = func(self, path)
        # self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(self.model.rootPath())))
        self.proxy_model.invalidate()
        return ret
    return wrapper



class FolderMonitorWorker(QObject):
    new_datafolder_found = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, root_path, poll_interval=0.3, stat_batch=4000):
        """
        Polling monitor for new data folders.

        We are not using the Qt built-in monitor because it uses lazy loading,
        but we do want to monitor nested fooder changes as well.

        How this works:
        - Keep a list of known directories and their mtimes.
        - On each tick, stat a round-robin batch.
        - If a dir’s mtime changed, we fully walk that branch:
            - If we see a datafolder, emit once and prune its children.
            - Otherwise, add any new subdirs to tracking.

        :param root_path: root directory to monitor
        :param poll_interval: interval between scans, in seconds
        :param stat_batch: how many dirs to stat per tick
        """
        super().__init__()
        self.root_path = os.path.abspath(root_path)
        self.poll_interval = poll_interval
        self.stat_batch = stat_batch
        self._stop_event = Event()

        # Tracking
        self.dir_state = {}        # path -> {"mtime": float, "last_scan": float}
        self._dir_keys = []        # round-robin list of dirs to stat each tick
        self._dir_cursor = 0
        self.known_datafolders = set()

        self._index_full_branch(self.root_path, emit=False)  # build initial dir state

    def _index_full_branch(self, path, emit=False):
        """
        Stat a batch of dirs. If a dir changed, re-walk that branch (emit=True).
        """
        logger.debug(f"Inspecting existing data folders under {path}...")
        for dirpath, dirnames, _ in os.walk(path, topdown=True):
            if self._stop_event.is_set():
                dirnames[:] = []  # prune children
                break  # stop walking this branch

            # Skip Trash folders entirely
            if os.path.basename(dirpath) == TRASH_FOLDER_NAME:
                dirnames[:] = []  # prune children
                continue

            # If we already know it's a datafolder, prune subtree
            if dirpath in self.known_datafolders:
                dirnames[:] = []
                continue

            if is_datafolder(dirpath):
                if dirpath not in self.known_datafolders:
                    self.known_datafolders.add(dirpath)
                    if emit:
                        self.new_datafolder_found.emit(dirpath)
                dirnames[:] = []  # prune children of a datafolder
                continue

            # Track this directory
            try:
                st = os.stat(dirpath, follow_symlinks=False)
            except (FileNotFoundError, PermissionError):
                dirnames[:] = []
                continue

            if dirpath not in self.dir_state:
                self.dir_state[dirpath] = {"mtime": st.st_mtime, "last_scan": 0.0}
                self._dir_keys.append(dirpath)
            else:
                # keep mtime fresh if we walked due to a parent change
                self.dir_state[dirpath]["mtime"] = st.st_mtime
        logger.debug(f"Done inspecting data folders under {path}.")

    def _scan_changed_dirs(self):
        """
        Round-robin stat a batch. For any dir whose mtime changed,
        walk the FULL branch rooted at that dir (emit=True).
        """
        if not self._dir_keys:
            return

        n = len(self._dir_keys)
        end = self._dir_cursor + min(self.stat_batch, n)
        i = self._dir_cursor

        while i < end and not self._stop_event.is_set():
            d = self._dir_keys[i % n]
            st = self.dir_state.get(d)
            if st is None:
                i += 1
                continue

            try:
                s = os.stat(d, follow_symlinks=False)
                if st["mtime"] != s.st_mtime:
                    # parent changed -> fully (re)index the branch and emit any new datafolders
                    st["mtime"] = s.st_mtime
                    self._index_full_branch(d, emit=True)
                st["last_scan"] = time.time()
            except (FileNotFoundError, PermissionError):
                # dropped; remove from state (lazy compact of _dir_keys later)
                self.dir_state.pop(d, None)

            i += 1

        self._dir_cursor = (i % max(1, n))

        # Light compaction of _dir_keys if many paths were removed
        if len(self._dir_keys) > max(1, int(len(self.dir_state) * 1.2)):
            self._dir_keys = [k for k in self._dir_keys if k in self.dir_state]
            self._dir_cursor = (self._dir_cursor % len(self._dir_keys)) if self._dir_keys else 0

    def run(self):
        try:
            while not self._stop_event.is_set():
                self._scan_changed_dirs()
                time.sleep(self.poll_interval)
        finally:
            self.finished.emit()

    def stop(self):
        self._stop_event.set()



# class FolderMonitorWorker(QObject):
#     new_datafolder_found = pyqtSignal(str)
#     finished = pyqtSignal()
#
#     def __init__(self, root_path, poll_interval=0.5):
#         super().__init__()
#         self.root_path = os.path.abspath(root_path)
#         self.poll_interval = poll_interval
#         self._stop_event = Event()
#         self.known_datafolders = set()
#         self._inspect_current_folders()
#
#     def _inspect_current_folders(self):
#         logger.info("Inspecting existing data folders...")
#         for dirpath, dirnames, _ in os.walk(self.root_path, topdown=True):
#             if is_datafolder(dirpath):
#                 self.known_datafolders.add(dirpath)
#                 # prune: no need to visit children of a datafolder
#                 dirnames[:] = []
#         logger.info("Done inspecting existing data folders.")
#
#     def stop(self):
#         self._stop_event.set()
#
#     def run(self):
#         try:
#             while not self._stop_event.is_set():
#                 for dirpath, dirnames, _ in os.walk(self.root_path, topdown=True):
#                     # prune entire subtree if we already know it's a datafolder
#                     if dirpath in self.known_datafolders:
#                         dirnames[:] = []
#                         continue
#
#                     if is_datafolder(dirpath):
#                         self.known_datafolders.add(dirpath)
#                         self.new_datafolder_found.emit(dirpath)
#                         dirnames[:] = []  # prune newly found subtree
#                 time.sleep(self.poll_interval)
#         finally:
#             self.finished.emit()
#


class CustomTreeView(QTreeView):
    def __init__(self, parent_widget):
        """
        Allows navigating with back and forward keys on the mouse
        """
        super().__init__()
        self.folder_widget = parent_widget

    def mousePressEvent(self, event):
        if event.button() == Qt.XButton1:  # Back
            self.folder_widget.go_back()
            return
        elif event.button() == Qt.XButton2:  # Forward
            self.folder_widget.go_forward()
            return
        super().mousePressEvent(event)


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

        self.tree = CustomTreeView(self)
        self.tree.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.tree.setModel(self.proxy_model)
        self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(root_path)))
        self.tree.clicked.connect(self.folder_selected)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(3, Qt.DescendingOrder)  # Column 3 = "Date Modified"

        self.tree.hideColumn(1)  # Size
        self.tree.hideColumn(2)  # Type
        self.tree.hideColumn(3)  # Time Modified

        # ----- top buttons --------------
        self.select_button = QPushButton("Select Root")
        self.select_button.clicked.connect(self.select_new_root)

        self.refresh_button = QPushButton(get_icon("refresh.svg"), "")
        self.refresh_button.setToolTip("Refresh folders")
        self.refresh_button.clicked.connect(self.refresh_model)

        self.sort_mtime_button = QPushButton(get_icon("sort_by_time.svg"), "")
        self.sort_mtime_button.setToolTip("Sort folders by modification time")
        self.sort_mtime_button.clicked.connect(self.sort_by_mtime)
        self.current_sort_order = Qt.DescendingOrder

        self.recent_button = QPushButton(get_icon("most_recent_folder.svg"), "")
        self.recent_button.setToolTip("Select most recent data folder.\nRight‑click to toggle auto‑jump.")
        self.recent_button.clicked.connect(self.select_most_recent_folder)

        # lock to always look at the most recent folder
        self.recent_lock_enabled = False
        self.recent_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.recent_button.customContextMenuRequested.connect(self.toggle_recent_lock)

        self.search_button = QPushButton(get_icon("search.svg"), "")
        self.search_button.setToolTip("Search folders by name")
        self.search_button.setCheckable(True)
        self.search_button.clicked.connect(self.toggle_search_box)

        button_row_upper = QHBoxLayout()
        button_row_upper.addWidget(self.select_button)
        button_row_upper.addWidget(self.refresh_button)
        button_row_upper.addWidget(self.sort_mtime_button)
        button_row_upper.addWidget(self.recent_button)
        button_row_upper.addWidget(self.search_button)

        # -----------  search box ------------------
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search folder name...")
        self.search_box.setVisible(False)
        self.search_box.returnPressed.connect(self.apply_search_filter)
        self.search_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.match_label = QLabel("0/0")
        self.match_label.setVisible(False)

        self.prev_match_button = QPushButton("↑")
        self.prev_match_button.setVisible(False)
        self.prev_match_button.setToolTip("Previous match")
        self.prev_match_button.clicked.connect(self.goto_prev_match)
        self.prev_match_button.setFixedWidth(24)

        self.next_match_button = QPushButton("↓")
        self.next_match_button.setVisible(False)
        self.next_match_button.setToolTip("Next match")
        self.next_match_button.clicked.connect(self.goto_next_match)
        self.next_match_button.setFixedWidth(24)

        self.search_box_row = QHBoxLayout()
        self.search_box_row.addWidget(self.search_box)
        self.search_box_row.addWidget(self.match_label)
        self.search_box_row.addWidget(self.prev_match_button)
        self.search_box_row.addWidget(self.next_match_button)

        # ----- bottom buttons --------------
        self.back_button = QPushButton("←")
        self.back_button.clicked.connect(self.go_back)
        self.back_button.setToolTip("Go back to previously viewed folder")
        self.forward_button = QPushButton("→")
        self.forward_button.clicked.connect(self.go_forward)
        self.forward_button.setToolTip("Go forward to next viewed folder")
        button_row_lower = QHBoxLayout()
        button_row_lower.addWidget(self.back_button)
        button_row_lower.addWidget(self.forward_button)


        layout = QVBoxLayout(self)
        layout.addLayout(button_row_upper)
        layout.addLayout(self.search_box_row)
        layout.addWidget(self.tree)
        layout.addLayout(button_row_lower)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        self.setLayout(layout)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_context_menu)

        # folder selection history
        self.history = []
        self.history_index = -1  # Points to current item in history
        self.history_max = 40

        # folder search matches
        self.search_text = None
        self.search_matches = []
        self.search_match_index = -1
        self.last_expanded_path = None

        # most recent folder monitoring
        self.monitor_worker = None
        self.monitor_thread = None
        self.destroyed.connect(self.stop_monitoring)

        # restore the persisted auto-jump-to-newest preference
        if load_user_config().get("auto_jump_to_newest"):
            self.start_monitoring()


    # ---------- path/index helpers ----------
    def path_to_source_index(self, path: str) -> QModelIndex:
        """Return QFileSystemModel index for path (invalid if path missing)."""
        if not path:
            return QModelIndex()
        return self.model.index(os.path.abspath(path))

    def path_to_proxy_index(self, path: str) -> QModelIndex:
        """Map path -> proxy index (invalid if unmappable)."""
        src = self.path_to_source_index(path)
        if not src.isValid():
            return QModelIndex()
        return self.proxy_model.mapFromSource(src)


    def source_path_from_proxy_index(self, proxy_index: QModelIndex) -> str:
        """Inverse of path_to_proxy_index (proxy -> source -> path)."""
        if not proxy_index.isValid():
            return ""
        source_index = self.proxy_model.mapToSource(proxy_index)
        return self.model.filePath(source_index)

    def focus_path(self, path: str, ensure_visible: bool = True,
                   update_history: bool = True, trigger_callback: bool = True) -> bool:
        """
        Centralized 'go to this folder' operation.
        Returns True if selection happened.
        """
        proxy = self.path_to_proxy_index(path)
        if not proxy.isValid():
            return False

        self.tree.setCurrentIndex(proxy)
        if ensure_visible:
            self.tree.scrollTo(proxy)

        if update_history and is_datafolder(path):
            self._update_history(path)

        if trigger_callback:
            self.on_select_callback(path)
        return True


    def refresh_model(self):
        # this effectively tells the model to "look again"
        self.model.setRootPath("")  # reset
        self.model.setRootPath(self.root_path)
        self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(self.root_path)))


    def folder_selected(self, index: QModelIndex):
        path = self.source_path_from_proxy_index(index)
        if is_datafolder(path):
            self._update_history(path)
        self.on_select_callback(path)

    def select_new_root(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        new_root = QFileDialog.getExistingDirectory(self, "Select Root Directory", self.root_path, options=options)
        self.set_root_path(new_root)

    def set_root_path(self, root_path):
        if os.path.isdir(root_path):
            # If monitoring, stop first
            was_enabled = self.recent_lock_enabled

            # stop current monitoring but DO NOT flip the flag
            self._pause_monitoring()

            self.model.setRootPath(root_path)
            self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(root_path)))
            self.root_path = root_path

            # remember this root for the next launch
            update_user_config(last_root=root_path)

            # clear navigation history
            self.history = []
            self.history_index = -1

            # resume monitoring if user had it enabled
            if was_enabled:
                self.start_monitoring()  # will respect recent_lock_enabled
            self._set_recent_lock_ui()

    def collapse_peer_folders(self, path):
        """
        Collapse all sibling folders of the selected top-level folder.
        Keeps the clicked folder expanded and visible, but collapses everything else at the same level and below.
        """
        proxy_index = self.path_to_proxy_index(path)

        if not proxy_index.isValid():
            return

        parent_proxy = proxy_index.parent()

        for i in range(self.proxy_model.rowCount(parent_proxy)):
            sibling_index = self.proxy_model.index(i, 0, parent_proxy)

            if sibling_index == proxy_index:
                continue  # Don't collapse the clicked folder

            # Collapse sibling and all of its children
            def recurse(index):
                for j in range(self.proxy_model.rowCount(index)):
                    child = self.proxy_model.index(j, 0, index)
                    recurse(child)
                self.tree.collapse(index)

            recurse(sibling_index)


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

        if not is_datafolder(path):
            menu.addAction("Collapse Peers")
            menu.addAction("Set as Root")

        if is_in_trash:
            menu.addAction("Restore")
        elif is_trash:
            menu.addAction("Empty")
        else:
            menu.addAction("Trash")

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

        elif action_text == "Collapse Peers":
            self.collapse_peer_folders(path)


    def open_in_file_explorer(self, path):
        if detect_platform() == "wsl":
            try:
                win_path = to_windows_path(path)
                subprocess.Popen(["explorer.exe", win_path])
            except Exception as e:
                logger.error(f"Failed to open path in Explorer: {e}", exc_info=True)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @update_explorer_after_trash
    def handle_trash(self, path):
        parent_dir = os.path.dirname(path)
        trash_dir = os.path.join(parent_dir, TRASH_FOLDER_NAME)
        os.makedirs(trash_dir, exist_ok=True)
        try:
            target = _unique_trash_path(trash_dir, os.path.basename(path))
            shutil.move(path, target)
            logger.info(f"Moved {path} to {trash_dir}")
        except Exception as e:
            logger.error(f"Failed to move {path} to trash: {e}", exc_info=True)

        # --- auto-select next index in the same parent ---
        trashed_proxy = self.path_to_proxy_index(path)
        parent_proxy = trashed_proxy.parent()

        # Try next sibling row
        next_row = trashed_proxy.row() + 1
        if next_row >= self.proxy_model.rowCount(parent_proxy):
            # No next sibling → try previous sibling
            next_row = trashed_proxy.row() - 1

        if 0 <= next_row < self.proxy_model.rowCount(parent_proxy):
            next_proxy = self.proxy_model.index(next_row, 0, parent_proxy)
        else:
            # No siblings → select parent
            next_proxy = parent_proxy

        if next_proxy.isValid():
            self.tree.setCurrentIndex(next_proxy)
            self.tree.scrollTo(next_proxy)
            self._update_history(self.source_path_from_proxy_index(next_proxy))
            self.on_select_callback(self.source_path_from_proxy_index(next_proxy))

    @update_explorer_after_trash
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
            logger.info(f"Restored {path} to {restore_path}")
            self.focus_path(restore_path)
        except Exception as e:
            logger.error(f"Failed to restore {path}: {e}", exc_info=True)


    @update_explorer_after_trash
    def handle_empty_trash(self, path):
        if not os.path.isdir(path):
            logger.warning(f"Path {path} is not a directory")
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
                logger.info(f"Deleted trash folder {path}")
            except Exception as e:
                logger.error(f"Failed to delete trash folder {path}: {e}", exc_info=True)

    def select_most_recent_folder(self):
        root_path = self.model.rootPath()
        most_recent_path = None
        latest_mtime = -1.0
        logger.debug(f"Scanning for most recent data folder under {root_path}...")

        stack = [root_path]
        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if not entry.is_dir(follow_symlinks=False):
                            continue

                        dirpath = entry.path
                        # Skip Trash folders entirely
                        if os.path.basename(dirpath) == TRASH_FOLDER_NAME:
                            continue

                        # Check if this is a data folder
                        run_py = os.path.join(dirpath, DATAFOLDER_INDICATOR_FILE)
                        if os.path.isfile(run_py):
                            try:
                                mtime = os.stat(run_py, follow_symlinks=False).st_mtime
                            except (FileNotFoundError, PermissionError):
                                continue
                            if mtime > latest_mtime:
                                latest_mtime = mtime
                                most_recent_path = dirpath
                            # prune children of a datafolder
                            continue

                        # Not a datafolder → add to stack for further scanning
                        stack.append(dirpath)
            except (FileNotFoundError, PermissionError):
                continue

        if most_recent_path:
            self.focus_path(most_recent_path)


    def sort_by_mtime(self):
        self.tree.sortByColumn(3, self.current_sort_order)
        # Toggle the sort order for next time
        self.current_sort_order = (
            Qt.AscendingOrder if self.current_sort_order == Qt.DescendingOrder else Qt.DescendingOrder
        )

    # navigate with side buttons
    def _update_history(self, path):
        # Avoid duplicates when navigating
        if self.history and self.history_index >= 0 and self.history[self.history_index] == path:
            return

        # Truncate forward history if we branched
        self.history = self.history[:self.history_index + 1]
        self.history.append(path)

        # Limit history length
        if len(self.history) > self.history_max:
            self.history.pop(0)

        self.history_index = len(self.history) - 1

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self._navigate_to_history_index()

    def go_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self._navigate_to_history_index()

    def _navigate_to_history_index(self):
        path = self.history[self.history_index]
        self.focus_path(path, ensure_visible=True, update_history=False)

    def toggle_search_box(self):
        visible = self.search_button.isChecked()
        self.search_box.setVisible(visible)
        self.prev_match_button.setVisible(visible)
        self.next_match_button.setVisible(visible)
        if visible:
            self.search_box.setFocus()
        else:
            self.clear_search_filter()


    # ------- folder searching functions ------------------------
    def apply_search_filter(self):
        text = self.search_box.text().strip()
        if not text:
            return

        # no text change, type enter move to next match
        if text == self.search_text:
            if len(self.search_matches) > 1:
                self.goto_next_match()
            return

        # text changed, redo search
        self.search_matches = self.find_folders_matching(text)
        self.search_text = text
        count = len(self.search_matches)
        if count == 0:
            self.match_label.setText("0/0")
            self.match_label.setStyleSheet("color: #ff5555; padding-left: 2px; padding-right: 2px;")
            self.match_label.setVisible(True)
            return

        self.search_match_index = 0
        self.match_label.setText(f"1/{count}")
        self.match_label.setStyleSheet("color: gray; padding-left: 2px; padding-right: 2px;")
        self.match_label.setVisible(True)
        self.jump_to_current_match()

    def find_folders_matching(self, pattern: str):
        matches = []
        pattern_lower = pattern.lower()

        for dirpath, dirnames, _ in os.walk(self.root_path):
            if pattern_lower in os.path.basename(dirpath).lower():
                matches.append(dirpath)

        return matches

    def jump_to_current_match(self):
        if 0 <= self.search_match_index < len(self.search_matches):
            path = self.search_matches[self.search_match_index]
            source_index = self.model.index(path)
            if not source_index.isValid():
                return
            proxy_index = self.proxy_model.mapFromSource(source_index)
            if not proxy_index.isValid():
                return

            # Collapse previously expanded using saved path
            if self.last_expanded_path:
                last_source = self.model.index(self.last_expanded_path)
                last_proxy = self.proxy_model.mapFromSource(last_source)
                if last_proxy.isValid():
                    self.tree.collapse(last_proxy)

            self.tree.expand(proxy_index)
            self.tree.scrollTo(proxy_index)
            self.tree.setCurrentIndex(proxy_index)
            self.folder_selected(proxy_index)

            self.last_expanded_path = path  # Save the current path

    def goto_next_match(self):
        if self.search_matches:
            self.search_match_index = (self.search_match_index + 1) % len(self.search_matches)
            self.match_label.setText(f"{self.search_match_index + 1}/{len(self.search_matches)}")
            self.jump_to_current_match()

    def goto_prev_match(self):
        if self.search_matches:
            self.search_match_index = (self.search_match_index - 1 + len(self.search_matches)) % len(self.search_matches)
            self.match_label.setText(f"{self.search_match_index + 1}/{len(self.search_matches)}")
            self.jump_to_current_match()

    def clear_search_filter(self):
        self.search_box.clear()
        self.search_matches = []
        self.search_text = None
        self.search_match_index = -1
        self.match_label.setVisible(False)
        self.last_expanded_path = None

    # -------------- monitor and lock to the most recent folder ----------
    def _set_recent_lock_ui(self):
        # locked icon when enabled, unlocked when disabled
        self.recent_button.setIcon(get_icon(
            "most_recent_folder_lock.svg" if self.recent_lock_enabled else "most_recent_folder.svg"
        ))

    def toggle_recent_lock(self, pos):
        """
        lock to always look at the most recent data folder
        """
        menu = QMenu()
        action_text = "Unlock auto-jump" if self.recent_lock_enabled else "Auto-jump to newest"
        action = menu.addAction(action_text)
        result = menu.exec_(self.recent_button.mapToGlobal(pos))
        if result == action:
            if self.recent_lock_enabled:
                self.stop_monitoring()  # flips flag OFF
            else:
                self.start_monitoring()  # flips flag ON
            # persist only on explicit user toggle (stop_monitoring also runs on
            # close, so saving there would wipe the preference every exit)
            update_user_config(auto_jump_to_newest=self.recent_lock_enabled)

    def start_monitoring(self):
        self.recent_lock_enabled = True
        # don’t start twice
        if self.monitor_thread is not None and self.monitor_thread.isRunning():
            self._set_recent_lock_ui()
            return

        # clear stale thread if present
        if self.monitor_thread is not None and not self.monitor_thread.isRunning():
            self.monitor_thread.deleteLater()
            self.monitor_thread = None

        thread = QThread(self)
        worker = FolderMonitorWorker(self.root_path)

        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.new_datafolder_found.connect(self.handle_new_datafolder)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        def _clear_refs(t=thread):
            # only clear if THIS thread is still the active one
            if self.monitor_thread is t:
                self.monitor_worker = None
                self.monitor_thread = None
                self._set_recent_lock_ui()

        thread.finished.connect(_clear_refs)

        self.monitor_thread = thread
        self.monitor_worker = worker

        thread.start()
        self._set_recent_lock_ui()

    def _pause_monitoring(self):
        """Internal: stop threads without changing recent_lock_enabled."""
        if self.monitor_worker is not None:
            self.monitor_worker.stop()
        if self.monitor_thread is not None:
            self.monitor_thread.quit()
            self.monitor_thread.wait()
        self.monitor_worker = None
        self.monitor_thread = None

    def stop_monitoring(self):
        """User intent: fully disable auto-jump."""
        self.recent_lock_enabled = False
        self._pause_monitoring()
        self._set_recent_lock_ui()

    def handle_new_datafolder(self, new_path: str):
        # Jump to most recent if toggle is ON
        if self.recent_lock_enabled:
            # schedule after 1 second (1000 ms) to let files populate
            # todo: maybe instead triggering with a single when first plot update happens?
            QTimer.singleShot(1000, lambda: self.focus_path(new_path))

    def closeEvent(self, e):
        self.stop_monitoring()
        super().closeEvent(e)


def _unique_trash_path(trash_dir, base_name):
    candidate = os.path.join(trash_dir, base_name)
    if not os.path.exists(candidate):
        return candidate
    i = 1
    while True:
        cand = os.path.join(trash_dir, f"{base_name} ({i})")
        if not os.path.exists(cand):
            return cand
        i += 1