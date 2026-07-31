"""
The pulse-sequence view for acadia_gui.

Reuses :class:`acadia_qmsmt.sequence_viz.SequenceView` for the drag-box zoom
behaviour (the same class also drives the notebook ipympl canvas) and only adds
the Qt chrome -- none of the zoom logic is duplicated. The rest of the
``sequence_viz`` package is pure matplotlib and imports no PyQt5.

Embedded in the data browser's centre panel by :class:`~.center_view.CenterView`,
which toggles between this and the live plot view. ``load_folder(path)`` is safe
to call with any path -- non-data folders and trace failures are reported in the
widget instead of raising.

Layout: a collapsible left column exposes every :func:`draw` option; the canvas
fills the rest. Navigation (zoom/pan/reset) is the reused ``SequenceView`` — drag
a box to zoom, scroll to zoom about the cursor, shift-drag to pan, double-click or
``r`` to reset — so there is no matplotlib toolbar.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                             QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QSpinBox, QSplitter, QToolButton,
                             QVBoxLayout, QWidget)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from acadia_qmsmt.sequence_viz import SequenceView, is_data_folder, trace_folder
from acadia_qmsmt.sequence_viz.plotting import DARK_THEME, LIGHT_THEME, fit_layout


def _combo(items):
    """A ``QComboBox`` whose options each carry a hover tooltip.

    ``items`` is ``[(option_text, tooltip), ...]``; the tooltip shows when hovering
    that option in the open dropdown (``Qt.ToolTipRole``).
    """
    box = QComboBox()
    for index, (text, tip) in enumerate(items):
        box.addItem(text)
        box.setItemData(index, tip, Qt.ToolTipRole)
    return box


class SequenceWidget(QWidget):
    """Show the compiled pulse sequence of a data folder, with drag-box zoom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trace = None
        self.view = None
        self.folder = None
        self._theme = LIGHT_THEME       # swapped by set_theme when the app goes dark

        # ---- trace controls (a change needs a re-trace: the Reload button) ----
        self.point = QSpinBox()
        self.point.setRange(0, 0)
        self.point.setToolTip(
            "Which sweep point to show (0 … N-1).\n"
            "The sequence is compiled once and every point shares that one\n"
            "schedule — only the pulse data and the register cache differ.\n"
            "A single dry run captures them all, so stepping this switches\n"
            "instantly, with no re-tracing.")
        self.resolve = QSpinBox()
        self.resolve.setRange(0, 10_000_000)
        self.resolve.setSingleStep(100)
        self.resolve.setToolTip(
            "Register cycles (resolve_indeterminate).\n"
            "Some dwell/pulse lengths are set at run time from a register or\n"
            "a DSP result, so they're unknown when the sequence is compiled\n"
            "and are drawn cross-hatched. This many sequencer cycles is\n"
            "assumed for any such length that can't be recovered from the\n"
            "per-point cache, so it renders at a concrete width.\n"
            "0 keeps them symbolic; lengths that resolve from the cache\n"
            "ignore this.")
        self.saved_qmsmt = QCheckBox("use saved qmsmt")
        self.saved_qmsmt.setChecked(True)
        self.saved_qmsmt.setToolTip(
            "Import against the acadia_qmsmt.py saved in the folder (falls back "
            "to the installed package if that fails)")
        self.reload_button = QPushButton("Reload")

        # ---- draw controls (a change only re-renders); each option is tooltip'd ----
        self.color_by = _combo([
            ("memory", "one hue per waveform memory — same hue means the same samples"),
            ("name", "one hue per pulse name, merged across channels"),
            ("channel", "one hue per lane")])

        self.envelopes = QCheckBox("envelopes")
        self.envelopes.setChecked(True)
        self.envelope_source = _combo([
            ("memory", "the samples actually loaded at this sweep point (swept "
                       "scale/detune/phase included)"),
            ("config", "the nominal pulse recomputed from the yaml")])
        self.envelope_scale = _combo([
            ("per-pulse", "fill every bar to its own peak — shape only, amplitude "
                          "not comparable"),
            ("channel", "scale to the loudest pulse on each DAC"),
            ("shared", "scale to the loudest pulse anywhere"),
            ("absolute", "scale to DAC full scale")])
        self.envelope_mode = _combo([
            ("magnitude", "draw |s| — hides detune, phase and the DRAG quadrature"),
            ("iq", "draw I and Q about the lane centre — shows detune, phase, DRAG")])

        self.label_pulses = QCheckBox("pulse labels")
        self.label_pulses.setChecked(True)
        self.pulse_label = _combo([
            ("name", "label each pulse with its name"),
            ("length", "label each pulse with its duration in ns")])
        self.group_copies = QCheckBox("group _copy pulses")
        self.group_copies.setChecked(True)
        self.show_barriers = QCheckBox("barriers")
        self.show_barriers.setChecked(True)
        self.show_blocks = QCheckBox("block starts")
        self.show_blocks.setChecked(True)
        self.show_gaps = QCheckBox("inter-block gaps")
        self.show_gaps.setChecked(True)
        self.show_branches = QCheckBox("control flow")
        self.show_branches.setChecked(True)
        self.legend = QCheckBox("legend")
        self.legend.setChecked(True)
        self.title = QCheckBox("title")
        self.title.setChecked(True)

        # ---- navigation ----
        self.jump = QComboBox()
        self.jump.setToolTip("Jump the viewport to a synchronizer block")
        self.reset_button = QPushButton("Reset zoom")

        # ---- canvas (no matplotlib toolbar; SequenceView handles zoom/pan) ----
        self.canvas = FigureCanvasQTAgg(Figure())
        self.canvas.setFocusPolicy(Qt.ClickFocus)   # so it receives key events

        # ---- status: errors / placeholder only, hidden when empty ----
        self.status = QLabel("Select a data folder to see its pulse sequence.")
        self.status.setWordWrap(True)

        self._build_layout()
        self._connect()

    # ---------------- layout ----------------

    def _build_layout(self):
        def form(*rows):
            f = QFormLayout()
            for label, widget in rows:
                f.addRow(label, widget)
            return f

        trace_box = QVBoxLayout()
        trace_box.addLayout(form(("point", self.point),
                                 ("register cycles", self.resolve)))
        trace_box.addWidget(self.saved_qmsmt)
        trace_box.addWidget(self.reload_button)
        trace_group = QGroupBox("Trace")
        trace_group.setLayout(trace_box)

        color_group = QGroupBox("Color && labels")   # && -> literal & (Qt mnemonic)
        color_group.setLayout(form(("color by", self.color_by),
                                    ("label", self.pulse_label)))

        env_box = QVBoxLayout()
        env_box.addWidget(self.envelopes)
        env_box.addLayout(form(("from", self.envelope_source),
                               ("scale", self.envelope_scale),
                               ("mode", self.envelope_mode)))
        env_group = QGroupBox("Envelope")
        env_group.setLayout(env_box)

        marks_box = QVBoxLayout()
        for widget in (self.label_pulses, self.group_copies, self.show_barriers,
                       self.show_blocks, self.show_gaps, self.show_branches,
                       self.legend, self.title):
            marks_box.addWidget(widget)
        marks_group = QGroupBox("Marks")
        marks_group.setLayout(marks_box)

        nav_box = QVBoxLayout()
        nav_box.addLayout(form(("block", self.jump)))
        nav_box.addWidget(self.reset_button)
        nav_group = QGroupBox("Navigate")
        nav_group.setLayout(nav_box)

        column = QVBoxLayout()
        for group in (trace_group, color_group, env_group, marks_group, nav_group):
            column.addWidget(group)
        column.addStretch(1)
        column_widget = QWidget()
        column_widget.setLayout(column)

        # scrollable so the column never overflows a short window; width is
        # drag-resizable via the splitter below (min stops it vanishing there)
        self.options = QScrollArea()
        self.options.setWidgetResizable(True)
        self.options.setWidget(column_widget)
        self.options.setMinimumWidth(140)

        # a thin full-height strip on the left edge collapses the options column
        # (no dedicated toggle row); the arrow points the way it will move
        self.options_toggle = QToolButton()
        self.options_toggle.setCheckable(True)
        self.options_toggle.setChecked(True)
        self.options_toggle.setArrowType(Qt.LeftArrow)
        self.options_toggle.setFixedWidth(16)
        self.options_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.options_toggle.setToolTip("Show/hide the draw options")
        self.options_toggle.toggled.connect(self._toggle_options)

        # splitter so the options / canvas divider can be dragged to resize
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.options)
        splitter.addWidget(self.canvas)
        splitter.setStretchFactor(0, 0)     # options keep their size
        splitter.setStretchFactor(1, 1)     # canvas takes the slack
        splitter.setSizes([240, 900])

        body = QHBoxLayout()
        body.addWidget(self.options_toggle)
        body.addWidget(splitter, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(body, stretch=1)
        layout.addWidget(self.status)

    def _connect(self):
        self.reload_button.clicked.connect(self.reload)
        self.reset_button.clicked.connect(self._reset)
        self.jump.currentIndexChanged.connect(self._jump_to_block)
        self.point.valueChanged.connect(self._select_point)
        for combo in (self.color_by, self.pulse_label, self.envelope_source,
                      self.envelope_scale, self.envelope_mode):
            combo.currentIndexChanged.connect(self._redraw)
        for check in (self.envelopes, self.group_copies, self.label_pulses,
                      self.show_barriers, self.show_blocks, self.show_gaps,
                      self.show_branches, self.legend, self.title):
            check.stateChanged.connect(self._redraw)

    def _toggle_options(self, shown):
        self.options.setVisible(shown)
        self.options_toggle.setArrowType(Qt.LeftArrow if shown else Qt.RightArrow)

    # ---------------- public API ----------------

    def load_folder(self, folder):
        """Trace ``folder`` and display it. Never raises."""
        self.folder = str(folder) if folder else None
        if not self.folder or not is_data_folder(self.folder):
            self.clear("Not a deployed data folder "
                       "(needs kwargs.json and runtime.py).")
            return
        self.reload()

    def reload(self):
        if not self.folder:
            return
        self._set_status(f"Tracing {self.folder} …")
        self.status.repaint()
        try:
            self.trace = trace_folder(
                self.folder,
                point=0,
                resolve_indeterminate=self.resolve.value(),
                use_saved_qmsmt=self.saved_qmsmt.isChecked())
        except Exception as exc:
            self.clear(f"Could not trace this folder: {type(exc).__name__}: {exc}")
            return
        self.point.blockSignals(True)
        self.point.setRange(0, max(self.trace.n_points - 1, 0))
        self.point.setValue(0)
        self.point.blockSignals(False)
        self._populate_blocks()
        self._redraw()
        self._set_status("")     # summary dropped; just clear the tracing message

    def clear(self, message=""):
        self.trace = None
        if self.view is not None:
            self.view.disconnect()
            self.view = None
        self.canvas.figure.clear()
        self.canvas.draw_idle()
        self.jump.clear()
        self._set_status(message)

    def set_theme(self, theme_name):
        """Follow the app's light/dark theme (called by CenterView.set_theme)."""
        dark = "dark" in (theme_name or "").lower()   # project convention
        self._theme = DARK_THEME if dark else LIGHT_THEME
        if self.trace is not None:
            self._redraw()

    # ---------------- internals ----------------

    def _set_status(self, message):
        self.status.setText(message or "")
        self.status.setVisible(bool(message))

    def _select_point(self, index):
        """Switch sweep point in place -- every point came from the one dry run."""
        if self.trace is None or self.view is None:
            return
        if not 0 <= index < self.trace.n_points:
            return
        self.view.set_point(index)
        self._populate_blocks()

    def _redraw(self):
        if self.trace is None:
            return
        keep = self.view.xlim_ns if self.view is not None else None
        if self.view is not None:
            self.view.disconnect()
        figure = self.canvas.figure
        figure.clear()
        ax = figure.add_subplot(111)
        self.view = SequenceView(
            self.trace, ax,
            group_copies=self.group_copies.isChecked(),
            label_pulses=self.label_pulses.isChecked(),
            pulse_label=self.pulse_label.currentText(),
            show_envelopes=self.envelopes.isChecked(),
            show_barriers=self.show_barriers.isChecked(),
            show_blocks=self.show_blocks.isChecked(),
            show_gaps=self.show_gaps.isChecked(),
            show_branches=self.show_branches.isChecked(),
            legend=self.legend.isChecked(),
            color_by=self.color_by.currentText(),
            envelope_source=self.envelope_source.currentText(),
            envelope_scale=self.envelope_scale.currentText(),
            envelope_mode=self.envelope_mode.currentText(),
            title=None if self.title.isChecked() else False,
            theme=self._theme)
        if keep is not None:
            self.view.set_window(*keep)
        fit_layout(figure, ax)
        self.canvas.draw_idle()

    def _populate_blocks(self):
        # Times live on placements (what executes); trace.blocks stay at 0.
        self.jump.blockSignals(True)
        self.jump.clear()
        self.jump.addItem("whole seq", None)
        ns = self.trace.ns_per_cycle
        placements = self.trace.placements or self.trace.blocks
        counts = {}
        for p in placements:
            counts[p.index] = counts.get(p.index, 0) + 1
        seen = {}
        for p in placements:
            seen[p.index] = seen.get(p.index, 0) + 1
            label = f"block {p.index}"
            if counts[p.index] > 1:                    # a looped block runs many times
                label += f" (pass {seen[p.index]})"
            label += f": {p.start * ns:.0f} ns"
            self.jump.addItem(label, (p.start, p.stop))   # store the cycle span
        self.jump.blockSignals(False)

    def _jump_to_block(self, _index):
        if self.trace is None or self.view is None:
            return
        span = self.jump.currentData()
        if span is None:
            self.view.reset()
            return
        start, stop = span
        ns = self.trace.ns_per_cycle
        pad = max((stop - start) * ns * 0.15, 20.0)
        self.view.set_window(start * ns - pad, stop * ns + pad)

    def _reset(self):
        if self.view is not None:
            self.view.reset()
