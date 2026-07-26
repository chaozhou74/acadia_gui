import os
import pickle
import logging
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QStackedLayout, QHBoxLayout,
    QSizePolicy
)
from PyQt5.QtGui import QPixmap, QFontMetrics
from PyQt5.QtCore import Qt

from acadia_gui.gui.live_plot_widget import LivePlotWidget

logger = logging.getLogger(__name__)

# Image formats we can render in the plot view.
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tif', '.tiff')

class FigureDisplayWidget(QWidget):
    def __init__(self):
        super().__init__()

        # PNG view widgets
        self.figure_selector = QComboBox()
        self.figure_selector.currentIndexChanged.connect(self.show_selected_image)

        from PyQt5.QtWidgets import QFrame

        # Frame to wrap the image and provide background
        self.image_frame = QFrame()
        # self.image_frame.setStyleSheet("background-color: #ffffff; border: 1px solid #B0B0B0;")

        frame_layout = QVBoxLayout(self.image_frame)
        frame_layout.setContentsMargins(2, 2, 2, 2)

        self.image_label = QLabel("No image")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(400, 400)
        self.image_label.setObjectName("plotBackground") # tag for styling with css

        frame_layout.addWidget(self.image_label)

        self.load_pickle_button = QPushButton("Load pickle")
        self.load_pickle_button.clicked.connect(self.load_pickle)

        self.switch_to_live_button = QPushButton("Live Mode")
        self.switch_to_live_button.clicked.connect(self.plot_live_mode)

        # Layout for buttons in one row
        button_row = QHBoxLayout()
        button_row.addWidget(self.load_pickle_button)
        button_row.addWidget(self.switch_to_live_button)

        self.png_view = QWidget()
        self.folder_label = QLabel(" ")
        self.folder_label.setAlignment(Qt.AlignCenter)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Deep/long folder paths must not dictate this widget's minimum width
        # (an un-elided QLabel's minimum width is its full text width, which
        # can exceed the whole window for deeply nested paths and jam the
        # splitter). Ignored lets the layout shrink it freely; the full path
        # is still available via elided text + tooltip.
        self.folder_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._full_folder_path = ""

        png_layout = QVBoxLayout(self.png_view)
        png_layout.addWidget(self.folder_label)
        png_layout.addWidget(self.figure_selector)
        png_layout.addWidget(self.image_frame, stretch=1)
        png_layout.addLayout(button_row)


        # Live plot widget
        self.live_plot = LivePlotWidget()

        # Stacked layout for toggling view modes
        self.stack = QStackedLayout()
        self.stack.addWidget(self.png_view)     # index 0
        self.stack.addWidget(self.live_plot)    # index 1

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.addLayout(self.stack)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.setLayout(main_layout)

        self.png_paths = []
        self.folder_path = None
        self.load_pickle_button.setEnabled(False)
        self.switch_to_live_button.setEnabled(False)

    def load_images(self, folder_path, is_data_folder=True):
        """Display the images found in ``folder_path``.

        :param is_data_folder: Whether this is an Acadia data folder (has run.py).
            Data-folder-only features (live mode, loading the matplotlib
            pickle, and the live-plot fallback for empty folders) are only
            enabled for actual data folders. Plain folders simply show
            whatever pictures they contain.
        """
        self.folder_path = folder_path
        image_files = [f for f in os.listdir(folder_path)
                       if f.lower().endswith(IMAGE_EXTENSIONS)]
        self.png_paths = sorted(os.path.join(folder_path, f) for f in image_files)
        self.figure_selector.clear()

        if self.png_paths:
            self.stack.setCurrentIndex(0)
            self._full_folder_path = folder_path
            self._update_folder_label()
            self.load_pickle_button.setEnabled(is_data_folder)
            self.switch_to_live_button.setEnabled(is_data_folder)
            self.figure_selector.addItems([os.path.basename(f) for f in self.png_paths])
            self.show_selected_image(0)
            self.live_plot.stop()
        elif is_data_folder:
            self.plot_live_mode()
        else:
            self.image_label.setText("No images to display")

    def plot_live_mode(self):
        if self.folder_path:
            self.load_pickle_button.setEnabled(False)
            try:
                self.live_plot.clear()
                self.stack.setCurrentIndex(1)
                self.live_plot.start(self.folder_path)
            except Exception as e:
                self.stack.setCurrentIndex(0)
                self.image_label.setText(f"Failed to load live plot: {e}")
                logger.error(e, exc_info=True)


    def show_selected_image(self, index):
        if index < 0 or index >= len(self.png_paths):
            return
        pixmap = QPixmap(self.png_paths[index])
        if pixmap.isNull():
            self.image_label.setText("Failed to load image")
        else:
            self.image_label.clear()
            self.image_label.setPixmap(pixmap.scaled(
                self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.stack.currentIndex() == 0:
            self.show_selected_image(self.figure_selector.currentIndex())
        self._update_folder_label()

    def _update_folder_label(self):
        metrics = QFontMetrics(self.folder_label.font())
        elided = metrics.elidedText(self._full_folder_path, Qt.ElideMiddle, self.folder_label.width())
        self.folder_label.setText(elided)
        self.folder_label.setToolTip(self._full_folder_path)

    def load_pickle(self):
        index = self.figure_selector.currentIndex()
        if index < 0 or index >= len(self.png_paths):
            return

        png_path = self.png_paths[index]
        base_name = os.path.splitext(os.path.basename(png_path))[0]
        pkl_path = os.path.join(self.folder_path, base_name + ".pkl")

        if not os.path.isfile(pkl_path):
            logger.warning(f"No .pkl file found for {base_name}")
            return

        try:
            with open(pkl_path, 'rb') as f:
                fig = pickle.load(f)
            if hasattr(fig, "show"):
                fig.show()
            else:
                logger.error("Pickled object is not a matplotlib figure.")
        except Exception as e:
            logger.error(f"Failed to load or show .pkl: {e}", exc_info=True)

    def set_theme(self, theme_name):
        self.live_plot.set_theme(theme_name)

    def clear(self):
        self.figure_selector.clear()
        self.image_label.clear()
        self.image_label.setText("Not a data folder (missing run.py)")
        self.load_pickle_button.setEnabled(False)
        self.switch_to_live_button.setEnabled(False)
        self.live_plot.clear()
        self._full_folder_path = ""
        self.folder_label.setText(" ")
        self.folder_label.setToolTip("")
        self.stack.setCurrentIndex(0)
