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
``r`` to reset — so there is no matplotlib toolbar. Two scrollbars, enabled while zoomed in,
walk the time axis and the lane stack at a fixed zoom and show where the window sits.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox,
                             QHBoxLayout, QLabel, QPushButton, QScrollArea, QScrollBar,
                             QSizePolicy, QSpinBox, QSplitter, QToolButton,
                             QVBoxLayout, QWidget)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from acadia_qmsmt.sequence_viz import SequenceView, is_data_folder, trace_folder
from acadia_qmsmt.sequence_viz.plotting import DARK_THEME, LIGHT_THEME, fit_layout


# The lane bar counts in thousandths of a lane: QScrollBar is int-only, and this pans
# continuously (the time bar counts in whole ns for the same reason).
LANE_STEPS = 1000


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


POINT_TIP = (
    "Which sweep point to show.\n"
    "The runtime's sweep, flattened to 1D in its defined order (0 … N-1).")

REGISTERS_TIP = (
    "Values the sequence sets while it runs, rather than when it is built.\n"
    "Ones read from the cache fill themselves in per sweep point.\n"
    "The rest are only known on the hardware -- type a value to see what\n"
    "the sequence looks like for it, or leave it on 'auto'.")


def _register_tip(entry):
    """Per-row tooltip, by what feeds the register and so what setting it does.

    Leads with the raw ``REGn``/``DSPn`` whenever the row is shown under the runtime's
    own variable name instead -- that id is what ``compiled.log`` and the assembled
    program use, so it has to stay discoverable for anyone debugging against those.
    """
    label = entry["label"]
    if entry["alias"] != entry["name"]:
        label = f"{label}  [{entry['name']}]"
    if entry["resolution"] == "cache":
        return (f"{label}\n\n"
                "Read from the cache, so this already shows the real value.\n"
                "It updates by itself when you change the sweep point.")
    if entry["resolution"] == "device":
        return (f"{label}\n\n"
                "This value comes from a measurement, so it is not known here.\n"
                "On 'auto', the plot picks one branch to show and says so.\n"
                "Type a value to choose the branch yourself.\n"
                "Set it back to 'auto' to clear what you typed.")
    return (f"{label}\n\n"
            "This length is set while the sequence runs, not when it is built.\n"
            "On 'auto', the plot uses whatever it can work out.\n"
            "Type a length in cycles to see the sequence with that value.\n"
            "Set it back to 'auto' to clear what you typed.")


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
        self.point.setToolTip(POINT_TIP)
        # the Registers panel is rebuilt from the trace on every reload; these hold
        # its live widgets by register name (spin boxes for settable lengths, labels
        # for the auto-resolved read-outs)
        self._reg_spins = {}
        self._reg_labels = {}
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
        self.envelope_scale.setCurrentText("channel")   # default to a comparable scale
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
        self.legend.setChecked(False)
        self.title = QCheckBox("title")
        self.title.setChecked(True)

        # ---- navigation ----
        self.jump = QComboBox()
        self.jump.setToolTip("Jump the viewport to a synchronizer block")
        self.reset_button = QPushButton("Reset zoom")

        # ---- canvas (no matplotlib toolbar; SequenceView handles zoom/pan) ----
        self.canvas = FigureCanvasQTAgg(Figure())
        self.canvas.setFocusPolicy(Qt.ClickFocus)   # so it receives key events

        # ---- scrollbars: pan at a fixed zoom; the handle is the visible fraction ----
        self.time_scroll = QScrollBar(Qt.Horizontal)       # in ns
        self.time_scroll.setToolTip("Pan along the sequence at the current zoom.\n"
                                    "Zoom with a drag-box or the wheel; double-click or 'r' resets.")
        self.lane_scroll = QScrollBar(Qt.Vertical)         # in LANE_STEPS-ths of a lane, downwards
        self.lane_scroll.setToolTip("Pan up and down the lanes at the current zoom.\n"
                                    "Drag a tall box to zoom into a lane range.")
        for bar in (self.time_scroll, self.lane_scroll):
            bar.setEnabled(False)
        # the view drives a bar (on_viewport / on_lanes) and the bar drives the view
        # (valueChanged): these stop each echoing the other back
        self._syncing_scroll = self._syncing_lanes = False

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
        point_label = QLabel("point")            # a QLabel so its text is hoverable
        point_label.setToolTip(POINT_TIP)
        trace_box.addLayout(form((point_label, self.point)))
        trace_box.addWidget(self.saved_qmsmt)
        trace_box.addWidget(self.reload_button)
        trace_group = QGroupBox("Trace")
        trace_group.setLayout(trace_box)

        # per-register panel, populated by _build_registers after each trace and
        # hidden when the runtime has no registers/register-driven lengths
        self._reg_form = QFormLayout()
        self.reg_group = QGroupBox("Registers")
        self.reg_group.setLayout(self._reg_form)
        self.reg_group.setToolTip(REGISTERS_TIP)
        self.reg_group.setVisible(False)

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
        for group in (trace_group, self.reg_group, color_group, env_group,
                      marks_group, nav_group):
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
        plot = QWidget()
        grid = QGridLayout(plot)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        grid.addWidget(self.canvas, 0, 0)
        grid.addWidget(self.lane_scroll, 0, 1)
        grid.addWidget(self.time_scroll, 1, 0)
        splitter.addWidget(self.options)
        splitter.addWidget(plot)
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
        self.time_scroll.valueChanged.connect(self._scroll_to)
        self.lane_scroll.valueChanged.connect(self._scroll_lanes_to)
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
                resolve_indeterminate=0,   # per-register overrides replace the blanket
                use_saved_qmsmt=self.saved_qmsmt.isChecked())
        except Exception as exc:
            self.clear(f"Could not trace this folder: {type(exc).__name__}: {exc}")
            return
        self.point.blockSignals(True)
        self.point.setRange(0, max(self.trace.n_points - 1, 0))
        self.point.setValue(0)
        self.point.blockSignals(False)
        self._build_registers()
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
        for bar in (self.time_scroll, self.lane_scroll):
            bar.setEnabled(False)
        self.jump.clear()
        self._build_registers()   # trace is None -> hides the panel
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
        self._refresh_registers()   # cache-fed values change per point
        self._populate_blocks()

    # ---------------- registers ----------------

    def _build_registers(self):
        """(Re)build the Registers panel from the current trace."""
        while self._reg_form.rowCount():
            self._reg_form.removeRow(0)
        self._reg_spins, self._reg_labels = {}, {}
        entries = self.trace.register_summary() if self.trace is not None else []
        if not entries:
            self.reg_group.setVisible(False)
            return
        for entry in entries:
            name = entry["name"]
            tip = _register_tip(entry)
            # the row header shows the runtime's own variable name for this register
            # (e.g. "rounds_reg"), not the internal "REG0" id -- see entry["alias"]
            # and sequence_viz.tracing._variable_name_spy
            name_label = QLabel(entry["alias"])  # a QLabel so its text is hoverable
            name_label.setToolTip(tip)
            if entry["settable"]:
                field = self._register_input(entry, tip)
            else:
                field = QLabel(self._register_text(entry))
                field.setToolTip(tip)
                self._reg_labels[name] = field
            self._reg_form.addRow(name_label, field)
        self.reg_group.setVisible(True)

    def _register_input(self, entry, tip):
        """Spin box for a settable register -- a register/DSP-driven length (in
        cycles) or a device-fed register gating a ``test`` (the raw register word).

        The minimum reads "auto" rather than 0, and means *no override*: the value is
        left to resolve itself (a length falls back to the cache, a branch to the
        mutually-exclusive/assumed default). Without that, a box showing 0 could not be
        told apart from a deliberate pin to 0 -- which is exactly the interesting value
        for a feedforward branch, where 0 and "not set" pick different arms.
        """
        name = entry["name"]
        spin = QSpinBox()
        spin.setRange(-1, 10_000_000)
        spin.setSpecialValueText("auto")          # shown at the minimum, -1
        spin.setValue(int(self.trace.register_overrides.get(name, -1)))
        spin.setToolTip(tip)
        spin.valueChanged.connect(lambda value, n=name: self._set_register(n, value))
        self._reg_spins[name] = spin
        return spin

    @staticmethod
    def _register_text(entry):
        """Read-only value of an auto-resolved register (the length itself is
        already drawn on the sequence, so this shows only the raw value)."""
        cycles = entry["value_cycles"]
        return (f"{entry['source']} → {cycles}" if cycles is not None
                else f"{entry['source']} → set per run")

    def _set_register(self, name, value):
        """Pin a register, or clear the pin at "auto" (-1); re-lay out in place.

        A device-fed register gates a ``test``, so changing one can add or remove whole
        blocks from the timeline -- hence the full relayout, same as for a length.
        """
        if self.trace is None:
            return
        if int(value) < 0:
            self.trace.register_overrides.pop(name, None)
        else:
            self.trace.register_overrides[name] = int(value)
        self.trace.relayout()
        self._redraw()
        self._refresh_registers()

    def _refresh_registers(self):
        """Update the read-outs after a point change or an override edit."""
        if self.trace is None:
            return
        for entry in self.trace.register_summary():
            name = entry["name"]
            if name in self._reg_labels:
                self._reg_labels[name].setText(self._register_text(entry))

    def _redraw(self):
        if self.trace is None:
            return
        # a draw option changes what is drawn, not where you look: keep both axes' windows
        # (a time window covering the whole sequence is not kept, so it follows a new length)
        keep = keep_lanes = None
        if self.view is not None:
            keep_lanes = self.view.ylim
            if self.view.xlim_ns != self.view.full_xlim:
                keep = self.view.xlim_ns
            self.view.disconnect()
        figure = self.canvas.figure
        figure.clear()
        ax = figure.add_subplot(111)
        self.view = SequenceView(
            self.trace, ax,
            on_viewport=self._sync_scroll, on_lanes=self._sync_lanes,
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
        if keep is not None or keep_lanes is not None:
            self.view.set_window(*(keep or self.view.full_xlim), ylim=keep_lanes)
        fit_layout(figure, ax)
        self.canvas.draw_idle()

    # ---------------- scrollbars ----------------

    def _sync_scroll(self, xlim_ns):
        """Mirror the view's time window onto its bar (SequenceView's on_viewport)."""
        if self.view is None or self.time_scroll.isSliderDown():
            return              # mid-drag the render is the drag's own result: no echo
        full_lo, full_hi = self.view.full_xlim
        lo, hi = xlim_ns
        span = max(hi - lo, 1e-9)
        self._syncing_scroll = True
        try:
            self.time_scroll.setRange(int(full_lo), max(int(full_hi - span), int(full_lo)))
            self.time_scroll.setPageStep(max(int(span), 1))
            self.time_scroll.setSingleStep(max(int(span / 10), 1))
            self.time_scroll.setValue(int(lo))
            self.time_scroll.setEnabled(span < full_hi - full_lo - 1e-6)
        finally:
            self._syncing_scroll = False

    def _scroll_to(self, value):
        """Time bar moved: pan at the current zoom (throttled, a drag emits per pixel)."""
        if self.view is None or self._syncing_scroll:
            return
        lo, hi = self.view.xlim_ns
        self.view.set_window(float(value), float(value) + (hi - lo), throttle=True)

    def _sync_lanes(self, ylim):
        """Mirror the view's lane range onto its bar (SequenceView's on_lanes). Qt counts a
        vertical bar downwards, so the value is how far the view's top sits below the stack's."""
        if self.view is None or self.lane_scroll.isSliderDown():
            return
        full_lo, full_hi = self.view.full_ylim
        low, high = ylim
        span = max(high - low, 1e-9)
        self._syncing_lanes = True
        try:
            self.lane_scroll.setRange(0, max(int(round((full_hi - full_lo - span) * LANE_STEPS)), 0))
            self.lane_scroll.setPageStep(max(int(span * LANE_STEPS), 1))
            self.lane_scroll.setSingleStep(max(int(span * LANE_STEPS / 10), 1))
            self.lane_scroll.setValue(int(round((full_hi - high) * LANE_STEPS)))
            self.lane_scroll.setEnabled(span < full_hi - full_lo - 1e-6)
        finally:
            self._syncing_lanes = False

    def _scroll_lanes_to(self, value):
        """Lane bar moved: pan up or down the stack at the current lane zoom."""
        if self.view is None or self._syncing_lanes:
            return
        low, high = self.view.ylim or self.view.full_ylim
        top = self.view.full_ylim[1] - value / LANE_STEPS
        self.view.set_lanes(top - (high - low), top, throttle=True)

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
