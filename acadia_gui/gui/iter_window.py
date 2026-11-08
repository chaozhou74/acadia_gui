"""GUI-side iteration windowing for runtimes that set ``iter_windowable = True``.

Such a runtime's data processor takes no ``iter_window`` argument. Instead, while the GUI calls
it, ``acadia_qmsmt.analysis.reshape_iq_data_by_axes`` returns only the iterations inside the
window (axis 0 is always iterations) and records the full count, which the GUI uses as the
total. The runtime opts in because this is only correct if ALL of its per-iteration data goes
through that function -- the GUI cannot check that.

Processing never runs on the board, so nothing here reaches it.
"""
import contextlib

import acadia_qmsmt.analysis as _analysis
import acadia_qmsmt.analysis.preprocess as _preprocess

ITER_WINDOWABLE_ATTR = "iter_windowable"  # runtime class attribute that opts in


class WindowState:
    def __init__(self):
        self.called = False  # whether the processor used reshape_iq_data_by_axes at all
        self.total = None    # full iteration count (min over the reshaped groups)


@contextlib.contextmanager
def windowed_reshape(iter_window):
    """While active, reshape_iq_data_by_axes keeps only iterations ``i0..i1`` (0-based, inclusive).

    Yields a WindowState; both module attributes are always restored on exit.
    """
    i0, i1 = int(iter_window[0]), int(iter_window[1])
    original = _preprocess.reshape_iq_data_by_axes
    state = WindowState()

    def reshape_iq_data_by_axes(*args, **kwargs):
        state.called = True
        data = original(*args, **kwargs)
        if data is None:
            return None
        n = len(data)
        state.total = n if state.total is None else min(state.total, n)
        return data[max(i0, 0):min(i1, n - 1) + 1]

    _analysis.reshape_iq_data_by_axes = _preprocess.reshape_iq_data_by_axes = reshape_iq_data_by_axes
    try:
        yield state
    finally:
        _analysis.reshape_iq_data_by_axes = _preprocess.reshape_iq_data_by_axes = original
