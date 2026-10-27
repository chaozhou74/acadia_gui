"""Smoke test of the Acadia Data Browser: every button and feature once, plus crash, exit and
stall checks.

Everything happens in a temporary directory with a temporary HOME, so it never touches your
configuration, your clipboard or the data share. No pytest needed:

    python tests/gui_smoke_test.py                                 # with generated demo runs
    python tests/gui_smoke_test.py --runs RUN RUN [RUN ...]        # copies of real runs too
    python tests/gui_smoke_test.py --data ROOT                     # picks a few recent runs
    python tests/gui_smoke_test.py --only keys,plots               # some sections only
    QT_QPA_PLATFORM=wayland python tests/gui_smoke_test.py ...     # on a real display

Sections: browser, keys, tasks, menus, plots, panels, instruments, sequence, tracker, robustness.

* Without --runs / --data the test writes two small demo runs itself (a runtime with a process
  method and two plots, its kwargs, a yaml, a log and saved figures), so it needs no data.
  Real runs (two or more, with their saved figures and data) are copied and used instead, which
  also exercises your runtimes' processing and lets SeeQuence trace them. The first run is also
  copied without its figures, so that the live plot processes it.
* ``instruments`` needs instrumentserver. It starts a private server on a free localhost port
  with dummy instruments only, and writes dummy parameters into the copies.
* ``sequence`` needs acadia_qmsmt's sequence_viz. It builds a few test folders off hardware
  (nothing is deployed) and also traces the given runs. The step slider is checked on a given
  run with three or more sweep steps (the generated folders have one).
* ``robustness`` runs each case in a child process (slot exceptions, a viewed folder deleted,
  closing while a folder walk or the folder monitor is stuck, a monitor that hits I/O errors).

Each check prints PASS / FAIL; the exit code is 0 when all of them passed.
"""
import argparse
import contextlib
import gc
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

DATAFOLDER_FILE = "run.py"
MAX_RUN_MB = 200                 # --data skips runs larger than this
SECTIONS = ("browser", "keys", "tasks", "menus", "plots", "panels", "instruments", "sequence",
            "tracker", "robustness")
ROBUSTNESS_CASES = ("slot_exc", "deleted_folder", "close_autojump", "stuck_exit",
                    "monitor_recovery", "monitor_restarts", "close_during_jump")


def isolate(tmp):
    """A temporary HOME (acadia_gui reads its config path at import) and no bytecode files."""
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    os.environ["HOME"] = home
    os.environ["MPLCONFIGDIR"] = os.path.join(tmp, "mpl")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.dont_write_bytecode = True


def pick_runs(data_root, n=3):
    """The newest data folders under ``data_root`` that are not too large, skipping Trash."""
    found = []
    for dirpath, dirnames, filenames in os.walk(data_root):
        dirnames[:] = [d for d in dirnames if d != "Trash"]
        if DATAFOLDER_FILE in filenames:
            dirnames[:] = []
            size = sum(os.path.getsize(os.path.join(dirpath, f)) for f in filenames
                       if os.path.isfile(os.path.join(dirpath, f)))
            if size < MAX_RUN_MB * 1e6 and any(f.endswith(".png") for f in filenames):
                found.append((os.path.getmtime(os.path.join(dirpath, DATAFOLDER_FILE)), dirpath))
    found.sort(reverse=True)
    return [p for _, p in found[:n]]


# ---------------------------------------------------------------------------------- fixture

DEMO_RUNTIME = '''"""A demo runtime written by the GUI smoke test: no hardware, generated data."""
import numpy as np
from acadia import Runtime
from acadia.runtime import annotate_method


class SmokeDemoRuntime(Runtime):
    iterations: int = 100
    points: int = 41
    phase: float = 0.0
    amplitude: float = 1.0

    def main(self):
        pass

    @annotate_method(is_data_processor=True)
    def process(self, scale: float = 1.0):
        self.x = np.linspace(0, 1, self.points)
        self.y = scale * self.amplitude * np.sin(2 * np.pi * self.x + self.phase)
        self.z = np.outer(np.cos(np.pi * self.x), self.y)
        return self.iterations

    @annotate_method(plot_name="1. sine", axs_shape=(1, 1))
    def plot_sine(self, axs=None):
        import matplotlib.pyplot as plt
        if axs is None:
            fig, axs = plt.subplots()
        ax = np.asarray(axs).flat[0]
        ax.plot(self.x, self.y, "o-")
        ax.set_xlabel("x")
        ax.set_ylabel("signal")
        ax.set_title(f"amplitude {self.amplitude:g}")
        return ax.figure, axs

    @annotate_method(plot_name="2. map", axs_shape=(1, 1))
    def plot_map(self, axs=None):
        import matplotlib.pyplot as plt
        if axs is None:
            fig, axs = plt.subplots()
        ax = np.asarray(axs).flat[0]
        ax.pcolormesh(self.x, self.x, self.z, shading="auto")
        ax.set_xlabel("x")
        ax.set_ylabel("y")
        return ax.figure, axs
'''


