import os
import html
import json
import time
import logging
import threading
from PyQt5.QtCore import Qt, QTimer, QEvent, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QFontMetrics
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QCheckBox, QPushButton, QLabel, QProgressBar, QMessageBox, QApplication
)

from acadia_gui.gui.gui_log_window import INSTRUMENT_LOG_LEVEL

logger = logging.getLogger(__name__)

def load_inst_params(path):
    inst_file = os.path.join(path, "inst_params.json")
    if not os.path.isfile(inst_file):
        return None
    try:
        with open(inst_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


# colors of the panel (and its tab) while parameters are loading, and of the load result.
# "loading" is a see-through tint (#AARRGGBB) over the panel; "pane" is the theme's panel color under it.
LOAD_COLORS = {
    "light": {"loading": "#38fff000", "loading_tree": "#38fff000", "loading_text": "#202020",
              "loading_border": None, "pane": "#efefef", "path": "#555555",
              "succeeded": "#2e8a57", "failed": "#c2453a", "not_available": "#8a8a8a"},
    "dark": {"loading": "#1ee6ff00", "loading_tree": "#16e6ff00", "loading_text": "#efff70",
             "loading_border": "#50e6ff00", "pane": "#3a3a3a", "path": "#a6a6a6",
             "succeeded": "#6dcf97", "failed": "#ef8579", "not_available": "#8e8e8e"},
}
LOAD_BUTTON_TEXT = "Load Parameters"
LOADING_BUTTON_TEXT = "Loading Parameters..."


def load_colors(theme_name=None):
    return LOAD_COLORS["dark" if theme_name and "dark" in str(theme_name).lower() else "light"]


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _qss_color(argb):
    c = QColor(argb)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"


def _close_client(client):
    """Close a station client that the station has replaced.

    After a failed call (e.g. a timeout) the station makes a new client and drops the old one with
    its socket still open. Garbage collection of that client's zmq context then blocks for good, on
    whichever thread happens to collect it; on the GUI thread the browser froze. The worker keeps a
    reference to the client during the call, so it is closed here and not collected while open.
    """
    try:
        client.disconnect()
        if getattr(client, "context", None) is not None:
            client.context.term()
    except Exception as e:
        logger.warning(f"Closing a replaced instrument client: {e}")


class _TintOverlay(QWidget):
    """A see-through color over a widget (here the tree's viewport), passing the mouse through.
    A see-through tree background in a stylesheet would also blank the item checkboxes."""
    def __init__(self, target):
        super().__init__(target)
        self._color = QColor(0, 0, 0, 0)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        target.installEventFilter(self)
        self.hide()

    def set_color(self, argb):
        self._color = QColor(argb)
        self.update()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._color)
        painter.end()


