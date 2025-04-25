import os
import json
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QCheckBox, QPushButton, QLabel
)


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

        # Load button
        self.load_button = QPushButton("Load Parameters")
        self.load_button.clicked.connect(self.load_selected_parameters)
        self.load_button.setEnabled(self.client_station is not None)
        self.layout.addWidget(self.load_button)

        self.setLayout(self.layout)

    def load_json(self, folder_path):
        self.tree.clear()
        self.selected_instruments.clear()
        self.select_all_checkbox.setChecked(False)
        self.notice_label.hide()
        self.tree.show()
        self.select_all_checkbox.show()
        self.load_button.show()

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
        if not self.inst_data:
            return
        dd = {k: v for k, v in self.inst_data.items() if k in self.selected_instruments}
        print("Loading parameters to instruments:", dd)
        self.client_station.set_parameters(dd)

    def clear(self):
        self.tree.clear()
        self.selected_instruments.clear()
        self.notice_label.hide()
        self.load_button.hide()
