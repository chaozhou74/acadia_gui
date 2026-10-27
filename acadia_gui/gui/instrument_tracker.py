"""Instrument tracker: the history of every run's saved instrument parameters under the data root.

Every data folder keeps the instrument parameters it ran with (``inst_params.json``). The tracker
reads them all, in run order, and shows when each instrument's settings changed: one row per
instrument on a time axis (split into its parameters on demand), a bar per change, and a strip of
all runs. A click puts a cursor there: the panel below lists the changes at that run and the runs
on either side of it, which open in the browser.

What counts as a change: readings (a temperature) and readbacks (a voltage) jitter from run to run,
so each numeric parameter has a threshold, in its own units: a change is reported when the value
has moved more than the threshold from the last reported value (a deadband, as data historians
record values). A slow drift then shows up as a step each time it has moved far enough, and jitter
never adds up to one. By default the threshold is ``k`` times the parameter's jitter, estimated from
its own history -- the 90th percentile of its run-to-run differences, divided by 1.645 (the same
quantile of a normal distribution); a threshold can also be set by hand, per parameter.
Parameters that never jitter (a frequency setting) and non-numeric ones (on/off, a reference
source) count on every change. The estimate assumes real changes happen in fewer than about 10% of
runs; a parameter that changes more often is marked.

The history is cached in the data root (``.instrument_tracking_record.json``): reopening reads only
the runs added since, and the Re-track button reads everything again.
"""
import bisect
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PyQt5.QtCore import QEvent, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QFrame, QGridLayout,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollBar, QSizePolicy,
                             QSplitter, QSplitterHandle, QStyle, QTableWidget, QTableWidgetItem, QToolButton, QToolTip, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from acadia_gui.gui.data_folder_view import (DATAFOLDER_INDICATOR_FILE, TRASH_FOLDER_NAME, scan_tree,
                                             scan_threads_for)
from acadia_gui.icons import get_icon
from acadia_gui.utils import load_user_config, update_user_config

logger = logging.getLogger(__name__)

RECORD_NAME = ".instrument_tracking_record.json"
PARAMS_FILE = "inst_params.json"
RECORD_VERSION = 2                    # 2: with the runs each instrument is saved in
PRESENCE = "(saved)"                  # the pseudo parameter "<instrument>.(saved)": it went missing, or came back
CONFIG_KEY = "instrument_tracker"     # settings in the GUI config
DEFAULT_K = 5.0                       # default threshold, in units of each parameter's jitter
UNRELIABLE_RATIO = 10.0               # 90th-percentile step this many times a small one: unreliable
BAR_DECADES = 4                       # a change of 10**BAR_DECADES thresholds draws a full bar
MIN_BAR = 0.15                        # the smallest bar drawn, as a fraction of the row
# Instrument colours in this order, for the light and the dark theme; past the eighth they repeat
# (each instrument has its own row and name).
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
# Timeline colours per theme (the dark ones match the dark stylesheet's background and text).
THEMES = {"light": {"fg": "#1a1a19", "bg": "#ffffff", "series": SERIES_LIGHT},
          "dark": {"fg": "#dcdcdc", "bg": "#3a3a3a", "series": SERIES_DARK}}


# ---------------------------------------------------------------------------------- tracking

def _flatten(params, prefix=""):
    out = {}
    for key, value in params.items():
        if isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
        else:
            out[prefix + key] = value
    return out


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_params(path):
    try:
        with open(path) as f:
            return _flatten(json.load(f))
    except (OSError, ValueError) as e:
        logger.debug(f"Instrument tracker: skipped {path}: {e}")
        return None


def _stat_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def load_record(root):
    """The saved tracking record of ``root``, or None if there is none or it is unusable."""
    try:
        with open(os.path.join(root, RECORD_NAME)) as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if record.get("version") != RECORD_VERSION:
        return None
    return record


def save_record(root, record):
    """Write the record into ``root`` atomically (temporary file, then rename). Returns the error
    text, or None."""
    path = os.path.join(root, RECORD_NAME)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(record, f, separators=(",", ":"))
        os.replace(tmp, path)
        return None
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return f"{type(e).__name__}: {e}"


def track(root, previous=None, progress=None, stop_event=None):
    """Read the instrument parameters of every run under ``root`` into a record.

    With a ``previous`` record whose runs are all still there, unchanged and older than every new
    run, only the new runs are read; otherwise everything is. ``progress(text)`` reports the steps.

    :return: ``{"version", "tracked_at", "runs": [[relative path, time, params mtime], ...] in time
        order, "series": {parameter: [[run index, value], ...] where the value differs from the
        previous run}, "presence": {instrument: [[first run, last run + 1], ...] the spans of runs
        it is saved in}}``, or None if stopped.
    """
    say = progress or (lambda text: None)
    say("Finding the runs")
    runs, _folders, _failed = scan_tree(root, stop_event=stop_event, skip_trash=False)
    if stop_event is not None and stop_event.is_set():
        return None
    threads = scan_threads_for(root)
    paths = list(runs)
    say(f"Checking {len(paths)} runs")
    with ThreadPoolExecutor(threads) as pool:
        mtimes = list(pool.map(lambda p: _stat_mtime(os.path.join(p, PARAMS_FILE)), paths))
    found = sorted(((runs[p], os.path.relpath(p, root), m) for p, m in zip(paths, mtimes)
                    if m is not None), key=lambda r: (r[0], r[1]))

    usable = previous is not None and previous.get("runs")
    if usable:
        old = {rel: (t, m) for rel, t, m in previous["runs"]}
        now = {rel: (t, m) for t, rel, m in found}
        last_time = previous["runs"][-1][1]
        usable = (all(rel in now and now[rel][1] == m for rel, (t, m) in old.items())
                  and all(t >= last_time for t, rel, m in found if rel not in old))
    if usable:
        record = {"version": RECORD_VERSION, "runs": [list(r) for r in previous["runs"]],
                  "series": {k: [list(p) for p in v] for k, v in previous["series"].items()},
                  "presence": {k: [list(p) for p in v] for k, v in previous["presence"].items()}}
        new = [(t, rel, m) for t, rel, m in found if rel not in old]
    else:
        record = {"version": RECORD_VERSION, "runs": [], "series": {}, "presence": {}}
        new = found
    last = {k: v[-1][1] for k, v in record["series"].items()}
    say(f"Reading {len(new)} new runs" if usable else f"Reading {len(new)} runs")
    with ThreadPoolExecutor(threads) as pool:
        params = pool.map(lambda r: _read_params(os.path.join(root, r[1], PARAMS_FILE)), new)
        for (t, rel, m), values in zip(new, params):
            if stop_event is not None and stop_event.is_set():
                return None
            if values is None:
                continue
            index = len(record["runs"])
            record["runs"].append([rel, t, m])
            for key, value in values.items():
                if key not in last or last[key] != value:
                    record["series"].setdefault(key, []).append([index, value])
                    last[key] = value
            for name in {key.split(".", 1)[0] for key in values}:
                spans = record["presence"].setdefault(name, [])
                if spans and spans[-1][1] == index:
                    spans[-1][1] = index + 1
                else:
                    spans.append([index, index + 1])
    record["tracked_at"] = time.time()
    return record


# ---------------------------------------------------------------------------------- analysis

def in_trash(rel):
    return TRASH_FOLDER_NAME in rel.replace("\\", "/").split("/")


def _spans(present):
    """``[[start, stop], ...]`` of the True runs of a boolean array."""
    edges = np.diff(np.concatenate([[0], present.astype(np.int8), [0]]))
    return [[int(a), int(b)] for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))]


def gaps(spans, n_runs):
    """The complement of presence spans: ``[[start, stop], ...]`` of the runs an instrument is missing from."""
    present = np.zeros(n_runs, dtype=bool)
    for a, b in spans:
        present[a:b] = True
    return _spans(~present)


def without_trash(record):
    """The record with the runs in Trash folders left out (the series rebuilt without them)."""
    keep = [i for i, (rel, _t, _m) in enumerate(record["runs"]) if not in_trash(rel)]
    if len(keep) == len(record["runs"]):
        return record
    new_index = {old: new for new, old in enumerate(keep)}
    series = {}
    for key, points in record["series"].items():
        out, j, value, last = [], 0, None, object()
        for old in keep:                       # the value at each kept run: the latest point at or before it
            while j < len(points) and points[j][0] <= old:
                value = points[j][1]
                j += 1
            if j and value != last:
                out.append([new_index[old], value])
                last = value
        if out:
            series[key] = out
    presence = {}
    for name, spans in record["presence"].items():
        present = np.zeros(len(record["runs"]), dtype=bool)
        for a, b in spans:
            present[a:b] = True
        kept = _spans(present[keep])
        if kept:
            presence[name] = kept
    return {**record, "runs": [record["runs"][i] for i in keep], "series": series, "presence": presence}


