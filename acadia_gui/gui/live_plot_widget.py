import os
import time
import inspect
from functools import partial
from typing import get_type_hints, Literal, get_args
from collections import defaultdict
import subprocess
import logging
import gc

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import ScalarFormatter

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QWidgetAction, QMenu, QAction, QApplication, QToolButton,
    QProgressBar, QLineEdit, QLabel, QComboBox, QGroupBox, QGridLayout, QCheckBox, QSizePolicy, QFrame
)
from PyQt5.QtCore import QTimer, Qt, QSize
from PyQt5 import QtCore, QtGui
from PyQt5.QtGui import QImage, QPainter, QFont, QIcon

from acadia_qmsmt.helpers import load_runtime_from_data_dir
from acadia_gui import AXS_SHAPE_TAG
from acadia_gui.helpers import get_registered_plot_methods, get_data_process_method
from acadia_gui.helpers.path_adapter import to_windows_path, detect_platform
from acadia_gui.icons import ICON_PATH

# files used for rough estimate of progress rate, ETA, etc
UPDATE_INDICATOR_FILE = "metadata.txt" # file whose last modified time indicates the last data update
CREATE_INDICATOR_FILE = "run.py" # file whose creation time indicates the experiment time

logger = logging.getLogger("acadia_gui.live_plot")

