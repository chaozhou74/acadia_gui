"""
Centre panel of the data browser: a live plot view and a pulse-sequence view with
a ``Plot | SeeQuence`` toggle above them.

Exposes the same ``clear`` / ``load_images`` / ``set_theme`` interface as
:class:`~.plot_view.FigureDisplayWidget`, so it drops into ``main_data_browser``
in place of it -- the browser keeps calling the same three methods.

The sequence view traces lazily: a folder selection refreshes the live view
immediately but only marks the sequence view dirty, tracing it when the user
actually switches to it (tracing costs ~0.1-0.3 s and folder clicks are frequent).
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QStackedWidget, QVBoxLayout, QWidget)

from .plot_view import FigureDisplayWidget

# Compatibility layer: the SeeQuence view needs acadia_qmsmt.sequence_viz, added in qmsmt
# 1.0. Against an older installed qmsmt the module is absent, so importing SequenceWidget
# (which imports it at module load) would break the whole browser. Probe for it first and
# only pull the widget in when present; otherwise CenterView greys the toggle out with a
# hover explaining why.
try:
    import acadia_qmsmt.sequence_viz  # noqa: F401  (presence probe)
    from .sequence_view import SequenceWidget
    SEQUENCE_AVAILABLE = True
except ImportError:
    SequenceWidget = None
    SEQUENCE_AVAILABLE = False

SEQUENCE_UNAVAILABLE_TIP = "Requires qmsmt version ≥ 1.0"

LIVE, SEQUENCE = 0, 1


class CenterView(QWidget):
    """Live plot view + pulse-sequence view, switched by a header toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.live = FigureDisplayWidget()
        # None when sequence_viz is unavailable -- the toggle is disabled, so the view is
        # never shown, but guard every use of it below anyway.
        self.sequence = SequenceWidget() if SEQUENCE_AVAILABLE else None

        self._folder = None
        self._is_data_folder = True
        self._sequence_dirty = True      # sequence needs a (re)trace before showing

        self.stack = QStackedWidget()
        self.stack.insertWidget(LIVE, self.live)
        if self.sequence is not None:
            self.stack.insertWidget(SEQUENCE, self.sequence)

        # compact segmented toggle -- two adjacent checkable buttons, exclusivity
        # enforced in set_mode
        self.btn_live = QPushButton("Plot")
        self.btn_sequence = QPushButton("SeeQuence")
        for button in (self.btn_live, self.btn_sequence):
            button.setCheckable(True)
            button.setObjectName("viewToggle")     # styled clickable in the themes
        self.btn_live.setChecked(True)
        self.btn_live.clicked.connect(lambda: self.set_mode(LIVE))
        self.btn_sequence.clicked.connect(lambda: self.set_mode(SEQUENCE))

        # what actually gets added to the header for the SeeQuence toggle: the bare button
        # when available, else a greyed (disabled) button. Qt won't show a tooltip on a
        # disabled widget, so the hover reason goes on an enabled wrapper around it.
        self._sequence_toggle = self.btn_sequence
        if not SEQUENCE_AVAILABLE:
            self.btn_sequence.setEnabled(False)
            self.btn_sequence.setToolTip(SEQUENCE_UNAVAILABLE_TIP)
            wrapper = QWidget()
            wrapper.setToolTip(SEQUENCE_UNAVAILABLE_TIP)
            inner = QHBoxLayout(wrapper)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(self.btn_sequence)
            self._sequence_toggle = wrapper

        # the selected folder's path, shown in the header and elided when too long
        self.path_label = QLabel("")
        self.path_label.setObjectName("centerPath")
        self.path_label.setToolTip("")
        # a deep path must not dictate the widget's width -- elide instead
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._full_path = ""

        header = QHBoxLayout()
        header.setSpacing(6)                        # two distinct, clickable buttons
        header.addWidget(self.btn_live)
        header.addWidget(self._sequence_toggle)
        header.addSpacing(12)
        header.addWidget(self.path_label, 1)        # fills the row; elides when long

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.stack, stretch=1)

    # ---------------- mode ----------------

    @property
    def mode(self):
        return self.stack.currentIndex()

    def set_mode(self, index):
        """Show LIVE or SEQUENCE; trace the sequence lazily the first time it shows."""
        if index == SEQUENCE and self.sequence is None:
            return                        # sequence_viz unavailable; toggle is disabled
        self.btn_live.setChecked(index == LIVE)
        self.btn_sequence.setChecked(index == SEQUENCE)
        self.stack.setCurrentIndex(index)
        if index == SEQUENCE and self._sequence_dirty:
            self.sequence.load_folder(self._folder)   # never raises
            self._sequence_dirty = False

    # ---------------- FigureDisplayWidget-compatible interface ----------------

    def load_images(self, folder_path, is_data_folder=True):
        """Refresh the live view now; mark the sequence view for lazy re-trace."""
        self._folder = folder_path
        self._is_data_folder = is_data_folder
        self._full_path = str(folder_path) if folder_path else ""
        self._update_path_label()
        self.live.load_images(folder_path, is_data_folder=is_data_folder)
        self._sequence_dirty = True
        if self.mode == SEQUENCE and self.sequence is not None:
            self.sequence.load_folder(folder_path)
            self._sequence_dirty = False

    def clear(self):
        self.live.clear()
        if self.sequence is not None:
            self.sequence.clear()
        self._folder = None
        self._sequence_dirty = True
        self._full_path = ""
        self._update_path_label()

    def set_theme(self, theme_name):
        self.live.set_theme(theme_name)
        if hasattr(self.sequence, "set_theme"):
            self.sequence.set_theme(theme_name)

    # ---------------- header path label ----------------

    def _update_path_label(self):
        metrics = QFontMetrics(self.path_label.font())
        self.path_label.setText(metrics.elidedText(
            self._full_path, Qt.ElideMiddle, max(self.path_label.width(), 0)))
        self.path_label.setToolTip(self._full_path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_path_label()
