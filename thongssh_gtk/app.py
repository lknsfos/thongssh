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

from gi.repository import Adw, Gio, Gtk, GdkPixbuf
from .window import ThongSSHWindow # Keep relative import
from .constants import APP_ID, resource_path # Import our new function
from .settings import SettingsManager

# --- Application Class ---
class ThongSSHApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id=APP_ID, **kwargs)
        # ✨ Register resources in the constructor, BEFORE creating the window
        try:
            res_path = resource_path("thongssh.gresource") # Use the helper function
            Gio.resources_register(Gio.Resource.load(res_path))
        except gi.repository.GLib.GError:
            logging.debug("Resources already registered, skipping.")
        self._apply_native_font()
        self.apply_macos_dock_icon()
        self.connect('activate', self.on_activate)

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
        icon_stem = SettingsManager().get("interface.icon")
        icon_path = resource_path(f"icons/{icon_stem}.png")
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
    # ✨ Configure logging
    # Use DEBUG level to see everything, or INFO for important messages only
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    @atexit.register
    def kill_all_sessions():
        logging.info("Exiting... Killing all active sessions.")
        for term, pid in ThongSSHWindow.open_sessions.values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except TypeError:
                logging.warning(f"Cannot kill PID: {pid}, it's not an int")

    app = ThongSSHApp()
    return app.run(sys.argv)

if __name__ == '__main__':
    exit_status = main()
    sys.exit(exit_status)