"""End-to-end smoke test of the Acadia Data Browser: every feature once, crash repros, stall budgets.

Runs headless (offscreen Qt) against COPIES of real data folders in a temp dir, with a temporary
HOME, so it never touches your config, your clipboard or the data share. No pytest needed:

    python tests/gui_smoke_test.py --data ~/data/<user>/<cooldown>          # picks a few runs
    python tests/gui_smoke_test.py --runs <run_dir> <run_dir> ...            # explicit runs

Exit code 0 = all checks passed. Each check prints PASS/FAIL with the measured UI stall.
"""
import os
import sys
import time
import shutil
import argparse
import tempfile
import subprocess
import textwrap

# ---- isolation BEFORE importing acadia_gui (its config path is fixed at import time) ----------
_TMP = tempfile.mkdtemp(prefix="acadia_gui_smoke_")
os.environ["HOME"] = os.path.join(_TMP, "home")
os.makedirs(os.environ["HOME"])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MPLCONFIGDIR"] = os.path.join(_TMP, "mpl")
sys.dont_write_bytecode = True

import logging                                                         # noqa: E402
from PyQt5.QtWidgets import QApplication, QComboBox, QMessageBox      # noqa: E402
from PyQt5.QtCore import QTimer, Qt                                    # noqa: E402
from PyQt5.QtTest import QTest                                         # noqa: E402

DATAFOLDER_FILE = "run.py"
MAX_RUN_MB = 60


