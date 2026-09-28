# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

import gi
import sys
import os
import signal
import atexit
import logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')
# --- Strict version check ---
try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    gi.require_version('Vte', '3.91')
except ValueError as e:
    logging.basicConfig(level=logging.CRITICAL)
    logging.critical(f"Error: Required libraries not found. {e}")
    logging.critical("Please ensure you have gir1.2-gtk-4.0, gir1.2-adw-1, and gir1.2-vte-3.91 installed.")
    logging.shutdown() # Ensure logs are flushed before exit
    sys.exit(1)

from gi.repository import Adw, Gio, Gtk, GdkPixbuf, GLib
from .window import ThongSSHWindow # Keep relative import
from .detached_tab_window import DetachedTabWindow
from .constants import APP_ID, resource_path # Import our new function
from .settings import SettingsManager
from .keyring import KeyringManager
from . import i18n

# Trampoline map for the app-scoped tab-menu actions (see __init__ below):
# action name -> the explicit-page-argument method to call on whichever
# window currently owns the clicked tab. These mirror already-existing
# win.* actions/handlers (window.py), just resolved via
# self.last_clicked_tab_page instead of a window-local "last clicked tab"
# — see this class's own comment on why win.* can't reach across windows.
_APP_TAB_ACTION_METHODS = {
    "tab-disconnect": "on_menu_tab_disconnect",
    "tab-reconnect": "on_menu_tab_reconnect",
    "tab-duplicate": "on_menu_tab_duplicate",
    "tab-detach": "detach_tab_page",
    "tab-rename": "rename_tab_page",
    "open-sftp": "open_sftp_for_tab_page",
    "open-ssh-from-tab": "on_menu_open_ssh_from_tab",
}

