"""Background index of the data tree, shared by auto-jump-to-newest, search and "most recent".

Why this exists. On an NFS data root (one measured root: 26,789 directories holding 27,490 data
folders) every whole-tree operation costs tens of thousands of network round trips:

* search and "most recent" walked the whole tree ON THE GUI THREAD: 59 s and 45 s frozen windows;
* the old auto-jump monitor first indexed the tree (54 s during which no new run was noticed),
  then stat'ed 4000 directories every 0.3 s forever -- ~2,400 NFS calls/s per open GUI against
  the measurement server -- and still needed up to ~11 s to notice a new run.

One worker thread now builds and maintains a single in-memory index:

* the initial walk visits the newest directories first (children ordered by mtime), so recent
  data is indexed within the first second;
* afterwards changes are found by re-stat'ing directories in three tiers. A directory's mtime
  changes when an entry is created in it, so a new run folder always bumps its parent.
  "Hot" directories (modified within HOT_WINDOW_S, or ancestors of runs made within it -- where
  new runs appear in practice) are checked every tick, "warm" ones (used within WARM_WINDOW_S)
  every couple of seconds, and the rest ("cold") are swept a batch per tick, so a run appearing
  anywhere is still found, just later;
* `fast` (auto-jump on) checks every 0.5 s; otherwise it idles along at a fraction of the load,
  just keeping search/"most recent" current.

The GUI thread only ever reads snapshots under a lock (search: filter names in memory; newest:
a handful of stats), so none of these operations can freeze the window again.
"""
import os
import math
import time
import logging
from threading import Event, Lock

from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

DATAFOLDER_INDICATOR_FILE = "run.py"   # indicates an Acadia data folder
TRASH_FOLDER_NAME = "Trash"

HOT_WINDOW_S = 3 * 24 * 3600    # "active" directories: re-checked every tick
WARM_WINDOW_S = 30 * 24 * 3600  # "used this month": re-checked every WARM period
FAST_TICK_S = 0.5               # tick period while auto-jump is on
SLOW_TICK_S = 2.0               # tick period otherwise (index only feeds search / most recent)
FAST_WARM_PERIOD_S = 2.0
SLOW_WARM_PERIOD_S = 8.0
WALK_BUDGET_FRACTION = 0.8      # share of a tick the initial walk may use
FAST_COLD_BATCH = 150           # cold directories re-stat'ed per tick (auto-jump on)
SLOW_COLD_BATCH = 50
# Measured on a 26,789-dir NFS root: ~22 dirs hot, ~530 warm. With auto-jump on, a new run is
# noticed within ~0.5 s in folders active in the last 3 days, ~2 s in folders used in the last
# month, and within one cold sweep (~90 s) anywhere else.


