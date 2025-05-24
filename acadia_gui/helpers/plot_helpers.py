import warnings

warnings.warn("This module is deprecated, use functions in `acadia_qmsmt` instead", DeprecationWarning)

# forwarding imports to the new modules
from acadia_qmsmt.helpers.annotation import get_registered_plot_methods, get_registered_button_methods, get_data_process_method, get_registered_methods
from acadia_qmsmt.helpers.annotation import PLOT_NAME_TAG, BUTTON_NAME_TAG, DISABLE_TAG, AXS_SHAPE_TAG, DATA_PROCESS_TAG

from acadia_qmsmt.plotting import save_registered_plots, prepare_plot_axes as prepare_axes
