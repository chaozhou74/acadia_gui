from setuptools import setup, find_packages

setup(
      name='acadia_gui',
      version='0.0.1',
      packages=find_packages(),
      entry_points={
            'console_scripts': [
                  'acadia_gui = acadia_gui.apps:acadia_gui_cli',
            ]
      },
      install_requires=[
        'pyqt5',
        'psutil', # for overall memory usage monitoring
        'pympler' # python object memory usage tracking
        'ruamel.yaml'
    ]
)