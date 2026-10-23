import os
import subprocess
import shutil
from functools import wraps
import heapq
import logging
import queue
from threading import Event, Lock, Thread
import time
from PyQt5.QtWidgets import (QWidget, QFileSystemModel, QTreeView, QVBoxLayout, QSizePolicy, QLineEdit, QLabel,
                             QPushButton, QFileDialog, QMenu, QApplication, QHBoxLayout, QMessageBox,
                             QProgressBar, QToolButton, QGridLayout)
from PyQt5.QtCore import Qt, QModelIndex, QDir, QUrl, QSortFilterProxyModel, QObject, pyqtSignal, QTimer
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



# file systems where each request is a network round trip: there, listings run in parallel
NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "v9fs", "drvfs", "fuse.sshfs",
                       "ceph", "glusterfs", "afs"}
SCAN_THREADS = 8

# a task's status row and busy cursor show only once it has run this long
TASK_ROW_DELAY_MS = 500


def scan_threads_for(path):
    """Threads to use for walking `path`: SCAN_THREADS on a network share, where parallel requests
    overlap their round trips; 1 on a local disk, where answers are fast enough that handing work
    between threads costs more than it saves. Unknown counts as a network share."""
    try:
        real, best, fstype = os.path.realpath(path), "", None
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2:
                    mount = parts[1].replace("\\040", " ")
                    # longest mount point wins; for the same one the later line (mounted on top,
                    # e.g. an NFS mount over its automount point) wins
                    if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                        best, fstype = mount, parts[2]
        if fstype is not None:
            return SCAN_THREADS if fstype in NETWORK_FILESYSTEMS else 1
    except OSError:
        pass
    return SCAN_THREADS