# --- Application Class ---
class ThongSSHApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id=APP_ID, **kwargs)

        # Real singletons, one per running app instance — NOT per-window.
        # Before detachable tabs, ThongSSHWindow.open_sessions/tab_data/
        # last_clicked_tab were plain CLASS attributes (shared across any
        # instances, since a second top-level window never existed) and
        # SettingsManager()/KeyringManager() were instantiated fresh per
        # window — both only "worked" by accident. A DetachedTabWindow
        # makes that untrue: every window (main + every detached one)
        # aliases these same objects, so a tab moved between windows (or
        # a Settings change made from the main window) is immediately and
        # correctly visible everywhere.
        self.settings_manager = SettingsManager()
        self.keyring = KeyringManager()
        self.tab_data = {}
        self.open_sessions = {}
        # Holds the Adw.TabPage last right-clicked (or menu-button-
        # activated) in ANY window's tab strip — see TerminalPaneWindow.
        # on_tabview_setup_menu and the app-scoped tab actions below.
        self.last_clicked_tab_page = None

        # ✨ Register resources in the constructor, BEFORE creating the window
        try:
            res_path = resource_path("thongssh.gresource") # Use the helper function
            Gio.resources_register(Gio.Resource.load(res_path))
        except gi.repository.GLib.GError:
            logging.debug("Resources already registered, skipping.")
        self._apply_native_font()
        self.apply_theme()
        self.apply_macos_dock_icon()
        # Forced Arabic/Hebrew doesn't also flip GTK's own default text
        # direction for free — that's driven by the process locale, which
        # picking a translation via gettext (see i18n.py) doesn't change.
        # Must run before ThongSSHWindow is constructed, so every widget
        # in it is built with the right direction from the start.
        i18n.apply_language_direction()
        self._setup_tab_actions()
        self.connect('activate', self.on_activate)

    def _setup_tab_actions(self):
        """Registers the app-scoped (not window-scoped) tab-menu actions —
        see TerminalPaneWindow.on_tabview_setup_menu / _build_tab_menu_model
        for where these are referenced ("app.tab-disconnect", etc.) and
        window.py's own module docstring/comments for why win.* couldn't
        be used instead: a menu inside a DetachedTabWindow can never
        resolve an action registered only on the main ThongSSHWindow's own
        action map, but "which tab was right-clicked" is naturally
        app-wide context, so these are registered here instead. Each is a
        thin trampoline: resolve the clicked Adw.TabPage
        (last_clicked_tab_page), resolve ITS current owning window (not
        necessarily the window this menu happened to be shown in — a page
        can outlive being dragged elsewhere), and call a same-shaped
        explicit-page-argument method on that window."""
        for action_name, method_name in _APP_TAB_ACTION_METHODS.items():
            action = Gio.SimpleAction.new(action_name, None)
            action.connect("activate", self._on_tab_action, method_name)
            self.add_action(action)

        for field in ("name", "address", "userhost"):
            action = Gio.SimpleAction.new(f"copy-host-{field}", None)
            action.connect("activate", self._on_copy_host_action, field)
            self.add_action(action)

    def _resolve_tab_action_window(self):
        """The window CURRENTLY hosting self.last_clicked_tab_page — not
        necessarily the window whose tab strip the menu was shown in,
        since transfer_page() never rebinds anything; get_root() always
        reflects the page's real, current parent window."""
        page = self.last_clicked_tab_page
        if page is None:
            return None
        child = page.get_child()
        if child is None:
            return None
        return child.get_root()

    def _on_tab_action(self, action, param, method_name):
        page = self.last_clicked_tab_page
        window = self._resolve_tab_action_window()
        if window is None:
            return
        method = getattr(window, method_name, None)
        if method is None:
            logging.warning(f"App tab action '{action.get_name()}': window has no method '{method_name}'.")
            return
        if method_name in ("detach_tab_page", "open_sftp_for_tab_page", "rename_tab_page"):
            method(page)
        else:
            method(None, None, page=page)

    def _on_copy_host_action(self, action, param, field):
        page = self.last_clicked_tab_page
        window = self._resolve_tab_action_window()
        if window is None:
            return
        window.copy_host_field_for_tab_page(page, field)

    def create_detached_window(self):
        """Builds a new DetachedTabWindow (not presented — the caller
        decides when: the "create-window" TabView signal handler needs it
        unpresented-but-realized-enough for the native drag machinery to
        finish transferring the page into it, while detach_tab_page's
        menu-driven path presents explicitly after its own transfer_page
        call)."""
        return DetachedTabWindow(application=self)

    def apply_theme(self):
        """Applies interface.theme (Settings -> General -> Appearance) to
        libadwaita's own app-wide style manager. Called once at startup and
        again from SettingsDialog.on_apply for an immediate live switch —
        Adw.StyleManager handles restyling every already-open window on its
        own, no per-window wiring needed.

        FORCE_*, not PREFER_* — confirmed live on GNOME (which reports
        system-supports-color-schemes=True) that PREFER_LIGHT/PREFER_DARK
        are only weak hints there and lose outright to the desktop's own
        light/dark portal setting, silently making "Light"/"Dark" a no-op
        whenever they disagree with it. FORCE_* is the only pair that
        actually overrides the system choice, which is the entire point of
        offering this as an explicit setting."""
        scheme = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(self.settings_manager.get("interface.theme"), Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def apply_macos_dock_icon(self):
        # GTK's icon-theme machinery (set_icon_name, etc.) has no reach into
        # the macOS Dock/Cmd-Tab switcher — that's owned by AppKit and keyed
        # off the running process, not a .desktop file. Set it directly via
        # NSApplication so the icon shows even for a bare `python3 thongssh.py`
        # with no .app bundle involved.
        #
        # Public (no leading underscore): also called from SettingsDialog to
        # refresh the Dock icon immediately after the user changes it, not
        # just once at startup.
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApplication, NSImage
        except ImportError:
            logging.warning("Dock icon: pyobjc-framework-Cocoa not installed; skipping native Dock icon.")
            return
        icon_stem = self.settings_manager.get("interface.icon")
        # icons/<stem>.png (used everywhere else — GTK window icon, Linux
        # hicolor/.desktop) has a soft alpha fade at the edges, by design:
        # it's meant to blend into whatever's behind it, which is exactly
        # what Linux docks/app grids do with it. The static macOS .app
        # bundle icon (built from the same source, see build-macos.sh)
        # looks fine despite that fade because LaunchServices auto-composites
        # a neutral backdrop behind "incomplete" icons — but
        # setApplicationIconImage_ here does no such compositing, so the
        # exact same file would show the logo floating on pure transparency
        # instead. icons/<stem>_dock.png is that same artwork with alpha
        # forced fully opaque (RGB untouched) — matches what the bundle
        # icon already looks like, macOS-only, Linux assets untouched.
        icon_path = resource_path(f"icons/{icon_stem}_dock.png")
        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is None:
            logging.warning(f"Dock icon: could not load image from {icon_path}")
            return
        NSApplication.sharedApplication().setApplicationIconImage_(image)

    def _apply_native_font(self):
        # macOS ships Adwaita Sans/Cantarell nowhere, so GTK falls back to a
        # generic serif-ish default there. Point it at Helvetica Neue instead
        # — closest match fontconfig can actually resolve on macOS. Linux/BSD
        # are untouched since this only runs under sys.platform == "darwin".
        if sys.platform != "darwin":
            return
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-font-name", "Helvetica Neue 13")

    def on_activate(self, app):
        # If the window doesn't exist yet, create it.
        if not self.props.active_window:
            self.win = ThongSSHWindow(application=self)
        # Present the window. This ensures it's shown correctly on subsequent activations.
        self.props.active_window.present()


def main():
    # ✨ Configure logging — level follows the "Enable debug logging" setting
    # (Settings -> General), off by default. force=True is required since
    # the module-level basicConfig() above already installed a handler;
    # without it this call would be a silent no-op.
    debug_mode = SettingsManager().get("interface.debug_mode")
    logging.basicConfig(level=logging.DEBUG if debug_mode else logging.WARNING,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        force=True)

    @atexit.register
    def kill_all_sessions():
        logging.info("Exiting... Killing all active sessions.")
        for term, pid in app.open_sessions.values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except TypeError:
                logging.warning(f"Cannot kill PID: {pid}, it's not an int")

    # Without this, GLib derives the program name (and, on X11, the
    # window's WM_CLASS) from sys.argv[0] — which for `python3 thongssh.py`
    # is the literal script path, so WM_CLASS ends up as "thongssh.py".
    # That doesn't match any .desktop file's StartupWMClass=terminal.thongssh
    # (or whatever APP_ID actually is), so the window manager can show the
    # nice icon from the .desktop file for the startup-notification phase,
    # then loses track of it the moment the real window maps and falls
    # back to a generic icon — exactly the "icon shows, then disappears a
    # few seconds later" symptom this fixes. Must be set before the first
    # Gtk/Gio call that would otherwise set its own default (GLib doesn't
    # let it change after that).
    GLib.set_prgname(APP_ID)
    app = ThongSSHApp()
    return app.run(sys.argv)

if __name__ == '__main__':
    exit_status = main()
    sys.exit(exit_status)