def parse_inputs(input_dict):
    kwargs = {}
    for k, w in input_dict.items():
        if isinstance(w, QCheckBox):
            kwargs[k] = w.isChecked()
        elif isinstance(w, QComboBox):
            kwargs[k] = w.currentText()
        elif isinstance(w, QLineEdit):
            try:
                val = eval(w.text())
                kwargs[k] = val
            except Exception:
                kwargs[k] = w.text()
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
        self.folder_label = QLabel(" ")
        self.folder_label.setAlignment(Qt.AlignCenter)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

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

        # --- Plot snapshot button -------
        self.snapshot_button = QToolButton()
        self.snapshot_button.setIcon(QIcon(os.path.join(ICON_PATH, "snapshot_plot.svg")))
        self.snapshot_button.setToolTip("Snapshot plot\n"
                                        "Create a high-DPI figure and scale it down\n"
                                        "for easier fitting in notebooks.\n"
                                        "Right-click for settings.")
        self.snapshot_button.setIconSize(self.toolbar.iconSize())
        self.snapshot_button.clicked.connect(self.snapshot_current_plot)
        self.snapshot_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.snapshot_button.customContextMenuRequested.connect(self.show_snapshot_settings)


        # --- Polling interval input ---
        self._pause_icon = QIcon(os.path.join(ICON_PATH, "pause_plot.svg"))
        self._resume_icon = QIcon(os.path.join(ICON_PATH, "resume_plot.svg"))
        self.pause_button = QToolButton()
        self.pause_button.setIcon(self._pause_icon)
        self.pause_button.setToolTip("Pause plotting")
        self.pause_button.setIconSize(self.toolbar.iconSize())
        self.pause_button.clicked.connect(self.toggle_pause)


        self.interval_input = QLineEdit(str(poll_interval_sec))
        self.interval_input.setFixedWidth(30)
        self.interval_input.setToolTip("Polling interval (in seconds)")
        self.interval_input.editingFinished.connect(self.update_poll_interval)

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

        # --- Progress bar ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setFormat("0/0")


        # --- Input regions for kwargs ---
        self.process_kwargs_box = QGroupBox("Process kwargs")
        self.process_kwargs_box.setFlat(False)
        self.process_kwargs_layout = QVBoxLayout()
        self.process_kwargs_box.setLayout(self.process_kwargs_layout)

        self.plot_kwargs_box = QGroupBox("Plot kwargs")
        self.plot_kwargs_box.setFlat(False)
        self.plot_kwargs_layout = QVBoxLayout()
        self.plot_kwargs_box.setLayout(self.plot_kwargs_layout)

        kwargs_row = QVBoxLayout()
        kwargs_row.addWidget(self.process_kwargs_box)
        kwargs_row.addWidget(self.plot_kwargs_box)


        # --- Layout setup ---
        layout = QVBoxLayout(self)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.plot_selector)
        layout.addWidget(self.canvas, stretch=1)
        layout.addLayout(interval_row)
        layout.addWidget(self.progress_bar)
        layout.insertLayout(layout.indexOf(self.progress_bar), kwargs_row)
        self.setLayout(layout)

        # --- Timer for update ---
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)


    def start(self, data_path):
        self.data_path = data_path
        # self.folder_label.setText(f"{os.path.basename(data_path)}")
        self.folder_label.setText(data_path)
        self.is_paused = False
        self.last_mtime = 0
        self.rt = load_runtime_from_data_dir(self.data_path)

        self.total_iter =  getattr(self.rt, "iterations", 1)
        self.progress_bar.setMaximum(self.total_iter)

        # Get all registered plots and populate dropdown
        self.plot_registry = get_registered_plot_methods(self.rt)
        self.plot_selector.clear()
        self.plot_selector.addItems(list(self.plot_registry.keys()))
        try:
            self.data_processor_name = get_data_process_method(self.rt)
        except AttributeError as e:
            logger.error(e)
            self.data_processor_name = None

        if hasattr(self.rt, self.data_processor_name):
            processor_func = getattr(self.rt, self.data_processor_name)
            self.process_inputs = self.create_inputs_from_signature(processor_func, self.process_kwargs_layout,
                                                                    self.process_kwargs_box)


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
            if seconds < 0.1:
                seconds = 0.1
            self.poll_interval_ms = int(seconds * 1000)
            self.timer.setInterval(self.poll_interval_ms)
        except ValueError:
            self.interval_input.setText(str(self.poll_interval_ms / 1000))


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
            self.plot_inputs = self.create_inputs_from_signature(plot_func, self.plot_kwargs_layout,
                                                                 self.plot_kwargs_box)
        self.checked_right_click_flags.clear()
        self.update_plot(force=True)


    def update_plot(self, force=False):
        if not self.ready:
            return

        if self.is_paused or not self.data_path or not self.current_plot_name:
            return

        try:
            current_mtime = self.get_latest_update_time()
            if not force and current_mtime <= self.last_mtime:
                return
            self.last_mtime = current_mtime

            # Clear and load runtime and process current data
            # need to reload runtime because that's how data got updated
            self.canvas.figure.clf()
            gc.collect()
            self.rt = load_runtime_from_data_dir(self.data_path)

            completed_iter = None
            if hasattr(self.rt, self.data_processor_name):
                processor_func = getattr(self.rt, self.data_processor_name)
                proc_kwargs = parse_inputs(self.process_inputs)
                completed_iter = processor_func(**proc_kwargs)
            else:
                self.progress_bar.setFormat(
                    f"Missing processor: {self.data_processor_name}"
                )
                return

            # Get selected plot method
            method_name = self.plot_registry.get(self.current_plot_name)
            if not method_name:
                return

            # Call plot into ax
            axs = self.make_plot(self.canvas.figure, method_name, prepare_pcm=True)

            self.canvas.draw()

            if completed_iter is not None:
                self._update_progress_bar(completed_iter)
            else:
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat(
                    f"'{self.rt.__class__.__name__}.{self.data_processor_name}' did not return a valid iteration count"
                )

        except Exception as e:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(f"Error: {str(e)}")
            logger.error(e)


    def make_plot(self, figure: Figure, plot_method_name: str, prepare_pcm=False):
        """
        make the plot with plot_method_name in the given figure
        """
        # get plot function
        plot_method = getattr(self.rt, plot_method_name)
        # get plot kwargs from gui input
        plot_kwargs = parse_inputs(self.plot_inputs)
        # prepare plot axs
        axs_shape = getattr(plot_method, AXS_SHAPE_TAG, (1, 1))
        axs = figure.subplots(*axs_shape)
        # make plot
        plot_method(axs=axs, **plot_kwargs)

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
        return axs


    def _update_progress_bar(self, completed_iter):
        self.progress_bar.setValue(completed_iter)

        try:
            start_time = self.get_creation_time()
            last_update_time = self.get_latest_update_time()
            elapsed = last_update_time - start_time

            if elapsed > 1e-3 and completed_iter > 0:
                rate = completed_iter / elapsed  # iterations per second
                remaining_iter = self.total_iter - completed_iter
                remaining = remaining_iter / rate if rate > 0 else float("inf")

                elapsed_str = time.strftime('%H:%M:%S', time.gmtime(elapsed))
                remaining_str = time.strftime('%H:%M:%S', time.gmtime(remaining))
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
            logger.error(f"ETA error: {e}")

    def set_theme(self, theme_name):
        from matplotlib import style as mpl_style
        from matplotlib import rcdefaults
        rcdefaults()
        if "dark" in theme_name.lower():
            try:
                import mplcyberpunk1
                mpl_style.use("cyberpunk")
            except ModuleNotFoundError:
                mpl_style.use("dark_background")
        else:
            mpl_style.use("default")

        self.update_plot(force=True)


    def add_kwarg_input(self, row_layout: QHBoxLayout, label: str, default="", annotation=None):
        label_widget = QLabel(label)
        widget = None

        # input fields with type hint bool will show as check box
        if annotation is bool:
            widget = QCheckBox()
            widget.setChecked(bool(default))
            widget.stateChanged.connect(lambda: self.update_plot(force=True))

        # input fields with type hint Literal will show as combobox (drop down menu)
        elif hasattr(annotation, '__origin__') and annotation.__origin__ is Literal:
            widget = QComboBox()
            choices = get_args(annotation)
            widget.addItems([str(c) for c in choices])
            if default in choices:
                widget.setCurrentText(str(default))
            widget.currentIndexChanged.connect(lambda: self.update_plot(force=True))

        else: # generic inputs
            widget = QLineEdit()
            widget.setText(str(default))

            def handle_return():
                try: # generic inputs will try to be evaluated
                    val = eval(widget.text())
                    widget.setText(str(val))
                except Exception:
                    pass
                self.update_plot(force=True)

            widget.returnPressed.connect(handle_return)

        # Add label and widget side by side
        row_layout.addWidget(label_widget)
        row_layout.addWidget(widget)
        return widget


    def create_inputs_from_signature(self, func, layout: QVBoxLayout, group_box: QGroupBox):
        clear_layout(layout)

        # --- Parse signature and type hints ---
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        widgets = {}
        row_layout = QHBoxLayout()
        items_in_row = 0
        max_items_per_row = 4

        for name, param in sig.parameters.items():
            if name in {"self", "axs"}:
                continue

            default = param.default if param.default is not inspect.Parameter.empty else ""
            annotation = type_hints.get(name, None)

            # Add input pair
            self.add_kwarg_input(row_layout, name, default, annotation)
            widgets[name] = row_layout.itemAt(row_layout.count() - 1).widget()

            # Add spacing between pairs
            row_layout.addSpacing(20)

            items_in_row += 1
            if items_in_row >= max_items_per_row:
                layout.addLayout(row_layout)
                row_layout = QHBoxLayout()
                items_in_row = 0

        if items_in_row > 0:
            layout.addLayout(row_layout)

        group_box.setVisible(bool(widgets))
        return widgets


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

                def toggler(checked, lbl=label, k=key, h=handler):
                    if checked:
                        self.checked_right_click_flags[k].add(lbl)
                        h(ax)
                    else:
                        self.checked_right_click_flags[k].discard(lbl)

                action.toggled.connect(toggler)
            else:
                action.triggered.connect(lambda checked=False, h=handler: h(ax))

            menu.addAction(action)

        menu.exec_(QtGui.QCursor.pos())

    def autoscale_ax(self, ax):
        # Autoscale line/plot data
        ax.set_autoscale_on(True)
        ax.relim()
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
            axs = self.make_plot(fig, method_name)
            fig.tight_layout()
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
                logger.info("Snapshot copied to Windows clipboard using PowerShell!")
            except Exception as e:
                logger.error(f"Failed to copy to clipboard: {e}")

        # if on actual linux, we can just directly copy to clipboard.
        else:
            # --- Copy directly to clipboard ---
            try:
                clipboard = QApplication.clipboard()
                clipboard.setImage(final_image)
                logger.info("Snapshot copied to clipboard!")
            except Exception as e:
                logger.error(f"Failed to copy snapshot to clipboard: {e}")

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

        widget.setLayout(layout)

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

    # ------------- clear -----------------
    def clear(self):
        self.stop()

        clear_layout(self.process_kwargs_layout)
        clear_layout(self.plot_kwargs_layout)

        self.process_inputs = {}
        self.plot_inputs = {}

        # Reset dropdown and state
        self.plot_selector.clear()
        self.checked_right_click_flags.clear()
        self.current_plot_name = None
        self.plot_registry = {}

        # Progress bar reset
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0/0")

        # Also clear these for sanity
        self.data_path = None
        self.rt = None
        self.data_processor_name = None

        self.ready = False
        self.is_paused = False
        self.pause_button.setIcon(self._pause_icon)
        self.last_mtime = 0

        self.canvas.figure.clf()
        self.canvas.draw()




    # fixme: add update button.
