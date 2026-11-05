import os
import time
import inspect
from functools import partial, wraps
from typing import Iterable, Union, Callable, Literal, Annotated, get_type_hints, get_args, get_origin
from collections import defaultdict
import subprocess
import logging
from pathlib import Path
import ast
import html

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import ScalarFormatter

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QWidgetAction, QMenu, QAction, QApplication, QToolButton, QPushButton,
    QProgressBar, QLineEdit, QLabel, QComboBox, QGroupBox, QGridLayout, QCheckBox, QSizePolicy, QFrame, QSlider
)
from PyQt5.QtCore import QTimer, Qt, QSize, pyqtSignal
from PyQt5 import QtCore, QtGui
from PyQt5.QtGui import QImage, QPainter, QFont, QIcon, QFontMetrics

from acadia_qmsmt.utils.saved_runtime_loader import get_saved_runtime_class, load_runtime_from_data_dir
from acadia_qmsmt.utils import get_registered_plot_methods, get_data_process_method, get_registered_button_methods
from acadia_qmsmt.utils.annotation import AXS_SHAPE_TAG, get_registered_methods, get_registered_customizer
from acadia_qmsmt.utils.path_adapter import to_windows_path, detect_platform

from acadia_gui.icons import get_icon, style_mpl_toolbar
from acadia_gui.gui.iter_window import ITER_WINDOWABLE_ATTR, windowed_reshape


# files used for rough estimate of progress rate, ETA, etc
UPDATE_INDICATOR_FILE = "metadata.txt" # file whose last modified time indicates the last data update
CREATE_INDICATOR_FILE = "run.py" # file whose creation time indicates the experiment time
STOP_INDICATOR_FILE = ".stop"

TOTAL_ITER_ATTRIBUTE = "iterations" # runtime class attribute that defines the total number of iterations
ITER_WINDOW_KWARG = "iter_window" # data-processor kwarg used to restrict processing to a range of iterations
# shown in the window button's tooltip: how a runtime's data processor takes the window
ITER_WINDOW_EXAMPLE = """# Option 1: the GUI windows reshape_iq_data_by_axes (axis 0).
# Only if ALL per-iteration data in the processor comes from it.
class MyRuntime(QMsmtRuntime):
    iter_windowable = True

# Option 2: the processor applies the window itself (also works in notebooks)
@annotate_method(is_data_processor=True)
def process_current_data(self, iter_window=None):
    data = reshape_iq_data_by_axes(...)  # axis 0 = iterations
    total = len(data)
    if iter_window is not None:
        i0, i1 = iter_window             # 0-based, inclusive
        data = data[i0:i1 + 1]
    ...                                  # process `data`
    return total                         # always the TOTAL count"""

logger = logging.getLogger(__name__)

def parse_inputs(input_dict):
    kwargs = {}
    for k, w in input_dict.items():
        if isinstance(w, QCheckBox):
            kwargs[k] = w.isChecked()
        elif isinstance(w, QComboBox):
            kwargs[k] = w.currentText()
        elif isinstance(w, QLineEdit):
            try:
                val = _safe_eval(w.text())
                kwargs[k] = val
            except Exception:
                kwargs[k] = w.text()
        elif isinstance(w, QSlider):
            kwargs[k] = w.value()
        else:
            kwargs[k] = w.text()  # fallback
    return kwargs

def format_duration(seconds):
    if not np.isfinite(seconds):
        return "?"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    hms = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{d}d {hms}" if d else hms


def shorten_path_for_display(path, max_chars=55):
    """
    Shorten a given path string to fit within a maximum character limit.
    Always preserves the root folder and the last two folders.
    Middle sections are included based on available space, prioritizing parts closest to the root and end.
    """
    if len(path) <= max_chars:
        return path
    parts = path.split(os.sep)
    if len(parts) < 4:
        return path[-max_chars:]

    head, mid, tail = [parts[0]], parts[1:-2], parts[-2:]
    length = 4 + sum(len(p) + 1 for p in head + tail)  # +1 for os.sep, 4 for "..."
    for i in range(len(mid)):
        idx = i//2 if i%2 == 0 else -(i//2)-1
        length += len(mid[idx]) + 1
        if length > max_chars:
            break
        if i%2 == 0: # from beginning
            head.append(mid[idx])
        else: # from end
            tail.insert(0, mid[idx])

    return os.sep.join(head + ["..."] + tail)

def clear_layout(layout):
    # --- Clear old layout ---
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())
            item.layout().deleteLater()

def format_pcm_coord(ax, x, y):
    """
    for overwriting the `format_coord` of mpl ax to include z coordinate for pcolormesh plots
    """
    xy_text = f"x={ax.format_xdata(x)}, y={ax.format_ydata(y)}"
    try:
        Xedges, Yedges, Z = ax._pcm_xedges, ax._pcm_yedges, ax._pcm_Z

        i = np.searchsorted(Xedges, x) - 1
        j = np.searchsorted(Yedges, y) - 1

        if 0 <= i < Z.shape[1] and 0 <= j < Z.shape[0]:
            z_val = Z[j, i]
            if np.ma.is_masked(z_val):
                z_str = np.nan
            else:
                z_str = ax._pcm_format_z(z_val)
            # Use default formatting for x and y
            return f"{xy_text}, z={z_str}"
        else:
            return xy_text

    except Exception as e:
        return xy_text

def _prepare_pcm_edges(ax) -> bool:
    """
    gather the edge and z data information of a pcolormesh plot, make a formater for z data
    """
    try:
        mesh = ax.collections[0]
        coords = mesh._coordinates
        xedges = np.unique(coords[...,0])
        yedges = np.unique(coords[...,1])
        nx = len(xedges)
        ny = len(yedges)
        ax._pcm_xedges = xedges
        ax._pcm_yedges = yedges
        ax._pcm_Z = mesh.get_array().reshape((ny-1, nx-1))

        # make a mpl formater for the z data
        # this is smarter than simply doing `.4g/.4f/.4e`, etc.
        # E.g. this knows the right number of digits to keep when data falls in a small region with a big offset
        scalar_fmt = ScalarFormatter(useMathText=True)
        scalar_fmt.set_powerlimits((-3, 4))
        scalar_fmt.create_dummy_axis()
        zmin, zmax = np.nanmin(ax._pcm_Z), np.nanmax(ax._pcm_Z)
        scalar_fmt.axis.set_view_interval(zmin, zmax)

        def format_z(z):
            if not np.isfinite(z):
                return str(z)
            return scalar_fmt.format_data_short(float(z))

        ax._pcm_format_z = format_z

        return True # is pcm plot
    except Exception as e:
        return False

def make_kwarg_box(title):
    box = QGroupBox(title)
    layout = QVBoxLayout()
    box.setLayout(layout)
    return box, layout

def _safe_eval(text):
    try:
        return ast.literal_eval(text)
    except Exception:
        return text