class InstrumentParamsViewer(QWidget):
    loading_changed = pyqtSignal(bool)          # parameters are loading / done
    go_to_runtime = pyqtSignal(str)             # "Go to runtime" in the load result popup
    _load_progress = pyqtSignal(int, str)       # (index, instrument) now being set, from the worker
    _load_finished = pyqtSignal(dict)           # instrument -> error text or None, from the worker

    def __init__(self, client_station=None):
        super().__init__()
        self.setObjectName("instrumentParamsViewer")
        self.setAttribute(Qt.WA_StyledBackground)

        self.client_station = client_station
        self.selected_instruments = set()
        self.inst_data = None
        self.folder_path = None
        self._colors = load_colors()

        self.layout = QVBoxLayout(self)

        # Notice label (e.g., when no file is found)
        self.notice_label = QLabel()
        self.notice_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.notice_label)
        self.notice_label.hide()

        # Select all checkbox
        self.select_all_checkbox = QCheckBox("Select/Unselect All")
        self.select_all_checkbox.stateChanged.connect(self.toggle_all_items)
        self.layout.addWidget(self.select_all_checkbox)

        # Tree widget with 2 columns
        self.tree = QTreeWidget()
        self.tree.setColumnWidth(0, 150)  # ~15 characters at 10px each
        self.tree.setHeaderLabels(["Parameter", "Value"])
        self.tree.setColumnCount(2)
        self.tree.itemChanged.connect(self.on_item_changed)
        self.layout.addWidget(self.tree)
        self._tree_tint = _TintOverlay(self.tree.viewport())

        # Progress of a load (instruments set / to set) and the instrument being set, then the
        # result. Some sets take long (a source ramping), so the instruments are set one by one on
        # a worker thread and the browser stays usable.
        self.load_bar = QProgressBar()
        self.load_bar.setFormat("%v/%m")
        self.load_bar.setAlignment(Qt.AlignCenter)
        bar_font = self.load_bar.font()
        bar_font.setPointSizeF(bar_font.pointSizeF() * 0.8)
        self.load_bar.setFont(bar_font)
        self.load_bar.setFixedHeight(QFontMetrics(bar_font).height() + 2)
        self.load_bar.hide()
        self.layout.addWidget(self.load_bar)
        self.load_status_label = QLabel()
        self.load_status_label.setTextFormat(Qt.RichText)
        self.load_status_label.setWordWrap(True)
        self.load_status_label.hide()
        self.layout.addWidget(self.load_status_label)
        self._loading = None                    # the running load (dict), None when idle
        self._load_t0 = 0.0
        self._load_tick_timer = QTimer(self)
        self._load_tick_timer.setInterval(100)
        self._load_tick_timer.timeout.connect(self._update_load_status)
        self._load_progress.connect(self._on_load_progress)
        self._load_finished.connect(self._on_load_finished)
        self._result_popup = None

        # Load button
        self.load_button = QPushButton(LOAD_BUTTON_TEXT)
        self.load_button.clicked.connect(self.load_selected_parameters)
        self.load_button.setEnabled(self.client_station is not None)
        self.layout.addWidget(self.load_button)
        if self.client_station is None:
            self.load_button.setToolTip("No Instrument client station found")

        self.setLayout(self.layout)

    def set_theme(self, theme_name):
        self._colors = load_colors(theme_name)
        if self._loading is not None:
            self._set_loading_style()

    def load_json(self, folder_path):
        self.clear_load_result()
        self.folder_path = folder_path
        self.tree.clear()
        self.selected_instruments.clear()
        self.select_all_checkbox.setChecked(False)
        self.notice_label.hide()
        self.tree.show()
        self.select_all_checkbox.show()
        self.load_button.show()
        self.load_button.setEnabled(self.client_station is not None and self._loading is None)

        self.inst_data = load_inst_params(folder_path)
        if not self.inst_data:
            self.tree.hide()
            self.select_all_checkbox.hide()
            self.load_button.setEnabled(False)
            self.notice_label.setText("inst_params.json not found")
            self.notice_label.show()
            return

        for inst_name, params in self.inst_data.items():
            inst_item = QTreeWidgetItem([inst_name])
            inst_item.setFlags(inst_item.flags() | Qt.ItemIsUserCheckable)
            inst_item.setCheckState(0, Qt.Unchecked)

            # Add parameters as children
            if isinstance(params, dict):
                for key, val in params.items():
                    if isinstance(val, dict):
                        sub_item = QTreeWidgetItem(["   " + key, "(submodule)"])
                        for subkey, subval in val.items():
                            leaf = QTreeWidgetItem(["   " + subkey, str(subval)])
                            sub_item.addChild(leaf)
                        inst_item.addChild(sub_item)
                    else:
                        param_item = QTreeWidgetItem(["   " + key, str(val)])
                        inst_item.addChild(param_item)

            self.tree.addTopLevelItem(inst_item)
            # self.tree.expandItem(inst_item)

    def toggle_all_items(self, state):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setCheckState(0, Qt.Checked if state == Qt.Checked else Qt.Unchecked)

    def on_item_changed(self, item, column):
        if item.parent() is None:  # top-level instrument
            name = item.text(0)
            if item.checkState(0) == Qt.Checked:
                self.selected_instruments.add(name)
            else:
                self.selected_instruments.discard(name)

    def get_selected_instruments(self):
        return sorted(self.selected_instruments)

    def load_selected_parameters(self):
        if not self.inst_data or self.client_station is None or self._loading is not None:
            return
        dd = {k: self.inst_data[k] for k in self.get_selected_instruments() if k in self.inst_data}
        if not dd:
            logger.warning("No instruments selected; nothing loaded.")
            return
        station_instruments = getattr(self.client_station, "instruments", None)
        not_available = [k for k in dd if station_instruments is not None and k not in station_instruments]
        inst_names = [k for k in dd if k not in not_available]
        logger.info(f"Loading parameters to instruments: {inst_names}")
        self.clear_load_result()
        self._loading = {"folder": self.folder_path, "inst_names": inst_names,
                         "not_available": not_available, "current": None}
        self._load_t0 = time.monotonic()
        self.load_button.setEnabled(False)
        self.load_button.setText(LOADING_BUTTON_TEXT)
        self.load_bar.setRange(0, max(len(inst_names), 1))
        self.load_bar.setValue(0)
        self.load_bar.show()
        self.load_status_label.show()
        self._update_load_status()
        self._load_tick_timer.start()
        self._set_loading_style()
        self.loading_changed.emit(True)

        station = self.client_station

        def emit(signal, *args):
            try:
                signal.emit(*args)
                return True
            except RuntimeError:        # the viewer was deleted while the server was busy
                return False

        def work():
            results = {}
            for i, inst_name in enumerate(inst_names):
                if not emit(self._load_progress, i, inst_name):
                    return
                client = getattr(station, "client", None)
                try:
                    station.set_parameters({inst_name: dd[inst_name]})
                    results[inst_name] = None
                except Exception as e:
                    results[inst_name] = f"{type(e).__name__}: {e}"
                finally:
                    if client is not None and getattr(station, "client", None) is not client:
                        _close_client(client)
            emit(self._load_finished, results)

        # one load at a time, so the station's server connection is never used by two threads
        threading.Thread(target=work, name="load-instrument-parameters", daemon=True).start()

    def _set_loading_style(self):
        c = self._colors
        border = f"border: 1px solid {_qss_color(c['loading_border'])};" if c["loading_border"] else ""
        self.setStyleSheet(
            f"#instrumentParamsViewer {{ background: {_qss_color(c['loading'])}; {border} }}"
            f"#instrumentParamsViewer QTreeWidget {{ color: {c['loading_text']}; {border} }}"
            f"#instrumentParamsViewer QLabel {{ background: transparent; color: {c['loading_text']}; }}")
        self._tree_tint.set_color(c["loading_tree"])
        self._tree_tint.setGeometry(self.tree.viewport().rect())
        self._tree_tint.show()

    def _on_load_progress(self, index, inst_name):
        if self._loading is None:
            return
        self._loading["current"] = inst_name
        self.load_bar.setValue(index)
        self._update_load_status()

    def _update_load_status(self):
        elapsed = time.monotonic() - self._load_t0
        current = self._loading["current"]
        self._set_load_status(f"Loading {current}, {elapsed:.1f}\u00a0s" if current else f"{elapsed:.1f}\u00a0s")

    def _on_load_finished(self, results):
        load = self._loading
        self._loading = None
        self._load_tick_timer.stop()
        self.load_bar.hide()
        self.load_button.setText(LOAD_BUTTON_TEXT)
        self.load_button.setEnabled(self.client_station is not None and bool(self.inst_data))
        self.setStyleSheet("")
        self._tree_tint.hide()
        self.loading_changed.emit(False)

        failed = {k: error for k, error in results.items() if error is not None}
        sections = [("Succeeded", [k for k, error in results.items() if error is None], "succeeded"),
                    ("Failed", list(failed), "failed"),
                    ("Not available", load["not_available"], "not_available")]
        for inst_name, error in failed.items():
            logger.error(f"Loading parameters to {inst_name} failed: {error}")
        self._log_load_result(load["folder"], sections)

        c = self._colors
        n_succeeded = len(sections[0][1])
        summary = [f'<span style="color:{c["succeeded"]}">Done setting {_plural(n_succeeded, "instrument")}</span>']
        if failed:
            summary.append(f'<span style="color:{c["failed"]}">{len(failed)} failed</span>')
        if load["not_available"]:
            summary.append(f'<span style="color:{c["not_available"]}">{len(load["not_available"])} not available</span>')
        self._set_load_status(", ".join(summary))

        # the names are painted only while the loaded run is the one shown
        if load["folder"] == self.folder_path:
            color_of = {k: key for _, names, key in sections for k in names}
            for item in self._inst_items():
                key = color_of.get(item.text(0))
                if key is not None:
                    item.setForeground(0, QColor(c[key]))
                if key == "failed":
                    item.setToolTip(0, failed[item.text(0)])
                elif key == "not_available":
                    item.setToolTip(0, "Not in this instrument station")
        self._show_load_result_popup(load["folder"], sections)

    def _log_load_result(self, folder, sections):
        c = self._colors
        styles = {"succeeded": {"italic": True}, "failed": {}, "not_available": {"bold": True}}
        runs = [("Load instrument parameters from runtime ", {}), (str(folder), {"italic": True})]
        for label, names, key in sections:
            if names:
                runs += [(f"; {label.lower()}: ", {}), (", ".join(names), {"color": c[key], **styles[key]})]
        logger.log(INSTRUMENT_LOG_LEVEL, "".join(text for text, _ in runs), extra={"gui_runs": runs})

    def _show_load_result_popup(self, folder, sections):
        c = self._colors
        n = sum(len(names) for _, names, _ in sections)
        lines = [f"Attempted loading <b>{n}</b> instrument{'' if n == 1 else 's'}' parameters from:",
                 f'<i style="color:{c["path"]}">{html.escape(str(folder))}</i>', ""]
        for label, names, key in sections:
            if names:
                bold = "; font-weight:bold" if key == "not_available" else ""
                lines += [f"{label}:", f'<span style="color:{c[key]}{bold}">{html.escape(", ".join(names))}</span>']
        if self._result_popup is not None:
            self._result_popup.close()
        popup = QMessageBox(QMessageBox.Warning if sections[1][1] else QMessageBox.Information,
                            "Instrument parameters", "<br>".join(lines), QMessageBox.NoButton, self)
        popup.setTextFormat(Qt.RichText)
        ok = popup.addButton(QMessageBox.Ok)
        go = popup.addButton("Go to runtime", QMessageBox.AcceptRole)
        for button in (ok, go):
            button.setMinimumWidth(90)
        text_label = popup.findChild(QLabel, "qt_msgbox_label")
        if text_label is not None:              # wide enough for a run path on one line
            screen_width = QApplication.desktop().availableGeometry(self).width()
            text_label.setMinimumWidth(min(text_label.fontMetrics().width(str(folder)) + 20,
                                           int(0.6 * screen_width)))
        popup.setDefaultButton(ok)              # Enter
        popup.setEscapeButton(ok)               # Esc
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup.buttonClicked.connect(lambda button: button is go and self.go_to_runtime.emit(folder))
        popup.finished.connect(
            lambda *_: setattr(self, "_result_popup", None) if self._result_popup is popup else None)
        popup.setModal(False)
        self._result_popup = popup
        popup.show()

    def _inst_items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def clear_load_result(self):
        """Hide the result of the last load and its colors in the list (a running load stays)."""
        if self._loading is not None:
            return
        self.load_status_label.hide()
        for item in self._inst_items():
            item.setData(0, Qt.ForegroundRole, None)
            item.setToolTip(0, "")

    def hideEvent(self, event):
        super().hideEvent(event)
        if not event.spontaneous():             # another tab chosen
            self.clear_load_result()

    def _set_load_status(self, text):
        self.load_status_label.setText(text)
        self._fit_load_status()

    def _fit_load_status(self):
        # a wrapped label in a tab page gets the height of a guessed width; give it all its lines
        label = self.load_status_label
        label.setMinimumHeight(label.heightForWidth(max(label.width(), 1)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.load_status_label.isVisible():
            QTimer.singleShot(0, self._fit_load_status)     # after the label has its new width

    def clear(self):
        self.clear_load_result()
        self.folder_path = None
        self.tree.clear()
        self.selected_instruments.clear()
        self.notice_label.hide()
        self.load_button.hide()
