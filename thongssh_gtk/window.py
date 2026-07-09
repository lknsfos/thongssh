import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')

import os
import sys
import signal
import shlex
import copy
import logging
import datetime
import re

from gi.repository import Gtk, Adw, Gdk, GLib, Vte, Pango, Gio, GObject

from .constants import APP_ID, COL_NAME, COL_TYPE, COL_ICON, COL_DATA
from .dialogs import InputDialog, HostDialog, GroupDialog # Removed SettingsDialog
from .config import load_and_migrate_config, save_config, CONFIG_DIR
from .settings import SettingsManager
from .keyring import KeyringManager
from .sftp_widget import SftpWidget
from .colors import COLOR_SCHEMES

# Placeholder for future internationalization (i18n)
_ = lambda s: s

# --- Main Window ---
class ThongSSHWindow(Adw.ApplicationWindow):

    open_sessions = {}
    tab_data = {} # ✨ Store config for each tab widget
    force_close_tabs = set() # ✨ Set of tab widgets to force close
    last_clicked_tab = None # ✨ Store the last right-clicked tab for context menu actions

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.set_default_size(1024, 768)

        self.set_deletable(True)

        self.set_icon_name(APP_ID)

        # Load and migrate the config
        self.config_data = load_and_migrate_config()

        # ✨ Load application settings
        self.settings_manager = SettingsManager()

        # ✨ Keyring manager for passwords
        self.keyring = KeyringManager()

        # ✨ Setup custom CSS to hide menu item markers
        self.setup_css()

        # --- 1. Main window structure ---
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(self.main_box)

        # --- 1.1. HeaderBar ---
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True) # Shows min/max/close

        title_widget = Adw.WindowTitle(title="ThongSSH", subtitle="0.3.12")
        header_bar.set_title_widget(title_widget)

        self.setup_global_menu(header_bar)
        self.main_box.append(header_bar)

        # --- ✨ Flap Toggle Button in HeaderBar ---
        self.sidebar_toggle_button = Gtk.ToggleButton(icon_name="go-previous-symbolic", active=True)
        self.sidebar_toggle_button.set_tooltip_text(_("Toggle Sidebar"))
        self.sidebar_toggle_button.connect("toggled", self.on_toggle_sidebar)
        header_bar.pack_start(self.sidebar_toggle_button)



        # --- ✨ Gtk.Paned for resizable sidebar ---
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_resize_start_child(False)
        self.paned.set_shrink_start_child(False)
        self.paned.set_vexpand(True)
        self.main_box.append(self.paned)

        # --- Full Left Panel (Tree) ---
        self.left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.left_panel.set_size_request(300, -1) # Set a default width
        self.paned.set_start_child(self.left_panel)
        self.left_panel.set_visible(True)


        
        # --- SearchBar (The correct way for GTK4) ---
        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_search_mode(False)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        search_box.append(self.search_entry)

        self.search_nav_label = Gtk.Label(label="")
        search_box.append(self.search_nav_label)

        self.search_up_button = Gtk.Button(icon_name="go-up-symbolic")
        self.search_down_button = Gtk.Button(icon_name="go-down-symbolic")
        search_box.append(self.search_up_button)
        search_box.append(self.search_down_button)

        self.search_bar.set_child(search_box)
        self.search_results = []
        self.current_search_index = -1

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.left_panel.append(scrolled_window)

        # Tree model (4 columns). Using Python types works when GObject is correctly imported.
        self.main_tree_store = Gtk.TreeStore(str, str, str, object)
        self.view_tree_store = self.main_tree_store # The model for display (can be changed)
        self.is_filtered = False # Flag indicating if a filter is active


        # --- Sorting setup ---
        def sort_func(model, iter1, iter2, user_data):
            type1 = model.get_value(iter1, COL_TYPE)
            type2 = model.get_value(iter2, COL_TYPE)

            if type1 == "group" and type2 == "host": return -1
            if type1 == "host" and type2 == "group": return 1

            name1 = model.get_value(iter1, COL_NAME).lower()
            name2 = model.get_value(iter2, COL_NAME).lower()

            if name1 < name2: return -1
            elif name1 > name2: return 1
            else: return 0

        self.main_tree_store.set_sort_func(COL_NAME, sort_func, None)
        self.main_tree_store.set_sort_column_id(COL_NAME, Gtk.SortType.ASCENDING)

        self.tree_view = Gtk.TreeView(model=self.view_tree_store)
        self.tree_view.set_headers_visible(False)

        # Disable the old built-in search, as we now have our own SearchBar


        # Renderers
        renderer_pixbuf = Gtk.CellRendererPixbuf()
        renderer_text = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(_("Hosts"))
        column.pack_start(renderer_pixbuf, False)
        column.pack_start(renderer_text, True)

        column.add_attribute(renderer_text, "text", COL_NAME)
        column.add_attribute(renderer_pixbuf, "icon-name", COL_ICON)

        self.tree_view.append_column(column)
        scrolled_window.set_child(self.tree_view)

        # Populate the tree from the config
        self.populate_tree()

        # --- 3. Tree functionality ---
        self.tree_view.connect("row-activated", self.on_tree_row_activated)

        # LEFT button gesture — stored on self so the right-click handler can
        # reset it to avoid a GTK4 gesture deadlock when both buttons are held.
        self.tree_left_gesture = Gtk.GestureClick.new()
        self.tree_left_gesture.set_button(Gdk.BUTTON_PRIMARY)
        self.tree_left_gesture.connect("pressed", self.on_tree_left_click)
        self.tree_view.add_controller(self.tree_left_gesture)
        # RIGHT button gesture — claim on press (and cancel left gesture to
        # prevent deadlock), show menu on release so the button-release event
        # is never delivered into the open popover.
        right_click_gesture = Gtk.GestureClick.new()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_tree_right_press)
        right_click_gesture.connect("released", self.on_tree_right_click)
        self.tree_view.add_controller(right_click_gesture)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self.on_tree_key_pressed)
        self.tree_view.add_controller(key_controller)

        self.setup_search_signals()

        self.left_panel.append(self.search_bar)

        # --- (GTK4 Menu) ---
        self.setup_actions_and_popovers()
        # --- ---

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_halign(Gtk.Align.CENTER)

        # ✨ Return the search button to the bottom panel
        search_btn = Gtk.Button(icon_name="edit-find-symbolic")
        search_btn.set_tooltip_text(_("Search (Ctrl+F)"))
        search_btn.connect("clicked", self.on_toggle_search)

        add_host_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_host_btn.set_tooltip_text(_("Add Host"))
        add_host_btn.connect("clicked", self.on_add_host_clicked)

        add_group_btn = Gtk.Button(icon_name="folder-new-symbolic")
        add_group_btn.set_tooltip_text(_("Create Group"))
        add_group_btn.connect("clicked", self.on_add_group_clicked)

        remove_btn = Gtk.Button(icon_name="list-remove-symbolic")
        remove_btn.set_tooltip_text(_("Remove Selected"))
        remove_btn.connect("clicked", self.on_remove_selected_clicked, None) # None - нет action

        button_box.append(add_host_btn)
        button_box.append(search_btn)
        button_box.append(add_group_btn)
        button_box.append(remove_btn)
        # The collapse button is now in the HeaderBar
        self.left_panel.append(button_box)

        # --- Right Panel (Tabs) ---
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_vexpand(True)
        self.notebook.set_hexpand(True)
        self.paned.set_end_child(self.notebook)
        # ✨ Add a small margin to prevent accidentally grabbing the paned handle
        self.notebook.set_margin_start(6)
        
        # ✨ Add mouse wheel scroll support for scrolling tabs.
        # This will SWITCH tabs, as requested.
        scroll_controller = Gtk.EventControllerScroll.new(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_controller.connect("scroll", self.on_notebook_scroll_switch)
        self.notebook.add_controller(scroll_controller)
        self.connect("map", self.on_first_map)


        # ✨ Connect signals to update menu sensitivity
        self.notebook.connect("notify::page", self.update_menu_sensitivity)
        self.tree_view.get_selection().connect("changed", self.update_menu_sensitivity)
        self.update_menu_sensitivity() # Первоначальная настройка

        # ✨ Add a global key controller for shortcuts like Ctrl+W
        key_controller_window = Gtk.EventControllerKey.new()
        key_controller_window.connect("key-pressed", self.on_window_key_pressed)
        self.add_controller(key_controller_window)

    def setup_css(self):
        """Applies custom CSS to the application."""
        css_provider = Gtk.CssProvider()
        css_data = """
        menuitem > label[label^=">_"] {
            -gtk-icon-source: none;
        }
        menuitem > label[label^="<b>&gt;_</b>"] {
            -gtk-icon-source: none;
        }
        """
        css_provider.load_from_string(css_data)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # --- Сохранение из TreeStore в JSON ---
    def rebuild_config_and_save(self):
        """Парсит Gtk.TreeStore и сохраняет его в hosts.json."""
        logging.debug("Saving tree to config...")

        def iter_tree(model, tree_iter):
            """Рекурсивно парсит Gtk.TreeStore в dict."""
            children = []
            while tree_iter:
                node_type = model.get_value(tree_iter, COL_TYPE)
                data = model.get_value(tree_iter, COL_DATA)
                path = model.get_path(tree_iter)

                if node_type == "group":
                    child_iter = model.iter_children(tree_iter)
                    group_children = iter_tree(model, child_iter)
                    data['children'] = group_children
                    data['expanded'] = self.tree_view.row_expanded(path) # ✨ Save expansion state
                    children.append(data)

                elif node_type == "host":
                    children.append({"type": "host", "config": data})

                tree_iter = model.iter_next(tree_iter)
            return children

        root_iter = self.main_tree_store.get_iter_first()
        root_children = iter_tree(self.main_tree_store, root_iter)

        self.config_data = {"type": "group", "name": "Root", "children": root_children}

        save_config(self.config_data)


    # --- 3. Tree Functionality (Left Panel) ---

    def populate_tree(self):
        self.main_tree_store.clear()

        def iter_nodes(node_data, parent_iter):
            if not isinstance(node_data, dict): return
            node_type = node_data.get("type")

            if node_type == "group":
                # Copy all group data, including 'expanded'
                group_node = {k: v for k, v in node_data.items() if k != 'children'}
                current_iter = self.main_tree_store.append(parent_iter, [group_node["name"], "group", "folder-symbolic", group_node])
                if "children" in node_data:
                    for child in node_data["children"]:
                        iter_nodes(child, current_iter)
                # ✨ Restore expansion state
                if node_data.get("expanded", True):
                    self.tree_view.expand_row(self.main_tree_store.get_path(current_iter), False)

            elif node_type == "host":
                config = node_data.get("config", {})
                name = config.get("name", "Unnamed Host")
                self.main_tree_store.append(parent_iter, [name, "host", "computer-symbolic", config])

        if self.config_data:
            root_children = self.config_data.get("children", [])
            for node in root_children:
                iter_nodes(node, None)

    def on_tree_row_activated(self, tree_view, path, column):
        model = tree_view.get_model()
        tree_iter = model.get_iter(path)

        if tree_iter:
            node_type = model.get_value(tree_iter, COL_TYPE)
            if node_type == "host":
                host_config = model.get_value(tree_iter, COL_DATA)
                logging.info(f"Connecting to: {host_config['name']}")
                self.start_session(host_config)
            elif node_type == "group":
                if tree_view.row_expanded(path):
                    tree_view.collapse_row(path)
                else:
                    tree_view.expand_row(path, False)

    def on_first_map(self, *args):
        """Set the initial position of the paned divider."""
        self.paned.set_position(300)
        # Disconnect the handler so it only runs once
        self.disconnect_by_func(self.on_first_map)

    def on_toggle_sidebar(self, button):
        """Collapses or expands the left sidebar."""
        is_active = button.get_active()
        self.left_panel.set_visible(is_active)
        if is_active:
            button.set_icon_name("go-previous-symbolic")
        else:
            button.set_icon_name("go-next-symbolic")

    def on_toggle_search(self, *args):
        """Activates/deactivates the search bar."""
        search_mode_active = not self.search_bar.get_search_mode()
        self.search_bar.set_search_mode(search_mode_active)
        if search_mode_active:
            self.search_entry.set_text("")
            self.search_entry.grab_focus()

    def setup_search_signals(self):
        """Connects signals for the search widgets."""
        self.search_entry.connect("activate", self.on_search_activate)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_up_button.connect("clicked", self.on_search_nav_up)
        self.search_down_button.connect("clicked", self.on_search_nav_down)

    def on_search_changed(self, search_entry):
        """Main search logic on text change."""
        query = search_entry.get_text().strip()
        self.search_results = []
        self.current_search_index = -1

        if not query:
            self.search_entry.remove_css_class("error")
            self.update_search_ui()
            return

        try:
            # Case-insensitive search
            regex = re.compile(query, re.IGNORECASE)
            self.search_entry.remove_css_class("error")
        except re.error:
            self.search_entry.add_css_class("error")
            self.update_search_ui()
            return

        def find_matches(model, path, iter):
            name = model.get_value(iter, COL_NAME)
            if regex.search(name):
                # Save the path, not the iterator, as it's stable
                self.search_results.append(path.copy())

        self.main_tree_store.foreach(find_matches)

        if self.search_results:
            self.current_search_index = 0
            self.navigate_to_result(self.current_search_index)

        self.update_search_ui()

    def on_search_activate(self, entry):
        """
        Handler for Enter key press in the search entry.
        1. Opens the selected host.
        2. Hides the search bar.
        """
        if self.search_results and 0 <= self.current_search_index < len(self.search_results):
            path = self.search_results[self.current_search_index]
            model = self.tree_view.get_model()
            tree_iter = model.get_iter(path)
            node_type = model.get_value(tree_iter, COL_TYPE)
            if node_type == "host":
                self.on_tree_row_activated(self.tree_view, path, None)

        self.search_bar.set_search_mode(False)

    def on_search_nav_up(self, button):
        if not self.search_results: return
        self.current_search_index = (self.current_search_index - 1 + len(self.search_results)) % len(self.search_results)
        self.navigate_to_result(self.current_search_index)
        self.update_search_ui()

    def on_search_nav_down(self, button):
        if not self.search_results: return
        self.current_search_index = (self.current_search_index + 1) % len(self.search_results)
        self.navigate_to_result(self.current_search_index)
        self.update_search_ui()

    def navigate_to_result(self, index):
        """Moves focus to the found item."""
        if 0 <= index < len(self.search_results):
            path = self.search_results[index]
            # Expand all parent nodes
            self.tree_view.expand_to_path(path)
            # Select the row
            self.tree_view.get_selection().select_path(path)
            # Scroll to it
            self.tree_view.scroll_to_cell(path, None, True, 0.5, 0.0)

    def update_search_ui(self):
        """Updates the state of navigation buttons and label."""
        has_results = len(self.search_results) > 0
        self.search_up_button.set_sensitive(has_results)
        self.search_down_button.set_sensitive(has_results)

        if has_results:
            self.search_nav_label.set_text(f"{self.current_search_index + 1} of {len(self.search_results)}")
        else:
            query = self.search_entry.get_text().strip()
            if query and not self.search_entry.get_style_context().has_class("error"):
                self.search_nav_label.set_text(_("Not found"))
            else:
                self.search_nav_label.set_text("")

    def _on_right_press_guard(self, gesture, n_press, x, y):
        """Generic right-click press guard for terminal and tab gestures.
        Denies the sequence when LMB is held to avoid the Wayland implicit-grab
        freeze that occurs if popup() is called while a button is down."""
        sequence = gesture.get_last_updated_sequence()
        event = gesture.get_last_event(sequence)
        if event and (event.get_modifier_state() & Gdk.ModifierType.BUTTON1_MASK):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_tree_right_press(self, gesture, n_press, x, y):
        """Fired when right button goes DOWN on the tree.
        If LMB is physically held, DENY immediately — calling popup() while
        any button is held creates a Wayland implicit-grab conflict that
        freezes the entire GTK main loop with no recovery path."""
        sequence = gesture.get_last_updated_sequence()
        event = gesture.get_last_event(sequence)
        if event and (event.get_modifier_state() & Gdk.ModifierType.BUTTON1_MASK):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.tree_left_gesture.reset()

    def on_tree_left_click(self, gesture, n_press, x, y):
        """LEFT click handler: deselects if clicked in an empty area."""
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        tree_view = gesture.get_widget()
        path_info = tree_view.get_path_at_pos(int(x), int(y))

        if path_info is None:
            logging.debug("Clicked in empty space, deselecting.")
            selection = tree_view.get_selection()
            selection.unselect_all()

    def on_tree_key_pressed(self, controller, keyval, keycode, modifier):
        """Key press handler (Delete, F2) in the host tree."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK

        # --- Intercept Ctrl+F to activate our search ---
        if is_ctrl and keyval == Gdk.KEY_f:
            self.on_toggle_search()
            return True # Event fully handled, do not propagate further
        
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()

        if not tree_iter:
            return False # Not handled, propagate further

        # --- Deletion with Delete key ---
        if keyval == Gdk.KEY_Delete and not is_ctrl: # Make sure it's not Ctrl+Delete
            logging.debug("Delete key pressed, calling remove handler...")
            # Just call the existing handler
            self.on_remove_selected_clicked(None, None)
            return True # Event handled

        # --- Edit/Rename with F2 ---
        if keyval == Gdk.KEY_F2:
            node_type = model.get_value(tree_iter, COL_TYPE) if tree_iter else None
            if node_type == "host":
                logging.debug("F2 pressed on host, calling edit handler...")
                self.on_menu_edit_host(None, None)
            elif node_type == "group":
                logging.debug("F2 pressed on group, calling rename handler...")
                self.on_menu_rename_group(None, None)
            return True # Event handled

        return False # For all other keys - propagate further

    def on_window_key_pressed(self, controller, keyval, keycode, modifier):
        """Handles global key presses for the window (e.g., Ctrl+W)."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK
        # ✨ Handle Ctrl+W globally to close any active tab
        if is_ctrl and keyval == Gdk.KEY_w:
            self.on_menu_close_tab(None, None)
            return True # Event handled
        return False

    # --- Глобальное меню ---
    def setup_global_menu(self, header_bar):
        """Creates and configures the application's global menu."""
        # 1. Create GActions (actions)
        action_close_tab = Gio.SimpleAction.new("close-tab", None)
        action_close_tab.connect("activate", self.on_menu_close_tab)
        self.add_action(action_close_tab)

        action_quit = Gio.SimpleAction.new("quit", None)
        action_quit.connect("activate", lambda a, p: self.get_application().quit())
        self.add_action(action_quit)

        action_settings = Gio.SimpleAction.new("settings", None)
        action_settings.connect("activate", self.on_menu_settings)
        self.add_action(action_settings)

        action_about = Gio.SimpleAction.new("about", None)
        action_about.connect("activate", self.on_menu_about)
        self.add_action(action_about)

        # 2. Create GMenu (model)
        main_menu_model = Gio.Menu()

        # "File" section
        file_section = Gio.Menu()
        file_section.append(_("Close Tab"), "win.close-tab")
        file_section.append(_("Quit"), "win.quit")
        main_menu_model.append_section(None, file_section)

        # "Edit" section
        edit_section = Gio.Menu()
        edit_section.append(_("Add Host..."), "win.add-host") # Use existing action
        edit_section.append(_("Create Group..."), "win.add-group")
        edit_section.append(_("Edit/Rename"), "win.edit-rename") # New intermediary action
        edit_section.append(_("Delete"), "win.delete")
        main_menu_model.append_section(None, edit_section)

        # "Settings" section
        settings_section = Gio.Menu()
        settings_section.append(_("Settings"), "win.settings")
        main_menu_model.append_section(None, settings_section)

        # "About" section
        about_section = Gio.Menu()
        about_section.append(_("About"), "win.about")
        main_menu_model.append_section(None, about_section)

        # 3. Create button and Popover
        menu_button = Gtk.MenuButton.new()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(main_menu_model)
        header_bar.pack_end(menu_button)

        # 4. Create intermediary actions
        action_edit_rename = Gio.SimpleAction.new("edit-rename", None)
        action_edit_rename.connect("activate", self.on_menu_edit_rename)
        self.add_action(action_edit_rename)

    def update_menu_sensitivity(self, *args):
        """Updates menu item sensitivity based on the current state."""
        # "Close Tab"
        can_close_tab = self.notebook.get_n_pages() > 0
        self.lookup_action("close-tab").set_enabled(can_close_tab)

        # "Edit" and "Delete"
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        item_selected = tree_iter is not None

        self.lookup_action("edit-rename").set_enabled(item_selected)
        self.lookup_action("delete").set_enabled(item_selected)


    # --- (GTK4 Menu) ---
    def setup_actions_and_popovers(self):
        """Creates GActions and Gtk.PopoverMenu for right-click. 100% GTK4."""

        # 1. Create GActions (actions)

        action_connect = Gio.SimpleAction.new("connect", None)
        action_connect.connect("activate", self.on_menu_connect_host)
        self.add_action(action_connect)

        action_add_host = Gio.SimpleAction.new("add-host", None)
        action_add_host.connect("activate", self.on_add_host_clicked)
        self.add_action(action_add_host)

        action_add_group = Gio.SimpleAction.new("add-group", None)
        action_add_group.connect("activate", self.on_add_group_clicked)
        self.add_action(action_add_group)

        action_edit = Gio.SimpleAction.new("edit", None)
        action_edit.connect("activate", self.on_menu_edit_host)
        self.add_action(action_edit)

        action_clone = Gio.SimpleAction.new("clone", None)
        action_clone.connect("activate", self.on_menu_clone_host)
        self.add_action(action_clone)

        action_open_sftp = Gio.SimpleAction.new("open-sftp", None)
        action_open_sftp.connect("activate", self.on_menu_open_sftp)
        self.add_action(action_open_sftp)

        action_rename = Gio.SimpleAction.new("rename", None)
        action_rename.connect("activate", self.on_menu_rename_group)
        self.add_action(action_rename)

        action_delete = Gio.SimpleAction.new("delete", None)
        action_delete.connect("activate", self.on_remove_selected_clicked)
        self.add_action(action_delete)

        action_copy = Gio.SimpleAction.new("copy-clipboard", None)
        action_copy.connect("activate", self.on_menu_copy)
        self.add_action(action_copy)

        action_paste = Gio.SimpleAction.new("paste-clipboard", None)
        action_paste.connect("activate", self.on_menu_paste)
        self.add_action(action_paste)
        
        action_user_cmd = Gio.SimpleAction.new_stateful("user-command", GLib.VariantType.new('s'), GLib.Variant.new_string(""))
        action_user_cmd.connect("activate", self.on_menu_user_command)
        self.add_action(action_user_cmd)

        # ✨ Action to open SSH from an SFTP tab
        action_open_ssh = Gio.SimpleAction.new("open-ssh-from-tab", None)
        action_open_ssh.connect("activate", self.on_menu_open_ssh_from_tab)
        self.add_action(action_open_ssh)
        action_tab_disconnect = Gio.SimpleAction.new("tab-disconnect", None)
        action_tab_disconnect.connect("activate", self.on_menu_tab_disconnect)
        self.add_action(action_tab_disconnect)

        action_tab_reconnect = Gio.SimpleAction.new("tab-reconnect", None)
        action_tab_reconnect.connect("activate", self.on_menu_tab_reconnect)
        self.add_action(action_tab_reconnect)

        action_tab_duplicate = Gio.SimpleAction.new("tab-duplicate", None)
        action_tab_duplicate.connect("activate", self.on_menu_tab_duplicate)
        self.add_action(action_tab_duplicate)


        # 2. Create GMenu (models)
        # Menu for a HOST
        host_menu = Gio.Menu()
        host_menu.append(_("Connect"), "win.connect")
        host_menu.append(_("Edit..."), "win.edit") # "win." = window prefix
        host_menu.append(_("Clone"), "win.clone")
        host_menu.append(_("Connect SFTP"), "win.open-sftp")
        host_menu.append(_("Delete"), "win.delete")
        self.user_commands_menu_section = Gio.Menu()
        host_menu.append_section(None, self.user_commands_menu_section)

        # Menu for a GROUP
        group_menu = Gio.Menu()
        group_menu.append(_("Rename..."), "win.rename")
        group_menu.append(_("Delete"), "win.delete")

        terminal_menu = Gio.Menu()
        terminal_menu.append(_("Copy"), "win.copy-clipboard")
        terminal_menu.append(_("Paste"), "win.paste-clipboard")

        tab_menu = Gio.Menu()
        tab_menu.append(_("Disconnect"), "win.tab-disconnect")
        tab_menu.append(_("Reconnect"), "win.tab-reconnect")
        tab_menu.append(_("Duplicate"), "win.tab-duplicate")
        tab_menu.append(_("Connect SFTP"), "win.open-sftp") # Re-use existing action
        tab_menu.append(_("Connect SSH"), "win.open-ssh-from-tab")

        # 3. Create Popover (widgets)
        self.popover_host = Gtk.PopoverMenu.new_from_model(host_menu)
        self.popover_group = Gtk.PopoverMenu.new_from_model(group_menu)
        self.popover_terminal = Gtk.PopoverMenu.new_from_model(terminal_menu) # TODO: Check if this is used
        self.popover_tab = Gtk.PopoverMenu.new_from_model(tab_menu) # ✨ New popover for tabs
        self.popover_host.set_parent(self) # Set parent once to the main window
        self.popover_terminal.connect("closed", self.on_popover_terminal_closed)
        self.popover_tab.set_parent(self)
        self.popover_group.set_parent(self) # Set parent once to the main window
        self.popover_terminal.set_parent(self) # Attach to the main window

    def on_tree_right_click(self, gesture, n_press, x, y):
        """Right-click handler: Shows PopoverMenu (100% GTK4).
        Connected to 'released' so the button is already up when the popover
        opens — prevents the release event from activating the first menu item."""
        # Clear stale tab context so SFTP/terminal actions use the tree selection,
        # not whatever tab was last right-clicked.
        self.last_clicked_tab = None
        tree_view = gesture.get_widget()
        path_info = tree_view.get_path_at_pos(int(x), int(y))

        if path_info:
            path, col, cell_x, cell_y = path_info
            tree_view.get_selection().select_path(path)

            model = tree_view.get_model()
            tree_iter = model.get_iter(path)
            node_type = model.get_value(tree_iter, COL_TYPE)

            # Get the row's rectangle to "attach" the popover to
            rect = tree_view.get_cell_area(path, col)

            self.build_user_commands_menu()

            self.lookup_action("connect").set_enabled(True)
            self.lookup_action("open-sftp").set_enabled(True)
            self.lookup_action("edit").set_enabled(True)
            self.lookup_action("clone").set_enabled(True)

            if node_type == "host":
                self.popover_host.set_pointing_to(rect)
                self.popover_host.popup()

            elif node_type == "group":
                self.popover_group.set_pointing_to(rect)
                self.popover_group.popup()

    def build_user_commands_menu(self):
        """Dynamically populates the user commands section of the host context menu."""
        # Clear previous items
        self.user_commands_menu_section.remove_all()

        user_commands = self.settings_manager.get("user_commands")
        if not user_commands:
            return

        # Add a separator if there are commands
        if len(user_commands) > 0:
            # The menu model doesn't have a direct separator item.
            # We rely on append_section in setup_actions_and_popovers to create a visual separation.
            pass

        for i, command_data in enumerate(user_commands):
            name = command_data.get("name")
            if name:
                label = f">_ {name}"
                menu_item = Gio.MenuItem.new(label, f"win.user-command('{name}')")
                self.user_commands_menu_section.append_item(menu_item)

    def on_menu_user_command(self, action, param):
        """Handler for clicking a user-defined command."""
        command_name = param.get_string()
        logging.debug(f"User command '{command_name}' activated.")

        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        host_config = model.get_value(tree_iter, COL_DATA)
        user_commands = self.settings_manager.get("user_commands")

        command_to_run = None
        for cmd_data in user_commands:
            if cmd_data.get("name") == command_name:
                command_to_run = self._prepare_command(cmd_data.get("command", ""), host_config)
                break

        if command_to_run:
            logging.info(f"Executing user command: {command_to_run}")
            # Execute the command in the background
            GLib.spawn_async(shlex.split(command_to_run), flags=GLib.SpawnFlags.SEARCH_PATH)

    def on_terminal_right_click(self, gesture, n_press, x, y):
        """Right-click handler for Vte.Terminal. Connected to 'released'."""
        terminal = gesture.get_widget()

        self.lookup_action("copy-clipboard").set_enabled(terminal.get_has_selection())
        self.lookup_action("paste-clipboard").set_enabled(True)

        translated_x, translated_y = terminal.translate_coordinates(self, x, y)

        rect = Gdk.Rectangle()
        rect.x = int(translated_x)
        rect.y = int(translated_y)
        rect.width, rect.height = 1, 1

        self.popover_terminal.set_parent(self)
        self.popover_terminal.set_pointing_to(rect)
        self.popover_terminal.popup()

    def on_menu_copy(self, action, param):
        """Copies selected text from the active terminal."""
        terminal = self.get_active_terminal()
        if terminal:
            terminal.copy_clipboard_format(Vte.Format.TEXT)

    def on_menu_paste(self, action, param):
        """Pastes text from the clipboard into the active terminal."""
        terminal = self.get_active_terminal()
        if terminal:
            terminal.paste_clipboard()

    def on_popover_terminal_closed(self, popover):
        """Gives focus back to the active terminal when the context menu is closed."""
        def refocus():
            terminal = self.get_active_terminal()
            if terminal: terminal.grab_focus()
        GLib.idle_add(refocus)

    def _prepare_command(self, command_template, host_config):
        """Replaces placeholders in a command template with values from host_config."""
        if not command_template:
            return ""

        host_str = host_config.get("host", "")
        user, _, host = host_str.rpartition('@')

        replacements = {
            "$name": host_config.get("name", ""),
            "$host": host,
            "$user": user
        }

        for placeholder, value in replacements.items():
            command_template = command_template.replace(placeholder, shlex.quote(value))
        return command_template
    # --- ---

    def on_menu_open_sftp(self, action, param):
        """Handles the 'Open sftp connection' action."""
        # ✨ Check if we're being called from a tab context menu
        host_config = None
        if self.last_clicked_tab and self.last_clicked_tab in self.tab_data:
            # Get config from the clicked tab
            host_config = self.tab_data[self.last_clicked_tab]["config"]
            logging.info(f"Opening SFTP for tab: {host_config['name']}")
        else:
            # Get config from tree selection
            selection = self.tree_view.get_selection()
            model, tree_iter = selection.get_selected()
            if not tree_iter: return
            host_config = model.get_value(tree_iter, COL_DATA)
            logging.info(f"Opening SFTP stub for: {host_config['name']}")

        # Create the new SFTP widget
        sftp_view = SftpWidget(host_config)

        # Create a tab label with a close button
        tab_label_box, close_btn = self._create_tab_label("folder-remote-symbolic", host_config['name'])

        # Add the new widget to the notebook
        page_num = self.notebook.append_page(sftp_view, tab_label_box)
        self.notebook.set_tab_reorderable(sftp_view, True)
        self.notebook.set_current_page(page_num)
        sftp_view.grab_focus()

        # Connect the close button to a simple tab-closing lambda
        close_btn.connect("clicked", lambda btn: self.notebook.remove_page(self.notebook.page_num(sftp_view)))
        # ✨ Store config for this tab
        self.tab_data[sftp_view] = {"type": "sftp", "config": host_config}



    # --- Handlers for the global menu ---
    def on_menu_close_tab(self, action, param):
        """Closes the active tab."""
        current_page_idx = self.notebook.get_current_page()
        if current_page_idx < 0: return

        page_widget = self.notebook.get_nth_page(current_page_idx)
        if not page_widget: return

        # ✨ Check if it's a terminal tab (has a PID)
        if page_widget in self.open_sessions:
            terminal, pid = self.open_sessions[page_widget]
            self.on_tab_close_button_clicked(None, page_widget, pid)
        else: # It's an SFTP tab or something else without a process
            self.notebook.remove_page(current_page_idx)

    def on_menu_edit_rename(self, action, param):
        """Calls 'Edit' or 'Rename' depending on the node type."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        node_type = model.get_value(tree_iter, COL_TYPE)
        if node_type == "host":
            self.on_menu_edit_host(None, None)
        elif node_type == "group":
            self.on_menu_rename_group(None, None)

    def on_menu_settings(self, action, param):
        """Placeholder for the settings dialog."""
        from .dialogs import SettingsDialog
        logging.info("Settings dialog called.")
        dialog = SettingsDialog(self, self.settings_manager)
        dialog.present()

    def on_menu_about(self, action, param):
        """Shows the 'About' window."""
        dialog = Adw.AboutWindow(transient_for=self)
        dialog.set_application_name("ThongSSH")
        dialog.set_version("0.3.12")
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.set_comments(_("SSH client with a tree-like host structure"))
        dialog.set_copyright("© 2025 Mikhael Karpov")
        dialog.set_developers(["Gemini Code Assist", "Claude Code (Anthropic)"])
        dialog.set_designers(["Mikhael Karpov (lknsfos)"])
        dialog.set_application_icon(APP_ID) # Используем ID для поиска иконки
        dialog.present()


    # --- 5. Dialogs ---
    def on_add_host_clicked(self, *args):
        parent_iter = None
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        child_iter = None

        if tree_iter:
            # If search is active, we need to get the iter from the main model
            if self.is_filtered:
                # This is a complex task, so for simplicity, we'll suggest adding to the root
                parent_iter = None
            else:
                child_iter = tree_iter
                node_type = model.get_value(child_iter, COL_TYPE)
                if node_type == "group":
                    parent_iter = child_iter
                else:
                    parent_iter = self.main_tree_store.iter_parent(child_iter)

        dialog = HostDialog(self, self.main_tree_store, parent_iter=parent_iter)

        def on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                config, new_parent_iter = dialog.get_data()
                self.main_tree_store.append(new_parent_iter, [
                    config['name'], 'host', 'computer-symbolic', config
                ])
                self.rebuild_config_and_save() # Saving will work with main_tree_store
            dialog.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def on_menu_connect_host(self, action, param):
        """Handles the 'Connect' action from the context menu."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        host_config = model.get_value(tree_iter, COL_DATA)
        logging.info(f"Connecting to: {host_config['name']} (from context menu)")
        self.start_session(host_config)


    def on_menu_edit_host(self, action, param):
        """Callback for the 'win.edit' GAction."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        # If search is active, editing can be risky. Let's warn.
        if self.is_filtered:
            # In a real application, it would be better to show a dialog or block the action here
            logging.warning("Editing during an active search is not supported.")
            return

        host_config = model.get_value(tree_iter, COL_DATA)
        child_iter = tree_iter
        parent_iter = self.main_tree_store.iter_parent(child_iter)

        dialog = HostDialog(self, self.main_tree_store, host_data_to_edit=host_config, parent_iter=parent_iter.copy() if parent_iter else None)

        def on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                new_config, new_parent_iter = dialog.get_data()
                new_parent_path = model.get_path(new_parent_iter) if new_parent_iter else None
                old_parent_path = model.get_path(parent_iter) if parent_iter else None

                if new_parent_path != old_parent_path:
                    # No D-n-D, so this is just a "re-creation"
                    model.remove(tree_iter)
                    self.main_tree_store.append(new_parent_iter, [
                        new_config['name'], 'host', 'computer-symbolic', new_config
                    ])
                else:
                    # Simple data update
                    model.set(tree_iter, [COL_NAME, COL_DATA], [new_config['name'], new_config])
                self.rebuild_config_and_save()
            dialog.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def on_menu_clone_host(self, action, param):
        """Callback for the 'win.clone' GAction."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        if self.is_filtered:
            logging.warning("Cloning during an active search is not supported.")
            return

        # 1. Get the data
        host_config = model.get_value(tree_iter, COL_DATA)
        parent_iter = model.iter_parent(tree_iter)

        # 2. Make a DEEP copy
        new_config = copy.deepcopy(host_config)

        # 3. Change the name
        new_config['name'] = f"{new_config['name']} (copy)"

        # 4. Add to the TreeStore
        self.main_tree_store.append(parent_iter, [
            new_config['name'],
            'host',
            'computer-symbolic',
            new_config
        ])
        self.rebuild_config_and_save()

    def on_add_group_clicked(self, *args):
        """Callback for the 'Create Group' button."""

        # Determine which group is SELECTED to suggest it as a parent
        parent_iter = None
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        child_iter = None

        if tree_iter:
            if self.is_filtered:
                parent_iter = None # Add to root during search
            else:
                child_iter = tree_iter
                node_type = model.get_value(child_iter, COL_TYPE)
                if node_type == "group":
                    parent_iter = child_iter
                else:
                    parent_iter = self.main_tree_store.iter_parent(child_iter)

        # Launch the NEW dialog
        dialog = GroupDialog(self, self.main_tree_store, parent_iter=parent_iter)

        def on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                new_name, new_parent_iter = dialog.get_data() # Get both name and parent

                if new_name:
                    # Create the node and add it
                    group_node = {"type": "group", "name": new_name}
                    self.main_tree_store.append(new_parent_iter, [ # Use new_parent_iter
                        new_name, "group", "folder-symbolic", group_node
                    ])
                    self.rebuild_config_and_save()
            dialog.destroy()

        dialog.connect("response", on_response)
        dialog.present()


    def on_menu_rename_group(self, action, param):
        """Callback for the 'win.rename' GAction."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        if self.is_filtered:
            logging.warning("Renaming during an active search is not supported.")
            return

        old_name = model.get_value(tree_iter, COL_NAME)
        dialog = InputDialog(self, title=_("Rename Group"), message=_("New name for '{old_name}':").format(old_name=old_name), default_text=old_name)

        def on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                new_name = dialog.get_text()
                if new_name and new_name != old_name:
                    # Re-get the iter just in case
                    selection = self.tree_view.get_selection()
                    model, tree_iter = selection.get_selected()
                    if tree_iter:
                        data = model.get_value(tree_iter, COL_DATA)
                        data['name'] = new_name
                        model.set(tree_iter, [COL_NAME, COL_DATA], [new_name, data])
                        self.rebuild_config_and_save()
            dialog.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def on_remove_selected_clicked(self, action_or_widget, param):
        """Callback for the 'win.delete' GAction AND the 'Delete' button."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter: return

        if self.is_filtered:
            logging.warning("Deletion during an active search is not supported.")
            return

        node_type = model.get_value(tree_iter, COL_TYPE)
        name = model.get_value(tree_iter, COL_NAME)

        # Prepare default text
        heading = _("Delete {node_type} '{name}'?").format(node_type=node_type, name=name)
        body = _("This action cannot be undone.")

        # If it's a NON-EMPTY group, change the text
        if node_type == "group" and model.iter_has_child(tree_iter):
            heading = _("Delete group '{name}' and ALL its contents?").format(name=name)
            body = _("All hosts and subgroups inside will be recursively deleted.\nThis action cannot be undone.")

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=heading,
            body=body
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            if response == "delete":
                logging.info(f"Deleting {name} and all its children...")
                # Re-get the iter
                model, tree_iter = selection.get_selected()
                if tree_iter:
                    model.remove(tree_iter)
                    self.rebuild_config_and_save()

        dialog.connect("response", on_response)
        dialog.present()

    def get_active_terminal(self):
        """Returns the active Vte.Terminal widget or None."""
        current_page_widget = self.notebook.get_nth_page(self.notebook.get_current_page())
        # scrolled_term is the key in self.open_sessions
        if current_page_widget and current_page_widget in self.open_sessions:
            terminal, pid = self.open_sessions[current_page_widget]
            return terminal
        return None

    def get_active_terminal_widget(self):
        """Returns the container widget (ScrolledWindow) of the active tab."""
        if self.notebook.get_n_pages() > 0:
            return self.notebook.get_nth_page(self.notebook.get_current_page())
        return None



    # --- 6. Логика подключения (Терминал) (Пункт 6) ---
    # --- 6. Connection Logic (Terminal) ---

    def start_session(self, config, existing_terminal_widget=None):
        """Starts a terminal session based on the host config (SSH or Telnet)."""
        host_str = config.get('host')
        if not host_str:
            logging.warning("Error: host is not set in the config.")
            return

        # Only ask for a username if it's an SSH connection and no user is specified.
        if config.get("protocol", "ssh") == "ssh" and "@" not in host_str:
            dialog = InputDialog(
                self,
                title=_("Username Required"),
                message=_("Enter username for {host_str}").format(host_str=host_str)
            )
            # Run asynchronously to not block the UI
            dialog.run_async(lambda username: self._continue_session(config, username, existing_terminal_widget))
        else:
            self._continue_session(config, None, existing_terminal_widget)

    def _continue_session(self, config, username_from_prompt, existing_terminal_widget=None):
        """Second part of the logic, called AFTER getting the username."""

        protocol = config.get("protocol", "ssh")

        # If "Cancel" was pressed in the dialog for an SSH connection that needs a username
        if protocol == "ssh" and username_from_prompt is None and "@" not in config.get('host'):
            logging.info("Connection canceled (no username provided).")
            # Ensure we destroy the dialog if it's still around
            return

        host_str = config.get('host')
        if username_from_prompt:
             host_str = f"{username_from_prompt}@{host_str}"

        protocol = config.get("protocol", "ssh")
        cmd = []
        password = None

        if protocol == "ssh":
            # ✨ Check for a password in the keyring
            password = self.keyring.load_password(config.get("name"))

            # --- 6.2. Сборка команды SSH ---
            if password and "@" in host_str:
                # Use sshpass if a password is set
                sshpass_path = self.settings_manager.get("client.sshpass_path")
                cmd = [sshpass_path, "-p", password, self.settings_manager.get("client.ssh_path")]
                # Add options to prevent host key prompts, as sshpass can't handle them
                cmd.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])
                logging.info("Password found in keyring, using sshpass.")
            else:
                # Standard SSH command
                cmd = [self.settings_manager.get("client.ssh_path")]
            
            if config.get('port'):
                cmd.extend(["-p", str(config['port'])])
            if config.get('key_path'):
                cmd.extend(["-i", config['key_path']])
            if config.get('forward_x', False):
                cmd.append("-X")
            if config.get('forward_agent', False):
                cmd.append("-A")
            if config.get('compat_old_systems', False):
                logging.debug("Compatibility mode enabled (old ciphers)")
                cmd.extend([
                   "-o", "KexAlgorithms=+diffie-hellman-group1-sha1",
                    "-o", "Ciphers=+aes128-cbc,3des-cbc",
                ])
                # ✨ Add HostKeyAlgorithms and PubkeyAcceptedKeyTypes for old systems
                cmd.extend(["-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa"])
            if config.get('ssh_options'):
                try:
                    extra_opts = shlex.split(config['ssh_options'])
                    cmd.extend(extra_opts)
                except Exception as e:
                    logging.warning(f"Error parsing extra options: {e}")

            cmd.append(host_str)

        elif protocol == "telnet":
            # --- 6.2. Сборка команды Telnet ---
            cmd = [self.settings_manager.get("client.telnet_path")]
            # Telnet usually takes host and port as separate arguments
            if "@" in host_str:
                host_str = host_str.split("@", 1)[1] # Telnet doesn't use user@host format
            cmd.append(host_str)
            if config.get('port'):
                cmd.append(str(config['port']))

        else:
            logging.error(f"Unknown protocol: {protocol}")
            return

        logging.debug(f"Assembled command: {' '.join(cmd)}")

        # ✨ Log command to file in config directory
        try:
            log_file_path = CONFIG_DIR / "session_commands.log"
            with open(log_file_path, "a", encoding="utf-8") as f:
                timestamp = datetime.datetime.now().isoformat()
                # Mask password if using sshpass
                log_cmd = list(cmd)
                try:
                    sshpass_idx = log_cmd.index("sshpass")
                    if log_cmd[sshpass_idx + 1] == "-p":
                        log_cmd[sshpass_idx + 2] = "'********'"
                except (ValueError, IndexError):
                    pass  # sshpass not in command or command is malformed
                f.write(f"[{timestamp}] {' '.join(log_cmd)}\n")
        except Exception as e:
            logging.error(f"Failed to write to command log file: {e}")

        # --- 6.3. Terminal Launch ---
        try:
            # If we are reconnecting, reuse the existing terminal. Otherwise, create a new one.
            if existing_terminal_widget and existing_terminal_widget in self.open_sessions:
                terminal, old_pid = self.open_sessions[existing_terminal_widget]
                logging.debug(f"Reusing existing terminal widget. Old PID: {old_pid}")
            else:
                terminal = Vte.Terminal()
            
            scrollback = self.settings_manager.get("terminal.scrollback_lines")
            font_str = self.settings_manager.get("terminal.font")
            scheme_key = self.settings_manager.get("terminal.color_scheme")

            terminal.set_scrollback_lines(scrollback)
            terminal.set_font(Pango.FontDescription.from_string(font_str))

            scheme = COLOR_SCHEMES.get(scheme_key)
            if scheme and "colors" in scheme:
                colors = scheme["colors"]
                
                # Helper function to correctly parse color strings
                def parse_color(spec):
                    rgba = Gdk.RGBA()
                    rgba.parse(spec)
                    return rgba

                palette = [parse_color(c) for c in colors["palette"]]
                terminal.set_colors(
                    foreground=parse_color(colors["foreground"]),
                    background=parse_color(colors["background"]),
                    palette=palette
                )

            success, pid = terminal.spawn_sync(
                Vte.PtyFlags.DEFAULT,
                os.environ['HOME'],
                cmd, [], GLib.SpawnFlags.DEFAULT, # Use DEFAULT instead of DO_NOT_REAP_CHILD
                None, None
            )

            if not success:
                logging.error(f"Error: failed to spawn VTE. Command: {' '.join(cmd)}")
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    heading=_("VTE Spawn Error"),
                    body=_("Failed to start the terminal. Check the command and permissions.\n\nCommand: {cmd_str}").format(cmd_str=' '.join(cmd)),
                )
                dialog.add_response("ok", _("OK"))
                dialog.present()
                return

            logging.debug(f"SSH process started with PID: {pid}")

            # If this is a new session, create all the widgets.
            if not existing_terminal_widget:
                terminal.set_vexpand(True)
                terminal.set_hexpand(True)

                right_click_gesture = Gtk.GestureClick.new()
                right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
                right_click_gesture.connect("pressed", self._on_right_press_guard)
                right_click_gesture.connect("released", self.on_terminal_right_click)
                terminal.add_controller(right_click_gesture)

                key_controller_terminal = Gtk.EventControllerKey.new()
                key_controller_terminal.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                key_controller_terminal.connect("key-pressed", self.on_terminal_key_pressed)
                terminal.add_controller(key_controller_terminal)

                scroll_controller = Gtk.EventControllerScroll.new(flags=Gtk.EventControllerScrollFlags.VERTICAL)
                scroll_controller.connect("scroll", self.on_terminal_scroll)
                terminal.add_controller(scroll_controller)

                scrolled_term = Gtk.ScrolledWindow()
                # ✨ This ensures the terminal gets the correct size allocation
                scrolled_term.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                scrolled_term.set_child(terminal)

                tab_label_box, close_btn = self._create_tab_label("utilities-terminal-symbolic", config['name'])

                page_num = self.notebook.append_page(scrolled_term, tab_label_box)
                self.notebook.set_tab_reorderable(scrolled_term, True)
                self.notebook.set_current_page(page_num)
                terminal.grab_focus()

                self.open_sessions[scrolled_term] = (terminal, pid)
                self.tab_data[scrolled_term] = {"type": "terminal", "config": config}
                close_btn.connect("clicked", self.on_tab_close_button_clicked, scrolled_term, pid)
                terminal.connect("child-exited", self.on_ssh_process_exited, scrolled_term)
            else: # This is a reconnect, just update the PID
                self.open_sessions[existing_terminal_widget] = (terminal, pid)
                terminal.grab_focus()

        except Exception as e:
            logging.critical(f"Critical error spawning VTE: {e}")
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=_("SSH Launch Error"),
                body=_("Failed to start the process. Make sure /usr/bin/ssh exists.\n\nError: {error}").format(error=e),
            )
            dialog.add_response("ok", _("OK"))
            dialog.present()

    def _create_tab_label(self, icon_name, label_text):
        """Creates a standard tab label box with icon, text, close button, and context menu."""
        tab_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name(icon_name) # No change here, this is correct
        tab_label = Gtk.Label(label=label_text)
        tab_label_box.append(icon)
        tab_label_box.append(tab_label)

        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        tab_label_box.append(close_btn)

        right_click_gesture = Gtk.GestureClick.new()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_right_press_guard)
        right_click_gesture.connect("released", self.on_tab_right_click)
        tab_label_box.add_controller(right_click_gesture)

        return tab_label_box, close_btn

    def on_tab_right_click(self, gesture, n_press, x, y):
        """Shows the context menu for a notebook tab. Connected to 'released'."""
        tab_label_box = gesture.get_widget()

        page_widget = None
        for i in range(self.notebook.get_n_pages()):
            child = self.notebook.get_nth_page(i)
            if self.notebook.get_tab_label(child) == tab_label_box:
                page_widget = child
                break

        # ✨ Store the clicked tab so the context menu knows which tab to act on.
        if page_widget:
            self.last_clicked_tab = page_widget
            # Do NOT switch to the tab, just show the menu for it.
        
        sftp_action = self.lookup_action("open-sftp")
        ssh_action = self.lookup_action("open-ssh-from-tab") # For sftp -> terminal

        if page_widget and page_widget in self.tab_data:
            is_sftp = self.tab_data[page_widget]["type"] == "sftp"
            sftp_action.set_enabled(not is_sftp)
            ssh_action.set_enabled(is_sftp)
        else:
            sftp_action.set_enabled(False)
            ssh_action.set_enabled(False)

        translated_x, translated_y = tab_label_box.translate_coordinates(self, x, y)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(translated_x), int(translated_y), 1, 1
        self.popover_tab.set_pointing_to(rect)
        self.popover_tab.popup()

    def on_terminal_key_pressed(self, controller, keyval, keycode, modifier):
        """Handles key presses directly on the Vte.Terminal widget."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK

        if is_ctrl and keyval == Gdk.KEY_w:
            self.on_menu_close_tab(None, None)
            return True # Event handled, stop propagation
        return False # Not handled, allow terminal to process

    def on_terminal_scroll(self, controller, dx, dy):
        """Handles Ctrl+Scroll to change font size in the terminal."""
        modifiers = controller.get_current_event_state()
        if not (modifiers & Gdk.ModifierType.CONTROL_MASK):
            return False # Propagate event if Ctrl is not held

        terminal = controller.get_widget()
        if not isinstance(terminal, Vte.Terminal):
            return False

        font_desc = terminal.get_font()
        current_size_pts = font_desc.get_size() / Pango.SCALE

        # dy < 0 is scroll up (zoom in), dy > 0 is scroll down (zoom out)
        if dy < 0:
            new_size_pts = current_size_pts + 1
        else:
            new_size_pts = current_size_pts - 1

        font_desc.set_size(int(new_size_pts * Pango.SCALE))
        terminal.set_font(font_desc)

        return True # Event handled, stop propagation

    # --- 6.4. Process Management ---
    def on_tab_close_button_clicked(self, button, tab_widget, pid):
        # ✨ Mark this tab for forced closure, so the "keep open" setting is ignored.
        self.force_close_tabs.add(tab_widget)

        logging.debug(f"Sending SIGTERM to process {pid}...")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            logging.debug(f"Process {pid} is already dead.")
            self.close_tab(tab_widget)
        except Exception as e:
            logging.warning(f"Error during os.kill: {e}")
            # Check if the process is alive
            if not os.path.exists(f"/proc/{pid}"):
                 self.close_tab(tab_widget)

    def on_ssh_process_exited(self, terminal, status, tab_widget):
        """Handles the 'child-exited' signal from Vte.Terminal."""
        logging.debug(f"VTE child process exited with status {status} for widget {tab_widget}.")

        is_forced = tab_widget in self.force_close_tabs

        if is_forced or self.settings_manager.get("terminal.close_on_disconnect"):
            if is_forced: self.force_close_tabs.remove(tab_widget)
            self.close_tab(widget=tab_widget)
        else:
            # Keep the tab open and show a message
            exit_message = _("\n\n--- Session finished with exit code: {status} ---").format(status=status)
            terminal.feed_child(exit_message.encode('utf-8'))
            # Make the terminal read-only
            terminal.set_input_enabled(False)

    def close_tab(self, widget):
        """
        Closes a tab, removes it from the notebook, and cleans up associated resources
        like session data and timers.
        """
        """Uses .remove_page()"""
        if widget in self.open_sessions:
            page_num = self.notebook.page_num(widget)
            if page_num != -1:
                 self.notebook.remove_page(page_num)
            
            if self.notebook.get_n_pages() > 0:
                def focus_active_terminal():
                    active_terminal = self.get_active_terminal()
                    if active_terminal: active_terminal.grab_focus()
                GLib.idle_add(focus_active_terminal)

            if widget in self.tab_data:
                del self.tab_data[widget]

            if widget in self.force_close_tabs:
                self.force_close_tabs.remove(widget)

            del self.open_sessions[widget]
        else:
            # It might be an SFTP tab or another non-session widget
            page_num = self.notebook.page_num(widget)
            if page_num != -1:
                self.notebook.remove_page(page_num)
            if widget in self.tab_data:
                del self.tab_data[widget]
            if widget in self.force_close_tabs:
                self.force_close_tabs.remove(widget)

            logging.warning(f"Attempted to close a tab that is not in open_sessions.")

    # --- Tab Context Menu Handlers ---
    def on_menu_tab_disconnect(self, action, param):
        """Closes the currently active tab."""
        # ✨ Use the last clicked tab if available, otherwise use current
        page_widget = self.last_clicked_tab if self.last_clicked_tab else self.notebook.get_nth_page(self.notebook.get_current_page())
        if not page_widget:
            return

        # For terminal, gracefully kill process. For SFTP, just remove.
        if page_widget in self.open_sessions:
            terminal, pid = self.open_sessions[page_widget]
            self.on_tab_close_button_clicked(None, page_widget, pid)
        else:
            self.close_tab(page_widget)

    def on_menu_tab_reconnect(self, action, param):
        """Reconnects the current tab without closing it."""
        # ✨ Use the last clicked tab if available, otherwise use current
        page_widget = self.last_clicked_tab if self.last_clicked_tab else self.notebook.get_nth_page(self.notebook.get_current_page())
        if not page_widget:
            return

        if page_widget in self.tab_data:
            tab_info = self.tab_data[page_widget]

            if tab_info["type"] == "terminal":
                logging.debug(f"Reconnecting terminal tab in place for config: {tab_info['config']['name']}")
                # Get the terminal widget for this specific page_widget
                if page_widget in self.open_sessions:
                    terminal, old_pid = self.open_sessions[page_widget]
                    # Reset terminal state
                    terminal.reset(True, True)
                    terminal.set_input_enabled(True)
                    # Re-run the full session start logic to handle username prompts correctly
                    self.start_session(tab_info['config'], existing_terminal_widget=page_widget)
                else: # Fallback to old behavior if something is wrong
                    # This part is tricky. Reconnecting should not require killing the process.
                    # Let's reset the terminal and re-run the command.
                    self.on_menu_tab_disconnect(None, None)
                    self.start_session(tab_info["config"])

            elif tab_info["type"] == "sftp":
                # For SFTP, we can use its internal reconnect method
                if hasattr(page_widget, 'reconnect'):
                    page_widget.reconnect()
                else: # Fallback
                    self.on_menu_tab_disconnect(None, None)
                    self.on_menu_open_sftp(None, None)

    def on_menu_tab_duplicate(self, action, param):
        """Opens a new tab with the same config as the selected tab."""
        # Use the last right-clicked tab. Fallback to the active tab if needed.
        target_widget = self.last_clicked_tab if self.last_clicked_tab else self.get_active_terminal_widget()
        if target_widget and target_widget in self.tab_data:
            tab_info = self.tab_data[target_widget]
            
            # Re-select the original host in the tree for clarity if cloning SFTP
            if tab_info["type"] == "sftp":
                # This is complex, for now, just open a new SFTP based on config
                sftp_view = SftpWidget(tab_info["config"])
                tab_label_box, close_btn = self._create_tab_label("folder-remote-symbolic", tab_info["config"]['name'])
                page_num = self.notebook.append_page(sftp_view, tab_label_box)
                self.notebook.set_tab_reorderable(sftp_view, True)
                self.notebook.set_current_page(page_num)
                close_btn.connect("clicked", lambda btn: self.notebook.remove_page(self.notebook.page_num(sftp_view)))
                self.tab_data[sftp_view] = {"type": "sftp", "config": tab_info["config"]}
            else: # terminal
                 self.start_session(tab_info["config"])

    def on_notebook_scroll_switch(self, controller, dx, dy):
        """Handles mouse wheel scrolling over the notebook to switch tabs."""
        # dy < 0 is scroll up, dy > 0 is scroll down
        n_pages = self.notebook.get_n_pages()
        if n_pages < 2:
            return False # Don't handle if there's nothing to switch to

        current_page = self.notebook.get_current_page()

        if dy < 0: # Scroll Up -> Previous Tab
            new_page = (current_page - 1 + n_pages) % n_pages
        elif dy > 0: # Scroll Down -> Next Tab
            new_page = (current_page + 1) % n_pages
        else:
            return False # No vertical scroll

        self.notebook.set_current_page(new_page)
        return True # Event handled, stop propagation

    def on_menu_open_ssh_from_tab(self, action, param):
        """Opens a terminal session based on the current SFTP tab's config."""
        # ✨ Use the last clicked tab if available, otherwise use current
        page_widget = self.last_clicked_tab if self.last_clicked_tab else self.notebook.get_nth_page(self.notebook.get_current_page())
        if not page_widget:
            return

        if page_widget in self.tab_data:
            tab_info = self.tab_data[page_widget]
            if tab_info["type"] == "sftp":
                self.start_session(tab_info["config"])