import sys
import os
import re
import logging
from pathlib import Path

from gi.repository import GLib

from .constants import APP_ID

_SYSTEM_DESKTOP_DIRS = ("/usr/share/applications", "/usr/local/share/applications")
_ICON_LINE_RE = re.compile(r"^Icon=.*$", re.MULTILINE)

# Written into every override this function creates, and checked before
# ever deleting one — see the "thongssh_orig" branch below for why: the
# user-local .desktop path this writes to is the EXACT same one a dev
# checkout's install-desktop-entry.sh installs its own (non-override,
# only-copy) launcher to. Without this marker, every app start with the
# default (non-"Original") icon setting was unconditionally deleting
# that dev-checkout launcher out from under the user — it looked
# identical to "our own override, no longer needed" from here, so it
# quietly disappeared a launch or two after being installed, taking the
# dock/taskbar icon with it (the window's own icon stayed fine, since
# that's set directly at runtime, not read from this file).
_OVERRIDE_MARKER = "X-ThongSSH-Launcher-Icon-Override=true"


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
            # Only ever remove a file this function itself wrote (see
            # _OVERRIDE_MARKER) — never a dev checkout's own launcher
            # sitting at this same path, which isn't an "override" of
            # anything and is the only thing making a dock/taskbar show an
            # icon at all when running from source.
            if user_desktop.exists() and _OVERRIDE_MARKER in user_desktop.read_text(encoding="utf-8"):
                user_desktop.unlink()
            return

        system_desktop = _find_system_desktop_file()
        if system_desktop is None:
            logging.debug("Launcher icon: no system desktop file found (running from source); skipping override.")
            return

        content = system_desktop.read_text(encoding="utf-8")
        new_content = _ICON_LINE_RE.sub(f"Icon={APP_ID}.orig", content, count=1)
        if _OVERRIDE_MARKER not in new_content:
            new_content = new_content.rstrip("\n") + f"\n{_OVERRIDE_MARKER}\n"

        user_desktop.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = user_desktop.with_name(user_desktop.name + ".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(tmp_path, user_desktop)
    except Exception as e:
        logging.warning(f"Launcher icon: failed to update desktop override: {e}")
