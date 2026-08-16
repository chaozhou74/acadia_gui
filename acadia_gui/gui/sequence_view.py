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

The Qt chrome adds a scrollbar per axis, along the bottom and down the right: they
say where in the sequence the current window sits (the handle is the visible
fraction) and let you walk along time or up the lane stack at a fixed zoom, which
the mouse gestures alone cannot do. Both are live only while that axis is zoomed
in, and both pan through ``SequenceView``'s throttled path -- a drag emits values
far faster than the plot can be redrawn.
"""
import functools
import inspect
import logging
import traceback

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QGridLayout,
                             QGroupBox, QHBoxLayout, QInputDialog, QLabel, QPushButton,
                             QScrollArea, QScrollBar, QSizePolicy, QSpinBox,
                             QSplitter, QToolButton, QVBoxLayout, QWidget)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from acadia_qmsmt.sequence_viz import SequenceView, is_data_folder, trace_folder
from acadia_qmsmt.sequence_viz.plotting import (DARK_THEME, LIGHT_THEME, execution_tag,
                                               fit_layout, flow_label)


logger = logging.getLogger(__name__)


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

# Sub-lane resolution of the vertical scrollbar. QScrollBar is int-only, and a
# thousandth of a lane is far finer than a pixel on any canvas, so the bar pans
# continuously -- the same reason the time bar counts in whole nanoseconds.
LANE_STEPS = 1000

REGISTERS_TIP = (
    "Values the hardware sets in real time (a register or DSP result) --\n"
    "not always a length. Cache-fed ones resolve automatically per point;\n"
    "for the rest, type in a value to preview the sequence for it.")


#: Explains what the Control flow panel is for, and -- more importantly -- what its three
#: sources MEAN, because that is the difference between a picture you can trust and one you
#: cannot. A drawn loop body is only as certain as the count behind it.
CONTROL_FLOW_TIP = (
    "How many times each loop body is drawn, and which arm of each test.\n\n"
    "resolved - read out of the run's own captured cache. Trustworthy.\n"
    "assumed  - the sequencer decides this at runtime from live data (a feedback\n"
    "           measurement, or a count this trace cannot see), so the drawing shows\n"
    "           ONE possibility. Set the value here to see the others.\n"
    "pinned   - you set it. The timeline re-times immediately; nothing is re-traced,\n"
    "           so the pulses and the compiled program are untouched.")


#: How many placements one edit may produce. Drawing costs a few milliseconds per placement --
#: every pulse, block outline and gap is an individual matplotlib artist -- so this is a redraw of
#: about a second, which is what an edit should feel like. Each spin box derives its maximum from
#: this and the measured cost of the body it repeats, and that maximum is RE-DERIVED after every
#: edit: with nested loops the cost of one pass depends on what else is pinned, so limits fixed once
#: at build time multiply (35 x 41 x 62 passes, minutes of redraw, from three ordinary edits).
#: Measured worst case with this in place: 1.6 s, from 475 s before the quadratics were fixed.
DRAW_PLACEMENT_BUDGET = 250


def _guarded(method):
    """Wrap a callback so a failure cannot reach the event loop.

    PyQt5 calls ``qFatal()`` when a Python exception escapes a slot: the process aborts and the
    whole data browser dies, not just this panel. Matplotlib's callback registry is no better --
    inside a live Qt loop it prints the traceback and continues, leaving the view half-updated with
    no visible sign that anything went wrong. For a tool whose job is telling you what the board
    played, both outcomes are disqualifying: it has to survive anything it cannot draw and SAY so.

    A fired guard is still a BUG, never a way of quietly living with breakage. Each one is recorded
    on the widget (``self.faults``) and shown in the status line through the panel's existing
    :meth:`_set_status`, and validation/gui_validation.py FAILS if that list is non-empty after
    driving the panel -- so a guard turns a crash into a test failure instead of hiding it.
    """
    # How many positional arguments the method really takes, so surplus SIGNAL arguments can be
    # dropped the way Qt drops them itself. Qt inspects a slot's arity and truncates the signal's
    # arguments to fit -- `stateChanged(int)` calls a no-argument slot with no arguments. A wrapper
    # taking *args hides that arity, so Qt passed the int through and every guarded no-argument
    # slot raised TypeError: connecting `_redraw` to a checkbox silently stopped redrawing. Trimming
    # here restores Qt's own behaviour for every guarded slot rather than changing signatures to
    # suit the decorator.
    spec = inspect.getfullargspec(method)
    limit = None if spec.varargs else max(len(spec.args) - 1, 0)

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if limit is not None and len(args) > limit:
            args = args[:limit]
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:                      # noqa: BLE001 -- boundary, by design
            self._record_fault(method.__name__, exc)
            return None
    wrapper._seqview_guarded = True                   # the invariant test in validation reads this
    return wrapper


def _register_tip(entry):
    """Per-row tooltip: settable vs auto-resolved."""
    label = entry["label"]
    if entry["settable"]:
        return (f"{label}: set in real time, not at compile time.\n"
                "Type in a value to preview the sequence for it.")
    return (f"{label}: set in real time, read from the cache --\n"
            "updates automatically per sweep point.")


class SequenceWidget(QWidget):
    """Show the compiled pulse sequence of a data folder, with drag-box zoom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # first, so a guard firing while the rest of __init__ is still running has
        # somewhere to record it instead of failing inside the handler itself
        self.faults = []
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
        self.show_branches.setToolTip(
            "Dashed boxes around each loop, repeat_until and test.\n"
            "One box per EXECUTION -- a vertical edge is where that construct was entered or left.")
        # Separate from the boxes: the boxes say what the structure IS, the tabs are the handles for
        # CHANGING it. Reading pulses on a sequence with many constructs, a strip of handles across
        # the top is clutter -- and hiding it gives that vertical space back to the lanes.
        self.show_flow_tabs = QCheckBox("control-flow tabs")
        self.show_flow_tabs.setChecked(True)
        self.show_flow_tabs.setToolTip(
            "The small labelled handles above the sequence (@8 x3).\n"
            "Hover one to highlight its block, click it to change the count or the test arm.\n"
            "Turn this off to read the pulses without them; the dashed boxes stay, and the\n"
            "Control flow panel on the left still edits everything.")
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

        # ---- horizontal time scrollbar ----
        # Drag-box zoom and shift-drag pan are mouse-only; on a long sequence there was no way
        # to walk along the time axis at a fixed zoom, and no indication of WHERE in the
        # sequence the current window sits. This scrollbar is both: its handle size is the
        # visible fraction of the sequence and its position is the window's offset.
        # Units are integer NANOSECONDS -- QScrollBar is int-only, and ns is finer than
        # anything the view resolves (MIN_SPAN_NS = 2.0), so nothing is lost.
        # It is only enabled while zoomed in; a fully zoomed-out view has nothing to scroll.
        self.time_scroll = QScrollBar(Qt.Horizontal)
        self.time_scroll.setToolTip(
            "Pan along the sequence at the current zoom.\n"
            "The handle's width is the visible fraction of the whole sequence.\n"
            "Zoom with a drag-box or the wheel; double-click or 'r' resets.")
        self.time_scroll.setEnabled(False)
        # Guards the two-way binding: the view drives the bar via on_viewport, and the bar
        # drives the view via valueChanged, so each must not echo the other back.
        self._syncing_scroll = False

        # ---- vertical lane scrollbar ----
        # The same argument as the time bar, for the other axis: a tall drag-box zoom
        # restricts the lane range, and on a nine-lane sequence there was then no way to
        # walk up and down the stack at that zoom, nor any sign of which part of it you
        # were looking at.
        # Units are LANE_STEPS-ths of a lane, so the pan is continuous like the time bar's
        # (which counts in ns) rather than jumping lane by lane -- lanes are tall, and a
        # notched pan cannot show you the two halves of a boundary.
        # Qt counts a vertical bar downwards and lane numbering runs the other way
        # (channels[0] is the top lane), so the value is how far the top of the view sits
        # below the top of the stack: 0 is at the top, which is what a scrollbar at rest
        # should mean.
        self.lane_scroll = QScrollBar(Qt.Vertical)
        self.lane_scroll.setToolTip(
            "Pan up and down the lanes at the current zoom.\n"
            "The handle's height is the fraction of the channels in view.\n"
            "Drag a tall box to zoom into a lane range; double-click or 'r' resets.")
        self.lane_scroll.setEnabled(False)
        self._syncing_lanes = False

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

        # per-construct control-flow panel: one row per loop / repeat_until / test, built by
        # _build_control_flow after each trace and hidden when the sequence is straight-line
        self._flow_form = QFormLayout()
        self.flow_group = QGroupBox("Control flow")
        self.flow_group.setLayout(self._flow_form)
        self.flow_group.setToolTip(CONTROL_FLOW_TIP)
        self.flow_group.setVisible(False)
        # Minimisable: a deeply nested sequence lists a row per construct AND per execution, which
        # is a lot of panel for someone who currently cares about the pulses. The checkbox in the
        # group's title collapses it without losing any pinned value -- overrides live on the trace,
        # not in these widgets.
        self.flow_group.setCheckable(True)
        self.flow_group.setChecked(True)
        self.flow_group.toggled.connect(self._toggle_control_flow)

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
                       self.show_blocks, self.show_gaps, self.show_branches, self.show_flow_tabs,
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
        for group in (trace_group, self.reg_group, self.flow_group, color_group, env_group,
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

        # canvas + its two scrollbars as one pane, so each bar spans the canvas exactly:
        # time along the bottom, lanes down the right (the lane labels are on the left,
        # so the right edge is the free one), and no bar extends under the options column
        canvas_pane = QGridLayout()
        canvas_pane.setContentsMargins(0, 0, 0, 0)
        canvas_pane.setSpacing(0)
        canvas_pane.addWidget(self.canvas, 0, 0)
        canvas_pane.addWidget(self.lane_scroll, 0, 1)
        canvas_pane.addWidget(self.time_scroll, 1, 0)
        canvas_pane.setRowStretch(0, 1)         # the canvas takes all the slack
        canvas_pane.setColumnStretch(0, 1)
        canvas_widget = QWidget()
        canvas_widget.setLayout(canvas_pane)

        # splitter so the options / canvas divider can be dragged to resize
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.options)
        splitter.addWidget(canvas_widget)
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
                      self.show_branches, self.show_flow_tabs, self.legend, self.title):
            check.stateChanged.connect(self._redraw)

    @_guarded
    def _toggle_options(self, shown):
        self.options.setVisible(shown)
        self.options_toggle.setArrowType(Qt.LeftArrow if shown else Qt.RightArrow)

    # ---------------- public API ----------------

    @_guarded
    def load_folder(self, folder):
        """Trace ``folder`` and display it. Never raises."""
        self.folder = str(folder) if folder else None
        if not self.folder or not is_data_folder(self.folder):
            self.clear("Not a deployed data folder "
                       "(needs kwargs.json and runtime.py).")
            return
        self.reload()

    @_guarded
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
        self.adopt_trace(self.trace)
        self._set_status("")     # summary dropped; just clear the tracing message

    @_guarded
    def adopt_trace(self, trace):
        """Make ``trace`` the one on display: sweep range, panels, block list, drawing.

        Public and separate from :meth:`reload` so that anything handing this widget a trace goes
        through the SAME bookkeeping a real load does. A test that assigned ``widget.trace`` and
        called the builders by hand skipped the sweep-point range, which stayed 0..0 -- so every
        point change it made was clamped to 0 and the check silently exercised nothing while
        reporting a pass. The product path and the test path must not be able to differ.
        """
        self.trace = trace
        self.point.blockSignals(True)
        self.point.setRange(0, max(getattr(trace, "n_points", 1) - 1, 0))
        wanted = min(self.point.value(), self.point.maximum())
        self.point.setValue(wanted)
        self.point.blockSignals(False)
        # The spin box deliberately keeps the point you were reading across a reload -- but a
        # freshly traced folder holds point 0, so SHOWING that number is not enough: the trace has
        # to be moved to it. Otherwise the panel labels point 0's data with someone else's index
        # and nothing on screen says which point you are actually looking at. (Found by
        # path_independence: pinning a loop and then moving the point left DualRail_RB at 79.7 us
        # while the header read point 279, whose real length is 321 us.)
        if wanted != trace.point - getattr(trace, "point_offset", 0):
            trace.select_point(wanted)
        self._build_registers()
        self._build_control_flow()
        self._populate_blocks()
        self._redraw()

    @_guarded
    def clear(self, message=""):
        self.trace = None
        if self.view is not None:
            self.view.disconnect()
            self.view = None
        self.canvas.figure.clear()
        self.canvas.draw_idle()
        self.jump.clear()
        self._syncing_scroll = True
        self.time_scroll.setEnabled(False)
        self._syncing_scroll = False
        self._syncing_lanes = True
        self.lane_scroll.setEnabled(False)
        self._syncing_lanes = False
        self._build_registers()   # trace is None -> hides the panel
        self._set_status(message)

    @_guarded
    def set_theme(self, theme_name):
        """Follow the app's light/dark theme (called by CenterView.set_theme)."""
        dark = "dark" in (theme_name or "").lower()   # project convention
        self._theme = DARK_THEME if dark else LIGHT_THEME
        if self.trace is not None:
            self._redraw()

    # ---------------- internals ----------------

    def _record_fault(self, where, exc):
        """Remember a guarded failure, tell the user, and keep the panel alive.

        Deliberately not guarded itself: it is the bottom of the stack, so it must not raise.
        """
        self.faults.append((where, f"{type(exc).__name__}: {exc}", traceback.format_exc()))
        try:
            logger.error("sequence view: %s failed\n%s", where, traceback.format_exc())
            self._set_status(f"Internal error in {where} — {type(exc).__name__}: {exc}. "
                             f"The view is still usable; please report this.")
        except Exception:                             # noqa: BLE001 -- last resort
            pass

    def _set_status(self, message):
        self.status.setText(message or "")
        self.status.setVisible(bool(message))

    @_guarded
    def _select_point(self, index):
        """Switch sweep point in place -- every point came from the one dry run."""
        if self.trace is None or self.view is None:
            return
        if not 0 <= index < self.trace.n_points:
            return
        self.view.set_point(index)
        self._refresh_registers()   # cache-fed values change per point
        # ...and so can the CONTROL FLOW. A register read from the cache decides a test's arm and a
        # repeat_until's count, so a different sweep point can add or remove constructs entirely:
        # Readout_Fidelity grows a construct at point 1 that point 0 does not have. Without this the
        # diagram showed a tab with no row beside it -- the panel describing a different sequence
        # from the one drawn.
        self._refresh_control_flow()
        self._populate_blocks()

    # ---------------- registers ----------------

    @_guarded
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
            name_label = QLabel(name)            # a QLabel so its text is hoverable
            name_label.setToolTip(tip)
            if entry["settable"]:
                field = self._register_input(entry, tip)
            else:
                field = QLabel(self._register_text(entry))
                field.setToolTip(tip)
                self._reg_labels[name] = field
            self._reg_form.addRow(name_label, field)
        self.reg_group.setVisible(True)

    # ---------------- control flow on the diagram ----------------

    @_guarded
    def _connect_flow_picking(self):
        """Point at a construct's TAB to highlight it; click the tab to edit it.

        The tab exists because the plot area is not a usable click target: it belongs to the
        box-zoom gesture, so clicking a span to edit it also dragged out a zoom rectangle. The
        viewport offers `claim_press` for exactly this -- a press inside a tab is taken before the
        zoom gesture sees it, so editing and zooming stop competing.
        """
        canvas = self.canvas
        cid = getattr(self, "_flow_motion_cid", None)
        if cid is not None:
            canvas.mpl_disconnect(cid)
        self._flow_motion_cid = canvas.mpl_connect(
            "motion_notify_event", self._on_flow_hover)
        self._flow_highlighted = None
        if self.view is not None:
            self.view.claim_press = self._claim_flow_tab

    @_guarded
    def _flow_tab_at(self, event):
        """``(frame, info)`` whose TAB is under the cursor, or None.

        The deepest tab wins where they overlap: an inner construct's tab is drawn on top of its
        parent's, so that is what the eye picks too.
        """
        axes = getattr(self.view, "ax", None) if self.view is not None else None
        if (self.trace is None or axes is None or event.inaxes is not axes
                or event.xdata is None or event.ydata is None):
            return None
        frames = getattr(axes, "_seqviz_flow_frames", None) or []
        if not frames:
            return None
        # The rect the renderer actually DREW, published on the info. Recomputing it here meant
        # keeping two copies of the strip-row and inset arithmetic in step, and the moment tabs
        # started being bumped to a free row the hit test would have pointed at empty space.
        best = None
        for frame, info in frames:
            rect = info.get("tab_rect")
            if not rect:
                continue
            x0, y0, width, height = rect
            if x0 <= event.xdata <= x0 + width and y0 <= event.ydata <= y0 + height:
                if best is None or info["depth"] > best[1]["depth"]:
                    best = (frame, info)
        return best

    @_guarded
    def _on_flow_hover(self, event):
        """Highlight the construct whose tab is under the cursor, and say what it is."""
        hit = self._flow_tab_at(event)
        previous = getattr(self, "_flow_highlighted", None)
        if hit is None:
            if previous is not None:
                self._restore_highlight(previous)
                self._flow_highlighted = None
                self.canvas.draw_idle()
            self.canvas.setToolTip("")
            return
        frame, info = hit
        # block AND depth: nested constructs share a first block, so matching on block alone named
        # whichever came first in the summary -- the tooltip could describe a different construct
        # from the one under the cursor
        summary = self.trace.control_flow_summary()
        entry = next((e for e in summary if e["block"] == info["block"]
                      and int(e["depth"]) == int(info.get("depth", 1))), None)
        if entry is None:
            entry = next((e for e in summary if e["block"] == info["block"]), None)
        if entry is None:
            return
        # the SAME label the tab and the panel row show, so all three agree, then the detail
        shared = {e["block"] for e in summary
                  if sum(1 for other in summary if other["block"] == e["block"]) > 1}
        heading = flow_label(entry, entry["block"] in shared, info.get("execution"))
        if entry["kind"] == "test":
            detail = (f"arm {'taken' if entry['taken'] else 'skipped'} "
                      f"({entry['source']})")
        else:
            drawn = info.get("count", entry["count"])
            detail = (f"{drawn} pass{'' if drawn == 1 else 'es'} drawn ({entry['source']})")
        if entry.get("indeterminate"):
            detail += "; the board decides this at runtime"
        if len(entry.get("executions") or ()) > 1:
            detail += (f"; execution {info.get('execution', '?')} of "
                       f"{len(entry['executions'])}")
        self.canvas.setToolTip(f"{heading} — {detail}\nclick this tab to change it")
        if previous is not None and previous[0] is frame:
            return                       # already highlighted; do not redraw on every motion
        if previous is not None:
            self._restore_highlight(previous)
        # solid and heavier, so the construct this tab governs is unmistakable
        self._flow_highlighted = (frame, frame.get_linewidth(), frame.get_linestyle(),
                                 frame.get_alpha())
        frame.set_linewidth(2.6)
        frame.set_linestyle("solid")
        frame.set_alpha(1.0)
        self.canvas.draw_idle()

    @staticmethod
    def _restore_highlight(saved):
        frame, linewidth, linestyle, alpha = saved
        try:
            frame.set_linewidth(linewidth)
            frame.set_linestyle(linestyle)
            frame.set_alpha(alpha)
        except Exception:
            pass                          # the figure was rebuilt under us; nothing to restore

    @_guarded
    def _claim_flow_tab(self, event):
        """Take a left-press inside a tab before the viewport turns it into a zoom drag."""
        if getattr(event, "button", None) != 1:
            return False
        hit = self._flow_tab_at(event)
        if hit is None:
            return False
        info = hit[1]
        self._edit_construct(info["block"], info.get("path", ()), info.get("depth", 1))
        return True                       # claimed: no zoom rectangle, no pan

    @_guarded
    def _edit_construct(self, block, path=(), depth=1):
        """Ask for this EXECUTION's iteration count, or which arm of its test to draw.

        ``path`` names the execution: a loop nested inside another is compiled once but runs once
        per outer pass, and how many rounds it takes can differ every time (an active-reset loop
        repeats until that qubit is cool). So the default is to change the one you clicked, with
        "all" offered explicitly rather than imposed.
        """
        # block AND depth: nested constructs share a first block, so matching on block alone
        # opened the dialog for whichever one happened to come first in the summary
        summary = self.trace.control_flow_summary()
        entry = next((e for e in summary
                      if e["block"] == block and int(e["depth"]) == int(depth)), None)
        if entry is None:                      # depth unknown to the caller: fall back to block
            entry = next((e for e in summary if e["block"] == block), None)
        if entry is None:
            return
        if entry["kind"] == "test":
            options = ["auto", "taken", "skipped"]
            pinned = entry["taken"] if entry["source"] == "pinned" else None
            choice, ok = QInputDialog.getItem(
                self, "Test arm", f"Which arm of test @{block}?", options,
                0 if pinned is None else (1 if pinned else 2), False)
            if ok:
                self._set_path_choice(entry["key"], options.index(choice))
            return

        # the canonical construct key: block AND nesting depth, plus the execution path when
        # editing one execution. Block alone collides between nested constructs that begin at the
        # same block, which made three tabs edit the same thing.
        key = (self.trace.construct_key(block, entry["depth"], tuple(path)) if path
               else entry["key"])
        current = self.trace.loop_counts.get(key, entry["count"] or 1)
        where = (f"this execution (outer pass {list(path)})" if path
                 else "this construct")
        # Bounded by the same budget the panel's spin boxes use. This dialog offered 0..100000 and
        # wrote loop_counts DIRECTLY, so the one control that bypassed every limit was the one on
        # the diagram -- type 100000 here and the plan became 100000 copies of the body.
        allowed = self._pass_budget(key)
        value, ok = QInputDialog.getInt(
            self, "Iterations",
            f"Passes of {entry['kind']} @{block} to draw for {where}\n"
            f"(currently {entry['count']}, {entry['source']}; up to {allowed} can be drawn):",
            int(min(current, allowed)), 0, allowed)
        if not ok:
            return
        # through _set_loop_count, not into the dict: one place applies the clamp, refreshes the
        # panel and redraws, so an edit from the diagram behaves exactly like an edit from the panel
        self._set_loop_count(key, int(value))

    # ---------------- control flow ----------------

    @_guarded
    def _build_control_flow(self):
        """(Re)build the Control flow panel: one row per loop / repeat_until / test.

        The backend has always accepted these overrides (``loop_counts`` for how many passes of a
        body to draw, ``path_choices`` for which arm of a test runs) and re-times in place. They
        were simply not reachable without editing block indices by hand, which meant the most
        important question about a drawn sequence -- "is this the run, or one possibility the
        tracer had to pick?" -- had no answer in the UI.
        """
        while self._flow_form.rowCount():
            self._flow_form.removeRow(0)
        self._flow_widgets = {}
        #: construct key -> the widgets of its per-execution child rows, so they can be folded away
        self._flow_children = {}
        #: child rows currently folded (by widget), so the group toggle does not unfold them
        self._flow_folded = set()
        #: construct keys the user has expanded, remembered across rebuilds
        self._flow_unfolded = getattr(self, "_flow_unfolded", set())
        #: every widget belonging to a row, recorded explicitly. Walking the layout is not enough:
        #: a construct row with an expander keeps its label inside a holder widget, so hiding the
        #: holder leaves the label's own state saying "shown" -- an ambiguity that makes the panel
        #: state untestable and depends on Qt's parent-visibility rules to look right.
        self._flow_rows = []
        # the row LABELS too, not just the fields: the label carries the count ("@0 x3") and it has
        # to be re-written when the count changes, or the row reads x1 while its own spin box reads
        # 3 and the tab on the diagram reads x3 -- three statements about one construct
        self._flow_labels = {}
        entries = (self.trace.control_flow_summary()
                   if self.trace is not None and hasattr(self.trace, "control_flow_summary")
                   else [])
        if not entries:
            self.flow_group.setVisible(False)
            return
        # A block can key several constructs: a cooling round and the active reset inside it
        # begin at the same block. Those rows must be told apart, or two identical-looking rows
        # edit different things -- so the depth is shown wherever it is what distinguishes them.
        shared = {e["block"] for e in entries
                  if sum(1 for other in entries if other["block"] == e["block"]) > 1}
        for entry in entries:
            # depth is one-based, so the outermost row is flush left
            indent = "  " * (int(entry["depth"]) - 1)
            # same text the tab in the diagram shows, so the two can be matched by eye
            label = flow_label(entry, ambiguous=entry["block"] in shared)
            name = QLabel(f"{indent}{label}")
            runs = entry.get("executions") or ()
            tip = (f"{entry['kind']} whose body starts at block {entry['block']}, "
                   f"nesting depth {entry['depth']}.\n"
                   f"value: {entry['source']}"
                   + (f"\nruns {len(runs)} times (once per enclosing pass); this row sets all of "
                      f"them, the rows below set one each" if len(runs) > 1 else "")
                   + f"\n\n{CONTROL_FLOW_TIP}")
            name.setToolTip(tip)
            field = (self._flow_test_input(entry, tip) if entry["kind"] == "test"
                     else self._flow_count_input(entry, tip))
            if len(runs) > 1:
                # an expander in the label cell, so the construct row keeps its own control
                holder = QWidget()
                row = QHBoxLayout(holder)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(2)
                toggle = QToolButton()
                toggle.setCheckable(True)
                # folded by default, but an expansion the user made survives a rebuild
                toggle.setChecked(entry["key"] in self._flow_unfolded)
                toggle.setArrowType(Qt.RightArrow)
                toggle.setFixedWidth(14)
                toggle.setToolTip(f"Show the {len(runs)} separate executions of this construct")
                toggle.toggled.connect(
                    lambda shown, k=entry["key"]: self._toggle_executions(k, shown))
                row.addWidget(toggle)
                row.addWidget(name)
                row.addStretch(1)
                self._flow_form.addRow(holder, field)
                self._flow_rows.append((holder, name, toggle, field))
            else:
                self._flow_form.addRow(name, field)
                self._flow_rows.append((name, field))
            self._flow_labels[entry["key"]] = (name, indent)

            if len(runs) < 2:
                continue
            children = []
            for number, run in enumerate(runs, 1):
                # tagged by its pass path, not by its position, so a sibling changing state cannot
                # renumber it (see plotting.execution_tag)
                child = self._execution_entry(entry, run, execution_tag(run["path"]))
                child_indent = indent + "    "
                child_label = QLabel(f"{child_indent}"
                                     f"{flow_label(child, entry['block'] in shared)}")
                child_tip = (f"execution {number} of {len(runs)}: enclosing pass "
                             f"{'.'.join(str(p) for p in run['path']) or 'none'}.\n"
                             f"Setting this changes only this one execution.\n\n"
                             f"{CONTROL_FLOW_TIP}")
                child_label.setToolTip(child_tip)
                child_field = (self._flow_test_input(child, child_tip) if child["kind"] == "test"
                               else self._flow_count_input(child, child_tip))
                self._flow_form.addRow(child_label, child_field)
                self._flow_labels[run["key"]] = (child_label, child_indent)
                self._flow_rows.append((child_label, child_field))
                expanded = entry["key"] in self._flow_unfolded
                for widget in (child_label, child_field):
                    widget.setVisible(expanded)
                    if expanded:
                        self._flow_folded.discard(widget)
                    else:
                        self._flow_folded.add(widget)
                children.append((child_label, child_field))
            self._flow_children[entry["key"]] = children
        self.flow_group.setVisible(True)

    @staticmethod
    def _execution_entry(entry, run, tag):
        """One execution of a construct, as a summary-shaped entry the row builders accept.

        Built here rather than in two places: the same shape is needed when the rows are created
        and when they are refreshed, and two copies of "what does this execution currently show"
        is how a panel starts disagreeing with itself.
        """
        pinned = run.get("pinned")
        return dict(entry,
                    key=run["key"],
                    execution=tag,
                    count=(pinned if pinned is not None and entry["kind"] != "test"
                           else entry["count"]),
                    taken=(pinned if pinned is not None and entry["kind"] == "test"
                           else entry["taken"]),
                    source="pinned" if pinned is not None else entry["source"])

    @_guarded
    def _toggle_executions(self, key, shown):
        """Fold or unfold one construct's per-execution rows.

        The choice is remembered on the widget, not just in the button: the panel rebuilds its rows
        when the number of executions changes (pinning an outer loop gives the constructs inside it
        more), and without this every list you had expanded would collapse at exactly the moment
        you were watching it grow.
        """
        if shown:
            self._flow_unfolded.add(key)
        else:
            self._flow_unfolded.discard(key)
        for widget in [w for pair in self._flow_children.get(key, ()) for w in pair]:
            widget.setVisible(bool(shown) and self.flow_group.isChecked())
            self._flow_folded.discard(widget) if shown else self._flow_folded.add(widget)

    @_guarded
    def _toggle_control_flow(self, shown):
        """Minimise the whole Control flow panel, remembering which children were folded."""
        for row in getattr(self, "_flow_rows", ()):
            for widget in row:
                widget.setVisible(bool(shown) and widget not in self._flow_folded)

    def _flow_count_input(self, entry, tip):
        """Spin box for how many passes of a loop body to draw."""
        spin = QSpinBox()
        # derived from what this construct's body costs to draw, not a round number: 100000 was
        # accepted and expanded the plan to 100000 copies of the body, which never finished
        body = max(int(entry.get("body") or 1), 1)
        spin.setMaximum(max(2, DRAW_PLACEMENT_BUDGET // body))
        spin.setMinimum(0)
        spin.setValue(min(int(entry["count"] or 1), spin.maximum()))
        # an assumed count is a guess, so say so where the user is about to change it
        # "(assumed)" understates a loop that cannot exit -- the count is not an assumption, it is
        # a number that does not exist. Say which it is.
        spin.setPrefix("(never exits) " if entry.get("nonterminating")
                       else "" if entry["source"] == "resolved" else f"({entry['source']}) ")
        spin.setToolTip(f"{tip}\n\nUp to {spin.maximum()} passes can be drawn here "
                        f"({body} block(s) per pass); beyond that the redraw takes longer than "
                        f"the edit is worth.")
        spin.valueChanged.connect(
            lambda value, k=entry["key"]: self._set_loop_count(k, value))
        self._flow_widgets[entry["key"]] = spin
        return spin

    def _flow_test_input(self, entry, tip):
        """Which arm of a test to draw: auto, or pinned taken/skipped."""
        box = QComboBox()
        box.addItems(["auto", "taken", "skipped"])
        # the trace resolved the pin for us with the right key precedence; asking path_choices
        # directly here is what used to show a pinned arm as "auto"
        pinned = entry["taken"] if entry["source"] == "pinned" else None
        box.setCurrentIndex(0 if pinned is None else (1 if pinned else 2))
        box.setToolTip(tip)
        box.currentIndexChanged.connect(
            lambda index, k=entry["key"]: self._set_path_choice(k, index))
        self._flow_widgets[entry["key"]] = box
        return box

    @_guarded
    def _set_loop_count(self, key, value):
        """Pin how many passes of a body are drawn; re-lay out in place (no re-trace).

        ``key`` is the canonical construct key -- ``(block, depth)`` -- not a bare block. A bare
        block is the pre-depth fallback and it matches EVERY construct whose body starts there, so
        editing the inner cooling loop through this panel also moved the outer one that begins at
        the same block. The tab in the diagram always used the canonical key; now both do, which is
        also what makes the tab and this row agree.
        """
        if self.trace is None:
            return
        # Clamp against the budget HERE, synchronously. The spin box maximum is re-derived in
        # _refresh_control_flow, which runs on a deferred timer -- so a burst of edits (a script, a
        # held arrow key, a fuzzer) lands several values before any maximum updates, and the plan
        # can balloon in between. Enforcing it at the point of application cannot be outrun.
        allowed = self._pass_budget(key)
        capped = min(int(value), allowed)
        self.trace.loop_counts[key] = capped
        if capped != int(value):
            self._set_status(f"{capped} passes is as many as can be drawn for this construct "
                             f"right now (a pass costs about "
                             f"{max(DRAW_PLACEMENT_BUDGET // max(allowed, 1), 1)} placements).")
        self._apply_control_flow()

    def _pass_budget(self, key):
        """Most passes this construct can be drawn with, given what is already pinned.

        Derived from the current plan, not from the value the control was built with: with nested
        loops the cost of one pass depends on everything outside it.
        """
        entry = next((e for e in self.trace.control_flow_summary()
                      if e["key"] == key or (isinstance(key, tuple) and len(key) == 3
                                             and e["key"] == key[:2])), None)
        body = max(int((entry or {}).get("body") or 1), 1)
        return max(2, DRAW_PLACEMENT_BUDGET // body)

    @_guarded
    def _set_path_choice(self, key, index):
        """Pin which arm of a test is drawn (0 = auto), then re-lay out in place."""
        if self.trace is None:
            return
        if index == 0:
            self.trace.path_choices.pop(key, None)
        else:
            self.trace.path_choices[key] = (index == 1)
        self._apply_control_flow()

    @_guarded
    def _apply_control_flow(self):
        """Re-lay out and redraw after a control-flow edit, then refresh this panel LATER.

        The panel must not be rebuilt from inside a spin box's own ``valueChanged`` handler:
        rebuilding removes the rows, which deletes the very widget currently emitting the
        signal, and Qt then returns into a destroyed C++ object -- the GUI dies on the first
        edit. Deferring the rebuild to the next event-loop turn lets the signal finish first.
        """
        self.trace.relayout()
        self._redraw()
        QTimer.singleShot(0, self._refresh_control_flow)

    @_guarded
    def _refresh_control_flow(self):
        """Update the existing control-flow rows IN PLACE after an edit.

        Not a rebuild. Rebuilding destroys and recreates every widget, which loses focus and
        the caret in the middle of typing a number -- and if it happens from inside the edited
        widget's own signal, Qt returns into a deleted object and the GUI dies. Rows are only
        created by :meth:`_build_control_flow`, which runs when the TRACE changes (new folder or
        sweep point), never in response to an edit.

        Signals are blocked while values are written back so a programmatic update cannot
        re-enter the handler that caused it.
        """
        if self.trace is None or not getattr(self, "_flow_widgets", None):
            return
        summary = self.trace.control_flow_summary()
        # HOW MANY EXECUTIONS there are is not fixed for a trace: pinning an outer loop to N passes
        # gives every construct inside it N executions. Rows are built once per trace and only
        # updated here, so those new executions appeared as tabs on the diagram with no row in the
        # panel -- the two views disagreeing about the same construct, which is the one thing they
        # must never do. When the set of keys changes, the rows are rebuilt.
        #
        # Safe from here specifically: this runs on the deferred timer, never inside the signal of
        # the widget being replaced, which is what made the panel crash on its first edit long ago.
        wanted = {entry["key"] for entry in summary}
        wanted |= {run["key"] for entry in summary for run in entry.get("executions") or ()}
        if wanted != set(self._flow_widgets):
            # NEVER rebuild out from under someone who is typing. Rebuilding destroys and recreates
            # every row, and Qt delivering an event to a destroyed widget is what killed this panel
            # on its first edit long ago -- so if the focus is in one of these rows, wait and try
            # again rather than pulling it away mid-keystroke.
            # Only TYPING needs protecting. A half-entered number in a spin box is lost if the
            # widget is destroyed under it, so that rebuild waits. A combo selection is atomic --
            # there is no partial state -- and deferring it would leave the panel missing rows for
            # as long as the user kept the focus there, which is the wrong trade: the panel would
            # be stale precisely while being used.
            focused = QApplication.focusWidget()
            typing = {id(field) for row in getattr(self, "_flow_rows", ()) for field in row
                      if isinstance(field, QSpinBox)}
            if focused is not None and id(focused) in typing:
                QTimer.singleShot(400, self._refresh_control_flow)
                return
            self._build_control_flow()
            return
        entries = {}
        for entry in summary:
            entries[entry["key"]] = entry
            # the execution rows are keyed by (block, depth, path) and would otherwise never
            # refresh: their labels and spin boxes would keep the values they were built with
            for run in entry.get("executions") or ():
                entries[run["key"]] = self._execution_entry(entry, run,
                                                            execution_tag(run["path"]))
        shared = {e["block"] for e in summary
                  if sum(1 for other in summary if other["block"] == e["block"]) > 1}
        for key, (label, indent) in getattr(self, "_flow_labels", {}).items():
            entry = entries.get(key)
            if entry is not None:
                label.setText(f"{indent}{flow_label(entry, entry['block'] in shared)}")
        for key, control in self._flow_widgets.items():
            entry = entries.get(key)
            if entry is None:
                continue
            blocked = control.blockSignals(True)
            try:
                if isinstance(control, QSpinBox):
                    # Re-derive the limit from the CURRENT plan, not just at build time. The cost of
                    # one more pass depends on what else is pinned: with three nested loops, raising
                    # the outer one makes every inner pass more expensive, and limits fixed at build
                    # time then multiply -- 35 x 41 x 62 passes, which is minutes of redraw from
                    # three ordinary edits. Recomputed here, each edit shrinks the others' room, so
                    # the product cannot run away. Never below what is already set: the limit exists
                    # to stop a runaway, not to overrule a value the user has.
                    body = max(int(entry.get("body") or 1), 1)
                    control.setMaximum(max(2, DRAW_PLACEMENT_BUDGET // body, control.value()))
                    control.setPrefix("(never exits) " if entry.get("nonterminating")
                                      else "" if entry["source"] == "resolved"
                                      else f"({entry['source']}) ")
                    if entry["count"] is not None and control.value() != int(entry["count"]):
                        control.setValue(int(entry["count"]))
                else:
                    pinned = entry["taken"] if entry["source"] == "pinned" else None
                    control.setCurrentIndex(0 if pinned is None else (1 if pinned else 2))
            finally:
                control.blockSignals(blocked)

    def _register_input(self, entry, tip):
        """Spin box (in cycles) for a settable register/DSP-driven length."""
        name = entry["name"]
        spin = QSpinBox()
        spin.setRange(0, 10_000_000)
        spin.setValue(int(self.trace.register_overrides.get(
            name, entry["value_cycles"] or 0)))
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

    @_guarded
    def _set_register(self, name, value):
        """Pin a register/DSP-driven length; re-lay out in place (no re-trace)."""
        if self.trace is None:
            return
        self.trace.register_overrides[name] = int(value)
        self.trace.relayout()
        self._redraw()
        self._refresh_registers()

    @_guarded
    def _refresh_registers(self):
        """Update the read-outs after a point change or an override edit."""
        if self.trace is None:
            return
        for entry in self.trace.register_summary():
            name = entry["name"]
            if name in self._reg_labels:
                self._reg_labels[name].setText(self._register_text(entry))

    @_guarded
    def _redraw(self):
        if self.trace is None:
            return
        # a draw option changes what is drawn, not where you are looking: hold on to
        # both axes' viewports, or every checkbox would throw away your zoom
        keep = self.view.xlim_ns if self.view is not None else None
        keep_lanes = self.view.ylim if self.view is not None else None
        # ...but a viewport that held the WHOLE sequence must keep holding the whole sequence.
        # Re-timing can change the length -- drawing more passes of a loop makes it longer -- and
        # restoring the old window then crops the tail while looking exactly like a complete
        # picture: raising one repeat_until count left a third of the sequence, and 10 of its 21
        # constructs, outside a view that appeared to be zoomed out. Whether it was zoomed out is
        # decided against the extent this view was built for, so a zoomed-IN window is still kept
        # verbatim (and clamped by set_window if the sequence shrank under it).
        if keep is not None:
            was_lo, was_hi = self.view.full_xlim
            if keep[0] <= was_lo + 1e-6 and keep[1] >= was_hi - 1e-6:
                keep = None
        if self.view is not None:
            self.view.disconnect()
        figure = self.canvas.figure
        figure.clear()
        ax = figure.add_subplot(111)
        self.view = SequenceView(
            self.trace, ax,
            on_viewport=self._sync_scroll,   # keep the time scrollbar in step with the view
            on_lanes=self._sync_lanes,       # and the lane scrollbar
            group_copies=self.group_copies.isChecked(),
            label_pulses=self.label_pulses.isChecked(),
            pulse_label=self.pulse_label.currentText(),
            show_envelopes=self.envelopes.isChecked(),
            show_barriers=self.show_barriers.isChecked(),
            show_blocks=self.show_blocks.isChecked(),
            show_gaps=self.show_gaps.isChecked(),
            show_branches=self.show_branches.isChecked(),
            show_flow_tabs=self.show_flow_tabs.isChecked(),
            legend=self.legend.isChecked(),
            color_by=self.color_by.currentText(),
            envelope_source=self.envelope_source.currentText(),
            envelope_scale=self.envelope_scale.currentText(),
            envelope_mode=self.envelope_mode.currentText(),
            title=None if self.title.isChecked() else False,
            theme=self._theme)
        if keep is not None:
            self.view.set_window(*keep, ylim=keep_lanes)
        elif keep_lanes is not None:
            # the time window is deliberately dropped above (it covered everything, and everything
            # has changed size), but a length change says nothing about the lane stack -- so where
            # the user was scrolled to vertically is still restored
            self.view.set_window(*self.view.full_xlim, ylim=keep_lanes)
        fit_layout(figure, ax)
        self._connect_flow_picking()
        self.canvas.draw_idle()

    # ---------------- horizontal time scrollbar ----------------

    @_guarded
    def _sync_scroll(self, xlim_ns):
        """Mirror the view's window onto the scrollbar. Passed to SequenceView as on_viewport."""
        if self.view is None:
            return
        if self.time_scroll.isSliderDown():
            # The handle is being dragged, and this render is the *result* of that
            # drag. Writing the value back mid-drag makes the handle stutter against
            # the cursor (the view's window is rounded to whole ns, and renders lag
            # the drag), and nothing else here changes while panning at fixed zoom.
            return
        full_lo, full_hi = self.view.full_xlim
        lo, hi = xlim_ns
        span = max(hi - lo, 1e-9)
        full = max(full_hi - full_lo, 1e-9)
        zoomed_out = span >= full - 1e-6
        self._syncing_scroll = True
        try:
            # value = window start, pageStep = window width, maximum = last valid start.
            # All in integer ns; a 1 ns granularity is far finer than MIN_SPAN_NS = 2.0.
            self.time_scroll.setMinimum(int(full_lo))
            self.time_scroll.setMaximum(max(int(full_hi - span), int(full_lo)))
            self.time_scroll.setPageStep(max(int(span), 1))
            self.time_scroll.setSingleStep(max(int(span / 10), 1))   # arrow = 10% of a window
            self.time_scroll.setValue(int(lo))
            self.time_scroll.setEnabled(not zoomed_out)
        finally:
            self._syncing_scroll = False

    @_guarded
    def _scroll_to(self, value):
        """Scrollbar dragged: pan the view, keeping the current zoom width.

        Throttled: dragging the handle emits a value per pixel of travel, and a
        render costs far more than that -- rendering each one queued frames faster
        than they could be drawn, so the plot crawled along behind the handle.
        ``SequenceView`` coalesces them and redraws in full once the drag settles.
        """
        if self.view is None or self._syncing_scroll:
            return
        lo, hi = self.view.xlim_ns
        span = hi - lo
        self.view.set_window(float(value), float(value) + span, throttle=True)

    # ---------------- vertical lane scrollbar ----------------

    @_guarded
    def _sync_lanes(self, ylim):
        """Mirror the view's lane range onto the vertical bar. Passed as on_lanes."""
        if self.view is None or self.trace is None:
            return
        if self.lane_scroll.isSliderDown():
            return                      # this render is the drag's own doing; see _sync_scroll
        full_lo, full_hi = self.view.full_ylim
        low, high = ylim
        span = max(high - low, 1e-9)
        full = max(full_hi - full_lo, 1e-9)
        zoomed_out = span >= full - 1e-6
        self._syncing_lanes = True
        try:
            # value = how far the top of the view sits below the top of the stack,
            # pageStep = its height, maximum = the lowest valid top. All in
            # thousandths of a lane, counted downwards (see LANE_STEPS).
            self.lane_scroll.setMinimum(0)
            # rounded, not truncated: truncating leaves the bottom of the stack a
            # sub-unit out of reach, so the last lane never quite sits on the axis
            self.lane_scroll.setMaximum(max(int(round((full - span) * LANE_STEPS)), 0))
            self.lane_scroll.setPageStep(max(int(span * LANE_STEPS), 1))
            self.lane_scroll.setSingleStep(max(int(span * LANE_STEPS / 10), 1))
            self.lane_scroll.setValue(int(round((full_hi - high) * LANE_STEPS)))
            self.lane_scroll.setEnabled(not zoomed_out)
        finally:
            self._syncing_lanes = False

    @_guarded
    def _scroll_lanes_to(self, value):
        """Lane bar moved: pan up or down the stack, keeping the lanes in view."""
        if self.view is None or self.trace is None or self._syncing_lanes:
            return
        low, high = self.view.ylim or self.view.full_ylim
        top = self.view.full_ylim[1] - value / LANE_STEPS
        self.view.set_lanes(top - (high - low), top, throttle=True)

    @_guarded
    def _populate_blocks(self):
        # Times live on placements (what executes); trace.blocks stay at 0.
        if self.trace is None:
            # clear() and load_folder() can leave no trace behind, and this is called from both;
            # every other method here checks, this one did not
            self.jump.blockSignals(True)
            self.jump.clear()
            self.jump.blockSignals(False)
            return
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

    @_guarded
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

    @_guarded
    def _reset(self):
        if self.view is not None:
            self.view.reset()
