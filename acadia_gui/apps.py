import sys
import argparse
from pathlib import Path
import logging

from PyQt5.QtWidgets import QApplication

from acadia_gui.gui import DataBrowser
from acadia_gui.utils import set_qt_scaling, check_wsl_interop
from acadia_gui.icons import get_icon

logger = logging.getLogger("__name__")
def launch_acadia_gui(root_path:str = None, instrument_station=None, dark_mode=False):
    """
    start the main acadia data browser gui
    :param root_path: path to the root data directory, default to the home directory
    :param instrument_station: A `instrumentserver.ClientStation` instance used to restore instrument
            states from previously saved configurations in the data browser GUI.
    """
    # Check if running in WSL and warn about missing interop setting
    check_wsl_interop()

    if root_path is None:
        root_path = str(Path.home())
    else:
        root_path = str(root_path)
    set_qt_scaling()
    app = QApplication(sys.argv)
    window = DataBrowser(root_path, instrument_station, theme="dark" if dark_mode else None)
    window.setWindowTitle("Acadia Data Browser")
    window.setWindowIcon(get_icon("app_icon.svg"))  # your icon file here
    window.show()
    sys.exit(app.exec_())



def acadia_gui_cli():
    """
    Command-line entry point for launching the Acadia data browser gui.

    Supports optional arguments for specifying the root data directory and
    initializing an instrument client station from a configuration file.

    Arguments:
        -r, --root_path: Optional. Path to the root data directory. Defaults to the user's home directory.
        -s, --station_config: Optional. Path to a JSON file specifying the instrument client station
                              initialization parameters. See `examples/instrument_client/ins_client_params.json`.
        -d, --dark: Optional. When present, enables dark mode at start.

    """
    parser = argparse.ArgumentParser()

    # for a normal data browser gui, that's all that's needed
    parser.add_argument('-r', '--root_path', type=Path, default=None)

    # for starting gui with a client instrument station, a JSON file that contains the station init
    # parameters must be provided. Check `examples/instrument_client/ins_client_params.json` for an example
    parser.add_argument('-s', '--station_config', type=Path, default=None)

    # use dark mode at start
    parser.add_argument('-d', '--dark', action='store_true', help='Enable dark mode')

    args = parser.parse_args()

    if args.root_path is not None and not args.root_path.exists():
        logger.warning(f"Provided root_path does not exist: {args.root_path}. "
                       f"Starting gui with home directory as root data path")
        args.root_path = None

    if args.station_config is not None:
        try:
            import json
            from instrumentserver.client import ClientStation
            with open(args.station_config, "r") as f:
                station_cfg = json.load(f)
            client_station = ClientStation(**station_cfg)
        except Exception as e:
            client_station = None
            logger.warning(f"Error initializing client instrument station: {e}. "
                           f"Starting gui without instrument station.")
    else:
        client_station = None

    launch_acadia_gui(root_path=args.root_path, instrument_station=client_station, dark_mode=args.dark)




