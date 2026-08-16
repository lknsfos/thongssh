import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GObject, Pango, GLib
import stat
import logging
import uuid
import re
import datetime

from .constants import COL_NAME, COL_TYPE, AI_STANDARD_PROVIDERS, CLI_STANDARD_PROVIDERS, CLI_MODEL_PRESETS
from .widgets import PositionGrid, set_split_button_active_style, ShortcutPicker
from .colors import COLOR_SCHEMES, DEFAULT_FALLBACK_COLORS, get_scheme_colors, save_custom_color_scheme
from .settings import DEFAULT_SETTINGS
from .launcher_icon import apply_launcher_icon
from .keyring import KeyringManager
from .ai_providers import DEFAULT_MODELS as AI_DEFAULT_MODELS, DEFAULT_BASE_URLS as AI_DEFAULT_BASE_URLS, fetch_models
from .cli_providers import is_available as cli_is_available

# Placeholder for future internationalization (i18n)
_ = lambda s: s

# "custom" is driven by its own switch + color pickers, not a selectable
# dropdown entry — the dropdown only ever lists real templates, one of
# which acts as the base (palette + starting bg/fg) for the custom scheme.
_BASE_SCHEME_IDS = [k for k in COLOR_SCHEMES if k != "custom"]

# Standard terminal ANSI order: 8 normal colors, then their 8 "bright"
# counterparts — matches how every other terminal emulator lays out its
# palette editor, so this is immediately familiar rather than needing its
# own legend.
_ANSI_COLOR_NAMES = [
    _("Black"), _("Red"), _("Green"), _("Yellow"), _("Blue"), _("Magenta"), _("Cyan"), _("White"),
    _("Bright Black"), _("Bright Red"), _("Bright Green"), _("Bright Yellow"),
    _("Bright Blue"), _("Bright Magenta"), _("Bright Cyan"), _("Bright White"),
]


def _rgba_to_hex(rgba):
    """Matches the "#rrggbb" format every built-in scheme in colors.py
    already uses, so the saved custom_color_scheme.json file stays in the
    same human-readable shape as the rest of that module."""
    def channel(value):
        return format(round(max(0.0, min(1.0, value)) * 255), "02x")
    return f"#{channel(rgba.red)}{channel(rgba.green)}{channel(rgba.blue)}"


def _widen_preferences_clamp(widget, max_size=900):
    """AdwPreferencesPage/AdwPreferencesGroup wrap their rows in an internal
    AdwClamp (default maximum-size ~600px, tightening-threshold ~400px)
    that isn't exposed via any public getter on the page/group itself —
    walk the widget tree to find it and loosen it. Without this, the
    settings window looks squeezed into a narrow centered column no matter
    how wide the dialog itself is resized to."""
    if isinstance(widget, Adw.Clamp):
        widget.set_maximum_size(max_size)
        widget.set_tightening_threshold(max_size)
        return
    child = widget.get_first_child()
    while child is not None:
        _widen_preferences_clamp(child, max_size)
        child = child.get_next_sibling()


def _populate_groups_combo(combo_group, group_iters, tree_store, active_parent_iter):
    """
    Recursively populates a group Gtk.ComboBoxText with groups from a
    Gtk.TreeStore, and selects active_parent_iter's group if given.
    Shared by HostDialog and GroupDialog, which both keep their own
    `self.combo_group`/`self.group_iters`/`self.tree_store` for this.
    """
    combo_group.append("root", _("Root (/)"))
    group_iters["root"] = None  # iter for the root

    def iter_groups(model, tree_iter, prefix=""):
        node_type = model.get_value(tree_iter, COL_TYPE)
        if node_type == "group":
            name = model.get_value(tree_iter, COL_NAME)
            display_name = f"{prefix} {name}"

            combo_group.append(display_name, display_name)
            group_iters[display_name] = tree_iter.copy()  # Copy the iter!

            child_iter = model.iter_children(tree_iter)
            while child_iter:
                iter_groups(model, child_iter, prefix + "  └─")
                child_iter = model.iter_next(child_iter)

    root_iter = tree_store.get_iter_first()
    while root_iter:
        iter_groups(tree_store, root_iter)
        root_iter = tree_store.iter_next(root_iter)

    combo_group.set_active_id("root")

    if active_parent_iter:
        for k, v in group_iters.items():
            if v and tree_store.get_path(v) == tree_store.get_path(active_parent_iter):
                combo_group.set_active_id(k)
                return


class ResponseDialog(Adw.Window):
    """
    Common base for the modal dialogs below: adds a 'response' signal (with
    an int response code, mirroring Gtk.ResponseType) and the emit-then-close
    helper they all shared as identical copy-pasted code.
    """
    __gsignals__ = {
        'response': (GObject.SignalFlags.RUN_FIRST, None, (int,))
    }

    def response(self, response_id):
        self.emit("response", response_id)
        self.close()


class InputDialog(ResponseDialog):
    """
    A simple dialog with a single text entry.
    Used for "Rename" and "Login Prompt".
    """
    def __init__(self, parent, title, message, default_text="", is_password=False):
        super().__init__(transient_for=parent, modal=True)
        self.set_default_size(400, -1)

        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Adw.WindowTitle(title=title))

        self.ok_button = Gtk.Button(label=_("OK"))
        self.ok_button.add_css_class("suggested-action")
        self.ok_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.OK))
        header_bar.pack_end(self.ok_button)

        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.CANCEL))
        header_bar.pack_start(cancel_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24, margin_bottom=24, margin_start=12, margin_end=12,
                              valign=Gtk.Align.CENTER)
        main_box.append(content_box)
        
        self.set_content(main_box)

        if message:
            content_box.append(Gtk.Label(label=message, halign=Gtk.Align.START))

        self.entry = Gtk.Entry()
        self.entry.set_text(default_text)
        if is_password:
            self.entry.set_visibility(False) # Hide text for passwords
        self.entry.connect("changed", self.on_validate)
        self.entry.connect("activate", lambda e: self.ok_button.get_sensitive() and self.response(Gtk.ResponseType.OK))
        content_box.append(self.entry)

        self.on_validate(self.entry)
        self.entry.grab_focus()

    def on_validate(self, entry):
        text = entry.get_text().strip()
        self.ok_button.set_sensitive(len(text) > 0)

    def get_text(self):
        return self.entry.get_text().strip()

    def run_async(self, callback):
        """Asynchronous launch for login prompt."""
        def on_response(dialog, response):
            text = self.get_text() if response == Gtk.ResponseType.OK else None
            self.destroy()
            callback(text)

        self.connect("response", on_response)
        self.present()

class MessageDialog(ResponseDialog):
    """
    A simple wrapper around Adw.MessageDialog to provide an async run method.
    """
    def __init__(self, parent, heading, body=None, buttons=None):
        super().__init__(transient_for=parent, modal=True)
        self.set_default_size(400, -1)

        # Use parent directly for Adw.MessageDialog, not self
        self.dialog = Adw.MessageDialog(
            transient_for=parent,
            heading=heading if heading else "",
            body=body if body else ""
        )
        
        # Store mapping of response IDs to use in callback
        self.response_mapping = {}
        
        if buttons:
            for label, response_id in buttons:
                # Use a simple string ID for Adw.MessageDialog
                str_id = f"response_{response_id}"
                self.dialog.add_response(str_id, label)
                self.response_mapping[str_id] = response_id
                
                if response_id == Gtk.ResponseType.OK:
                    # Use SUGGESTED appearance for OK button (green/blue)
                    self.dialog.set_response_appearance(str_id, Adw.ResponseAppearance.SUGGESTED)
        else:
            self.dialog.add_response("ok", _("OK"))
            self.dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            self.response_mapping["ok"] = Gtk.ResponseType.OK

        self.dialog.set_default_response(f"response_{Gtk.ResponseType.OK}" if buttons else "ok")
        self.dialog.set_close_response(f"response_{Gtk.ResponseType.CANCEL}" if buttons else "ok")

    def run_async(self, callback):
        """Helper to run the dialog and get the result in a callback."""
        def on_response(dialog_widget, response_id_str):
            # Map the string response ID back to the original ResponseType
            response_id = self.response_mapping.get(response_id_str, Gtk.ResponseType.CANCEL)
            callback(self, response_id)
            # Don't call self.destroy() since self is just a wrapper
        self.dialog.connect("response", on_response)
        self.dialog.present() # Present the actual dialog, not the wrapper

class PermissionsDialog(ResponseDialog):
    """A dialog for viewing and editing file permissions (chmod)."""

    def __init__(self, parent, initial_mode):
        super().__init__(transient_for=parent, modal=True)
        self.set_default_size(350, -1)
        self._is_updating = False  # Flag to prevent signal loops

        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Adw.WindowTitle(title=_("Change Permissions")))

        self.ok_button = Gtk.Button(label=_("OK"), css_classes=["suggested-action"])
        self.ok_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.OK))
        header_bar.pack_end(self.ok_button)

        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.CANCEL))
        header_bar.pack_start(cancel_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        self.set_content(main_box)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        main_box.append(content_box)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        grid.attach(Gtk.Label(label="", xalign=0), 0, 0, 1, 1) # Spacer
        grid.attach(Gtk.Label(label=_("Read"), halign=Gtk.Align.CENTER), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Write"), halign=Gtk.Align.CENTER), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Execute"), halign=Gtk.Align.CENTER), 3, 0, 1, 1)

        self.checks = {}
        labels = [_("Read"), _("Write"), _("Execute")]
        values = [stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR,
                  stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP,
                  stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH]

        for i, val in enumerate(values):
            row, col = divmod(i, 3) # row: 0=user, 1=group, 2=other. col: 0=read, 1=write, 2=exec
            # Add row labels (User, Group, Other)
            if col == 0:
                row_labels = [_("User"), _("Group"), _("Other")]
                grid.attach(Gtk.Label(label=row_labels[row], xalign=0), 0, row + 1, 1, 1)

            chk = Gtk.CheckButton()
            chk.connect("toggled", self.on_check_toggled)
            self.checks[val] = chk
            grid.attach(chk, col + 1, row + 1, 1, 1) # Attach checkbox

        content_box.append(grid)

        octal_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
        octal_box.append(Gtk.Label(label=_("Octal value:")))
        self.entry_octal = Gtk.Entry(max_length=4, width_chars=5)
        self.entry_octal.connect("changed", self.on_octal_changed)
        octal_box.append(self.entry_octal)
        content_box.append(octal_box)

        self.set_mode(initial_mode)

    def on_check_toggled(self, checkbox):
        """Updates the octal entry when a checkbox is toggled."""
        if self._is_updating: return
        self._is_updating = True
        mode = 0
        for val, chk in self.checks.items():
            if chk.get_active():
                mode |= val
        self.entry_octal.set_text(oct(mode)[-3:])
        self._is_updating = False

    def on_octal_changed(self, entry):
        """Updates checkboxes when the octal entry is changed."""
        if self._is_updating: return
        self._is_updating = True
        try:
            text = entry.get_text().strip()
            if text:
                mode = int(text, 8)
                for val, chk in self.checks.items():
                    chk.set_active(bool(mode & val))
        except ValueError:
            # Handle invalid input if necessary, e.g., by showing an error style
            pass
        self._is_updating = False

    def set_mode(self, mode):
        """Sets the initial state of the dialog from a given mode."""
        self._is_updating = True
        for val, chk in self.checks.items():
            chk.set_active(bool(mode & val))
        self.entry_octal.set_text(oct(mode)[-3:])
        self._is_updating = False

    def get_mode(self):
        """Returns the currently selected mode as an integer."""
        try:
            return int(self.entry_octal.get_text().strip(), 8)
        except (ValueError, TypeError):
            return 0 # Or handle error appropriately

    def run_async(self, callback):
        """Helper to run the dialog and get the result in a callback."""
        def on_response(dialog, response_id):
            mode = self.get_mode() if response_id == Gtk.ResponseType.OK else None
            callback(mode)
        self.connect("response", on_response)
        self.present()

