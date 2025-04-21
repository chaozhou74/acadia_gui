import os
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QProgressBar, QLineEdit, QLabel, QComboBox
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PyQt5.QtCore import QTimer
from PyQt5 import QtCore, QtGui
import time

from acadia_qmsmt.helpers import load_runtime_from_data_dir
from acadia_gui import AXS_SHAPE_TAG
from acadia_gui.helpers import get_registered_plot_methods, get_data_process_method

# files used for rough estimate of progress rate, ETA, etc
UPDATE_INDICATOR_FILE = "metadata.txt" # file whose last modified time indicates the last data update
CREATE_INDICATOR_FILE = "run.py" # file whose creation time indicates the experiment time


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

        # --- Matplotlib figure/canvas ---
        self.canvas = FigureCanvasQTAgg(Figure())
        self.canvas.figure.set_facecolor("none") # will be set later
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        self.toolbar.setObjectName("livePlotToolBar")  # tags for styling with css
        self.canvas.setObjectName("livePlotCanvas")


        # --- Plot selector ---
        self.plot_selector = QComboBox()
        self.plot_selector.currentIndexChanged.connect(self.select_plot)

        # --- Polling interval input ---
        self.pause_button = QPushButton("Pause Plot")
        self.pause_button.clicked.connect(self.toggle_pause)

        self.interval_input = QLineEdit(str(poll_interval_sec))
        self.interval_input.setFixedWidth(60)
        self.interval_input.setToolTip("Polling interval (in seconds)")
        self.interval_input.editingFinished.connect(self.update_poll_interval)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Plot:"))
        interval_row.addWidget(self.plot_selector)
        interval_row.addStretch()
        interval_row.addWidget(QLabel("Poll every:"))
        interval_row.addWidget(self.interval_input)
        interval_row.addWidget(QLabel("sec"))
        interval_row.addWidget(self.pause_button)

        # --- Progress bar ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setFormat("0/0")

        # --- Layout setup ---
        layout = QVBoxLayout(self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)
        layout.addLayout(interval_row)
        layout.addWidget(self.progress_bar)
        self.setLayout(layout)

        # --- Timer for update ---
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)

    def start(self, data_path):
        self.data_path = data_path
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
            print(e)
            self.data_processor_name = None

        # Set default plot
        self.current_plot_name = self.plot_selector.currentText()
        self.ready = True  # safe to allow plotting now
        self.update_plot(force=True)
        self.timer.start(self.poll_interval_ms)


    def stop(self):
        self.timer.stop()
        self.canvas.figure.clf()
        self.canvas.draw()
        self.ready = False


    def toggle_pause(self):
        self.is_paused = not self.is_paused
        self.pause_button.setText("Resume Plot" if self.is_paused else "Pause Plot")

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

            # load runtime and process current data
            self.rt = load_runtime_from_data_dir(self.data_path)

            completed_iter = None
            if hasattr(self.rt, self.data_processor_name):
                processor_func = getattr(self.rt, self.data_processor_name)
                completed_iter = processor_func() # todo: allows this to take some arguments
            else:
                self.progress_bar.setFormat(
                    f"Missing processor: {self.data_processor_name}"
                )
                return

            # Get selected plot method
            method_name = self.plot_registry.get(self.current_plot_name)
            if not method_name:
                return

            plot_method = getattr(self.rt, method_name)

            # Clear and call plot into ax
            self.canvas.figure.clf()
            axs_shape = getattr(plot_method, AXS_SHAPE_TAG, (1, 1))
            axs = self.canvas.figure.subplots(*axs_shape)
            plot_method(axs=axs)
            self.canvas.draw()

            if completed_iter is not None:
                # self.progress_bar.setValue(completed_iter)
                # self.progress_bar.setFormat(f"{completed_iter}/{self.total_iter or '?'}")
                self._update_progress_bar(completed_iter)
            else:
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat(
                    f"'{self.rt.__class__.__name__}.{self.data_processor_name}' did not return a valid iteration count"
                )

        except Exception as e:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(f"Error: {str(e)}")

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


    # todo: right-click options on images
    # todo: plot function arguments