class FolderIndexWorker(QObject):
    new_datafolder_found = pyqtSignal(str)   # a data folder that appeared after indexing began
    progress = pyqtSignal(int, bool)         # (indexed directory count, initial walk complete)
    finished = pyqtSignal()

    def __init__(self, root_path, fast=False):
        super().__init__()
        self.root_path = os.path.abspath(root_path)
        self.fast = fast
        self._stop_event = Event()
        self._lock = Lock()

        self.dirs = {}           # non-data directory -> mtime when last checked
        self.datafolders = {}    # data folder -> directory mtime (ordering hint)
        self.hot = set()
        self.warm = set()
        self._warm_keys = []
        self._warm_pos = 0
        self._walk = []          # stack of (path, emit_if_datafolder, known_mtime or None)
        self._cold_keys = []
        self._cold_pos = 0
        self.walk_done = False
        self.stat_calls = 0      # NFS round-trip counter, for diagnostics/tests

    # ------------------------------------------------------------------ public (any thread)
    def stop(self):
        self._stop_event.set()

    def search(self, pattern: str):
        """Indexed folders whose NAME contains `pattern` (case-insensitive), newest first."""
        p = pattern.lower()
        with self._lock:
            items = list(self.dirs.items()) + list(self.datafolders.items())
        hits = [(m, path) for path, m in items
                if path != self.root_path and p in os.path.basename(path).lower()]
        hits.sort(reverse=True)
        return [path for _, path in hits]

    def newest_datafolder(self, candidates=20):
        """Most recently created data folder (by run.py mtime among the newest directories)."""
        with self._lock:
            top = sorted(self.datafolders.items(), key=lambda kv: kv[1], reverse=True)[:candidates]
        best, best_t = None, -1.0
        for path, _ in top:
            try:
                t = os.stat(os.path.join(path, DATAFOLDER_INDICATOR_FILE)).st_mtime
            except OSError:
                continue
            if t > best_t:
                best, best_t = path, t
        return best

    def counts(self):
        with self._lock:
            return len(self.dirs), len(self.datafolders)

    # ------------------------------------------------------------------ worker thread
    def run(self):
        try:
            self._walk.append((self.root_path, False, None))
            while not self._stop_event.is_set():
                started = time.monotonic()
                tick = FAST_TICK_S if self.fast else SLOW_TICK_S
                self._tick(started + tick * WALK_BUDGET_FRACTION)
                if self._walk:
                    # one-time initial indexing: keep going (hot checks still run every tick)
                    self._stop_event.wait(0.005)
                else:
                    self._stop_event.wait(max(0.05, tick - (time.monotonic() - started)))
        except Exception as e:
            logger.error(f"Folder index stopped unexpectedly: {e}", exc_info=True)
        finally:
            self.finished.emit()

    def _tick(self, walk_deadline):
        now = time.time()
        # 1) hot directories: where new runs appear in practice
        for d in list(self.hot):
            if self._stop_event.is_set():
                return
            self._check(d, now)

        # 2) initial walk, newest first, within the time budget
        if self._walk:
            while self._walk and time.monotonic() < walk_deadline and not self._stop_event.is_set():
                self._visit(*self._walk.pop(), now=now)
            if not self._walk and not self.walk_done:
                self.walk_done = True
                self._recompute_hot(now)
            self.progress.emit(len(self.dirs), self.walk_done)
            if not self.walk_done:
                return   # finish indexing before sweeping cold directories

        # 3) a slice of the warm directories, so that each is re-checked every warm period
        nw = len(self._warm_keys)
        if nw:
            period = FAST_WARM_PERIOD_S if self.fast else SLOW_WARM_PERIOD_S
            tick = FAST_TICK_S if self.fast else SLOW_TICK_S
            for _ in range(min(nw, max(1, math.ceil(nw * tick / period)))):
                if self._stop_event.is_set():
                    return
                d = self._warm_keys[self._warm_pos % nw]
                self._warm_pos += 1
                if d not in self.hot:
                    self._check(d, now)

        # 4) a batch of cold directories, round robin
        n = len(self._cold_keys)
        if n:
            batch = min(FAST_COLD_BATCH if self.fast else SLOW_COLD_BATCH, n)
            for _ in range(batch):
                if self._stop_event.is_set():
                    return
                d = self._cold_keys[self._cold_pos % n]
                self._cold_pos += 1
                if d not in self.hot and d not in self.warm:
                    self._check(d, now)
            if self._cold_pos >= n:          # completed a full sweep
                self._cold_pos = 0
                with self._lock:
                    self._cold_keys = list(self.dirs)
                self._recompute_hot(now)

    # ------------------------------------------------------------------ helpers (worker thread)
    def _stat_mtime(self, path):
        self.stat_calls += 1
        try:
            return os.stat(path, follow_symlinks=False).st_mtime
        except OSError:
            return None

    def _check(self, d, now):
        """Re-stat a known directory; if it changed, look for what was added."""
        old = self.dirs.get(d)
        if old is None:
            self.hot.discard(d)
            return
        mtime = self._stat_mtime(d)
        if mtime is None:                     # removed / trashed
            with self._lock:
                self.dirs.pop(d, None)
            self.hot.discard(d)
            self.warm.discard(d)
            return
        if mtime == old:
            return
        with self._lock:
            self.dirs[d] = mtime
        self._classify(d, mtime, now)
        self._rescan(d, now)

    def _rescan(self, d, now):
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            return
        if any(e.name == DATAFOLDER_INDICATOR_FILE for e in entries):
            # a known directory that has just become a data folder (run.py written after mkdir)
            with self._lock:
                mtime = self.dirs.pop(d, None)
                self.datafolders[d] = mtime or now
            self.hot.discard(d)
            self._mark_ancestors_hot(d)
            self.new_datafolder_found.emit(d)
            return
        for e in entries:
            if not _is_subdir(e) or e.path in self.dirs or e.path in self.datafolders:
                continue
            self._visit(e.path, True, None, now=now)   # new subtree: index (and report) right away

    def _visit(self, path, emit, mtime, now):
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            return
        self.stat_calls += 1
        if mtime is None:
            mtime = self._stat_mtime(path)
            if mtime is None:
                return
        if any(e.name == DATAFOLDER_INDICATOR_FILE for e in entries):
            with self._lock:
                self.datafolders[path] = mtime
            if now - mtime < HOT_WINDOW_S:
                self._mark_ancestors_hot(path)
            if emit:
                self.new_datafolder_found.emit(path)
            return   # never descend into a data folder

        with self._lock:
            self.dirs[path] = mtime
            self._cold_keys.append(path)
        self._classify(path, mtime, now)

        children = []
        for e in entries:
            if not _is_subdir(e):
                continue
            try:
                child_mtime = e.stat(follow_symlinks=False).st_mtime
                self.stat_calls += 1
            except OSError:
                continue
            children.append((child_mtime, e.path))
        children.sort()   # oldest first onto the stack -> newest popped (visited) first
        if emit:
            for child_mtime, child in reversed(children):   # new subtree: visit now, newest first
                self._visit(child, True, child_mtime, now)
        else:
            self._walk.extend((child, False, child_mtime) for child_mtime, child in children)

    def _classify(self, d, mtime, now):
        age = now - mtime
        if age < HOT_WINDOW_S:
            self.hot.add(d)
        elif age < WARM_WINDOW_S and d not in self.warm:
            self.warm.add(d)
            self._warm_keys.append(d)

    def _mark_ancestors_hot(self, path):
        parent = os.path.dirname(path)
        while parent.startswith(self.root_path) and parent in self.dirs:
            self.hot.add(parent)
            if parent == self.root_path:
                break
            parent = os.path.dirname(parent)

    def _recompute_hot(self, now):
        with self._lock:
            hot = {d for d, m in self.dirs.items() if now - m < HOT_WINDOW_S}
            warm = {d for d, m in self.dirs.items() if HOT_WINDOW_S <= now - m < WARM_WINDOW_S}
            recent_runs = [p for p, m in self.datafolders.items() if now - m < HOT_WINDOW_S]
        self.hot = hot
        self.warm = warm
        self._warm_keys = list(warm)
        self._warm_pos = 0
        for p in recent_runs:
            self._mark_ancestors_hot(p)


def _is_subdir(entry):
    try:
        return entry.is_dir(follow_symlinks=False) and entry.name != TRASH_FOLDER_NAME
    except OSError:
        return False
