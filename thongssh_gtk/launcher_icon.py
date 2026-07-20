import sys
import os
import re
import logging
from pathlib import Path

from gi.repository import GLib

from .constants import APP_ID

_SYSTEM_DESKTOP_DIRS = ("/usr/share/applications", "/usr/local/share/applications")
_ICON_LINE_RE = re.compile(r"^Icon=.*$", re.MULTILINE)


def _find_system_desktop_file():
    for base in _SYSTEM_DESKTOP_DIRS:
        path = Path(base) / f"{APP_ID}.desktop"
        if path.exists():
            return path
    return None


def apply_launcher_icon(icon_stem):
    """Update the user-local .desktop override so GNOME Shell (Wayland) picks
    up the chosen app icon for the dock/taskbar, which it reads from the
    .desktop file's Icon= key rather than the window itself."""
    if sys.platform in ("darwin", "win32"):
        return

    try:
        user_desktop = Path(GLib.get_user_data_dir()) / "applications" / f"{APP_ID}.desktop"

        if icon_stem != "thongssh_orig":
            if user_desktop.exists():
                user_desktop.unlink()
            return

        system_desktop = _find_system_desktop_file()
        if system_desktop is None:
            logging.debug("Launcher icon: no system desktop file found (running from source); skipping override.")
            return

        content = system_desktop.read_text(encoding="utf-8")
        new_content = _ICON_LINE_RE.sub(f"Icon={APP_ID}.orig", content, count=1)

        user_desktop.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = user_desktop.with_name(user_desktop.name + ".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(tmp_path, user_desktop)
    except Exception as e:
        logging.warning(f"Launcher icon: failed to update desktop override: {e}")
