import os
import json
import time
import logging
import threading
from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QCheckBox, QPushButton, QLabel, QApplication
)

logger = logging.getLogger(__name__)


class _LoadDone(QObject):
    finished = pyqtSignal(list, object)    # (instrument names, error string or None) -> GUI thread

def load_inst_params(path):
    inst_file = os.path.join(path, "inst_params.json")
    if not os.path.isfile(inst_file):
        return None
    try:
        with open(inst_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


class InstrumentParamsViewer(QWidget):
    def __init__(self, client_station=None):
        super().__init__()

        self.client_station = client_station
        self.selected_instruments = set()
        self.inst_data = None

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

        # background parameter loading (see load_selected_parameters)
        self._load_thread = None
        self._load_started = 0.0
        self._load_done = _LoadDone()
        self._load_done.finished.connect(self._on_load_finished)
        self._load_label_timer = QTimer(self)
        self._load_label_timer.timeout.connect(self._tick_load_label)

        # Load button
        self.load_button = QPushButton("Load Parameters")
        self.load_button.clicked.connect(self.load_selected_parameters)
        self.load_button.setEnabled(self.client_station is not None)
        self.layout.addWidget(self.load_button)
        if self.client_station is None:
            self.load_button.setToolTip("No Instrument client station found")

        self.setLayout(self.layout)

    def load_json(self, folder_path):
        self.tree.clear()
        self.selected_instruments.clear()
        self.select_all_checkbox.setChecked(False)
        self.notice_label.hide()
        self.tree.show()
        self.select_all_checkbox.show()
        self.load_button.show()
        self.load_button.setEnabled(self.client_station is not None and self._load_thread is None)

        self.inst_data = load_inst_params(folder_path)
        if not self.inst_data or not isinstance(self.inst_data, dict) or set(self.inst_data) == {"error"}:
            message = "inst_params.json not found"
            if isinstance(self.inst_data, dict) and "error" in self.inst_data:
                message = f"Could not read inst_params.json:\n{self.inst_data['error']}"
            self.inst_data = None
            self.tree.hide()
            self.select_all_checkbox.hide()
            self.load_button.setEnabled(False)
            self.notice_label.setText(message)
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
        """Send the selected instruments' saved parameters to the instrument server.

        Runs in the background: the server only replies once EVERY parameter is set, and some
        sets are slow by design (e.g. the QDAC ramps at 0.1 V/s: 60 s measured for two channels
        moving 3 V each), which used to freeze the whole browser for the duration.
        The worker uses its OWN server connection (ZMQ sockets must not be shared between
        threads), with the station's host/port/timeout, and sends exactly what
        ClientStation.set_parameters sends.
        """
        if not self.inst_data or self.client_station is None or self._load_thread is not None:
            return
        dd = {k: v for k, v in self.inst_data.items() if k in self.selected_instruments}
        station_instruments = getattr(self.client_station, "instruments", None)
        if station_instruments is not None:
            skipped = sorted(k for k in dd if k not in station_instruments)
            for k in skipped:
                logger.warning(f"Instrument {k} parameter neglected, as it doesn't belong to this station")
            dd = {k: v for k, v in dd.items() if k in station_instruments}
        if not dd:
            logger.warning("No instruments selected; nothing loaded.")
            return
        names = sorted(dd)
        logger.info(f"!!! Loading parameters to instruments: {names}")

        self._load_started = time.monotonic()
        self.load_button.setEnabled(False)
        self._tick_load_label()
        self._load_label_timer.start(1000)

        station = self.client_station
        emitter = self._load_done

        def work():
            error = None
            try:
                if hasattr(station, "_host") and hasattr(station, "_port"):
                    from instrumentserver.client.proxy import Client
                    from instrumentserver.helpers import flatten_dict
                    client = Client(host=station._host, port=station._port,
                                    timeout=getattr(station, "_timeout", 900), raise_exceptions=True)
                    try:
                        client.setParameters(flatten_dict(dd))
                    finally:
                        client.disconnect()
                else:       # an unfamiliar station object: fall back to its own method
                    station.set_parameters(dd)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            emitter.finished.emit(names, error)

        self._load_thread = threading.Thread(target=work, name="acadia-load-instrument-params", daemon=True)
        self._load_thread.start()

    def _tick_load_label(self):
        elapsed = int(time.monotonic() - self._load_started)
        self.load_button.setText(f"Loading parameters... {elapsed} s (instruments may be ramping)")

    def _on_load_finished(self, names, error):
        self._load_label_timer.stop()
        self._load_thread = None
        self.load_button.setText("Load Parameters")
        self.load_button.setEnabled(self.client_station is not None)
        took = time.monotonic() - self._load_started
        if error is None:
            logger.info(f"Loaded parameters to instruments: {names} ({took:.1f} s)")
        else:
            logger.error(f"Failed to load parameters to instruments {names} after {took:.0f} s: {error}")

    def clear(self):
        self.tree.clear()
        self.selected_instruments.clear()
        self.notice_label.hide()
        self.load_button.hide()
        self.select_all_checkbox.hide()
