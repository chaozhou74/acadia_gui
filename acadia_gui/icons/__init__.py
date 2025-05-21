import os
ICON_PATH = os.path.dirname(__file__)

def get_icon(name: str):
    return os.path.join(ICON_PATH, name)