def make_demo_runs(folder, n=2):
    """``n`` small runs of :data:`DEMO_RUNTIME`, laid out as a deploy would leave them."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    runs = []
    for i in range(n):
        run = os.path.join(folder, f"demo_{i}")
        os.makedirs(run)
        with open(os.path.join(run, "runtime.py"), "w") as f:
            f.write(DEMO_RUNTIME)
        with open(os.path.join(run, DATAFOLDER_FILE), "w") as f:
            f.write("# demo run written by the GUI smoke test\n")
        with open(os.path.join(run, "kwargs.json"), "w") as f:
            json.dump({"iterations": 100, "points": 41, "phase": 0.5 * i}, f)
        with open(os.path.join(run, "metadata.txt"), "w") as f:
            f.write("0 groups\n")
        with open(os.path.join(run, "config_demo.yaml"), "w") as f:
            f.write("demo:\n  points: 41\n  phase: %s\n" % (0.5 * i))
        with open(os.path.join(run, "runtime.log"), "w") as f:
            f.write("INFO demo run started\nINFO demo run finished\n")
        x = np.linspace(0, 1, 41)
        for name, draw in (("1. sine", lambda ax: ax.plot(x, np.sin(2 * np.pi * x + 0.5 * i))),
                           ("2. map", lambda ax: ax.pcolormesh(np.outer(np.cos(np.pi * x), x)))):
            fig, ax = plt.subplots()
            draw(ax)
            fig.savefig(os.path.join(run, f"{name}.png"))
            plt.close(fig)
        runs.append(run)
    return runs


class Fixture:
    """The test data tree::

        root/Exp/260101/120000, 130000, ...   copies of the given runs (figures kept)
        root/Exp/260101/190000                the first run again, without its figures (newest)
        root/Exp/plain/f000 ... f199          plain folders (f100 has a subfolder)
        root/Exp/empty                        an empty folder
        root/images                           three generated PNGs
        root/seq/<case>/<stamp>               sequence test folders (section 'sequence')
    """

    def __init__(self, tmp, runs):
        self.root = os.path.join(tmp, "data")
        self.day = os.path.join(self.root, "Exp", "260101")
        os.makedirs(self.day)
        self.runs = []
        for i, src in enumerate(runs):
            dst = os.path.join(self.day, f"1{2 + i}0000")
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".stop"))
            self.runs.append(dst)
        self.live = os.path.join(self.day, "190000")
        shutil.copytree(self.runs[0], self.live,
                        ignore=shutil.ignore_patterns("*.png", "*.pkl", "__pycache__", ".stop"))
        now = time.time()
        for i, run in enumerate(self.runs):                       # the live copy is the newest
            os.utime(os.path.join(run, DATAFOLDER_FILE), (now - 3600 + i, now - 3600 + i))
        os.utime(os.path.join(self.live, DATAFOLDER_FILE), (now, now))
        self.plain = os.path.join(self.root, "Exp", "plain")
        for i in range(200):
            os.makedirs(os.path.join(self.plain, f"f{i:03d}"))
        os.makedirs(os.path.join(self.plain, "f100", "sub"))
        self.empty = os.path.join(self.root, "Exp", "empty")
        os.makedirs(self.empty)
        self.images = os.path.join(self.root, "images")
        os.makedirs(self.images)
        self.seq = os.path.join(self.root, "seq")
        self.seq_folders = []
        self.demo = False

    def make_images(self):
        from PyQt5.QtGui import QColor, QImage
        for i, color in enumerate(("red", "green", "blue")):
            image = QImage(60, 40, QImage.Format_RGB32)
            image.fill(QColor(color))
            image.save(os.path.join(self.images, f"{i}_{color}.png"))

    def make_sequence_folders(self, cases):
        """Folders of the sequence_viz loopback cases, written as a deploy would write them,
        without any board contact. Returns the error, or None."""
        try:
            import acadia_qmsmt
            import acadia_qmsmt.qmsmt as qmsmt_module
            validation = os.path.join(os.path.dirname(acadia_qmsmt.__file__), "sequence_viz",
                                      "validation")
            sys.path.insert(0, validation)
            import timing_validation
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        for case in cases:
            folder = os.path.join(self.seq, case, "260101_120000")
            os.makedirs(folder)
            with contextlib.redirect_stdout(io.StringIO()):
                runtime = timing_validation.build_runtime(case, iterations=100)
                runtime.local_directory = folder
                runtime.remote_directory = "/tmp/not_deployed"
                shutil.copy(sys.modules[type(runtime).__module__].__file__,
                            os.path.join(folder, "runtime.py"))
                shutil.copy(qmsmt_module.__file__, os.path.join(folder, "acadia_qmsmt.py"))
                runtime._dump_fields()
            with open(os.path.join(folder, DATAFOLDER_FILE), "w") as f:
                f.write("# made off-hardware by the GUI smoke test; never deployed\n")
            self.seq_folders.append(folder)
        return None


# ---------------------------------------------------------------------------------- harness

class Smoke:
    def __init__(self, app, fixture, args):
        self.app, self.fx, self.args = app, fixture, args
        self.results = []
        self.section = ""

    def check(self, name, ok, info=""):
        self.results.append((self.section, name, bool(ok)))
        info = str(info).replace("\n", " | ")
        print(f"{'PASS' if ok else 'FAIL'}  [{self.section}] {name}  {info}", flush=True)
        return bool(ok)

    def pump(self, seconds):
        """Process events for ``seconds``; returns the longest single event-loop pass (s)."""
        end, worst = time.perf_counter() + seconds, 0.0
        while time.perf_counter() < end:
            t = time.perf_counter()
            self.app.processEvents()
            worst = max(worst, time.perf_counter() - t)
            time.sleep(0.003)
        return worst

    def pump_until(self, condition, limit):
        end = time.perf_counter() + limit
        while time.perf_counter() < end and not condition():
            self.app.processEvents()
            time.sleep(0.005)
        return condition()

    def browser(self, root=None, station=None, size=(1600, 950)):
        from acadia_gui.gui import DataBrowser
        win = DataBrowser(root or self.fx.root, station)
        win.resize(*size)
        win.show()
        self.pump(0.8)
        return win

    def focus(self, win, path, trigger=True):
        """Select ``path`` in the folder tree, waiting for the tree to load it."""
        ft = win.folder_tree
        ok = self.pump_until(lambda: ft.focus_path(path, trigger_callback=trigger), 10)
        self.pump(0.3)
        return ok

    def current(self, win):
        ft = win.folder_tree
        return ft.source_path_from_proxy_index(ft.tree.currentIndex())

    def close(self, win):
        win.close()
        self.pump(0.3)


# ---------------------------------------------------------------------------------- sections

def section_browser(s):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from acadia_gui.gui.center_view import LIVE, SEQUENCE
    t = time.perf_counter()
    win = s.browser()
    s.check("window up without waiting for the folder walk", time.perf_counter() - t < 5,
            f"{time.perf_counter() - t:.2f} s")
    ft, cv = win.folder_tree, win.center_view
    budget = s.args.click_budget
    for run in s.fx.runs + [s.fx.live]:
        ft.focus_path(run)
        worst = s.pump(1.5)
        s.check(f"open {os.path.basename(run)}: no long stall", worst < budget,
                f"longest stall {1e3 * worst:.0f} ms (budget {1e3 * budget:.0f})")
        s.check(f"open {os.path.basename(run)}: shown", cv._folder == run, str(cv._folder))
    s.pump_until(lambda: not ft.task_running(), 60)

    # back / forward
    a, b = s.fx.runs[0], s.fx.runs[1]
    s.focus(win, a)
    s.focus(win, b)
    ft.back_button.click()
    s.pump(0.5)
    s.check("back button: previous folder", s.current(win) == a, s.current(win))
    ft.forward_button.click()
    s.pump(0.5)
    s.check("forward button: next folder", s.current(win) == b, s.current(win))

    # collapse all and its undo
    btn, tree = ft.collapse_all_button, ft.tree

    def expanded():
        return sorted(ft._expanded_paths())
    s.focus(win, os.path.join(s.fx.plain, "f100", "sub"), trigger=False)
    s.pump(0.5)
    before = expanded()
    QTest.mouseClick(btn, Qt.LeftButton)
    s.pump(0.3)
    s.check("collapse all: nothing expanded", expanded() == [], str(expanded()))
    s.check("collapse all: selection kept", s.current(win).endswith("sub"), s.current(win))
    QTest.mouseClick(btn, Qt.LeftButton)
    s.pump(0.3)
    s.check("collapse all again: the same folders reopen", expanded() == before,
            f"{len(before)} folders")

    # Plot / SeeQuence buttons
    cv.btn_sequence.click()
    s.pump(0.5)
    s.check("SeeQuence button", cv.mode == SEQUENCE)
    cv.btn_live.click()
    s.pump(0.5)
    s.check("Plot button", cv.mode == LIVE)

    # menu bar: right panel, log window, every theme, memory label
    bar = win.menuBar() if hasattr(win, "menuBar") else None
    top = getattr(win, "top_menu", None) or getattr(win, "menubar", None) or bar
    rt_button = getattr(top, "toggle_right_tabs_button", None)
    if rt_button is not None:
        shown = win.right_tabs.isVisible()
        rt_button.click()
        s.pump(0.3)
        s.check("right panel button hides / shows the panel", win.right_tabs.isVisible() != shown)
        rt_button.click()
        s.pump(0.3)
    if hasattr(top, "theme_menu"):
        names = [a.text() for a in top.theme_menu.actions()]
        for action in top.theme_menu.actions():
            action.trigger()
            s.pump(0.4)
        s.check("every theme applies", bool(names), ", ".join(names))
        win.apply_theme("default")
        s.pump(0.3)
    if hasattr(top, "help_action"):
        import acadia_gui.gui.top_menubar as tmb
        opened = []
        real_open = tmb._open_external
        tmb._open_external = lambda target, is_file: (opened.append(target), True)[1]   # opens nothing
        try:
            top.help_action.trigger()
        finally:
            tmb._open_external = real_open
        s.check("Help opens the readme", opened == [str(tmb.README_PATH)] if tmb.README_PATH.is_file()
                else len(opened) <= 1, str(opened))
    if hasattr(top, "update_memory_label"):
        top.update_memory_label()
        s.check("memory label", bool(top.mem_label.text()), top.mem_label.text())
    s.close(win)


def section_keys(s):
    """Arrow keys follow the focus: the tree opens the folder reached after a short pause; the
    plot area steps the plots or images."""
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtTest import QTest
    win = s.browser()
    ft, tree, pm = win.folder_tree, win.folder_tree.tree, win.folder_tree.proxy_model
    fd = win.center_view.live
    lp = fd.live_plot
    opened = []
    real = ft.on_select_callback
    ft.on_select_callback = lambda p: (opened.append(p), real(p))

    def key(widget, k, mod=Qt.NoModifier, wait=0.5):
        opened.clear()
        QTest.keyClick(widget, k, mod)
        s.pump(wait)

    def click_folder(path):
        s.focus(win, path, trigger=False)
        QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                         tree.visualRect(tree.currentIndex()).center())
        s.pump(1.2)

    def children(folder, count):
        """``folder``'s subfolders in the order the tree shows them, once all ``count`` loaded."""
        index = ft.path_to_proxy_index(folder)
        tree.expand(index)
        s.pump_until(lambda: pm.rowCount(ft.path_to_proxy_index(folder)) >= count, 20)
        index = ft.path_to_proxy_index(folder)
        return [ft.source_path_from_proxy_index(pm.index(i, 0, index)) for i in range(pm.rowCount(index))]

    s.focus(win, s.fx.live, trigger=False)
    runs = children(s.fx.day, len(s.fx.runs) + 1)
    click_folder(runs[0])
    s.check("a click puts the focus in the tree", s.app.focusWidget() is tree,
            type(s.app.focusWidget()).__name__)
    opened.clear()
    QTest.keyClick(tree, Qt.Key_Down)
    s.pump(0.05)
    s.check("Down: moves, but does not open before the pause", s.current(win) == runs[1] and not opened,
            f"{s.current(win)} {opened}")
    s.pump(0.4)
    s.check("Down: opens after the pause", opened == [runs[1]], str(opened))
    opened.clear()
    for k in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Down):
        QTest.keyClick(tree, k)
        s.pump(0.03)
    s.pump(0.5)
    s.check("quick keys: only the folder reached opens", opened == [runs[2]], str(opened))
    key(tree, Qt.Key_Down, Qt.ControlModifier)
    s.check("Ctrl+Down opens nothing", not opened, str(opened))
    key(tree, Qt.Key_Left)
    s.check("Left opens nothing", not opened, str(opened))
    click_folder(runs[0])
    opened.clear()
    QTest.keyClick(tree, Qt.Key_Down)
    s.pump(0.05)
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     tree.visualRect(ft.path_to_proxy_index(runs[2])).center())
    s.pump(1.2)
    s.check("a click during the pause: only the clicked folder opens", opened == [runs[2]],
            str(opened))

    # PgUp / PgDn: first / last folder of the level; Home / End: first / last top folder
    s.focus(win, os.path.join(s.fx.plain, "f100"), trigger=False)
    plain = children(s.fx.plain, 200)
    s.focus(win, plain[100], trigger=False)
    tree.setFocus()
    key(tree, Qt.Key_PageDown)
    s.check("PgDn: last folder of the level", s.current(win) == plain[-1] and opened == [plain[-1]],
            s.current(win))
    key(tree, Qt.Key_PageUp)
    s.check("PgUp: first folder of the level", s.current(win) == plain[0] and opened == [plain[0]],
            s.current(win))
    tops = [ft.source_path_from_proxy_index(pm.index(i, 0, tree.rootIndex()))
            for i in range(pm.rowCount(tree.rootIndex()))]
    key(tree, Qt.Key_Home)
    s.check("Home: first top folder", s.current(win) == tops[0], s.current(win))
    key(tree, Qt.Key_End)
    s.check("End: last top folder", s.current(win) == tops[-1], s.current(win))

    # plot area: plots of a run processed by the live plot
    click_folder(s.fx.live)
    s.pump_until(lambda: not ft.task_running(), 120)
    s.pump(0.5)
    n, i0 = lp.plot_selector.count(), lp.plot_selector.currentIndex()
    c = lp.canvas
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(c.width() // 2, c.height() // 2))
    s.pump(0.3)
    s.check("a click on the plot puts the focus in the plot area", s.app.focusWidget() is fd,
            type(s.app.focusWidget()).__name__)
    if n >= 2:
        key(fd, Qt.Key_Down, wait=0.6)
        s.check("Down in the plot area: next plot", lp.plot_selector.currentIndex() == i0 + 1,
                f"{i0} -> {lp.plot_selector.currentIndex()} of {n}")
        s.check("Down in the plot area: the folder stays", not opened and s.current(win) == s.fx.live)
        key(fd, Qt.Key_Up, wait=0.6)
        s.check("Up in the plot area: previous plot", lp.plot_selector.currentIndex() == i0)
    else:
        print(f"      (the live run has {n} plot; plot stepping not checked)")
    lp.interval_input.setFocus()
    idx = lp.plot_selector.currentIndex()
    key(lp.interval_input, Qt.Key_Up, wait=0.3)
    s.check("Up in an input box leaves the plot alone", lp.plot_selector.currentIndex() == idx)

    # plot area: images
    s.fx.make_images()
    click_folder(s.fx.images)
    s.check("an image folder shows its images", fd.stack.currentWidget() is fd.png_view
            and fd.figure_selector.count() == 3, str(fd.figure_selector.count()))
    QTest.mouseClick(fd.image_label, Qt.LeftButton)
    s.pump(0.3)
    key(fd, Qt.Key_Down, wait=0.3)
    s.check("Down on an image: next image", fd.figure_selector.currentIndex() == 1,
            fd.figure_selector.currentText())
    s.close(win)


def section_tasks(s):
    """The progress row under the folder tree, the busy cursor, and that the latest click wins."""
    from PyQt5.QtCore import QEvent, QObject
    from PyQt5.QtWidgets import QApplication
    from acadia_gui.gui import data_folder_view as dfv
    win = s.browser()
    ft = win.folder_tree
    a, b, live = s.fx.runs[0], s.fx.runs[1], s.fx.live
    log = []

    class PaintSpy(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Paint:
                log.append(("paint", time.perf_counter()))
            return False
    spy = PaintSpy()
    ft.task_row.installEventFilter(spy)
    real_open = win._open_folder
    win._open_folder = lambda p: (log.append(("open", time.perf_counter(), p)), real_open(p))

    def opened():
        return [e[2] for e in log if e[0] == "open"]

    def idle():
        return not ft.task_row.isVisible() and QApplication.overrideCursor() is None

    s.pump(0.5)
    # 1. a run the live plot must process: the row is drawn before the open starts
    log.clear()
    ft.focus_path(live)
    s.pump(0.0)
    s.pump_until(lambda: not ft.task_running(), 120)
    s.pump(0.2)
    paint = next((e[1] for e in log if e[0] == "paint"), None)
    op = next((e for e in log if e[0] == "open"), None)
    s.check("a run to process: progress row painted before it opens",
            paint is not None and op is not None and paint < op[1])
    s.check("a run to process: row and busy cursor cleared after", idle())

    real_list = dfv._list_folder
    dfv._list_folder = lambda *a, **k: (time.sleep(0.01), real_list(*a, **k))[1]   # a slow share
    try:
        # 2. jump to the newest run: the row shows after a moment, then the newest opens
        log.clear()
        ft.select_most_recent_folder()
        s.pump_until(lambda: not ft.task_running(), 60)
        s.pump(0.3)
        s.check("jump to newest: opens the newest run", opened() == [live], str(opened()))
        s.check("jump to newest: cleared after", idle())
        # 3. jump aborted with the x button
        log.clear()
        ft.select_most_recent_folder()
        s.pump(0.5)
        ft.task_abort_button.click()
        idle_now = idle()
        s.pump(3.0)
        s.check("jump aborted: cleared at once, nothing opens later", idle_now and opened() == [],
                str(opened()))
        # 4. a click while the jump walks wins
        log.clear()
        ft.select_most_recent_folder()
        s.pump(0.4)
        ft.focus_path(a)
        s.pump(3.0)
        s.check("a click during a jump: only the clicked run opens", opened() == [a], str(opened()))
        # 5. a second jump click while one walks is ignored
        log.clear()
        ft.select_most_recent_folder()
        s.pump(0.2)
        tid = ft._task_id
        ft.select_most_recent_folder()
        same = ft._task_id == tid
        s.pump_until(lambda: not ft.task_running(), 60)
        s.pump(0.3)
        s.check("a second jump click is ignored", same and opened() == [live], str(opened()))
        # 6. search, aborted; then completed
        ft.search_button.click()
        log.clear()
        ft.search_box.setText(os.path.basename(b))
        ft.apply_search_filter()
        s.pump(0.3)
        ft.task_abort_button.click()
        s.pump(2.0)
        s.check("search aborted: nothing opens", opened() == [] and ft.search_matches == [],
                f"{opened()} {ft.search_matches}")
        log.clear()
        ft.apply_search_filter()
        s.pump_until(lambda: not ft.task_running(), 60)
        s.pump(0.3)
        s.check("search: opens the match", opened() == [b] and ft.match_label.text() == "1/1",
                f"{opened()} {ft.match_label.text()}")
        # 7. closing the search box stops a search
        log.clear()
        ft.search_box.setText("f1")
        ft.apply_search_filter()
        s.pump(0.3)
        ft.search_button.click()
        s.pump(2.0)
        s.check("closing the search box stops the search", opened() == [] and not ft.task_running())
        # 8. a root change stops a jump
        log.clear()
        ft.select_most_recent_folder()
        s.pump(0.3)
        ft.set_root_path(s.fx.plain)
        s.pump(2.0)
        s.check("a root change stops the jump", opened() == [] and not ft.task_running())
        ft.set_root_path(s.fx.root)
        s.pump(0.5)
    finally:
        dfv._list_folder = real_list
    # 9. two quick clicks: only the last opens
    log.clear()
    ft.focus_path(a)
    ft.focus_path(b)
    s.pump(2.0)
    s.check("two quick clicks: only the last opens", opened() == [b], str(opened()))
    # 10. plain folders and runs with figures: no row, no busy cursor
    shown = []
    real_show = ft._show_task_row
    ft._show_task_row = lambda: (shown.append(time.perf_counter()), real_show())
    for p in (s.fx.empty, s.fx.day, a):
        log.clear()
        shown.clear()
        ft.focus_path(p)
        s.pump(1.0)
        s.check(f"quiet open of {os.path.relpath(p, s.fx.root)}: no progress row",
                not shown and opened() == [p], f"{shown} {opened()}")
    s.close(win)


def section_menus(s):
    """Every right-click menu opens at the click, as a popup of the window, and still works."""
    from PyQt5.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication, QMenu
    from matplotlib.backend_bases import MouseEvent
    win = s.browser(size=(1200, 760))
    win.move(60, 60)
    run = s.fx.live
    s.focus(win, run)
    s.pump_until(lambda: not win.folder_tree.task_running(), 120)
    s.pump(1.0)
    real_clip = QApplication.clipboard().text()

    def menus():
        return [m for m in s.app.allWidgets() if isinstance(m, QMenu) and m.parent() is not None]

    def inspect_then(label, expect_global, choose=None):
        def go():
            m = s.app.activePopupWidget()
            if m is None:
                s.check(label + ": menu opened", False)
                return
            handle = m.windowHandle()
            screen = (m.screen() or s.app.primaryScreen()).availableGeometry()
            ex = min(expect_global.x(), screen.right() + 1 - m.width())   # kept on screen
            ey = min(expect_global.y(), screen.bottom() + 1 - m.height())
            d = m.pos() - QPoint(ex, ey)
            s.check(label + ": opens at the click", abs(d.x()) <= 2 and abs(d.y()) <= 2,
                    f"menu {m.pos().x()},{m.pos().y()} click {expect_global.x()},{expect_global.y()}")
            s.check(label + ": a popup of the window",
                    handle is not None and handle.transientParent() is not None)
            action = next((a for a in m.actions() if choose and a.text() == choose), None)
            if action:
                m.setActiveAction(action)
                QTest.keyClick(m, Qt.Key_Return)
            else:
                m.close()
        QTimer.singleShot(250, go)

    before = len(menus())
    tree = win.folder_tree.tree
    pos = tree.visualRect(tree.currentIndex()).center()
    inspect_then("folder tree menu", tree.viewport().mapToGlobal(pos), choose="Copy Path")
    win.folder_tree.open_context_menu(pos)
    s.pump(0.3)
    s.check("folder tree menu: Copy Path", QApplication.clipboard().text().rstrip("/") == run,
            QApplication.clipboard().text())
    QApplication.clipboard().setText(real_clip)                 # leave the clipboard as it was
    b = win.folder_tree.recent_button
    inspect_then("jump-to-newest menu", b.mapToGlobal(QPoint(5, 5)))
    win.folder_tree.toggle_recent_lock(QPoint(5, 5))
    s.pump(0.3)
    kv = win.right_tabs.kwargs_json_tab
    kv.tree.expandAll()
    s.pump(0.2)
    item = kv.tree.topLevelItem(0)
    if item is not None:
        ipos = kv.tree.visualItemRect(item).center()
        inspect_then("kwargs menu", kv.tree.viewport().mapToGlobal(ipos))
        kv.show_context_menu(ipos)
        s.pump(0.3)
    lp = win.center_view.live.live_plot
    c = lp.canvas
    if c.figure.axes:
        ax = c.figure.axes[0]
        x, y = ax.bbox.x0 + ax.bbox.width / 2, ax.bbox.y0 + ax.bbox.height / 2
        dpr = c.device_pixel_ratio
        local = QPoint(int(x / dpr), int(c.height() - y / dpr))
        qe = QMouseEvent(QEvent.MouseButtonPress, local, Qt.RightButton, Qt.RightButton, Qt.NoModifier)
        ev = MouseEvent("button_press_event", c, x, y, button=3, guiEvent=qe)
        key = (lp.current_plot_name, 0)
        was = "Autoscale" in lp.checked_right_click_flags[key]
        inspect_then("plot axis menu", c.mapToGlobal(local), choose="Autoscale")
        lp.handle_right_click(ev)
        s.pump(0.3)
        s.check("plot axis menu: Autoscale toggles",
                ("Autoscale" in lp.checked_right_click_flags[key]) != was)
    else:
        s.check("plot axis menu: the live run has a plot", False)
    s.pump(0.5)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    s.pump(0.2)
    s.check("menus freed after use", len(menus()) <= before, f"{before} -> {len(menus())}")
    s.close(win)


def section_plots(s):
    """The live plot: every plot, toolbar zoom / pan / back / forward / home, autoscale,
    subplot dialog, save, snapshot, themes, resizes, live updates, and that a hidden live plot
    does not load or poll."""
    import numpy as np
    from PyQt5.QtCore import QEvent, QPointF, Qt, QTimer
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QApplication, QMenu
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
    import acadia_gui.gui.live_plot_widget as lpw
    from acadia_gui.gui.center_view import LIVE, SEQUENCE
    win = s.browser()
    cv = win.center_view
    lp = cv.live.live_plot
    tb = lp.toolbar
    run = s.fx.live

    # a hidden live plot neither loads nor polls
    starts = []
    real_start = lp.start
    lp.start = lambda p: (starts.append(p), real_start(p))[1]
    cv.set_mode(SEQUENCE)
    s.pump(0.3)
    cv.load_images(run)
    s.pump(1.0)
    s.check("SeeQuence mode: the live plot does not load the run", not starts)
    s.check("SeeQuence mode: no polling", not lp.timer.isActive())
    cv.set_mode(LIVE)
    s.pump_until(lambda: bool(starts), 10)
    s.pump(1.0)
    s.check("Plot mode: the live plot loads the run", starts == [run], str(starts))
    s.check("Plot mode: polling", lp.timer.isActive())
    cv.set_mode(SEQUENCE)
    s.pump(0.3)
    paused = not lp.timer.isActive()
    cv.set_mode(LIVE)
    s.pump(0.3)
    s.check("SeeQuence and back: polling pauses and resumes without reloading",
            paused and lp.timer.isActive() and len(starts) == 1)
    lp.start = real_start
    s.pump_until(lambda: not win.folder_tree.task_running(), 120)
    s.pump(1.0)

    renders = []
    real_draw = FigureCanvasQTAgg.draw

    def counted_draw(canvas, *a, **k):
        r = real_draw(canvas, *a, **k)
        if canvas is lp.canvas:
            renders.append(time.perf_counter())
        return r
    FigureCanvasQTAgg.draw = counted_draw

    def current(name):
        """The canvas shows the current figure once the render throttle settles."""
        s.pump(0.3)
        shown = np.asarray(lp.canvas.buffer_rgba()).copy()
        real_draw(lp.canvas)
        fresh = np.asarray(lp.canvas.buffer_rgba())
        s.check(f"{name}: canvas up to date", shown.shape == fresh.shape and np.array_equal(shown, fresh))

    def ax_point(ax, fx, fy):
        x0, y0, x1, y1 = ax.bbox.extents
        dpr, height = lp.canvas.device_pixel_ratio, lp.canvas.figure.bbox.height
        return QPointF((x0 + fx * (x1 - x0)) / dpr, (height - (y0 + fy * (y1 - y0))) / dpr)

    def mouse(kind, pos, button=Qt.LeftButton, buttons=None):
        t = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
             "release": QEvent.MouseButtonRelease}[kind]
        if buttons is None:
            buttons = button if kind != "release" else Qt.NoButton
        QApplication.sendEvent(lp.canvas, QMouseEvent(t, pos, button if kind != "move" else Qt.NoButton,
                                                      buttons, Qt.NoModifier))

    def drag(a, b, steps=10):
        mouse("press", a)
        for i in range(1, steps + 1):
            mouse("move", a + (b - a) * (i / steps), buttons=Qt.LeftButton)
            s.app.processEvents()
        mouse("release", b)
        s.pump(0.05)

    try:
        for i in range(lp.plot_selector.count()):
            lp.plot_selector.setCurrentIndex(i)
            s.pump(0.4)
            axes = lp.canvas.figure.axes
            s.check(f"plot '{lp.plot_selector.itemText(i)}' draws", any(
                ax.lines or ax.collections or ax.images for ax in axes), f"{len(axes)} axes")
        lp.plot_selector.setCurrentIndex(0)
        s.pump(0.4)
        current("plot list")
        ax = lp.canvas.figure.axes[0]
        full = ax.get_xlim()
        tb.zoom()
        drag(ax_point(ax, 0.3, 0.3), ax_point(ax, 0.6, 0.7))
        zoomed = ax.get_xlim()
        tb.zoom()
        s.check("toolbar zoom", (zoomed[1] - zoomed[0]) < 0.5 * (full[1] - full[0]), f"{full} -> {zoomed}")
        tb.back()
        s.pump(0.1)
        s.check("toolbar back", np.allclose(ax.get_xlim(), full))
        tb.forward()
        s.pump(0.1)
        s.check("toolbar forward", np.allclose(ax.get_xlim(), zoomed))
        tb.home()
        s.pump(0.1)
        s.check("toolbar home", np.allclose(ax.get_xlim(), full))
        tb.pan()
        drag(ax_point(ax, 0.5, 0.5), ax_point(ax, 0.2, 0.5))
        tb.pan()
        s.check("toolbar pan", ax.get_xlim()[0] > full[0], f"{full} -> {ax.get_xlim()}")
        current("pan")
        tb.home()
        s.pump(0.2)
        # right-click Autoscale on, then off (restores the zoom)
        ax.set_xlim(full[0] + 0.4 * (full[1] - full[0]), full[0] + 0.5 * (full[1] - full[0]))
        lp.canvas.draw_idle()
        s.pump(0.2)
        zoom_lim = ax.get_xlim()

        def click_autoscale():
            menu = QApplication.activePopupWidget() or next(
                m for m in win.findChildren(QMenu) if m.isVisible())
            next(a for a in menu.actions() if a.text() == "Autoscale").trigger()
            menu.close()
        key = (lp.current_plot_name, 0)
        for state in ("on", "off"):
            QTimer.singleShot(200, click_autoscale)
            p = ax_point(ax, 0.5, 0.5)
            mouse("press", p, button=Qt.RightButton)
            mouse("release", p, button=Qt.RightButton)
            s.pump(0.3)
            on = "Autoscale" in lp.checked_right_click_flags[key]
            if state == "on":
                s.check("Autoscale on", on and (ax.get_xlim()[1] - ax.get_xlim()[0])
                        > 2 * (zoom_lim[1] - zoom_lim[0]))
            else:
                s.check("Autoscale off restores the zoom", not on and np.allclose(ax.get_xlim(), zoom_lim))
        tb.home()
        s.pump(0.2)
        dialog = tb.configure_subplots()
        s.pump(0.3)
        before = lp.canvas.figure.subplotpars.left
        dialog._spinboxes["left"].setValue(min(0.4, before + 0.1))
        s.pump(0.2)
        s.check("subplot dialog", abs(lp.canvas.figure.subplotpars.left - before) > 0.05)
        dialog.close()
        lp.update_plot(force=True, reprocess_data=False)
        s.pump(0.3)
        import matplotlib.backends.qt_compat as qc
        out = os.path.join(tempfile.mkdtemp(dir=s.args.tmp), "saved.png")
        real_dialog = qc.QtWidgets.QFileDialog.getSaveFileName
        qc.QtWidgets.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
        try:
            tb.save_figure()
            s.pump(0.3)
        finally:
            qc.QtWidgets.QFileDialog.getSaveFileName = real_dialog
        s.check("toolbar save", os.path.exists(out) and os.path.getsize(out) > 1000)
        got = {}

        class FakeClipboard:
            def setImage(self, image):
                got["image"] = image

        class FakeApp:
            @staticmethod
            def clipboard():
                return FakeClipboard()
        real_app, real_platform = lpw.QApplication, lpw.detect_platform
        lpw.QApplication, lpw.detect_platform = FakeApp, (lambda: "linux")   # not the real clipboard
        try:
            lp.snapshot_current_plot()
        finally:
            lpw.QApplication, lpw.detect_platform = real_app, real_platform
        image = got.get("image")
        s.check("snapshot", image is not None and not image.isNull() and image.width() > 100)
        for theme in ("dark", "light"):
            renders.clear()
            lp.set_theme(theme)
            s.pump(0.3)
            s.check(f"plot theme {theme}", len(renders) >= 1)
        for name, heights in (("shrink", range(950, 700, -25)), ("grow", range(700, 1000, 25))):
            size0 = (lp.canvas.width(), lp.canvas.height())
            for h in heights:
                win.resize(1600, h)
                s.pump(0.015)
            s.pump(0.4)
            s.check(f"window {name}: canvas follows", (lp.canvas.width(), lp.canvas.height()) != size0)
            current(f"window {name}")
        lp.timer.setInterval(300)
        lp.timer.start(300)
        renders.clear()
        t0 = time.perf_counter()
        os.utime(os.path.join(run, "metadata.txt"))
        s.pump(2.5)
        s.check("live update after new data", any(r > t0 for r in renders), f"{len(renders)} renders")
        if s.fx.demo:
            # new data that needs more room (a longer title, wider tick labels): after the live
            # update every title, label and tick label is still inside the figure
            lp.plot_selector.setCurrentIndex(0)
            s.pump(0.5)
            kwargs_path = os.path.join(run, "kwargs.json")
            with open(kwargs_path) as f:
                kwargs = json.load(f)
            for amplitude in (1.0, 123456.789, 0.000123):
                kwargs["amplitude"] = amplitude
                with open(kwargs_path, "w") as f:
                    json.dump(kwargs, f)
                renders.clear()
                os.utime(os.path.join(run, "metadata.txt"), (time.time() + 1, time.time() + 1))
                s.pump_until(lambda: bool(renders), 5)
                s.pump(0.3)
                s.check(f"live update to amplitude {amplitude:g}: text stays inside the figure",
                        *_text_inside(lp.canvas))
    finally:
        FigureCanvasQTAgg.draw = real_draw
    s.close(win)


def _text_inside(canvas):
    """``(ok, info)``: everything drawn for each axes (title, labels, tick labels) lies inside
    the figure."""
    renderer = canvas.get_renderer()
    fig = canvas.figure.bbox
    out = []
    for i, ax in enumerate(canvas.figure.axes):
        e = ax.get_tightbbox(renderer)
        if e.x0 < fig.x0 - 1 or e.x1 > fig.x1 + 1 or e.y0 < fig.y0 - 1 or e.y1 > fig.y1 + 1:
            out.append(f"axes {i}: x {e.x0:.0f}..{e.x1:.0f}, y {e.y0:.0f}..{e.y1:.0f}")
    return not out, (f"outside a {fig.width:.0f}x{fig.height:.0f} figure: {out}" if out else "")


def section_panels(s):
    """Side panels follow the folder (yaml, kwargs, logs), and the log window's level filters."""
    win = s.browser()
    run = s.fx.runs[0]
    s.focus(win, run)
    s.pump(1.0)
    rt = win.right_tabs
    s.check("kwargs panel lists the run's arguments", rt.kwargs_json_tab.tree.topLevelItemCount() > 0,
            f"{rt.kwargs_json_tab.tree.topLevelItemCount()} items")
    from acadia_gui.gui.log_view import find_log_files
    from acadia_gui.gui.yaml_view import find_yaml_files
    yamls, logs = find_yaml_files(run), find_log_files(run)
    yaml_tabs = [rt.config_yaml_tab.tabText(i) for i in range(rt.config_yaml_tab.count())]
    s.check("yaml panel: a tab per yaml file", yaml_tabs == yamls if yamls else yaml_tabs == ["No Config"],
            f"{yaml_tabs}")
    first = rt.config_yaml_tab.widget(0)
    s.check("yaml panel: the file's text", first is not None and bool(first.toPlainText().strip()))
    s.check("log panel: a tab per log file", rt.log_tab.count() == max(len(logs), 1),
            f"{rt.log_tab.count()} tabs, {len(logs)} log files")
    s.focus(win, s.fx.empty)
    s.pump(0.5)
    s.check("a plain folder clears the kwargs panel", rt.kwargs_json_tab.tree.topLevelItemCount() == 0)

    lw = win.log_window
    lw.setVisible(True)
    logger = logging.getLogger("acadia_gui_smoke")
    logger.setLevel(logging.DEBUG)
    boxes = {"Debug": lw.checkbox_debug, "Info": lw.checkbox_info, "Warning": lw.checkbox_warning,
             "Error": lw.checkbox_error}
    levels = {"Debug": logging.DEBUG, "Info": logging.INFO, "Warning": logging.WARNING,
              "Error": logging.ERROR}
    for name, box in boxes.items():
        box.setChecked(True)
    for name, level in levels.items():
        logger.log(level, f"smoke {name.lower()} line")
    s.pump(0.3)
    text = lw.log_box.toPlainText()
    s.check("log window shows every level", all(f"smoke {n.lower()} line" in text for n in levels),
            text[-200:].replace("\n", " | "))
    for name, box in boxes.items():           # the boxes filter the lines that arrive after
        box.setChecked(False)
        logger.log(levels[name], f"smoke hidden {name.lower()} line")
        box.setChecked(True)
        logger.log(levels[name], f"smoke shown {name.lower()} line")
        s.pump(0.2)
        text = lw.log_box.toPlainText()
        s.check(f"log window: {name} box filters new lines", f"smoke hidden {name.lower()} line" not in text
                and f"smoke shown {name.lower()} line" in text)
    s.check("log window: Instrument box present", lw.checkbox_instrument.isChecked())
    s.close(win)


INSTRUMENT_DUMMIES = '''
"""Test-only dummy instruments for the GUI smoke test (never real hardware)."""
import time
from qcodes.utils import validators
from instrumentserver.testing.dummy_instruments.generic import DummyInstrumentTimeout


class SlowSetDummy(DummyInstrumentTimeout):
    """voltage_a / voltage_b take set_wait seconds to set; limited accepts 0..1 only."""

    def __init__(self, name, set_wait=1.0, **kwargs):
        super().__init__(name, **kwargs)
        self.set_wait = float(set_wait)
        self._values = {}
        for p in ("voltage_a", "voltage_b"):
            self._values[p] = 0.0
            self.add_parameter(p, unit="V", get_cmd=lambda p=p: self._values[p],
                               set_cmd=lambda v, p=p: self._slow_set(p, v))
        self.add_parameter("limited", set_cmd=None, initial_value=0.5, vals=validators.Numbers(0, 1))

    def _slow_set(self, p, v):
        time.sleep(self.set_wait)
        self._values[p] = v


class FaultyDummy(DummyInstrumentTimeout):
    """Every load fails: looking up its output parameter raises."""

    def __getattr__(self, name):
        if name == "output":
            raise RuntimeError("Device not responding")
        return super().__getattr__(name)


class HangingDummy(DummyInstrumentTimeout):
    """level takes set_wait seconds to set, longer than the station's timeout."""

    def __init__(self, name, set_wait=10.0, **kwargs):
        super().__init__(name, **kwargs)
        self.set_wait = float(set_wait)
        self.add_parameter("level", set_cmd=lambda v: time.sleep(self.set_wait), get_cmd=lambda: 0.0)
'''

INSTRUMENT_SERVER = '''
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from instrumentserver.apps import server
sys.exit(server(port=int(sys.argv[1]), addresses=[]))
'''


def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def section_instruments(s):
    """Loading a run's instrument parameters onto a private server's dummy instruments."""
    try:
        import instrumentserver  # noqa: F401
        from instrumentserver.client import ClientStation
    except Exception as exc:
        print(f"      skipped: instrumentserver not available ({exc})")
        return
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    folder = os.path.join(s.args.tmp, "instruments")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "smoke_dummies.py"), "w") as f:
        f.write(INSTRUMENT_DUMMIES)
    with open(os.path.join(folder, "smoke_server.py"), "w") as f:
        f.write(INSTRUMENT_SERVER)
    good = {"slow_dummy": {"voltage_a": 1.25, "voltage_b": -0.75, "limited": 0.3},
            "fast_dummy": {"param1": 7, "param2": 8}, "not_in_station": {"x": 1},
            "faulty_dummy": {"output": 1.0, "param1": 3}, "hanging_dummy": {"level": 0.5}}
    a, b, other = s.fx.runs[0], s.fx.runs[1], s.fx.live
    for run, params in ((a, good), (other, good), (b, {"slow_dummy": {"limited": 5.0}})):
        with open(os.path.join(run, "inst_params.json"), "w") as f:     # in the copies only
            json.dump(params, f)
    port = _free_port()
    server = subprocess.Popen([sys.executable, os.path.join(folder, "smoke_server.py"), str(port)],
                              stdout=open(os.path.join(folder, "server.log"), "w"),
                              stderr=subprocess.STDOUT)
    station = None
    try:
        dummies = {"slow_dummy": {"instrument_class": "smoke_dummies.SlowSetDummy", "set_wait": 1.0},
                   "fast_dummy": {"instrument_class":
                                  "instrumentserver.testing.dummy_instruments.generic.DummyInstrumentTimeout"},
                   "faulty_dummy": {"instrument_class": "smoke_dummies.FaultyDummy"},
                   "hanging_dummy": {"instrument_class": "smoke_dummies.HangingDummy", "set_wait": 10.0}}
        for _ in range(60):
            try:
                station = ClientStation(host="localhost", port=port, timeout=5, init_instruments=dummies)
                break
            except Exception:
                time.sleep(0.5)
        if not s.check("private test server with dummy instruments", station is not None):
            return
        win = s.browser(station=station)
        win.log_window.setVisible(True)
        tabs, ft = win.right_tabs, win.folder_tree
        iv = tabs.instruments_tab

        def open_run(path):
            s.focus(win, path)
            s.pump(0.5)
            tabs.setCurrentWidget(iv)
            s.pump(0.2)

        def select_all():
            iv.select_all_checkbox.setChecked(False)
            iv.select_all_checkbox.setChecked(True)
            s.pump(0.1)

        def wait_done(limit):
            t0, last, worst = time.perf_counter(), time.perf_counter(), 0.0
            bars = set()
            while iv._loading is not None and time.perf_counter() - t0 < limit:
                s.app.processEvents()
                now = time.perf_counter()
                worst, last = max(worst, now - last), now
                bars.add(iv.load_bar.text())
                time.sleep(0.005)
            s.pump(0.2)
            return worst, bars

        def name_colors():
            out = {}
            for i in range(iv.tree.topLevelItemCount()):
                item = iv.tree.topLevelItem(i)
                fg = item.data(0, Qt.ForegroundRole)
                out[item.text(0)] = fg.color().name() if fg is not None else None
            return out
        colors = iv._colors
        open_run(a)
        select_all()
        QTest.mouseClick(iv.load_button, Qt.LeftButton)
        s.app.processEvents()
        s.check("Load: button says it is loading", iv.load_button.text() == "Loading Parameters..."
                and not iv.load_button.isEnabled(), iv.load_button.text())
        s.check("Load: progress bar starts at 0/4", iv.load_bar.isVisible() and iv.load_bar.text() == "0/4",
                iv.load_bar.text())
        worst, bars = wait_done(120)
        s.check("Load: the GUI stays responsive", worst < 0.25, f"longest stall {1e3 * worst:.0f} ms")
        s.check("Load: the bar counts the instruments", {"1/4", "2/4", "3/4"} & bars, str(sorted(bars)))
        t0 = time.perf_counter()
        gc.collect()
        s.check("garbage collection after a timeout does not hang", time.perf_counter() - t0 < 2)
        text = iv.load_status_label.text()
        s.check("result line", "Done setting 2 instruments" in text and "2 failed" in text
                and "1 not available" in text, text)
        s.check("instrument names colored by result", name_colors() == {
            "fast_dummy": colors["succeeded"], "slow_dummy": colors["succeeded"],
            "faulty_dummy": colors["failed"], "hanging_dummy": colors["failed"],
            "not_in_station": colors["not_available"]}, str(name_colors()))
        box = iv._result_popup
        s.check("result popup", box is not None and box.isVisible() and "Succeeded" in box.text()
                and sorted(b_.text().replace("&", "") for b_ in box.buttons()) == ["Go to runtime", "OK"])
        lines = [l for l in win.log_window.log_box.toPlainText().splitlines() if "[INSTRUMENT]" in l]
        s.check("log line", len(lines) == 1 and "failed: faulty_dummy, hanging_dummy" in lines[0],
                repr(lines))
        QTest.keyClick(box, Qt.Key_Return)
        s.pump(0.3)
        s.check("Enter closes the popup", iv._result_popup is None)
        tabs.setCurrentIndex(0)
        s.pump(0.2)
        tabs.setCurrentWidget(iv)
        s.pump(0.2)
        s.check("a tab switch clears the result", not iv.load_status_label.isVisible())
        # another run mid-load, then Go to runtime
        open_run(a)
        select_all()
        QTest.mouseClick(iv.load_button, Qt.LeftButton)
        s.pump(0.8)
        open_run(other)
        s.check("another run during a load: still loading", iv._loading is not None)
        wait_done(120)
        box = iv._result_popup
        go = [b_ for b_ in box.buttons() if "Go to runtime" in b_.text()][0]
        QTest.mouseClick(go, Qt.LeftButton)
        s.pump(1.0)
        s.check("Go to runtime opens the loaded run", s.current(win) == a and iv._result_popup is None,
                s.current(win))
        # a rejected value, Esc
        open_run(b)
        select_all()
        QTest.mouseClick(iv.load_button, Qt.LeftButton)
        wait_done(60)
        s.check("a run with one instrument: result shown", "Done setting" in iv.load_status_label.text()
                and iv._result_popup is not None, iv.load_status_label.text())
        QTest.keyClick(iv._result_popup, Qt.Key_Escape)
        s.pump(0.3)
        s.check("Esc closes the popup", iv._result_popup is None)
        # the server goes away
        server.kill()
        server.wait()
        open_run(a)
        select_all()
        QTest.mouseClick(iv.load_button, Qt.LeftButton)
        worst, _bars = wait_done(120)
        s.check("server gone: every instrument failed", "Done setting 0 instruments"
                in iv.load_status_label.text(), iv.load_status_label.text())
        s.check("server gone: the GUI stays responsive", worst < 0.25, f"longest stall {1e3 * worst:.0f} ms")
        if iv._result_popup is not None:
            iv._result_popup.close()
        win.log_window.checkbox_instrument.setChecked(False)
        n0 = win.log_window.log_box.toPlainText().count("[INSTRUMENT]")
        iv._log_load_result(a, [("Succeeded", ["x"], "succeeded"), ("Failed", [], "failed"),
                                ("Not available", [], "not_available")])
        s.pump(0.2)
        s.check("log window: unticking Instrument hides those lines",
                win.log_window.log_box.toPlainText().count("[INSTRUMENT]") == n0)
        s.close(win)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        if station is not None:
            # close the station's connection before dropping it: collected unclosed, its zmq
            # context and socket are finalized together and the context's term() never returns
            from acadia_gui.gui.instrument_parameter_view import _close_client
            _close_client(station.client)


