from setuptools import setup, find_packages

setup(
      name='acadia_gui',
      version='0.0.1',
      packages=find_packages(),
      package_data={
          'acadia_gui': ['icons/*.svg', 'themes/*.css'],
      },
      entry_points={
            'console_scripts': [
                  'acadia_gui = acadia_gui.apps:acadia_gui_cli',
            ]
      },
      install_requires=[
        'pyqt5',
        'psutil', # for overall memory usage monitoring
        'pympler', # python object memory usage tracking
        'ruamel.yaml',
        # safe to hard-require: tiny pure-Python pkg, and set_theme() falls back
        # to a built-in dark style if it's ever missing or fails to load.
        'mplcyberpunk',
    ]
)