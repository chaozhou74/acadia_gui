"""Dropdown lists drawn INSIDE the main window (WSLg only).

Under WSLg every Qt popup is a separate window bridged to Windows, and with the GUI scale != 1
(X11 backend, see utils.set_wsl_display_backend) a closed dropdown list stayed visible for ~1.2 s
(measured: still on screen 236 ms after picking an item, gone after ~1.2 s), and a list reopened
in that time could sit under the dead one. Drawing the list as an overlay child of the combo's
top-level window avoids separate windows entirely: it opens and closes instantly and clicks land
exactly where the item is drawn.

`install_inline_combo_popups()` routes every QComboBox's showPopup/hidePopup through the overlay.
Selection behaves like Qt's own popup: mouse click, Up/Down/PageUp/PageDown/Home/End, Enter to
choose, Escape or a click elsewhere to cancel; `activated` and `currentIndexChanged` are emitted
as usual.
"""
import logging

from PyQt5.QtCore import Qt, QObject, QEvent, QPoint, QRect
from PyQt5.QtWidgets import QApplication, QComboBox, QListView, QAbstractItemView, QFrame, QWidget

logger = logging.getLogger(__name__)


class _Overlay(QListView):
    def __init__(self, combo: QComboBox, parent):
        super().__init__(parent)
        self.combo = combo
        self.setObjectName("inlineComboPopup")
        self.setWindowFlags(Qt.Widget)
        self.setFrameShape(QFrame.StyledPanel)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setModel(combo.model())
        self.setModelColumn(combo.modelColumn())
        self.entered.connect(self.setCurrentIndex)          # highlight follows the mouse
        self.clicked.connect(lambda idx: self.choose(idx.row()))

    def choose(self, row):
        combo = self.combo
        self.close_overlay()
        if 0 <= row < combo.count():
            changed = row != combo.currentIndex()
            combo.setCurrentIndex(row)
            combo.activated[int].emit(row)
            combo.activated[str].emit(combo.itemText(row))
            if hasattr(combo, "textActivated"):
                combo.textActivated.emit(combo.itemText(row))
            if not changed:
                combo.update()
        combo.setFocus(Qt.PopupFocusReason)

    def close_overlay(self):
        if self.isVisible():
            self.hide()
        QApplication.instance().removeEventFilter(_closer)
        if _closer.overlay is self:
            _closer.overlay = None

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.choose(self.currentIndex().row())
        elif key == Qt.Key_Escape:
            self.close_overlay()
            self.combo.setFocus(Qt.PopupFocusReason)
        else:
            super().keyPressEvent(event)


class _OutsideClickCloser(QObject):
    """Closes the open overlay on a mouse press anywhere outside it (like a real popup)."""
    overlay = None

    def eventFilter(self, obj, event):
        ov = self.overlay
        if ov is None:
            return False
        # Mouse presses are delivered to QWindows too; only widgets tell us where the click was.
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and isinstance(obj, QWidget):
            try:
                inside = obj is ov or ov.isAncestorOf(obj)
            except RuntimeError:
                inside = False
            if not inside:
                on_combo = obj is ov.combo or ov.combo.isAncestorOf(obj)
                ov.close_overlay()
                return on_combo          # a click on the combo itself just closes the list
        elif event.type() in (QEvent.WindowDeactivate,) and obj is ov.window():
            ov.close_overlay()
        return False


_closer = _OutsideClickCloser()


def _show_popup(combo: QComboBox):
    top = combo.window()
    ov = getattr(combo, "_inline_overlay", None)
    if ov is None or ov.parent() is not top:
        if ov is not None:
            ov.deleteLater()
        ov = _Overlay(combo, top)
        combo._inline_overlay = ov
        # the overlay is a child of the top-level window, not of the combo: free it with the combo
        # (views like SeeQuence rebuild their dropdowns often)
        combo.destroyed.connect(ov.deleteLater)
    count = combo.count()
    if count == 0:
        return
    ov.setModel(combo.model())
    ov.setModelColumn(combo.modelColumn())
    rows = min(count, max(1, combo.maxVisibleItems()))
    row_h = max(ov.sizeHintForRow(0), combo.fontMetrics().height() + 6)
    frame = 2 * ov.frameWidth()
    height = rows * row_h + frame
    width = max(combo.width(), min(ov.sizeHintForColumn(0) + frame + 24, top.width()))
    if count > rows:
        ov.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        width += ov.verticalScrollBar().sizeHint().width()
    else:
        ov.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    below = combo.mapTo(top, QPoint(0, combo.height()))
    above_y = combo.mapTo(top, QPoint(0, 0)).y() - height
    x = max(0, min(below.x(), top.width() - width))
    y = below.y() if below.y() + height <= top.height() or above_y < 0 else above_y
    ov.setGeometry(QRect(x, y, width, height))
    ov.setCurrentIndex(combo.model().index(max(combo.currentIndex(), 0), combo.modelColumn()))
    ov.scrollTo(ov.currentIndex())
    ov.show()
    ov.raise_()
    ov.setFocus(Qt.PopupFocusReason)
    _closer.overlay = ov
    QApplication.instance().installEventFilter(_closer)


def _hide_popup(combo: QComboBox):
    ov = getattr(combo, "_inline_overlay", None)
    if ov is not None:
        ov.close_overlay()


def install_inline_combo_popups():
    """Route every QComboBox popup through the in-window overlay (idempotent)."""
    if getattr(QComboBox, "_acadia_inline_popups", False):
        return
    QComboBox._acadia_inline_popups = True
    QComboBox.showPopup = _show_popup
    QComboBox.hidePopup = _hide_popup