def parameter_noise(series, n_runs):
    """``{parameter: (noise, reliable)}`` from the run-to-run differences: noise = 90th percentile
    of |difference| / 1.645, the unchanged runs counted as zeros.

    Jitter makes steps of one typical size, so the 90th-percentile step is close to the small
    nonzero steps (their 25th percentile). When real changes are frequent enough to reach the 90th
    percentile, that step is much larger than the small ones: the noise is then overestimated and
    the estimate is marked unreliable (ratio above UNRELIABLE_RATIO)."""
    out = {}
    n = max(n_runs - 1, 1)
    for key, points in series.items():
        steps = sorted(abs(b[1] - a[1]) for a, b in zip(points, points[1:])
                       if _is_number(a[1]) and _is_number(b[1]))
        zeros = n - len(steps)
        rank = int(math.ceil(0.9 * n)) - 1          # the 90th percentile of all n differences
        q90 = 0.0 if rank < zeros or not steps else steps[min(rank - zeros, len(steps) - 1)]
        nonzero = [x for x in steps if x > 0]
        small = nonzero[len(nonzero) // 4] if nonzero else 0.0
        out[key] = (q90 / 1.645, not (q90 > 0 and q90 > UNRELIABLE_RATIO * small))
    return out


def bar_height(ratio):
    if math.isinf(ratio):
        return 1.0
    return min(1.0, max(MIN_BAR, math.log10(ratio) / BAR_DECADES))


def find_changes(record, k=DEFAULT_K, thresholds=None):
    """The reported changes and each parameter's threshold: ``(events, info)``.

    A numeric parameter's change is reported when it has moved more than its threshold from the
    last reported value: ``thresholds[parameter]`` when given, else k times its jitter. Without
    jitter (threshold 0), and for non-numeric parameters, every change counts.

    events: ``{instrument: [{"run": index, "height": 0..1, "changes": [(parameter, old, new,
    ratio), ...]}, ...]}``, ratio = |change| / threshold (inf when every change counts). An
    instrument missing from some runs gets a change of ``<instrument>.(saved)`` where it goes
    missing ("saved" -> "missing") and where it comes back.
    info: ``{parameter: {"jitter", "reliable", "threshold", "set", "changes", "numeric"}}``, ``set``
    when the threshold was given."""
    noise = parameter_noise(record["series"], len(record["runs"]))
    thresholds = thresholds or {}
    grouped, info = {}, {}
    for key, points in record["series"].items():
        jitter, reliable = noise[key]
        limit = thresholds.get(key, k * jitter)
        entry = info[key] = {"jitter": jitter, "reliable": reliable, "threshold": limit,
                             "set": key in thresholds, "changes": 0,
                             "numeric": all(_is_number(value) for _index, value in points)}
        ref = points[0][1]
        for index, value in points[1:]:
            if value == ref:
                continue
            if _is_number(ref) and _is_number(value) and limit > 0:
                ratio = abs(value - ref) / limit
                if ratio <= 1:
                    continue
            else:
                ratio = math.inf
            instrument = key.split(".", 1)[0]
            grouped.setdefault((instrument, index), []).append((key, ref, value, ratio))
            entry["changes"] += 1
            ref = value
    n_runs = len(record["runs"])
    for instrument, spans in record["presence"].items():       # missing from some runs: a change too
        for a, b in spans:
            if a > 0:
                grouped.setdefault((instrument, a), []).append((f"{instrument}.{PRESENCE}", "missing", "saved", math.inf))
            if b < n_runs:
                grouped.setdefault((instrument, b), []).append((f"{instrument}.{PRESENCE}", "saved", "missing", math.inf))
    events = {}
    for (instrument, index), changes in sorted(grouped.items(), key=lambda kv: kv[0][1]):
        height = max(bar_height(ratio) for _key, _old, _new, ratio in changes)
        events.setdefault(instrument, []).append({"run": index, "height": height, "changes": changes})
    return events, info


# ---------------------------------------------------------------------------------- window

SNAP_PIXELS = 6              # a click, drag or hover this close to a bar (or the cursor) picks it
DIM_ALPHA = 0.55             # the other bars while the cursor is on a run
BAND_ALPHA = 0.07            # behind a split instrument's parameter rows, in the instrument's colour
SCROLL_STEPS = 10000         # resolution of the time scroll bar
MIN_SPAN = 30.0              # the narrowest view, seconds
STRIP_PX = 20                # the strip of all runs under the rows, pixels
AXIS_PX = 36                 # the time axis under it (ticks and the date line), pixels
INSTRUMENT_ROW_PX = 30       # row heights, shared by the tree and the timeline
PARAMETER_ROW_PX = 24
FILTER_DELAY_MS = 300        # the filter applies this long after the last key
PANEL_DELAY_MS = 40          # while the cursor is dragged, the panel follows at most this often
CURSOR_HINT = "Click the timeline to place the cursor. Drag it, or step through the changes with ‹ ›."
AFTER_TITLES = ("Changed at run", "No change at run", "First run after")   # on a change, a run, neither
JITTER_TIP = "Jitter: how much the value wobbles from run to run when nobody changes it."
_RUNTIME_IMPORT = re.compile(r"^\s*from runtime import (\w+)", re.M)
_NO_ROW = object()
_FONT = []


def _colors(n, dark=False):
    series = SERIES_DARK if dark else SERIES_LIGHT
    return [series[i % len(series)] for i in range(n)]


def _number(value):
    return f"{value:.3g}"


def _value_text(value):
    return f"{value:.10g}" if isinstance(value, float) else str(value)


def _mix(a, b, f):
    """Colour ``a`` moved a fraction ``f`` towards ``b``, as hex."""
    a, b = QColor(a), QColor(b)
    return QColor(*(round(x + (y - x) * f) for x, y in
                    zip((a.red(), a.green(), a.blue()), (b.red(), b.green(), b.blue())))).name()


def _stamp(t, full=True):
    return time.strftime("%Y-%m-%d %H:%M:%S" if full else "%m-%d %H:%M:%S", time.localtime(t))


def _bars(x, y0, y1):
    """Vertical bars from ``y0`` (a number) to ``y1`` (an array) at ``x``, as one line broken by NaNs:
    a single path draws much faster than one per bar."""
    xs = np.repeat(np.asarray(x, dtype=float), 3)
    ys = np.empty_like(xs)
    ys[0::3] = y0
    ys[1::3] = y1
    xs[2::3] = ys[2::3] = np.nan
    return xs, ys


def _font_family():
    """The app's font family if matplotlib has it, so that the timeline reads like the window."""
    if not _FONT:
        from matplotlib import font_manager
        family = QApplication.font().family()
        try:
            font_manager.findfont(font_manager.FontProperties(family=family), fallback_to_default=False)
            _FONT.append(family)
        except Exception:
            _FONT.append(None)
    return _FONT[0]


def runtime_name(folder):
    """The runtime class a data folder ran, from the head of its run.py ('' if unknown)."""
    try:
        with open(os.path.join(folder, DATAFOLDER_INDICATOR_FILE), errors="replace") as f:
            match = _RUNTIME_IMPORT.search(f.read(4096))
    except OSError:
        return ""
    return match.group(1) if match else ""


class _Canvas(FigureCanvasQTAgg):
    """Draws in matplotlib's default style whatever style the rest of the app has switched to (the
    live plot uses a dark style with a grid); every colour of the timeline is set explicitly."""

    def draw(self):
        from matplotlib import style
        with style.context("default"):
            super().draw()


class _RowTree(QTreeWidget):
    """The names, with a rule under every row that the timeline's row rules continue."""

    rule = QColor("#e0e0e0")

    def drawRow(self, painter, option, index):
        super().drawRow(painter, option, index)
        painter.save()
        painter.setPen(QPen(self.rule, 1))
        y = option.rect.bottom()
        painter.drawLine(0, y, self.viewport().width(), y)
        painter.restore()


class _RowsSplitter(QSplitter):
    """The names and the timeline side by side, with no line between them: ``rules()`` gives the
    theme, the y of each row rule and the rows' tints, which the handle draws across the gap."""

    def __init__(self, rules):
        super().__init__(Qt.Horizontal)
        self.rules = rules

    def createHandle(self):
        return _RowsHandle(self.orientation(), self)


class _RowsHandle(QSplitterHandle):
    """Invisible but for the row rules running across it; a line shows while hovered or dragged."""

    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        self._hovered = self._pressed = False

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._pressed = True
        self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        th, ys, fills = self.splitter().rules()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(th["bg"]))
        for y, h, color in fills:
            painter.fillRect(0, y, self.width(), h, color)
        painter.setPen(QPen(QColor(th["rule"]), 1))
        for y in ys:
            painter.drawLine(0, y, self.width(), y)
        if self._hovered or self._pressed:
            painter.fillRect(self.width() // 2, 0, 1, self.height(), QColor(th["faint"]))
        painter.end()


class InstrumentTrackerWindow(QWidget):
    """The tracker's window. Left: the instruments, each expandable into its parameters (which also
    splits its timeline row), and the threshold of the selected parameters. Right: the timeline, a
    row per instrument (or parameter) and a strip of all runs; below it the cursor: its time, the
    runs on either side of it and the changes there."""

    tracked = pyqtSignal(object, str)             # record (or None), message
    progressed = pyqtSignal(str)

    def __init__(self, root, open_folder=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Instrument tracker")
        self.root = root
        self.open_folder = open_folder
        self.full_record = None         # every run, Trash included
        self.record = None              # what is shown
        self.events, self.info, self.param_events, self.events_at = {}, {}, {}, {}
        self.instruments, self.colors, self.rows = [], {}, []
        self.cursor = None              # {"time": seconds, "rel": the run it is on, or None}
        self.theme_name = None          # set by the browser; without it the palette decides
        self._view = None               # (lo, hi) in matplotlib date units
        self._times = np.zeros(0)
        self._run_of, self._row_y, self._art = {}, {}, {}
        self._change_runs = np.zeros(0, dtype=int)     # runs with a change in a shown instrument
        self.missing = {}               # instrument -> [[first run, stop], ...] it is not saved in
        self._stop = threading.Event()
        self._thread = None
        self._drag = None
        self._syncing = False
        self._restyling = True          # until built
        self._qss = None
        self._background = None         # the rendered figure, for drawing the hover line on top
        self._runtime_names = {}
        settings = load_user_config().get(CONFIG_KEY, {})
        self.k = float(settings.get("k", DEFAULT_K))
        self.hidden = set(settings.get("hidden", []))
        self.split = set(settings.get("split", []))
        self.hidden_parameters = set(settings.get("hidden_parameters", []))
        self.thresholds = {key: float(v) for key, v in settings.get("thresholds", {}).items()}
        self.skip_trash = bool(settings.get("skip_trash", False))
        self._build()
        self.tracked.connect(self._on_tracked)
        self.progressed.connect(self.status.setText)
        self._restyling = False

    @staticmethod
    def _tool_button(icon, tip, slot):
        button = QToolButton()
        button.setProperty("icon_name", icon)
        button.setIcon(get_icon(icon))
        button.setToolTip(tip)
        button.setAutoRaise(True)
        button.clicked.connect(slot)
        return button

    def _build(self):
        # top bar
        self.root_label = QLabel(self.root)
        self.root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.root_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.root_label.setToolTip(self.root)
        self.status = QLabel("")
        self.trash_box = QCheckBox("Skip Trash")
        self.trash_box.setToolTip("Leave out the runs in Trash folders (they are still tracked)")
        self.trash_box.setChecked(self.skip_trash)
        self.trash_box.toggled.connect(self._set_skip_trash)
        self.retrack_button = QPushButton("Re-track")
        self.retrack_button.setToolTip("Read every run's instrument parameters again")
        self.retrack_button.clicked.connect(lambda: self.start_tracking(full=True))
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.setToolTip("Show only the instruments and parameters whose name contains this")
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DELAY_MS)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.filter_box.textChanged.connect(lambda _text: self._filter_timer.start())
        self.filter_box.returnPressed.connect(self._apply_filter)
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.filter_box)
        top.addWidget(self.root_label, 1)
        top.addWidget(self.status)
        top.addWidget(self.trash_box)
        top.addWidget(self.retrack_button)

        # the rows: the tree is the timeline's row headers, row for row
        self.tree = _RowTree()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["instrument", "changes"])
        self.tree.headerItem().setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(False)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # a bar would shift the rows
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)     # its bar is on the left, below
        tree_bar = self.tree.verticalScrollBar()
        tree_bar.valueChanged.connect(lambda _v: self._follow_tree())
        self.row_scrollbar = QScrollBar(Qt.Vertical)
        policy = self.row_scrollbar.sizePolicy()
        policy.setRetainSizeWhenHidden(True)            # the names don't move when it comes and goes
        self.row_scrollbar.setSizePolicy(policy)
        self.row_scrollbar.hide()
        tree_bar.rangeChanged.connect(self._sync_row_scrollbar)
        tree_bar.valueChanged.connect(self.row_scrollbar.setValue)
        self.row_scrollbar.valueChanged.connect(tree_bar.setValue)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._tree_checked)
        self.tree.itemExpanded.connect(lambda item: self._set_split(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._set_split(item, False))
        self.tree.itemSelectionChanged.connect(self._selection_changed)

        self.threshold_title = QLabel("Threshold")
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        self.selection_label.setToolTip(JITTER_TIP)
        self.threshold_edit = QLineEdit()
        self.threshold_edit.setPlaceholderText("threshold")
        self.threshold_edit.returnPressed.connect(self._set_threshold)
        self.set_button = QPushButton("Set")
        self.set_button.setToolTip("Use this threshold for the selected parameters")
        self.set_button.clicked.connect(self._set_threshold)
        self.default_button = QPushButton("Default")
        self.default_button.setToolTip("Back to the default threshold")
        self.default_button.clicked.connect(self._default_threshold)
        self.k_box = QDoubleSpinBox()
        self.k_box.setRange(1.0, 1000.0)
        self.k_box.setDecimals(1)
        self.k_box.setValue(self.k)
        self.k_box.setSuffix(" × jitter")
        self.k_box.setToolTip("The threshold of parameters not set by hand.\n" + JITTER_TIP)
        self.k_box.valueChanged.connect(self._set_k)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.threshold_edit, 1)
        edit_row.addWidget(self.set_button)
        edit_row.addWidget(self.default_button)
        default_row = QHBoxLayout()
        self.default_label = QLabel("Default")
        self.default_label.setToolTip(self.k_box.toolTip())
        default_row.addWidget(self.default_label)
        default_row.addWidget(self.k_box, 1)
        self.runs_label = QLabel("runs")
        self.runs_label.setFixedHeight(STRIP_PX)
        self.runs_label.setIndent(24)
        self.tree_footer = QWidget()                    # as tall as the runs strip, axis and scroll bar
        footer = QVBoxLayout(self.tree_footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.runs_label)
        footer.addStretch(1)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self.tree, 1)
        names.addWidget(self.tree_footer)
        self.row_bar_top = QWidget()                    # the bar spans the rows only
        self.row_bar_bottom = QWidget()
        bar = QVBoxLayout()
        bar.setSpacing(0)
        bar.addWidget(self.row_bar_top)
        bar.addWidget(self.row_scrollbar, 1)
        bar.addWidget(self.row_bar_bottom)
        headers = QWidget()
        hl = QHBoxLayout(headers)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(2)
        hl.addLayout(bar)
        hl.addLayout(names, 1)
        headers.setMinimumWidth(200)
        headers.setObjectName("surface")
        editor = QWidget()
        side = QVBoxLayout(editor)
        side.setContentsMargins(0, 4, 0, 0)
        side.setSpacing(6)
        side.addWidget(self.threshold_title)
        side.addWidget(self.selection_label)
        side.addLayout(edit_row)
        side.addLayout(default_row)
        side.addStretch(1)

        # the timeline and its scroll bar
        self.canvas = _Canvas(Figure())
        self.canvas.setFocusPolicy(Qt.StrongFocus)
        self.canvas.setMouseTracking(True)
        self.ax = self.canvas.figure.add_axes([0, 0.1, 1, 0.8])
        self.strip_ax = self.canvas.figure.add_axes([0, 0, 1, 0.1], sharex=self.ax)
        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.valueChanged.connect(self._scrolled)
        self.fit_button = self._tool_button("fit_width.svg", "Reset zoom: the whole time range "
                                            "(or double-click an empty spot)", self._fit)
        self.fit_button.setParent(self.canvas)          # floats in the plot's top right corner
        self.fit_button.hide()
        timeline = QWidget()
        timeline.setObjectName("surface")
        tl = QVBoxLayout(timeline)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addWidget(self.canvas, 1)
        tl.addWidget(self.scrollbar)

        # the cursor: its time, the runs on either side, the changes there
        self.prev_button = self._tool_button("chevron_left.svg", "Previous change (←)", lambda: self._step(-1))
        self.next_button = self._tool_button("chevron_right.svg", "Next change (→)", lambda: self._step(1))
        for button in (self.prev_button, self.next_button):
            button.setObjectName("framed")
            button.setAutoRaise(False)
        self.cursor_label = QLabel()
        head = QHBoxLayout()
        head.setSpacing(2)
        head.addWidget(self.prev_button)
        head.addWidget(self.next_button)
        head.addSpacing(8)
        head.addWidget(self.cursor_label, 1)
        runs = QGridLayout()
        runs.setHorizontalSpacing(12)
        runs.setVerticalSpacing(0)
        runs.setColumnStretch(1, 1)
        self.run_labels, self.run_buttons, self.run_titles = [], [], []
        for row, (title, tip) in enumerate((
                ("Last run before", "The last run started before the cursor, with the old settings"),
                (AFTER_TITLES[2], "The first run at or after the cursor, with the new settings"))):
            title_label = QLabel(title)
            title_label.setToolTip(tip)
            label = QLabel()
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            button = QPushButton("Go to run")
            button.setToolTip("Show this run in the data browser")
            button.clicked.connect(lambda _checked=False, i=row: self._open_run(i))
            runs.addWidget(title_label, row, 0)
            runs.addWidget(label, row, 1)
            runs.addWidget(button, row, 2)
            self.run_titles.append(title_label)
            self.run_labels.append(label)
            self.run_buttons.append(button)
        self._run_targets = [None, None]
        self.changes = QTableWidget(0, 5)
        self.changes.setHorizontalHeaderLabels(["instrument", "parameter", "before", "after", "change"])
        self.changes.verticalHeader().setVisible(False)
        self.changes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.changes.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.changes.setFocusPolicy(Qt.NoFocus)
        self.changes.setShowGrid(False)
        self.changes.setWordWrap(False)
        self.changes.setFrameShape(QFrame.NoFrame)
        header = self.changes.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setHighlightSections(False)
        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 4, 0, 0)
        pl.setSpacing(4)
        pl.addLayout(head)
        pl.addLayout(runs)
        pl.addSpacing(4)
        pl.addWidget(self.changes, 1)

        self.upper = _RowsSplitter(self._row_rules)
        self.upper.addWidget(headers)
        self.upper.addWidget(timeline)
        self.lower = QSplitter(Qt.Horizontal)
        self.lower.addWidget(editor)
        self.lower.addWidget(panel)
        for splitter in (self.upper, self.lower):
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([290, 1010])
            splitter.setHandleWidth(10)
        self.upper.setObjectName("rows")
        self.upper.setHandleWidth(3)            # the names and their rows: one table, a hairline between
        self.upper.splitterMoved.connect(lambda *_a: self._columns_moved(self.upper))
        self.lower.splitterMoved.connect(lambda *_a: self._columns_moved(self.lower))
        self.filter_box.setFixedWidth(290)
        outer = QSplitter(Qt.Vertical)
        outer.addWidget(self.upper)
        outer.addWidget(self.lower)
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 2)
        outer.setHandleWidth(10)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(outer, 1)
        self.resize(1300, 850)

        self._panel_timer = QTimer(self)
        self._panel_timer.setSingleShot(True)
        self._panel_timer.setInterval(PANEL_DELAY_MS)
        self._panel_timer.timeout.connect(self._show_cursor)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("key_press_event", self._on_key)
        self.canvas.mpl_connect("figure_leave_event", lambda _e: self._hide_hover())
        self.canvas.mpl_connect("resize_event", lambda _e: self._layout())
        self.canvas.mpl_connect("draw_event", self._on_drawn)
        self._apply_style()
        self._build_plot()
        self._selection_changed()
        self._show_cursor()

    def _columns_moved(self, splitter):
        """The names column is one width everywhere: the tree, the threshold editor, the filter."""
        sizes = splitter.sizes()
        for other in (self.upper, self.lower):
            if other is not splitter:
                other.setSizes(sizes)
        self.filter_box.setFixedWidth(max(sizes[0], 120))

    # ---- tracking
    def start_tracking(self, full=False):
        """Track in a background thread: from the saved record, or everything with ``full``."""
        if self._thread is not None and self._thread.is_alive():
            return
        self.retrack_button.setEnabled(False)
        self._stop.clear()
        previous = None if full else load_record(self.root)
        root, stop = self.root, self._stop

        def work():
            t0 = time.perf_counter()
            try:
                record = track(root, previous, progress=self._emit_progress, stop_event=stop)
            except Exception as e:
                logger.error(f"Instrument tracking failed: {e}", exc_info=True)
                self._emit(None, f"Tracking failed: {type(e).__name__}: {e}")
                return
            if record is None:
                return
            error = save_record(root, record)
            took = time.perf_counter() - t0
            note = f"; could not save the record ({error})" if error else ""
            self._emit(record, f"{len(record['runs'])} runs · tracked in {took:.1f} s{note}")
        self._thread = threading.Thread(target=work, name="instrument-tracker", daemon=True)
        self._thread.start()

    def _emit(self, record, message):
        try:
            self.tracked.emit(record, message)
        except RuntimeError:
            pass                        # the window was closed meanwhile

    def _emit_progress(self, text):
        try:
            self.progressed.emit(text + " ...")
        except RuntimeError:
            pass

    def _on_tracked(self, record, message):
        self.retrack_button.setEnabled(True)
        self.status.setText(message)
        if record is None:
            return
        self.full_record = record
        self._analyse()

    def closeEvent(self, event):
        self._stop.set()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._qss = None                # a stylesheet set while hidden does not reach styled children
        self._restyling = True
        try:
            self._apply_style()
        finally:
            self._restyling = False

    # ---- theme
    def set_theme(self, theme_name):
        """Follow the app's light/dark theme (a name with "dark" in it is dark)."""
        self.theme_name = theme_name
        self._restyle()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.StyleChange) and not self._restyling:
            self._restyling = True                  # one redraw for a burst of changes
            QTimer.singleShot(0, self._restyle)

    def _restyle(self):
        self._restyling = True
        try:
            self._apply_style()
            self._build_plot()
            self._show_cursor()
        finally:
            self._restyling = False

    def _theme(self):
        if self.theme_name is not None:
            dark = "dark" in self.theme_name.lower()
        else:
            dark = self.palette().color(self.palette().Window).lightness() < 128
        th = dict(THEMES["dark" if dark else "light"], dark=dark)
        fg, bg = th["fg"], th["bg"]
        th.update(muted=_mix(fg, bg, 0.45), faint=_mix(fg, bg, 0.65), edge=_mix(fg, bg, 0.75),
                  rule=_mix(fg, bg, 0.88), hover=_mix(fg, bg, 0.86), pressed=_mix(fg, bg, 0.78),
                  selected=_mix(th["series"][0], bg, 0.8),
                  up=th["series"][0], down=th["series"][1])
        return th

    def _apply_style(self):
        """The window's own touches on top of the app's stylesheet: quiet headers and selection,
        flat tool buttons, secondary text in a muted colour, icons in the theme's colour."""
        th = self._theme()
        qss = f"""
            QTreeWidget, QTableWidget {{ show-decoration-selected: 0; selection-color: {th['fg']};
                selection-background-color: {th['selected']}; }}
            QTreeWidget::item:selected, QTableWidget::item:selected {{
                background: {th['selected']}; color: {th['fg']}; }}
            QScrollBar:horizontal {{ height: 13px; background: {th['rule']}; border: none; border-radius: 6px;
                margin: 2px 0; }}
            QScrollBar::handle:horizontal {{ background: {th['faint']}; border-radius: 6px; min-width: 32px; }}
            QScrollBar::handle:horizontal:hover {{ background: {th['muted']}; }}
            QScrollBar::handle:horizontal:disabled {{ background: {th['rule']}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; border: none;
                background: none; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}
            QScrollBar:vertical {{ width: 13px; background: {th['rule']}; border: none; border-radius: 6px;
                margin: 0 2px; }}
            QScrollBar::handle:vertical {{ background: {th['faint']}; border-radius: 6px; min-height: 32px; }}
            QScrollBar::handle:vertical:hover {{ background: {th['muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none;
                background: none; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
            QHeaderView::section {{ background: transparent; color: {th['muted']}; border: none;
                border-bottom: 1px solid {th['rule']}; padding: 3px 6px; }}
            QToolButton {{ border: none; border-radius: 4px; padding: 3px; background: transparent; }}
            QToolButton:hover {{ background: {th['hover']}; }}
            QToolButton:pressed {{ background: {th['pressed']}; }}
            QToolButton#framed, QPushButton {{ border: 1px solid {th['edge']}; border-radius: 4px;
                background: {th['bg']}; color: {th['fg']}; }}
            QToolButton#framed {{ padding: 3px; }}
            QPushButton {{ padding: 3px 12px; min-height: 18px; }}
            QToolButton#framed:hover, QPushButton:hover {{ background: {th['hover']}; border-color: {th['faint']}; }}
            QToolButton#framed:pressed, QPushButton:pressed {{ background: {th['pressed']}; }}
            QToolButton#framed:disabled, QPushButton:disabled {{ color: {th['faint']}; border-color: {th['rule']};
                background: transparent; }}
            QLabel#muted {{ color: {th['muted']}; }}
            QWidget#surface, QWidget#surface QTreeWidget, QWidget#surface QHeaderView {{ background: {th['bg']};
                border: none; }}
        """
        for label in [self.root_label, self.status, self.default_label, self.threshold_title] + self.run_titles:
            label.setObjectName("muted")
        width = max(self.run_titles[0].fontMetrics().horizontalAdvance(text)
                    for text in (self.run_titles[0].text(),) + AFTER_TITLES)
        for label in self.run_titles:           # the runs stay put when the title changes
            label.setMinimumWidth(width)
        if qss != self._qss:
            self._qss = qss
            self.setStyleSheet(qss)
        for button in self.findChildren(QToolButton):
            name = button.property("icon_name")
            if name:
                button.setIcon(get_icon(name))
        self.tree_footer.setFixedHeight(STRIP_PX + AXIS_PX + self.scrollbar.sizeHint().height())
        self.row_bar_bottom.setFixedHeight(self.tree_footer.height())
        self.fit_button.setFixedSize(26, 26)
        self.fit_button.setIconSize(QSize(18, 18))

    # ---- analysis
    def _analyse(self):
        """Find the changes in what is shown; keeps the view, the cursor and the selection."""
        self.record = without_trash(self.full_record) if self.skip_trash else self.full_record
        self.events, self.info = find_changes(self.record, self.k, self.thresholds)
        if self.hidden_parameters:                  # unticked parameters: their changes are left out
            for name in list(self.events):
                kept = []
                for event in self.events[name]:
                    changes = [c for c in event["changes"] if c[0] not in self.hidden_parameters]
                    if changes:
                        kept.append({**event, "changes": changes,
                                     "height": max(bar_height(c[3]) for c in changes)})
                self.events[name] = kept
        n_runs = len(self.record["runs"])
        self.missing = {name: g for name, spans in self.record["presence"].items() if (g := gaps(spans, n_runs))}
        self.param_events, self.events_at = {}, {}
        for name, events in self.events.items():
            for event in events:
                for change in event["changes"]:
                    self.events_at.setdefault(event["run"], []).append((name, *change))
                    if not change[0].endswith(PRESENCE):
                        self.param_events.setdefault(change[0], []).append(
                            {"run": event["run"], "height": bar_height(change[3]), "changes": [change]})
        self.instruments = sorted({key.split(".", 1)[0] for key in self.record["series"]})
        self._parameters_of = {}
        for key in sorted(self.record["series"]):
            self._parameters_of.setdefault(key.split(".", 1)[0], []).append(key)
        self._times = np.array([run[1] for run in self.record["runs"]], dtype=float)
        self._run_of = {run[0]: i for i, run in enumerate(self.record["runs"])}
        if self._view is None and len(self._times):
            self._view = self._full_range()
        self._fill_tree()
        self._build_plot()
        self._show_cursor()

    def _reanalyse(self):
        if self.full_record is not None:
            self._analyse()

    # ---- sidebar
    def _fill_tree(self):
        selected = {item.data(0, Qt.UserRole) for item in self.tree.selectedItems()}
        scroll = self.tree.verticalScrollBar().value()
        warning = self.style().standardIcon(QStyle.SP_MessageBoxWarning)
        right = Qt.AlignRight | Qt.AlignVCenter
        self.tree.blockSignals(True)
        self.tree.clear()
        for name in self.instruments:
            item = QTreeWidgetItem([name, str(len(self.events.get(name, [])))])
            item.setData(0, Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked if name in self.hidden else Qt.Checked)
            item.setTextAlignment(1, right)
            item.setSizeHint(0, QSize(0, INSTRUMENT_ROW_PX))
            tip = f"{name}\nTick to show it, expand it to split its row into its parameters."
            missing = sum(b - a for a, b in self.missing.get(name, []))
            if missing:
                tip += f"\nNot saved in {missing} of {len(self.record['runs'])} runs (hatched)."
            item.setToolTip(0, tip)
            keys = sorted(key for key, entry in self.info.items() if key.split(".", 1)[0] == name
                          and (entry["changes"] or entry["jitter"] > 0 or entry["set"]))
            for key in keys:
                entry = self.info[key]
                child = QTreeWidgetItem([key.split(".", 1)[1], str(entry["changes"])])
                child.setData(0, Qt.UserRole, key)
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked if key in self.hidden_parameters else Qt.Checked)
                child.setTextAlignment(1, right)
                child.setSizeHint(0, QSize(0, PARAMETER_ROW_PX))
                child.setToolTip(0, self._parameter_tip(key))
                if entry["set"]:
                    font = child.font(0)
                    font.setBold(True)
                    child.setFont(0, font)
                if not entry["reliable"]:
                    child.setIcon(0, warning)
                item.addChild(child)
            self.tree.addTopLevelItem(item)           # expanding and selecting work once it is in the tree
            item.setExpanded(name in self.split)
            item.setSelected(name in selected)
            for j in range(item.childCount()):
                item.child(j).setSelected(item.child(j).data(0, Qt.UserRole) in selected)
        self.tree.blockSignals(False)
        self._apply_filter(rebuild=False)
        self.tree.verticalScrollBar().setValue(scroll)
        self._selection_changed()

    def _how(self, key):
        """When a change of ``key`` counts, in a few words."""
        entry = self.info[key]
        if not entry["numeric"]:
            return "every change counts (not a number)"
        if entry["set"]:
            return f"threshold {_number(entry['threshold'])}, set by hand"
        if entry["jitter"] > 0:
            return f"threshold {_number(entry['threshold'])} = {self.k:g} × jitter"
        return "every change counts (it never wobbles)"

    def _parameter_tip(self, key):
        entry = self.info[key]
        lines = [key, self._how(key)]
        if key in self.hidden_parameters:
            lines.append("Unticked: its changes are left out.")
        if entry["numeric"] and entry["jitter"] > 0:
            lines.append(f"jitter {_number(entry['jitter'])}. {JITTER_TIP}")
        if not entry["reliable"]:
            lines.append("It changes often, so its jitter may be overestimated: set a threshold by hand.")
        return "\n".join(lines)

    def _apply_filter(self, rebuild=True):
        """Show the instruments and parameters matching the filter, in the tree and the timeline."""
        self._filter_timer.stop()
        text = self.filter_box.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            own = not text or text in item.text(0).lower()
            shown = own
            for j in range(item.childCount()):
                child = item.child(j)
                match = own or text in child.data(0, Qt.UserRole).lower()
                child.setHidden(not match)
                shown = shown or match
            item.setHidden(not shown)
        if rebuild and self.record is not None:
            self._build_plot()

    def _tree_checked(self, item, column):
        if column != 0:
            return
        name = item.data(0, Qt.UserRole)
        if item.parent() is not None:               # a parameter: its changes are counted or left out
            if item.checkState(0) == Qt.Checked:
                self.hidden_parameters.discard(name)
            else:
                self.hidden_parameters.add(name)
            self._save_settings()
            QTimer.singleShot(0, self._reanalyse)   # not while the tree is still in its signal
            return
        if item.checkState(0) == Qt.Checked:
            self.hidden.discard(name)
        else:
            self.hidden.add(name)
        self._save_settings()
        self._build_plot()
        self._show_cursor()

    def _set_split(self, item, on):
        if item.parent() is not None:
            return
        name = item.data(0, Qt.UserRole)
        (self.split.add if on else self.split.discard)(name)
        self._save_settings()
        self._build_plot()

    def _selected_parameters(self):
        """The numeric parameters selected in the tree (an instrument stands for all of its own)."""
        keys = []
        for item in self.tree.selectedItems():
            if item.parent() is None:
                keys += [item.child(j).data(0, Qt.UserRole) for j in range(item.childCount())]
            else:
                keys.append(item.data(0, Qt.UserRole))
        return [key for key in dict.fromkeys(keys) if self.info.get(key, {}).get("numeric")]

    def _selection_changed(self):
        if hasattr(self, "_selection"):
            self._paint_selection()
        th = self._theme()
        keys = self._selected_parameters()
        for widget in (self.threshold_edit, self.set_button):
            widget.setEnabled(bool(keys))
        self.default_button.setEnabled(any(self.info[key]["set"] for key in keys))
        if not keys:
            self.selection_label.setText(f"<span style='color:{th['muted']}'>Select parameters to set "
                                         "their threshold.</span>")
            self.threshold_edit.clear()
            return
        limits = {_number(self.info[key]["threshold"]) for key in keys}
        n_set = sum(self.info[key]["set"] for key in keys)
        if len(keys) == 1:
            entry = self.info[keys[0]]
            jitter = f"jitter {_number(entry['jitter'])} · " if entry["jitter"] > 0 else ""
            title, detail = keys[0], jitter + self._how(keys[0])
        else:
            parts = [f"{len(keys) - n_set} default"] if len(keys) - n_set else []
            if n_set:
                parts.append(f"{n_set} set by hand")
            title, detail = f"{len(keys)} parameters", ", ".join(parts)
        self.selection_label.setText(f"{title}<br><span style='color:{th['muted']}'>{detail}</span>")
        self.threshold_edit.setText(next(iter(limits)) if len(limits) == 1 else "")
        self.threshold_edit.setPlaceholderText("mixed" if len(limits) > 1 else "threshold")

    def _set_threshold(self):
        keys = self._selected_parameters()
        try:
            value = float(self.threshold_edit.text())
        except ValueError:
            value = 0.0
        if not keys or not value > 0:
            self.selection_label.setText("The threshold must be a positive number.")
            return
        self.thresholds.update({key: value for key in keys})
        self._save_settings()
        self._reanalyse()

    def _default_threshold(self):
        for key in self._selected_parameters():
            self.thresholds.pop(key, None)
        self._save_settings()
        self._reanalyse()

    def _set_k(self, value):
        self.k = float(value)
        self._save_settings()
        self._reanalyse()

    def _set_skip_trash(self, checked):
        self.skip_trash = bool(checked)
        self._save_settings()
        self._reanalyse()

    def _save_settings(self):
        update_user_config(**{CONFIG_KEY: {"k": self.k, "hidden": sorted(self.hidden),
                                           "split": sorted(self.split), "skip_trash": self.skip_trash,
                                           "hidden_parameters": sorted(self.hidden_parameters),
                                           "thresholds": self.thresholds}})

    # ---- drawing
    def _swatch(self, name):
        pixmap = QPixmap(10, 10)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.colors.get(name, "#888888")))
        painter.drawRoundedRect(0, 0, 10, 10, 2.5, 2.5)
        painter.end()
        return QIcon(pixmap)

    def _visible_items(self):
        """The tree's shown rows, top to bottom: ``[(item, (instrument, parameter or None)), ...]``."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.isHidden():
                continue
            name = item.data(0, Qt.UserRole)
            out.append((item, (name, None)))
            if item.isExpanded():
                for j in range(item.childCount()):
                    child = item.child(j)
                    if not child.isHidden():
                        out.append((child, (name, child.data(0, Qt.UserRole))))
        return out

    def _row_spans(self):
        """Each shown row's (top, bottom) in the tree's content pixels: the timeline's y coordinate,
        so that a timeline row sits exactly beside its name."""
        self.tree.executeDelayedItemsLayout()
        scroll = self.tree.verticalScrollBar().value()
        spans = {}
        for item, row in self._visible_items():
            rect = self.tree.visualItemRect(item)
            spans[row] = (rect.top() + scroll, rect.bottom() + 1 + scroll)
        return spans

    def _paint_tree(self, th):
        """The tree's colours: a swatch per instrument, parameter rows on their instrument's band,
        unticked rows faint."""
        self.tree.blockSignals(True)            # a new icon is an itemChanged too
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            name = item.data(0, Qt.UserRole)
            item.setIcon(0, self._swatch(name))
            item.setForeground(0, QColor(th["faint"] if name in self.hidden else th["fg"]))
            band = QColor(self.colors.get(name, "#888888"))
            band.setAlphaF(BAND_ALPHA)
            for j in range(item.childCount()):
                child = item.child(j)
                off = name in self.hidden or child.data(0, Qt.UserRole) in self.hidden_parameters
                for column in (0, 1):
                    child.setBackground(column, band)
                    child.setForeground(column, QColor(th["faint"] if off else th["fg"]))
        self.tree.blockSignals(False)
        self.runs_label.setStyleSheet(f"color: {th['muted']}; border: none; border-bottom: 1px solid {th['rule']};")
        self.tree.rule = QColor(th["rule"])
        self.tree.viewport().update()

    def _build_plot(self):
        """Draw the timeline from scratch: when the rows, the theme or the record change. The cursor,
        the hover line, the selection and the tree's scrolling only move their own artists."""
        from matplotlib import style
        with style.context("default"):          # whatever style the rest of the app has switched to
            self._draw_rows()
        self._layout()
        self._sync_scrollbar()

    def _draw_rows(self):
        from matplotlib import dates as mdates
        from matplotlib.collections import PolyCollection
        from matplotlib.transforms import Bbox, TransformedBbox, blended_transform_factory
        th = self._theme()
        family = _font_family()
        size = QApplication.font().pointSizeF()
        size = size if size > 0 else 9.0
        self.colors = dict(zip(self.instruments, _colors(len(self.instruments), th["dark"])))
        self._paint_tree(th)

        ax, strip = self.ax, self.strip_ax
        ax.clear()
        strip.clear()
        self.canvas.figure.set_facecolor(th["bg"])
        for a in (ax, strip):
            a.set_facecolor(th["bg"])
            a.grid(False)
            for spine in a.spines.values():
                spine.set_visible(False)
        spans = self._row_spans()
        self._row_span = spans
        self.rows = list(spans) + [None]                    # None: the strip of all runs
        self._row_y = {row: (a + b) / 2 for row, (a, b) in spans.items()}
        days = self._times / 86400.0
        line = {"solid_capstyle": "butt", "solid_joinstyle": "miter"}
        px = 72.0 * self.canvas.device_pixel_ratio / self.canvas.figure.dpi     # one pixel, in points
        self._header_rule = ax.plot([0, 1], [1, 1], color=th["rule"], linewidth=px, transform=ax.transAxes,
                                    clip_on=False, snap=False)[0]   # the tree header's rule, continued
        self._art = {}
        for row, (y_top, y_bottom) in spans.items():
            name, key = row
            ax.axhline(y_bottom - 0.5, color=th["rule"], linewidth=px, snap=False, zorder=0.5)   # the row's last pixel
            if key is not None:
                ax.axhspan(y_top, y_bottom, color=self.colors[name], alpha=BAND_ALPHA, linewidth=0, zorder=0)
            gap_spans = self._gap_days(name)
            if gap_spans:                                   # not saved in these runs: hatched
                ax.add_collection(PolyCollection(
                    [[(x0, y_top), (x1, y_top), (x1, y_bottom), (x0, y_bottom)] for x0, x1 in gap_spans],
                    facecolors="none", edgecolors=th["faint"], hatch="////", linewidths=0, zorder=0.4))
            shown = name not in self.hidden and (key is None or key not in self.hidden_parameters)
            events = (self.events.get(name, []) if key is None else self.param_events.get(key, [])) if shown else []
            runs = np.array([e["run"] for e in events], dtype=int)
            base_y = y_bottom - 3.0
            top = base_y - np.array([e["height"] for e in events]) * (y_bottom - y_top - 6.0)
            x = days[runs] if len(runs) else np.zeros(0)
            color = self.colors[name]
            base, = ax.plot(*_bars(x, base_y, top), color=color, linewidth=2.2, **line)
            hi, = ax.plot([], [], color=color, linewidth=3.6, **line)
            self._art[row] = {"base": base, "hi": hi, "x": x, "y0": base_y, "top": top, "runs": runs}
        base, = strip.plot(*_bars(days, 0.15, np.full(len(days), 0.85)), color=th["faint"], linewidth=0.6, **line)
        hi, = strip.plot([], [], color=th["fg"], linewidth=2.0, **line)
        self._art[None] = {"base": base, "hi": hi, "x": days, "y0": 0.15, "top": np.full(len(days), 0.85),
                           "runs": np.arange(len(days))}
        shown = [name for name in self.instruments if name not in self.hidden]
        self._change_runs = np.array(sorted({e["run"] for name in shown for e in self.events.get(name, [])}),
                                     dtype=int)
        self._selection = PolyCollection([], facecolors=th["selected"], linewidths=0, zorder=0.2)
        ax.add_collection(self._selection)

        ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
        strip.tick_params(left=False, labelleft=False)
        strip.set_ylim(0, 1)
        strip.plot([0, 1], [0.5 / STRIP_PX] * 2, color=th["rule"], linewidth=px, transform=strip.transAxes,
                   clip_on=False, snap=False)             # its last pixel row, like the rows' rules
        above = TransformedBbox(Bbox([[0, -1], [1, 2]]), ax.transAxes)   # the axes and the band above
        on_top = blended_transform_factory(ax.transData, ax.transAxes)
        text = {"fontsize": size - 1.5, "ha": "center", "va": "bottom", "transform": on_top, "in_layout": False}
        if family:
            text["fontfamily"] = family
        self._cursor_line = ax.axvline(0, color=th["muted"], linewidth=1.0, zorder=3, visible=False)
        self._cursor_strip = strip.axvline(0, color=th["muted"], linewidth=1.0, zorder=3, visible=False)
        self._cursor_label = ax.text(0, 1.0, "", color=th["bg"], visible=False, zorder=4, **text,
                                     bbox={"boxstyle": "round,pad=0.3,rounding_size=0.5", "fc": th["muted"],
                                           "ec": "none"})
        self._cursor_label.set_clip_box(above)
        self._cursor_label.set_y(1.0 + 4.0 / max(self._axes_px(), 1.0))
        self._hover_line = ax.axvline(0, color=th["faint"], linewidth=0.8, zorder=2.5, visible=False,
                                      animated=True)
        self._hover_strip = strip.axvline(0, color=th["faint"], linewidth=0.8, zorder=2.5, visible=False,
                                          animated=True)
        self._hover_label = ax.text(0, self._cursor_label.get_position()[1], "", color=th["muted"],
                                    visible=False, animated=True, **text)
        self._hover_label.set_clip_box(above)
        strip.xaxis_date()
        strip.xaxis.set_major_formatter(mdates.ConciseDateFormatter(strip.xaxis.get_major_locator()))
        strip.tick_params(axis="x", length=3, colors=th["rule"], labelcolor=th["muted"], labelsize=size - 1.5)
        offset = strip.xaxis.get_offset_text()
        offset.set_fontsize(size - 1.5)
        offset.set_color(th["muted"])
        for label in strip.get_xticklabels() + [offset]:
            if family:
                label.set_fontfamily(family)
        if self._view is not None:
            ax.set_xlim(*self._view)
        self._paint_selection(draw=False)
        self._update_cursor(draw=False)

    def _gap_days(self, name):
        """The spans an instrument is missing, as (from, to) in date units: from its last saved run
        before the gap to its first one after (the ends of the time range at the edges)."""
        days = self._times / 86400.0
        if not len(days):
            return []
        lo, hi = self._full_range()
        n = len(days)
        return [(days[a - 1] if a > 0 else lo, days[b] if b < n else hi) for a, b in self.missing.get(name, [])]

    def _paint_selection(self, draw=True):
        """The rows selected in the tree, tinted across the timeline too."""
        selected = {item.data(0, Qt.UserRole) for item in self.tree.selectedItems()}
        lo, hi = -1e9, 1e9
        verts = [[(lo, a), (hi, a), (hi, b), (lo, b)] for row, (a, b) in getattr(self, "_row_span", {}).items()
                 if (row[1] or row[0]) in selected]
        self._selection.set_verts(verts)
        self.upper.handle(1).update()
        if draw:
            self.canvas.draw_idle()

    def _update_cursor(self, draw=True):
        """Move the cursor's artists and bring out the bars of its run, dimming the others."""
        on = self._cursor_run()
        for row, art in self._art.items():
            art["base"].set_alpha(1.0 if on is None or row is None else DIM_ALPHA)
            mask = art["runs"] == on if on is not None else None
            if mask is not None and mask.any():
                art["hi"].set_data(*_bars(art["x"][mask], art["y0"], art["top"][mask]))
            else:
                art["hi"].set_data([], [])
        visible = self.cursor is not None
        for artist in (self._cursor_line, self._cursor_strip, self._cursor_label):
            artist.set_visible(visible)
        if visible:
            x = self.cursor["time"] / 86400.0
            self._cursor_line.set_xdata([x, x])
            self._cursor_strip.set_xdata([x, x])
            self._cursor_label.set_x(x)
            self._cursor_label.set_text(_stamp(self.cursor["time"], full=False))
        if draw:
            self.canvas.draw_idle()

    def _header_px(self):
        header = self.tree.header()
        return header.height() if header.height() > 0 else header.sizeHint().height()

    def _axes_px(self):
        return max(self.canvas.height() - STRIP_PX - AXIS_PX - self._header_px(), 1)

    def _layout(self):
        """Place the rows' axes beside the tree's rows (the band above them, as tall as the tree's
        header, holds the cursor's time), the runs strip and the time axis under them."""
        width, height = max(self.canvas.width(), 1), max(self.canvas.height(), 1)
        left, right = 0.0, 10.0                 # the row rules meet the names' rules
        w = 1 - (left + right) / width
        self.ax.set_position([left / width, (STRIP_PX + AXIS_PX) / height, w, self._axes_px() / height])
        self.strip_ax.set_position([left / width, AXIS_PX / height, w, STRIP_PX / height])
        y = 1.0 + 4.0 / self._axes_px()
        if hasattr(self, "_cursor_label"):
            self._cursor_label.set_y(y)
            self._hover_label.set_y(y)
            rule = 1.0 + 0.5 / self._axes_px()          # the pixel row just above the axes
            self._header_rule.set_ydata([rule, rule])
        self.row_bar_top.setFixedHeight(self._header_px())
        self.fit_button.move(width - self.fit_button.width() - 4, max(0, (self._header_px() - self.fit_button.height()) // 2))
        self._follow_tree(draw=False)
        self.canvas.draw_idle()

    def _follow_tree(self, draw=True):
        """Scroll the rows with the tree: the y range is the tree's visible content pixels."""
        scroll = self.tree.verticalScrollBar().value()
        self.ax.set_ylim(scroll + self._axes_px(), scroll)
        if draw:
            self.canvas.draw_idle()
        if hasattr(self, "upper"):
            self.upper.handle(1).update()

    def _row_rules(self):
        """For the edge between the names and the timeline: the theme, the y of the rules (under the
        header, each visible row and the runs row), and the rows' tints (parameter bands, the selection)."""
        th = self._theme()
        viewport = self.tree.viewport()
        top, height = viewport.mapTo(self.upper, QPoint(0, 0)).y(), viewport.height()
        ys, fills = [top - 1], []
        for item, (name, key) in self._visible_items():
            rect = self.tree.visualItemRect(item)
            if rect.bottom() < 0 or rect.top() >= height:
                continue
            if item.isSelected():
                fills.append((top + rect.top(), rect.height(), QColor(th["selected"])))
            elif key is not None:
                band = QColor(getattr(self, "colors", {}).get(name, "#888888"))
                band.setAlphaF(BAND_ALPHA)
                fills.append((top + rect.top(), rect.height(), band))
            if rect.bottom() < height:
                ys.append(top + rect.bottom())
        ys.append(self.tree_footer.mapTo(self.upper, QPoint(0, 0)).y() + STRIP_PX - 1)   # under the runs
        return th, ys, fills

    def _sync_row_scrollbar(self, lo, hi):
        tree_bar = self.tree.verticalScrollBar()
        self.row_scrollbar.setRange(lo, hi)
        self.row_scrollbar.setPageStep(tree_bar.pageStep())
        self.row_scrollbar.setSingleStep(tree_bar.singleStep())
        self.row_scrollbar.setValue(tree_bar.value())
        self.row_scrollbar.setVisible(hi > lo)

    def _on_drawn(self, _event):
        """Keep the rendered figure for the hover line, and draw the line into this frame (the paint
        that follows shows it; blitting here would repaint inside a paint)."""
        self._background = self.canvas.copy_from_bbox(self.canvas.figure.bbox)
        self._draw_hover()

    def _draw_hover(self):
        if self._hover_line.get_visible():
            self.ax.draw_artist(self._hover_line)
            self.strip_ax.draw_artist(self._hover_strip)
            self.ax.draw_artist(self._hover_label)

    def _blit_hover(self):
        """Draw the hover line over the last rendered figure, without rendering it again."""
        if self._background is None:
            return
        self.canvas.restore_region(self._background)
        self._draw_hover()
        self.canvas.blit(self.canvas.figure.bbox)

    def _hide_hover(self):
        if self._hover_line.get_visible():
            for artist in (self._hover_line, self._hover_strip, self._hover_label):
                artist.set_visible(False)
            self._blit_hover()
        QToolTip.hideText()

    # ---- the view: zoom, pan, scroll bar
    def _full_range(self):
        t0, t1 = float(self._times[0]), float(self._times[-1])
        pad = max((t1 - t0) * 0.01, 60.0)
        return (t0 - pad) / 86400.0, (t1 + pad) / 86400.0

    def _set_view(self, lo, hi):
        if not len(self._times):
            return
        full_lo, full_hi = self._full_range()
        span = max(min(hi - lo, full_hi - full_lo), MIN_SPAN / 86400.0)
        lo = min(max(lo, full_lo), full_hi - span) if span < full_hi - full_lo else full_lo
        self._view = (lo, lo + span)
        self.ax.set_xlim(*self._view)
        self._sync_scrollbar()
        self.canvas.draw_idle()

    def _fit(self):
        if len(self._times):
            self._set_view(*self._full_range())

    def _sync_scrollbar(self):
        if self._view is None or not len(self._times):
            self.scrollbar.setEnabled(False)
            self.fit_button.hide()
            return
        full_lo, full_hi = self._full_range()
        lo, hi = self._view
        scale = SCROLL_STEPS / (full_hi - full_lo)
        self._syncing = True
        page = max(1, int(round((hi - lo) * scale)))
        self.scrollbar.setRange(0, max(0, SCROLL_STEPS - page))
        self.scrollbar.setPageStep(page)
        self.scrollbar.setSingleStep(max(1, page // 10))
        self.scrollbar.setValue(int(round((lo - full_lo) * scale)))
        self.scrollbar.setEnabled(page < SCROLL_STEPS)
        self.fit_button.setVisible(page < SCROLL_STEPS)
        self._syncing = False

    def _scrolled(self, value):
        if self._syncing or self._view is None:
            return
        full_lo, full_hi = self._full_range()
        span = self._view[1] - self._view[0]
        lo = full_lo + value * (full_hi - full_lo) / SCROLL_STEPS
        self._set_view(lo, lo + span)

    def _show(self, run):
        """Pan, if needed, so that ``run`` is in view."""
        x = self._times[run] / 86400.0
        lo, hi = self._view
        if not lo <= x <= hi:
            span = hi - lo
            self._set_view(x - span / 2, x + span / 2)

    # ---- the cursor
    def _cursor_run(self):
        if self.cursor is None or self.cursor["rel"] is None:
            return None
        return self._run_of.get(self.cursor["rel"])

    def set_cursor(self, run=None, t=None):
        """Put the cursor on a run (its index), at a time (seconds), or remove it (neither)."""
        if run is not None:
            self.cursor = {"time": float(self._times[run]), "rel": self.record["runs"][run][0]}
        elif t is not None:
            self.cursor = {"time": float(t), "rel": None}
        else:
            self.cursor = None
        self._update_cursor()
        self._show_cursor()

    def _step(self, direction):
        """Move the cursor to the previous (-1) or next (+1) change in the shown instruments."""
        runs = self._change_runs
        if self.record is None or self._view is None or not len(runs):
            return
        if self.cursor is None:
            lo, hi = self._view
            days = self._times[runs] / 86400.0
            inside = runs[(days >= lo) & (days <= hi)]
            inside = inside if len(inside) else runs
            target = inside[0] if direction > 0 else inside[-1]
        else:
            on, t = self._cursor_run(), self.cursor["time"]
            if direction > 0:
                after = runs[runs > on] if on is not None else runs[self._times[runs] > t]
                target = after[0] if len(after) else None
            else:
                before = runs[runs < on] if on is not None else runs[self._times[runs] < t]
                target = before[-1] if len(before) else None
            if target is None:
                return
        self.set_cursor(run=int(target))
        self._show(int(target))

    def _runtime(self, run):
        rel = self.record["runs"][run][0]
        if rel not in self._runtime_names:
            self._runtime_names[rel] = runtime_name(os.path.join(self.root, rel))
        return self._runtime_names[rel]

    def _show_cursor(self):
        th = self._theme()
        muted = f"color:{th['muted']}"
        self.prev_button.setEnabled(self.record is not None)
        self.next_button.setEnabled(self.record is not None)
        if self.cursor is None or self.record is None:
            self.cursor_label.setText(f"<span style='{muted}'>{CURSOR_HINT}</span>")
            for i in range(2):
                self.run_labels[i].setText("")
                self.run_buttons[i].setEnabled(False)
            self._run_targets = [None, None]
            self.changes.setRowCount(0)
            return
        on, t = self._cursor_run(), self.cursor["time"]
        changes = sorted(c for c in self.events_at.get(on, []) if c[0] not in self.hidden) if on is not None else []
        changes = self._panel_rows(changes, on)
        self.run_titles[1].setText(AFTER_TITLES[0 if changes else 1 if on is not None else 2])
        n_instruments = len({c[0] for c in changes})
        summary = ("no change here" if not changes else
                   f"{len(changes)} change{'s' * (len(changes) != 1)} · {n_instruments} "
                   f"instrument{'s' * (n_instruments != 1)}")
        big = QApplication.font().pointSizeF() + 1.5
        self.cursor_label.setText(f"<span style='font-size:{big:g}pt'>{_stamp(t)}</span>"
                                  f"&nbsp;&nbsp;&nbsp;<span style='{muted}'>{summary}</span>")
        if on is not None:
            before, after = on - 1, on
        else:
            after = int(np.searchsorted(self._times, t, side="left"))
            before = after - 1
        for i, run in enumerate((before, after)):
            ok = 0 <= run < len(self._times)
            self._run_targets[i] = run if ok else None
            self.run_buttons[i].setEnabled(ok and self.open_folder is not None)
            if not ok:
                self.run_labels[i].setText(f"<span style='{muted}'>none</span>")
                self.run_labels[i].setToolTip("")
                continue
            rel = self.record["runs"][run][0]
            name = self._runtime(run) or os.path.basename(rel.replace("\\", "/"))
            self.run_labels[i].setText(f"<span style='{muted}'>{_stamp(self._times[run], full=False)}</span>"
                                       f"&nbsp;&nbsp;&nbsp;{name}&nbsp;&nbsp;&nbsp;<span style='{muted}'>{rel}</span>")
            self.run_labels[i].setToolTip(os.path.join(self.root, rel))
        self._fill_changes(changes, th)

    def _value_at(self, key, run):
        """The parameter's value in the last run at or before ``run`` that saved it, or None."""
        points = self.record["series"].get(key, [])
        i = bisect.bisect_right(points, run, key=lambda point: point[0]) - 1
        return points[i][1] if i >= 0 else None

    def _panel_rows(self, changes, run):
        """The panel's rows: ``(instrument, parameter, old, new, ratio, presence)``. An instrument
        going missing or coming back gets a row per parameter, with its last value before or its
        value on return (a change it brings back folded in); presence is "missing", "back" or None."""
        back = {c[0] for c in changes if c[1].endswith(PRESENCE) and c[3] == "saved"}
        rows = []
        for name, key, old, new, ratio in changes:
            if not key.endswith(PRESENCE):
                if name not in back:
                    rows.append((name, key, old, new, ratio, None))
                continue
            for parameter in self._parameters_of.get(name, []):
                if parameter in self.hidden_parameters:
                    continue
                if new == "missing":
                    value = self._value_at(parameter, run - 1)
                    if value is not None:
                        rows.append((name, parameter, value, None, ratio, "missing"))
                elif self._value_at(parameter, run) is not None:
                    rows.append((name, parameter, self._value_at(parameter, run - 1),
                                 self._value_at(parameter, run), ratio, "back"))
        return rows

    def _fill_changes(self, changes, th):
        self.changes.setRowCount(len(changes))
        right = Qt.AlignRight | Qt.AlignVCenter
        for row, (name, key, old, new, ratio, presence) in enumerate(changes):
            entry = self.info.get(key, {})
            instrument = QTableWidgetItem(self._swatch(name), name)
            parameter = QTableWidgetItem(key.split(".", 1)[1])
            parameter.setToolTip(self._parameter_tip(key) if key in self.info else key)
            before = QTableWidgetItem(_value_text(old))
            before.setForeground(QColor(th["muted"]))
            after = QTableWidgetItem(_value_text(new))
            for item in (before, after):
                if _is_number(old if item is before else new):
                    item.setTextAlignment(right)
            if presence == "missing":               # the instrument is not saved from this run on
                after.setText("missing")
                after.setForeground(QColor(th["muted"]))
                change = QTableWidgetItem("▼ missing")
                tip = f"{name} is not saved from this run on"
            elif presence == "back":                # saved again: its value now, and since before the gap
                before.setText("missing")
                moved = (f" · {new - old:+.3g}" if _is_number(old) and _is_number(new) and new != old else
                         " · changed" if old is not None and new != old else "")
                change = QTableWidgetItem("▲ back" + moved)
                tip = (f"{name} is saved again from this run on"
                       + (f"; before the gap it was {_value_text(old)}" if old is not None else ""))
            elif _is_number(old) and _is_number(new):
                delta = new - old
                change = QTableWidgetItem(f"{'▲' if delta > 0 else '▼'} {delta:+.3g}")
            else:
                change = QTableWidgetItem("changed")
                change.setForeground(QColor(th["muted"]))
            if presence is None:
                tip = ("every change counts" if math.isinf(ratio) else
                       f"{ratio:.3g} × its threshold ({_number(entry.get('threshold', 0.0))})")
            change.setToolTip(tip)
            for column, item in enumerate((instrument, parameter, before, after, change)):
                self.changes.setItem(row, column, item)

    def _open_run(self, i):
        run = self._run_targets[i]
        if run is not None and self.open_folder is not None:
            self.open_folder(os.path.join(self.root, self.record["runs"][run][0]))

    # ---- mouse and keys
    def _hit(self, mouse):
        """What is under the cursor: ("event", (instrument, parameter or None), event), ("run", None,
        run index) on the runs strip, or None."""
        if mouse.xdata is None or self.record is None:
            return None
        if mouse.inaxes is self.strip_ax:
            row = None
        elif mouse.inaxes is self.ax:
            row = next((r for r, (a, b) in self._row_span.items() if a <= mouse.ydata < b), _NO_ROW)
        else:
            return None
        if row is _NO_ROW or row not in self._art:
            return None
        art = self._art[row]
        if not len(art["x"]):
            return None
        px = self._to_px(art["x"])
        i = int(np.argmin(np.abs(px - mouse.x)))
        if abs(px[i] - mouse.x) > SNAP_PIXELS:
            return None
        if row is None:
            return ("run", None, int(art["runs"][i]))
        name, key = row
        events = self.events.get(name, []) if key is None else self.param_events.get(key, [])
        return ("event", row, events[i])

    def _to_px(self, days):
        """Screen x (pixels) of matplotlib date values."""
        days = np.asarray(days, dtype=float)
        return self.ax.transData.transform(np.column_stack([days, np.zeros(len(days))]))[:, 0]

    def _near_cursor(self, mouse):
        if self.cursor is None or mouse.x is None or mouse.inaxes not in (self.ax, self.strip_ax):
            return False
        return abs(self._to_px([self.cursor["time"] / 86400.0])[0] - mouse.x) <= SNAP_PIXELS

    def _hover_text(self, hit):
        kind, row, target = hit
        if kind == "run":
            rel = self.record["runs"][target][0]
            changes = [c for c in self.events_at.get(target, []) if c[0] not in self.hidden]
            return f"{_stamp(self._times[target])}\n{rel}\n{len(changes)} changes"
        changes = target["changes"]
        names = [c[0].split(".", 1)[1] for c in changes]
        listed = ", ".join(names[:4]) + (", …" if len(names) > 4 else "")
        return (f"{row[0]} · {_stamp(self._times[target['run']])}\n"
                f"{len(changes)} change{'s' * (len(changes) != 1)}: {listed}")

    def _on_motion(self, mouse):
        drag = self._drag
        if drag is not None and mouse.x is not None:
            if drag["kind"] == "cursor":
                self._drag_cursor(mouse)
                return
            x0, view = drag["x"], drag["view"]
            if abs(mouse.x - x0) > 3:
                drag["moved"] = True
            shift = (mouse.x - x0) / max(self.ax.bbox.width, 1) * (view[1] - view[0])
            self._set_view(view[0] - shift, view[1] - shift)
            return
        if mouse.inaxes not in (self.ax, self.strip_ax) or mouse.xdata is None:
            self._hide_hover()
            self.canvas.unsetCursor()
            return
        self._hover_line.set_xdata([mouse.xdata, mouse.xdata])
        self._hover_strip.set_xdata([mouse.xdata, mouse.xdata])
        self._hover_label.set_x(mouse.xdata)
        self._hover_label.set_text(_stamp(mouse.xdata * 86400.0, full=False))
        self._hover_line.set_visible(True)
        self._hover_strip.set_visible(True)
        self._hover_label.set_visible(not self._near_cursor(mouse))
        self._blit_hover()
        if self._near_cursor(mouse):
            self.canvas.setCursor(Qt.SizeHorCursor)
            QToolTip.hideText()
            return
        hit = self._hit(mouse)
        if hit is None:
            self.canvas.unsetCursor()
            gap = self._gap_text(mouse)
            if gap:
                QToolTip.showText(QCursor.pos(), gap, self.canvas)
            else:
                QToolTip.hideText()
            return
        self.canvas.setCursor(Qt.PointingHandCursor)
        QToolTip.showText(QCursor.pos(), self._hover_text(hit), self.canvas)

    def _gap_text(self, mouse):
        """The tooltip over a hatched span: which runs the instrument is missing from."""
        if mouse.inaxes is not self.ax or mouse.ydata is None:
            return ""
        row = next((r for r, (a, b) in self._row_span.items() if a <= mouse.ydata < b), None)
        if row is None:
            return ""
        name = row[0]
        for (a, b), (x0, x1) in zip(self.missing.get(name, []), self._gap_days(name)):
            if x0 <= mouse.xdata <= x1:
                return (f"{name} is not saved in {b - a} run{'s' * (b - a != 1)}\n"
                        f"{_stamp(self._times[a])} – {_stamp(self._times[b - 1])}")
        return ""

    def _drag_cursor(self, mouse):
        """The cursor follows the mouse, snapping to a change it passes over."""
        x = self.ax.transData.inverted().transform((mouse.x, mouse.y))[0]
        runs = self._change_runs
        cursor = {"time": x * 86400.0, "rel": None}
        if len(runs):
            px = self._to_px(self._times[runs] / 86400.0)
            i = int(np.argmin(np.abs(px - mouse.x)))
            if abs(px[i] - mouse.x) <= SNAP_PIXELS:
                run = int(runs[i])
                cursor = {"time": float(self._times[run]), "rel": self.record["runs"][run][0]}
        if cursor != self.cursor:
            self.cursor = cursor
            self._update_cursor()
            self._panel_timer.start()

    def _on_press(self, mouse):
        if mouse.inaxes not in (self.ax, self.strip_ax) or mouse.button != 1 or self.record is None:
            return
        self.canvas.setFocus()
        QToolTip.hideText()
        hit = self._hit(mouse)
        run = None if hit is None else hit[2] if hit[0] == "run" else hit[2]["run"]
        if mouse.dblclick:
            self._drag = None
            if run is not None and self.open_folder is not None:
                self.open_folder(os.path.join(self.root, self.record["runs"][run][0]))
            elif run is None:
                self._fit()
            return
        if self._near_cursor(mouse):
            self._drag = {"kind": "cursor"}
        elif run is not None:
            self.set_cursor(run=run)
        else:
            self._drag = {"kind": "pan", "x": mouse.x, "xdata": mouse.xdata, "view": self._view, "moved": False}

    def _on_release(self, _mouse):
        drag, self._drag = self._drag, None
        if drag is None:
            return
        if drag["kind"] == "cursor":
            self._panel_timer.stop()
            self._show_cursor()
        elif not drag["moved"]:
            self.set_cursor(t=drag["xdata"] * 86400.0)       # a click on an empty spot

    def _on_scroll(self, mouse):
        if mouse.inaxes not in (self.ax, self.strip_ax) or mouse.xdata is None or self._view is None:
            return
        factor = 0.8 if mouse.button == "up" else 1.25
        lo, hi = self._view
        x = mouse.xdata
        self._set_view(x - (x - lo) * factor, x + (hi - x) * factor)

    def _on_key(self, key):
        if key.key == "left":
            self._step(-1)
        elif key.key == "right":
            self._step(1)
        elif key.key == "escape":
            self.set_cursor()
        elif key.key == "enter":
            self._open_run(1)
