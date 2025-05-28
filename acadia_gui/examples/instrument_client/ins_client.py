import pathlib
from instrumentserver.client import ClientStation


current_path = str(pathlib.Path(__file__).parent)
# for neo 1, the Windows computer
config_path = current_path + "/instruments.yaml"  # path to the initial instrument configuration file
param_path = current_path + "/ins_params.json"  # path to the instrument parameter saving file
host = "10.66.xxx.xxx" # ip or hostname of the instrument server
host_port = 5560
timeout = 600

def make_client_station():
    # create instrument clients
    cli_station = ClientStation(host=host, port=host_port,
                                init_instruments=config_path, param_path=param_path, timeout=timeout)
    return cli_station

