import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib, Gio
from gi.repository import GObject # Import GObject for custom sort functions
import os
from pathlib import Path
import datetime
import stat
import logging
import threading
import time
import shutil
import tempfile
import queue
import subprocess
import json

from .settings import SettingsManager
from .keyring import KeyringManager
from .dialogs import InputDialog, PermissionsDialog, MessageDialog # MessageDialog for delete confirmation

# Attempt to import paramiko
IS_PARAMIKO_AVAILABLE = False
try:
    import paramiko
    IS_PARAMIKO_AVAILABLE = True
except ImportError:
    logging.warning("SFTP functionality is disabled. Please install 'paramiko' (`pip install paramiko`).")

# Placeholder for future internationalization (i18n)
_ = lambda s: s

# --- Constants for the local file list store ---
(
    COL_ICON,
    COL_NAME,
    COL_SIZE_STR,
    COL_SIZE_BYTES, # For sorting
    COL_PERMS_STR,
    COL_PERMS_MODE, # For chmod (integer mode)
    COL_MODIFIED_STR,
    COL_MODIFIED_TS, # For sorting
    COL_IS_DIR,
    COL_FULL_PATH
) = range(10)


class SftpWidget(Gtk.Box):
    """
    A dual-pane SFTP file manager widget.
    """
    def __init__(self, host_config):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        # Add a flag to identify this widget as an SFTP tab, not a terminal.
        self.is_sftp_widget = True

        self.host_config = host_config
        self.settings = SettingsManager()

        default_path_str = self.settings.get("sftp.local_default_path")
        default_path = Path(default_path_str.replace("~", str(Path.home()))).resolve()
        if default_path.is_dir():
            self.current_local_path = str(default_path)
        else:
            self.current_local_path = str(Path.home())

        self.keyring = KeyringManager()

        # SFTP connection state
        self.ssh_client = None
        self.sftp_client = None
        self.current_remote_path = None
        self.is_reconnecting = False # Flag to prevent multiple reconnect attempts
        self.is_connected = False # Flag to track connection status
        self.ui_queue = queue.Queue() # For thread-safe UI updates
        self._sftp_lock = threading.RLock() # Serialises all sftp_client calls across threads
        self.connection_check_timer_id = None # To store the ID of the connection check timer

        self.temp_dir = tempfile.mkdtemp(prefix="thongssh_sftp_")
        self.file_monitors = {} # {local_temp_path: (monitor, remote_path)}
        self._log_message(f"Created temporary directory for remote editing: {self.temp_dir}")

        # --- RESIZABLE LAYOUT ---
        # [ Paned (H): [Frame: Local] | [Frame: Remote] ]  <- drag the handle to resize
        # [ Box: Button > | Button < ]
        # [ Paned (V) divider, drag to resize the log panel's height ]
        # [ Log Panel             ]

        # 1. A horizontal paned holding the two panels — drag the handle in the
        #    middle to resize them relative to each other.
        hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, hexpand=True, wide_handle=True)

        # 2. The left panel (Local File Manager).
        local_panel = self._create_local_panel()
        hpaned.set_start_child(local_panel)
        hpaned.set_resize_start_child(True)
        hpaned.set_shrink_start_child(False)

        # 3. The right panel (Remote).
        right_panel = self._create_remote_panel()
        hpaned.set_end_child(right_panel)
        hpaned.set_resize_end_child(True)
        hpaned.set_shrink_end_child(False)

        # Start the handle at the middle, same as the old fixed 50/50 layout.
        hpaned.connect("realize", lambda w: GLib.idle_add(
            lambda: w.set_position(w.get_width() // 2) if w.get_width() > 0 else None))

        # Initial load
        self._load_local_directory(self.current_local_path)

        # 4. The transfer buttons, kept centered ON the local/remote divider
        #    so they still visually mean "left panel <-> right panel" once
        #    that divider is draggable instead of fixed at the midpoint.
        self.button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, vexpand=False, hexpand=False, halign=Gtk.Align.START)
        self.button_box.set_sensitive(False) # Disabled until connection is established
        upload_button = Gtk.Button(label=">")
        upload_button.connect("clicked", self.on_upload_clicked)
        upload_button.set_tooltip_text(_("Upload selected to remote"))
        download_button = Gtk.Button(label="<")
        download_button.set_tooltip_text(_("Download selected to local"))
        download_button.connect("clicked", self.on_download_clicked)
        self.button_box.append(upload_button)
        self.button_box.append(download_button)

        # An invisible spacer sized to (divider position - half the button
        # row's width), so the button row's center lands exactly on the
        # divider. Driven synchronously off "notify::position" — no idle_add,
        # no margin recomputation racing against layout (that's what caused
        # the buttons to drift on their own before).
        divider_spacer = Gtk.Box()

        def _sync_divider_spacer(paned, *_args):
            _, natural_width, _, _ = self.button_box.measure(Gtk.Orientation.HORIZONTAL, -1)
            divider_spacer.set_property("width-request", max(paned.get_position() - natural_width // 2, 0))

        hpaned.connect("notify::position", _sync_divider_spacer)
        _sync_divider_spacer(hpaned)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        button_row.append(divider_spacer)
        button_row.append(self.button_box)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=True, hexpand=True)
        content_box.append(hpaned)
        content_box.append(button_row)

        # 5. The log panel at the bottom.
        log_frame = Gtk.Frame(vexpand=True)
        log_scrolled = Gtk.ScrolledWindow()
        log_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False)
        log_scrolled.set_child(self.log_view)
        log_frame.set_child(log_scrolled)

        # 6. A vertical paned between the main content and the log panel —
        #    drag its handle to resize the log panel's height.
        vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True, wide_handle=True)
        vpaned.set_start_child(content_box)
        vpaned.set_resize_start_child(True)
        vpaned.set_shrink_start_child(False)
        vpaned.set_end_child(log_frame)
        vpaned.set_resize_end_child(True)
        vpaned.set_shrink_end_child(True)

        # Start with roughly the old fixed 100px log height, still adjustable afterwards.
        vpaned.connect("realize", lambda w: GLib.idle_add(
            lambda: w.set_position(max(w.get_height() - 100, 100)) if w.get_height() > 0 else None))

        self.append(vpaned)

        # Start the connection process
        self.setup_actions_and_popovers()
        self._connect_sftp()
        GLib.timeout_add(100, self._process_ui_queue)
        self.connection_check_timer_id = GLib.timeout_add_seconds(15, self._check_connection_and_reconnect) # Check connection every 15 seconds
        self.connect("unrealize", self.on_widget_destroy)

    def reconnect(self):
        """Public method to trigger a reconnection."""
        self._log_message(_("Reconnecting..."))

        # Clear UI and state before attempting to reconnect.
        self.ui_queue.put(lambda: self.remote_store.clear())
        self.ui_queue.put(lambda: self.button_box.set_sensitive(False))
        self.ui_queue.put(lambda: self.remote_path_entry.set_text(""))

        # Close existing clients in a separate thread to avoid blocking the UI
        self.is_connected = False
        def close_clients():
            if self.sftp_client: self.sftp_client.close()
            if self.ssh_client: self.ssh_client.close()
            self.sftp_client = None
            self.ssh_client = None
        threading.Thread(target=close_clients, daemon=True).start()

        self._connect_sftp()

    def _check_connection_and_reconnect(self):
        """Periodically checks if the SFTP connection is active and reconnects if not.

        Uses transport.is_active() — a purely in-memory, non-blocking check —
        instead of sftp_client.stat('.') which made a blocking network call on
        the GTK main thread and froze the entire window when the server was slow.
        """
        if self.is_reconnecting or not self.is_connected:
            return True

        if self.sftp_client:
            transport = self.ssh_client.get_transport() if self.ssh_client else None
            if transport is None or not transport.is_active():
                self._log_message(_("Connection lost. Attempting to reconnect..."), is_error=True)
                self.is_reconnecting = True
                self.reconnect()

        return True # Keep the timer running

    def _create_local_panel(self):
        """Builds the entire local file manager widget."""
        frame = Gtk.Frame(label=_("Local"))
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        frame.set_child(main_vbox)

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        up_button = Gtk.Button(icon_name="go-up-symbolic")
        up_button.connect("clicked", self.on_local_up_clicked)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.connect("clicked", self.on_local_refresh_clicked)
        refresh_button.set_tooltip_text(_("Refresh"))

        self.local_path_entry = Gtk.Entry()
        self.local_path_entry.set_hexpand(True) # Allow the entry to fill the space
        self.local_path_entry.connect("activate", self.on_local_path_activated)
        toolbar.append(up_button)
        toolbar.append(refresh_button)
        toolbar.append(self.local_path_entry)
        main_vbox.append(toolbar)

        # TreeView for files
        scrolled_window = Gtk.ScrolledWindow(vexpand=True)
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        main_vbox.append(scrolled_window)

        self.local_store = Gtk.ListStore(str, str, str, GObject.TYPE_INT64, str, int, str, GObject.TYPE_INT64, bool, str)
        self.local_sortable_model = Gtk.TreeModelSort(model=self.local_store)
        self.local_view = Gtk.TreeView(model=self.local_sortable_model)
        self.local_view.connect("row-activated", self.on_local_row_activated)

        scrolled_window.set_child(self.local_view)

        column_definitions = [
            (_("Name"), COL_NAME),
            (_("Size"), COL_SIZE_BYTES),
            (_("Date Modified"), COL_MODIFIED_TS),
            (_("Permissions"), None) # Permissions column is not directly sortable
        ]

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self.on_local_view_key_pressed)
        self.local_view.add_controller(key_controller)

        for i, (col_title, sort_col_id) in enumerate(column_definitions):
            column = Gtk.TreeViewColumn(col_title)
            column.set_resizable(True)
            column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

            # Set up sorting for the column
            if sort_col_id is not None:
                column.set_sort_column_id(sort_col_id)

            if i == 0: # Name column with icon and text
                column.set_fixed_width(250)
                icon_renderer = Gtk.CellRendererPixbuf()
                text_renderer = Gtk.CellRendererText()
                column.pack_start(icon_renderer, False)
                column.pack_start(text_renderer, True) # Text renderer expands
                column.add_attribute(icon_renderer, "icon-name", COL_ICON) # Icon from COL_ICON
                column.add_attribute(text_renderer, "text", COL_NAME) # Text from COL_NAME
                column.set_expand(True) # Make this column fill available space
            else: # Other columns with only text
                text_renderer = Gtk.CellRendererText()
                column.pack_start(text_renderer, True)
                # Link renderer to the correct display column
                if i == 1: # Size
                    column.set_fixed_width(80)
                    column.add_attribute(text_renderer, "text", COL_SIZE_STR)
                elif i == 2: # Date
                    column.set_fixed_width(140)
                    column.add_attribute(text_renderer, "text", COL_MODIFIED_STR)
                elif i == 3: # Permissions
                    column.set_fixed_width(100)
                    column.add_attribute(text_renderer, "text", COL_PERMS_STR)

            self.local_view.append_column(column)

        sort_col_map = {"name": COL_NAME, "size": COL_SIZE_BYTES, "date": COL_MODIFIED_TS}
        sort_dir_map = {"asc": Gtk.SortType.ASCENDING, "desc": Gtk.SortType.DESCENDING}
        sort_col = sort_col_map.get(self.settings.get("sftp.local_default_sort_column"), COL_NAME)
        sort_dir = sort_dir_map.get(self.settings.get("sftp.local_default_sort_direction"), Gtk.SortType.ASCENDING)
        self.local_sortable_model.set_sort_column_id(sort_col, sort_dir)

        right_click_gesture = Gtk.GestureClick.new()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self.on_view_right_click, self.local_view)
        self.local_view.add_controller(right_click_gesture)

        self._setup_drag_source(self.local_view, is_local=True)
        self._setup_drop_target(self.local_view, is_local=True)

        return frame

    def _create_remote_panel(self):
        """Builds the entire remote file manager widget."""
        frame = Gtk.Frame(label=_("Remote: {host}").format(host=self.host_config.get('name', '')))
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        frame.set_child(main_vbox)

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        up_button = Gtk.Button(icon_name="go-up-symbolic")
        up_button.connect("clicked", self.on_remote_up_clicked)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.connect("clicked", self.on_remote_refresh_clicked)
        refresh_button.set_tooltip_text(_("Refresh"))

        self.remote_path_entry = Gtk.Entry()
        self.remote_path_entry.set_hexpand(True)
        self.remote_path_entry.connect("activate", self.on_remote_path_activated)
        toolbar.append(up_button)
        toolbar.append(refresh_button)
        toolbar.append(self.remote_path_entry)
        main_vbox.append(toolbar)

        # TreeView for files
        scrolled_window = Gtk.ScrolledWindow(vexpand=True)
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        main_vbox.append(scrolled_window)

        self.remote_store = Gtk.ListStore(str, str, str, GObject.TYPE_INT64, str, int, str, GObject.TYPE_INT64, bool, str)
        self.remote_sortable_model = Gtk.TreeModelSort(model=self.remote_store)
        self.remote_view = Gtk.TreeView(model=self.remote_sortable_model)
        self.remote_view.connect("row-activated", self.on_remote_row_activated)
        scrolled_window.set_child(self.remote_view)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self.on_remote_view_key_pressed)
        self.remote_view.add_controller(key_controller)

        right_click_gesture = Gtk.GestureClick.new()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self.on_view_right_click, self.remote_view)
        self.remote_view.add_controller(right_click_gesture)

        column_definitions = [
            (_("Name"), COL_NAME), (_("Size"), COL_SIZE_BYTES),
            (_("Date Modified"), COL_MODIFIED_TS), (_("Permissions"), None) # Permissions column is not sortable
        ]
        # ✨ Use a different variable name to avoid overwriting the _ function
        for i, (col_title, sort_col_id) in enumerate(column_definitions):
            column = Gtk.TreeViewColumn(col_title)
            column.set_resizable(True)
            column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            if sort_col_id is not None:
                column.set_sort_column_id(sort_col_id)

            if i == 0: # Name column
                column.set_fixed_width(250)
                column.pack_start(Gtk.CellRendererPixbuf(), False)
                column.pack_start(Gtk.CellRendererText(), True)
                column.add_attribute(column.get_cells()[0], "icon-name", COL_ICON)
                column.add_attribute(column.get_cells()[1], "text", COL_NAME)
                column.set_expand(True) # Make this column fill available space
            else: # Other columns
                renderer = Gtk.CellRendererText()
                column.pack_start(renderer, True)
                if i == 1: # Size
                    column.set_fixed_width(80)
                    column.add_attribute(renderer, "text", COL_SIZE_STR)
                elif i == 2: # Date
                    column.set_fixed_width(140)
                    column.add_attribute(renderer, "text", COL_MODIFIED_STR)
                elif i == 3: # Permissions
                    column.set_fixed_width(100)
                    column.add_attribute(renderer, "text", COL_PERMS_STR)
            self.remote_view.append_column(column)

        sort_col_map = {"name": COL_NAME, "size": COL_SIZE_BYTES, "date": COL_MODIFIED_TS}
        sort_dir_map = {"asc": Gtk.SortType.ASCENDING, "desc": Gtk.SortType.DESCENDING}
        sort_col = sort_col_map.get(self.settings.get("sftp.remote_default_sort_column"), COL_NAME)
        sort_dir = sort_dir_map.get(self.settings.get("sftp.remote_default_sort_direction"), Gtk.SortType.ASCENDING)
        self.remote_sortable_model.set_sort_column_id(sort_col, sort_dir)

        self._setup_drag_source(self.remote_view, is_local=False)
        self._setup_drop_target(self.remote_view, is_local=False)

        return frame

    def _load_local_directory(self, path):
        """Populates the local file view with the contents of the given path."""
        self.local_store.clear()
        self.current_local_path = path
        self.local_path_entry.set_text(path)

        try:
            for filename in os.listdir(path):
                full_path = Path(path) / filename
                try:
                    # Use lstat to get info about the link itself, not its target
                    st = full_path.lstat()
                    is_link = stat.S_ISLNK(st.st_mode)
                    
                    # For symlinks, try to determine what they point to
                    if is_link:
                        try:
                            # Use stat() to follow the symlink
                            target_st = full_path.stat()
                            is_dir = stat.S_ISDIR(target_st.st_mode)
                        except (OSError, PermissionError):
                            # Broken symlink or permission denied
                            is_dir = False
                    else:
                        is_dir = stat.S_ISDIR(st.st_mode)
                    
                    if is_dir:
                        icon = "folder-visiting-symbolic" if is_link else "folder-symbolic"
                        self.local_store.append([ # Icon, Name, Size Str, Size Bytes, Perms Str, Perms Mode, Modified Str, Modified TS, Is Dir, Full Path (10 elements)
                            icon, filename, "<DIR>", -1, stat.filemode(st.st_mode), st.st_mode, self._format_date(st.st_mtime), int(st.st_mtime), True, str(full_path)
                        ])
                    else:
                        icon = "emblem-symbolic-link" if is_link else "document-symbolic"
                        self.local_store.append([ # Icon, Name, Size Str, Size Bytes, Perms Str, Perms Mode, Modified Str, Modified TS, Is Dir, Full Path (10 elements)
                            icon, filename, self._format_size(st.st_size), st.st_size, stat.filemode(st.st_mode), st.st_mode, self._format_date(st.st_mtime), int(st.st_mtime), False, str(full_path)
                        ])
                except (OSError, PermissionError):
                    continue

        except (PermissionError, FileNotFoundError) as e:
            logging.error(f"Error loading local directory '{path}': {e}")
            # Optionally, show an error in the UI

    def _load_remote_directory_threaded(self, path):
        """Wrapper to run _load_remote_directory in a background thread."""
        if not self.sftp_client:
            self._log_message(_("Error: SFTP client not connected."))
            return
        thread = threading.Thread(target=self._load_remote_directory, args=(path,))
        thread.daemon = True
        thread.start()

    def _load_remote_directory(self, path):
        """Populates the remote file view (runs in a thread)."""
        if not self._sftp_lock.acquire(timeout=30):
            self._log_message(_("Directory listing skipped: another SFTP operation is still running."), is_error=True)
            return
        try:
            self._log_message(_("Reading remote directory: {path}...").format(path=path))
            try:
                items = self.sftp_client.listdir_attr(path)

                rows_to_add = []
                dirs, files = [], []
                for attr in items:
                    is_link = stat.S_ISLNK(attr.st_mode)
                    is_dir = False
                    if is_link:
                        try:
                            full_path = os.path.join(path, attr.filename).replace('\\', '/')
                            target_attr = self.sftp_client.stat(full_path)
                            is_dir = stat.S_ISDIR(target_attr.st_mode)
                        except (IOError, OSError):
                            is_dir = False
                    else:
                        is_dir = stat.S_ISDIR(attr.st_mode)
                    if is_dir:
                        dirs.append((attr, is_link))
                    else:
                        files.append((attr, is_link))

                dirs.sort(key=lambda x: x[0].filename.lower())
                files.sort(key=lambda x: x[0].filename.lower())

                for attr, is_link in dirs:
                    icon = "folder-visiting-symbolic" if is_link else "folder-symbolic"
                    rows_to_add.append([
                        icon, attr.filename, "<DIR>", -1, stat.filemode(attr.st_mode), attr.st_mode,
                        self._format_date(attr.st_mtime), int(attr.st_mtime), True, os.path.join(path, attr.filename)
                    ])
                for attr, is_link in files:
                    icon = "emblem-symbolic-link" if is_link else "document-symbolic"
                    rows_to_add.append([
                        icon, attr.filename, self._format_size(attr.st_size), attr.st_size,
                        stat.filemode(attr.st_mode), attr.st_mode, self._format_date(attr.st_mtime),
                        int(attr.st_mtime), False, os.path.join(path, attr.filename)
                    ])

                def update_ui():
                    self.remote_store.clear()
                    for row in rows_to_add:
                        self.remote_store.append(row)
                    self.current_remote_path = path
                    self.remote_path_entry.set_text(path)
                    self.remote_sortable_model.set_sort_column_id(COL_NAME, Gtk.SortType.ASCENDING)

                self.ui_queue.put(update_ui)
                self._log_message(_("Successfully listed remote directory: {path}").format(path=path))

            except Exception as e:
                self._log_message(_("Error reading remote directory '{path}': {e}").format(path=path, e=e), is_error=True)
        finally:
            self._sftp_lock.release()

    def _connect_sftp(self):
        """Connects to the SFTP server in a background thread."""
        if not IS_PARAMIKO_AVAILABLE:
            self._log_message(_("SFTP is disabled. Please install 'paramiko'."), is_error=True)
            return

        host_str = self.host_config.get("host", "")
        if not host_str:
            self._log_message(_("Error: host is not set in the config."), is_error=True)
            return

        if '@' not in host_str:
            dialog = InputDialog(
                self.get_root(),
                title=_("Username Required"),
                message=_("Enter username for {host_str}").format(host_str=host_str)
            )
            dialog.run_async(lambda username: self._start_sftp_worker_with_user(username))
        else:
            self._start_sftp_worker_with_user(None)

    def _start_sftp_worker_with_user(self, username_from_prompt, key_passphrase=None, auth_password=None):
        """Starts the connection thread after getting the username (if needed)."""
        if username_from_prompt is None and '@' not in self.host_config.get("host", ""):
            self._log_message(_("SFTP connection canceled (no username provided)."))
            return

        self._log_message(_("Connecting to {name}...").format(name=self.host_config.get("name")))
        thread = threading.Thread(target=self._sftp_connect_worker, args=(username_from_prompt, key_passphrase, auth_password))
        thread.daemon = True
        thread.start()

    def _sftp_connect_worker(self, username_from_prompt, key_passphrase=None, auth_password=None):
        """The actual connection logic that runs in a thread."""
        cfg = self.host_config
        host_str = cfg.get("host", "")
        
        if '@' in host_str:
            user, host = host_str.split('@', 1)
        elif username_from_prompt:
            user, host = username_from_prompt, host_str
        else:
            # This case should not be reached due to the prompt, but as a fallback:
            self._log_message(_("Authentication failed: Username is missing."), is_error=True)
            return

        port = int(cfg.get("port") or 22)
        key_filename = cfg.get("key_path")
        
        if auth_password:
            try:
                self._log_message(_("Attempting connection with provided password..."))
                self.ssh_client = paramiko.SSHClient()
                self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.ssh_client.connect(host, port=port, username=user, password=auth_password, timeout=10, allow_agent=False, look_for_keys=False)
                # NO keepalive - it causes deadlock during file transfers
                self.sftp_client = self.ssh_client.open_sftp()
                self._log_message(_("SFTP connection established successfully with provided password."))
                initial_path = self.sftp_client.normalize('.')
                self._load_remote_directory(initial_path)
                self.ui_queue.put(lambda: self.button_box.set_sensitive(True))
                self.is_connected = True
                self.is_reconnecting = False
                return
            except Exception as e:
                self._log_message(_("Provided password authentication failed: {e}").format(e=e), is_error=True)

        # 2. Try with key first, if it exists and no auth_password was successful.
        if key_filename and not self.sftp_client: # Only try key if not already connected
            try:
                self._log_message(_("Attempting connection with SSH key..."))
                self.ssh_client = paramiko.SSHClient()
                self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.ssh_client.connect(host, port=port, username=user, password=key_passphrase, key_filename=key_filename, timeout=10)
                # Configure transport for large file transfers
                transport = self.ssh_client.get_transport()
                if transport:
                    transport.set_keepalive(30)
                    transport.window_size = 2147483647
                    transport.packetizer.REKEY_BYTES = 2**40
                    transport.packetizer.REKEY_PACKETS = 2**40
                self.sftp_client = self.ssh_client.open_sftp()
                self.sftp_client.get_channel().settimeout(None)
                self._log_message(_("SFTP connection established successfully with key."))
                self.is_connected = True
                self.is_reconnecting = False
            except paramiko.PasswordRequiredException:
                self._log_message(_("SSH key is encrypted. Please enter the passphrase."))
                def prompt_for_key_password():
                    dialog = InputDialog(
                        self.get_root(),
                        title=_("SSH Key Passphrase"),
                        message=_("Enter passphrase for key '{key}'").format(key=os.path.basename(key_filename)),
                        is_password=True
                    )
                    dialog.run_async(lambda passphrase: self._start_sftp_worker_with_user(username_from_prompt, key_passphrase=passphrase, auth_password=None))
                GLib.idle_add(prompt_for_key_password)
                return
            except paramiko.AuthenticationException:
                self._log_message(_("Key authentication failed. Falling back to password..."))
                pass
            except Exception as e:
                self._log_message(_("SFTP connection failed with key: {e}").format(e=e), is_error=True)
                return # Stop on other errors

        # 3. If key auth was skipped or failed, and no auth_password was successful, try saved password from keyring.
        if not self.sftp_client:
            password_from_keyring = self.keyring.load_password(cfg.get("name"))
            if password_from_keyring:
                self._log_message(_("Attempting connection with saved password..."))
                try:
                    self.ssh_client = paramiko.SSHClient()
                    self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    self.ssh_client.connect(host, port=port, username=user, password=password_from_keyring, key_filename=None, timeout=10, allow_agent=False, look_for_keys=False)
                    # Minimal transport configuration
                    transport = self.ssh_client.get_transport()
                    if transport:
                        transport.set_keepalive(30)
                    self.sftp_client = self.ssh_client.open_sftp()
                    self._log_message(_("SFTP connection established successfully with password."))
                    self.is_connected = True
                    self.is_reconnecting = False
                except Exception as e:
                    self._log_message(_("Saved password authentication failed: {e}").format(e=e), is_error=True)
            else:
                # 4. If no saved password, prompt for one.
                self._log_message(_("No key or saved password. Please enter password."))
                def prompt_for_password():
                    dialog = InputDialog(
                        self.get_root(),
                        title=_("Password Required"),
                        message=_("Enter password for {user}@{host}").format(user=user, host=host),
                        is_password=True
                    )
                    def on_password_entered(pwd):
                        if pwd is not None: # Check for None, as empty string is valid
                            self._start_sftp_worker_with_user(username_from_prompt, auth_password=pwd) # Re-run with password
                        else:
                            self._log_message(_("Connection canceled."), is_error=True)
                    dialog.run_async(on_password_entered)
                self.ui_queue.put(prompt_for_password)
                return # Stop this worker

        # 3. If connection was successful (either by key or password)
        if self.sftp_client:
            initial_path = self.sftp_client.normalize('.')
            self._load_remote_directory(initial_path)
            self.ui_queue.put(lambda: self.button_box.set_sensitive(True))
            self.is_connected = True
            self.is_reconnecting = False
        else:
            # If we reach here, it means all attempts failed.
            self._log_message(_("Authentication failed. Please check credentials and connection."), is_error=True)
            self.is_reconnecting = False # Reset the flag on total failure

    def on_upload_clicked(self, button):
        """Handles the click on the Upload (>) button."""
        selection = self.local_view.get_selection()
        model, paths = selection.get_selected_rows()
        if not paths:
            self._log_message(_("No local files selected for upload."), is_error=True)
            return

        for path in paths:
            tree_iter = model.get_iter(path)
            local_path = model.get_value(tree_iter, COL_FULL_PATH)
            is_dir = model.get_value(tree_iter, COL_IS_DIR)
            remote_dest_dir = self.current_remote_path

            # Start worker thread WITHOUT logging to avoid any potential GTK conflicts
            thread = threading.Thread(target=self._upload_worker, args=(local_path, remote_dest_dir, is_dir))
            thread.daemon = True
            thread.start()

    def _upload_worker(self, local_path, remote_dest_dir, is_dir):
        """Uploads a file or directory recursively (runs in a thread)."""
        if not self.sftp_client: return
        basename = os.path.basename(local_path)
        remote_path = os.path.join(remote_dest_dir, basename).replace('\\', '/')

        with self._sftp_lock:
            try:
                if not is_dir: # It's a file
                    try:
                        local_size = os.path.getsize(local_path)
                        start_time = time.time()
                        self.sftp_client.put(local_path, remote_path)
                        elapsed = time.time() - start_time
                        speed = local_size / elapsed if elapsed > 0 else 0
                        def log_success():
                            self._log_message(_("Upload complete: {basename} ({size} in {time:.1f}s, {speed}/s)").format(
                                basename=basename, size=self._format_size(local_size),
                                time=elapsed, speed=self._format_size(speed)
                            ), _from_idle=True)
                            return False
                        GLib.idle_add(log_success)
                    except Exception as e:
                        def log_error():
                            self._log_message(_("Upload failed for {basename}: {e}").format(basename=basename, e=e), is_error=True, _from_idle=True)
                            return False
                        GLib.idle_add(log_error)
                        return
                else: # It's a directory
                    self._log_message(_("Uploading directory {local} to {remote}...").format(local=local_path, remote=remote_path))
                    self.sftp_client.mkdir(remote_path)
                    for dirpath, subdirs, filenames in os.walk(local_path):
                        relative_path = os.path.relpath(dirpath, local_path)
                        remote_dir = remote_path if relative_path == '.' else os.path.join(remote_path, relative_path).replace('\\', '/')
                        for sub_dir in subdirs:
                            try:
                                self.sftp_client.mkdir(os.path.join(remote_dir, sub_dir).replace('\\', '/'))
                            except Exception:
                                pass
                        for filename in filenames:
                            local_file = os.path.join(dirpath, filename)
                            remote_file = os.path.join(remote_dir, filename).replace('\\', '/')
                            self.sftp_client.put(local_file, remote_file)
                    self._log_message(_("Directory upload successful: {basename}").format(basename=basename))

                GLib.idle_add(lambda: self._load_remote_directory_threaded(self.current_remote_path) or False)

            except Exception as e:
                self._log_message(_("Upload failed for {basename}: {e}").format(basename=basename, e=e), is_error=True)

    def on_download_clicked(self, button):
        """Handles the click on the Download (<) button."""
        selection = self.remote_view.get_selection()
        model, paths = selection.get_selected_rows()
        if not paths:
            self._log_message(_("No remote files selected for download."), is_error=True)
            return

        for path in paths:
            tree_iter = model.get_iter(path)
            remote_path = model.get_value(tree_iter, COL_FULL_PATH)
            is_dir = model.get_value(tree_iter, COL_IS_DIR)
            local_dest_dir = self.current_local_path

            # Start worker thread WITHOUT logging to avoid any potential GTK conflicts
            thread = threading.Thread(target=self._download_worker, args=(remote_path, local_dest_dir, is_dir))
            thread.daemon = True
            thread.start()

    def _download_worker(self, remote_path, local_dest_dir, is_dir, _visited_paths=None):
        """Downloads a file or directory recursively (runs in a thread).
        Uses RLock so recursive directory calls re-enter without deadlocking."""
        if not self.sftp_client: return

        if _visited_paths is None:
            _visited_paths = set()

        with self._sftp_lock:
            try:
                normalized_path = self.sftp_client.normalize(remote_path)
            except Exception:
                normalized_path = remote_path

        if normalized_path in _visited_paths:
            self._log_message(_("Skipping circular symlink: {path}").format(path=remote_path), is_error=True)
            return
        _visited_paths.add(normalized_path)

        basename = os.path.basename(remote_path)
        local_path = os.path.join(local_dest_dir, basename)

        if os.path.exists(local_path):
            self._log_message(_("Warning: File already exists: {path}. Will skip.").format(path=local_path), is_error=True)
            self.ui_queue.put(lambda: self._load_local_directory(self.current_local_path))
            return

        with self._sftp_lock:
            try:
                if not is_dir:
                    try:
                        file_attrs = self.sftp_client.stat(remote_path)
                        total_size = file_attrs.st_size
                        start_time = time.time()
                        self.sftp_client.get(remote_path, local_path)
                        elapsed = time.time() - start_time
                        speed = total_size / elapsed if elapsed > 0 else 0
                        def log_success():
                            self._log_message(_("Download complete: {basename} ({size} in {time:.1f}s, {speed}/s)").format(
                                basename=basename, size=self._format_size(total_size),
                                time=elapsed, speed=self._format_size(speed)
                            ), _from_idle=True)
                            return False
                        GLib.idle_add(log_success)
                    except Exception as e:
                        def log_error():
                            self._log_message(_("Download failed for {basename}: {e}").format(basename=basename, e=e), is_error=True, _from_idle=True)
                            return False
                        GLib.idle_add(log_error)
                        if os.path.exists(local_path):
                            try:
                                os.remove(local_path)
                            except Exception:
                                pass
                        return
                else:
                    self._log_message(_("Downloading directory {remote} to {local}...").format(remote=remote_path, local=local_path))
                    os.makedirs(local_path, exist_ok=True)
                    try:
                        items = self.sftp_client.listdir_attr(remote_path)
                    except Exception as e:
                        self._log_message(_("Cannot list directory {path}: {e}").format(path=remote_path, e=e), is_error=True)
                        return
                    item_list = []
                    for item in items:
                        item_path = os.path.join(remote_path, item.filename).replace('\\', '/')
                        is_link = stat.S_ISLNK(item.st_mode)
                        if is_link:
                            try:
                                target_attr = self.sftp_client.stat(item_path)
                                item_is_dir = stat.S_ISDIR(target_attr.st_mode)
                            except (IOError, OSError):
                                self._log_message(_("Skipping broken symlink: {path}").format(path=item_path), is_error=True)
                                continue
                        else:
                            item_is_dir = stat.S_ISDIR(item.st_mode)
                        item_list.append((item_path, item_is_dir))

                # Recurse outside the lock so child calls can re-acquire (RLock allows it)
                if is_dir:
                    for item_path, item_is_dir in item_list:
                        self._download_worker(item_path, local_path, item_is_dir, _visited_paths)
                    self._log_message(_("Directory download successful: {basename}").format(basename=basename))

                GLib.idle_add(lambda: self._load_local_directory(self.current_local_path) or False)

            except Exception as e:
                self._log_message(_("Download failed for {basename}: {e}").format(basename=basename, e=e), is_error=True)

    def _format_size(self, size_bytes):
        """Formats a size in bytes to a human-readable string."""
        if size_bytes == 0:
            return "0 B"
        size_name = ("B", "KiB", "MiB", "GiB", "TiB")
        i = int(size_bytes.bit_length() / 10)
        p = 1024 ** i
        s = round(size_bytes / p, 1)
        return f"{s} {size_name[i]}"

    def _format_date(self, timestamp):
        """Formats a UNIX timestamp to a human-readable string."""
        dt_object = datetime.datetime.fromtimestamp(timestamp)
        return dt_object.strftime("%Y-%m-%d %H:%M")

    def on_local_up_clicked(self, button):
        """Handles the 'Up' button click."""
        parent_path = os.path.dirname(self.current_local_path)
        if parent_path != self.current_local_path: # Avoid getting stuck at "/"
            self._load_local_directory(parent_path)

    def on_local_refresh_clicked(self, button):
        """Handles the 'Refresh' button click for the local panel."""
        if self.current_local_path:
            self._load_local_directory(self.current_local_path)

    def on_remote_refresh_clicked(self, button):
        """Handles the 'Refresh' button click for the remote panel."""
        if self.current_remote_path:
            self._load_remote_directory_threaded(self.current_remote_path)

    def on_local_path_activated(self, entry):
        """Handles Enter press in the path entry."""
        new_path = entry.get_text().strip()
        if os.path.isdir(new_path):
            self._load_local_directory(new_path)
        else:
            # Maybe show an error tooltip
            entry.set_text(self.current_local_path)

    def on_local_row_activated(self, tree_view, path, column):
        """Handles double-click on a file or directory."""
        model = tree_view.get_model()
        tree_iter = model.get_iter(path)
        is_dir = model.get_value(tree_iter, COL_IS_DIR)
        full_path = model.get_value(tree_iter, COL_FULL_PATH)

        if is_dir:
            self._load_local_directory(full_path)
        else: # It's a file, open it with the default application
            try:
                gfile = Gio.File.new_for_path(full_path)
                # Use Gtk.FileLauncher for the modern, correct way to open files
                launcher = Gtk.FileLauncher.new()
                launcher.set_file(gfile)
                launcher.launch(self.get_root(), None, None, None)
            except Exception as e:
                self._log_message(_("Failed to open local file {path}: {e}").format(path=full_path, e=e), is_error=True)

    def on_remote_up_clicked(self, button):
        if not self.current_remote_path: return
        parent_path = os.path.dirname(self.current_remote_path)
        if parent_path != self.current_remote_path:
            self._load_remote_directory_threaded(parent_path)

    def on_remote_path_activated(self, entry):
        self._load_remote_directory_threaded(entry.get_text().strip())

    def on_remote_row_activated(self, tree_view, path, column):
        model = tree_view.get_model()
        tree_iter = model.get_iter(path)
        is_dir = model.get_value(tree_iter, COL_IS_DIR)
        full_path = model.get_value(tree_iter, COL_FULL_PATH)
        if is_dir:
            self._load_remote_directory_threaded(full_path)
        else: # It's a file, start the download-edit-upload cycle
            self._log_message(_("Opening remote file for editing: {path}").format(path=full_path))
            thread = threading.Thread(target=self._remote_edit_worker, args=(full_path,))
            thread.daemon = True
            thread.start()

    def _remote_edit_worker(self, remote_path):
        """Downloads a remote file to a temp location, opens it, and monitors for changes."""
        if not self.sftp_client: return

        basename = os.path.basename(remote_path)
        local_temp_path = os.path.join(self.temp_dir, basename)

        try:
            self._log_message(_("Downloading {basename} to temporary location...").format(basename=basename))
            with self._sftp_lock:
                self.sftp_client.get(remote_path, local_temp_path)

            # 2. Open the local temporary file (in the main thread)
            def open_and_monitor():
                try:
                    gfile = Gio.File.new_for_path(local_temp_path)
                    # Open with default app
                    launcher = Gtk.FileLauncher.new()
                    launcher.set_file(gfile)
                    launcher.launch(self.get_root(), None, None, None)

                    # 3. Monitor for changes
                    monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
                    monitor.connect("changed", self.on_temp_file_changed, local_temp_path, remote_path)
                    self.file_monitors[local_temp_path] = (monitor, remote_path)
                    self._log_message(_("Now monitoring {basename} for changes.").format(basename=basename))

                except Exception as e:
                    self._log_message(_("Failed to open or monitor temporary file: {e}").format(e=e), is_error=True)

            self.ui_queue.put(open_and_monitor)

        except Exception as e:
            self._log_message(_("Failed to download file for editing: {e}").format(e=e), is_error=True)

    def on_temp_file_changed(self, monitor, file, other_file, event_type, local_path, remote_path):
        """Callback when a monitored temporary file is changed."""
        # We are interested in actual content changes, not just closing.
        if event_type == Gio.FileMonitorEvent.CHANGES_DONE_HINT:
            self._log_message(_("Detected changes in {basename}. Uploading back to server...").format(basename=os.path.basename(local_path)))
            # Start the upload in a background thread to avoid blocking the UI
            # We can reuse the existing upload worker.
            thread = threading.Thread(target=self._upload_worker, args=(local_path, os.path.dirname(remote_path), False))
            thread.daemon = True
            thread.start()

    def on_local_view_key_pressed(self, controller, keyval, keycode, modifier):
        """Handles key presses on the local file list, specifically Backspace and Delete."""
        if keyval == Gdk.KEY_BackSpace:
            self.on_local_up_clicked(None)
            return True # Event handled
        elif keyval == Gdk.KEY_Delete:
            self.last_clicked_view = self.local_view
            self.on_delete_activated(None, None)
            return True # Event handled
        return False # Event not handled

    def on_remote_view_key_pressed(self, controller, keyval, keycode, modifier):
        """Handles key presses on the remote file list, specifically Backspace and Delete."""
        if keyval == Gdk.KEY_BackSpace:
            self.on_remote_up_clicked(None)
            return True # Event handled
        elif keyval == Gdk.KEY_Delete:
            self.last_clicked_view = self.remote_view
            self.on_delete_activated(None, None)
            return True # Event handled
        return False # Event not handled

    def _log_message(self, message, is_error=False, _from_idle=False):
        """Appends a message to the log view in a thread-safe way."""
        def append_log():
            try:
                scroll_adj = self.log_view.get_parent().get_vadjustment()
                is_at_bottom = (scroll_adj.get_value() >= scroll_adj.get_upper() - scroll_adj.get_page_size() - 5) # 5px tolerance

                buf = self.log_view.get_buffer()
                timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                log_line = f"[{timestamp}] {message}\n"
                end_iter = buf.get_end_iter()
                buf.insert(end_iter, log_line)

                # Limit buffer size to prevent memory issues
                line_count = buf.get_line_count()
                if line_count > 1000:
                    # Remove old lines, keep last 800
                    start = buf.get_start_iter()
                    delete_end = buf.get_iter_at_line(200)
                    buf.delete(start, delete_end)

                if is_at_bottom:
                    # scroll_adj.get_upper() isn't updated yet right after insert
                    # (GTK recomputes it on the next layout pass), so drive the
                    # adjustment directly from idle_add — by then the new
                    # line's height is accounted for and this reliably lands
                    # at the true bottom instead of one line short.
                    def scroll_to_bottom():
                        scroll_adj.set_value(scroll_adj.get_upper() - scroll_adj.get_page_size())
                        return False
                    GLib.idle_add(scroll_to_bottom)
            except Exception as e:
                # Silently ignore errors to prevent cascade failures
                pass
            return False  # Don't repeat

        # If already called from idle_add, execute directly
        if _from_idle:
            append_log()
        else:
            # Use GLib.idle_add directly instead of queue - avoids deadlock issues
            try:
                GLib.idle_add(append_log)
            except:
                pass # Ignore errors

    def _process_ui_queue(self):
        """Process UI updates from background threads."""
        while not self.ui_queue.empty():
            callback = self.ui_queue.get()
            callback()
        return True # Keep the timeout running

    def on_widget_destroy(self, *args):
        """Clean up resources when the widget is destroyed."""
        # Stop the periodic connection check timer
        if self.connection_check_timer_id:
            GLib.source_remove(self.connection_check_timer_id)
            self.connection_check_timer_id = None

        if self.sftp_client: self.sftp_client.close()
        if self.ssh_client: self.ssh_client.close()

        for monitor, _ in self.file_monitors.values():
            monitor.cancel()
        self.file_monitors.clear()
        try:
            shutil.rmtree(self.temp_dir)
            self._log_message(f"Cleaned up temporary directory: {self.temp_dir}")
        except Exception as e:
            self._log_message(f"Failed to clean up temporary directory {self.temp_dir}: {e}", is_error=True)

        self._log_message("SFTP connection closed.")

    def setup_actions_and_popovers(self):
        """Creates GActions and PopoverMenus for context menus."""
        self.sftp_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("sftp", self.sftp_action_group)

        action_rename = Gio.SimpleAction.new("rename-file", None)
        action_rename.connect("activate", self.on_rename_activated)
        self.sftp_action_group.add_action(action_rename)

        action_delete = Gio.SimpleAction.new("delete-file", None)
        action_delete.connect("activate", self.on_delete_activated)
        self.sftp_action_group.add_action(action_delete)

        action_transfer = Gio.SimpleAction.new("transfer-file", None)
        action_transfer.connect("activate", self.on_transfer_activated)
        self.sftp_action_group.add_action(action_transfer)

        action_chmod = Gio.SimpleAction.new("chmod-file", None)
        action_chmod.connect("activate", self.on_chmod_activated)
        self.sftp_action_group.add_action(action_chmod)

        action_new_folder = Gio.SimpleAction.new("new-folder", None)
        action_new_folder.connect("activate", self.on_new_folder_activated)
        self.sftp_action_group.add_action(action_new_folder)

        item_section = Gio.Menu()
        item_section.append(_("Transfer"), "sftp.transfer-file")
        item_section.append(_("Rename..."), "sftp.rename-file")
        item_section.append(_("Change Permissions..."), "sftp.chmod-file")
        item_section.append(_("Delete"), "sftp.delete-file")

        menu = Gio.Menu()
        menu.append(_("New Folder..."), "sftp.new-folder")
        menu.append_section(None, item_section)

        self.popover_file = Gtk.PopoverMenu.new_from_model(menu)
        self.popover_file.set_parent(self) # The popover is a child of the whole widget

    def on_view_right_click(self, gesture, n_press, x, y, view):
        """Shows the context menu for a file/directory, or for empty space
        (in which case only 'New Folder' is offered, for the current directory)."""
        # Stop the event from propagating further to prevent selection issues.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.last_clicked_view = view

        item_action_names = ("rename-file", "delete-file", "transfer-file", "chmod-file")
        path_info = view.get_path_at_pos(int(x), int(y))

        if not path_info:
            view.get_selection().unselect_all()
            for name in item_action_names:
                action = self.sftp_action_group.lookup_action(name)
                if action: action.set_enabled(False)
        else:
            path, col, _, _ = path_info
            # It's crucial to select the row *before* showing the menu.
            view.get_selection().select_path(path)
            model = view.get_model()

            for name in ("rename-file", "delete-file", "transfer-file"):
                action = self.sftp_action_group.lookup_action(name)
                if action: action.set_enabled(True)

            chmod_action = self.sftp_action_group.lookup_action("chmod-file")
            if model.get_value(model.get_iter(path), COL_NAME) == "..":
                if chmod_action: chmod_action.set_enabled(False)
            else:
                if chmod_action: chmod_action.set_enabled(True)

        translated_x, translated_y = view.translate_coordinates(self, x, y)

        rect = Gdk.Rectangle()
        rect.x = int(translated_x)
        rect.y = int(translated_y)
        rect.width = rect.height = 1
        self.popover_file.set_pointing_to(rect)
        self.popover_file.popup()

    def on_rename_activated(self, action, param):
        """Handles the 'Rename' action from the context menu."""
        if not self.last_clicked_view: return
        selection = self.last_clicked_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        old_full_path = model.get_value(tree_iter, COL_FULL_PATH)
        old_name = model.get_value(tree_iter, COL_NAME)

        dialog = InputDialog(self.get_root(), title=_("Rename"), message=_("New name for '{old_name}':").format(old_name=old_name), default_text=old_name)
        dialog.run_async(lambda new_name: self._execute_rename(old_full_path, new_name))

    def _execute_rename(self, old_path, new_name):
        """Performs the actual rename operation."""
        if not new_name or os.path.basename(old_path) == new_name: return

        new_path = os.path.join(os.path.dirname(old_path), new_name)
        is_local = (self.last_clicked_view == self.local_view)

        def rename_task():
            try:
                if is_local:
                    os.rename(old_path, new_path)
                    self.ui_queue.put(lambda: self._load_local_directory(self.current_local_path))
                else: # Remote
                    with self._sftp_lock:
                        self.sftp_client.rename(old_path, new_path)
                    self.ui_queue.put(lambda: self._load_remote_directory_threaded(self.current_remote_path))
                self._log_message(_("Renamed '{old}' to '{new}'").format(old=os.path.basename(old_path), new=new_name))
            except Exception as e:
                self._log_message(_("Rename failed: {e}").format(e=e), is_error=True)

        thread = threading.Thread(target=rename_task)
        thread.daemon = True
        thread.start()

    def on_new_folder_activated(self, action, param):
        """Handles the 'New Folder' action from the context menu."""
        if not self.last_clicked_view: return
        is_local = (self.last_clicked_view == self.local_view)
        parent_dir = self.current_local_path if is_local else self.current_remote_path
        if not parent_dir:
            self._log_message(_("Cannot create folder: no current directory."), is_error=True)
            return

        dialog = InputDialog(self.get_root(), title=_("New Folder"), message=_("Folder name:"), default_text=_("New Folder"))
        dialog.run_async(lambda name: self._execute_mkdir(parent_dir, name, is_local))

    def _execute_mkdir(self, parent_dir, name, is_local):
        """Creates a new directory locally or remotely (runs in a thread)."""
        if not name: return

        def mkdir_task():
            try:
                if is_local:
                    new_path = os.path.join(parent_dir, name)
                    os.mkdir(new_path)
                    self.ui_queue.put(lambda: self._load_local_directory(self.current_local_path))
                else:
                    new_path = os.path.join(parent_dir, name).replace('\\', '/')
                    with self._sftp_lock:
                        self.sftp_client.mkdir(new_path)
                    self.ui_queue.put(lambda: self._load_remote_directory_threaded(self.current_remote_path))
                self._log_message(_("Created directory: {path}").format(path=new_path))
            except Exception as e:
                self._log_message(_("Failed to create directory '{name}': {e}").format(name=name, e=e), is_error=True)

        thread = threading.Thread(target=mkdir_task)
        thread.daemon = True
        thread.start()

    def on_delete_activated(self, action, param):
        """Handles the 'Delete' action from the context menu."""
        if not self.last_clicked_view: return
        selection = self.last_clicked_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        full_path = model.get_value(tree_iter, COL_FULL_PATH)
        is_dir = model.get_value(tree_iter, COL_IS_DIR)
        is_local = (self.last_clicked_view == self.local_view)

        # Determine if the directory is empty (for local only, remote is harder to check without recursion)
        is_empty_dir = False
        if is_dir and is_local:
            try:
                if not os.listdir(full_path):
                    is_empty_dir = True
            except OSError:
                pass # Can't list, assume not empty or permission denied

        # Prepare dialog messages
        heading = _("Delete '{name}'?").format(name=os.path.basename(full_path))
        body = _("This action cannot be undone.")
        
        if is_dir and not is_empty_dir:
            heading = _("Delete non-empty directory '{name}'?").format(name=os.path.basename(full_path))
            body = _("This will recursively delete all its contents.\nThis action cannot be undone.")

        dialog = MessageDialog(
            self.get_root(),
            heading=heading,
            body=body,
            buttons=[(_("Cancel"), Gtk.ResponseType.CANCEL), (_("Delete"), Gtk.ResponseType.OK)]
        )

        def on_response(dialog, response_id):
            if response_id == Gtk.ResponseType.OK:
                self._execute_delete(full_path, is_dir, is_local)
            # No need to call dialog.destroy() - handled by MessageDialog

        dialog.run_async(on_response)

    def _execute_delete(self, full_path, is_dir, is_local):
        """Performs the actual delete operation in a thread."""
        def delete_task():
            try:
                if is_local:
                    if is_dir:
                        shutil.rmtree(full_path) # Recursive delete for local directories
                    else:
                        os.remove(full_path)
                    self.ui_queue.put(lambda: self._load_local_directory(self.current_local_path))
                else: # Remote
                    with self._sftp_lock:
                        if is_dir:
                            self._sftp_rm_recursive(full_path)
                        else:
                            self.sftp_client.remove(full_path)
                    self.ui_queue.put(lambda: self._load_remote_directory_threaded(self.current_remote_path))
                self._log_message(_("Deleted: {path}").format(path=full_path))
            except Exception as e:
                self._log_message(_("Delete failed: {e}").format(e=e), is_error=True)

        thread = threading.Thread(target=delete_task)
        thread.daemon = True
        thread.start()

    def _sftp_rm_recursive(self, path):
        """Recursively removes a directory and its contents on the remote server."""
        if not self.sftp_client: return
        
        for item in self.sftp_client.listdir_attr(path):
            full_remote_path = os.path.join(path, item.filename).replace('\\', '/')
            if stat.S_ISDIR(item.st_mode):
                self._sftp_rm_recursive(full_remote_path)
            else:
                self.sftp_client.remove(full_remote_path)
        self.sftp_client.rmdir(path)
        self._log_message(_("Recursively deleted remote directory: {path}").format(path=path))

    # --- Drag and Drop Support ---
    
    def _setup_drag_source(self, tree_view, is_local):
        """Sets up a tree view as a drag source."""
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        
        # Store which view this is
        drag_source.tree_view = tree_view
        drag_source.is_local = is_local
        
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        tree_view.add_controller(drag_source)
    
    def _setup_drop_target(self, tree_view, is_local):
        """Sets up a tree view as a drop target."""
        # Accept text/plain content type which we'll use to transfer path info
        drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        
        # Store which view this is
        drop_target.tree_view = tree_view
        drop_target.is_local = is_local
        
        drop_target.connect("drop", self._on_drop)
        tree_view.add_controller(drop_target)
    
    def _on_drag_prepare(self, source, x, y):
        """Prepares data for drag operation."""
        tree_view = source.tree_view
        
        # Get the selected row
        selection = tree_view.get_selection()
        model, paths = selection.get_selected_rows()
        
        if not paths:
            return None
        
        # For now, only support single file drag
        if len(paths) > 1:
            self._log_message(_("Multi-file drag not yet supported. Please drag one file at a time."), is_error=True)
            return None
        
        path = paths[0]
        tree_iter = model.get_iter(path)
        
        # Get file info
        full_path = model.get_value(tree_iter, COL_FULL_PATH)
        is_dir = model.get_value(tree_iter, COL_IS_DIR)
        filename = model.get_value(tree_iter, COL_NAME)
        
        # Store drag data - encode as a special string format
        # Format: "local|/path/to/file|1" or "remote|/path/to/file|0"
        location = "local" if source.is_local else "remote"
        drag_string = f"{location}|{full_path}|{int(is_dir)}"
        
        self._log_message(f"Drag started: {filename}")
        
        # Create content provider with our custom string
        content = Gdk.ContentProvider.new_for_value(drag_string)
        return content
    
    def _on_drag_begin(self, source, drag):
        """Called when drag operation begins."""
        tree_view = source.tree_view
        selection = tree_view.get_selection()
        model, paths = selection.get_selected_rows()
        
        if paths:
            tree_iter = model.get_iter(paths[0])
            is_dir = model.get_value(tree_iter, COL_IS_DIR)
            icon_name = "folder-symbolic" if is_dir else "document-symbolic"
            
            # Create drag icon
            paintable = Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).lookup_icon(
                icon_name, None, 48, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_SYMBOLIC
            )
            if paintable:
                source.set_icon(paintable, 24, 24)
    
    def _on_drop(self, target, value, x, y):
        """Handles drop operation."""
        if not isinstance(value, str):
            self._log_message("Invalid drop data", is_error=True)
            return False
        
        # Parse the drag data string
        # Format: "local|/path/to/file|1" or "remote|/path/to/file|0"
        try:
            parts = value.split('|')
            if len(parts) != 3:
                return False
            
            source_location, source_path, is_dir_str = parts
            is_dir = bool(int(is_dir_str))
            source_is_local = (source_location == "local")
            target_is_local = target.is_local
            
        except Exception as e:
            self._log_message(f"Failed to parse drop data: {e}", is_error=True)
            return False
        
        # Don't allow drop on the same panel
        if source_is_local == target_is_local:
            self._log_message(_("Cannot drop on the same panel"), is_error=True)
            return False
        
        # Determine destination directory and start transfer
        if source_is_local:
            # Dragging from local to remote (upload)
            dest_dir = self.current_remote_path
            self._log_message(_("Drag and drop: uploading {path}...").format(path=source_path))
            thread = threading.Thread(target=self._upload_worker, args=(source_path, dest_dir, is_dir))
            thread.daemon = True
            thread.start()
        else:
            # Dragging from remote to local (download)
            dest_dir = self.current_local_path
            self._log_message(_("Drag and drop: downloading {path}...").format(path=source_path))
            thread = threading.Thread(target=self._download_worker, args=(source_path, dest_dir, is_dir))
            thread.daemon = True
            thread.start()
        
        return True


    def on_transfer_activated(self, action, param):
        """Handles the 'Transfer' action from the context menu."""
        if not self.last_clicked_view: return
        if self.last_clicked_view == self.local_view:
            self.on_upload_clicked(None)
        else:
            self.on_download_clicked(None)

    def on_chmod_activated(self, action, param):
        """Handles the 'Change Permissions' action."""
        if not self.last_clicked_view: return

        selection = self.last_clicked_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        full_path = model.get_value(tree_iter, COL_FULL_PATH)
        current_mode = model.get_value(tree_iter, COL_PERMS_MODE)

        dialog = PermissionsDialog(self.get_root(), initial_mode=current_mode)
        dialog.run_async(lambda new_mode: self._execute_chmod(full_path, new_mode))

    def _execute_chmod(self, path, new_mode):
        """Applies new permissions to a remote file/directory."""
        if new_mode is None: return # Dialog was cancelled
        is_local = (self.last_clicked_view == self.local_view)

        def chmod_task():
            try:
                if is_local:
                    os.chmod(path, new_mode)
                    # Refresh local view
                    self.ui_queue.put(lambda: self._load_local_directory(self.current_local_path))
                else: # Remote
                    with self._sftp_lock:
                        self.sftp_client.chmod(path, new_mode)
                    self.ui_queue.put(lambda: self._load_remote_directory_threaded(self.current_remote_path))

                self._log_message(_("Permissions changed for {path} to {mode}").format(path=path, mode=oct(new_mode)[2:]))
            except Exception as e:
                self._log_message(_("Failed to change permissions for {path}: {e}").format(path=path, e=e), is_error=True)

        thread = threading.Thread(target=chmod_task)
        thread.daemon = True
        thread.start()

