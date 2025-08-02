import os
from PyQt5.QtGui import QIcon

ICON_PATH = os.path.dirname(__file__)

def get_icon(name: str):
    return QIcon(os.path.join(ICON_PATH, name))