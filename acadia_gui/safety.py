"""App-wide safety nets for the data browser."""
import logging
import sys
import traceback

logger = logging.getLogger("acadia_gui")


def install_excepthook():
    """Log unhandled exceptions instead of letting PyQt abort the application.

    Under PyQt5 >= 5.5 an exception escaping any Qt slot or timer callback calls qFatal() and
    aborts the whole browser, unless ``sys.excepthook`` has been replaced. A custom hook already
    installed (IPython's, say) is left alone.
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