def _list_folder(path):
    """One folder's subfolders {path: mtime} and data folders {path: run.py mtime}, or None if
    it cannot be read now. One listing, plus one stat of each subfolder's run.py, which both
    tells a data folder apart and gives its time (the jump-to-newest order)."""
    subs, runs = {}, {}
    try:
        with os.scandir(path) as it:
            entries = [e for e in it if e.name != TRASH_FOLDER_NAME and e.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return subs, runs
    except OSError:
        return None
    for e in entries:
        try:
            runs[e.path] = os.stat(os.path.join(e.path, DATAFOLDER_INDICATOR_FILE)).st_mtime
        except FileNotFoundError:
            try:
                subs[e.path] = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                subs[e.path] = None
        except OSError:
            subs[e.path] = None          # not answering now: kept, and checked again later
    return subs, runs


def scan_tree(root, threads=None, stop_event=None):
    """Walk `root` once: ({data folder: run.py mtime}, {other folder: mtime or None}, [folders
    that could not be read]). Data folders are not descended into. With several threads the
    listings run in parallel on daemon threads, so a request stuck on a dead share never holds
    up closing the program."""
    threads = threads or scan_threads_for(root)
    runs, folders, failed = {}, {}, []
    try:
        folders[root] = os.stat(root).st_mtime
    except OSError:
        return runs, folders, [root]

    def take(path, result):
        if result is None:
            failed.append(path)
            return []
        subs, found = result
        runs.update(found)
        folders.update(subs)
        return list(subs)

    if threads <= 1:
        stack = [root]
        while stack and not (stop_event and stop_event.is_set()):
            path = stack.pop()
            stack.extend(take(path, _list_folder(path)))
        return runs, folders, failed

    todo, lock, done = queue.Queue(), Lock(), Event()
    pending = [1]
    todo.put(root)

    def worker():
        while True:
            path = todo.get()
            if path is None:
                return
            result = _list_folder(path)
            with lock:
                subs = take(path, result)
                pending[0] += len(subs) - 1
                for sub in subs:
                    todo.put(sub)
                if pending[0] == 0:
                    done.set()

    workers = [Thread(target=worker, name="acadia-folder-scan", daemon=True) for _ in range(threads)]
    for w in workers:
        w.start()
    while not done.wait(0.1):
        if stop_event is not None and stop_event.is_set():
            break
    for _ in workers:
        todo.put(None)
    return runs, folders, failed


class FolderMonitorWorker(QObject):
    """Polls the data tree for new data folders (auto-jump to newest).

    Qt's own file watcher is not used: it only watches what the tree has loaded, and on a
    network share it does not see changes made by other machines anyway.

    How this works:
    - On start (in ``run``, on the monitor thread), walk the whole root once (``scan_tree``) to
      learn the existing folders and data folders.
    - A new data folder changes the modification time of the folder it appears in, so every
      known folder is re-checked on its own schedule, by how recently it changed: within
      ``HOT_AGE`` every ``HOT_S``, within ``WARM_AGE`` every ``WARM_S``, else every ``COLD_S``.
      A folder that changes becomes hot. Re-checks are one stat each, on this thread.
    - A folder that changed is re-walked (``scan_tree``); data folders found there are reported.
    - A folder that cannot be read (e.g. a network share not answering) is kept and retried.
    """
    new_datafolder_found = pyqtSignal(str)
    finished = pyqtSignal()

    HOT_S, WARM_S, COLD_S = 0.5, 2.0, 5.0
    HOT_AGE, WARM_AGE = 3 * 24 * 3600, 30 * 24 * 3600
    TICK_S = 0.1

    def __init__(self, root_path, threads=None):
        """
        :param root_path: root directory to monitor
        :param threads: threads for walking; default from ``scan_threads_for(root_path)``
        """
        super().__init__()
        self.root_path = os.path.abspath(root_path)
        self.threads = threads
        self._stop_event = Event()

        self.dir_state = {}           # folder -> mtime (None until it could be read)
        self.known_datafolders = set()
        self._next_check = {}         # folder -> time of its next check (monotonic)
        self._due = []                # heap of (time, folder); entries replaced since are skipped
        self._failures = 0            # failed checks since the share last answered
        self.walk_done = False        # the first walk has finished

    def _schedule(self, path, mtime, now):
        if mtime is None:
            interval = self.HOT_S
        else:
            age = time.time() - mtime
            interval = self.HOT_S if age < self.HOT_AGE else self.WARM_S if age < self.WARM_AGE else self.COLD_S
        when = now + interval
        self._next_check[path] = when
        heapq.heappush(self._due, (when, path))

    def _take_scan(self, scan, report, now):
        runs, folders, failed = scan
        for path in runs:
            if path not in self.known_datafolders:
                self.known_datafolders.add(path)
                if report:
                    self.new_datafolder_found.emit(path)
        for path, mtime in folders.items():
            if self.dir_state.get(path, -1) != mtime:
                self.dir_state[path] = mtime
                self._schedule(path, mtime, now)
        for path in failed:
            self._failed(path, None)
            if path not in self.dir_state:
                self.dir_state[path] = None
                self._schedule(path, None, now)

    def _failed(self, path, error):
        if self._failures == 0:
            logger.warning(f"Folder monitor: cannot read {path} ({error}); will keep retrying "
                           f"(further failures are not logged until the folders answer again).")
        self._failures += 1

    def _check_due(self):
        now = time.monotonic()
        due = []
        while self._due and self._due[0][0] <= now:
            when, path = heapq.heappop(self._due)
            if self._next_check.get(path) == when:
                due.append(path)
        changed = []
        for path in due:
            if self._stop_event.is_set():
                return
            try:
                mtime = os.stat(path, follow_symlinks=False).st_mtime
            except (FileNotFoundError, PermissionError):
                self.dir_state.pop(path, None)
                self._next_check.pop(path, None)
                continue
            except OSError as e:          # e.g. EIO / ESTALE from a network share
                self._failed(path, e)
                self._schedule(path, self.dir_state.get(path), now)
                continue
            if self._failures:
                logger.info(f"Folder monitor: the folders answer again ({self._failures} failed checks).")
                self._failures = 0
            if mtime != self.dir_state.get(path):
                changed.append(path)
            self.dir_state[path] = mtime
            self._schedule(path, mtime, now)
        for path in changed:
            if os.path.isfile(os.path.join(path, DATAFOLDER_INDICATOR_FILE)):
                # a folder that was empty when first seen and has since become a data folder
                self.dir_state.pop(path, None)
                self._next_check.pop(path, None)
                if path not in self.known_datafolders:
                    self.known_datafolders.add(path)
                    self.new_datafolder_found.emit(path)
                continue
            self._take_scan(scan_tree(path, self.threads, self._stop_event), report=True,
                            now=time.monotonic())

    def run(self):
        try:
            self.threads = self.threads or scan_threads_for(self.root_path)
            self._take_scan(scan_tree(self.root_path, self.threads, self._stop_event), report=False,
                            now=time.monotonic())
            self.walk_done = True
            while not self._stop_event.is_set():
                try:
                    self._check_due()
                except Exception:
                    logger.exception("Folder monitor: check failed; continuing.")
                self._stop_event.wait(self.TICK_S)
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
    # results of the background walks, for the GUI thread: (task id, newest data folder or "")
    # and (task id, search text, matching folders)
    _newest_found = pyqtSignal(int, str)
    _search_found = pyqtSignal(int, str, object)

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

        # ----- status of the running task (jump, search, opening a folder) --------------
        self.task_label = QLabel()          # what runs, shortened to its width
        self.task_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.task_time_label = QLabel()     # elapsed time
        self.task_step_label = QLabel()     # step of opening a folder
        self.task_abort_button = QToolButton()
        self.task_abort_button.setText("×")
        self.task_abort_button.setToolTip("Stop")
        self.task_abort_button.setAutoRaise(True)
        self.task_abort_button.clicked.connect(self.cancel_task)
        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 0)            # busy: the length of a task is not known
        self.task_bar.setTextVisible(False)
        self.task_bar.setMaximumHeight(6)
        self.task_row = QWidget()
        task_layout = QGridLayout(self.task_row)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.setVerticalSpacing(1)
        task_layout.addWidget(self.task_label, 0, 0)
        task_layout.addWidget(self.task_time_label, 0, 1)
        task_layout.addWidget(self.task_step_label, 1, 0, 1, 2)
        task_layout.addWidget(self.task_bar, 2, 0, 1, 2)
        task_layout.addWidget(self.task_abort_button, 0, 2, 3, 1)
        self.task_step_label.setVisible(False)
        task_layout.setColumnStretch(0, 1)
        self.task_row.setVisible(False)

        self._task_id = 0                   # id of the running task; results of older ones are dropped
        self._task_kind = None              # (kind, text) of the running task, None when idle
        self._task_stop = None              # stop event of the running walk
        self._task_t0 = 0.0
        self._busy_cursor = False
        self._task_show_timer = QTimer(self)    # the row shows only for tasks that take a while
        self._task_show_timer.setSingleShot(True)
        self._task_show_timer.timeout.connect(self._show_task_row)
        self._task_tick_timer = QTimer(self)    # elapsed time
        self._task_tick_timer.setInterval(100)
        self._task_tick_timer.timeout.connect(self._update_task_label)
        self._task_step = ""


        layout = QVBoxLayout(self)
        layout.addLayout(button_row_upper)
        layout.addLayout(self.search_box_row)
        layout.addWidget(self.tree)
        layout.addWidget(self.task_row)
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
        self._newest_found.connect(self._on_newest_found)
        self._search_found.connect(self._on_search_found)
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


    # ---------- the running task ----------
    def begin_task(self, kind, text, stop_event=None, show_after_ms=None):
        """Start a task that changes the view (jump to newest, search, opening a folder). Only one
        runs at a time and the latest one wins: the running task is stopped and its result
        dropped. Its status row and the busy cursor show after `show_after_ms` (default
        TASK_ROW_DELAY_MS), with the elapsed time and a stop button (only when there is a
        `stop_event` to set). Returns the task id."""
        self.cancel_task()
        self._task_kind = (kind, text)
        self._task_stop = stop_event
        self._task_t0 = time.monotonic()
        self._task_step = ""
        self.task_abort_button.setEnabled(stop_event is not None)
        if show_after_ms is None:
            show_after_ms = TASK_ROW_DELAY_MS
        if show_after_ms > 0:
            self._task_show_timer.start(show_after_ms)
        else:
            self._show_task_row()
        return self._task_id

    def task_running(self, kind=None, text=None):
        return self._task_kind is not None and (kind is None or self._task_kind == (kind, text))

    def task_is_current(self, task_id):
        return self._task_kind is not None and task_id == self._task_id

    def set_task_step(self, step):
        """Show the current step of opening a folder. Drawn at once, because the step may be
        followed by work that keeps the GUI thread busy."""
        if self._task_kind is None or self._task_kind[0] != "open":
            return
        self._task_step = step
        if self.task_row.isVisible():
            self._update_task_label()
            self.task_row.repaint()

    def end_task(self, task_id):
        if self.task_is_current(task_id):
            self._finish_task()

    def cancel_task(self):
        """Stop the running task: the stop button, or another click that changes the view."""
        if self._task_stop is not None:
            self._task_stop.set()
        self._finish_task()

    def _finish_task(self):
        self._task_id += 1
        self._task_kind = None
        self._task_stop = None
        self._task_show_timer.stop()
        self._task_tick_timer.stop()
        self.task_row.setVisible(False)
        if self._busy_cursor:
            QApplication.restoreOverrideCursor()
            self._busy_cursor = False

    def _show_task_row(self):
        if self._task_kind is None:
            return
        self.task_row.setVisible(True)
        if not self._busy_cursor:
            QApplication.setOverrideCursor(Qt.BusyCursor)
            self._busy_cursor = True
        self.layout().activate()            # place the row now, so the label elides to its width
        self.task_row.layout().activate()
        self._update_task_label()
        self._task_tick_timer.start()

    def draw_task_row(self):
        """Draw the status row now, before work that keeps the GUI thread busy."""
        if self.task_row.isVisible():
            self.task_row.repaint()

    def _update_task_label(self):
        if self._task_kind is None:
            return
        name = self._task_kind[1]
        self.task_label.setToolTip(name)
        self.task_label.setText(self.task_label.fontMetrics().elidedText(
            name, Qt.ElideMiddle, max(self.task_label.width(), 30)))
        self.task_time_label.setText(f"{time.monotonic() - self._task_t0:.1f} s")
        self.task_step_label.setText(self._task_step)
        self.task_step_label.setVisible(bool(self._task_step))

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
            self.cancel_task()

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
        menu.deleteLater()      # the window owns it now

    def get_context_actions(self, path):
        parent_dir = os.path.dirname(path)
        is_in_trash = os.path.basename(parent_dir) == TRASH_FOLDER_NAME
        is_trash = os.path.basename(path) == TRASH_FOLDER_NAME

        menu = QMenu(self.window())   # parented: see LivePlotWidget.handle_right_click
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
        """Jump to the newest data folder under the root (by its run.py time).

        The whole tree is walked again on each click, so the answer is always current; the walk
        runs in the background (``scan_tree``, parallel on a network share) as a task (see
        ``begin_task``) and the jump happens when it is done. A click while the jump walk is
        still running is ignored."""
        if self.task_running("jump", "Jump to newest"):
            return
        root_path = self.model.rootPath()
        logger.debug(f"Scanning for most recent data folder under {root_path}...")
        stop = Event()
        task_id = self.begin_task("jump", "Jump to newest", stop)

        def work():
            runs, _, _ = scan_tree(root_path, stop_event=stop)
            if not stop.is_set():
                self._newest_found.emit(task_id, max(runs, key=runs.get) if runs else "")

        Thread(target=work, name="acadia-newest-folder", daemon=True).start()

    def _on_newest_found(self, task_id, path):
        if not self.task_is_current(task_id):     # stopped, or replaced by a later click
            return
        if not (path and self.focus_path(path)):
            self.end_task(task_id)

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
        if not visible and self.task_running() and self._task_kind[0] == "search":
            self.cancel_task()
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

        # text changed, redo search: the walk runs in the background as a task (see begin_task)
        label = f"Search '{text}'"
        if self.task_running("search", label):
            return
        root_path = self.root_path
        stop = Event()
        task_id = self.begin_task("search", label, stop)

        def work():
            matches = self.find_folders_matching(text, root_path, stop)
            if not stop.is_set():
                self._search_found.emit(task_id, text, matches)

        Thread(target=work, name="acadia-folder-search", daemon=True).start()

    def _on_search_found(self, task_id, text, matches):
        if not self.task_is_current(task_id):     # stopped, or replaced by a later click
            return
        self.search_matches = matches
        self.search_text = text
        count = len(self.search_matches)
        if count == 0:
            self.end_task(task_id)
            self.match_label.setText("0/0")
            self.match_label.setStyleSheet("color: #ff5555; padding-left: 2px; padding-right: 2px;")
            self.match_label.setVisible(True)
            return

        self.search_match_index = 0
        self.match_label.setText(f"1/{count}")
        self.match_label.setStyleSheet("color: gray; padding-left: 2px; padding-right: 2px;")
        self.match_label.setVisible(True)
        self.jump_to_current_match()
        self.end_task(task_id)          # no-op once opening the match has replaced it

    def find_folders_matching(self, pattern: str, root_path=None, stop_event=None):
        """Folders and data folders under `root_path` (default: the root) whose name contains
        `pattern`. Folders inside data folders are not searched: the tree cannot open a data
        folder."""
        pattern_lower = pattern.lower()
        runs, folders, _ = scan_tree(root_path or self.root_path, stop_event=stop_event)
        return sorted(path for path in list(folders) + list(runs)
                      if pattern_lower in os.path.basename(path).lower())

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
        menu = QMenu(self.window())
        action_text = "Unlock auto-jump" if self.recent_lock_enabled else "Auto-jump to newest"
        action = menu.addAction(action_text)
        result = menu.exec_(self.recent_button.mapToGlobal(pos))
        menu.deleteLater()
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
        if self.monitor_thread is not None and self.monitor_thread.is_alive():
            self._set_recent_lock_ui()
            return

        # The monitor only reads (os.walk / os.stat), so it runs in a daemon thread: a stat stuck
        # on a network share that stopped answering then cannot hold up closing the browser.
        # New data folders reach the GUI thread through the (queued) signal.
        worker = FolderMonitorWorker(self.root_path)
        worker.new_datafolder_found.connect(self.handle_new_datafolder)
        thread = Thread(target=worker.run, name="acadia-folder-monitor", daemon=True)
        self.monitor_thread = thread
        self.monitor_worker = worker
        thread.start()
        self._set_recent_lock_ui()

    def _pause_monitoring(self, timeout=1.0):
        """Internal: stop the monitor without changing recent_lock_enabled. Waits at most
        ``timeout`` s; a monitor still stuck in a stat after that ends by itself once the call
        returns."""
        worker, thread = self.monitor_worker, self.monitor_thread
        self.monitor_worker = None
        self.monitor_thread = None
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Folder monitor did not stop within %.1f s (a folder is not "
                               "answering); leaving it to finish in the background.", timeout)
                if worker is not None:
                    try:
                        worker.new_datafolder_found.disconnect(self.handle_new_datafolder)
                    except TypeError:
                        pass

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