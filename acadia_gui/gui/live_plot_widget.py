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

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import ScalarFormatter

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QWidgetAction, QMenu, QAction, QApplication, QToolButton, QPushButton,
    QProgressBar, QLineEdit, QLabel, QComboBox, QGroupBox, QGridLayout, QCheckBox, QSizePolicy, QFrame, QSlider
)
from PyQt5.QtCore import QTimer, Qt, QSize
from PyQt5 import QtCore, QtGui
from PyQt5.QtGui import QImage, QPainter, QFont, QIcon

from acadia_qmsmt.utils.saved_runtime_loader import get_saved_runtime_class, load_runtime_from_data_dir
from acadia_qmsmt.utils import get_registered_plot_methods, get_data_process_method, get_registered_button_methods
from acadia_qmsmt.utils.annotation import AXS_SHAPE_TAG, get_registered_methods, get_registered_customizer
from acadia_qmsmt.utils.path_adapter import to_windows_path, detect_platform

from acadia_gui.icons import get_icon, style_mpl_toolbar


# files used for rough estimate of progress rate, ETA, etc
UPDATE_INDICATOR_FILE = "metadata.txt" # file whose last modified time indicates the last data update
CREATE_INDICATOR_FILE = "run.py" # file whose creation time indicates the experiment time
STOP_INDICATOR_FILE = ".stop"

TOTAL_ITER_ATTRIBUTE = "iterations" # runtime class attribute that defines the total number of iterations
DATAMANAGER_ATTRIBUTE = "data" # runtime class attribute for data manager

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