def pick_runs(data_root, n=4):
    """Newest data folders under `data_root` (skipping Trash and very large runs)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(data_root):
        dirnames[:] = [d for d in dirnames if d != "Trash"]
        if DATAFOLDER_FILE in filenames:
            dirnames[:] = []
            size = sum(os.path.getsize(os.path.join(dirpath, f)) for f in filenames
                       if os.path.isfile(os.path.join(dirpath, f)))
            if size < MAX_RUN_MB * 1e6:
                found.append((os.path.getmtime(os.path.join(dirpath, DATAFOLDER_FILE)), dirpath))
    found.sort(reverse=True)
    return [p for _, p in found[:n]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="data root to pick a few recent runs from")
    ap.add_argument("--runs", nargs="*", default=[], help="explicit run folders to use")
    ap.add_argument("--click-budget", type=float, default=1.0, help="max UI stall per folder click (s)")
    args = ap.parse_args()
    runs = list(args.runs) or (pick_runs(os.path.expanduser(args.data)) if args.data else [])
    if len(runs) < 2:
        print("Need at least 2 run folders (use --data or --runs).")
        return 2

    # ---- fixture tree: copies of the runs, laid out like real data ----------------------------
    tree = os.path.join(_TMP, "data")
    day = os.path.join(tree, "Exp", "q1", "260101")
    os.makedirs(day)
    copies = []
    for i, r in enumerate(runs):
        dst = os.path.join(day, f"12{i:02d}00")
        shutil.copytree(r, dst, ignore=shutil.ignore_patterns("__pycache__", ".stop"))
        copies.append(dst)
    print(f"fixture: {len(copies)} runs copied to {day}")

    import acadia_gui.gui.live_plot_widget as lpw
    from acadia_gui.utils import load_user_config, update_user_config
    app = QApplication(sys.argv)
    from acadia_gui.gui import DataBrowser
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)   # confirm "Empty trash"

    snapshots = []
    lpw.copy_image_to_clipboard = lambda image, description="": snapshots.append(image.size())

    update_user_config(theme="default.css", scale_factor="1.0", auto_jump_to_newest=False, keep_me="x")
    win = DataBrowser(tree, None)
    win.resize(1600, 1000)
    win.show()
    ft, cv = win.folder_tree, win.center_view
    lp = cv.live.live_plot

    errors, results = [], []

    class ErrorCollector(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage()[:200])
    logging.getLogger().addHandler(ErrorCollector())

    gaps, last = [], [time.perf_counter()]
    def beat():
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now
    hb = QTimer()
    hb.timeout.connect(beat)
    hb.start(5)

    def wait(seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(0.002)

    def check(name, action, verify=lambda: True, budget=1.0, settle=0.5, expect_errors=False):
        gaps.clear(); last[0] = time.perf_counter()
        n_err = len(errors)
        exc = None
        try:
            action()
        except Exception as e:
            exc = f"{type(e).__name__}: {e}"
        wait(settle)
        stall = max(gaps + [0.0])
        try:
            ok_verify = bool(verify())
        except Exception as e:
            ok_verify, exc = False, exc or f"verify: {type(e).__name__}: {e}"
        new_errors = errors[n_err:]
        ok = exc is None and ok_verify and stall <= budget and (expect_errors or not new_errors)
        results.append(ok)
        detail = "" if ok else f"  exc={exc} verify={ok_verify} errors={new_errors[:2]}"
        print(f"{'PASS' if ok else 'FAIL'}  {name:<55s} max UI stall {stall*1000:6.0f} ms (budget {budget*1000:.0f}){detail}",
              flush=True)

    def open_run(path):
        ft.focus_path(path)
        if cv.live.png_paths:
            cv.live.plot_live_mode()

    # ---- browsing -------------------------------------------------------------------------------
    wait(1.5)   # let startup work (import warm-up) finish, as a user would
    for p in copies:
        check(f"open run + Live: {os.path.basename(p)}", lambda p=p: open_run(p),
              verify=lambda: lp.ready and lp.rt is not None, budget=args.click_budget)
    check("cycle every plot of the run", lambda: [lp.plot_selector.setCurrentIndex(i)
                                                   for i in range(lp.plot_selector.count())],
          budget=args.click_budget)

    def change_dropdowns():
        for w in (lp.process_inputs or {}).values():
            if isinstance(w, QComboBox) and w.count() > 1:
                w.setCurrentIndex((w.currentIndex() + 1) % w.count())
    check("change process-kwarg dropdowns (e.g. readout_classifier)", change_dropdowns, budget=args.click_budget)
    check("pause / resume", lambda: (lp.toggle_pause(), lp.toggle_pause()), verify=lambda: not lp.is_paused)
    check("poll interval edit", lambda: (lp.interval_input.setText("1.5"), lp.update_poll_interval()),
          verify=lambda: lp.poll_interval_ms == 1500)
    check("history back / forward", lambda: (ft.go_back(), ft.go_forward()), budget=args.click_budget)

    def key_nav():
        ft.focus_path(copies[0])
        ft.tree.setFocus()
        key = Qt.Key_Up if ft.tree.currentIndex().row() > 0 else Qt.Key_Down
        QTest.keyClick(ft.tree, key)
        wait(0.4)
    check("keyboard arrow loads the folder", key_nav,
          verify=lambda: ft._last_loaded_path not in (None, copies[0]), budget=args.click_budget)
    check("sort / refresh", lambda: (ft.sort_by_mtime(), ft.sort_by_mtime(), ft.refresh_model()))

    def expanded_count():
        n, stack = 0, [ft.tree.rootIndex()]
        while stack:
            idx = stack.pop()
            for r in range(ft.proxy_model.rowCount(idx)):
                c = ft.proxy_model.index(r, 0, idx)
                if ft.tree.isExpanded(c):
                    n += 1
                    stack.append(c)
        return n
    check("collapse-all button closes every expanded folder",
          lambda: (ft.focus_path(copies[0]), wait(0.3), ft.collapse_all_button.click()),
          verify=lambda: expanded_count() == 0)

    # ---- search / most recent / auto-jump (background index) ----------------------------------
    def search(text):
        ft.search_button.setChecked(True)
        ft.toggle_search_box()
        ft.search_box.setText(text)
        ft.apply_search_filter()
    check("search returns immediately", lambda: search(os.path.basename(copies[1])), budget=0.15)
    check("search finds the folder once indexed", lambda: wait(3),
          verify=lambda: copies[1] in ft.search_matches, budget=0.3, settle=0.1)
    check("close search", lambda: (ft.search_button.setChecked(False), ft.toggle_search_box()))
    check("most recent data folder", ft.select_most_recent_folder,
          verify=lambda: ft._last_loaded_path == max(copies, key=lambda p: os.path.getmtime(
              os.path.join(p, DATAFOLDER_FILE))), budget=args.click_budget)

    new_run = os.path.join(day, "235959")
    def auto_jump():
        ft.start_monitoring()
        wait(1.0)
        shutil.copytree(copies[0], new_run, ignore=shutil.ignore_patterns("__pycache__", ".stop"))
        wait(3.0)
    check("auto-jump to a newly created run", auto_jump, verify=lambda: ft._last_loaded_path == new_run,
          budget=args.click_budget, settle=0.2)
    check("auto-jump off", ft.stop_monitoring, verify=lambda: not ft.recent_lock_enabled)

    # ---- snapshot ----------------------------------------------------------------------------
    open_run(copies[0]); wait(0.5)
    check("snapshot (clipboard stubbed)", lp.snapshot_current_plot, verify=lambda: len(snapshots) == 1)
    def snapshot_settings():
        def fill():
            d = lp.snapshot_settings_dialog
            from PyQt5.QtWidgets import QSpinBox, QDoubleSpinBox
            s = d.findChildren(QSpinBox) + d.findChildren(QDoubleSpinBox)
            vals = {}
            for spin in s:
                vals[spin.toolTip()] = spin
            vals["DPI of the original figure before scaling"].setValue(400)
            vals["Width of the original figure before scaling"].setValue(8.0)
            d.accept()
        QTimer.singleShot(100, fill)
        lp.show_snapshot_settings()
    check("snapshot settings dialog applies values", snapshot_settings,
          verify=lambda: lp.snapshot_original_dpi == 400 and lp.snapshot_original_width_inch == 8.0)
    check("snapshot settings kept after changing folder", lambda: open_run(copies[1]),
          verify=lambda: lp.snapshot_original_dpi == 400 and lp.snapshot_original_width_inch == 8.0,
          budget=args.click_budget)

    # ---- views, theme, menus -------------------------------------------------------------------
    if cv.sequence is not None:
        check("SeeQuence view", lambda: (cv.set_mode(1), open_run(copies[0])), budget=2.0)
        # the plot view (hidden while in SeeQuence mode) loads the folder when shown again:
        # saved images if the run has them, else the live plot
        check("back to Plot view loads the folder", lambda: cv.set_mode(0),
              verify=lambda: cv.live.folder_path == copies[0] and (bool(cv.live.png_paths) or lp.ready),
              budget=args.click_budget)
    check("dark theme keeps other settings", lambda: win.menu_bar.set_theme("dark.css"),
          verify=lambda: load_user_config().get("keep_me") == "x" and load_user_config().get("theme") == "dark.css")
    check("default theme", lambda: win.menu_bar.set_theme("default.css"))
    check("memory popup", win.menu_bar.toggle_memory_popup, budget=2.0)
    check("memory popup close", win.menu_bar.toggle_memory_popup)
    check("an exception inside a Qt slot does not abort", lambda: QTimer.singleShot(0, lambda: 1 / 0),
          expect_errors=True)

    # ---- instruments tab with a stub station -------------------------------------------------
    class StubStation:
        def __init__(self): self.calls = []
        def set_parameters(self, d): self.calls.append(sorted(d))
    tab = win.right_tabs.instruments_tab
    stub = StubStation()
    tab.client_station = stub
    def load_instruments():
        tab.load_json(copies[0])
        if tab.tree.topLevelItemCount():
            tab.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
            tab.load_selected_parameters()
    check("instruments: load selected parameters (stub)", load_instruments,
          verify=lambda: tab.tree.topLevelItemCount() == 0 or len(stub.calls) == 1)

    # ---- STOP + trash / restore / empty + external delete (all on the copies) ------------------
    open_run(copies[0]); wait(0.3)
    check("STOP writes .stop", lp.drop_stop_flag, verify=lambda: os.path.exists(os.path.join(copies[0], ".stop")))
    victim = copies[-1]
    check("trash a run", lambda: ft.handle_trash(victim), verify=lambda: not os.path.exists(victim))
    check("restore it", lambda: ft.handle_restore(os.path.join(day, "Trash", os.path.basename(victim))),
          verify=lambda: os.path.isdir(victim))
    check("trash again + empty trash", lambda: (ft.handle_trash(victim),
                                               ft.handle_empty_trash(os.path.join(day, "Trash"))),
          verify=lambda: not os.path.exists(os.path.join(day, "Trash")))
    gone = copies[1]
    check("viewed folder deleted externally (log/plot timers)", lambda: (open_run(gone), wait(0.3),
                                                                        ft.focus_path(day), shutil.rmtree(gone),
                                                                        wait(2.5)))

    # ---- shutdown ----------------------------------------------------------------------------
    win.close()
    app.processEvents()

    # closing with auto-jump ON used to abort ("QThread: Destroyed while thread is still running")
    child = textwrap.dedent(f"""
        import sys; sys.dont_write_bytecode = True
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QTimer
        app = QApplication(sys.argv)
        from acadia_gui.gui import DataBrowser
        from acadia_gui.utils import update_user_config
        update_user_config(auto_jump_to_newest=True)
        w = DataBrowser({tree!r}, None); w.show()
        QTimer.singleShot(1500, w.close)
        sys.exit(app.exec_())
    """)
    rc = subprocess.run([sys.executable, "-c", child], env=dict(os.environ), capture_output=True,
                        text=True, timeout=60).returncode
    ok = rc == 0
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {'close the window with auto-jump on (exit code 0)':<55s} exit code {rc}")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
