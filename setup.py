from distutils.core import setup

setup(
      name='acadia_gui',
      version='0.0.1',
      packages=['acadia_gui'],
      entry_points={
            'console_scripts': [
                  'acadia_gui = acadia_gui.apps:acadia_gui_cli',
            ]
      },
      install_requires=[
        'pyqt5',
        'psutil', # for memory usage monitoring
    ]
)