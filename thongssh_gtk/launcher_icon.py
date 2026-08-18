# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

import sys
import os
import re
import logging
import shutil
import subprocess
from pathlib import Path

from gi.repository import GLib

from .constants import APP_ID

_SYSTEM_DESKTOP_DIRS = ("/usr/share/applications", "/usr/local/share/applications")
_ICON_LINE_RE = re.compile(r"^Icon=.*$", re.MULTILINE)

# Written into every override this function creates for a packaged
# (system-installed) app, and checked before ever deleting one — see the
# "packaged install" branch below for why: the user-local .desktop path
# this writes to would otherwise be indistinguishable from a dev
# checkout's own install-desktop-entry.sh launcher sitting at that exact
# same path, which isn't an override of anything and got deleted by
# mistake before this marker existed.
_OVERRIDE_MARKER = "X-ThongSSH-Launcher-Icon-Override=true"


def _find_system_desktop_file():
    for base in _SYSTEM_DESKTOP_DIRS:
        path = Path(base) / f"{APP_ID}.desktop"
        if path.exists():
            return path
    return None


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
    # Same as install-desktop-entry.sh's own post-install step — without
    # this, some desktop shells only pick up an edited .desktop file's new
    # Icon= line on their own schedule (or not until the next login),
    # rather than the next time the dock/app-menu happens to redraw it.
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(
                ["update-desktop-database", str(path.parent)],
                capture_output=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as e:
            logging.debug(f"Launcher icon: update-desktop-database failed (non-fatal): {e}")


def apply_launcher_icon(icon_stem):
    """Update whichever .desktop file is actually in effect so GNOME Shell
    (Wayland), Plank, etc. pick up the chosen app icon for the dock/
    taskbar — they read it from the .desktop file's Icon= key, never from
    the running window itself.

    Two entirely different situations, handled differently:
    - Packaged install (a real .desktop under /usr/share/applications or
      /usr/local/share/applications): that file needs root to edit and
      may be overwritten by a future package upgrade anyway, so instead a
      disposable user-local *override* copy is created/removed in
      ~/.local/share/applications, which XDG lookup rules let take
      precedence over the system one.
    - Dev checkout (installed by install-desktop-entry.sh, straight into
      ~/.local/share/applications with no system file backing it at all):
      there's nothing to "override" — that user-local file already *is*
      the one and only desktop entry, freely user-writable, so its own
      Icon= line is edited in place instead. Treating this case as
      "no system file found, nothing to do" (as this function used to)
      silently no-ops every icon change for exactly the setup most
      contributors actually run.
    """
    if sys.platform in ("darwin", "win32"):
        return

    try:
        user_desktop = Path(GLib.get_user_data_dir()) / "applications" / f"{APP_ID}.desktop"
        system_desktop = _find_system_desktop_file()

        if system_desktop is not None:
            if icon_stem != "thongssh_orig":
                # Only ever remove a file this function itself wrote.
                if user_desktop.exists() and _OVERRIDE_MARKER in user_desktop.read_text(encoding="utf-8"):
                    user_desktop.unlink()
                return

            content = system_desktop.read_text(encoding="utf-8")
            new_content = _ICON_LINE_RE.sub(f"Icon={APP_ID}.orig", content, count=1)
            if _OVERRIDE_MARKER not in new_content:
                new_content = new_content.rstrip("\n") + f"\n{_OVERRIDE_MARKER}\n"
            _atomic_write(user_desktop, new_content)
            return

        # Dev checkout: edit the existing user-local launcher's own Icon=
        # line directly — install-desktop-entry.sh points it at an
        # absolute path (thongssh_gtk/icons/<stem>.png), so swapping stems
        # is just swapping the filename within that same directory.
        if not user_desktop.exists():
            logging.debug("Launcher icon: no desktop file installed yet (neither system nor user-local); skipping.")
            return
        content = user_desktop.read_text(encoding="utf-8")
        match = _ICON_LINE_RE.search(content)
        if not match:
            return
        current_icon = match.group(0)[len("Icon="):]
        new_icon = str(Path(current_icon).parent / f"{icon_stem}.png")
        if new_icon == current_icon:
            return
        new_content = _ICON_LINE_RE.sub(f"Icon={new_icon}", content, count=1)
        _atomic_write(user_desktop, new_content)
    except Exception as e:
        logging.warning(f"Launcher icon: failed to update desktop override: {e}")
