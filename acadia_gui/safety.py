"""App-wide safety nets for the data browser.

* `install_excepthook`: under PyQt5 >= 5.5 an exception escaping any Qt slot/timer callback calls
  qFatal() and aborts the whole app unless `sys.excepthook` has been replaced. A single bug in one
  button handler must not take the browser down, so unhandled exceptions are logged instead.
* `use_local_pycache`: the saved `runtime.py` of every data folder is imported when the folder is
  viewed. By default Python writes its bytecode next to it -- i.e. a `__pycache__` folder into the
  shared data folders on the file server. Keep bytecode in a local cache instead.
* `UiStallWatchdog`: if the GUI thread is blocked for more than a couple of seconds, log the
  Python stack it is stuck in, so any future freeze names its own cause in the GUI log.
"""
import os
import sys
import time
import logging
import threading
import traceback
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer

logger = logging.getLogger("acadia_gui")


def install_excepthook():
    """Log unhandled exceptions instead of letting PyQt abort the application.

    A custom hook already installed (e.g. IPython's) is left alone -- PyQt only aborts when
    `sys.excepthook` is still the interpreter default.
    """
    if sys.excepthook is not sys.__excepthook__:
        return

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.error("Unhandled error (the browser keeps running):", exc_info=(exc_type, exc, tb))
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = _hook


LOG_FILE = Path.home() / ".cache" / "acadia_gui" / "acadia_gui.log"


def install_file_log(path=LOG_FILE):
    """Also write INFO+ messages (incl. freeze stacks from the watchdog) to a rotating log file,
    so a freeze or error can be diagnosed after the fact, without the GUI log window open."""
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger()
    if any(getattr(h, "_acadia_file_log", False) for h in root.handlers):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2)
    except OSError as e:
        logger.warning(f"Could not open log file {path}: {e}")
        return
    handler._acadia_file_log = True
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    logger.info(f"--- acadia_gui started (pid {os.getpid()}) ---")


def use_local_pycache():
    """Write bytecode of imported files (incl. data folders' runtime.py) to a local cache dir."""
    if sys.pycache_prefix is not None:      # respect PYTHONPYCACHEPREFIX / -X pycache_prefix
        return
    cache = Path.home() / ".cache" / "acadia_gui" / "pycache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        sys.pycache_prefix = str(cache)
    except OSError as e:
        logger.warning(f"Could not create local bytecode cache {cache}: {e}")
        sys.dont_write_bytecode = True      # still never write into the data folders


# Third-party packages the saved runtimes import when a data folder is first opened (fitting,
# uncertainties, pandas, ...). Importing them costs ~0.5 s, which used to land on the user's first
# click. They are imported in the background right after startup instead.
WARMUP_MODULES = ("numpy", "scipy.optimize", "lmfit", "uncertainties", "pandas", "acadia_qmsmt",
                  "acadia_qmsmt.analysis.fitting.fitter_base")


def warm_up_imports_in_background(modules=WARMUP_MODULES):
    def _work():
        import importlib
        for name in modules:
            try:
                importlib.import_module(name)
            except Exception as e:     # an optional package missing is fine
                logger.debug(f"warm-up import of {name} skipped: {e}")
    threading.Thread(target=_work, name="acadia-import-warmup", daemon=True).start()


def install_fast_popup_close():
    """WSLg + X11 only: make closed dropdown lists and menus disappear immediately.

    Under WSLg, an unmapped X11 popup (combo-box list, menu) stays on screen for ~1.2 s
    (measured). Reopening a dropdown within that time put the new list UNDER the dead one, so a
    click on an item could land on the dead list and do nothing. Moving the popup off-screen
    right before it is hidden makes its leftover image vanish at once.
    """
    if os.environ.get("QT_QPA_PLATFORM") != "xcb":
        return
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QComboBox, QMenu, QApplication

    if getattr(QComboBox, "_acadia_fast_close", False):
        return
    QComboBox._acadia_fast_close = True
    far = QPoint(-30000, -30000)

    if not getattr(QComboBox, "_acadia_inline_popups", False):   # dropdowns are in-window overlays
        original_hide_popup = QComboBox.hidePopup
        def hidePopup(self):
            try:
                container = self.view().window()
                if container is not None and container.isVisible():
                    container.move(far)
                    QApplication.instance().flush()
            except RuntimeError:
                pass
            original_hide_popup(self)
        QComboBox.hidePopup = hidePopup

    class _MenuHook(QObject):
        def eventFilter(self, obj, event):
            from PyQt5.QtCore import QEvent
            if isinstance(obj, QMenu) and event.type() == QEvent.Show and not obj.property("_acadia_fast_close"):
                obj.setProperty("_acadia_fast_close", True)
                obj.aboutToHide.connect(lambda m=obj: (m.move(far), QApplication.instance().flush()))
            return False
    hook = _MenuHook(QApplication.instance())
    QApplication.instance().installEventFilter(hook)


class DialogCenterer(QObject):
    """Centers every dialog / message box on the main window when it is shown.

    On WSLg (X11 backend, several monitors) Qt's own placement put dialogs on a DIFFERENT monitor
    than the main window (measured: snapshot settings on the right screen, browser on the left).
    A modal dialog nobody can see makes the whole browser look frozen.
    """

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        from PyQt5.QtWidgets import QDialog
        if (event.type() == QEvent.Show and isinstance(obj, QDialog) and obj.isWindow()
                and obj is not self.main_window):
            self._center(obj)
            # The X11 window manager of WSLg places a newly mapped window itself (on the primary
            # monitor); re-apply our position once it has been mapped.
            from PyQt5.QtCore import QTimer
            for delay in (50, 200):
                QTimer.singleShot(delay, lambda o=obj: self._center(o))
        return False

    def _center(self, dialog):
        try:
            if not dialog.isVisible():
                return
            frame = self.main_window.frameGeometry()
            if frame.width() > 0:
                target = frame.center() - dialog.rect().center()
                if (dialog.pos() - target).manhattanLength() > 2:
                    dialog.move(target)
        except RuntimeError:      # dialog already deleted
            pass


class UiStallWatchdog(QObject):
    """Logs the GUI thread's Python stack whenever it stops processing events for > threshold."""

    def __init__(self, parent=None, threshold_s=1.0, beat_ms=200):
        super().__init__(parent)
        self.threshold_s = threshold_s
        self._last_beat = time.monotonic()
        self._gui_thread_id = threading.get_ident()
        self._stop = threading.Event()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._beat)
        self._timer.start(beat_ms)
        self._thread = threading.Thread(target=self._watch, name="acadia-ui-watchdog", daemon=True)
        self._thread.start()

    def _beat(self):
        self._last_beat = time.monotonic()

    def _watch(self):
        reported_for = None
        while not self._stop.wait(0.5):
            beat = self._last_beat
            blocked = time.monotonic() - beat
            if blocked > self.threshold_s and reported_for != beat:
                reported_for = beat         # one report per stall
                frame = sys._current_frames().get(self._gui_thread_id)
                stack = "".join(traceback.format_stack(frame)[-12:]) if frame else "(no frame)"
                logger.warning(f"GUI blocked for {blocked:.1f} s so far, in:\n{stack}")

    def stop(self):
        self._stop.set()
        self._timer.stop()