class LivePlotWidget(QWidget):
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
        self.plot_axes = None
        self.last_axs_shape = None

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
        self.canvas = FigureCanvasQTAgg(Figure())
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
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setFormat("0/0")
        # ---- stop button ------------
        self.stop_button = QPushButton()
        self.stop_button.setText("STOP")
        self.stop_button.setObjectName("stop_button")
        self.stop_button.setFixedWidth(80)
        self.stop_button.clicked.connect(self.drop_stop_flag)


        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.stop_button)
        self.stop_button.setFixedHeight(self.progress_bar.sizeHint().height())

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
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)

        # --- Autoscale cache ----
        self._ax_state_before_action = {}  # (plot_name, ax_index, label) -> state dict


    def start(self, data_path):
        self.data_path = data_path
        # self.folder_label.setText(f"{os.path.basename(data_path)}")
        self.folder_label.setText(data_path)
        self.is_paused = False
        self.last_mtime = 0
        self.completed_iter = None

        # Load the saved acadia_qmsmt.py as the acadia_qmsmt.qmsmt submodule
        try:
            # Get the saved runtime class and load runtime, with the saved acadia_qmsmt
            self.runtime_class = get_saved_runtime_class(data_path, use_saved_qmsmt=True)

        except Exception as e:
            logger.warning(f"Failed to load the saved acadia_qmsmt.qmsmt submodule : {e}. "
                           f"Using the global version", exc_info=True)
            self.runtime_class = get_saved_runtime_class(data_path, use_saved_qmsmt=False)

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

        if hasattr(self.rt, self.data_processor_name):
            processor_func = getattr(self.rt, self.data_processor_name)
            self.process_inputs = self.create_inputs_from_signature(processor_func, self.process_kwargs_layout,
                                                                    self.process_kwargs_box,
                                                                    update_callback=lambda: self.update_plot(force=True))
            self.update_buttons = self.create_update_buttons()

        # Disable STOP button if .stop file already exists
        self.update_stop_button_state()

        # Set default plot
        self.current_plot_name = self.plot_selector.currentText()
        self.ready = True  # safe to allow plotting now
        self.select_plot(self.plot_selector.currentIndex())
        self.timer.start(self.poll_interval_ms)

    def stop(self):
        self.timer.stop()
        self.canvas.figure.clf()
        self.canvas.draw()
        self.ready = False
        self.completed_iter = None


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

        # if we already have data, just redo plot
        if self.completed_iter is not None:
            try:
                self.make_plot(self.canvas.figure, method_name, prepare_pcm=True)
            except Exception as e:
                self.canvas.figure.clf()
                logger.error(f"Error in making plot '{method_name}', {e}", exc_info=True)
            self.canvas.draw()
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

        if reprocess_data:
            try:
                # reload runtime data
                try:
                    getattr(self.rt, DATAMANAGER_ATTRIBUTE).load(self.data_path)
                except Exception as e:
                    logger.warning(f"Failed to load data from existing runtime: {e}. Attempting full reload...",
                                   exc_info=True)
                    try:
                        self.rt = self.runtime_class.load(self.data_path)
                        logger.info(f"Reloaded runtime from {self.data_path}")
                    except Exception as e2:
                        logger.error(f"Failed to reload runtime: {e2}", exc_info=True)
                        return

                completed_iter = None
                if hasattr(self.rt, self.data_processor_name):
                    processor_func = getattr(self.rt, self.data_processor_name)
                    proc_kwargs = parse_inputs(self.process_inputs)
                    try:
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
                    self._update_progress_bar(completed_iter)
                    self.completed_iter = completed_iter
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
                # Call plot into ax
                axs = self.make_plot(self.canvas.figure, method_name, prepare_pcm=True, force_remake_axes=force)
                self.canvas.draw()
            except Exception as e:
                logger.error(f"Error making plot '{method_name}': {e}", exc_info=True)


    def make_plot(self, figure: Figure, plot_method_name: str, prepare_pcm=False, force_remake_axes=False):
        """
        make the plot with plot_method_name in the given figure
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

        if self.current_plot_uses_axs is True:
            axs = self.plot_axes
            flat_axes = list(np.asarray(axs).flat)
            if len(figure.axes) > len(flat_axes):# if plotter is making new axes somehow (colorbar)
                figure.clf()
                self.plot_axes = figure.subplots(*self.current_plot_axs_shape)
                self.last_axs_shape = self.current_plot_axs_shape
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
        return axs


    def _update_progress_bar(self, completed_iter):
        self.progress_bar.setValue(int(completed_iter))

        try:
            start_time = self.get_creation_time()
            last_update_time = self.get_latest_update_time()
            elapsed = last_update_time - start_time

            if elapsed > 1e-3 and completed_iter > 0:
                elapsed_str = time.strftime('%H:%M:%S', time.gmtime(elapsed))
                rate = completed_iter / elapsed  # iterations per second
                if self.total_iter is not None:
                    remaining_iter = self.total_iter - completed_iter
                    remaining = remaining_iter / rate if rate > 0 else float("inf")
                    remaining_str = time.strftime('%H:%M:%S', time.gmtime(remaining))
                else:
                    remaining_str = "?"

                time_per_iter_ms = 1 / rate * 1000
                if time_per_iter_ms > 1000:
                    rate_str = f"{time_per_iter_ms / 1000:.2f} s/it"
                elif time_per_iter_ms < 0.1:
                    rate_str = f"{time_per_iter_ms * 1000:.1f} µs/it"
                else:
                    rate_str = f"{time_per_iter_ms:.1f} ms/it"
                fmt = f"{completed_iter}/{self.total_iter} | {elapsed_str} < {remaining_str} | {rate_str}"
            else:
                fmt = f"{completed_iter}/{self.total_iter} | waiting..."

            self.progress_bar.setFormat(fmt)

        except Exception as e:
            self.progress_bar.setFormat(f"{completed_iter}/{self.total_iter} | ETA error: {e}")
            logger.error(f"ETA error: {e}", exc_info=True)


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
            if name in {"self", "axs", "fig"}:
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
            update_method = getattr(self.rt, method_name)
            button, input_widgets = self.update_buttons[button_name]

            def _update_method_try(checked=False, update_method=update_method, input_widgets=input_widgets):
                try:
                    kwargs = parse_inputs(input_widgets)
                    update_method(**kwargs)
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
        menu = QMenu()

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

        menu.exec_(QtGui.QCursor.pos())

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

        # STOP button reset
        self.stop_button.setEnabled(True)
        self.stop_button.setText("STOP")

        # Also clear these for sanity
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

        self.canvas.figure.clf()
        self.canvas.draw()