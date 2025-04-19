from matplotlib import pyplot as plt

from acadia_gui.helpers import to_local_path
from acadia_qmsmt.helpers import load_runtime_from_data_dir

data_path = r"\\wsl.localhost\Ubuntu\home\chao\Data\test_gui\AmpSweep\new_annoatation\250416_012401\\"

rt = load_runtime_from_data_dir(to_local_path(data_path))

rt.process_current_data()
rt.plot_data_iq()
plt.show()


# ------------ manually killing a running runtime from a different instance -------------------------
# import os
# rt.login = f"root@10.66.3.214"
# rt._ssh_options = ["-i " + os.path.expanduser("~/.ssh/id_acadia"), "-o StrictHostKeyChecking=no"]
# rt.kill()