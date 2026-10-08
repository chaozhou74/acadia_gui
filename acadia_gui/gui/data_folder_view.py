import os
import subprocess
import shutil
from functools import wraps
import logging
import time
from PyQt5.QtWidgets import (QWidget, QFileSystemModel, QTreeView, QVBoxLayout, QSizePolicy, QLineEdit, QLabel,
                             QPushButton, QFileDialog, QMenu, QApplication, QHBoxLayout, QMessageBox)
from PyQt5.QtCore import Qt, QModelIndex, QDir, QUrl, QSortFilterProxyModel, QThread, QTimer
from PyQt5.QtGui import QIcon, QDesktopServices, QColor, QBrush


from acadia_qmsmt.utils.path_adapter import detect_platform, to_windows_path
from acadia_gui.icons import get_icon
from acadia_gui.utils import load_user_config, update_user_config
from acadia_gui.gui.folder_index import FolderIndexWorker


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
        # data()/hasChildren() run for every visible row on every repaint (hover, scroll, select).
        # Uncached, each was an NFS stat of <row>/run.py on the GUI thread. Cache the answer
        # briefly; a folder that has just become a data folder updates within the TTL.
        self._datafolder_cache = {}

    DATAFOLDER_CACHE_TTL_S = 5.0

    def is_datafolder_cached(self, path):
        now = time.monotonic()
        hit = self._datafolder_cache.get(path)
        if hit is not None and now - hit[1] < self.DATAFOLDER_CACHE_TTL_S:
            return hit[0]
        result = is_datafolder(path)
        if len(self._datafolder_cache) > 20000:
            self._datafolder_cache.clear()
        self._datafolder_cache[path] = (result, now)
        return result

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if role not in (Qt.DecorationRole, Qt.ForegroundRole) or index.column() != 0:
            return super().data(index, role)
        path = self.filePath(index)
        base_name = os.path.basename(path)

        if role == Qt.DecorationRole and index.column() == 0:
            if base_name == TRASH_FOLDER_NAME:
                return self.trash_icon
            if self.is_datafolder_cached(path):
                return self.datafolder_icon

        if role == Qt.ForegroundRole and index.column() == 0:
            if base_name == TRASH_FOLDER_NAME:
                return QBrush(QColor("#888888"))  # Greyed out color

        return super().data(index, role)

    def hasChildren(self, index: QModelIndex):
        path = self.filePath(index)
        if path and self.is_datafolder_cached(path):
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

    NAV_KEYS = (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End)

    def keyPressEvent(self, event):
        """Keyboard navigation loads the folder too (it used to need a mouse click).

        Arrow/page keys load after a short pause, so holding a key down doesn't load every folder
        it passes; Enter/Return loads immediately.
        """
        super().keyPressEvent(event)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.folder_widget.load_current_index()
        elif event.key() in self.NAV_KEYS:
            self.folder_widget.schedule_load_current_index()


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

        self.collapse_all_button = QPushButton(get_icon("collapse_all.svg"), "")
        self.collapse_all_button.setToolTip("Collapse all expanded folders")
        self.collapse_all_button.clicked.connect(self.collapse_all_folders)

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
        button_row_lower.addWidget(self.collapse_all_button)


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

        # background folder index (auto-jump, search, most recent) -- see folder_index.py
        self.index_worker = None
        self.index_thread = None
        self._index_ready_seen = False
        self._pending_most_recent = False

        # auto-jump: one pending (cancellable) jump, 1 s after a new folder appears
        self._pending_jump_path = None
        self._jump_timer = QTimer(self)
        self._jump_timer.setSingleShot(True)
        self._jump_timer.setInterval(1000)
        self._jump_timer.timeout.connect(self._do_pending_jump)

        # keyboard navigation: load the folder the cursor rests on
        self._last_loaded_path = None
        self._key_load_timer = QTimer(self)
        self._key_load_timer.setSingleShot(True)
        self._key_load_timer.setInterval(150)
        self._key_load_timer.timeout.connect(self.load_current_index)

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
            self._last_loaded_path = path
            self.on_select_callback(path)
        return True


    def refresh_model(self):
        # this effectively tells the model to "look again"
        self.model.setRootPath("")  # reset
        self.model.setRootPath(self.root_path)
        self.tree.setRootIndex(self.proxy_model.mapFromSource(self.model.index(self.root_path)))


    def folder_selected(self, index: QModelIndex):
        path = self.source_path_from_proxy_index(index)
        if not path:
            return
        if is_datafolder(path):
            self._update_history(path)
        self._last_loaded_path = path
        self.on_select_callback(path)

    def schedule_load_current_index(self):
        self._key_load_timer.start()

    def load_current_index(self):
        self._key_load_timer.stop()
        index = self.tree.currentIndex()
        if index.isValid() and self.source_path_from_proxy_index(index) != self._last_loaded_path:
            self.folder_selected(index)

    def select_new_root(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        new_root = QFileDialog.getExistingDirectory(self, "Select Root Directory", self.root_path, options=options)
        self.set_root_path(new_root)

    def set_root_path(self, root_path):
        if root_path and os.path.isdir(root_path):
            # If monitoring, stop first
            was_enabled = self.recent_lock_enabled

            # stop the index of the old root (and any pending jump) but DO NOT flip the flag
            self._jump_timer.stop()
            self._pending_most_recent = False
            self.stop_index()
            self.clear_search_filter()

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
        elif root_path:
            logger.warning(f"Not a directory: {root_path}")

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

        menu = QMenu(self.tree)   # parented: Wayland popups need a parent surface
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

        # --- choose the folder to select afterwards BEFORE moving (the index dies with the move) ---
        next_path = parent_dir
        trashed_proxy = self.path_to_proxy_index(path)
        if trashed_proxy.isValid():
            parent_proxy = trashed_proxy.parent()
            rows = self.proxy_model.rowCount(parent_proxy)
            for row in (trashed_proxy.row() + 1, trashed_proxy.row() - 1):   # next, else previous
                if 0 <= row < rows:
                    candidate = self.source_path_from_proxy_index(self.proxy_model.index(row, 0, parent_proxy))
                    if candidate and os.path.basename(candidate) != TRASH_FOLDER_NAME:
                        next_path = candidate
                        break

        try:
            os.makedirs(trash_dir, exist_ok=True)
            target = _unique_trash_path(trash_dir, os.path.basename(path))
            shutil.move(path, target)
            logger.info(f"Moved {path} to {trash_dir}")
        except Exception as e:
            logger.error(f"Failed to move {path} to trash: {e}", exc_info=True)
            return

        if next_path and os.path.isdir(next_path):
            self.focus_path(next_path, update_history=is_datafolder(next_path))

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
        """Jump to the newest data folder under the root.

        Answered from the background index (instant once it is built). Before that, the jump
        happens as soon as indexing completes; the window stays responsive meanwhile. (This used
        to walk the whole tree on the GUI thread: 45 s frozen on a 27k-folder NFS root.)
        """
        worker = self.ensure_index()
        if worker.walk_done:
            self._pending_most_recent = False
            newest = worker.newest_datafolder()
            if newest:
                self.focus_path(newest)
            else:
                logger.info(f"No data folders found under {self.root_path}")
        else:
            self._pending_most_recent = True
            self.recent_button.setToolTip("Finding the newest data folder (indexing)...")
            logger.info("Indexing the data folders; will jump to the newest one when done.")

    def collapse_all_folders(self):
        """Collapse every expanded folder, keeping the selected folder's top-level ancestor in view."""
        current = self.tree.currentIndex()
        self.tree.collapseAll()
        self.last_expanded_path = None
        if current.isValid():
            top = current
            while top.parent().isValid() and top.parent() != self.tree.rootIndex():
                top = top.parent()
            self.tree.scrollTo(top)
        else:
            self.tree.scrollToTop()

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
        self.search_text = text
        self.search_matches = self.find_folders_matching(text)
        self.search_match_index = -1
        self._show_search_results(jump=True)

    def _show_search_results(self, jump):
        """Update the match label (and jump to the first match) for the current search."""
        indexing = self.index_worker is not None and not self.index_worker.walk_done
        suffix = "+ (searching...)" if indexing else ""
        count = len(self.search_matches)
        if count == 0:
            self.match_label.setText("0" + suffix if indexing else "0/0")
            color = "gray" if indexing else "#ff5555"
            self.match_label.setStyleSheet(f"color: {color}; padding-left: 2px; padding-right: 2px;")
            self.match_label.setVisible(True)
            return
        if self.search_match_index < 0:
            self.search_match_index = 0
        self.match_label.setText(f"{self.search_match_index + 1}/{count}{suffix}")
        self.match_label.setStyleSheet("color: gray; padding-left: 2px; padding-right: 2px;")
        self.match_label.setVisible(True)
        if jump:
            self.jump_to_current_match()

    def find_folders_matching(self, pattern: str):
        """Folders under the root whose NAME contains `pattern`, newest first.

        Answered from the background index: instant, never blocks the GUI. While the index is
        still being built the matches found so far are returned, and the result refreshes itself
        when indexing completes. (The old version walked the whole tree on the GUI thread: 59 s
        frozen on a 27k-folder NFS root.) Folders inside data folders are not indexed -- the tree
        cannot expand a data folder, so such a match could never be navigated to anyway.
        """
        return self.ensure_index().search(pattern)

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

    def reload_icons(self):
        """Re-apply toolbar icons so they pick up the current theme's color."""
        self.refresh_button.setIcon(get_icon("refresh.svg"))
        self.sort_mtime_button.setIcon(get_icon("sort_by_time.svg"))
        self.search_button.setIcon(get_icon("search.svg"))
        self.collapse_all_button.setIcon(get_icon("collapse_all.svg"))
        self._set_recent_lock_ui()  # recent button (lock state aware)

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
        menu = QMenu(self.recent_button)
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

    # ---------- background index lifecycle ----------
    def ensure_index(self) -> FolderIndexWorker:
        """Start the background folder index for the current root if it isn't running."""
        if self.index_worker is not None and self.index_thread is not None and self.index_thread.isRunning():
            return self.index_worker
        self.stop_index()
        # No QObject parent: if the thread is ever still running when the widget is destroyed, a
        # parented QThread would be deleted with it -> "QThread: Destroyed while thread is still
        # running" -> abort. Lifetime is managed explicitly (stop_index / shutdown).
        thread = QThread()
        worker = FolderIndexWorker(self.root_path, fast=self.recent_lock_enabled)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.new_datafolder_found.connect(self.handle_new_datafolder)
        worker.progress.connect(self._on_index_progress)
        worker.finished.connect(thread.quit)
        self.index_thread, self.index_worker = thread, worker
        self._index_ready_seen = False
        _live_index_threads.add((thread, worker))
        thread.finished.connect(lambda t=thread, w=worker: _live_index_threads.discard((t, w)))
        thread.start(QThread.LowPriority)
        return worker

    def stop_index(self, timeout_ms=3000):
        """Stop the index thread; waits at most `timeout_ms` (a stat stuck on a dead NFS server
        cannot hang the GUI -- the thread is then left to finish on its own)."""
        worker, thread = self.index_worker, self.index_thread
        self.index_worker = self.index_thread = None
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.quit()
            if not thread.wait(timeout_ms):
                logger.warning("Folder index thread did not stop in time (slow file server?)")

    def _on_index_progress(self, n_dirs, done):
        if self.sender() is not self.index_worker:
            return          # a stopped index of a previous root
        if done and not self._index_ready_seen:
            self._index_ready_seen = True
            n_d, n_df = self.index_worker.counts()
            logger.info(f"Indexed {n_df} data folders in {n_d} folders under {self.root_path}")
            self.recent_button.setToolTip("Select most recent data folder.\nRight-click to toggle auto-jump.")
            if self._pending_most_recent:
                self._pending_most_recent = False
                self.select_most_recent_folder()
        if self.search_text:   # refresh a search that ran while the index was being built
            self.search_matches = self.ensure_index().search(self.search_text)
            self._show_search_results(jump=self.search_match_index < 0 and bool(self.search_matches))

    def start_monitoring(self):
        """User intent: auto-jump to newly created data folders."""
        self.recent_lock_enabled = True
        self.ensure_index().fast = True
        self._set_recent_lock_ui()

    def _pause_monitoring(self):
        """Internal: stop jumping without changing recent_lock_enabled."""
        self._jump_timer.stop()
        self._pending_jump_path = None
        if self.index_worker is not None:
            self.index_worker.fast = False

    def stop_monitoring(self):
        """User intent: fully disable auto-jump (the index keeps serving search/most recent)."""
        self.recent_lock_enabled = False
        self._pause_monitoring()
        self._set_recent_lock_ui()

    def handle_new_datafolder(self, new_path: str):
        if self.sender() is not None and self.sender() is not self.index_worker:
            return          # from a stopped index of a previous root
        # Jump to most recent if toggle is ON, 1 s later to let files populate. A newer folder
        # arriving meanwhile replaces the pending one; turning the lock off cancels it.
        if self.recent_lock_enabled:
            self._pending_jump_path = new_path
            self._jump_timer.start()

    def _do_pending_jump(self):
        path, self._pending_jump_path = self._pending_jump_path, None
        if path and self.recent_lock_enabled and os.path.isdir(path):
            self.focus_path(path)

    def shutdown(self):
        """Stop all background work; called when the main window closes."""
        self._jump_timer.stop()
        self._key_load_timer.stop()
        self.stop_index()

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)


# index threads that may still be running (e.g. stuck on a dead NFS server at exit)
_live_index_threads = set()


def index_threads_running() -> bool:
    return any(t.isRunning() for t, _ in list(_live_index_threads))


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