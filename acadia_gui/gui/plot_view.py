import os
import pickle
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QStackedLayout
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtCore import Qt

from acadia_gui.gui.live_plot_widget import LivePlotWidget


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

        self.png_view = QWidget()
        png_layout = QVBoxLayout(self.png_view)
        png_layout.addWidget(self.figure_selector)
        png_layout.addWidget(self.image_frame)
        png_layout.addWidget(self.load_pickle_button)

        # Live plot widget
        self.live_plot = LivePlotWidget()

        # Stacked layout for toggling view modes
        self.stack = QStackedLayout()
        self.stack.addWidget(self.png_view)     # index 0
        self.stack.addWidget(self.live_plot)    # index 1

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.addLayout(self.stack)
        self.setLayout(main_layout)

        self.png_paths = []
        self.folder_path = None
        self.load_pickle_button.setEnabled(False)

    def load_images(self, folder_path):
        self.folder_path = folder_path
        png_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.png')]
        self.png_paths = [os.path.join(folder_path, f) for f in png_files]
        self.figure_selector.clear()

        if self.png_paths:
            self.stack.setCurrentIndex(0)
            self.load_pickle_button.setEnabled(True)
            self.figure_selector.addItems([os.path.basename(f) for f in self.png_paths])
            self.show_selected_image(0)
            self.live_plot.stop()
        else:
            try:
                self.stack.setCurrentIndex(1)
                self.load_pickle_button.setEnabled(False)
                self.live_plot.start(folder_path)
            except Exception as e:
                self.stack.setCurrentIndex(0)
                self.load_pickle_button.setEnabled(False)
                self.image_label.setText(f"Failed to load live plot: {e}")


    def show_selected_image(self, index):
        if index < 0 or index >= len(self.png_paths):
            return
        pixmap = QPixmap(self.png_paths[index])
        if pixmap.isNull():
            self.image_label.setText("Failed to load image")
        else:
            self.image_label.setPixmap(pixmap.scaled(
                self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.stack.currentIndex() == 0:
            self.show_selected_image(self.figure_selector.currentIndex())

    def load_pickle(self):
        index = self.figure_selector.currentIndex()
        if index < 0 or index >= len(self.png_paths):
            return

        png_path = self.png_paths[index]
        base_name = os.path.splitext(os.path.basename(png_path))[0]
        pkl_path = os.path.join(self.folder_path, base_name + ".pkl")

        if not os.path.isfile(pkl_path):
            print(f"No .pkl file found for {base_name}")
            return

        try:
            with open(pkl_path, 'rb') as f:
                fig = pickle.load(f)
            if hasattr(fig, "show"):
                fig.show()
            else:
                print("Pickled object is not a matplotlib figure.")
        except Exception as e:
            print(f"Failed to load or show .pkl: {e}")

    def set_theme(self, theme_name):
        self.live_plot.set_theme(theme_name)

    def clear(self):
        self.figure_selector.clear()
        self.image_label.setText("Not a data folder (missing run.py)")
        self.load_pickle_button.setEnabled(False)
        self.live_plot.stop()
        self.stack.setCurrentIndex(0)