def section_sequence(s):
    """SeeQuence: tracing, the Control flow panel, A buttons, the drawing limit, the tabs above
    the plot, the step slider and the arrow keys."""
    try:
        from acadia_qmsmt.sequence_viz.plotting import branch_regions, tab_row, tab_transform
    except Exception as exc:
        print(f"      skipped: acadia_qmsmt.sequence_viz not available ({exc})")
        return
    from PyQt5.QtCore import QEvent, QPoint, Qt
    from PyQt5.QtGui import QKeyEvent, QMouseEvent
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QComboBox, QGroupBox, QLabel, QSpinBox, QToolButton
    from matplotlib.backend_bases import MouseEvent
    from acadia_gui.gui import sequence_view
    from acadia_gui.gui.center_view import SEQUENCE
    error = s.fx.make_sequence_folders(("loop_2", "test_true", "test_false_nospec", "rb_stream",
                                        "xeb_cycle_cz"))
    s.check("sequence test folders built off hardware", error is None, error or "")
    win = s.browser(size=(1800, 1000))
    cv = win.center_view
    cv.set_mode(SEQUENCE)
    s.pump(0.3)
    sw = cv.sequence

    def load(folder):
        sw.load_folder(folder)
        s.pump(0.3)
        return sw.trace

    def layout(trace):
        return tuple((p.index, p.start, p.stop) for p in trace.placements), round(trace.length_ns, 3)

    # every folder traces; the user's runs leave no __pycache__ behind
    for folder in (s.fx.runs if not s.fx.demo else []) + s.fx.seq_folders:
        name = os.path.relpath(folder, s.fx.root)
        tr = load(folder)
        s.check(f"trace {name}", tr is not None, sw.status.text()[:120])
        if folder in s.fx.runs:
            s.check(f"trace {name}: no __pycache__ written into the run",
                    not os.path.isdir(os.path.join(folder, "__pycache__")))

    # Control flow panel: one row per construct; every pin, then back to auto, draws the original
    n_rows = n_pins = 0
    for folder in s.fx.seq_folders:
        tr = load(folder)
        if tr is None:
            continue
        name = os.path.basename(os.path.dirname(folder))
        entries = tr.control_flow_summary()
        s.check(f"{name}: Control flow panel shown iff there are loops / tests",
                sw.flow_group.isVisible() == bool(entries))
        if not entries:
            continue
        s.check(f"{name}: one row per construct", len(sw._flow_rows) == len(entries))
        base = layout(tr)
        for e in entries:
            n_rows += 1
            label, field = sw._flow_rows[e["key"]]
            s.check(f"{name} {e['tag']}: row starts with its tag", label.text().strip().startswith(e["tag"]))
            if not (e["settable"] and e["reached"]):
                s.check(f"{name} {e['tag']}: read-only row is a label", isinstance(field, QLabel)
                        or not e["reached"])
                continue
            button = field.parentWidget().findChild(QToolButton)
            if isinstance(field, QComboBox):
                field.setCurrentIndex(2 if e["taken"] else 1)
            else:
                field.setValue(min((e["count"] or 1) + 1, field.maximum()))
            s.pump(0.05)
            st = tr.flow_state.get(e["key"])
            s.check(f"{name} {e['tag']}: pinned", st is not None and st["source"] == "pinned", str(st))
            s.check(f"{name} {e['tag']}: A button enabled while pinned", button is not None and button.isEnabled())
            button.click()
            s.pump(0.05)
            n_pins += 1
            s.check(f"{name} {e['tag']}: A goes back to auto and the original drawing",
                    layout(tr) == base and not tr.loop_counts and not tr.path_choices
                    and not button.isEnabled())
    s.check("rows and pins exercised", n_rows > 0 and n_pins > 0, f"{n_rows} rows, {n_pins} pins")

    # the drawing limit: a count that would draw too many blocks is lowered, other pins refused
    loop = next((f for f in s.fx.seq_folders if "loop_2" in f), None)
    if loop is not None:
        tr = load(loop)
        key = next(iter(sw._flow_rows))
        field = sw._flow_rows[key][1]
        limit = sequence_view.MAX_DRAWN_BLOCKS
        sequence_view.MAX_DRAWN_BLOCKS = len(tr.placements) + 6
        try:
            field.setValue(100)
            s.pump(0.2)
            s.check("limit: a pass count is lowered to what fits", 1 < field.value() < 100
                    and len(tr.placements) <= sequence_view.MAX_DRAWN_BLOCKS
                    and "most that can be drawn" in sw.status.text(), f"{field.value()} passes")
        finally:
            sequence_view.MAX_DRAWN_BLOCKS = limit
        field.parentWidget().findChild(QToolButton).click()
        s.pump(0.1)
    test = next((f for f in s.fx.seq_folders if "test_true" in f), None)
    if test is not None:
        tr = load(test)
        key = next(iter(sw._flow_rows))
        field = sw._flow_rows[key][1]
        field.setCurrentIndex(2)                          # skipped: fewer blocks, kept
        s.pump(0.1)
        limit = sequence_view.MAX_DRAWN_BLOCKS
        sequence_view.MAX_DRAWN_BLOCKS = 1
        try:
            field.setCurrentIndex(1)                      # runs: more blocks, refused
            s.pump(0.1)
            s.check("limit: a pin that adds blocks is refused, the box shows the old value",
                    field.currentIndex() == 2 and sw.status.text().startswith("Not applied"))
        finally:
            sequence_view.MAX_DRAWN_BLOCKS = limit
        field.setCurrentIndex(0)
        s.pump(0.1)

    # tabs above the plot: one row per level, no overlaps, hover, click edits the row
    nested = next((f for f in s.fx.seq_folders if "xeb_cycle_cz" in f), None)
    if nested is not None:
        tr = load(nested)
        ax = sw.view.ax
        sw.canvas.draw()
        tabs = ax._seqviz_tabs

        def tab_px(tab):
            b, h = tab_row(tab["level"], ax._seqviz_tab_levels)
            (x0, y0), (x1, y1) = tab_transform(ax).transform([(tab["x0"], b), (tab["x1"], b + h)])
            return x0, y0, x1, y1
        boxes = [tab_px(t) for t in tabs]
        overlap = sum(1 for i, p in enumerate(boxes) for q in boxes[i + 1:]
                      if min(p[2], q[2]) - max(p[0], q[0]) > 0.5 and min(p[3], q[3]) - max(p[1], q[1]) > 0.5)
        s.check("tabs: one per box, one row per nesting level",
                len(tabs) == len(branch_regions(tr)) and ax._seqviz_tab_levels >= 2,
                f"{len(tabs)} tabs, {ax._seqviz_tab_levels} rows")
        s.check("tabs: none overlap", overlap == 0)
        settable = [t for t in tabs if isinstance(sw._flow_rows[t["key"]][1], (QComboBox, QSpinBox))]
        if settable:
            tab = settable[0]
            x0, y0, x1, y1 = tab_px(tab)
            mid = ((x0 + x1) / 2, (y0 + y1) / 2)
            sw.canvas.callbacks.process("motion_notify_event",
                                        MouseEvent("motion_notify_event", sw.canvas, *mid))
            s.pump(0.1)
            s.check("tabs: hover shows the caption", sw.view._hover.get_visible()
                    and "click to change" in sw.view._hover.get_text(), sw.view._hover.get_text())
            xlim = sw.view.xlim_ns
            sw.canvas.callbacks.process("button_press_event",
                                        MouseEvent("button_press_event", sw.canvas, *mid, button=1))
            s.pump(0.2)
            menu = sw._flow_menu
            s.check("tabs: a click opens the menu and does not zoom", menu.isVisible() and sw.view.xlim_ns == xlim)
            field = sw._flow_rows[tab["key"]][1]
            if isinstance(field, QComboBox):
                next(a for a in menu.actions() if a.isCheckable() and a.text() == "skipped").trigger()
                s.pump(0.2)
                s.check("tabs: the menu pins through the row", field.currentIndex() == 2)
                field.setCurrentIndex(0)
            else:
                spin = menu.findChild(QSpinBox)
                spin.setValue(spin.value() + 2)
                s.pump(0.2)
                s.check("tabs: the menu pins through the row", field.value() == spin.value())
                field.setValue(field.minimum())
            menu.close()
            s.pump(0.2)

    # the step slider, its box and the arrow keys (a run with several sweep steps)
    stepped = None
    for folder in (s.fx.runs if not s.fx.demo else []):
        tr = load(folder)
        if tr is not None and tr.n_points >= 3:
            stepped = folder
            break
    if stepped is None:
        print("      (no given run has 3 or more sweep steps: the step slider is not checked;"
              " give one with --runs)")
    else:
        n = sw.trace.n_points
        tracer = next(g for g in sw.options.widget().findChildren(QGroupBox) if g.title() == "Tracer")
        s.check("Tracer group has no step box", not tracer.findChildren(QSpinBox))
        bar = sw.step_scroll
        s.check("step slider under the plot, enabled, box beside it",
                bar.isEnabled() and bar.maximum() == n - 1 == sw.step.maximum())
        QTest.mouseClick(bar, Qt.LeftButton, Qt.NoModifier, QPoint(int(bar.width() * 0.75), bar.height() // 2))
        s.pump(0.3)
        s.check("a click on the slider jumps there", abs(sw.step.value() - 0.75 * (n - 1)) <= max(n // 20, 2)
                and sw.trace.point == sw.step.value(), f"step {sw.step.value()} of {n}")
        y = bar.height() // 2
        QTest.mousePress(bar, Qt.LeftButton, Qt.NoModifier, QPoint(int(bar.width() * 0.1), y))
        s.app.sendEvent(bar, QMouseEvent(QEvent.MouseMove, QPoint(int(bar.width() * 0.5), y),
                                         Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        QTest.mouseRelease(bar, Qt.LeftButton, Qt.NoModifier, QPoint(int(bar.width() * 0.5), y))
        s.pump(0.3)
        s.check("a drag on the slider follows the cursor", abs(sw.step.value() - 0.5 * (n - 1)) <= max(n // 20, 2),
                f"step {sw.step.value()}")
        sw.step.setValue(1)
        s.pump(0.2)
        s.check("the box moves the slider", bar.value() == 1 and sw.trace.point == 1)

        def key(k):
            for kind in (QEvent.KeyPress, QEvent.KeyRelease):
                s.app.sendEvent(sw.canvas, QKeyEvent(kind, k, Qt.NoModifier))
            s.pump(0.2)
        key(Qt.Key_Right)
        right = sw.trace.point
        key(Qt.Key_Left)
        s.check("Right / Left on the plot step through the sweep", (right, sw.trace.point) == (2, 1))
        sw.step.setValue(0)
        s.pump(0.2)
        key(Qt.Key_Left)
        s.check("Left at the first step stays", sw.trace.point == 0)
    s.close(win)


def _tracker_root(folder, n=120):
    """A data root of ``n`` runs whose generated (not real) instrument parameters change in known
    ways: a source with a jittering temperature reading, a clock that loses lock once and a
    frequency set three times; a DAC whose voltage readback flickers, is moved twice and is missing
    from runs 105-111; one run in a Trash folder with a stray setting and no source."""
    import random
    random.seed(7)
    os.makedirs(folder)
    expected = {"src_a": 3 + 2, "dac_b": 2 + 2}   # frequency changes + lock lost/regained; moves + gone/back
    t0 = time.time() - n * 600
    for i in range(n):
        day = os.path.join(folder, "Exp", "260101" if i < n // 2 else "260102")
        run = os.path.join(day, f"{i:06d}")
        os.makedirs(run)
        frequency = 5e9 if i < 30 else 5.1e9 if i < 70 else 5.2e9 if i < 100 else 5.25e9
        locked = 0 if 50 <= i < 55 else 1
        voltage = (0.1 if i < 40 else 0.35 if i < 90 else 0.30) + random.choice([0.0, 2e-5])
        params = {"src_a": {"frequency": frequency, "temperature": round(40 + random.gauss(0, 0.05), 3),
                            "reference_detected": locked, "power": 1.0},
                  "dac_b": {"voltage_1": round(voltage, 6), "ramp": 0.01}}
        if 105 <= i < 112:
            del params["dac_b"]
        for name, value in ((DATAFOLDER_FILE, "# generated by the GUI smoke test\n"),
                            ("inst_params.json", json.dumps(params))):
            with open(os.path.join(run, name), "w") as f:
                f.write(value)
        t = t0 + i * 600
        for name in (DATAFOLDER_FILE, "inst_params.json"):
            os.utime(os.path.join(run, name), (t, t))
    trash = os.path.join(folder, "Exp", "260102", "Trash", "999999")
    os.makedirs(trash)
    with open(os.path.join(trash, DATAFOLDER_FILE), "w") as f:
        f.write("#\n")
    with open(os.path.join(trash, "inst_params.json"), "w") as f:
        json.dump({"dac_b": {"voltage_1": 5.0, "ramp": 0.01}}, f)
    t = t0 + 60.5 * 600
    os.utime(os.path.join(trash, DATAFOLDER_FILE), (t, t))
    return expected


def section_tracker(s):
    """Tools -> Instrument tracker: tracking, the record, the rows and the runs strip, Skip Trash,
    the sidebar (thresholds for one, several or an instrument's parameters; splitting a row),
    hover, the cursor and its panel (changes, runs on either side, stepping, Esc), double-click,
    zoom and the scroll bar, the settings in the config, reopening reads only new runs, both themes."""
    from matplotlib.backend_bases import KeyEvent, MouseEvent
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtGui import QColor
    import acadia_gui.gui.instrument_tracker as itr
    root = os.path.join(s.args.tmp, "tracker_root")
    expected = _tracker_root(root)
    win = s.browser(root=root)
    win.apply_theme("default")                  # an earlier section may have saved another theme
    win.menu_bar.instrument_tracker_action.trigger()
    tracker = win.instrument_tracker
    s.check("Tools -> Instrument tracker opens the window", tracker is not None and tracker.isVisible())
    s.pump_until(lambda: tracker.full_record is not None, 30)
    s.pump(0.3)
    rec = tracker.full_record
    s.check("all runs tracked, Trash included", rec is not None and len(rec["runs"]) == 121,
            tracker.status.text())
    s.check("record written in the root", os.path.isfile(os.path.join(root, itr.RECORD_NAME)))
    counts = {n: len(e) for n, e in tracker.events.items()}
    s.check("a row per instrument and the runs strip", tracker.rows == [("dac_b", None), ("src_a", None), None],
            str(tracker.rows))
    s.check("changes found: set changes and the lock, not the jitter; the Trash run lacks the source",
            counts.get("src_a") == expected["src_a"] + 2, str(counts))
    s.check("the DAC: its two moves plus the stray Trash value", counts.get("dac_b") == expected["dac_b"] + 2,
            str(counts))
    tracker.trash_box.setChecked(True)
    s.pump(0.3)
    counts = {n: len(e) for n, e in tracker.events.items()}
    s.check("Skip Trash hides the Trash run's changes", counts == expected
            and len(tracker.record["runs"]) == 120, str(counts))

    # sidebar: the tree, thresholds for one / several / an instrument, Default
    tree = tracker.tree
    items = {tree.topLevelItem(i).data(0, Qt.UserRole): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
    children = {name: [item.child(j).data(0, Qt.UserRole) for j in range(item.childCount())]
                for name, item in items.items()}
    s.check("tree: instruments with their change counts", list(items) == ["dac_b", "src_a"]
            and items["src_a"].text(1) == str(expected["src_a"]), str({n: i.text(1) for n, i in items.items()}))
    s.check("tree: the parameters that change or jitter, not the constant ones",
            children == {"dac_b": ["dac_b.voltage_1"],
                         "src_a": ["src_a.frequency", "src_a.reference_detected", "src_a.temperature"]},
            str(children))

    def select(*keys):
        tree.clearSelection()
        for item in items.values():
            item.setSelected(item.data(0, Qt.UserRole) in keys)
            for j in range(item.childCount()):
                item.child(j).setSelected(item.child(j).data(0, Qt.UserRole) in keys)
        s.pump(0.1)

    def refresh():
        items.clear()
        items.update({tree.topLevelItem(i).data(0, Qt.UserRole): tree.topLevelItem(i)
                      for i in range(tree.topLevelItemCount())})

    select("dac_b.voltage_1")
    auto = tracker.info["dac_b.voltage_1"]
    s.check("one parameter selected: its default threshold, k x jitter",
            tracker.threshold_edit.text() == itr._number(tracker.k * auto["jitter"])
            and "dac_b.voltage_1" in tracker.selection_label.text(), tracker.threshold_edit.text())
    tracker.threshold_edit.setText("1")
    tracker.set_button.click()
    s.pump(0.3)
    refresh()
    s.check("a threshold set by hand hides the smaller moves",
            all(c[0] == "dac_b." + itr.PRESENCE for e in tracker.events["dac_b"] for c in e["changes"])
            and tracker.thresholds == {"dac_b.voltage_1": 1.0} and items["dac_b"].child(0).font(0).bold()
            and items["dac_b"].child(0).isSelected(), str(tracker.thresholds))
    select("dac_b.voltage_1", "src_a.temperature")
    s.check("several selected: the editor says how many", "2 parameters" in tracker.selection_label.text()
            and tracker.threshold_edit.text() == "", tracker.selection_label.text())
    tracker.threshold_edit.setText("0.5")
    tracker.threshold_edit.returnPressed.emit()
    s.pump(0.3)
    s.check("one value set for all the selected", tracker.thresholds == {"dac_b.voltage_1": 0.5,
                                                                         "src_a.temperature": 0.5},
            str(tracker.thresholds))
    tracker.default_button.click()
    s.pump(0.3)
    s.check("Default puts them back", tracker.thresholds == {}
            and len(tracker.events.get("dac_b", [])) == expected["dac_b"], str(tracker.thresholds))
    refresh()
    select("src_a")
    s.check("an instrument selected stands for its parameters",
            tracker._selected_parameters() == children["src_a"], str(tracker._selected_parameters()))
    tracker.filter_box.setText("temp")
    s.pump(0.6)                                 # applied a moment after the last key
    s.check("the filter also narrows the timeline", tracker.rows == [("src_a", None), None], str(tracker.rows))
    s.check("filter: only the matching parameters", items["dac_b"].isHidden()
            and [items["src_a"].child(j).isHidden() for j in range(3)] == [True, True, False])
    tracker.filter_box.clear()
    s.pump(0.6)
    # split a row into its parameters
    items["src_a"].setExpanded(True)
    s.pump(0.2)
    s.check("the tree has a 'changes' column", tree.headerItem().text(1) == "changes")
    s.check("expanding an instrument splits its row into its changed parameters",
            tracker.rows == [("dac_b", None), ("src_a", None), ("src_a", "src_a.frequency"),
                             ("src_a", "src_a.reference_detected"), ("src_a", "src_a.temperature"), None],
            str(tracker.rows))
    spans = tracker._row_span
    rect = tree.visualItemRect(items["src_a"].child(0))
    top = rect.top() + tree.verticalScrollBar().value()
    s.check("a timeline row sits beside its name", spans[("src_a", "src_a.frequency")] == (top, top + rect.height()),
            f"{spans[('src_a', 'src_a.frequency')]} vs {(top, top + rect.height())}")
    # no line between the names and the timeline: the row rules run across, on the same pixel rows
    img = tracker.grab().toImage()
    bg = QColor(tracker._theme()["bg"]).name()
    viewport, handle = tree.viewport(), tracker.upper.handle(1)
    rows_top = viewport.mapTo(tracker, QPoint(0, 0)).y()

    def ruled(widget, x):
        """The rows where a column of the widget is not the background, from the names' first row."""
        o = widget.mapTo(tracker, QPoint(0, 0))
        return [y - rows_top for y in range(rows_top, rows_top + viewport.height())
                if img.pixelColor(o.x() + x, y).name() != bg]
    names_rules, plot_rules = ruled(viewport, viewport.width() - 2), ruled(tracker.canvas, 1)
    s.check("the row rules of the names and the timeline line up", names_rules and names_rules == plot_rules,
            f"{names_rules} vs {plot_rules}")
    s.check("no line between the names and the timeline", ruled(handle, handle.width() // 2) == names_rules,
            str(ruled(handle, handle.width() // 2)))
    footer = tracker.tree_footer.mapTo(tracker, QPoint(0, 0))
    under_runs = footer.y() + itr.STRIP_PX - 1
    line = [img.pixelColor(w.mapTo(tracker, QPoint(0, 0)).x() + x, y).name()
            for w, x in ((tracker.tree_footer, tracker.tree_footer.width() // 2), (handle, handle.width() // 2),
                         (tracker.canvas, 1)) for y in (under_runs - 1, under_runs)]
    rule = QColor(tracker._theme()["rule"]).name()
    s.check("a line under the runs row, across names and timeline", line == [bg, rule] * 3, str(line))
    tree_bar, bar = tree.verticalScrollBar(), tracker.row_scrollbar
    s.check("the rows' scroll bar is left of the names", tree.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
            and bar.mapTo(tracker, QPoint(0, 0)).x() < tree.mapTo(tracker, QPoint(0, 0)).x())
    height = tracker.height()
    tracker.resize(tracker.width(), 360)        # too short for the rows: the bar shows
    s.pump(0.3)
    shown = bar.isVisible() and bar.maximum() == tree_bar.maximum() > 0
    bar.setValue(bar.maximum())
    s.pump(0.1)
    s.check("the rows' scroll bar scrolls the names and the timeline", shown and tree_bar.value() == bar.maximum()
            and tracker.ax.get_ylim()[1] == tree_bar.value(), f"{shown} {bar.maximum()} {tree_bar.value()}")
    tree_bar.setValue(0)
    s.check("and follows the names", bar.value() == 0)
    tracker.resize(tracker.width(), height)
    s.pump(0.3)
    s.check("the rows' scroll bar hides when everything fits", not bar.isVisible())
    # a parameter's box: unticked, its changes are left out
    item = items["src_a"].child(0)
    s.check("parameters have boxes too", item.data(0, Qt.UserRole) == "src_a.frequency"
            and item.checkState(0) == Qt.Checked)
    item.setCheckState(0, Qt.Unchecked)
    s.pump(0.3)
    refresh()
    s.check("unticking a parameter leaves its changes out", len(tracker.events["src_a"]) == 2
            and not len(tracker._art[("src_a", "src_a.frequency")]["x"]) and items["src_a"].text(1) == "2"
            and tracker.hidden_parameters == {"src_a.frequency"}, str(tracker.rows))
    items["src_a"].child(0).setCheckState(0, Qt.Checked)
    s.pump(0.3)
    refresh()
    s.check("ticking it again brings them back", len(tracker.events["src_a"]) == expected["src_a"]
            and not tracker.hidden_parameters)
    # an instrument missing from some runs: hatched, and its going and coming back are changes
    s.check("an instrument missing from runs is found", tracker.missing == {"dac_b": [[105, 112]]},
            str(tracker.missing))
    gone = [e["run"] for e in tracker.events["dac_b"] if any(c[0].endswith(itr.PRESENCE) for c in e["changes"])]
    s.check("going missing and coming back are changes", gone == [105, 112], str(gone))
    hatched = [c for c in tracker.ax.collections if c.get_hatch()]
    s.check("its row is hatched where it is missing", len(hatched) == 1)
    mid = (tracker._times[107] / 86400.0, tracker._row_y[("dac_b", None)])
    gx, gy = tracker.ax.transData.transform(mid)
    tip = tracker._gap_text(MouseEvent("motion_notify_event", tracker.canvas, gx, gy))
    s.check("hovering the hatching says which runs", "not saved in 7 runs" in tip, tip)
    table = lambda: [[tracker.changes.item(r, c).text() for c in range(1, 5)]
                     for r in range(tracker.changes.rowCount())]
    tracker.set_cursor(run=105)
    last = itr._value_text(tracker._value_at("dac_b.voltage_1", 104))
    s.check("going missing: each parameter, from its last value", table() == [
        ["ramp", "0.01", "missing", "▼ missing"], ["voltage_1", last, "missing", "▼ missing"]], str(table()))
    tracker.set_cursor(run=112)
    now = tracker._value_at("dac_b.voltage_1", 112)
    moved = now - tracker._value_at("dac_b.voltage_1", 111)
    s.check("coming back: each parameter, to its value then", table() == [
        ["ramp", "missing", "0.01", "▲ back"],
        ["voltage_1", "missing", itr._value_text(now), "▲ back" + (f" · {moved:+.3g}" if moved else "")]],
            str(table()))
    tracker.set_cursor()
    from matplotlib.colors import to_hex
    bands = {to_hex(p.get_facecolor(), keep_alpha=False) for p in tracker.ax.patches}
    s.check("the parameter rows sit on a band of their instrument's colour", bands == {tracker.colors["src_a"]},
            str(bands))

    # hover, the cursor and its panel
    canvas, ax = tracker.canvas, tracker.ax

    def px(row, run):
        canvas.draw()
        return ax.transData.transform((tracker._times[run] / 86400.0, tracker._row_y[row]))

    def mouse(kind, x, y, **kw):
        canvas.callbacks.process(kind, MouseEvent(kind, canvas, x, y, **kw))
        s.pump(0.05)

    event = tracker.events["src_a"][0]
    run = event["run"]
    x, y = px(("src_a", None), run)
    mouse("motion_notify_event", x, y)
    hit = tracker._hit(MouseEvent("motion_notify_event", canvas, x, y))
    s.check("hover finds the bar", hit is not None and hit[2]["run"] == run and "frequency" in tracker._hover_text(hit),
            str(hit and tracker._hover_text(hit)))
    mouse("button_press_event", x, y, button=1)
    mouse("button_release_event", x, y, button=1)
    table = tracker.changes
    rows = [[table.item(r, c).text() for c in range(5)] for r in range(table.rowCount())]
    s.check("a click puts the cursor on that run", tracker._cursor_run() == run and ax.lines, str(tracker.cursor))
    s.check("the panel lists the change, before -> after, with its direction",
            rows == [["src_a", "frequency", "5000000000", "5100000000", "▲ +1e+08"]], str(rows))
    s.check("the panel names the runs on either side", tracker._run_targets == [run - 1, run]
            and rec["runs"][run][0].split("/")[-1] in tracker.run_labels[1].text(), str(tracker._run_targets))
    titles = [label.text() for label in tracker.run_titles]
    tracker.set_cursor(t=(tracker._times[run - 2] + tracker._times[run - 1]) / 2)   # between runs: no change
    s.pump(0.1)
    between = [label.text() for label in tracker.run_titles]
    quiet = next(r for r in range(1, run) if r not in tracker.events_at)
    tracker.set_cursor(run=quiet)
    s.pump(0.1)
    no_change = tracker.run_titles[1].text()
    tracker.set_cursor(run=run)
    s.pump(0.1)
    s.check("the run is 'Changed at run' on a change, 'No change at run' on a quiet run, "
            "'First run after' between runs",
            titles == ["Last run before", "Changed at run"] and no_change == "No change at run"
            and between == ["Last run before", "First run after"]
            and [label.text() for label in tracker.run_titles] == titles, f"{titles} {no_change} {between}")
    opened = []
    tracker.open_folder = opened.append
    tracker.run_buttons[0].click()
    s.check("Open on the last run before opens it", opened == [os.path.join(root, tracker.record["runs"][run - 1][0])],
            str(opened))
    later = sorted({e["run"] for e in tracker.events["src_a"] + tracker.events["dac_b"] if e["run"] > run})[0]
    tracker.next_button.click()
    s.check("▶ steps to the next change", tracker._cursor_run() == later, str(tracker.cursor))
    canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", canvas, "left"))
    s.check("← steps back", tracker._cursor_run() == run, str(tracker.cursor))
    # an empty spot: the cursor at that time, no changes, the runs around it
    gap = (tracker._times[run + 3] + tracker._times[run + 4]) / 2        # away from the cursor, which drags
    x0, y0 = ax.transData.transform((gap / 86400.0, tracker._row_y[("dac_b", None)]))
    mouse("button_press_event", x0, y0, button=1)
    mouse("button_release_event", x0, y0, button=1)
    s.check("a click on an empty spot: the cursor at that time, between two runs",
            tracker.cursor["rel"] is None and abs(tracker.cursor["time"] - gap) < 60
            and tracker._run_targets == [run + 3, run + 4] and table.rowCount() == 0
            and "no change" in tracker.cursor_label.text(), str(tracker.cursor))
    canvas.callbacks.process("key_press_event", KeyEvent("key_press_event", canvas, "escape"))
    s.check("Esc removes the cursor", tracker.cursor is None and table.rowCount() == 0)
    # the hover line follows the mouse; the cursor can be dragged and snaps to the changes it passes
    mouse("motion_notify_event", x0, y0)
    s.check("a faint line follows the mouse", tracker._hover_line.get_visible()
            and abs(tracker._hover_line.get_xdata()[0] - gap / 86400.0) < 1e-3)
    tracker.set_cursor(t=gap)
    cx = ax.transData.transform((gap / 86400.0, 0))[0]
    mouse("button_press_event", cx, y0, button=1)
    tx, _ty = px(("src_a", None), later)
    mouse("motion_notify_event", tx + 2, y0)
    s.check("dragging the cursor onto a change snaps to its run", tracker._cursor_run() == later, str(tracker.cursor))
    mouse("button_release_event", tx + 2, y0, button=1)
    s.check("after the drag the panel shows that run", tracker._run_targets == [later - 1, later]
            and tracker.changes.rowCount() >= 1, str(tracker._run_targets))
    tracker.set_cursor()
    mouse("button_press_event", x, y, button=1, dblclick=True)
    s.check("double-click on a bar opens that run", opened[-1] == os.path.join(root, tracker.record["runs"][run][0]),
            str(opened))
    # zoom and the scroll bar
    mouse("scroll_event", x, y, button="up")
    mouse("scroll_event", x, y, button="up")
    bar = tracker.scrollbar
    zoomed = tracker._view
    bar.setValue(bar.maximum())
    s.pump(0.1)
    full = tracker._full_range()
    s.check("zoomed in, the scroll bar moves the view to the end", bar.isEnabled() and tracker.fit_button.isVisible()
            and abs(tracker._view[1] - full[1]) < 1e-6 and abs((tracker._view[1] - tracker._view[0])
                                                                - (zoomed[1] - zoomed[0])) < 1e-6, str(tracker._view))
    tracker.fit_button.click()
    s.check("Reset zoom shows everything and then hides", tracker._view == full and not bar.isEnabled()
            and tracker.fit_button.isHidden())

    # settings kept: k, a threshold, hidden instrument, split, Skip Trash
    tracker.thresholds["src_a.temperature"] = 0.5
    tracker.k_box.setValue(8.0)
    refresh()
    items["dac_b"].setCheckState(0, Qt.Unchecked)
    s.pump(0.3)
    from acadia_gui.utils import load_user_config
    saved = load_user_config().get(itr.CONFIG_KEY, {})
    s.check("settings saved in the GUI config", saved.get("k") == 8.0 and saved.get("hidden") == ["dac_b"]
            and saved.get("skip_trash") is True and saved.get("thresholds") == {"src_a.temperature": 0.5}
            and saved.get("split") == ["src_a"], str(saved))
    s.check("a hidden instrument keeps its row, empty", ("dac_b", None) in tracker.rows
            and not len(tracker._art[("dac_b", None)]["x"]), str(tracker.rows))
    # reopen: from the record, only the new run is read
    new = os.path.join(root, "Exp", "260102", "999000")
    os.makedirs(new)
    with open(os.path.join(new, DATAFOLDER_FILE), "w") as f:
        f.write("from runtime import DemoRuntime\n")
    with open(os.path.join(new, "inst_params.json"), "w") as f:
        json.dump({"src_a": {"frequency": 6e9, "temperature": 40.0, "reference_detected": 1, "power": 1.0}}, f)
    tracker.close()
    s.pump(0.2)
    steps = []
    real_track = itr.track
    itr.track = lambda *a, **k: real_track(*a, **{**k, "progress": (lambda t: (steps.append(t), k["progress"](t)))})
    try:
        win.menu_bar.instrument_tracker_action.trigger()
        tracker = win.instrument_tracker
        s.pump_until(lambda: tracker.full_record is not None, 30)
        s.pump(0.3)
    finally:
        itr.track = real_track
    s.check("reopening reads only the new run", "Reading 1 new runs" in steps and len(tracker.full_record["runs"]) == 122,
            str(steps))
    s.check("the settings come back", tracker.k == 8.0 and tracker.hidden == {"dac_b"} and tracker.trash_box.isChecked()
            and tracker.thresholds == {"src_a.temperature": 0.5} and tracker.split == {"src_a"}
            and tracker.tree.topLevelItem(1).isExpanded())
    tracker._step(-1)
    tracker.set_cursor(run=len(tracker.record["runs"]) - 1)
    s.check("the panel names the runtime from run.py", "DemoRuntime" in tracker.run_labels[1].text(),
            tracker.run_labels[1].text())
    s.check("instrument colours: the fixed set, in order", tracker.colors == {"dac_b": itr.SERIES_LIGHT[0],
                                                                              "src_a": itr.SERIES_LIGHT[1]},
            f"{tracker.colors} theme {tracker.theme_name!r}")
    th = tracker._theme()       # a pointer cannot be moved reliably here (Wayland): check the rules
    qss = tracker.styleSheet()
    s.check("buttons have a frame and shade under the mouse", "QToolButton#framed:hover, QPushButton:hover" in qss
            and f"background: {th['hover']}" in qss and th["hover"] != th["bg"]
            and tracker.prev_button.objectName() == "framed")
    s.check("Re-track is a text button", tracker.retrack_button.text() == "Re-track")
    tracker.retrack_button.click()
    s.pump_until(lambda: tracker.retrack_button.isEnabled(), 30)
    s.check("Re-track reads every run again", "122 runs" in tracker.status.text(), tracker.status.text())
    win.apply_theme("dark")
    s.pump(0.5)
    s.check("the window follows the dark theme by itself", tracker.canvas.figure.get_facecolor() != (1, 1, 1, 1)
            and tracker.colors["dac_b"] == itr.SERIES_DARK[0], str(tracker.colors))
    import matplotlib
    matplotlib.rcParams["axes.grid"] = True     # as a dark plot style elsewhere in the app may leave it
    tracker._build_plot()
    tracker.canvas.draw()
    grid = [l for a in (tracker.ax, tracker.strip_ax) for l in a.get_xgridlines() + a.get_ygridlines() if l.get_visible()]
    matplotlib.rcParams["axes.grid"] = False
    s.check("no grid on the timeline, whatever the app's matplotlib style", not grid, f"{len(grid)} grid lines")
    win.apply_theme("default")
    s.pump(0.3)
    tracker.close()
    s.close(win)


def section_robustness(s):
    """Each case in a child process: the browser must survive it, or close promptly."""
    for case in ROBUSTNESS_CASES:
        tmp = tempfile.mkdtemp(prefix=f"{case}_", dir=s.args.tmp)
        cmd = [sys.executable, os.path.abspath(__file__), "--case", case, "--tmp", tmp,
               "--runs", s.fx.runs[0]]
        t0 = time.time()
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            text, code = out.stdout + out.stderr, out.returncode
        except subprocess.TimeoutExpired as exc:
            text, code = str(exc.stdout or "") + "\nTIMEOUT", -1
        took = time.time() - t0
        ok = code == 0 and "CASE_OK" in text
        detail = next((l for l in text.splitlines() if l.startswith("CASE_")), text.strip()[-200:])
        s.check(f"{case}", ok, f"exit {code}, {took:.1f} s: {detail}")


def run_case(case, tmp, run):
    """The robustness cases, run in a child process (see section_robustness)."""
    isolate(tmp)
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    from acadia_gui.utils import update_user_config
    update_user_config(auto_jump_to_newest=case in ("close_autojump", "stuck_exit",
                                                    "monitor_recovery", "monitor_restarts"))
    from acadia_gui.gui import DataBrowser
    from acadia_gui.gui import data_folder_view as dfv
    root = os.path.join(tmp, "root")
    day = os.path.join(root, "Exp", "260101")
    os.makedirs(day)
    copy = os.path.join(day, "120000")
    shutil.copytree(run, copy, ignore=shutil.ignore_patterns("__pycache__", ".stop"))

    def pump(seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(0.003)

    if case == "close_during_jump":
        real_scandir = os.scandir

        class StuckOS:
            def __getattr__(self, name):
                return getattr(os, name)

            def scandir(self, path):
                if threading.current_thread().name in ("acadia-folder-scan", "acadia-newest-folder"):
                    time.sleep(30)                    # a listing that never comes back in time
                return real_scandir(path)
        dfv.os = StuckOS()
    mode = {"v": "ok"}
    if case in ("stuck_exit", "monitor_recovery", "monitor_restarts"):
        real_stat = os.stat

        class FlakyOS:
            def __getattr__(self, name):
                return getattr(os, name)

            def stat(self, path, *a, **k):
                if threading.current_thread().name == "acadia-folder-monitor":
                    if mode["v"] == "stall":
                        time.sleep(20)
                        raise OSError(5, "Input/output error", path)
                    if mode["v"] == "eio":
                        raise OSError(5, "Input/output error", path)
                return real_stat(path, *a, **k)
        dfv.os = FlakyOS()

    def monitors():
        return [t for t in threading.enumerate() if t.name == "acadia-folder-monitor" and t.is_alive()]

    win = DataBrowser(root, None)
    win.show()
    app.aboutToQuit.connect(win.shutdown)
    pump(1.0)
    ft = win.folder_tree
    if case == "slot_exc":
        QTimer.singleShot(10, lambda: 1 / 0)
        pump(0.5)
        print("CASE_OK survived an exception in a Qt slot", flush=True)
        os._exit(0)
    if case == "deleted_folder":
        ft.focus_path(copy)
        pump(1.0)
        shutil.rmtree(copy)
        pump(2.5)
        print("CASE_OK survived the viewed folder being deleted", flush=True)
        os._exit(0)
    if case == "close_autojump":
        QTimer.singleShot(100, win.close)
        code = app.exec_()
        print(f"CASE_OK app.exec_ returned {code}", flush=True)
        sys.exit(code)
    if case == "close_during_jump":
        ft.select_most_recent_folder()
        pump(1.0)
        t0 = time.perf_counter()
        win.close()
        app.quit()
        app.processEvents()
        took = time.perf_counter() - t0
        print(f"CASE_{'OK' if took < 2 else 'FAIL'} close took {took:.2f} s with the walk stuck", flush=True)
        sys.exit(0)
    if case == "stuck_exit":
        mode["v"] = "stall"
        pump(1.0)
        t0 = time.perf_counter()
        win.close()
        app.quit()
        app.processEvents()
        took = time.perf_counter() - t0
        print(f"CASE_{'OK' if took < 2 else 'FAIL'} close took {took:.2f} s with the monitor stuck", flush=True)
        sys.exit(0)                    # the normal interpreter exit, with the monitor still stuck
    if case == "monitor_recovery":
        seen = []
        real_new = ft.handle_new_datafolder
        ft.handle_new_datafolder = lambda p: (seen.append(p), real_new(p))
        ft._pause_monitoring()
        ft.start_monitoring()
        pump(1.0)
        mode["v"] = "eio"
        pump(2.0)
        alive = len(monitors()) == 1
        mode["v"] = "ok"
        pump(1.0)
        new = os.path.join(day, "130000")
        os.makedirs(new)
        open(os.path.join(new, DATAFOLDER_FILE), "w").close()
        pump(3.0)
        ok = alive and new in seen
        print(f"CASE_{'OK' if ok else 'FAIL'} monitor alive after I/O errors: {alive}; "
              f"new run found after: {new in seen}", flush=True)
        win.close()
        pump(0.3)
        os._exit(0)
    if case == "monitor_restarts":
        for _ in range(3):
            ft.stop_monitoring()
            pump(0.2)
            ft.start_monitoring()
            pump(0.3)
        ft.set_root_path(os.path.join(root, "Exp"))
        pump(0.5)
        ft.set_root_path(root)
        pump(0.5)
        n = len(monitors())
        print(f"CASE_{'OK' if n == 1 else 'FAIL'} {n} monitor thread(s) after toggles and root changes",
              flush=True)
        win.close()
        pump(0.3)
        os._exit(0)
    print(f"CASE_FAIL unknown case {case}", flush=True)
    os._exit(1)


# ---------------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", help="data root to pick a few recent runs from")
    ap.add_argument("--runs", nargs="*", default=[], help="run folders to use (two or more)")
    ap.add_argument("--only", default="", help="comma-separated sections: " + ", ".join(SECTIONS))
    ap.add_argument("--click-budget", type=float, default=1.0,
                    help="longest acceptable GUI stall while a folder opens (s)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    ap.add_argument("--case", help=argparse.SUPPRESS)        # a robustness case (child process)
    ap.add_argument("--tmp", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.case:
        return run_case(args.case, args.tmp, args.runs[0])

    runs = [os.path.abspath(r) for r in args.runs] or (
        pick_runs(os.path.expanduser(args.data)) if args.data else [])
    if (args.runs or args.data) and len(runs) < 2:
        print("Need at least two run folders (or neither --runs nor --data, for demo runs).")
        return 2
    only = [x.strip() for x in args.only.split(",") if x.strip()] or list(SECTIONS)
    unknown = sorted(set(only) - set(SECTIONS))
    if unknown:
        print(f"Unknown section(s): {', '.join(unknown)}. Sections: {', '.join(SECTIONS)}")
        return 2
    args.tmp = tempfile.mkdtemp(prefix="acadia_gui_smoke_")
    isolate(args.tmp)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    try:
        demo = not runs
        if demo:
            runs = make_demo_runs(os.path.join(args.tmp, "demo_runs"))
        fixture = Fixture(args.tmp, runs)
        fixture.demo = demo
        print(f"fixture: {len(runs)} {'demo' if demo else 'given'} runs copied to {fixture.day} "
              f"(QT_QPA_PLATFORM={os.environ['QT_QPA_PLATFORM']})", flush=True)
        from PyQt5.QtWidgets import QApplication
        app = QApplication(sys.argv)
        smoke = Smoke(app, fixture, args)
        for name in SECTIONS:
            if name not in only:
                continue
            smoke.section = name
            print(f"\n== {name}", flush=True)
            t0 = time.perf_counter()
            try:
                globals()[f"section_{name}"](smoke)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                smoke.check("section ran to the end", False, f"{type(exc).__name__}: {exc}")
                for widget in app.topLevelWidgets():
                    widget.close()
                smoke.pump(0.3)
            print(f"   ({time.perf_counter() - t0:.0f} s)", flush=True)
        failed = [(sec, name) for sec, name, ok in smoke.results if not ok]
        print(f"\n{len(smoke.results) - len(failed)}/{len(smoke.results)} checks passed", flush=True)
        for sec, name in failed:
            print(f"   FAIL [{sec}] {name}")
        return 0 if not failed else 1
    finally:
        if args.keep:
            print(f"kept {args.tmp}")
        else:
            shutil.rmtree(args.tmp, ignore_errors=True)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code or 0)          # do not wait for background threads (a stuck share, a server)
