import numpy as np
from matplotlib import pyplot as plt

from acadia_gui.helpers import to_local_path
from acadia_qmsmt.helpers import load_runtime_from_data_dir

data_path = r"/home/chao/Data/test/ReadoutSpec2/250524_015352"

rt = load_runtime_from_data_dir(to_local_path(data_path))

rt.process_current_data()
rt.plot_data()
# plt.show()

# from acadia_qmsmt.analysis.fitting import Lorentzian
# fit = Lorentzian(rt.frequencies, np.abs(rt.avg_iq), method="lm")
# # Lorentzian.guess(rt.frequencies, np.abs(rt.avg_iq))
# # rt.plot_data()
# fit.plot()
# ------------ manually killing a running runtime from a different instance -------------------------
# import os
# rt.login = f"root@10.66.3.214"
# rt._ssh_options = ["-i " + os.path.expanduser("~/.ssh/id_acadia"), "-o StrictHostKeyChecking=no"]
# rt.kill()