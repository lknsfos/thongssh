import sys
import os

__version__ = "0.5.2"

APP_ID = "com.example.thongssh"

def resource_path(relative_path):
    """
    Get absolute path to resource, works for dev and for PyInstaller.
    """
    # PyInstaller creates a temp folder and stores path in _MEIPASS
    if hasattr(sys, '_MEIPASS'):
        # When bundled, the 'thongssh_gtk' folder is at the root.
        return os.path.join(sys._MEIPASS, 'thongssh_gtk', relative_path)

    # In development, the path is relative to the thongssh_gtk directory
    return os.path.join(os.path.dirname(__file__), relative_path)

# TreeStore columns: name, type, icon, data object (config/node)
(
    COL_NAME,
    COL_TYPE,
    COL_ICON,
    COL_DATA
) = range(4)