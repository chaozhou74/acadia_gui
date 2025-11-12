import warnings

warnings.warn("This module is deprecated, use functions in `acadia_qmsmt.utils.path_adapter` instead", DeprecationWarning)

from acadia_qmsmt.utils.path_adapter import detect_platform, to_windows_path, to_local_path