class HostDialog(ResponseDialog):

    def __init__(self, parent_window, tree_store, host_data_to_edit=None, parent_iter=None):

        super().__init__(transient_for=parent_window, modal=True)

        self.tree_store = tree_store
        self.host_config = host_data_to_edit or {}
        self.keyring = KeyringManager()
        self.is_edit_mode = (host_data_to_edit is not None)

        self.set_default_size(550, -1)

        header_bar = Adw.HeaderBar()
        if self.is_edit_mode:
            header_bar.set_title_widget(Adw.WindowTitle(title=_("Edit Host: {host_name}").format(host_name=self.host_config.get('name', ''))))
            self.ok_button = Gtk.Button(label=_("Save"))
        else:
            header_bar.set_title_widget(Adw.WindowTitle(title=_("Add New Host")))
            self.ok_button = Gtk.Button(label=_("Add"))

        self.ok_button.add_css_class("suggested-action")
        self.ok_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.OK))
        self.ok_button.set_sensitive(False)
        header_bar.pack_end(self.ok_button)

        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.CANCEL))
        header_bar.pack_start(cancel_button)

        # --- Basic Settings (not tabbed — always visible) ---
        page_basic = Adw.PreferencesPage()

        group_main = Adw.PreferencesGroup()
        group_main.set_title(_("Basic Settings"))
        page_basic.add(group_main)

        self.protocol_row = Adw.ComboRow(title=_("Protocol"), model=Gtk.StringList.new(["SSH", "Telnet"]))
        self.protocol_row.connect("notify::selected-item", self.on_protocol_changed)
        group_main.add(self.protocol_row)

        self.entry_name = Adw.EntryRow(title=_("Name"))
        group_main.add(self.entry_name)

        row_host = Adw.ActionRow(title=_("Hostname/IP"), subtitle=_("Hostname or IP address"))
        self.entry_host = Gtk.Entry()
        self.entry_host.set_valign(Gtk.Align.CENTER)
        row_host.add_suffix(self.entry_host)
        row_host.set_activatable_widget(self.entry_host)
        group_main.add(row_host)

        row_group = Adw.ActionRow(title=_("Group"))
        self.combo_group = Gtk.ComboBoxText()
        row_group.add_suffix(self.combo_group)
        group_main.add(row_group)

        self.group_iters = {}
        self.populate_groups_combo(parent_iter)

        self.entry_port = Adw.SpinRow(
            title=_("Port"),
            adjustment=Gtk.Adjustment(value=22, lower=0, upper=65535, step_increment=1)
        )
        group_main.add(self.entry_port)

        # --- Tabbed div: Authentication / Options ---
        self.tabs_stack = Adw.ViewStack()

        page_auth = Adw.PreferencesPage()
        self.group_auth = Adw.PreferencesGroup(
            title=_("Authentication"),
            description=_("Saved securely in system keyring") # Moved from subtitle
        )
        page_auth.add(self.group_auth)

        self.entry_username = Adw.EntryRow(title=_("Username"))
        self.group_auth.add(self.entry_username)

        self.password_row = Adw.PasswordEntryRow(title=_("Password"))
        self.group_auth.add(self.password_row)

        self.clear_password_button = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                                                tooltip_text=_("Clear saved password"))
        self.clear_password_button.connect("clicked", self.on_clear_password)
        self.password_row.add_suffix(self.clear_password_button)
        self.clear_password_button.set_sensitive(False) # Enabled only in edit mode if password exists

        self.row_key_file = Adw.EntryRow(title=_("Path to key (IdentityFile)"))
        key_button = Gtk.Button(icon_name="document-open-symbolic")
        key_button.set_valign(Gtk.Align.CENTER)
        key_button.connect("clicked", self.on_choose_key_file_clicked)
        self.row_key_file.add_suffix(key_button)
        self.group_auth.add(self.row_key_file)

        self.tabs_stack.add_titled(page_auth, "auth", _("Authentication")).set_icon_name("dialog-password-symbolic")

        page_options = Adw.PreferencesPage()

        group_logging = Adw.PreferencesGroup(title=_("Logging"))
        page_options.add(group_logging)

        self.switch_save_log = Adw.SwitchRow(
            title=_("Save session log"),
            subtitle=_("Records every session to a file in the configured log directory (see Settings → Client Options)")
        )
        group_logging.add(self.switch_save_log)

        self.group_ssh_opts = Adw.PreferencesGroup(title=_("SSH Options"))
        page_options.add(self.group_ssh_opts)

        self.switch_compat = Adw.SwitchRow(title=_("Compatibility with old systems"),
                                             subtitle=_("Enables old ciphers (for CentOS 5/6, etc.)"))
        self.group_ssh_opts.add(self.switch_compat)

        self.switch_forward_x = Adw.SwitchRow(title=_("X11 Forwarding"),
                                                subtitle=_("Enables the -X flag (ForwardX11)"))
        self.group_ssh_opts.add(self.switch_forward_x)

        self.switch_agent = Adw.SwitchRow(title=_("ssh-agent Forwarding"),
                                             subtitle=_("Enables the -A flag (ForwardAgent)"))
        self.group_ssh_opts.add(self.switch_agent)

        row_options = Adw.ActionRow(title=_("Extra SSH Options"),
                                      subtitle=_("Example: -o ServerAliveInterval=60"))
        self.entry_options = Gtk.Entry()
        self.entry_options.set_valign(Gtk.Align.CENTER)
        row_options.add_suffix(self.entry_options)
        row_options.set_activatable_widget(self.entry_options)
        self.group_ssh_opts.add(row_options)

        self.group_telnet_opts = Adw.PreferencesGroup(title=_("Telnet Options"))
        page_options.add(self.group_telnet_opts)

        self.switch_telnet_binary = Adw.SwitchRow(title=_("Binary Mode"),
                                                  subtitle=_("Enable binary mode transmission"))
        self.group_telnet_opts.add(self.switch_telnet_binary)

        self.switch_telnet_echo = Adw.SwitchRow(title=_("Local Echo"),
                                                subtitle=_("Echo typed characters locally"))
        self.group_telnet_opts.add(self.switch_telnet_echo)

        self.tabs_stack.add_titled(page_options, "options", _("Options")).set_icon_name("preferences-other-symbolic")

        view_switcher = Adw.ViewSwitcher()
        view_switcher.set_stack(self.tabs_stack)
        view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        switcher_box.append(view_switcher)

        self.tabs_stack.set_vexpand(True)

        # --- Field Population and Validation ---
        if self.is_edit_mode:
            self.populate_fields()

        self.on_protocol_changed(self.protocol_row, None)

        self.entry_name.connect("notify::text", self.on_validate)
        self.entry_host.connect("changed", self.on_validate)
        self.entry_username.connect("notify::text", self.on_username_entry_changed)
        self.on_validate(None)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        main_box.append(page_basic) # Adw.PreferencesPage is already scrollable
        main_box.append(switcher_box)
        main_box.append(self.tabs_stack)
        self.set_content(main_box)

    def on_username_entry_changed(self, entry, *args):
        """Enable/disable password field based on username presence."""
        has_user = bool(entry.get_text().strip())
        self.password_row.set_sensitive(has_user)
        if not has_user:
            if self.is_edit_mode and self.keyring.load_password(self.entry_name.get_text().strip()):
                self.keyring.clear_password(self.entry_name.get_text().strip())
                self.clear_password_button.set_sensitive(False)
            self.password_row.set_text("")


    def on_protocol_changed(self, combo_row, param):
        """Shows/hides options based on the selected protocol."""
        selected_protocol = self.protocol_row.get_selected_item().get_string().lower()
        is_ssh = (selected_protocol == "ssh")

        self.row_key_file.set_visible(is_ssh) # Telnet has no key-based auth
        self.group_ssh_opts.set_visible(is_ssh)
        self.group_telnet_opts.set_visible(not is_ssh)

        # Default port for whichever protocol is now selected, unless it's
        # already been set to something else (including the *other*
        # protocol's default, in which case switch it over).
        current_port = self.entry_port.get_value()
        if is_ssh:
            if current_port in (0, 23): self.entry_port.set_value(22)
        else: # Telnet
            if current_port in (0, 22): self.entry_port.set_value(23)


    def on_choose_key_file_clicked(self, button):
        """Shows the native file chooser dialog."""

        file_chooser = Gtk.FileChooserDialog(
            title=_("Select SSH Key"),
            transient_for=self, # Attach to this dialog
            action=Gtk.FileChooserAction.OPEN
        )
        file_chooser.add_button(_("Select"), Gtk.ResponseType.OK)
        file_chooser.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)

        filter_any = Gtk.FileFilter()
        filter_any.set_name(_("All files"))
        filter_any.add_pattern("*")
        file_chooser.add_filter(filter_any)

        file_chooser.connect("response", self.on_key_file_chosen)
        file_chooser.present()

    def on_key_file_chosen(self, dialog, response):
        """Callback for when a file is chosen in the FileChooserDialog."""
        if response == Gtk.ResponseType.OK:
            gfile = dialog.get_file()
            if gfile:
                self.row_key_file.set_text(gfile.get_path())
        dialog.destroy()

    def populate_groups_combo(self, active_parent_iter):
        _populate_groups_combo(self.combo_group, self.group_iters, self.tree_store, active_parent_iter)

    def populate_fields(self):
        """Fills the fields with data from self.host_config (Edit mode)."""
        cfg = self.host_config

        protocol = cfg.get("protocol", "ssh")
        if protocol == "telnet":
            self.protocol_row.set_selected(1)
        else:
            self.protocol_row.set_selected(0)
        self.entry_name.set_text(cfg.get("name", ""))
        # The stored "host" is still the combined "[user@]hostname" string
        # (unchanged on-disk format — see get_data) split apart just for
        # display, so existing hosts.json entries keep working untouched.
        host_str = cfg.get("host", "")
        if "@" in host_str:
            username, hostname = host_str.split("@", 1)
        else:
            username, hostname = "", host_str
        self.entry_username.set_text(username)
        self.entry_host.set_text(hostname)

        default_port = 23 if protocol == "telnet" else 22
        self.entry_port.set_value(int(cfg.get("port") or default_port))

        self.switch_save_log.set_active(cfg.get("save_log", False))

        key_path = cfg.get("key_path")
        if key_path:
            self.row_key_file.set_text(key_path)

        self.switch_compat.set_active(cfg.get("compat_old_systems", False))
        self.switch_forward_x.set_active(cfg.get("forward_x", False))
        self.switch_agent.set_active(cfg.get("forward_agent", False))
        self.entry_options.set_text(cfg.get("ssh_options", "") or "")

        self.switch_telnet_binary.set_active(cfg.get("telnet_binary", False))
        self.switch_telnet_echo.set_active(cfg.get("telnet_local_echo", False))

        # Enable password fields based on current data
        self.on_username_entry_changed(self.entry_username)
        # Check if a password exists to enable the clear button
        self.clear_password_button.set_sensitive(self.keyring.load_password(cfg.get("name")) is not None)

    def on_validate(self, widget, *args): # *args because signals differ
        """Validates required fields."""
        name_ok = len(self.entry_name.get_text().strip()) > 0
        host_ok = len(self.entry_host.get_text().strip()) > 0
        self.ok_button.set_sensitive(name_ok and host_ok)

    def on_clear_password(self, button):
        """Handles click on the 'clear password' button."""
        host_name = self.entry_name.get_text().strip()
        if not host_name:
            return

        self.keyring.clear_password(host_name)
        self.password_row.set_text("") # Clear the entry field
        self.clear_password_button.set_sensitive(False) # Disable button after clearing

    def get_data(self):
        """Collects data from the fields and returns a config dict and parent iter."""

        port_val = self.entry_port.get_value()
        port = int(port_val) if port_val > 0 else None

        # Handle password saving
        new_password = self.password_row.get_text()
        if new_password:
            self.keyring.save_password(self.entry_name.get_text().strip(), new_password)

        key_path = self.row_key_file.get_text().strip()
        if not key_path:
            key_path = None

        protocol = self.protocol_row.get_selected_item().get_string().lower()

        # Recombined into the same "[user@]hostname" string everything else
        # in the app (window.py, sftp_widget.py, send_file.py) still expects
        # in the "host" field — only the dialog's UI splits it into two rows.
        username = self.entry_username.get_text().strip()
        hostname = self.entry_host.get_text().strip()
        host_value = f"{username}@{hostname}" if username else hostname

        config = {
            "protocol": protocol,
            "name": self.entry_name.get_text().strip(),
            "host": host_value,
            "port": port,
            "key_path": key_path,
            "compat_old_systems": self.switch_compat.get_active(),
            "forward_x": self.switch_forward_x.get_active(),
            "forward_agent": self.switch_agent.get_active(),
            "ssh_options": self.entry_options.get_text().strip() or None,
            "telnet_binary": self.switch_telnet_binary.get_active(),
            "telnet_local_echo": self.switch_telnet_echo.get_active(),
            "save_log": self.switch_save_log.get_active(),
        }

        parent_id = self.combo_group.get_active_id()
        parent_iter = self.group_iters.get(parent_id)

        return config, parent_iter