def coalesce_calls(flag_name="_updating", pending_name="_pending_update", reschedule_ms=0):
    """
    Prevents reentrancy and coalesces missed calls into one extra run.
    - If called while busy, sets `pending_name` and returns immediately.
    - On exit, if pending, schedules one more call after `reschedule_ms`.
    """
    def deco(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if getattr(self, flag_name, False):
                setattr(self, pending_name, True)
                return
            setattr(self, flag_name, True)
            try:
                return func(self, *args, **kwargs)
            finally:
                setattr(self, flag_name, False)
                if getattr(self, pending_name, False):
                    setattr(self, pending_name, False)
                    # schedule one more pass, coalesced
                    QtCore.QTimer.singleShot(reschedule_ms, lambda: func(self, *args, **kwargs))
        return wrapper
    return deco


class ThrottledCanvas(FigureCanvasQTAgg):
    """
    Canvas that renders at most once per `interval_ms` for redraw requests (`draw_idle`).

    Opening a plot resizes the canvas several times while the input boxes settle, and the next
    paint renders each resize's redraw request, so the plot was rendered 5-6 times per open.
    Requests within `interval_ms` now give one render. `draw()` still renders right away.
    """
    def __init__(self, figure=None, interval_ms=50):
        super().__init__(figure)
        self._interval_ms = interval_ms
        self._last_render = 0.0
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(super().draw_idle)

    def draw(self):
        self._idle_timer.stop()     # this render serves the queued requests too
        self._last_render = time.perf_counter()
        super().draw()

    def draw_idle(self):
        # like matplotlib's own draw_idle, requests made during a render are dropped
        if self._idle_timer.isActive() or getattr(self, "_is_drawing", False):
            return
        if QApplication.mouseButtons() != Qt.NoButton:     # pan, zoom, slider drags: full frame rate
            super().draw_idle()
            return
        since_ms = (time.perf_counter() - self._last_render) * 1000
        self._idle_timer.start(int(max(0, self._interval_ms - since_ms)))


class IterationWindowBar(QProgressBar):
    """Progress bar with an optional draggable iteration-selection window.

    In window mode a translucent band with two handles is painted over the normal bar.
    Handle positions are 0-based INCLUSIVE iteration indices ``(i0, i1)``: the window
    selects ``i1 - i0 + 1`` iterations, which a runtime slices as ``data[i0:i1 + 1]``.
    The band is drawn from ``i0`` to ``i1 + 1`` because iteration ``i`` occupies the
    span ``[i, i + 1)`` of the bar.

    The bar's own coordinate system stays ``0..maximum()`` -- the REQUESTED iteration
    count -- so the band lines up with the fill, but the handles are clamped to the
    iterations that actually exist (``available``). A run stopped at 496 of 1000 thus
    shows a bar reading 496/1000 whose window cannot be dragged past iteration 495.

    ``windowChanged`` fires on mouse RELEASE only. Reprocessing runs synchronously on
    the GUI thread and takes seconds on large datasets, so it must never fire per mouse
    move; ``windowMoving`` fires continuously for cheap updates (the text boxes).
    """

    windowChanged = QtCore.pyqtSignal(int, int)
    windowMoving = QtCore.pyqtSignal(int, int)

    GRAB_PX = 6  # how close the cursor must be to a handle to grab it

    def __init__(self, parent=None):
        super().__init__(parent)
        self._window_mode = False
        self._available = 0
        self._i0 = 0
        self._i1 = 0
        self._drag = None
        self._drag_ref = None
        self.setMouseTracking(True)

    # ---- public API ------------------------------------------------------
    def window_mode(self):
        return self._window_mode

    def set_window_mode(self, on: bool):
        self._window_mode = bool(on)
        if self._window_mode:  # (re)open on the full available range
            self._i0, self._i1 = self._clamp(0, max(self._available - 1, 0))
        self.unsetCursor()
        self._drag = None
        self.update()

    def set_available(self, n: int):
        """Tell the bar how many iterations actually exist.

        Pin semantics: an active window keeps its absolute indices when new data
        arrives and is only clamped if it would fall outside the data.
        """
        self._available = max(int(n), 0)
        if self._window_mode:
            self._i0, self._i1 = self._clamp(self._i0, self._i1)
        else:  # track the full range so opening the window later spans everything
            self._i0, self._i1 = 0, max(self._available - 1, 0)
        self.update()

    def available(self):
        return self._available

    def window(self):
        """Current window as 0-based inclusive ``(i0, i1)``, or None if inactive."""
        if not self._window_mode or self._available <= 0:
            return None
        return int(self._i0), int(self._i1)

    def set_window(self, i0, i1, emit=False):
        self._i0, self._i1 = self._clamp(i0, i1)
        self.update()
        if emit:
            self.windowChanged.emit(self._i0, self._i1)

    # ---- geometry --------------------------------------------------------
    def _clamp(self, i0, i1):
        top = max(self._available - 1, 0)
        i0 = int(min(max(int(i0), 0), top))
        i1 = int(min(max(int(i1), i0), top))
        return i0, i1

    def _bar_rect(self):
        return self.rect().adjusted(1, 1, -1, -1)

    def _xof(self, v):
        r = self._bar_rect()
        return r.left() + r.width() * (float(v) / max(self.maximum(), 1))

    def _vof(self, x):
        r = self._bar_rect()
        if r.width() <= 0:
            return 0.0
        return (float(x) - r.left()) / r.width() * max(self.maximum(), 1)

    # ---- mouse -----------------------------------------------------------
    def _interactive(self):
        return self._window_mode and self._available > 0

    def mousePressEvent(self, event):
        if not self._interactive() or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        x = event.pos().x()
        x0, x1 = self._xof(self._i0), self._xof(self._i1 + 1)
        if abs(x - x0) <= self.GRAB_PX:
            self._drag = "lo"
        elif abs(x - x1) <= self.GRAB_PX:
            self._drag = "hi"
        elif x0 < x < x1:
            self._drag = "band"
            self._drag_ref = (self._vof(x), self._i0, self._i1)
        else:  # clicked outside the band: grab the nearer edge and jump it here
            self._drag = "lo" if abs(x - x0) <= abs(x - x1) else "hi"
            self._apply_drag(x)
        self.setCursor(Qt.ClosedHandCursor if self._drag == "band" else Qt.SizeHorCursor)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._interactive():
            return super().mouseMoveEvent(event)
        if self._drag is None:  # hover feedback only
            x = event.pos().x()
            x0, x1 = self._xof(self._i0), self._xof(self._i1 + 1)
            if abs(x - x0) <= self.GRAB_PX or abs(x - x1) <= self.GRAB_PX:
                self.setCursor(Qt.SizeHorCursor)
            elif x0 < x < x1:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.unsetCursor()
            return super().mouseMoveEvent(event)
        self._apply_drag(event.pos().x())
        self.update()
        self.windowMoving.emit(self._i0, self._i1)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is None:
            return super().mouseReleaseEvent(event)
        self._drag = None
        self.unsetCursor()
        self.update()
        # only now do we pay for reprocessing
        self.windowChanged.emit(self._i0, self._i1)
        event.accept()

    def _apply_drag(self, x):
        v = self._vof(x)
        if self._drag == "lo":
            i0, i1 = int(round(v)), self._i1
        elif self._drag == "hi":
            i0, i1 = self._i0, int(round(v)) - 1
        else:  # move the whole window, preserving its width
            v_ref, a, b = self._drag_ref
            width = b - a
            shift = int(round(v - v_ref))
            i0 = min(max(a + shift, 0), max(self._available - 1 - width, 0))
            i1 = i0 + width
        self._i0, self._i1 = self._clamp(i0, i1)

    # ---- painting --------------------------------------------------------
    def paintEvent(self, event):
        super().paintEvent(event)  # normal bar + its ETA text first
        if not self._interactive():
            return
        painter = QPainter(self)
        r = self._bar_rect()
        x0, x1 = self._xof(self._i0), self._xof(self._i1 + 1)

        # dim everything outside the window so the selection reads at a glance
        shade = QtGui.QColor(0, 0, 0, 110)
        if x0 > r.left():
            painter.fillRect(QtCore.QRectF(r.left(), r.top(), x0 - r.left(), r.height()), shade)
        if x1 < r.right():
            painter.fillRect(QtCore.QRectF(x1, r.top(), r.right() - x1, r.height()), shade)

        accent = self.palette().highlight().color()
        painter.setPen(QtGui.QPen(accent, 1))
        painter.drawRect(QtCore.QRectF(x0, r.top(), max(x1 - x0, 1), r.height() - 1))
        for x in (x0, x1):
            painter.fillRect(QtCore.QRectF(x - 1.5, r.top(), 3, r.height()), accent)
        painter.end()


class LivePlotWidget(QWidget):
    # step of opening a data folder: "loading", "processing", "plotting"
    open_step = pyqtSignal(str)

    def __init__(self, poll_interval_sec=2, update_indicator_file=UPDATE_INDICATOR_FILE,
                 create_indicator_file=CREATE_INDICATOR_FILE):
        """
        LivePlotWidget shows a matplotlib plot updated regularly based on experiment data.

        - It periodically reloads the runtime object using `load_runtime_from_data_dir`.
        - Plotting is updated only if the `indicator_file` changes.
        - User can pause/resume updates, adjust polling interval, and select from multiple plots.
        """
        super().__init__()

        self.rt = None
        self.poll_interval_ms = poll_interval_sec * 1000
        self.is_paused = False
        self.data_path = None
        self.update_indicator_file = update_indicator_file
        self.create_indicator_file = create_indicator_file
        self.last_mtime = 0
        self.plot_registry = {}
        self.current_plot_name = None
        self.ready = False
        self.completed_iter = None
        self.folder_label = QLabel(" ")
        self.folder_label.setAlignment(Qt.AlignCenter)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Deep/long folder paths must not dictate this widget's minimum width
        # (an un-elided QLabel's minimum width is its full text width). Since
        # this widget sits alongside the static image view in a QStackedLayout,
        # which sizes to the max minimum size across ALL pages, an unbounded
        # label here forces a huge minimum even while the static page is shown.
        self.folder_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.folder_label.setVisible(False)   # path is shown in the CenterView header
        self._full_folder_path = ""
        self.plot_axes = None
        self.last_axs_shape = None
        # tight_layout runs on every plot update (see make_plot); these track a resize after it
        self._needs_layout = True
        self._laid_out_size = None      # canvas size (px) the last tight_layout was done for
        # metadata.txt time of the data start() just loaded, while that runtime is still unused:
        # the first update processes it instead of reading the data a second time
        self._fresh_load_mtime = None

        # -- default snapshot settings
        self.snapshot_original_dpi = 800 # high dpi
        self.snapshot_original_width_inch = 5 # size for the high DPI figure
        self.snapshot_original_height_inch = 4
        self.snapshot_scale_factor = 0.1 # scale factor for the smaller plot
        self.snapshot_title_font = 10


        # Each item is a tuple: (label, is_checkable, handler_function)
        self.right_click_actions = [
            ("Autoscale", True, self.autoscale_ax),
            #  can have more like:
            # ("Reset Zoom", False, self.reset_zoom),
        ]
        self.checked_right_click_flags = defaultdict(set)  # (plot_name, ax_index) → set of flags

        # --- Matplotlib figure/canvas ---
        self.canvas = ThrottledCanvas(Figure())
        self.canvas.figure.set_facecolor("none") # will be set later
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        self.toolbar.setObjectName("livePlotToolBar")  # tags for styling with css
        self.canvas.setObjectName("livePlotCanvas")
        self.canvas.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )
        self.canvas.setMinimumHeight(200)
        self.canvas.mpl_connect("button_press_event", self.handle_right_click)
        # Axes positions are figure fractions fixed by the last tight_layout, so after the canvas
        # changes size the title or tick labels can end up clipped; lay out again once a resize
        # settles (a finished run gets no further updates that would do it).
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(120)
        self._relayout_timer.timeout.connect(self._relayout_after_resize)
        self.canvas.mpl_connect("resize_event", self._on_canvas_resize)


        # --- Plot selector ---
        self.plot_selector = QComboBox()
        self.plot_selector.currentIndexChanged.connect(self.select_plot)
        self.plot_selector.setMinimumWidth(5)



        # --- Polling interval input ---
        self.interval_input = QLineEdit(str(poll_interval_sec))
        self.interval_input.setFixedWidth(40)
        self.interval_input.setToolTip("Polling interval (in seconds)")
        self.interval_input.editingFinished.connect(self.update_poll_interval)

        self._pause_icon = get_icon("pause_plot.svg")
        self._resume_icon = get_icon("resume_plot.svg")
        self.pause_button = QToolButton()
        self.pause_button.setIcon(self._pause_icon)
        self.pause_button.setToolTip("Pause plotting")
        self.pause_button.setIconSize(self.toolbar.iconSize())
        self.pause_button.clicked.connect(self.toggle_pause)

        # --- Plot snapshot button -------
        self.snapshot_button = QToolButton()
        self.snapshot_button.setIcon(get_icon("snapshot_plot.svg"))
        self.snapshot_button.setToolTip("Snapshot plot\n"
                                        "Create a high-DPI figure and scale it down\n"
                                        "for easier fitting in notebooks.\n"
                                        "Right-click for settings.")
        self.snapshot_button.setIconSize(self.toolbar.iconSize())
        self.snapshot_button.clicked.connect(self.snapshot_current_plot)
        self.snapshot_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.snapshot_button.customContextMenuRequested.connect(self.show_snapshot_settings)

        separator_line = QFrame()
        separator_line.setFrameShape(QFrame.VLine)
        separator_line.setFrameShadow(QFrame.Sunken)
        separator_line.setFixedHeight(25)

        interval_row = QHBoxLayout()
        interval_row.addWidget(self.toolbar)
        interval_row.addWidget(separator_line)
        interval_row.addWidget(QLabel("Poll every:"))
        interval_row.addWidget(self.interval_input)
        interval_row.addWidget(QLabel("s"))
        interval_row.addWidget(self.pause_button)
        interval_row.addWidget(self.snapshot_button)


        # --- Input regions for kwargs ---
        self.process_kwargs_box, self.process_kwargs_layout = make_kwarg_box("Process kwargs")
        self.plot_kwargs_box, self.plot_kwargs_layout = make_kwarg_box("Plot kwargs")
        self.update_buttons_box, self.update_buttons_layout = make_kwarg_box("")

        kwargs_row = QVBoxLayout()
        kwargs_row.addWidget(self.process_kwargs_box)
        kwargs_row.addWidget(self.plot_kwargs_box)
        kwargs_row.addWidget(self.update_buttons_box)

        # --- Progress bar ------------
        self.progress_bar = IterationWindowBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setFormat("0/0")
        self.progress_bar.windowMoving.connect(self._on_window_moving)
        self.progress_bar.windowChanged.connect(self._on_window_changed)

        # --- Iteration-window controls ------------
        # Only usable when the runtime's data processor accepts the `iter_window` kwarg;
        # `start()` enables the toggle accordingly so an unsupported runtime is visibly
        # unavailable rather than silently ignoring the window.
        self.window_button = QToolButton()
        self.window_button.setText("[ ]")
        self.window_button.setCheckable(True)
        self.window_button.setEnabled(False)
        self.window_button.toggled.connect(self.toggle_iteration_window)

        self.window_start_input = QLineEdit()
        self.window_stop_input = QLineEdit()
        for box, tip in ((self.window_start_input, "First iteration (0-based, inclusive)"),
                         (self.window_stop_input, "Last iteration (0-based, inclusive)")):
            box.setFixedWidth(60)
            box.setAlignment(Qt.AlignCenter)
            box.setToolTip(tip)
            box.setVisible(False)
            box.editingFinished.connect(self._on_window_text_edited)

        # ---- stop button ------------
        self.stop_button = QPushButton()
        self.stop_button.setText("STOP")
        self.stop_button.setObjectName("stop_button")
        self.stop_button.setFixedWidth(80)
        self.stop_button.clicked.connect(self.drop_stop_flag)


        progress_row = QHBoxLayout()
        progress_row.addWidget(self.window_button)
        progress_row.addWidget(self.window_start_input)
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.window_stop_input)
        progress_row.addWidget(self.stop_button)
        self.stop_button.setFixedHeight(self.progress_bar.sizeHint().height())
        self.window_button.setFixedHeight(self.progress_bar.sizeHint().height())

        # iteration-window state
        self._supports_window = False
        self._window_by_gui = False   # True: the GUI applies the window (runtime sets iter_windowable)
        self._last_window = None      # window used for the last actual reprocess
        self._last_available = 0      # iterations present at the last actual reprocess

        # --- Layout setup ---
        layout = QVBoxLayout(self)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.plot_selector)
        layout.addWidget(self.canvas, stretch=1)
        layout.addLayout(interval_row)
        layout.addLayout(kwargs_row)
        layout.addLayout(progress_row)
        self.setLayout(layout)

        # --- Timer for update ---
        # single shot, restarted when an update ends: the poll interval is the wait between the
        # end of one update and the start of the next, so slow processing leaves the window time
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._poll)

        # --- Autoscale cache ----
        self._ax_state_before_action = {}  # (plot_name, ax_index, label) -> state dict


    def _load_runtime(self):
        """
        Load the runtime (with its data) from `data_path` and run its customizer, if any. Used when a folder is opened
        and on every refresh: `runtime_class.load` decides how the data is read, which allows customizing data read.
        """
        self.rt = self.runtime_class.load(self.data_path)

        # run the customizer for programmatic plot/button modification if it exists
        customizer_name = get_registered_customizer(self.rt)
        if customizer_name is not None:
            try:# call the customizer method for preparing programmatically generated plots/buttons
                getattr(self.rt, customizer_name)()
                logger.debug(f"Using customizer method {self.runtime_class.__name__}.{customizer_name}")
            except Exception as e:
                logger.error(f"Error in customizer method "
                             f"{self.runtime_class.__name__}.{customizer_name} : {e}", exc_info=True)
        else:
            logger.debug(f"No customizer method found in {self.runtime_class.__name__}, skipped.")

    def start(self, data_path):
        self.open_step.emit("loading")
        self.data_path = data_path
        self._full_folder_path = data_path
        self._update_folder_label()
        self.is_paused = False
        self.last_mtime = 0
        self.completed_iter = None
        self._needs_layout = True
        self._fresh_load_mtime = None

        # Load the saved acadia_qmsmt.py as the acadia_qmsmt.qmsmt submodule
        try:
            # Get the saved runtime class and load runtime, with the saved acadia_qmsmt
            self.runtime_class = get_saved_runtime_class(data_path, use_saved_qmsmt=True)

        except Exception as e:
            logger.warning(f"Failed to load the saved acadia_qmsmt.qmsmt submodule : {e}. "
                           f"Using the global version", exc_info=True)
            self.runtime_class = get_saved_runtime_class(data_path, use_saved_qmsmt=False)

        loaded_mtime = self.get_latest_update_time()
        self._load_runtime()
        self._fresh_load_mtime = loaded_mtime

        self.total_iter =  getattr(self.rt, TOTAL_ITER_ATTRIBUTE, None)
        if self.total_iter is None:
            logger.warning(f"total iteration attribute `{TOTAL_ITER_ATTRIBUTE}` not found in runtime class {self.rt}"
                           f"The ETA is disabled")
            self.progress_bar.setMaximum(1)
        else:
            self.progress_bar.setMaximum(self.total_iter)

        # Get all registered plots and populate dropdown
        self.plot_registry = get_registered_plot_methods(self.rt)
        self.plot_selector.clear()
        self.plot_selector.addItems(list(self.plot_registry.keys()))
        try:
            self.data_processor_name = get_data_process_method(self.rt)
        except AttributeError as e:
            logger.error(e, exc_info=True)
            self.data_processor_name = None

        # reset the iteration window before probing the new runtime
        self._reset_iteration_window()

        if self.data_processor_name and hasattr(self.rt, self.data_processor_name):
            processor_func = getattr(self.rt, self.data_processor_name)
            self.process_inputs = self.create_inputs_from_signature(processor_func, self.process_kwargs_layout,
                                                                    self.process_kwargs_box,
                                                                    update_callback=lambda: self.update_plot(force=True))
            self.update_buttons = self.create_update_buttons()

            native = self._processor_supports_window(processor_func)
            self._window_by_gui = not native and bool(getattr(self.rt, ITER_WINDOWABLE_ATTR, False))
            self._supports_window = native or self._window_by_gui
            self.window_button.setEnabled(self._supports_window)
            if self._supports_window:
                tip = ("Restrict processing to a range of iterations.<br>"
                       "Drag either handle to resize, or the band itself to slide the window.<br>"
                       "Reprocessing happens on mouse release. Update buttons turn the window off first.")
            else:
                tip = (f"Unavailable: <code>{self.runtime_class.__name__}.{self.data_processor_name}()</code> "
                       f"does not accept an <code>{ITER_WINDOW_KWARG}</code> argument, and "
                       f"<code>{self.runtime_class.__name__}</code> does not set "
                       f"<code>{ITER_WINDOWABLE_ATTR} = True</code>.")
            self._set_window_tooltip(tip)

        # Disable STOP button if .stop file already exists
        self.update_stop_button_state()

        # Set default plot
        self.current_plot_name = self.plot_selector.currentText()
        self.ready = True  # safe to allow plotting now
        self.select_plot(self.plot_selector.currentIndex())
        self.timer.start(self.poll_interval_ms)

    def _poll(self):
        try:
            self.update_plot()
        finally:
            if self.ready:
                self.timer.start(self.poll_interval_ms)

    def stop(self):
        self.timer.stop()
        self._fresh_load_mtime = None
        self.canvas.figure.clf()
        self.canvas.draw_idle()
        self.ready = False
        self.completed_iter = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_folder_label()

    def _update_folder_label(self):
        metrics = QFontMetrics(self.folder_label.font())
        elided = metrics.elidedText(self._full_folder_path, Qt.ElideMiddle, self.folder_label.width())
        self.folder_label.setText(elided)
        self.folder_label.setToolTip(self._full_folder_path)


    def toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_button.setIcon(self._resume_icon)
            self.pause_button.setToolTip("Resume plotting")
        else:
            self.pause_button.setIcon(self._pause_icon)
            self.pause_button.setToolTip("Pause plotting")


    def update_poll_interval(self):
        try:
            seconds = float(self.interval_input.text())
            if seconds < 0.015:
                seconds = 0.015 # 60Hz maximum...
                logger.warning(f"polling interval clipped to minimum of 0.015s")
            self.poll_interval_ms = int(seconds * 1000)
            self.timer.setInterval(self.poll_interval_ms)
        except ValueError:
            logger.error(f"error interval input: {self.interval_input.text()}")
        finally:
            self.interval_input.setText(str(self.poll_interval_ms / 1000))
            self.interval_input.clearFocus()


    def get_latest_update_time(self):
        try:
            path = os.path.join(self.data_path, self.update_indicator_file)
            return os.path.getmtime(path)
        except Exception:
            return 0


    def get_creation_time(self):
        try:
            path = os.path.join(self.data_path, self.create_indicator_file)
            return os.path.getctime(path)
        except Exception:
            return 0


    def select_plot(self, index):
        self.current_plot_name = self.plot_selector.itemText(index)

        # Refresh plot input fields
        method_name = self.plot_registry.get(self.current_plot_name)
        if method_name and hasattr(self.rt, method_name):
            plot_func = getattr(self.rt, method_name)
            current_plot_signature = inspect.signature(plot_func)

            if "axs" in current_plot_signature.parameters:
                self.current_plot_uses_axs = True
                self.current_plot_axs_shape = getattr(plot_func, AXS_SHAPE_TAG, (1, 1))
            elif "fig" in current_plot_signature.parameters:
                self.current_plot_uses_axs = False
                self.current_plot_axs_shape = None
            else:
                self.current_plot_uses_axs = None  # Invalid, will raise in make_plot
                self.current_plot_axs_shape = None

            # Create and cache axes only if changed
            if self.current_plot_uses_axs and self.current_plot_axs_shape != self.last_axs_shape:
                self.canvas.figure.clf()
                self.plot_axes = self.canvas.figure.subplots(*self.current_plot_axs_shape)
                self.last_axs_shape = self.current_plot_axs_shape
            elif not self.current_plot_uses_axs:
                self.plot_axes = None
                self.last_axs_shape = None

            self.plot_inputs = self.create_inputs_from_signature(plot_func, self.plot_kwargs_layout,
                                                                 self.plot_kwargs_box,
                                                                 update_callback=lambda: self.update_plot(force=True, reprocess_data=False))
        self.checked_right_click_flags.clear()
        self._ax_state_before_action.clear()
        self._needs_layout = True

        # if we already have data, just redo plot
        if self.completed_iter is not None:
            try:
                self.make_plot(self.canvas.figure, method_name, prepare_pcm=True)
            except Exception as e:
                self.canvas.figure.clf()
                logger.error(f"Error in making plot '{method_name}', {e}", exc_info=True)
            self.canvas.draw_idle()
        else: # if we don't have data yet, redo data processing, then plot
            self.update_plot(force=True)

    @coalesce_calls()
    def update_plot(self, force=False, reprocess_data=True):
        """
        Update the current selected plot.

        :param force: When True, force remake plot regardless of whether there is new data/if the plot is paused.
        :param reprocess_data: When True, re-run data processing method before plotting
        :return:
        """
        # do nothing if the data is not ready yet
        if not self.ready or not self.data_path or not self.current_plot_name:
            return

        # Disable STOP button if .stop file is present (e.g., created externally)
        self.update_stop_button_state()

        # do nothing if plot is paused and not force update
        if not force and self.is_paused:
            return

        # do nothing if there is no new data and not force update
        current_mtime = self.get_latest_update_time()
        if not force and current_mtime <= self.last_mtime:
            return

        self.last_mtime = current_mtime

        # A pinned window that already sits entirely inside processed data cannot change
        # when new data arrives, and reprocessing is synchronous on the GUI thread. Skip
        # the work on data-driven polls; an explicit `force` (drag, kwarg edit) still runs.
        if not force and self._supports_window:
            win = self.progress_bar.window()
            if (win is not None and win == self._last_window
                    and self._last_available >= win[1] + 1):
                logger.debug("iteration window pinned inside processed data; skipping reprocess")
                return

        if reprocess_data:
            try:
                if self._fresh_load_mtime is not None:
                    self.open_step.emit("processing")
                    # start() has just loaded the runtime and its data: process that rather than
                    # read it again. Its time stands as the last update, so data written since
                    # is picked up on the next poll.
                    self.last_mtime, self._fresh_load_mtime = self._fresh_load_mtime, None
                else:
                    # reload the runtime with its data
                    try:
                        self._load_runtime()
                    except Exception as e:
                        logger.error(f"Failed to reload runtime: {e}", exc_info=True)
                        return

                completed_iter = None
                if hasattr(self.rt, self.data_processor_name):
                    processor_func = getattr(self.rt, self.data_processor_name)
                    proc_kwargs = parse_inputs(self.process_inputs)
                    active_window = self.progress_bar.window() if self._supports_window else None
                    try:
                        if active_window is not None and self._window_by_gui:
                            with windowed_reshape(active_window) as window_state:
                                completed_iter = processor_func(**proc_kwargs)
                            if window_state.called:
                                completed_iter = window_state.total  # full count, never the window's
                            else:  # reshapes by hand: the window had no effect, so don't offer it
                                self._disable_gui_window()
                        else:
                            if active_window is not None:
                                proc_kwargs[ITER_WINDOW_KWARG] = active_window
                            completed_iter = processor_func(**proc_kwargs)
                        logger.debug(f"Processed data with completed_iter: {completed_iter} ")
                    except Exception as e:
                        logger.error(f"Error in data processing funciton "
                                     f"`{self.runtime_class.__name__}.{self.data_processor_name}`: {e}", exc_info=True)
                    self.refresh_update_button_methods()

                else:
                    self.progress_bar.setFormat(
                        f"Missing processor: {self.data_processor_name}"
                    )
                    logger.error(f"Missing data processor: {self.data_processor_name} in {self.runtime_class.__name__}")
                    return

                if completed_iter is not None:
                    # `completed_iter` is the TOTAL number of completed iterations, not the
                    # windowed count -- a windowed count here would shrink the slider's own
                    # range every time the window narrowed, which cannot be undone.
                    self.completed_iter = completed_iter
                    self.progress_bar.set_available(int(completed_iter))
                    self._last_available = int(completed_iter)
                    self._last_window = self.progress_bar.window()
                    self._sync_window_inputs()
                    self._update_progress_bar(completed_iter)
                else:
                    self.progress_bar.setValue(0)
                    self.progress_bar.setFormat(
                        f"'{self.rt.__class__.__name__}.{self.data_processor_name}' did not return a valid iteration count"
                    )

            except Exception as e:
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat(f"Error: {str(e)}")
                logger.error(e, exc_info=True)


        # make plot
        method_name = self.plot_registry.get(self.current_plot_name)
        if not method_name:
            return
        if self.completed_iter is not None:
            try:
                self.open_step.emit("plotting")
                # Call plot into ax
                axs = self.make_plot(self.canvas.figure, method_name, prepare_pcm=True,
                                     force_remake_axes=force)
                self.canvas.draw()
            except Exception as e:
                logger.error(f"Error making plot '{method_name}': {e}", exc_info=True)


    def make_plot(self, figure: Figure, plot_method_name: str, prepare_pcm=False, force_remake_axes=False):
        """
        make the plot with plot_method_name in the given figure

        The figure is laid out (`tight_layout`) every time: each update draws new titles, labels and
        tick labels, which need room the previous layout did not leave.
        """
        # get plot function
        plot_method = getattr(self.rt, plot_method_name)
        # get plot kwargs from gui input
        plot_kwargs = parse_inputs(self.plot_inputs)

        # --- Defensive check for missing axes ---
        if self.current_plot_uses_axs and (self.plot_axes is None or force_remake_axes):
            if not force_remake_axes:
                logger.warning("Expected plot_axes to exist but found None. Rebuilding...")
            figure.clf()
            self.plot_axes = figure.subplots(*self.current_plot_axs_shape)
            self.last_axs_shape = self.current_plot_axs_shape
            self._needs_layout = True

        if self.current_plot_uses_axs is True:
            axs = self.plot_axes
            flat_axes = list(np.asarray(axs).flat)
            if len(figure.axes) > len(flat_axes):# if plotter is making new axes somehow (colorbar)
                figure.clf()
                self.plot_axes = figure.subplots(*self.current_plot_axs_shape)
                self.last_axs_shape = self.current_plot_axs_shape
                self._needs_layout = True
                axs = self.plot_axes  # update `axs` too
            else:
                for ax in flat_axes:
                    ax.cla()
                    ax.set_aspect("auto")
            plot_method(axs=axs, **plot_kwargs)

        elif self.current_plot_uses_axs is False:
            figure.clf()
            plot_method(fig=figure, **plot_kwargs)
            axs = figure.axes
        else:
            figure.clf()
            logger.error(f"Plot function '{plot_method.__name__}' must accept either `axs` or `fig`.")
            return

        for idx, ax in enumerate(np.asarray(axs).flat):
            # add z display for pcm plots
            if prepare_pcm:
                # do this first so that we don't have to prepare edges over and over again when we hover
                is_pcm = _prepare_pcm_edges(ax)
                if is_pcm:
                    ax.format_coord = partial(format_pcm_coord, ax)
            # apply right-click options
            key = (self.current_plot_name, idx)
            for label, _, handler in self.right_click_actions:
                if label in self.checked_right_click_flags[key]:
                    handler(ax)
        figure.tight_layout()
        self._needs_layout = False
        self._laid_out_size = self.canvas.get_width_height()
        return axs

    def _on_canvas_resize(self, _event):
        self._needs_layout = True
        self._relayout_timer.start()        # restarts while a resize is still going on

    def _relayout_after_resize(self):
        figure = self.canvas.figure
        if not figure.axes or self._laid_out_size == self.canvas.get_width_height():
            return                          # nothing drawn yet, or already laid out at this size
        try:
            figure.tight_layout()
        except Exception as e:
            logger.debug(f"tight_layout after resize failed: {e}")
            return
        self._needs_layout = False
        self._laid_out_size = self.canvas.get_width_height()
        self.canvas.draw_idle()


    def _run_finished(self, completed_iter):
        """Whether acquisition is over, so the bar can total what actually ran.

        Deliberately conservative -- while a run is still going we must keep showing the
        requested count, or the bar would pin at 100% with a meaningless ETA. Being wrong
        is cheap and self-correcting, since the maximum is recomputed on every pass.

        No single on-disk marker covers every case: `.stop` is only written by this GUI's
        STOP button, and the plot files only appear if `finalize()` ran. A run whose host
        died mid-flight (as 021116 did) leaves neither, so staleness is the last resort --
        scaled by the run's OWN mean iteration period, so that a legitimately slow runtime
        (minutes per iteration) is never mistaken for a dead one.
        """
        if not self.data_path:
            return False
        try:
            if (Path(self.data_path) / STOP_INDICATOR_FILE).exists():
                return True
            if self.total_iter is not None and completed_iter >= self.total_iter:
                return True
            last = self.get_latest_update_time()
            if not last:
                return False
            elapsed = last - self.get_creation_time()
            per_iter = elapsed / completed_iter if completed_iter > 0 and elapsed > 0 else 0.0
            return (time.time() - last) > max(60.0, 5 * per_iter)
        except Exception as e:
            logger.debug(f"could not decide whether the run is finished: {e}")
            return False

    def _update_progress_bar(self, completed_iter):
        # Once the run is over, total the iterations that actually ran rather than the
        # requested `iterations` kwarg: a run stopped at 496 of 1000 reads 496/496 with a
        # full bar, and the iteration window then spans the whole width of it.
        finished = self._run_finished(completed_iter)
        if finished and completed_iter > 0:
            denom = int(completed_iter)
            self.progress_bar.setMaximum(denom)
            if self.total_iter is not None and self.total_iter != denom:
                self.progress_bar.setToolTip(
                    f"Run ended after {denom} of {self.total_iter} requested iterations.")
            else:
                self.progress_bar.setToolTip("")
        else:
            denom = self.total_iter
            self.progress_bar.setMaximum(self.total_iter if self.total_iter is not None else 1)
            self.progress_bar.setToolTip("")

        self.progress_bar.setValue(int(completed_iter))

        # A finished run has no denominator worth showing -- the requested count moves to
        # the tooltip, and the bar simply states how many iterations there are.
        count_txt = f"{completed_iter} iterations" if finished else f"{completed_iter}/{denom}"

        # Spell the window out in the bar text: the two flanking boxes give the endpoints,
        # this gives the count, so the inclusive convention never has to be reasoned about.
        win = self.progress_bar.window()
        win_txt = f" | win {win[0]}-{win[1]} ({win[1] - win[0] + 1} it)" if win else ""

        try:
            start_time = self.get_creation_time()
            last_update_time = self.get_latest_update_time()
            elapsed = last_update_time - start_time

            if elapsed > 1e-3 and completed_iter > 0:
                elapsed_str = format_duration(elapsed)
                rate = completed_iter / elapsed  # iterations per second
                if self.total_iter is not None:
                    remaining_iter = self.total_iter - completed_iter
                    remaining = remaining_iter / rate if rate > 0 else float("inf")
                    remaining_str = format_duration(remaining)
                else:
                    remaining_str = "?"

                time_per_iter_ms = 1 / rate * 1000
                if time_per_iter_ms > 1000:
                    rate_str = f"{time_per_iter_ms / 1000:.2f} s/it"
                elif time_per_iter_ms < 0.1:
                    rate_str = f"{time_per_iter_ms * 1000:.1f} µs/it"
                else:
                    rate_str = f"{time_per_iter_ms:.1f} ms/it"
                if finished:  # no ETA to give, and the requested count is in the tooltip
                    fmt = f"{count_txt} | {elapsed_str} | {rate_str}"
                else:
                    fmt = f"{count_txt} | {elapsed_str} < {remaining_str} | {rate_str}"
            else:
                fmt = f"{count_txt} | waiting..."

            self.progress_bar.setFormat(fmt + win_txt)

        except Exception as e:
            self.progress_bar.setFormat(f"{count_txt}{win_txt} | ETA error: {e}")
            logger.error(f"ETA error: {e}", exc_info=True)


    # ----------- iteration window --------------------------------------
    @staticmethod
    def _processor_supports_window(func):
        """Whether a data processor accepts the iteration-window kwarg.

        Passing an unexpected kwarg would raise TypeError in every existing runtime, so
        the window is only ever handed to processors that declare it (or take **kwargs).
        """
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return False
        if ITER_WINDOW_KWARG in params:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _reset_iteration_window(self):
        was_blocked = self.window_button.blockSignals(True)
        self.window_button.setChecked(False)
        self.window_button.blockSignals(was_blocked)
        self.window_button.setEnabled(False)
        self.window_button.setToolTip("")
        self._style_window_button(False)
        self.progress_bar.set_window_mode(False)
        self.progress_bar.set_available(0)
        self.window_start_input.setVisible(False)
        self.window_stop_input.setVisible(False)
        self._supports_window = False
        self._window_by_gui = False
        self._last_window = None
        self._last_available = 0

    def _style_window_button(self, checked):
        # filled with the progress bar's own fill colour while the window is active
        if checked:
            pal = self.progress_bar.palette()
            fill, text = pal.highlight().color().name(), pal.highlightedText().color().name()
            self.window_button.setStyleSheet(
                f"QToolButton {{ background-color: {fill}; color: {text}; "
                f"border: 1px solid {fill}; border-radius: 2px; }}")
        else:
            self.window_button.setStyleSheet("")

    def _set_window_tooltip(self, tip):
        self.window_button.setToolTip(
            f"<p style='white-space:pre'>{tip}</p>"
            f"<p>How a runtime enables it:</p>"
            f"<pre style='font-family:Consolas,\"DejaVu Sans Mono\",monospace'>{html.escape(ITER_WINDOW_EXAMPLE)}</pre>")

    def _disable_gui_window(self):
        name = self.runtime_class.__name__
        logger.warning(f"{name} sets {ITER_WINDOWABLE_ATTR} = True, but {self.data_processor_name}() never called "
                       f"reshape_iq_data_by_axes, so the iteration window cannot be applied; turning it off")
        was_blocked = self.window_button.blockSignals(True)
        self.window_button.setChecked(False)
        self.window_button.blockSignals(was_blocked)
        self._style_window_button(False)
        self.progress_bar.set_window_mode(False)
        self.window_start_input.setVisible(False)
        self.window_stop_input.setVisible(False)
        self.window_button.setEnabled(False)
        self._supports_window = self._window_by_gui = False
        self._set_window_tooltip(f"Unavailable: <code>{name}</code> sets <code>{ITER_WINDOWABLE_ATTR} = True</code>, but "
                                 f"its processor reshapes its data without <code>reshape_iq_data_by_axes</code>.")

    def toggle_iteration_window(self, checked):
        self._style_window_button(checked)
        self.progress_bar.set_window_mode(checked)
        self.window_start_input.setVisible(checked)
        self.window_stop_input.setVisible(checked)
        self._sync_window_inputs()
        self._last_window = None  # make sure the next pass actually reprocesses
        self.update_plot(force=True)

    def _sync_window_inputs(self):
        win = self.progress_bar.window()
        if win is None:
            return
        self.window_start_input.setText(str(win[0]))
        self.window_stop_input.setText(str(win[1]))

    def _on_window_moving(self, i0, i1):
        # cheap: text only, no reprocessing while the mouse is down
        self.window_start_input.setText(str(i0))
        self.window_stop_input.setText(str(i1))

    def _on_window_changed(self, i0, i1):
        self._sync_window_inputs()
        self.update_plot(force=True)

    def _on_window_text_edited(self):
        if not self.progress_bar.window_mode():
            return
        try:
            i0 = int(float(self.window_start_input.text()))
            i1 = int(float(self.window_stop_input.text()))
        except ValueError:
            self._sync_window_inputs()  # bad input: snap back to the real window
            return
        if (i0, i1) == self.progress_bar.window():
            return
        self.progress_bar.set_window(i0, i1)
        self._sync_window_inputs()
        self.update_plot(force=True)


    # ----------- kwarg inputs ------------------------------------------
    def is_annotated_type(self, annotation):
        origin = get_origin(annotation)
        if origin is Annotated:
            return True
        if origin is Union:
            # Check if any of the union args are Annotated
            return any(get_origin(arg) is Annotated for arg in get_args(annotation))
        return False

    def add_kwarg_input(self, row_layout: QHBoxLayout, label: str, default="",
                        annotation=None, update_callback:Callable=None):
        label_widget = QLabel(label)
        widget = None
        # input fields with type hint bool will show as check box
        if annotation is bool:
            widget = QCheckBox()
            widget.setChecked(bool(default))
            if update_callback is not None:
                widget.stateChanged.connect(update_callback)

        # input fields with type hint Literal will show as combobox (drop down menu)
        elif get_origin(annotation) is Literal:
            choices = get_args(annotation)
            widget = self._make_literal_kwarg(default, choices, update_callback)

        elif self.is_annotated_type(annotation):
            for arg in get_args(annotation):
                if get_origin(arg) is Annotated:
                    annotation = arg
            if len(get_args(annotation)) < 3:
                logger.error("Annotated args must contain at least 3 items: (type, desc, *metadata)")
                return None
            type_hint, desc, *metadata = get_args(annotation)
            match desc:
                # we should have a convention of capitalizing 1st letter, lower case is for backward compatibility here.
                case "slider"|"Slider":
                    widget = self._make_slider_kwarg(default, *metadata, update_callback=update_callback)
                case "IOConfig":
                    if len(metadata)!=1:
                        logger.warning("IOConfig args must contain exactly 1 item: "
                                       "`{channel_name}.{entry1}.{entry2}...`. Got {metadata}. Using the 1st item only")
                    try:
                        config_path = metadata[0].split(".")
                        io, entries = config_path[0], config_path[1:]
                        choices = self.rt._ios[io].get_config(*entries).keys()
                        widget = self._make_literal_kwarg(default, choices, update_callback=update_callback)
                    except Exception as e:
                        logger.error(f"Error parsing annotated argument: {e}. Falling back to user input")
                        widget = self._make_manual_input_kwarg(default, update_callback)

                case _:
                    logger.error(f"Unsupported Annotated description: {desc}")
                    return None

        else: # generic inputs
            widget = self._make_manual_input_kwarg(default, update_callback)

        # Add label and widget side by side
        row_layout.addWidget(label_widget)
        if isinstance(widget, tuple) or isinstance(widget, list):
            for widg in widget:
                widg.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                row_layout.addWidget(widg, 1)
            return widget[0] # assuming the first widget always has a gettable value
        else:
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            row_layout.addWidget(widget, 1)
            return widget

    def create_inputs_from_signature(self, func, layout: QVBoxLayout, group_box: QGroupBox,
                                     update_callback:Callable=None):
        clear_layout(layout)

        # --- Parse signature and type hints ---
        sig = inspect.signature(func)
        type_hints = get_type_hints(func, include_extras=True)

        widgets = {}
        row_layout = QHBoxLayout()
        items_in_row = 0
        max_items_per_row = 4

        for name, param in sig.parameters.items():
            # `iter_window` is driven by the progress-bar window, not by a text box
            if name in {"self", "axs", "fig", ITER_WINDOW_KWARG}:
                continue

            default = param.default if param.default is not inspect.Parameter.empty else ""
            annotation = type_hints.get(name, None)

            # Add input pair
            widget = self.add_kwarg_input(row_layout, name, default, annotation, update_callback=update_callback)
            if widget is not None:
                widgets[name] = widget

            # Add spacing between pairs
            row_layout.addSpacing(20)

            items_in_row += 1
            if items_in_row >= max_items_per_row:
                layout.addLayout(row_layout)
                row_layout = QHBoxLayout()
                items_in_row = 0

        if items_in_row > 0:
            if items_in_row <= 2:
                row_layout.addStretch(1)  # keep single pair left-aligned
            layout.addLayout(row_layout)

        group_box.setVisible(bool(widgets))
        return widgets


    def _make_literal_kwarg(self, default, choices, update_callback=None):
        widget = QComboBox()
        widget.setObjectName("kwarg_combo_box")
        widget.addItems([str(c) for c in choices])
        if default in choices:
            widget.setCurrentText(str(default))
        if update_callback is not None:
            widget.currentIndexChanged.connect(update_callback)
        return widget

    def _make_manual_input_kwarg(self, default, update_callback=None):
        widget = QLineEdit()
        widget.setText(str(default))

        def handle_return():
            try: # generic inputs will try to be evaluated
                val = _safe_eval(widget.text())
                widget.setText(str(val))
            except Exception:
                pass
            if update_callback is not None:
                update_callback()

        widget.returnPressed.connect(handle_return)
        return widget

    def _make_slider_kwarg(self, default, *metadata, update_callback=None):
        if len(metadata) != 1:
            logger.warning("Slider metadata must contain exactly one item of type str")
        if isinstance(metadata[0], str):
            if metadata[0].startswith("self."):
                arg = metadata[0][5:]  # remove "self." prefix
                if not hasattr(self.rt, arg):
                    logger.error(f"Runtime does not have attribute '{arg}' for slider")
                    return None
                values = np.array(self.rt.__getattribute__(arg))
            else:
                # can implement other metadata types here
                logger.error(f"Unsupported slider metadata type: {type(metadata[0])}")
                return None

        elif isinstance(metadata[0], (list, np.ndarray)):
            values = np.array(metadata[0])
        else:
            logger.warning(f"Unsupported slider metadata type: {type(metadata[0])}")
            return None

        # Find initial index
        if isinstance(default, (int, float)):
            init_idx = np.argmin(np.abs(values - float(default)))
        else:
            init_idx = np.argmin(np.abs(values - values.mean()))


        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(0)
        slider.setMaximum(len(values) - 1)
        slider.setSingleStep(1)
        slider.setPageStep(1)
        slider.setValue(init_idx)
        slider.setTickPosition(QSlider.TicksBothSides)
        slider.setTickInterval(1)
        init_val = values[init_idx]
        if abs(init_val) < 1e-3 or abs(init_val) >= 1e+4:
            init_val_str = f"{init_val:.4e}"  # Scientific for very small or large numbers
        else:
            init_val_str = f"{init_val:.4f}"  # Standard format otherwise
        lineedit = QLineEdit(init_val_str)
        lineedit.setFixedWidth(80)

        def slider_changed(i):
            val = values[i]
            if abs(val) < 1e-3 or abs(val) >= 1e+4:
                val_str = f"{val:.4e}"  # Scientific for very small or large numbers
            else:
                val_str = f"{val:.4f}"  # Standard format otherwise
            
            lineedit.setText(val_str)
            if update_callback is not None:
                update_callback()

        slider.valueChanged.connect(slider_changed)

        def lineedit_changed():
            try:
                val = float(lineedit.text())
                if abs(val) < 1e-3 or abs(val) >= 1e+4:
                    val_str = f"{val:.4e}"  # Scientific for very small or large numbers
                else:
                    val_str = f"{val:.4f}"  # Standard format otherwise
                lineedit.setText(val_str)
                idx = np.argmin(np.abs(values - val))
                slider.setValue(idx)

            except Exception as e:
                logger.error(f"Error setting slider value: {metadata}, {e}", exc_info=True)

        lineedit.returnPressed.connect(lineedit_changed)

        return (lineedit, slider)
    
    # -------------- update buttons ---------------------------
    def create_update_buttons(self):
        """
        create the update button layout
        """
        clear_layout(self.update_buttons_layout)
        self.update_buttons_layout.setSpacing(4)
        self.update_buttons_layout.setContentsMargins(2, 2, 2, 2)

        # --- get registered button methods ---
        self.update_button_registary = get_registered_button_methods(self.rt)
        widgets = {}
        row_layout = QHBoxLayout()
        row_layout.setSpacing(4)
        items_in_row = 0
        max_items_per_row = 6

        for button_name, method_name in self.update_button_registary.items():
            method = getattr(self.rt, method_name)
            sig = inspect.signature(method)

            has_kwargs = any(
                name not in {"self"}
                for name in sig.parameters
            )

            # if update method has input:
                # make a new row_layout with kwarg inputs and the button
            if has_kwargs:
                # flush current button row
                if row_layout.count() > 0:
                    self.update_buttons_layout.addLayout(row_layout)
                    row_layout = QHBoxLayout()
                    items_in_row = 0

                full_row = QHBoxLayout()
                # input_widgets = self.create_inputs_from_signature(method, QVBoxLayout(), self.update_buttons_box)
                # for w in input_widgets.values():
                #     full_row.addWidget(w)

                input_widgets = {}
                input_row = QHBoxLayout()
                sig = inspect.signature(method)
                type_hints = get_type_hints(method)

                for name, param in sig.parameters.items():
                    if name == "self":
                        continue
                    default = param.default if param.default is not inspect.Parameter.empty else ""
                    annotation = type_hints.get(name, None)
                    widget = self.add_kwarg_input(input_row, name, default, annotation)
                    input_widgets[name] = widget

                full_row.addLayout(input_row)

                button = QPushButton()
                button.setText(button_name)
                button.setObjectName("update_button")
                button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                full_row.addWidget(button)
                self.update_buttons_layout.addLayout(full_row)

                widgets[button_name] = (button, input_widgets)
            else:
                #make row layout with buttons
                button = QPushButton()
                button.setObjectName("update_button")
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                button.setText(button_name)
                row_layout.addWidget(button)
                widgets[button_name] = (button, {})
                items_in_row += 1
                if items_in_row >= max_items_per_row:
                    self.update_buttons_layout.addLayout(row_layout)
                    row_layout = QHBoxLayout()
                    items_in_row = 0

        if items_in_row > 0:
            self.update_buttons_layout.addLayout(row_layout)

        self.update_buttons_box.setVisible(bool(widgets))
        return widgets

    def refresh_update_button_methods(self):
        """
        Reconnect the buttons to the bound methods of the updated runtime instance.
        """
        for button_name, method_name in self.update_button_registary.items():
            button, input_widgets = self.update_buttons[button_name]

            def _update_method_try(checked=False, method_name=method_name, input_widgets=input_widgets):
                try:
                    if self.progress_bar.window() is not None:
                        # an update must use the fit from ALL the data, never a windowed one
                        logger.info(f"Turning the iteration window off before `{method_name}`")
                        self.window_button.setChecked(False)  # reprocesses on all iterations
                    kwargs = parse_inputs(input_widgets)
                    getattr(self.rt, method_name)(**kwargs)  # self.rt is reloaded on every reprocess
                except Exception as e:
                    logger.error(e, exc_info=True)

            try:
                button.clicked.disconnect()
            except TypeError:
                pass

            button.clicked.connect(_update_method_try)

    # ---------- right click options --------------------------
    def handle_right_click(self, event):
        if event.button != 3 or event.inaxes is None:
            return

        ax = event.inaxes
        plot_name = self.current_plot_name
        ax_list = self.canvas.figure.axes
        try:
            ax_index = ax_list.index(ax)
        except ValueError:
            return  # shouldn't happen

        key = (plot_name, ax_index)
        # Parented to the window: on Wayland a menu without a parent is attached to whichever
        # window had focus last, and opens in the wrong place after a focus change.
        menu = QMenu(self.window())

        for label, is_checkable, handler in self.right_click_actions:
            action = QAction(label, self)
            action.setCheckable(is_checkable)
            if is_checkable:
                action.setChecked(label in self.checked_right_click_flags[key])

                def toggler(checked, lbl=label, k=key, h=handler, ax=ax):
                    state_key = (k[0], k[1], lbl)  # (plot_name, ax_index, label)

                    if checked:
                        # snapshot BEFORE applying the action
                        self._ax_state_before_action[state_key] = self._capture_ax_state(ax)
                        self.checked_right_click_flags[k].add(lbl)
                        h(ax)
                    else:
                        self.checked_right_click_flags[k].discard(lbl)
                        # restore if we have a snapshot
                        state = self._ax_state_before_action.pop(state_key, None)
                        if state is not None:
                            self._restore_ax_state(ax, state)
                            self.canvas.draw_idle()

                action.toggled.connect(toggler)
            else:
                action.triggered.connect(lambda checked=False, h=handler: h(ax))

            menu.addAction(action)

        gui_event = getattr(event, "guiEvent", None)        # at the click itself
        menu.exec_(self.canvas.mapToGlobal(gui_event.pos()) if gui_event is not None
                   else QtGui.QCursor.pos())
        menu.deleteLater()      # the window owns it now

    def autoscale_ax(self, ax):
        # Autoscale line/plot data
        ax.set_autoscale_on(True)
        if not ax.collections and not ax.images:
            ax.relim()  # safe for normal line plots
        ax.autoscale_view()

        # Try to autoscale color data (like pcolormesh)
        for artist in ax.get_children():
            if hasattr(artist, 'get_array') and hasattr(artist, 'set_clim'):
                arr = artist.get_array()
                if arr is not None:
                    data = arr.compressed() if hasattr(arr, 'compressed') else arr
                    if data.size > 0:
                        artist.set_clim(vmin=data.min(), vmax=data.max())

        self.canvas.draw_idle()

    def _capture_ax_state(self, ax):
        state = {
            "xlim": ax.get_xlim(),
            "ylim": ax.get_ylim(),
        }

        # store a representative clim (first collection/image with clim)
        clim = None
        for artist in list(ax.collections) + list(ax.images):
            if hasattr(artist, "get_clim") and hasattr(artist, "set_clim"):
                try:
                    clim = artist.get_clim()
                    break
                except Exception:
                    pass
        state["clim"] = clim
        return state

    def _restore_ax_state(self, ax, state):
        ax.set_xlim(*state["xlim"])
        ax.set_ylim(*state["ylim"])
        if state.get("clim") is not None:
            for artist in list(ax.collections) + list(ax.images):
                if hasattr(artist, "set_clim"):
                    try:
                        artist.set_clim(*state["clim"])
                    except Exception:
                        pass
        self.canvas.draw_idle()


    # --------------- snapshot for note taking -------------------------------------
    def snapshot_current_plot(self):
        """
        Create a high DPI version of the plot, then scale it down for easier fitting into onenote pages, then copy that
        image into clipboard.

        We have to do in this way because directly scaling will just average over pixels, which makes the final
        image very blurry.

        """
        # --- Create the high-DPI figure ---
        fig = Figure(figsize=(self.snapshot_original_width_inch, self.snapshot_original_height_inch),
                     dpi=self.snapshot_original_dpi)
        canvas = FigureCanvasAgg(fig)

        # --- Call the registered plot method to make a new plot ---
        method_name = self.plot_registry.get(self.current_plot_name)
        if method_name and hasattr(self.rt, method_name):
            try:
                plot_func = getattr(self.rt, method_name)
                plot_kwargs = parse_inputs(self.plot_inputs)
                if self.current_plot_uses_axs:
                    axs = fig.subplots(*self.current_plot_axs_shape)
                    fig, axs = plot_func(axs=axs, **plot_kwargs)
                else:
                    fig, axs = plot_func(fig=fig, **plot_kwargs)

                # --- apply right-click options and set the x/ylim of the snapshot to be the same as the current figure
                current_axs = self.canvas.figure.get_axes()
                for idx, (ax_snap, ax_now) in enumerate(zip(np.asarray(axs).flat, np.asarray(current_axs).flat)):
                    # apply right-click options
                    key = (self.current_plot_name, idx)
                    for label, _, handler in self.right_click_actions:
                        if label in self.checked_right_click_flags[key]:
                            handler(ax_snap)
                    # set x/ylim
                    ax_snap.set_xlim(*(ax_now.get_xlim()))
                    ax_snap.set_ylim(*(ax_now.get_ylim()))

                fig.tight_layout()

            except Exception as e:
                logger.error(f"Failed to remake plot '{self.current_plot_name}' for snapshot: {e}", exc_info=True)
                return

        else:
            logger.error("Plot method not found.")
            return
        

        # --- Draw canvas and grab as raw buffer ---
        canvas.draw()
        raw_data = canvas.buffer_rgba()
        w, h = canvas.get_width_height()

        # --- Create QImage from buffer ---
        qimg = QImage(raw_data, w, h, QImage.Format_RGBA8888)

        # --- Now down-scale the QImage ---
        target_width = int(w * self.snapshot_scale_factor)
        target_height = int(h * self.snapshot_scale_factor)
        scaled_qimg = qimg.scaled(target_width, target_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # --- Add top margin with text ---
        margin_height = 40
        final_width = scaled_qimg.width()
        final_height = scaled_qimg.height() + margin_height

        final_image = QImage(final_width, final_height, QImage.Format_RGB32)
        final_image.fill(Qt.white)

        painter = QPainter(final_image)
        painter.setPen(Qt.black)
        painter.setFont(QFont("Arial", self.snapshot_title_font))
        max_char = int(self.snapshot_original_width_inch * self.snapshot_scale_factor / self.snapshot_title_font * 1000)
        text = f"{shorten_path_for_display(self.data_path, max_char)}\n{self.current_plot_name}"
        painter.drawText(QtCore.QRect(10, 0, final_width - 20, margin_height), Qt.AlignHCenter | Qt.AlignVCenter, text)
        painter.drawImage(0, margin_height, scaled_qimg)
        painter.end()


        # if on WSL, we have to first save the figure to a temp file, then transfer it to windows clipboard via
        # powershell, because WSL doesn't have direct access to windows clipboard.
        if detect_platform() == "wsl":
            # --- Save to PNG ---
            temp_dir = f"/tmp"
            temp_path = os.path.join(temp_dir, "snapshot.png")
            final_image.save(temp_path, "PNG")

            # --- Wait for file to appear ---
            timeout = 2
            start_time = time.time()
            while not os.path.exists(temp_path):
                if time.time() - start_time > timeout:
                    raise RuntimeError(f"Timeout waiting for snapshot file {temp_path}")
                time.sleep(0.05)

            # --- Copy to clipboard using PowerShell ---
            powershell_script = f"""
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $img = [System.Drawing.Image]::FromFile('{to_windows_path(temp_path)}')
            [System.Windows.Forms.Clipboard]::SetImage($img)
            """

            try:
                subprocess.run(
                    ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-Command', powershell_script],
                    check=True
                )
                logger.info(f"Snapshot {self.data_path}/{self.current_plot_name} copied to clipboard!")
            except Exception as e:
                logger.error(f"Failed to copy snapshot to clipboard: {e}", exc_info=True)

        # if on actual linux, we can just directly copy to clipboard.
        else:
            # --- Copy directly to clipboard ---
            try:
                clipboard = QApplication.clipboard()
                clipboard.setImage(final_image)
                logger.info(f"Snapshot {self.data_path}/{self.current_plot_name} copied to clipboard!")
            except Exception as e:
                logger.error(f"Failed to copy snapshot to clipboard: {e}", exc_info=True)

        # clean up
        del canvas
        fig.clf()
        del fig

    def show_snapshot_settings(self):
        """
        menu for adjusting the snapshot parameters
        """
        menu = QMenu(self)
        # --- Create a small QWidget to hold all input fields ---
        widget = QWidget()
        layout = QGridLayout(widget)

        dpi_input = QLineEdit(str(self.snapshot_original_dpi))
        width_input = QLineEdit(str(self.snapshot_original_width_inch))
        height_input = QLineEdit(str(self.snapshot_original_height_inch))
        scale_input = QLineEdit(str(self.snapshot_scale_factor))
        font_input = QLineEdit(str(self.snapshot_title_font))

        for lineedit in (dpi_input, width_input, height_input, scale_input, font_input):
            lineedit.setFixedWidth(50)

        label_ = QLabel("Original DPI:")
        label_.setToolTip("DPI of the original figure before scaling")
        layout.addWidget(label_, 0, 0)
        layout.addWidget(dpi_input, 0, 1)
        label_ = QLabel("Original width (in):")
        label_.setToolTip("Width of the original figure before scaling")
        layout.addWidget(label_, 1, 0)
        layout.addWidget(width_input, 1, 1)
        label_ = QLabel("Original height (in):")
        label_.setToolTip("Height of the original figure before scaling")
        layout.addWidget(label_, 2, 0)
        layout.addWidget(height_input, 2, 1)
        label_ = QLabel("Scale Factor:")
        label_.setToolTip("Scaling factor applied to the original figure, for copying into clipboard")
        layout.addWidget(label_, 3, 0)
        layout.addWidget(scale_input, 3, 1)
        label_ = QLabel("Title Font Size:")
        label_.setToolTip("Font size of the data path title")
        layout.addWidget(label_, 4, 0)
        layout.addWidget(font_input, 4, 1)


        widget_action = QWidgetAction(menu)
        widget_action.setDefaultWidget(widget)
        menu.addAction(widget_action)

        # --- Add OK and Cancel buttons at the bottom ---
        ok_action = QAction("OK", self)
        menu.addAction(ok_action)
        def accept():
            try:
                self.snapshot_original_dpi = int(dpi_input.text())
                self.snapshot_original_width_inch = float(width_input.text())
                self.snapshot_original_height_inch = float(height_input.text())
                self.snapshot_scale_factor = float(scale_input.text())
                self.snapshot_title_font = int(font_input.text())
            except ValueError:
                pass
            menu.close()
        ok_action.triggered.connect(accept)

        # --- Connect Enter key (returnPressed) ---
        for lineedit in (dpi_input, width_input, height_input, scale_input, font_input):
            lineedit.returnPressed.connect(accept)

        # # --- Make menu release the button when it closes ---
        def reset_button():
            self.snapshot_button.setDown(False)
            self.snapshot_button.update()
        menu.aboutToHide.connect(reset_button)

        # --- Show the menu just below the snapshot button ---
        menu.exec_(self.snapshot_button.mapToGlobal(QtCore.QPoint(0, self.snapshot_button.height())))

    # ---------- stop button behaviour ---------------
    def drop_stop_flag(self):
        stop_path = Path(self.data_path) / STOP_INDICATOR_FILE
        stop_path.touch(exist_ok=True)
        logger.info(f"Dropped '{STOP_INDICATOR_FILE}' in {self.data_path}")
        self.update_stop_button_state()

    def update_stop_button_state(self):
        """
        Checks if the `.stop` file exists and updates the STOP button accordingly.
        Disables the button and updates text if already stopped.
        """
        if not self.data_path:
            return
        stop_path = Path(self.data_path) / STOP_INDICATOR_FILE
        if stop_path.exists():
            if self.stop_button.isEnabled():
                self.stop_button.setEnabled(False)
                self.stop_button.setText("STOPPED")
        else:
            self.stop_button.setEnabled(True)
            self.stop_button.setText("STOP")


    # --------- theme ------------
    def set_theme(self, theme_name):
        dark = "dark" in theme_name.lower()

        # refresh our toolbar icons (currentColor) + matplotlib's nav icons
        self._pause_icon = get_icon("pause_plot.svg")
        self._resume_icon = get_icon("resume_plot.svg")
        self.pause_button.setIcon(self._resume_icon if self.is_paused else self._pause_icon)
        self.snapshot_button.setIcon(get_icon("snapshot_plot.svg"))
        style_mpl_toolbar(self.toolbar, dark)

        from matplotlib import style as mpl_style
        from matplotlib import rcdefaults
        rcdefaults()
        if dark:
            # mplcyberpunk is an optional extra; fall back to a built-in dark
            # style if it's missing or ever fails to load, so it can never break
            # plotting.
            try:
                import mplcyberpunk  # noqa: F401
                mpl_style.use("cyberpunk")
            except Exception:
                mpl_style.use("dark_background")
        else:
            mpl_style.use("default")

        self.update_plot(force=True, reprocess_data=False)

    # ------------- clear -----------------
    def clear(self):
        self.stop()

        clear_layout(self.process_kwargs_layout)
        clear_layout(self.plot_kwargs_layout)
        clear_layout(self.update_buttons_layout)

        self.process_inputs = {}
        self.plot_inputs = {}
        self.update_buttons = {}

        # Reset dropdown and state
        self.plot_selector.clear()
        self.checked_right_click_flags.clear()
        self._ax_state_before_action.clear()
        self.current_plot_name = None
        self.plot_registry = {}

        # Progress bar reset
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0/0")
        self._reset_iteration_window()

        # STOP button reset
        self.stop_button.setEnabled(True)
        self.stop_button.setText("STOP")

        # Also clear these for sanity
        self._full_folder_path = ""
        self.folder_label.setText(" ")
        self.folder_label.setToolTip("")
        self.data_path = None
        self.rt = None
        self.runtime_class = None
        self.data_processor_name = None
        self.current_plot_uses_axs = None
        self.current_plot_axs_shape = None
        self.plot_axes = None
        self.last_axs_shape = None

        self.ready = False
        self.completed_iter = None
        self.is_paused = False
        self.pause_button.setIcon(self._pause_icon)
        self.last_mtime = 0
        self._needs_layout = True
        self._fresh_load_mtime = None

        self.canvas.figure.clf()
        self.canvas.draw()

        # The kwarg/button group boxes just got emptied above. The QVBoxLayout
        # that places these 3 boxes (kwargs_row) caches each box's minimum size
        # per-widget (QWidgetItemV2) independent of the box's own, freshly-small
        # minimumSizeHint(); that cache is flushed only by updateGeometry() on
        # the box itself.
        for box in (self.process_kwargs_box, self.plot_kwargs_box, self.update_buttons_box):
            box.updateGeometry()

        # Even with the per-box caches flushed, the ancestor layouts' own
        # cached total size isn't recomputed until something processes the
        # QEvent::LayoutRequest that updateGeometry() posts -- which Qt skips
        # delivering while this widget is hidden (as it is whenever we're not
        # the active stack page, i.e. exactly when clear() tends to run).
        # Force a synchronous recompute instead so this widget's own minimum size
        # shrinks back down immediately rather than staying pinned at
        # whatever it was the last time kwargs were populated here.
        self.layout().invalidate()
        self.layout().activate()
