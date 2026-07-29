import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GObject, Pango
import stat
import logging

from .constants import COL_NAME, COL_TYPE
from .colors import COLOR_SCHEMES
from .settings import DEFAULT_SETTINGS
from .launcher_icon import apply_launcher_icon
from .keyring import KeyringManager

# Placeholder for future internationalization (i18n)
_ = lambda s: s


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



# --- CLASS: Settings Dialog ---
class SettingsDialog(Adw.Window):
    def __init__(self, parent_window, settings_manager):
        super().__init__(transient_for=parent_window, modal=True)
        self.parent_window = parent_window
        self.settings_manager = settings_manager

        self.set_default_size(600, 450)

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

        scheme_names = [v['name'] for v in COLOR_SCHEMES.values()]
        self.scheme_row = Adw.ComboRow(title=_("Color Scheme"), model=Gtk.StringList.new(scheme_names))
        
        # Find index of current scheme
        current_scheme_key = self.settings_manager.get("terminal.color_scheme")
        current_scheme_name = COLOR_SCHEMES.get(current_scheme_key, {}).get('name')
        try:
            current_index = scheme_names.index(current_scheme_name)
            self.scheme_row.set_selected(current_index)
        except ValueError:
            self.scheme_row.set_selected(0) # Default

        group_appearance.add(self.scheme_row)

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

        # --- User Commands Page (Placeholder) ---
        page_commands = Adw.PreferencesPage()
        page_commands.set_title(_("User Commands"))
        page_commands.set_icon_name("document-edit-symbolic")

        group_commands = Adw.PreferencesGroup(title=_("Custom Commands"))
        page_commands.add(group_commands)

        commands_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scrolled_view = Gtk.ScrolledWindow(vexpand=True)
        
        self.commands_store = Gtk.ListStore(str, str)
        # Populate from settings
        for cmd in self.settings_manager.get("user_commands"):
            self.commands_store.append([cmd.get("name", ""), cmd.get("command", "")])

        self.commands_view = Gtk.TreeView(model=self.commands_store)
        
        renderer_name = Gtk.CellRendererText(editable=True)
        renderer_name.connect("edited", self.on_command_edited, 0)
        col_name = Gtk.TreeViewColumn(_("Name"), renderer_name, text=0)
        self.commands_view.append_column(col_name)

        renderer_cmd = Gtk.CellRendererText(editable=True)
        renderer_cmd.connect("edited", self.on_command_edited, 1)
        col_cmd = Gtk.TreeViewColumn(_("Command"), renderer_cmd, text=1)
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


        sidebar = Gtk.ListBox()
        sidebar.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sidebar.get_style_context().add_class("navigation-sidebar")

        # Connect ListBox selection to Stack
        sidebar.connect("row-selected", lambda listbox, row: self.stack.set_visible_child_name(row.get_name()))

        # Add pages to stack and rows to sidebar
        self.stack.add_titled_with_icon(page_terminal, "terminal", _("Terminal"), "utilities-terminal-symbolic")
        self.stack.add_titled_with_icon(page_sftp, "sftp", _("SFTP"), "folder-remote-symbolic")
        self.stack.add_titled_with_icon(page_client, "client", _("Client Options"), "network-wired-symbolic")
        self.stack.add_titled_with_icon(page_commands, "commands", _("User Commands"), "document-edit-symbolic")
        self.stack.add_titled_with_icon(page_interface, "interface", _("General"), "preferences-desktop-appearance-symbolic")

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

    def on_apply(self, button):
        """Save settings and close the window."""
        self.settings_manager.set("terminal.font", self.font_button.get_font())
        self.settings_manager.set("terminal.scrollback_lines", int(self.scrollback_row.get_value()))
        self.settings_manager.set("terminal.close_on_disconnect", self.close_on_disconnect_row.get_active())
        
        selected_idx = self.scheme_row.get_selected()
        scheme_key = list(COLOR_SCHEMES.keys())[selected_idx]
        self.settings_manager.set("terminal.color_scheme", scheme_key)

        self.settings_manager.set("client.ssh_path", self.ssh_path_row.get_text())
        self.settings_manager.set("client.telnet_path", self.telnet_path_row.get_text())
        self.settings_manager.set("client.sshpass_path", self.sshpass_path_row.get_text())
        self.settings_manager.set("client.log_dir", self.log_dir_row.get_text().strip())

        # User commands
        user_commands = []
        for row in self.commands_store:
            user_commands.append({"name": row[0], "command": row[1]})
        self.settings_manager.set("user_commands", user_commands)

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

        self.settings_manager.save()

        # Debug logging can take effect immediately, no restart needed.
        logging.getLogger().setLevel(logging.DEBUG if self.debug_mode_row.get_active() else logging.WARNING)

        # Ditto for the search bar's position in the host panel.
        self.parent_window.apply_search_bar_position()

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


    def on_reset(self, button):
        """Reset the settings on the current page to their default values."""
        current_page_name = self.stack.get_visible_child_name()
        
        if current_page_name == "terminal":
            self.scrollback_row.set_value(DEFAULT_SETTINGS["terminal.scrollback_lines"])
            self.close_on_disconnect_row.set_active(DEFAULT_SETTINGS["terminal.close_on_disconnect"])
            self.font_button.set_font(DEFAULT_SETTINGS["terminal.font"])
            
            default_scheme_key = DEFAULT_SETTINGS["terminal.color_scheme"]
            default_scheme_name = COLOR_SCHEMES.get(default_scheme_key, {}).get('name')
            scheme_names = [v['name'] for v in COLOR_SCHEMES.values()]
            if default_scheme_name in scheme_names:
                self.scheme_row.set_selected(scheme_names.index(default_scheme_name))
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
        elif current_page_name == "sftp":
            self.sftp_path_row.set_text(DEFAULT_SETTINGS["sftp.local_default_path"])
            sort_col_map = {"name": 0, "size": 1, "date": 2}
            self.sftp_sort_col_row.set_selected(sort_col_map.get(DEFAULT_SETTINGS["sftp.local_default_sort_column"], 0))
            sort_dir_map = {"asc": 0, "desc": 1}
            self.sftp_sort_dir_row.set_selected(sort_dir_map.get(DEFAULT_SETTINGS["sftp.local_default_sort_direction"], 0))
            self.sftp_remote_sort_col_row.set_selected(sort_col_map.get(DEFAULT_SETTINGS["sftp.remote_default_sort_column"], 0))
            self.sftp_remote_sort_dir_row.set_selected(sort_dir_map.get(DEFAULT_SETTINGS["sftp.remote_default_sort_direction"], 0))


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