# --- CLASS: Add Group Dialog ---
class GroupDialog(ResponseDialog):

    def __init__(self, parent_window, tree_store, parent_iter=None):
        super().__init__(transient_for=parent_window, modal=True)
        self.set_default_size(400, -1)

        self.tree_store = tree_store

        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Adw.WindowTitle(title=_("Create New Group")))

        self.ok_button = Gtk.Button(label=_("Create"))
        self.ok_button.add_css_class("suggested-action")
        self.ok_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.OK))
        self.ok_button.set_sensitive(False)
        header_bar.pack_end(self.ok_button)

        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.CANCEL))
        header_bar.pack_start(cancel_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        main_box.append(content_box)
        self.set_content(main_box)

        content_box.append(Gtk.Label(label=_("Group Name:"), halign=Gtk.Align.START))
        self.entry_name = Gtk.Entry()
        self.entry_name.connect("changed", self.on_validate)
        self.entry_name.connect("activate", lambda e: self.response(Gtk.ResponseType.OK))
        content_box.append(self.entry_name)

        content_box.append(Gtk.Label(label=_("Parent Group:"), halign=Gtk.Align.START))
        self.combo_group = Gtk.ComboBoxText()
        content_box.append(self.combo_group)

        self.group_iters = {}
        self.populate_groups_combo(parent_iter)

        self.entry_name.grab_focus()

    def on_validate(self, entry):
        text = entry.get_text().strip()
        self.ok_button.set_sensitive(len(text) > 0)

    def populate_groups_combo(self, active_parent_iter):
        _populate_groups_combo(self.combo_group, self.group_iters, self.tree_store, active_parent_iter)

    def get_data(self):
        new_name = self.entry_name.get_text().strip()
        parent_id = self.combo_group.get_active_id()
        parent_iter = self.group_iters.get(parent_id)
        return new_name, parent_iter


class QuickyDialog(ResponseDialog):
    """Add/edit a single Quicky: a name + a multi-line text snippet that
    gets inserted — not executed — into the active terminal (see
    ThongSSHWindow.on_quicky_row_activated)."""
    def __init__(self, parent_window, existing=None):
        super().__init__(transient_for=parent_window, modal=True)
        self.set_default_size(420, 320)
        is_edit_mode = existing is not None

        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Adw.WindowTitle(
            title=_("Edit Quicky") if is_edit_mode else _("Add Quicky")
        ))

        self.ok_button = Gtk.Button(label=_("Save") if is_edit_mode else _("Add"))
        self.ok_button.add_css_class("suggested-action")
        self.ok_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.OK))
        header_bar.pack_end(self.ok_button)

        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.connect("clicked", lambda w: self.response(Gtk.ResponseType.CANCEL))
        header_bar.pack_start(cancel_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                               margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        main_box.append(content_box)
        self.set_content(main_box)

        self.name_entry = Gtk.Entry(text=existing.get("name", "") if existing else "")
        self.name_entry.set_placeholder_text(_("Name"))
        self.name_entry.connect("changed", self.on_validate)
        content_box.append(self.name_entry)

        # Multi-line, Ctrl+Enter-to-save — same pattern as
        # BatchCommandDialog.command_view: plain Enter just inserts a
        # newline, since a Quicky's own text can legitimately span lines.
        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.text_view.add_css_class("card")
        self.text_view.set_top_margin(6)
        self.text_view.set_bottom_margin(6)
        self.text_view.set_left_margin(8)
        self.text_view.set_right_margin(8)
        self.text_buffer = self.text_view.get_buffer()
        if existing:
            self.text_buffer.set_text(existing.get("text", ""))

        text_key_controller = Gtk.EventControllerKey.new()
        text_key_controller.connect("key-pressed", self.on_text_key_pressed)
        self.text_view.add_controller(text_key_controller)

        text_scroller = Gtk.ScrolledWindow()
        text_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        text_scroller.set_min_content_height(80)
        text_scroller.set_max_content_height(200)
        text_scroller.set_propagate_natural_height(True)
        text_scroller.set_vexpand(True)
        text_scroller.set_child(self.text_view)
        content_box.append(text_scroller)

        hint_label = Gtk.Label(
            label=_("Available variables: $name, $host, $user"),
            xalign=0, wrap=True, css_classes=["dim-label", "caption"]
        )
        content_box.append(hint_label)

        self.on_validate()
        self.name_entry.grab_focus()

    def on_validate(self, *_args):
        self.ok_button.set_sensitive(len(self.name_entry.get_text().strip()) > 0)

    def on_text_key_pressed(self, controller, keyval, keycode, modifier):
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK
        if is_ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.ok_button.get_sensitive():
                self.response(Gtk.ResponseType.OK)
            return True
        return False

    def get_data(self):
        name = self.name_entry.get_text().strip()
        start, end = self.text_buffer.get_bounds()
        text = self.text_buffer.get_text(start, end, False)
        return name, text


# --- CLASS: Settings Dialog ---
class SettingsDialog(Adw.Window):
    def __init__(self, parent_window, settings_manager):
        super().__init__(transient_for=parent_window, modal=True)
        self.parent_window = parent_window
        self.settings_manager = settings_manager
        self.keyring = KeyringManager()

        self.set_default_size(800, 620)

        header_bar = Adw.HeaderBar()

        self.stack = Adw.ViewStack()

        # --- Terminal Page ---
        page_terminal = Adw.PreferencesPage()
        self.page_terminal = page_terminal # Save reference for reset
        page_terminal.set_title(_("Terminal"))
        page_terminal.set_icon_name("utilities-terminal-symbolic")

        group_appearance = Adw.PreferencesGroup()
        group_appearance.set_title(_("Appearance"))
        page_terminal.add(group_appearance)

        font_row = Adw.ActionRow(title=_("Font"))
        self.font_button = Gtk.FontButton()
        self.font_button.set_font(self.settings_manager.get("terminal.font"))
        self.font_button.set_use_font(True)
        self.font_button.set_use_size(True)
        self.font_button.set_level(Gtk.FontChooserLevel.FAMILY | Gtk.FontChooserLevel.SIZE | Gtk.FontChooserLevel.STYLE)
        font_row.add_suffix(self.font_button)
        font_row.set_activatable_widget(self.font_button)
        group_appearance.add(font_row)

        scheme_names = [COLOR_SCHEMES[k]['name'] for k in _BASE_SCHEME_IDS]
        self.scheme_row = Adw.ComboRow(title=_("Color Scheme"), model=Gtk.StringList.new(scheme_names))

        # Find index of current scheme. While "custom" is active, the
        # dropdown can't show "custom" itself (it's not one of its
        # entries) — it shows whichever template last acted as its base.
        current_scheme_key = self.settings_manager.get("terminal.color_scheme")
        is_custom_scheme = current_scheme_key == "custom"
        base_scheme_key = (
            self.settings_manager.get("terminal.custom_scheme_base") if is_custom_scheme else current_scheme_key
        )
        try:
            self.scheme_row.set_selected(_BASE_SCHEME_IDS.index(base_scheme_key))
        except ValueError:
            self.scheme_row.set_selected(0) # Default

        group_appearance.add(self.scheme_row)

        self.custom_scheme_switch = Adw.SwitchRow(
            title=_("Custom Colors"),
            subtitle=_("Manually set every color, starting from the template above"),
        )
        self.custom_scheme_switch.set_active(is_custom_scheme)
        group_appearance.add(self.custom_scheme_switch)

        group_custom_colors = Adw.PreferencesGroup()
        group_custom_colors.set_title(_("Custom Colors"))
        group_custom_colors.set_visible(is_custom_scheme)
        page_terminal.add(group_custom_colors)

        def _make_color_button(hex_value):
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            rgba = Gdk.RGBA()
            rgba.parse(hex_value)
            button.set_rgba(rgba)
            return button

        def _add_color_row(title, hex_value):
            row = Adw.ActionRow(title=title)
            button = _make_color_button(hex_value)
            row.add_suffix(button)
            row.set_activatable_widget(button)
            group_custom_colors.add(row)
            return button

        # Seed values: an already-saved custom scheme if one's active, else
        # the selected template's own colors ("Default" has none, hence the
        # xterm-classic fallback), never blank.
        seed_colors = (get_scheme_colors("custom") if is_custom_scheme else None) or \
            COLOR_SCHEMES.get(base_scheme_key, {}).get("colors") or DEFAULT_FALLBACK_COLORS

        self.custom_bg_button = _add_color_row(_("Background"), seed_colors["background"])
        self.custom_fg_button = _add_color_row(_("Foreground"), seed_colors["foreground"])

        # The 16-color ANSI palette apps use for text formatting (e.g. "red"
        # for errors, "green" for success) — colorblind-accessibility is
        # exactly why this needs to be per-swatch editable rather than just
        # inherited wholesale from the chosen template.
        palette_row = Adw.ActionRow(title=_("Palette"))
        palette_row.set_subtitle(_("The 16 colors terminal apps use for colored text"))
        palette_grid = Gtk.Grid(row_spacing=4, column_spacing=4)
        palette_grid.set_valign(Gtk.Align.CENTER)
        self.custom_palette_buttons = []
        for i, color_name in enumerate(_ANSI_COLOR_NAMES):
            swatch = _make_color_button(seed_colors["palette"][i])
            swatch.set_tooltip_text(color_name)
            palette_grid.attach(swatch, i % 8, i // 8, 1, 1)
            self.custom_palette_buttons.append(swatch)
        palette_row.add_suffix(palette_grid)
        group_custom_colors.add(palette_row)

        def _seed_pickers_from(colors):
            bg_rgba, fg_rgba = Gdk.RGBA(), Gdk.RGBA()
            bg_rgba.parse(colors["background"])
            fg_rgba.parse(colors["foreground"])
            self.custom_bg_button.set_rgba(bg_rgba)
            self.custom_fg_button.set_rgba(fg_rgba)
            for button, hex_value in zip(self.custom_palette_buttons, colors["palette"]):
                rgba = Gdk.RGBA()
                rgba.parse(hex_value)
                button.set_rgba(rgba)

        def _on_custom_switch_toggled(row, _pspec):
            active = row.get_active()
            group_custom_colors.set_visible(active)
            if not active:
                return
            # Just turned on: (re-)seed everything from whichever template
            # is currently selected above, so the starting point is always
            # recognizable rather than leftover from a previous toggle.
            base_key = _BASE_SCHEME_IDS[self.scheme_row.get_selected()]
            base_colors = COLOR_SCHEMES.get(base_key, {}).get("colors") or DEFAULT_FALLBACK_COLORS
            _seed_pickers_from(base_colors)

        # Connected after the initial set_active() above, so restoring an
        # already-custom scheme on dialog-open doesn't get clobbered by the
        # reseed-from-template behavior meant only for a fresh toggle.
        self.custom_scheme_switch.connect("notify::active", _on_custom_switch_toggled)

        group_behavior = Adw.PreferencesGroup()
        group_behavior.set_title(_("Behavior"))
        page_terminal.add(group_behavior)

        self.scrollback_row = Adw.SpinRow(
            title=_("Scrollback History"),
            subtitle=_("Number of lines to keep in history"),
            adjustment=Gtk.Adjustment(value=self.settings_manager.get("terminal.scrollback_lines"), lower=100, upper=100000, step_increment=1024)
        )
        group_behavior.add(self.scrollback_row)

        self.close_on_disconnect_row = Adw.SwitchRow(
            title=_("Close tab on disconnect"),
            subtitle=_("Automatically close the terminal tab when the session ends")
        )
        self.close_on_disconnect_row.set_active(self.settings_manager.get("terminal.close_on_disconnect"))
        group_behavior.add(self.close_on_disconnect_row)

        # Only makes sense when tabs survive a disconnect at all (see
        # above) — a dead tab's name gets struck through (on_ssh_process_
        # exited) and "Reconnect" from its context menu normally just
        # reuses whatever username it last connected with; this makes that
        # prompt itself again instead, pre-filled with the old one so
        # confirming without changes reproduces the old behavior.
        self.reconnect_prompt_username_row = Adw.SwitchRow(
            title=_("Ask for username when reconnecting"),
            subtitle=_("Prompt again instead of reusing the previous username when reconnecting to a disconnected tab")
        )
        self.reconnect_prompt_username_row.set_active(self.settings_manager.get("terminal.reconnect_prompt_username"))
        group_behavior.add(self.reconnect_prompt_username_row)

        def _update_reconnect_prompt_visibility(*_args):
            self.reconnect_prompt_username_row.set_visible(not self.close_on_disconnect_row.get_active())
        self.close_on_disconnect_row.connect("notify::active", _update_reconnect_prompt_visibility)
        _update_reconnect_prompt_visibility()

        # --- Watermark (the header-bar button next to the split buttons is
        # the same live on/off switch as the row below — either one flips
        # interface.watermark_enabled, so they never fall out of sync) ---
        group_watermark = Adw.PreferencesGroup(title=_("Watermark"))
        page_terminal.add(group_watermark)

        self.watermark_enabled_row = Adw.SwitchRow(
            title=_("Enabled on start"),
            subtitle=_("Whether the watermark is on when the app launches — same switch as the header-bar button")
        )
        self.watermark_enabled_row.set_active(self.settings_manager.get("interface.watermark_enabled"))
        group_watermark.add(self.watermark_enabled_row)

        self.watermark_text_row = Adw.EntryRow(title=_("Text"))
        self.watermark_text_row.set_text(self.settings_manager.get("interface.watermark_text"))
        watermark_text_note = Gtk.Label(
            label=_("Available variables: $name, $host, $user"),
            xalign=0, wrap=True, css_classes=["dim-label", "caption"]
        )
        group_watermark.add(self.watermark_text_row)
        group_watermark.add(watermark_text_note)

        # Same 3x3 anchor-point grid as the header-bar watermark button's
        # own quick-picker popover (see window.py) — one shared widget
        # (widgets.PositionGrid) so the two never show/save anything
        # different from each other.
        self.watermark_position_grid = PositionGrid(self.settings_manager.get("interface.watermark_position"))
        self.watermark_position_row = Adw.ActionRow(title=_("Position"))
        self.watermark_position_row.add_suffix(self.watermark_position_grid)
        group_watermark.add(self.watermark_position_row)

        self.watermark_font_size_row = Adw.SpinRow(
            title=_("Font Size"),
            adjustment=Gtk.Adjustment(lower=6, upper=96, step_increment=1)
        )
        self.watermark_font_size_row.set_value(self.settings_manager.get("interface.watermark_font_size"))
        group_watermark.add(self.watermark_font_size_row)

        # Family only (Gtk.FontLevel.FAMILY) — size is the SpinRow above,
        # weight/style would just get overridden by the plain Pango
        # attributes _update_watermark_for_tab already sets. Deliberately
        # excluded from Sync (see settings_sync.py) for the same reason as
        # terminal.font: a family on one machine often isn't installed on
        # another.
        watermark_font_family_row = Adw.ActionRow(title=_("Font"))
        self.watermark_font_family_button = Gtk.FontDialogButton(dialog=Gtk.FontDialog())
        self.watermark_font_family_button.set_level(Gtk.FontLevel.FAMILY)
        self.watermark_font_family_button.set_valign(Gtk.Align.CENTER)
        self.watermark_font_family_button.set_font_desc(
            Pango.FontDescription.from_string(self.settings_manager.get("interface.watermark_font_family"))
        )
        watermark_font_family_row.add_suffix(self.watermark_font_family_button)
        watermark_font_family_row.set_activatable_widget(self.watermark_font_family_button)
        group_watermark.add(watermark_font_family_row)

        watermark_color_row = Adw.ActionRow(title=_("Color"))
        self.watermark_color_button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        watermark_color_rgba = Gdk.RGBA()
        watermark_color_rgba.parse(self.settings_manager.get("interface.watermark_color"))
        self.watermark_color_button.set_rgba(watermark_color_rgba)
        watermark_color_row.add_suffix(self.watermark_color_button)
        watermark_color_row.set_activatable_widget(self.watermark_color_button)
        group_watermark.add(watermark_color_row)

        self.watermark_opacity_row = Adw.SpinRow(
            title=_("Opacity (%)"),
            adjustment=Gtk.Adjustment(lower=1, upper=100, step_increment=1)
        )
        self.watermark_opacity_row.set_value(self.settings_manager.get("interface.watermark_opacity"))
        group_watermark.add(self.watermark_opacity_row)

        watermark_scope_model = Gtk.StringList.new([_("Active terminal only"), _("All panes")])
        self.watermark_scope_row = Adw.ComboRow(title=_("Show on"), model=watermark_scope_model)
        watermark_scope_map = {"active": 0, "all": 1}
        self.watermark_scope_row.set_selected(
            watermark_scope_map.get(self.settings_manager.get("interface.watermark_scope"), 0)
        )
        group_watermark.add(self.watermark_scope_row)

        # 100 ("Off") first, then 90..10 — index into this list IS the
        # percentage to multiply the base font size by while any split
        # layout is active; 100 means "don't shrink at all".
        self._watermark_shrink_percents = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]
        watermark_shrink_model = Gtk.StringList.new(
            [_("Off")] + [f"{p}%" for p in self._watermark_shrink_percents[1:]]
        )
        self.watermark_shrink_row = Adw.ComboRow(
            title=_("Shrink in split view"),
            subtitle=_("Scale the size down to this percent while any split layout is active"),
            model=watermark_shrink_model,
        )
        try:
            shrink_index = self._watermark_shrink_percents.index(
                self.settings_manager.get("interface.watermark_shrink_percent")
            )
        except ValueError:
            shrink_index = 0
        self.watermark_shrink_row.set_selected(shrink_index)
        group_watermark.add(self.watermark_shrink_row)

        # --- Adaptive Watermarks: regex rules that override the plain
        # color/opacity above when the rendered watermark text matches —
        # e.g. red whenever it contains "root". Ordered list, topmost
        # match wins (see _resolve_watermark_color_and_opacity in
        # window.py) — "root@arbe-svc053" matches both a "root" rule and
        # an "arbe" rule, but stays red if "root" is listed first. No
        # native GTK "reorder a PreferencesGroup row" primitive exists, so
        # moving a rule up/down just removes+re-adds every row in the new
        # order (_rebuild_watermark_rules_group) rather than fighting for one.
        group_watermark_rules = Adw.PreferencesGroup(
            title=_("Adaptive Watermarks"),
            description=_("Override the color/opacity above when the watermark text matches a pattern. "
                           "The topmost matching rule wins — put more specific patterns above more general ones."),
        )
        page_terminal.add(group_watermark_rules)

        self._watermark_rule_rows = []  # [{"row_widget", "pattern_row", "color_button", "opacity_spin"}, ...]
        self._watermark_add_rule_row = None

        def _validate_watermark_pattern(entry_row):
            try:
                re.compile(entry_row.get_text())
                entry_row.remove_css_class("error")
            except re.error:
                entry_row.add_css_class("error")

        def _rebuild_watermark_rules_group():
            for state in self._watermark_rule_rows:
                group_watermark_rules.remove(state["row_widget"])
            if self._watermark_add_rule_row is not None:
                group_watermark_rules.remove(self._watermark_add_rule_row)
            for state in self._watermark_rule_rows:
                group_watermark_rules.add(state["row_widget"])
            if self._watermark_add_rule_row is not None:
                group_watermark_rules.add(self._watermark_add_rule_row)

        def _clear_watermark_rule_rows():
            for state in list(self._watermark_rule_rows):
                group_watermark_rules.remove(state["row_widget"])
            self._watermark_rule_rows.clear()

        def _move_watermark_rule(state, delta):
            idx = self._watermark_rule_rows.index(state)
            new_idx = idx + delta
            if not (0 <= new_idx < len(self._watermark_rule_rows)):
                return
            rows = self._watermark_rule_rows
            rows[idx], rows[new_idx] = rows[new_idx], rows[idx]
            _rebuild_watermark_rules_group()

        def add_watermark_rule_row(existing=None):
            row = Adw.EntryRow(title=_("Pattern (regex)"))
            row.set_text((existing or {}).get("pattern", ""))
            row.connect("notify::text", lambda r, _p: _validate_watermark_pattern(r))

            color_button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            color_button.set_valign(Gtk.Align.CENTER)
            color_button.set_tooltip_text(_("Color"))
            color_rgba = Gdk.RGBA()
            color_rgba.parse((existing or {}).get("color") or "#ff0000")
            color_button.set_rgba(color_rgba)
            row.add_suffix(color_button)

            opacity_spin = Gtk.SpinButton(
                adjustment=Gtk.Adjustment(value=(existing or {}).get("opacity", 15), lower=1, upper=100, step_increment=1)
            )
            opacity_spin.set_valign(Gtk.Align.CENTER)
            opacity_spin.set_tooltip_text(_("Opacity (%)"))
            row.add_suffix(opacity_spin)

            up_button = Gtk.Button(icon_name="go-up-symbolic")
            up_button.add_css_class("flat")
            up_button.set_valign(Gtk.Align.CENTER)
            up_button.set_tooltip_text(_("Move up (higher priority)"))
            row.add_suffix(up_button)

            down_button = Gtk.Button(icon_name="go-down-symbolic")
            down_button.add_css_class("flat")
            down_button.set_valign(Gtk.Align.CENTER)
            down_button.set_tooltip_text(_("Move down (lower priority)"))
            row.add_suffix(down_button)

            remove_button = Gtk.Button(icon_name="user-trash-symbolic")
            remove_button.add_css_class("flat")
            remove_button.set_valign(Gtk.Align.CENTER)
            remove_button.set_tooltip_text(_("Remove"))
            row.add_suffix(remove_button)

            row_state = {"row_widget": row, "pattern_row": row, "color_button": color_button, "opacity_spin": opacity_spin}
            up_button.connect("clicked", lambda _b: _move_watermark_rule(row_state, -1))
            down_button.connect("clicked", lambda _b: _move_watermark_rule(row_state, 1))

            def on_remove(_btn, state=row_state):
                group_watermark_rules.remove(state["row_widget"])
                self._watermark_rule_rows.remove(state)
            remove_button.connect("clicked", on_remove)

            self._watermark_rule_rows.append(row_state)
            group_watermark_rules.add(row)
            if self._watermark_add_rule_row is not None:
                group_watermark_rules.remove(self._watermark_add_rule_row)
                group_watermark_rules.add(self._watermark_add_rule_row)
            _validate_watermark_pattern(row)

        for rule in self.settings_manager.get("interface.watermark_rules") or []:
            add_watermark_rule_row(rule)

        add_watermark_rule_action_row = Adw.ActionRow(title=_("Add Rule"))
        add_watermark_rule_action_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_watermark_rule_action_row.set_activatable(True)
        add_watermark_rule_action_row.connect("activated", lambda row: add_watermark_rule_row())
        group_watermark_rules.add(add_watermark_rule_action_row)
        self._watermark_add_rule_row = add_watermark_rule_action_row
        # Exposed so on_reset can clear the list back to empty without
        # duplicating this closure's logic.
        self._add_watermark_rule_row = add_watermark_rule_row
        self._clear_watermark_rule_rows = _clear_watermark_rule_rows

        # Grey out the "how" rows while the watermark is off — same idiom
        # as _update_ai_sensitivity above, minus the enable switch itself.
        watermark_detail_widgets = [
            self.watermark_text_row, watermark_text_note, self.watermark_position_row,
            self.watermark_font_size_row, watermark_font_family_row, watermark_color_row,
            self.watermark_opacity_row, self.watermark_scope_row, self.watermark_shrink_row,
            group_watermark_rules,
        ]
        def _update_watermark_sensitivity(*_args):
            enabled = self.watermark_enabled_row.get_active()
            for widget in watermark_detail_widgets:
                widget.set_sensitive(enabled)
        self.watermark_enabled_row.connect("notify::active", _update_watermark_sensitivity)
        _update_watermark_sensitivity()

        # --- Client Options Page ---
        page_client = Adw.PreferencesPage()
        page_client.set_title(_("Client Options"))
        page_client.set_icon_name("network-wired-symbolic")

        group_paths = Adw.PreferencesGroup(title=_("Executable Paths"))
        page_client.add(group_paths)

        self.ssh_path_row = Adw.EntryRow(title=_("SSH Client Path"))
        self.ssh_path_row.set_text(self.settings_manager.get("client.ssh_path"))
        group_paths.add(self.ssh_path_row)

        self.sshpass_path_row = Adw.EntryRow(title=_("sshpass Client Path"))
        self.sshpass_path_row.set_text(self.settings_manager.get("client.sshpass_path"))
        group_paths.add(self.sshpass_path_row)

        self.telnet_path_row = Adw.EntryRow(title=_("Telnet Client Path"))
        self.telnet_path_row.set_text(self.settings_manager.get("client.telnet_path"))
        group_paths.add(self.telnet_path_row)

        group_logging = Adw.PreferencesGroup(
            title=_("Logging"),
            description=_("Used for hosts with \"Save session log\" enabled. Leave empty to use "
                           "the .config_path pointer's terminal_logs_path, or the config directory "
                           "if that's empty too.")
        )
        page_client.add(group_logging)

        self.log_dir_row = Adw.EntryRow(title=_("Log Directory"))
        self.log_dir_row.set_text(self.settings_manager.get("client.log_dir"))
        log_dir_button = Gtk.Button(icon_name="folder-open-symbolic")
        log_dir_button.set_valign(Gtk.Align.CENTER)
        log_dir_button.set_tooltip_text(_("Choose a folder"))
        log_dir_button.connect("clicked", self.on_choose_log_dir_clicked)
        self.log_dir_row.add_suffix(log_dir_button)
        group_logging.add(self.log_dir_row)

        # --- Sync Page ---
        page_sync = Adw.PreferencesPage()
        page_sync.set_title(_("Sync"))
        page_sync.set_icon_name("emblem-synchronizing-symbolic")

        group_sync_main = Adw.PreferencesGroup(
            title=_("Sync"),
            description=_("Keeps hosts/Quickies/AI chats/user commands/general/terminal settings in step "
                           "across machines via a plain shared folder — Dropbox, iCloud, a network share, "
                           "or just another local directory. The app never talks to any cloud API directly, "
                           "it only reads/writes files inside whatever folder you point it at below."),
        )
        page_sync.add(group_sync_main)

        self.sync_enabled_row = Adw.SwitchRow(title=_("Enable Sync"))
        self.sync_enabled_row.set_active(self.settings_manager.get("sync.enabled"))
        group_sync_main.add(self.sync_enabled_row)

        self.sync_folder_row = Adw.EntryRow(title=_("Sync Folder"))
        self.sync_folder_row.set_text(self.settings_manager.get("sync.folder"))
        sync_folder_button = Gtk.Button(icon_name="folder-open-symbolic")
        sync_folder_button.set_valign(Gtk.Align.CENTER)
        sync_folder_button.set_tooltip_text(_("Choose a folder"))
        sync_folder_button.connect("clicked", self.on_choose_sync_dir_clicked)
        self.sync_folder_row.add_suffix(sync_folder_button)
        group_sync_main.add(self.sync_folder_row)

        self.sync_interval_row = Adw.SpinRow(
            title=_("Sync Interval (seconds)"),
            subtitle=_("Minimum 60 seconds"),
            adjustment=Gtk.Adjustment(
                value=self.settings_manager.get("sync.interval_seconds"), lower=60, upper=86400, step_increment=30
            ),
        )
        group_sync_main.add(self.sync_interval_row)

        group_sync_categories = Adw.PreferencesGroup(title=_("What to Sync"))
        page_sync.add(group_sync_categories)

        self.sync_hosts_row = Adw.SwitchRow(title=_("Hosts"))
        self.sync_hosts_row.set_active(self.settings_manager.get("sync.sync_hosts"))
        group_sync_categories.add(self.sync_hosts_row)

        self.sync_quickies_row = Adw.SwitchRow(title=_("Quickies"))
        self.sync_quickies_row.set_active(self.settings_manager.get("sync.sync_quickies"))
        group_sync_categories.add(self.sync_quickies_row)

        self.sync_ai_chats_row = Adw.SwitchRow(title=_("AI Chats"))
        self.sync_ai_chats_row.set_active(self.settings_manager.get("sync.sync_ai_chats"))
        group_sync_categories.add(self.sync_ai_chats_row)

        self.sync_user_commands_row = Adw.SwitchRow(title=_("User Commands"))
        self.sync_user_commands_row.set_active(self.settings_manager.get("sync.sync_user_commands"))
        group_sync_categories.add(self.sync_user_commands_row)

        self.sync_general_row = Adw.SwitchRow(title=_("General"))
        self.sync_general_row.set_active(self.settings_manager.get("sync.sync_general"))
        group_sync_categories.add(self.sync_general_row)

        self.sync_terminal_row = Adw.SwitchRow(title=_("Terminal Settings"), subtitle=_("Colors, watermarks"))
        self.sync_terminal_row.set_active(self.settings_manager.get("sync.sync_terminal"))
        group_sync_categories.add(self.sync_terminal_row)

        group_sync_status = Adw.PreferencesGroup()
        page_sync.add(group_sync_status)

        sync_status_row = Adw.ActionRow(title=_("Force Sync Now"))
        self.sync_status_label = Gtk.Label(xalign=1)
        self.sync_status_label.add_css_class("dim-label")
        # sync.last_sync_error can be an arbitrarily long human-readable
        # message (see settings_sync.py's own safety-check errors) — a
        # plain unbounded Label demands however much width the full string
        # needs on one line, and Adw.ViewStack sizes itself homogeneously
        # across ALL its pages by default, not just whichever one is
        # visible. One long error here was enough to blow out the whole
        # Settings window's width even while looking at a different page.
        # Capped + ellipsized, with the full text still reachable via
        # tooltip, so no error message can ever do that again.
        self.sync_status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.sync_status_label.set_max_width_chars(40)
        sync_status_row.add_suffix(self.sync_status_label)
        force_sync_button = Gtk.Button(label=_("Sync Now"), css_classes=["suggested-action"])
        force_sync_button.set_valign(Gtk.Align.CENTER)
        force_sync_button.connect("clicked", self.on_force_sync_clicked)
        sync_status_row.add_suffix(force_sync_button)
        group_sync_status.add(sync_status_row)
        self._refresh_sync_status_label()

        # --- User Commands Page (Placeholder) ---
        page_commands = Adw.PreferencesPage()
        page_commands.set_title(_("User Commands"))
        page_commands.set_icon_name("document-edit-symbolic")

        group_commands = Adw.PreferencesGroup(title=_("Custom Commands"))
        page_commands.add(group_commands)

        # hexpand=True end to end (box -> scroller -> treeview) plus an
        # explicit minimum width on the scroller: a plain Gtk.Box added to
        # an Adw.PreferencesGroup doesn't get the automatic full-row-width
        # treatment Adw.PreferencesRow subclasses (EntryRow, SwitchRow, ...)
        # do, and — unlike what hexpand alone would suggest — Adw's own
        # width-limiting AdwClamp only ever *caps* a page's natural width,
        # it never stretches undersized content to fill leftover space; the
        # only thing hexpand affects is how space *beyond* every child's
        # own natural/minimum size gets divided up, which isn't in play if
        # nothing on the page asked for more room in the first place. A
        # page like Terminal ends up wider simply because ITS content
        # (font button, 16-swatch custom palette grid, ...) genuinely needs
        # that much room; this page's rows don't unless told to, hence the
        # explicit size_request below sized to roughly match.
        commands_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        scrolled_view = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scrolled_view.set_size_request(780, 220)

        self.commands_store = Gtk.ListStore(str, str)
        # Populate from settings
        for cmd in self.settings_manager.get("user_commands"):
            self.commands_store.append([cmd.get("name", ""), cmd.get("command", "")])

        self.commands_view = Gtk.TreeView(model=self.commands_store, hexpand=True)

        renderer_name = Gtk.CellRendererText(editable=True)
        renderer_name.connect("edited", self.on_command_edited, 0)
        col_name = Gtk.TreeViewColumn(_("Name"), renderer_name, text=0)
        self.commands_view.append_column(col_name)

        renderer_cmd = Gtk.CellRendererText(editable=True)
        renderer_cmd.connect("edited", self.on_command_edited, 1)
        col_cmd = Gtk.TreeViewColumn(_("Command"), renderer_cmd, text=1)
        col_cmd.set_expand(True)  # the command itself is what's worth the extra room, not its name
        self.commands_view.append_column(col_cmd)

        scrolled_view.set_child(self.commands_view)
        commands_box.append(scrolled_view)

        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.START)
        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.connect("clicked", self.on_add_command)
        remove_button = Gtk.Button(icon_name="list-remove-symbolic")
        remove_button.connect("clicked", self.on_remove_command)
        buttons_box.append(add_button)
        buttons_box.append(remove_button)
        commands_box.append(buttons_box)

        note_label = Gtk.Label(
            label=_("Commands for the host's context menu.\nAvailable variables: $name, $host, $user"),
            halign=Gtk.Align.START,
            css_classes=["dim-label"] # Make it less prominent
        )
        commands_box.append(note_label)

        group_commands.add(commands_box)

        # --- Quickies Page (insert-not-execute text snippets — the header-
        # bar button next to the split/watermark buttons is the same live
        # on/off switch as the row below, same pairing as the watermark) ---
        page_quickies = Adw.PreferencesPage()
        page_quickies.set_title(_("Quickies"))
        page_quickies.set_icon_name("insert-text-symbolic")

        group_quickies_settings = Adw.PreferencesGroup(title=_("Panel"))
        page_quickies.add(group_quickies_settings)

        self.quickies_enabled_row = Adw.SwitchRow(
            title=_("Enabled on start"),
            subtitle=_("Whether the Quickies panel is shown when the app launches — same switch as the header-bar button")
        )
        self.quickies_enabled_row.set_active(self.settings_manager.get("quickies.enabled"))
        group_quickies_settings.add(self.quickies_enabled_row)

        quickies_position_model = Gtk.StringList.new([_("Above hosts"), _("Below hosts")])
        self.quickies_position_row = Adw.ComboRow(title=_("Position"), model=quickies_position_model)
        quickies_position_map = {"above": 0, "below": 1}
        self.quickies_position_row.set_selected(
            quickies_position_map.get(self.settings_manager.get("quickies.position"), 1)
        )
        group_quickies_settings.add(self.quickies_position_row)

        quickies_search_position_model = Gtk.StringList.new([_("Top"), _("Bottom")])
        self.quickies_search_position_row = Adw.ComboRow(title=_("Search bar position"), model=quickies_search_position_model)
        quickies_search_position_map = {"top": 0, "bottom": 1}
        self.quickies_search_position_row.set_selected(
            quickies_search_position_map.get(self.settings_manager.get("quickies.search_position"), 1)
        )
        group_quickies_settings.add(self.quickies_search_position_row)

        group_quickies_items = Adw.PreferencesGroup(title=_("Snippets"))
        page_quickies.add(group_quickies_items)

        # Same situation and same fix as User Commands' commands_box above
        # (see its comment) — a plain Gtk.Box in a PreferencesGroup, sized
        # explicitly since nothing here would otherwise ask for more room
        # than its own modest content needs.
        quickies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        quickies_scrolled_view = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        quickies_scrolled_view.set_size_request(780, 220)

        self.quickies_store = Gtk.ListStore(str, str)
        for quicky in self.settings_manager.get("quickies.items"):
            self.quickies_store.append([quicky.get("name", ""), quicky.get("text", "")])

        self.quickies_view = Gtk.TreeView(model=self.quickies_store, hexpand=True)

        renderer_quicky_name = Gtk.CellRendererText(editable=True)
        renderer_quicky_name.connect("edited", self.on_quicky_row_edited, 0)
        col_quicky_name = Gtk.TreeViewColumn(_("Name"), renderer_quicky_name, text=0)
        self.quickies_view.append_column(col_quicky_name)

        renderer_quicky_text = Gtk.CellRendererText(editable=True)
        renderer_quicky_text.connect("edited", self.on_quicky_row_edited, 1)
        col_quicky_text = Gtk.TreeViewColumn(_("Text"), renderer_quicky_text, text=1)
        col_quicky_text.set_expand(True)  # the snippet body is what's worth seeing more of, not its name
        self.quickies_view.append_column(col_quicky_text)

        quickies_scrolled_view.set_child(self.quickies_view)
        quickies_box.append(quickies_scrolled_view)

        quickies_buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.START)
        quickies_add_button = Gtk.Button(icon_name="list-add-symbolic")
        quickies_add_button.connect("clicked", self.on_add_quicky_row)
        quickies_remove_button = Gtk.Button(icon_name="list-remove-symbolic")
        quickies_remove_button.connect("clicked", self.on_remove_quicky_row)
        quickies_buttons_box.append(quickies_add_button)
        quickies_buttons_box.append(quickies_remove_button)
        quickies_box.append(quickies_buttons_box)

        quickies_note_label = Gtk.Label(
            label=_("Inserted into the active terminal, not executed.\nAvailable variables: $name, $host, $user"),
            halign=Gtk.Align.START,
            css_classes=["dim-label"]
        )
        quickies_box.append(quickies_note_label)

        group_quickies_items.add(quickies_box)

        # --- General Page (app icon, host tree look & feel, debug logging) ---
        page_interface = Adw.PreferencesPage()
        page_interface.set_title(_("General"))
        page_interface.set_icon_name("preferences-desktop-appearance-symbolic")

        group_logo = Adw.PreferencesGroup(title=_("Logo"))
        page_interface.add(group_logo)

        # (label, settings value / icon file stem without extension)
        self._icon_options = [
            (_("Safe"), "thongssh"),
            (_("Original"), "thongssh_orig"),
        ]
        icon_labels = [label for label, stem in self._icon_options]
        self.icon_row = Adw.ComboRow(title=_("App Icon"), model=Gtk.StringList.new(icon_labels))
        current_icon_stem = self.settings_manager.get("interface.icon")
        try:
            current_icon_index = [stem for label, stem in self._icon_options].index(current_icon_stem)
        except ValueError:
            current_icon_index = 0
        self.icon_row.set_selected(current_icon_index)
        group_logo.add(self.icon_row)

        group_host_tree = Adw.PreferencesGroup(title=_("Host Tree"))
        page_interface.add(group_host_tree)

        self.tree_row_striping_row = Adw.SwitchRow(
            title=_("Alternating row highlight"),
            subtitle=_("Tint every other row with a faint accent-color wash")
        )
        self.tree_row_striping_row.set_active(self.settings_manager.get("interface.tree_row_striping"))
        group_host_tree.add(self.tree_row_striping_row)

        search_position_model = Gtk.StringList.new([_("Top"), _("Bottom")])
        self.search_position_row = Adw.ComboRow(title=_("Search bar position"), model=search_position_model)
        search_position_map = {"top": 0, "bottom": 1}
        self.search_position_row.set_selected(
            search_position_map.get(self.settings_manager.get("interface.host_search_position"), 1)
        )
        group_host_tree.add(self.search_position_row)

        group_debug = Adw.PreferencesGroup(title=_("Debugging"))
        page_interface.add(group_debug)

        self.debug_mode_row = Adw.SwitchRow(
            title=_("Enable debug logging"),
            subtitle=_("Print verbose debug messages to the console. Leave off unless troubleshooting an issue.")
        )
        self.debug_mode_row.set_active(self.settings_manager.get("interface.debug_mode"))
        group_debug.add(self.debug_mode_row)

        # --- Shortcuts Page ---
        # Every keyboard shortcut this app used to hardcode, made
        # configurable — several collide with standard shell keybindings
        # on some setups (Ctrl+W deletes the last word in bash/zsh's own
        # line editing, for one). See window.py's _shortcut_matches for
        # where these actually get read.
        page_shortcuts = Adw.PreferencesPage()
        page_shortcuts.set_title(_("Shortcuts"))
        page_shortcuts.set_icon_name("preferences-desktop-keyboard-shortcuts-symbolic")

        group_shortcuts = Adw.PreferencesGroup(
            title=_("Keyboard Shortcuts"),
            description=_(
                "Click a shortcut, then press the new key combination — Escape cancels, "
                "leaving it unchanged."
            ),
        )
        page_shortcuts.add(group_shortcuts)

        self._shortcut_pickers = {}
        for key, label in [
            ("shortcuts.close_tab", _("Close Tab")),
            ("shortcuts.focus_search", _("Focus Host Search")),
            ("shortcuts.find_in_terminal", _("Find in Terminal")),
            ("shortcuts.copy", _("Copy")),
            ("shortcuts.paste", _("Paste")),
        ]:
            row = Adw.ActionRow(title=label)
            picker = ShortcutPicker(self.settings_manager.get(key))
            picker.set_valign(Gtk.Align.CENTER)
            row.add_suffix(picker)
            group_shortcuts.add(row)
            self._shortcut_pickers[key] = picker

        # --- SFTP Page ---
        page_sftp = Adw.PreferencesPage()
        page_sftp.set_title(_("SFTP"))
        page_sftp.set_icon_name("folder-remote-symbolic")

        group_sftp_local = Adw.PreferencesGroup(title=_("Local Panel"))
        page_sftp.add(group_sftp_local)

        self.sftp_path_row = Adw.EntryRow(title=_("Default Local Path"))
        self.sftp_path_row.set_text(self.settings_manager.get("sftp.local_default_path"))
        group_sftp_local.add(self.sftp_path_row)

        sort_col_model = Gtk.StringList.new([_("Name"), _("Size"), _("Date")])
        self.sftp_sort_col_row = Adw.ComboRow(title=_("Default Sort Column"), model=sort_col_model)
        sort_col_map = {"name": 0, "size": 1, "date": 2}
        self.sftp_sort_col_row.set_selected(sort_col_map.get(self.settings_manager.get("sftp.local_default_sort_column"), 0))
        group_sftp_local.add(self.sftp_sort_col_row)

        sort_dir_model = Gtk.StringList.new([_("Ascending"), _("Descending")])
        self.sftp_sort_dir_row = Adw.ComboRow(title=_("Default Sort Direction"), model=sort_dir_model)
        sort_dir_map = {"asc": 0, "desc": 1}
        self.sftp_sort_dir_row.set_selected(sort_dir_map.get(self.settings_manager.get("sftp.local_default_sort_direction"), 0))
        group_sftp_local.add(self.sftp_sort_dir_row)

        group_sftp_remote = Adw.PreferencesGroup(title=_("Remote Panel"))
        page_sftp.add(group_sftp_remote)

        self.sftp_remote_sort_col_row = Adw.ComboRow(title=_("Default Sort Column"), model=sort_col_model) # Reuse model
        remote_sort_col_map = {"name": 0, "size": 1, "date": 2}
        self.sftp_remote_sort_col_row.set_selected(remote_sort_col_map.get(self.settings_manager.get("sftp.remote_default_sort_column"), 0))
        group_sftp_remote.add(self.sftp_remote_sort_col_row)

        self.sftp_remote_sort_dir_row = Adw.ComboRow(title=_("Default Sort Direction"), model=sort_dir_model) # Reuse model
        remote_sort_dir_map = {"asc": 0, "desc": 1}
        self.sftp_remote_sort_dir_row.set_selected(remote_sort_dir_map.get(self.settings_manager.get("sftp.remote_default_sort_direction"), 0))
        group_sftp_remote.add(self.sftp_remote_sort_dir_row)

        # --- AI Page: shared settings up top, then API/CLI Client as
        # actual nested tabs underneath (not separate top-level sidebar
        # entries) — Adw.PreferencesPage only accepts PreferencesGroup
        # children, so the shared prefs live in their own PreferencesPage
        # and the two provider-specific ones are switched via a plain
        # Gtk.Stack + Gtk.StackSwitcher, all wrapped in one outer Gtk.Box.
        page_ai = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        page_ai_shared = Adw.PreferencesPage()
        # AdwPreferencesPage's internal ScrolledWindow defaults to
        # vexpand=True (it's normally a whole page on its own). Left as-is
        # here, it competed with the sub-tab Gtk.Stack below (which also
        # needs vexpand) for space in the shared parent Gtk.Box, squeezing
        # this page below its own content's natural height and clipping
        # the Connection group. It only needs its own (small, fixed)
        # natural height — all the *extra* space belongs to the sub-tabs.
        page_ai_shared.set_vexpand(False)
        self.page_ai_shared_widget = page_ai_shared

        # Read once up front — also gates whether the CLI tab below even
        # bothers probing PATH for claude/codex (see cli_is_available call
        # further down): with AI disabled, opening Settings should cause
        # zero AI-related I/O, not just hide the result of it.
        ai_disabled_initial = bool(self.settings_manager.get("ai.disabled"))

        group_ai_master = Adw.PreferencesGroup()
        page_ai_shared.add(group_ai_master)

        self.ai_disabled_row = Adw.SwitchRow(
            title=_("Disable AI"),
            subtitle=_("Turns the feature off entirely: no header-bar buttons, no local CLI tool "
                       "detection, no keyring lookups, no requests of any kind."),
        )
        self.ai_disabled_row.set_active(ai_disabled_initial)
        group_ai_master.add(self.ai_disabled_row)

        group_ai_prompt = Adw.PreferencesGroup(
            title=_("System Prompt"),
            description=_("Shared by every provider on the API and CLI Client tabs below — sent as the system/initial prompt for every conversation."),
        )
        page_ai_shared.add(group_ai_prompt)

        self.ai_system_prompt_row = Adw.EntryRow(title=_("Initial prompt"))
        self.ai_system_prompt_row.set_text(self.settings_manager.get("ai.system_prompt") or "")
        reset_prompt_button = Gtk.Button(icon_name="edit-undo-symbolic")
        reset_prompt_button.set_valign(Gtk.Align.CENTER)
        reset_prompt_button.add_css_class("flat")
        reset_prompt_button.set_tooltip_text(_("Reset to default"))
        reset_prompt_button.connect(
            "clicked",
            lambda _b: self.ai_system_prompt_row.set_text(DEFAULT_SETTINGS["ai.system_prompt"]),
        )
        self.ai_system_prompt_row.add_suffix(reset_prompt_button)
        self.ai_system_prompt_reset_button = reset_prompt_button
        group_ai_prompt.add(self.ai_system_prompt_row)

        group_ai_connection = Adw.PreferencesGroup(
            title=_("Connection"),
            description=_("Shared by every provider — raise it if requests to a slower/local model keep timing out."),
        )
        page_ai_shared.add(group_ai_connection)

        self.ai_timeout_row = Adw.SpinRow(
            title=_("Request timeout (seconds)"),
            adjustment=Gtk.Adjustment(
                value=self.settings_manager.get("ai.request_timeout_seconds"),
                lower=10, upper=1800, step_increment=10,
            ),
        )
        group_ai_connection.add(self.ai_timeout_row)

        page_ai.append(page_ai_shared)

        # --- API Page (HTTP-based AI providers) ---
        page_api = Adw.PreferencesPage()
        page_api.set_title(_("API"))
        page_api.set_icon_name("network-workgroup-symbolic")

        # Read once, up here, since both the custom and standard provider
        # loops below need it (the custom one is built first).
        provider_model_overrides = self.settings_manager.get("ai.provider_models") or {}

        # Custom providers first — a user with only local/manual endpoints
        # configured shouldn't have to scroll past the 5 standard ones to
        # reach them.
        group_ai_custom = Adw.PreferencesGroup(
            title=_("Custom Providers"),
            description=_("Any OpenAI-compatible chat completions endpoint (self-hosted, OpenRouter, etc.)."),
        )
        page_api.add(group_ai_custom)

        self._ai_custom_rows = []  # [{"id", "expander", "name_row", "url_row", "key_row"}, ...]
        self._ai_add_custom_row = None

        def add_custom_provider_row(existing=None):
            custom_id = (existing or {}).get("id") or str(uuid.uuid4())
            expander = Adw.ExpanderRow(title=(existing or {}).get("name") or _("New Provider"))

            name_row = Adw.EntryRow(title=_("Name"))
            name_row.set_text((existing or {}).get("name", ""))
            name_row.connect("notify::text", lambda row, _p: expander.set_title(row.get_text() or _("New Provider")))
            expander.add_row(name_row)

            url_row = Adw.EntryRow(title=_("Base URL"))
            url_row.set_text((existing or {}).get("base_url", ""))
            expander.add_row(url_row)

            key_row = Adw.PasswordEntryRow(title=_("API Key (optional)"))
            if existing:
                saved_key = self.keyring.load_password(f"ai:custom:{custom_id}")
                if saved_key:
                    key_row.set_text(saved_key)
            expander.add_row(key_row)

            model_row = Adw.EntryRow(title=_("Model"))
            model_row.set_text(provider_model_overrides.get(f"custom:{custom_id}", ""))
            model_row.add_suffix(self._build_fetched_model_picker_button(
                "custom", key_row, lambda url_row=url_row: url_row.get_text().strip(), model_row
            ))
            expander.add_row(model_row)

            remove_button = Gtk.Button(icon_name="user-trash-symbolic")
            remove_button.set_valign(Gtk.Align.CENTER)
            remove_button.add_css_class("flat")
            remove_button.set_tooltip_text(_("Remove"))
            expander.add_suffix(remove_button)

            row_state = {"id": custom_id, "expander": expander, "name_row": name_row,
                         "url_row": url_row, "key_row": key_row, "model_row": model_row}

            def on_remove(_btn, state=row_state):
                group_ai_custom.remove(state["expander"])
                self._ai_custom_rows.remove(state)
            remove_button.connect("clicked", on_remove)

            group_ai_custom.add(expander)
            self._ai_custom_rows.append(row_state)

            # Keep the "Add" row pinned at the bottom — PreferencesGroup only
            # appends, it has no insert-before-index API.
            if self._ai_add_custom_row is not None:
                group_ai_custom.remove(self._ai_add_custom_row)
                group_ai_custom.add(self._ai_add_custom_row)

        for cp in self.settings_manager.get("ai.custom_providers") or []:
            add_custom_provider_row(cp)

        add_custom_row = Adw.ActionRow(title=_("Add Custom Provider"))
        add_custom_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_custom_row.set_activatable(True)
        add_custom_row.connect("activated", lambda row: add_custom_provider_row())
        group_ai_custom.add(add_custom_row)
        self._ai_add_custom_row = add_custom_row

        group_ai_standard = Adw.PreferencesGroup(
            title=_("Providers"),
            description=_("A header-bar button appears for each provider with a key saved here."),
        )
        page_api.add(group_ai_standard)

        self._ai_key_rows = {}
        self._ai_model_rows = {}
        for provider_id, label in AI_STANDARD_PROVIDERS:
            expander = Adw.ExpanderRow(title=label)

            key_row = Adw.PasswordEntryRow(title=_("API Key"))
            existing_key = self.keyring.load_password(f"ai:{provider_id}")
            if existing_key:
                key_row.set_text(existing_key)
            expander.add_row(key_row)

            model_row = Adw.EntryRow(title=_("Model"))
            model_row.set_text(provider_model_overrides.get(provider_id) or AI_DEFAULT_MODELS.get(provider_id, ""))
            model_row.add_suffix(self._build_fetched_model_picker_button(
                provider_id, key_row, lambda pid=provider_id: AI_DEFAULT_BASE_URLS.get(pid, ""), model_row
            ))
            expander.add_row(model_row)

            self._ai_key_rows[provider_id] = key_row
            self._ai_model_rows[provider_id] = model_row
            group_ai_standard.add(expander)

        # --- CLI Client Page (local CLI tools instead of an HTTP API) ---
        page_cli = Adw.PreferencesPage()
        page_cli.set_title(_("CLI Client"))
        page_cli.set_icon_name("utilities-terminal-symbolic")

        group_cli_standard = Adw.PreferencesGroup(
            title=_("Standard Tools"),
            description=_(
                "Runs the command fresh for every message and reads its stdout as the reply — "
                "install the tool yourself first (npm, etc.). \"{message}\" and \"{system_prompt}\" "
                "are each substituted with their own argument (never through a shell, so both are "
                "always safe regardless of content) — use \"{system_prompt}\" if the tool has its own "
                "flag for one (e.g. Claude's --append-system-prompt), otherwise it's prepended into "
                "the message automatically."
            ),
        )
        page_cli.add(group_cli_standard)

        cli_command_overrides = self.settings_manager.get("cli.commands") or {}
        cli_model_overrides = self.settings_manager.get("cli.provider_models") or {}
        self._cli_command_rows = {}
        self._cli_model_rows = {}
        for cli_id, cli_label, default_command in CLI_STANDARD_PROVIDERS:
            command = cli_command_overrides.get(cli_id) or default_command
            if ai_disabled_initial:
                subtitle = _("AI is disabled")
            elif cli_is_available(command):
                subtitle = _("Found on PATH")
            else:
                subtitle = _("Not found on PATH")
            expander = Adw.ExpanderRow(title=cli_label, subtitle=subtitle)
            command_row = Adw.EntryRow(title=_("Command"))
            command_row.set_text(command)
            expander.add_row(command_row)

            model_row = Adw.EntryRow(
                title=_("Model"),
                tooltip_text=_("Leave empty to use the tool's own default — no --model flag is sent at all."),
            )
            model_row.set_text(cli_model_overrides.get(cli_id, ""))
            picker_button = self._build_static_model_picker_button(CLI_MODEL_PRESETS.get(cli_id, []), model_row)
            if picker_button is not None:
                model_row.add_suffix(picker_button)
            expander.add_row(model_row)

            self._cli_command_rows[cli_id] = command_row
            self._cli_model_rows[cli_id] = model_row
            group_cli_standard.add(expander)

        group_cli_custom = Adw.PreferencesGroup(
            title=_("Custom Tools"),
            description=_("Any other locally-installed CLI (gemini-cli, aider, ...)."),
        )
        page_cli.add(group_cli_custom)

        self._cli_custom_rows = []  # [{"id", "expander", "name_row", "command_row"}, ...]
        self._cli_add_custom_row = None

        def add_cli_custom_row(existing=None):
            custom_id = (existing or {}).get("id") or str(uuid.uuid4())
            expander = Adw.ExpanderRow(title=(existing or {}).get("name") or _("New Tool"))

            name_row = Adw.EntryRow(title=_("Name"))
            name_row.set_text((existing or {}).get("name", ""))
            name_row.connect("notify::text", lambda row, _p: expander.set_title(row.get_text() or _("New Tool")))
            expander.add_row(name_row)

            command_row = Adw.EntryRow(title=_("Command"))
            command_row.set_text((existing or {}).get("command", ""))
            expander.add_row(command_row)

            model_row = Adw.EntryRow(
                title=_("Model"),
                tooltip_text=_("Leave empty to use the tool's own default — no --model flag is sent at all."),
            )
            model_row.set_text(cli_model_overrides.get(f"custom:{custom_id}", ""))
            expander.add_row(model_row)

            remove_button = Gtk.Button(icon_name="user-trash-symbolic")
            remove_button.set_valign(Gtk.Align.CENTER)
            remove_button.add_css_class("flat")
            remove_button.set_tooltip_text(_("Remove"))
            expander.add_suffix(remove_button)

            row_state = {"id": custom_id, "expander": expander, "name_row": name_row,
                         "command_row": command_row, "model_row": model_row}

            def on_remove(_btn, state=row_state):
                group_cli_custom.remove(state["expander"])
                self._cli_custom_rows.remove(state)
            remove_button.connect("clicked", on_remove)

            group_cli_custom.add(expander)
            self._cli_custom_rows.append(row_state)

            if self._cli_add_custom_row is not None:
                group_cli_custom.remove(self._cli_add_custom_row)
                group_cli_custom.add(self._cli_add_custom_row)

        for tool in self.settings_manager.get("cli.custom_tools") or []:
            add_cli_custom_row(tool)

        add_cli_row = Adw.ActionRow(title=_("Add Custom Tool"))
        add_cli_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_cli_row.set_activatable(True)
        add_cli_row.connect("activated", lambda row: add_cli_custom_row())
        group_cli_custom.add(add_cli_row)
        self._cli_add_custom_row = add_cli_row

        # Nested API / CLI Client sub-tabs underneath the shared prefs.
        ai_sub_stack = Gtk.Stack()
        ai_sub_stack.set_vexpand(True)
        ai_sub_stack.add_titled(page_api, "api", _("API"))
        ai_sub_stack.add_titled(page_cli, "cli", _("CLI Client"))

        ai_sub_switcher = Gtk.StackSwitcher()
        ai_sub_switcher.set_stack(ai_sub_stack)
        ai_sub_switcher.set_halign(Gtk.Align.CENTER)

        page_ai.append(ai_sub_switcher)
        page_ai.append(ai_sub_stack)

        # Grey out everything except the switch itself while AI is
        # disabled — the settings underneath are meaningless (and, for the
        # CLI tab, misleadingly stale) until it's turned back on.
        def _update_ai_sensitivity(*_args):
            enabled = not self.ai_disabled_row.get_active()
            group_ai_prompt.set_sensitive(enabled)
            group_ai_connection.set_sensitive(enabled)
            ai_sub_switcher.set_sensitive(enabled)
            ai_sub_stack.set_sensitive(enabled)
        self.ai_disabled_row.connect("notify::active", _update_ai_sensitivity)
        _update_ai_sensitivity()

        # page_ai is a composite (shared prefs + switcher + sub-tabs), not a
        # single AdwPreferencesPage — when the combined content is taller
        # than the dialog, a plain Gtk.Box's non-homogeneous layout hands
        # every child only its *minimum* height (not natural), since the
        # auto-created Viewport inside a ScrolledWindow defaults to
        # requesting MINIMUM size along the scroll axis from its child (the
        # whole point of scrolling is normally "show the minimum, scroll
        # for the rest"). That's what was clipping the Connection group:
        # the shared-prefs sub-page was being squeezed to its bare minimum
        # to make room for a tall Providers list below it. Wrapping it in
        # an explicit Viewport with vscroll-policy=NATURAL makes the
        # Viewport request (and allocate) the *natural* height instead, so
        # every child gets its full natural size and the excess (if any)
        # scrolls as a single, coherent whole.
        ai_viewport = Gtk.Viewport(child=page_ai)
        ai_viewport.set_vscroll_policy(Gtk.ScrollablePolicy.NATURAL)
        page_ai_scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        page_ai_scrolled.set_child(ai_viewport)

        sidebar = Gtk.ListBox()
        sidebar.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sidebar.get_style_context().add_class("navigation-sidebar")

        # Connect ListBox selection to Stack
        sidebar.connect("row-selected", lambda listbox, row: self.stack.set_visible_child_name(row.get_name()))

        # Add pages to stack and rows to sidebar — order here is the
        # sidebar's own order (built below from self.stack.get_pages(),
        # which iterates in add-order).
        self.stack.add_titled_with_icon(page_interface, "interface", _("General"), "preferences-desktop-appearance-symbolic")
        self.stack.add_titled_with_icon(page_shortcuts, "shortcuts", _("Shortcuts"), "preferences-desktop-keyboard-shortcuts-symbolic")
        self.stack.add_titled_with_icon(page_terminal, "terminal", _("Terminal"), "utilities-terminal-symbolic")
        self.stack.add_titled_with_icon(page_client, "client", _("Client Options"), "network-wired-symbolic")
        self.stack.add_titled_with_icon(page_commands, "commands", _("User Commands"), "document-edit-symbolic")
        self.stack.add_titled_with_icon(page_quickies, "quickies", _("Quickies"), "insert-text-symbolic")
        self.stack.add_titled_with_icon(page_sync, "sync", _("Sync"), "emblem-synchronizing-symbolic")
        self.stack.add_titled_with_icon(page_sftp, "sftp", _("SFTP"), "folder-remote-symbolic")
        # API and CLI Client are nested tabs *inside* this one "AI" entry
        # (see the Gtk.Stack/StackSwitcher built above) — not separate
        # top-level sidebar entries.
        self.stack.add_titled_with_icon(page_ai_scrolled, "ai", _("AI"), "dialog-messages-symbolic")

        for page in (page_terminal, page_sftp, page_client, page_commands, page_interface, page_shortcuts, page_ai_shared, page_api, page_cli, page_sync):
            _widen_preferences_clamp(page)

        for page in self.stack.get_pages():
            row = Adw.ActionRow(title=page.get_title())
            row.set_name(page.get_name())
            sidebar.append(row)

        split_view = Adw.NavigationSplitView(collapsed=False)
        split_view.set_sidebar(Adw.NavigationPage.new(sidebar, _("Settings")))
        split_view.set_content(Adw.NavigationPage.new(self.stack, _("Settings")))
        split_view.set_vexpand(True)

        header_bar.set_title_widget(Adw.WindowTitle(title=_("Settings")))
        apply_button = Gtk.Button(label=_("Apply"), css_classes=["suggested-action"])
        apply_button.connect("clicked", self.on_apply)
        header_bar.pack_end(apply_button)

        reset_button = Gtk.Button(label=_("Reset"))
        reset_button.connect("clicked", self.on_reset)
        header_bar.pack_start(reset_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        main_box.append(split_view)
        self.set_content(main_box)

    def _build_model_list_popover_button(self, populate):
        """Shared shell for the two "pick a model" buttons below — a
        Gtk.MenuButton whose popover is (re)filled by `populate(list_box)`
        every time it's opened, so it always reflects whatever's currently
        in the key/URL fields rather than a stale snapshot from when the
        dialog was built."""
        button = Gtk.MenuButton(icon_name="view-list-symbolic")
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(_("Choose from a list"))

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        list_box.set_margin_top(6)
        list_box.set_margin_bottom(6)
        list_box.set_margin_start(6)
        list_box.set_margin_end(6)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(240)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(list_box)

        popover = Gtk.Popover()
        popover.set_child(scroller)
        popover.connect("show", lambda _p: populate(list_box, popover))
        button.set_popover(popover)
        return button

    def _clear_box(self, box):
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def _fill_model_list_box(self, list_box, models, model_row, popover):
        self._clear_box(list_box)
        if not models:
            list_box.append(Gtk.Label(label=_("No models found.")))
            return
        for model_id in models:
            item_button = Gtk.Button(label=model_id)
            item_button.add_css_class("flat")
            item_button.get_child().set_xalign(0)

            def on_pick(_b, model_id=model_id):
                model_row.set_text(model_id)
                popover.popdown()
            item_button.connect("clicked", on_pick)
            list_box.append(item_button)

    def _build_static_model_picker_button(self, models, model_row):
        """The CLI-side model picker — a fixed suggestion list (there's no
        "list models" API for a local binary the way there is for an HTTP
        provider), same popover shell as the API one for a consistent feel.
        Returns None (no button at all) when there's nothing to suggest,
        rather than showing a picker that just says "no suggestions"."""
        if not models:
            return None
        def populate(list_box, popover):
            self._fill_model_list_box(list_box, models, model_row, popover)
        return self._build_model_list_popover_button(populate)

    def _build_fetched_model_picker_button(self, family, key_row, base_url_getter, model_row):
        """The API-side model picker — fetches the live list for whatever
        key is *currently typed* in key_row (not necessarily saved yet) the
        moment the popover is opened. Disabled while the key field is
        empty; manual entry into model_row always works regardless of
        whether fetching does."""
        button = self._build_model_list_popover_button(
            lambda list_box, popover: self._populate_fetched_models(
                list_box, popover, family, key_row.get_text().strip(), base_url_getter(), model_row
            )
        )
        button.set_sensitive(bool(key_row.get_text().strip()))
        key_row.connect("notify::text", lambda row, _p: button.set_sensitive(bool(row.get_text().strip())))
        return button

    def _populate_fetched_models(self, list_box, popover, family, api_key, base_url, model_row):
        self._clear_box(list_box)
        list_box.append(Gtk.Label(label=_("Loading…")))

        def on_models(models):
            self._fill_model_list_box(list_box, models, model_row, popover)
            return False

        def on_error(message):
            self._clear_box(list_box)
            error_label = Gtk.Label(
                label=_("Could not fetch models: {error}").format(error=message), wrap=True
            )
            error_label.add_css_class("error")
            list_box.append(error_label)
            return False

        fetch_models(family, api_key, base_url, on_models, on_error)

    def on_apply(self, button):
        """Save settings and close the window."""
        self.settings_manager.set("terminal.font", self.font_button.get_font())
        self.settings_manager.set("terminal.scrollback_lines", int(self.scrollback_row.get_value()))
        self.settings_manager.set("terminal.close_on_disconnect", self.close_on_disconnect_row.get_active())
        self.settings_manager.set("terminal.reconnect_prompt_username", self.reconnect_prompt_username_row.get_active())
        
        selected_idx = self.scheme_row.get_selected()
        base_scheme_key = _BASE_SCHEME_IDS[selected_idx]
        self.settings_manager.set("terminal.custom_scheme_base", base_scheme_key)

        if self.custom_scheme_switch.get_active():
            save_custom_color_scheme({
                "background": _rgba_to_hex(self.custom_bg_button.get_rgba()),
                "foreground": _rgba_to_hex(self.custom_fg_button.get_rgba()),
                "palette": [_rgba_to_hex(button.get_rgba()) for button in self.custom_palette_buttons],
            })
            self.settings_manager.set("terminal.color_scheme", "custom")
        else:
            self.settings_manager.set("terminal.color_scheme", base_scheme_key)

        self.settings_manager.set("client.ssh_path", self.ssh_path_row.get_text())
        self.settings_manager.set("client.telnet_path", self.telnet_path_row.get_text())
        self.settings_manager.set("client.sshpass_path", self.sshpass_path_row.get_text())
        self.settings_manager.set("client.log_dir", self.log_dir_row.get_text().strip())

        # User commands
        user_commands = []
        for row in self.commands_store:
            user_commands.append({"name": row[0], "command": row[1]})
        self.settings_manager.set("user_commands", user_commands)
        # Rebuild the host context menu's dynamic section now, not lazily on
        # next right-click — see build_user_commands_menu's own docstring
        # for why that lazy rebuild used to make the *first* right-click
        # after commands were added show a truncated/scrolled popover.
        self.parent_window.build_user_commands_menu()

        # Quickies
        self.settings_manager.set("quickies.enabled", self.quickies_enabled_row.get_active())
        quickies_position_map_rev = {0: "above", 1: "below"}
        self.settings_manager.set(
            "quickies.position",
            quickies_position_map_rev.get(self.quickies_position_row.get_selected(), "below")
        )
        quickies_search_position_map_rev = {0: "top", 1: "bottom"}
        self.settings_manager.set(
            "quickies.search_position",
            quickies_search_position_map_rev.get(self.quickies_search_position_row.get_selected(), "bottom")
        )
        quickies_items = []
        for row in self.quickies_store:
            quickies_items.append({"name": row[0], "text": row[1]})
        self.settings_manager.set("quickies.items", quickies_items)

        self.settings_manager.set("sftp.local_default_path", self.sftp_path_row.get_text())
        sort_col_map_rev = {0: "name", 1: "size", 2: "date"}
        self.settings_manager.set("sftp.local_default_sort_column", sort_col_map_rev.get(self.sftp_sort_col_row.get_selected(), "name"))
        sort_dir_map_rev = {0: "asc", 1: "desc"}
        self.settings_manager.set("sftp.local_default_sort_direction", sort_dir_map_rev.get(self.sftp_sort_dir_row.get_selected(), "asc"))

        self.settings_manager.set("sftp.remote_default_sort_column", sort_col_map_rev.get(self.sftp_remote_sort_col_row.get_selected(), "name"))
        self.settings_manager.set("sftp.remote_default_sort_direction", sort_dir_map_rev.get(self.sftp_remote_sort_dir_row.get_selected(), "asc"))

        icon_stem = self._icon_options[self.icon_row.get_selected()][1]
        self.settings_manager.set("interface.icon", icon_stem)

        self.settings_manager.set("interface.tree_row_striping", self.tree_row_striping_row.get_active())

        search_position_map_rev = {0: "top", 1: "bottom"}
        self.settings_manager.set(
            "interface.host_search_position",
            search_position_map_rev.get(self.search_position_row.get_selected(), "bottom")
        )

        self.settings_manager.set("interface.debug_mode", self.debug_mode_row.get_active())

        for key, picker in self._shortcut_pickers.items():
            self.settings_manager.set(key, picker.get_accelerator())

        self.settings_manager.set("interface.watermark_enabled", self.watermark_enabled_row.get_active())
        self.settings_manager.set("interface.watermark_text", self.watermark_text_row.get_text())
        self.settings_manager.set("interface.watermark_position", self.watermark_position_grid.get_selected())
        self.settings_manager.set("interface.watermark_font_size", int(self.watermark_font_size_row.get_value()))
        self.settings_manager.set(
            "interface.watermark_font_family", self.watermark_font_family_button.get_font_desc().get_family()
        )
        self.settings_manager.set("interface.watermark_color", _rgba_to_hex(self.watermark_color_button.get_rgba()))
        self.settings_manager.set("interface.watermark_opacity", int(self.watermark_opacity_row.get_value()))
        watermark_scope_map_rev = {0: "active", 1: "all"}
        self.settings_manager.set(
            "interface.watermark_scope",
            watermark_scope_map_rev.get(self.watermark_scope_row.get_selected(), "active")
        )
        self.settings_manager.set(
            "interface.watermark_shrink_percent",
            self._watermark_shrink_percents[self.watermark_shrink_row.get_selected()]
        )

        watermark_rules = []
        for state in self._watermark_rule_rows:
            pattern = state["pattern_row"].get_text().strip()
            if not pattern:
                continue  # skip a still-blank row left over from "Add"
            watermark_rules.append({
                "pattern": pattern,
                "color": _rgba_to_hex(state["color_button"].get_rgba()),
                "opacity": int(state["opacity_spin"].get_value()),
            })
        self.settings_manager.set("interface.watermark_rules", watermark_rules)

        # --- AI ---
        self.settings_manager.set("ai.disabled", self.ai_disabled_row.get_active())
        self.settings_manager.set("ai.system_prompt", self.ai_system_prompt_row.get_text().strip())
        self.settings_manager.set("ai.request_timeout_seconds", int(self.ai_timeout_row.get_value()))

        provider_model_overrides = {}
        for provider_id, key_row in self._ai_key_rows.items():
            key_text = key_row.get_text().strip()
            if key_text:
                self.keyring.save_password(f"ai:{provider_id}", key_text)
            else:
                self.keyring.clear_password(f"ai:{provider_id}")
            model_text = self._ai_model_rows[provider_id].get_text().strip()
            if model_text and model_text != AI_DEFAULT_MODELS.get(provider_id):
                provider_model_overrides[provider_id] = model_text

        custom_providers = []
        for row_state in self._ai_custom_rows:
            name = row_state["name_row"].get_text().strip()
            base_url = row_state["url_row"].get_text().strip()
            if not name and not base_url:
                continue  # skip a still-blank row left over from "Add"
            key_text = row_state["key_row"].get_text().strip()
            custom_id = row_state["id"]
            if key_text:
                self.keyring.save_password(f"ai:custom:{custom_id}", key_text)
            else:
                self.keyring.clear_password(f"ai:custom:{custom_id}")
            custom_providers.append({
                "id": custom_id,
                "name": name or _("Custom"),
                "base_url": base_url,
                "has_key": bool(key_text),
            })
            model_text = row_state["model_row"].get_text().strip()
            if model_text:
                provider_model_overrides[f"custom:{custom_id}"] = model_text
        self.settings_manager.set("ai.custom_providers", custom_providers)
        self.settings_manager.set("ai.provider_models", provider_model_overrides)

        # --- CLI Client ---
        cli_default_commands = {pid: cmd for pid, _label, cmd in CLI_STANDARD_PROVIDERS}
        cli_command_overrides = {}
        cli_model_overrides = {}
        for cli_id, command_row in self._cli_command_rows.items():
            command_text = command_row.get_text().strip()
            if command_text and command_text != cli_default_commands.get(cli_id):
                cli_command_overrides[cli_id] = command_text
            model_text = self._cli_model_rows[cli_id].get_text().strip()
            if model_text:
                cli_model_overrides[cli_id] = model_text
        self.settings_manager.set("cli.commands", cli_command_overrides)

        custom_tools = []
        for row_state in self._cli_custom_rows:
            name = row_state["name_row"].get_text().strip()
            command = row_state["command_row"].get_text().strip()
            if not name and not command:
                continue  # skip a still-blank row left over from "Add"
            custom_tools.append({"id": row_state["id"], "name": name or _("Custom CLI"), "command": command})
            model_text = row_state["model_row"].get_text().strip()
            if model_text:
                cli_model_overrides[f"custom:{row_state['id']}"] = model_text
        self.settings_manager.set("cli.custom_tools", custom_tools)
        self.settings_manager.set("cli.provider_models", cli_model_overrides)

        self.settings_manager.set("sync.enabled", self.sync_enabled_row.get_active())
        self.settings_manager.set("sync.folder", self.sync_folder_row.get_text().strip())
        self.settings_manager.set("sync.interval_seconds", int(self.sync_interval_row.get_value()))
        self.settings_manager.set("sync.sync_hosts", self.sync_hosts_row.get_active())
        self.settings_manager.set("sync.sync_quickies", self.sync_quickies_row.get_active())
        self.settings_manager.set("sync.sync_ai_chats", self.sync_ai_chats_row.get_active())
        self.settings_manager.set("sync.sync_user_commands", self.sync_user_commands_row.get_active())
        self.settings_manager.set("sync.sync_general", self.sync_general_row.get_active())
        self.settings_manager.set("sync.sync_terminal", self.sync_terminal_row.get_active())

        self.settings_manager.save()

        # Debug logging can take effect immediately, no restart needed.
        logging.getLogger().setLevel(logging.DEBUG if self.debug_mode_row.get_active() else logging.WARNING)

        # Ditto for the search bar's position in the host panel.
        self.parent_window.apply_search_bar_position()

        # And for the terminal color scheme — every already-open terminal
        # gets recolored immediately, not just the next new tab.
        self.parent_window.apply_terminal_color_scheme_to_all()

        # And for the watermark — sync the header-bar toggle's visual state
        # to the "Enabled on start" row (same underlying setting, either
        # control can flip it), then every already-open terminal tab picks
        # up the new enabled/text/position/size/color/opacity/scope state.
        set_split_button_active_style(self.parent_window.watermark_toggle_button, self.watermark_enabled_row.get_active())
        self.parent_window.apply_watermark_settings_to_all()

        # And for Quickies — same sync as the watermark above, then rebuild
        # the live panel's listbox and left-panel layout from the new items/position.
        self.parent_window.quickies_toggle_button.set_active(self.quickies_enabled_row.get_active())
        self.parent_window.refresh_quickies_panel()

        # And for the AI header buttons — react to new/removed keys and
        # custom providers immediately, no restart needed.
        self.parent_window.refresh_ai_provider_buttons()

        # And for sync — show/hide the header button and (re)start the
        # timer at whatever interval/enabled-state was just saved.
        self.parent_window.refresh_sync_button_visibility()
        self.parent_window.restart_sync_timer()

        # Apply the icon change immediately — the window's own icon, the
        # macOS Dock icon (About dialog just reads the setting fresh next
        # time it's opened, no extra work needed there), and the GNOME
        # Wayland dock/taskbar icon (via a user-local .desktop override,
        # since Shell reads Icon= from the .desktop file, not the window).
        self.parent_window.set_icon_name(icon_stem)
        apply_launcher_icon(icon_stem)
        app = self.parent_window.get_application()
        if app is not None and hasattr(app, "apply_macos_dock_icon"):
            app.apply_macos_dock_icon()

        # The striping cell-data-func reads the setting live, it just needs
        # a redraw to pick up the new value immediately.
        self.parent_window.tree_view.queue_draw()

        self.close()

    def on_choose_log_dir_clicked(self, button):
        """Shows a native folder chooser for the log directory setting."""
        file_chooser = Gtk.FileChooserDialog(
            title=_("Select Log Directory"),
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        file_chooser.add_button(_("Select"), Gtk.ResponseType.OK)
        file_chooser.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        file_chooser.connect("response", self.on_log_dir_chosen)
        file_chooser.present()

    def on_log_dir_chosen(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            gfile = dialog.get_file()
            if gfile:
                self.log_dir_row.set_text(gfile.get_path())
        dialog.destroy()

    def on_choose_sync_dir_clicked(self, button):
        """Shows a native folder chooser for the sync folder setting —
        same pattern as on_choose_log_dir_clicked above."""
        file_chooser = Gtk.FileChooserDialog(
            title=_("Select Sync Folder"),
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        file_chooser.add_button(_("Select"), Gtk.ResponseType.OK)
        file_chooser.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        file_chooser.connect("response", self.on_sync_dir_chosen)
        file_chooser.present()

    def on_sync_dir_chosen(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            gfile = dialog.get_file()
            if gfile:
                self.sync_folder_row.set_text(gfile.get_path())
        dialog.destroy()

    def _refresh_sync_status_label(self):
        last_sync_at = self.settings_manager.get("sync.last_sync_at")
        last_error = self.settings_manager.get("sync.last_sync_error")
        if last_error:
            text = _("Last attempt failed: {error}").format(error=last_error)
        elif last_sync_at:
            when = datetime.datetime.fromtimestamp(last_sync_at).strftime("%Y-%m-%d %H:%M:%S")
            text = _("Last synced: {time}").format(time=when)
        else:
            text = _("Never synced")
        self.sync_status_label.set_text(text)
        # The label itself is capped+ellipsized (see its construction
        # above) — the tooltip is how the full message stays reachable,
        # long error text included.
        self.sync_status_label.set_tooltip_text(text)

    def on_force_sync_clicked(self, button):
        """Saves the sync settings first (folder/what-to-sync must be
        current for the sync that's about to run to pick them up — Apply
        hasn't necessarily been clicked yet) and triggers an immediate
        sync on the parent window, then polls briefly (capped, same
        give-up-quietly pattern used elsewhere in this app for "wait for
        an async result") to refresh the status line once it's done."""
        self.settings_manager.set("sync.folder", self.sync_folder_row.get_text().strip())
        self.settings_manager.set("sync.sync_hosts", self.sync_hosts_row.get_active())
        self.settings_manager.set("sync.sync_quickies", self.sync_quickies_row.get_active())
        self.settings_manager.set("sync.sync_ai_chats", self.sync_ai_chats_row.get_active())
        self.settings_manager.set("sync.sync_user_commands", self.sync_user_commands_row.get_active())
        self.settings_manager.set("sync.sync_general", self.sync_general_row.get_active())
        self.settings_manager.set("sync.sync_terminal", self.sync_terminal_row.get_active())
        self.settings_manager.save()

        self.parent_window.force_sync_now()
        self.sync_status_label.set_text(_("Syncing…"))

        attempts = [0]
        def poll():
            if self.parent_window.sync_in_progress:
                attempts[0] += 1
                return attempts[0] < 200  # ~20s ceiling, then give up quietly
            self._refresh_sync_status_label()
            return False
        GLib.timeout_add(100, poll)

    def on_command_edited(self, widget, path, text, column_index):
        """Saves the edited text in the user commands ListStore."""
        self.commands_store[path][column_index] = text

    def on_add_command(self, button):
        """Adds a new empty row to the user commands list."""
        self.commands_store.append([_("New Command"), ""])

    def on_remove_command(self, button):
        """Removes the selected row from the user commands list."""
        selection = self.commands_view.get_selection()
        model, tree_iter = selection.get_selected()
        if tree_iter:
            model.remove(tree_iter)

    def on_quicky_row_edited(self, widget, path, text, column_index):
        """Saves the edited text in the Quickies ListStore."""
        self.quickies_store[path][column_index] = text

    def on_add_quicky_row(self, button):
        """Adds a new empty row to the Quickies list."""
        self.quickies_store.append([_("New Quicky"), ""])

    def on_remove_quicky_row(self, button):
        """Removes the selected row from the Quickies list."""
        selection = self.quickies_view.get_selection()
        model, tree_iter = selection.get_selected()
        if tree_iter:
            model.remove(tree_iter)


    def on_reset(self, button):
        """Reset the settings on the current page to their default values."""
        current_page_name = self.stack.get_visible_child_name()
        
        if current_page_name == "terminal":
            self.scrollback_row.set_value(DEFAULT_SETTINGS["terminal.scrollback_lines"])
            self.close_on_disconnect_row.set_active(DEFAULT_SETTINGS["terminal.close_on_disconnect"])
            self.reconnect_prompt_username_row.set_active(DEFAULT_SETTINGS["terminal.reconnect_prompt_username"])
            self.font_button.set_font(DEFAULT_SETTINGS["terminal.font"])
            
            default_scheme_key = DEFAULT_SETTINGS["terminal.color_scheme"]
            try:
                self.scheme_row.set_selected(_BASE_SCHEME_IDS.index(default_scheme_key))
            except ValueError:
                self.scheme_row.set_selected(0)
            self.custom_scheme_switch.set_active(False)

            self.watermark_enabled_row.set_active(DEFAULT_SETTINGS["interface.watermark_enabled"])
            self.watermark_text_row.set_text(DEFAULT_SETTINGS["interface.watermark_text"])
            self.watermark_position_grid.set_selected(DEFAULT_SETTINGS["interface.watermark_position"])
            self.watermark_font_size_row.set_value(DEFAULT_SETTINGS["interface.watermark_font_size"])
            self.watermark_font_family_button.set_font_desc(
                Pango.FontDescription.from_string(DEFAULT_SETTINGS["interface.watermark_font_family"])
            )
            default_watermark_rgba = Gdk.RGBA()
            default_watermark_rgba.parse(DEFAULT_SETTINGS["interface.watermark_color"])
            self.watermark_color_button.set_rgba(default_watermark_rgba)
            self.watermark_opacity_row.set_value(DEFAULT_SETTINGS["interface.watermark_opacity"])
            self.watermark_scope_row.set_selected({"active": 0, "all": 1}.get(DEFAULT_SETTINGS["interface.watermark_scope"], 0))
            try:
                self.watermark_shrink_row.set_selected(
                    self._watermark_shrink_percents.index(DEFAULT_SETTINGS["interface.watermark_shrink_percent"])
                )
            except ValueError:
                self.watermark_shrink_row.set_selected(0)
            self._clear_watermark_rule_rows()
            for rule in DEFAULT_SETTINGS.get("interface.watermark_rules", []):
                self._add_watermark_rule_row(rule)
        elif current_page_name == "client":
            self.ssh_path_row.set_text(DEFAULT_SETTINGS["client.ssh_path"])
            self.telnet_path_row.set_text(DEFAULT_SETTINGS["client.telnet_path"])
            self.sshpass_path_row.set_text(DEFAULT_SETTINGS["client.sshpass_path"])
            self.log_dir_row.set_text(DEFAULT_SETTINGS["client.log_dir"])
        elif current_page_name == "commands":
            self.commands_store.clear()
            default_commands = DEFAULT_SETTINGS.get("user_commands", [])
            for cmd in default_commands:
                self.commands_store.append([cmd.get("name", ""), cmd.get("command", "")])
        elif current_page_name == "quickies":
            self.quickies_enabled_row.set_active(DEFAULT_SETTINGS["quickies.enabled"])
            self.quickies_position_row.set_selected({"above": 0, "below": 1}.get(DEFAULT_SETTINGS["quickies.position"], 1))
            self.quickies_search_position_row.set_selected({"top": 0, "bottom": 1}.get(DEFAULT_SETTINGS["quickies.search_position"], 1))
            self.quickies_store.clear()
            for quicky in DEFAULT_SETTINGS.get("quickies.items", []):
                self.quickies_store.append([quicky.get("name", ""), quicky.get("text", "")])
        elif current_page_name == "sftp":
            self.sftp_path_row.set_text(DEFAULT_SETTINGS["sftp.local_default_path"])
            sort_col_map = {"name": 0, "size": 1, "date": 2}
            self.sftp_sort_col_row.set_selected(sort_col_map.get(DEFAULT_SETTINGS["sftp.local_default_sort_column"], 0))
            sort_dir_map = {"asc": 0, "desc": 1}
            self.sftp_sort_dir_row.set_selected(sort_dir_map.get(DEFAULT_SETTINGS["sftp.local_default_sort_direction"], 0))
            self.sftp_remote_sort_col_row.set_selected(sort_col_map.get(DEFAULT_SETTINGS["sftp.remote_default_sort_column"], 0))
            self.sftp_remote_sort_dir_row.set_selected(sort_dir_map.get(DEFAULT_SETTINGS["sftp.remote_default_sort_direction"], 0))
        elif current_page_name == "interface":
            try:
                default_icon_index = [stem for _label, stem in self._icon_options].index(
                    DEFAULT_SETTINGS["interface.icon"]
                )
            except ValueError:
                default_icon_index = 0
            self.icon_row.set_selected(default_icon_index)
            self.tree_row_striping_row.set_active(DEFAULT_SETTINGS["interface.tree_row_striping"])
            search_position_map = {"top": 0, "bottom": 1}
            self.search_position_row.set_selected(
                search_position_map.get(DEFAULT_SETTINGS["interface.host_search_position"], 1)
            )
            self.debug_mode_row.set_active(DEFAULT_SETTINGS["interface.debug_mode"])
        elif current_page_name == "shortcuts":
            for key, picker in self._shortcut_pickers.items():
                picker.set_accelerator(DEFAULT_SETTINGS[key])
        elif current_page_name == "sync":
            self.sync_enabled_row.set_active(DEFAULT_SETTINGS["sync.enabled"])
            self.sync_folder_row.set_text(DEFAULT_SETTINGS["sync.folder"])
            self.sync_interval_row.set_value(DEFAULT_SETTINGS["sync.interval_seconds"])
            self.sync_hosts_row.set_active(DEFAULT_SETTINGS["sync.sync_hosts"])
            self.sync_quickies_row.set_active(DEFAULT_SETTINGS["sync.sync_quickies"])
            self.sync_ai_chats_row.set_active(DEFAULT_SETTINGS["sync.sync_ai_chats"])
            self.sync_user_commands_row.set_active(DEFAULT_SETTINGS["sync.sync_user_commands"])
            self.sync_general_row.set_active(DEFAULT_SETTINGS["sync.sync_general"])
            self.sync_terminal_row.set_active(DEFAULT_SETTINGS["sync.sync_terminal"])


class BatchCommandDialog(Adw.Window):
    """
    Sends one command to a chosen set of currently open terminal tabs at
    once (never SFTP tabs — those have no shell to feed a command into).
    The div filter dropdown (next to Select/Deselect All) narrows that set
    to whichever split panes are checked — its options depend on the
    window's current split mode (none/vertical/horizontal/grid).
    """
    def __init__(self, parent_window):
        super().__init__(transient_for=parent_window)
        self.parent_window = parent_window
        self.set_default_size(420, 480)
        self.set_title(_("Batch Command"))
        self._syncing_select_all = False
        self._syncing_region_all = False

        header_bar = Adw.HeaderBar()
        send_button = Gtk.Button(label=_("Send"), css_classes=["suggested-action"])
        send_button.connect("clicked", self.on_send_clicked)
        header_bar.pack_end(send_button)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(header_bar)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        content_box.append(main_box)

        # A multi-row, auto-expanding box (grows with content up to
        # max_content_height, then scrolls) rather than a single-line Entry
        # — so a long command is visible in full instead of scrolling off
        # to the side. Ctrl+Enter sends (see on_command_key_pressed); plain
        # Enter inserts a newline, same as any other multi-line text input.
        self.command_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.command_view.add_css_class("card")
        self.command_view.set_top_margin(6)
        self.command_view.set_bottom_margin(6)
        self.command_view.set_left_margin(8)
        self.command_view.set_right_margin(8)
        self.command_buffer = self.command_view.get_buffer()

        command_key_controller = Gtk.EventControllerKey.new()
        command_key_controller.connect("key-pressed", self.on_command_key_pressed)
        self.command_view.add_controller(command_key_controller)

        command_scroller = Gtk.ScrolledWindow()
        command_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        command_scroller.set_min_content_height(36)   # ~1 line
        command_scroller.set_max_content_height(150)  # ~6 lines, then it scrolls
        command_scroller.set_propagate_natural_height(True)
        command_scroller.set_child(self.command_view)
        main_box.append(command_scroller)

        self.close_after_send_check = Gtk.CheckButton(label=_("Close window after send"), active=True)
        main_box.append(self.close_after_send_check)

        # --- "Select / Deselect All" + the div filter dropdown beside it ---
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=6)
        self.select_all_check = Gtk.CheckButton(label=_("Select / Deselect All"), active=True)
        self.select_all_check.connect("toggled", self.on_select_all_toggled)
        top_row.append(self.select_all_check)

        # (region key, checkbutton) for whichever panes exist in the current
        # split mode — e.g. [("left", ...), ("right", ...)] for a vertical
        # split, or the 4 quadrants for a grid. Empty when there's only one
        # pane, since there'd be nothing to filter by.
        self.region_checks = []
        self.region_menu_button = Gtk.MenuButton(label=_("Divs"), halign=Gtk.Align.END, hexpand=True)
        region_options = parent_window._get_region_options()
        if region_options:
            popover = Gtk.Popover()
            popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                                   margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            self.region_all_check = Gtk.CheckButton(label=_("All divs"), active=True)
            self.region_all_check.connect("toggled", self.on_region_all_toggled)
            popover_box.append(self.region_all_check)
            popover_box.append(Gtk.Separator(margin_top=3, margin_bottom=3))
            for key, label in region_options:
                check = Gtk.CheckButton(label=label, active=True)
                check.connect("toggled", self.on_region_toggled)
                popover_box.append(check)
                self.region_checks.append((key, check))
            popover.set_child(popover_box)
            self.region_menu_button.set_popover(popover)
        else:
            self.region_menu_button.set_sensitive(False)
            self.region_menu_button.set_tooltip_text(_("Split the view to filter by pane"))
        top_row.append(self.region_menu_button)
        main_box.append(top_row)

        self.terminal_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin_top=6)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(self.terminal_list_box)
        main_box.append(scrolled)

        # (checkbutton, page_widget) — page_widget is the key into
        # parent_window.open_sessions, which holds the actual Vte.Terminal.
        # Rebuilt whenever the div filter changes.
        self.terminal_checks = []
        self._rebuild_terminal_list()

        self.set_content(content_box)

    def _rebuild_terminal_list(self):
        """Repopulates the terminal checklist from tab_data, restricted to
        whichever div(s) are currently checked in the region filter."""
        child = self.terminal_list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.terminal_list_box.remove(child)
            child = next_child

        active_regions = None
        if self.region_checks:
            active_regions = {key for key, check in self.region_checks if check.get_active()}

        self.terminal_checks = []
        for page_widget, info in self.parent_window.tab_data.items():
            if info.get("type") != "terminal":
                continue
            if page_widget not in self.parent_window.open_sessions:
                continue
            if active_regions is not None:
                owner_pane = self.parent_window._find_notebook_for_page_widget(page_widget)
                if self.parent_window._pane_region_label(owner_pane) not in active_regions:
                    continue
            name = info.get("config", {}).get("name", _("Unnamed"))
            check = Gtk.CheckButton(label=name, active=True)
            check.connect("toggled", self.on_individual_toggled)
            self.terminal_list_box.append(check)
            self.terminal_checks.append((check, page_widget))

        if not self.terminal_checks:
            self.select_all_check.set_sensitive(False)
            self.terminal_list_box.append(Gtk.Label(label=_("No open terminal sessions."), css_classes=["dim-label"]))
            return

        self.select_all_check.set_sensitive(True)
        self._syncing_select_all = True
        self.select_all_check.set_active(all(check.get_active() for check, _widget in self.terminal_checks))
        self._syncing_select_all = False

    def on_region_all_toggled(self, checkbutton):
        if self._syncing_region_all:
            return
        self._syncing_region_all = True
        active = checkbutton.get_active()
        for _key, check in self.region_checks:
            check.set_active(active)
        self._syncing_region_all = False
        self._rebuild_terminal_list()

    def on_region_toggled(self, checkbutton):
        if self._syncing_region_all:
            return
        self._syncing_region_all = True
        all_active = all(check.get_active() for _key, check in self.region_checks)
        self.region_all_check.set_active(all_active)
        self._syncing_region_all = False
        self._rebuild_terminal_list()

    def on_select_all_toggled(self, checkbutton):
        if self._syncing_select_all:
            return
        self._syncing_select_all = True
        active = checkbutton.get_active()
        for check, _widget in self.terminal_checks:
            check.set_active(active)
        self._syncing_select_all = False

    def on_individual_toggled(self, checkbutton):
        if self._syncing_select_all:
            return
        self._syncing_select_all = True
        all_active = all(check.get_active() for check, _widget in self.terminal_checks)
        self.select_all_check.set_active(all_active)
        self._syncing_select_all = False

    def on_command_key_pressed(self, controller, keyval, keycode, modifier):
        """Ctrl+Enter sends, mirroring the old single-line Entry's
        activate-on-Enter — plain Enter is left alone to insert a newline,
        like any other multi-line text view."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK
        if is_ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.on_send_clicked()
            return True
        return False

    def on_send_clicked(self, *args):
        start, end = self.command_buffer.get_bounds()
        command = self.command_buffer.get_text(start, end, False)
        if not command:
            return
        payload = (command + "\n").encode("utf-8")

        sent = 0
        errors = []
        for check, page_widget in self.terminal_checks:
            if not check.get_active():
                continue
            session = self.parent_window.open_sessions.get(page_widget)
            if session is None:
                continue
            terminal, pid = session
            try:
                terminal.feed_child(payload)
                sent += 1
            except Exception as e:
                errors.append(str(e))

        if errors:
            # Fail loud instead of silently doing nothing, so a real error
            # here is visible rather than looking like a dead Send button.
            error_dialog = Adw.MessageDialog(
                transient_for=self,
                heading=_("Some terminals didn't receive the command"),
                body="\n".join(errors[:5]),
            )
            error_dialog.add_response("ok", _("OK"))
            error_dialog.present()
            return

        if sent == 0:
            # Nothing was actually selected/available — leave the window
            # open rather than closing on a no-op send.
            return

        if self.close_after_send_check.get_active():
            self.close()
        else:
            self.command_buffer.set_text("")