# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')

import os
import sys
import shlex
import copy
import json
import logging
import datetime
import re

from gi.repository import Gtk, Adw, Gdk, GLib, Vte, Pango, Gio

from .constants import APP_ID, COL_NAME, COL_TYPE, COL_ICON, COL_DATA, AI_STANDARD_PROVIDERS, CLI_STANDARD_PROVIDERS, WATERMARK_POSITIONS, resource_path, __version__
from .cli_providers import is_available as cli_is_available
from .dialogs import InputDialog, HostDialog, GroupDialog, BatchCommandDialog, QuickyDialog # Removed SettingsDialog
from .config import load_and_migrate_config, save_config, CONFIG_DIR
from .tab_window_base import TerminalPaneWindow, _tabview_has_page
from .detached_tab_window import DetachedTabWindow
from .launcher_icon import apply_launcher_icon
from .sftp_widget import SftpWidget
from .ai_panel import AiPanel
from .provider_badges import icon_name_for as _icon_name_for, badge_family as _badge_family, badge_text as _badge_text
from .widgets import PositionGrid, set_split_button_active_style
from . import settings_sync
import threading

from .i18n import _

# Window size/maximized state cache. Deliberately separate from
# settings.json (SettingsManager) — it's regenerated on every close and
# holds nothing a user would ever want to hand-edit or back up, so it
# doesn't belong mixed in with actual preferences.
WINDOW_STATE_FILE = CONFIG_DIR / "window_state.json"

# --- Main Window ---
class ThongSSHWindow(TerminalPaneWindow):

    last_clicked_quicky_index = None # Store the last right-clicked Quicky's index for its context menu actions

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # The header bar's own Adw.WindowTitle (set up below) is what's
        # actually visible day to day, but this is the window's *X11-level*
        # title (_NET_WM_NAME/WM_NAME) — read by Alt-Tab switchers, taskbar
        # tooltips, etc. Left unset, GTK defaults it to the process's
        # prgname (see app.py's GLib.set_prgname), which is APP_ID
        # ("terminal.thongssh") — technically correct-ish now, but still
        # not the friendly name a window switcher should show.
        self.set_title("ThongSSH")

        self.set_deletable(True)

        # Load and migrate the config
        self.config_data = load_and_migrate_config()

        # Real ThongSSHApp-level singletons (see app.py) — same object/dict
        # identity as every other window's, not fresh instances. Before
        # detachable tabs, these were instantiated fresh per-window
        # (SettingsManager/KeyringManager) or plain CLASS attributes
        # (tab_data/open_sessions) — both only "worked" because a second
        # top-level window never existed before; a DetachedTabWindow makes
        # that untrue.
        app = self.get_application()
        self.settings_manager = app.settings_manager
        self.keyring = app.keyring
        self.tab_data = app.tab_data
        self.open_sessions = app.open_sessions

        self._restore_window_geometry()
        self.connect("close-request", self._on_close_request)

        # Make the bundled icon resolvable by name even when no .desktop file
        # (or icon-theme install step) has registered it in hicolor. Reads
        # straight from the icons/ directory on disk — not the compiled
        # .gresource, which only updates on an explicit rebuild and used to
        # go stale silently whenever someone swapped the PNG on disk.
        icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
        icon_theme.add_search_path(resource_path("icons"))
        self.set_icon_name(self.settings_manager.get("interface.icon"))
        apply_launcher_icon(self.settings_manager.get("interface.icon"))

        self.setup_css()

        # --- 1. Main window structure ---
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(self.main_box)

        # --- 1.1. HeaderBar ---
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True) # Shows min/max/close

        # macOS + RTL (Hebrew/Arabic) header bar bug: see i18n.py's
        # apply_language_direction for why this can't be fixed here on the
        # header bar itself (verified empirically — no per-widget or even
        # per-window direction override changes AdwHeaderBar's native-
        # controls placement on macOS; it reads the process-wide default
        # direction directly, so there is no safe app-level workaround).

        title_widget = Adw.WindowTitle(title="ThongSSH", subtitle=__version__)
        header_bar.set_title_widget(title_widget)

        self.setup_global_menu(header_bar)

        # Single toggle for the whole AI panel, next to the system menu —
        # per-provider buttons live inside the panel's own header instead
        # (see AiPanel.provider_button_box / refresh_ai_provider_buttons).
        # Hidden by default; refresh_ai_provider_buttons() decides whether
        # anything is actually configured before showing it, and it stays
        # hidden entirely whenever Settings -> AI's "Disable AI" is on.
        self.ai_toggle_button = Gtk.ToggleButton(label=_("AI"))
        self.ai_toggle_button.set_tooltip_text(_("AI Chat"))
        self.ai_toggle_button.set_visible(False)
        self.ai_toggle_button.connect("toggled", self._on_ai_toggle_button_toggled)
        header_bar.pack_end(self.ai_toggle_button)

        # Plain button, not a toggle — every click just force-syncs. Hidden
        # entirely unless Settings -> Sync has it enabled (see
        # refresh_sync_button_visibility, called once here and again from
        # Settings' on_apply).
        # Adw.SplitButton, not a plain Gtk.Button: the main face is still a
        # single click = "sync now" (unchanged), but the small attached
        # arrow opens a menu for the rarer "Reset Sync State" action (see
        # win.sync-reset in setup_global_menu) — pointing sync.folder at a
        # genuinely new/empty share and wanting to seed it from this
        # machine is a real case perform_sync's own safety check can't
        # tell apart from "old folder just isn't mounted", so it needs an
        # explicit, deliberate action instead.
        self.sync_button = Adw.SplitButton()
        self.sync_button.set_icon_name("emblem-synchronizing-symbolic")
        self.sync_button.set_tooltip_text(_("Sync now"))
        self.sync_button.set_visible(False)
        self.sync_button.connect("clicked", self.on_sync_button_clicked)
        header_bar.pack_end(self.sync_button)

        # win.sync-reset itself is registered in setup_global_menu (called
        # earlier in __init__, before this button exists) — the menu model
        # just needs to be attached once the button actually does.
        sync_menu_model = Gio.Menu()
        sync_menu_model.append(_("Reset Sync State (Start Fresh)…"), "win.sync-reset")
        self.sync_button.set_menu_model(sync_menu_model)

        self.main_box.append(header_bar)
        self._ai_provider_buttons = {}

        self.sidebar_toggle_button = Gtk.ToggleButton(icon_name="go-previous-symbolic", active=True)
        self.sidebar_toggle_button.set_tooltip_text(_("Toggle Sidebar"))
        self.sidebar_toggle_button.connect("toggled", self.on_toggle_sidebar)
        header_bar.pack_start(self.sidebar_toggle_button)

        header_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.batch_command_button = Gtk.Button(icon_name="mail-send-symbolic")
        self.batch_command_button.set_tooltip_text(_("Batch Command"))
        self.batch_command_button.connect("clicked", self.on_menu_batch_command)
        header_bar.pack_start(self.batch_command_button)

        # Grouped with Batch Command, not the split-view buttons below —
        # all three (send a command, watermark, Quickies) act on/around the
        # terminal's content, whereas split-view is purely about layout.
        # Plain Adw.SplitButton — same widget the sync button uses, so the
        # two look visually consistent (AdwSplitButton is a *final*
        # GObject type, confirmed unsubclassable, hence no toggle-style
        # get_active()/set_active() of its own — see
        # widgets.set_split_button_active_style for how "on" is shown, and
        # on_watermark_toggle_clicked for where the actual on/off state
        # lives instead: interface.watermark_enabled). The arrow segment
        # opens a popover with the same 3x3 position grid Settings uses,
        # so the position can be changed without a trip through the full
        # Settings dialog.
        self.watermark_toggle_button = Adw.SplitButton()
        self.watermark_toggle_button.set_icon_name("insert-image-symbolic")
        self.watermark_toggle_button.set_tooltip_text(_("Toggle terminal watermark"))
        self.watermark_toggle_button.connect("clicked", self.on_watermark_toggle_clicked)

        self.watermark_position_grid = PositionGrid(self.settings_manager.get("interface.watermark_position"))
        self.watermark_position_grid.connect_changed(self._on_watermark_position_changed)
        watermark_position_popover = Gtk.Popover()
        watermark_position_popover.set_child(self.watermark_position_grid)
        self.watermark_toggle_button.set_popover(watermark_position_popover)

        # After pack_start, not before — a Gtk.StateFlags set on a widget
        # that isn't parented/realized yet doesn't survive the realize
        # that follows (confirmed live: True right after set_state_flags,
        # False again once actually packed+presented), unlike a real
        # widget property (e.g. a Gtk.ToggleButton's own "active").
        header_bar.pack_start(self.watermark_toggle_button)
        set_split_button_active_style(
            self.watermark_toggle_button, self.settings_manager.get("interface.watermark_enabled")
        )

        self.quickies_toggle_button = Gtk.ToggleButton(icon_name="media-seek-forward-symbolic")
        self.quickies_toggle_button.set_tooltip_text(_("Toggle Quickies panel"))
        self.quickies_toggle_button.set_active(self.settings_manager.get("quickies.enabled"))
        self.quickies_toggle_button.connect("toggled", self.on_quickies_toggle_clicked)
        header_bar.pack_start(self.quickies_toggle_button)

        header_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # "view-dual-symbolic" renders as a literal open-book icon on some
        # icon themes (Yaru, at least) — no relation to splitting a view.
        # System icon themes don't have a clean "2 equal panes" icon in the
        # same visual language as "view-grid-symbolic" (used below for the
        # 4-way split) — a CSS-rotated stand-in looked lopsided rotated —
        # so these two are bundled app icons instead (icons/split-*.svg),
        # drawn to match view-grid-symbolic's own style: same bordered-
        # square look, just 2 cells instead of 4. No rotation trick needed
        # since each is its own native drawing.
        self.split_vertical_btn = Gtk.Button(icon_name="split-columns-symbolic")
        self.split_vertical_btn.set_tooltip_text(_("Split view left/right"))
        self.split_vertical_btn.connect("clicked", lambda w: self.on_split_button_clicked("vertical"))
        header_bar.pack_start(self.split_vertical_btn)

        self.split_horizontal_btn = Gtk.Button(icon_name="split-rows-symbolic")
        self.split_horizontal_btn.set_tooltip_text(_("Split view top/bottom"))
        self.split_horizontal_btn.connect("clicked", lambda w: self.on_split_button_clicked("horizontal"))
        header_bar.pack_start(self.split_horizontal_btn)

        self.split_grid_btn = Gtk.Button(icon_name="view-grid-symbolic")
        self.split_grid_btn.set_tooltip_text(_("Split view into 4"))
        self.split_grid_btn.connect("clicked", lambda w: self.on_split_button_clicked("grid"))
        header_bar.pack_start(self.split_grid_btn)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_resize_start_child(False)
        # Shrinkable (not just draggable-wider) — the old fixed 300px floor
        # below couldn't be dragged past, which was too wide a minimum for
        # some host lists. Real floor now just enough to keep the icon +
        # a sliver of text visible/grabbable (see set_size_request below).
        self.paned.set_shrink_start_child(True)
        self.paned.set_vexpand(True)
        self.main_box.append(self.paned)

        # --- Full Left Panel (Tree [+ optional Quickies]) ---
        # left_panel always holds exactly one direct child — either
        # hosts_box alone, or a Gtk.Paned(VERTICAL) of hosts_box/quickies_box
        # (see _build_left_panel_root/_apply_left_panel_layout) — so
        # on_toggle_sidebar and the width-tracking below, which only ever
        # touch left_panel as a whole, need no changes for Quickies.
        self.left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.left_panel.set_size_request(80, -1) # Small floor only — default width is computed later (see on_first_map)
        self.paned.set_start_child(self.left_panel)
        self.left_panel.set_visible(True)

        # Everything that used to be appended straight into left_panel now
        # goes into hosts_box instead — see the block starting at
        # "self.tree_scrolled_window = Gtk.ScrolledWindow()" below.
        self.hosts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._left_panel_root = None

        # Ratio (not raw pixels — this Paned's own height varies with the
        # window) of the hosts/Quickies divider, tracked continuously below
        # and restored both across a Quickies off/on toggle (the Paned
        # itself is rebuilt from scratch each time — see
        # _build_left_panel_root/_apply_left_panel_layout) and across app
        # restarts (see _on_close_request/_load_window_state).
        self._last_quickies_split_ratio = self._load_window_state().get("quickies_split_ratio")

        # Continuously track the last dragged width (only while the sidebar
        # is actually visible — toggling it off via on_toggle_sidebar must
        # never overwrite this with a collapsed/meaningless value) so it can
        # be persisted and restored on next launch, mirroring the
        # _last_normal_size window-geometry pattern below.
        self._last_left_panel_width = None
        def _track_left_panel_width(*_args):
            if self.left_panel.get_visible():
                self._last_left_panel_width = self.paned.get_position()
        self.paned.connect("notify::position", _track_left_panel_width)

        # Permanent end child of self.paned — the actual per-split-mode
        # widget tree (see _apply_pane_layout) is swapped in and out as
        # *its* child, so overlaid widgets (currently just the in-terminal
        # find bar, see _build_find_window) stay anchored to a fixed spot
        # regardless of which split layout is showing underneath.
        self.terminal_overlay = Gtk.Overlay()
        self.terminal_overlay.set_hexpand(True)
        self.terminal_overlay.set_vexpand(True)

        # AI chat panel — hidden by default, revealed by a header-bar
        # provider button (see refresh_ai_provider_buttons). Built once and
        # never destroyed, so collapsing it never loses conversation state.
        self.ai_panel = AiPanel(self, self.settings_manager, self.keyring)
        self.ai_panel.set_visible(False)

        # Nested Paned so the AI panel gets its own independent, resizable,
        # cacheable width without disturbing self.paned (left sidebar) or
        # the split/tab machinery, which is keyed entirely off
        # self.terminal_overlay's own children (see _apply_pane_layout).
        self.center_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.center_paned.set_start_child(self.terminal_overlay)
        self.center_paned.set_resize_start_child(True)
        self.center_paned.set_shrink_start_child(True)
        self.center_paned.set_end_child(self.ai_panel)
        self.center_paned.set_resize_end_child(False)
        self.center_paned.set_shrink_end_child(True)
        self.center_paned.set_vexpand(True)
        self.center_paned.set_hexpand(True)

        self._last_ai_panel_width = None
        def _track_ai_panel_width(*_args):
            if self.ai_panel.get_visible():
                total = self.center_paned.get_width()
                position = self.center_paned.get_position()
                if total > 0:
                    self._last_ai_panel_width = total - position
        self.center_paned.connect("notify::position", _track_ai_panel_width)

        self.paned.set_end_child(self.center_paned)


        
        # --- SearchBar (The correct way for GTK4) ---
        # Always revealed — there's no dedicated toggle button anymore, the
        # bar is just a permanent part of the host panel. "Activating" it
        # (click, or Ctrl+F from anywhere) only needs to move focus into it.
        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_search_mode(True)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        # No magnifying-glass icon — the entry's own dimmed placeholder text
        # already signals "this is a search box" (and disappears the moment
        # the user types, with no filtering effect of its own), which reads
        # cleaner than an icon competing with the host list for attention.
        self.search_entry.set_placeholder_text(_("Search"))
        # GtkSearchEntry's leading magnifying-glass icon is its own first
        # child widget with no distinct style class CSS can target (min-
        # width/opacity tricks don't shrink it — it keeps its intrinsic
        # icon size regardless). Hiding that child directly is stable: the
        # entry never re-toggles its visibility itself, and the trailing
        # "clear" icon (shown once there's text) is a separate child that
        # keeps working normally.
        _leading_icon = self.search_entry.get_first_child()
        if _leading_icon is not None:
            _leading_icon.set_visible(False)
        search_box.append(self.search_entry)

        self.search_up_button = Gtk.Button(icon_name="go-up-symbolic")
        self.search_down_button = Gtk.Button(icon_name="go-down-symbolic")
        # "flat" + "circular" (the standard GNOME compact-icon-button combo)
        # instead of full-size bordered buttons — these are just a pair of
        # nav arrows next to a search box, not primary actions.
        for _btn in (self.search_up_button, self.search_down_button):
            _btn.add_css_class("flat")
            _btn.add_css_class("circular")
        search_box.append(self.search_up_button)
        search_box.append(self.search_down_button)

        self.search_bar.set_child(search_box)
        self.search_results = []
        self.current_search_index = -1

        self.tree_scrolled_window = Gtk.ScrolledWindow()
        self.tree_scrolled_window.set_vexpand(True)
        self.tree_scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.hosts_box.append(self.tree_scrolled_window)

        # Tree model (4 columns). Using Python types works when GObject is correctly imported.
        self.main_tree_store = Gtk.TreeStore(str, str, str, object)
        self.view_tree_store = self.main_tree_store # The model for display (can be changed)
        self.is_filtered = False # Flag indicating if a filter is active


        # --- Sorting setup ---
        def sort_func(model, iter1, iter2, user_data):
            type1 = model.get_value(iter1, COL_TYPE)
            type2 = model.get_value(iter2, COL_TYPE)

            # The synthetic "local machine" row always sorts first, then
            # groups, then hosts.
            rank = {"local": 0, "group": 1, "host": 2}
            rank1, rank2 = rank.get(type1, 3), rank.get(type2, 3)
            if rank1 != rank2:
                return -1 if rank1 < rank2 else 1

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
        self.tree_view.set_enable_search(False)

        # Renderers
        renderer_pixbuf = Gtk.CellRendererPixbuf()
        renderer_text = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(_("Hosts"))
        column.pack_start(renderer_pixbuf, False)
        column.pack_start(renderer_text, True)

        column.add_attribute(renderer_text, "text", COL_NAME)
        column.add_attribute(renderer_pixbuf, "icon-name", COL_ICON)

        # ✨ Optional, very subtle alternating-row tint (interface.tree_row_striping
        # setting) — set as a per-cell data func rather than baked-in attributes
        # since it needs to react live to both the setting and the current accent
        # color, not just row data.
        column.set_cell_data_func(renderer_pixbuf, self._tree_row_cell_data_func)
        column.set_cell_data_func(renderer_text, self._tree_row_cell_data_func)

        self.tree_view.append_column(column)
        self.tree_scrolled_window.set_child(self.tree_view)

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

        self.hosts_box.append(self.search_bar)
        self.apply_search_bar_position()

        # --- (GTK4 Menu) ---
        self.setup_actions_and_popovers()
        # --- ---

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_halign(Gtk.Align.CENTER)
        # hosts_box's own spacing=6 only inserts a gap *between* siblings, so
        # this (the LAST child) naturally gets a 6px gap above it (from
        # search_bar before it) but 0px below (nothing after it to space
        # against) — it visually hugs the Paned divider on one side only.
        # Mirroring that same 6px below makes it sit centered between the
        # divider above and the one below, instead of pinned to the latter.
        button_box.set_margin_bottom(6)

        add_host_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_host_btn.set_tooltip_text(_("Add Host"))
        add_host_btn.set_valign(Gtk.Align.CENTER)
        add_host_btn.connect("clicked", self.on_add_host_clicked)

        add_group_btn = Gtk.Button(icon_name="folder-new-symbolic")
        add_group_btn.set_tooltip_text(_("Create Group"))
        add_group_btn.set_valign(Gtk.Align.CENTER)
        add_group_btn.connect("clicked", self.on_add_group_clicked)

        remove_btn = Gtk.Button(icon_name="list-remove-symbolic")
        remove_btn.set_tooltip_text(_("Remove Selected"))
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.connect("clicked", self.on_remove_selected_clicked, None)

        button_box.append(add_host_btn)
        button_box.append(add_group_btn)
        button_box.append(remove_btn)
        # The collapse button is now in the HeaderBar
        self.hosts_box.append(button_box)

        self._build_quickies_box()
        self._apply_left_panel_layout()

        # --- Right Panel (Tabs, with up to 4-way split support) ---
        # Four persistent "panes" (an Adw.TabBar + Adw.TabView pair, wrapped
        # in a Gtk.Box — see _create_pane_tabview) are created up front and
        # never destroyed — the split-view buttons only ever reparent their
        # boxes into a different Gtk.Paned tree and move pages between the
        # TabViews, so terminal PIDs / SFTP connections / tab_data entries
        # (keyed by Adw.TabPage) stay valid across split/merge/orientation
        # changes.
        self.split_mode = None  # None | 'vertical' | 'horizontal' | 'grid'
        self._pane_boxes = []
        self.pane_tabviews = []
        self.pane_tab_bars = []
        for _i in range(4):
            _box, _tabview, _tab_bar = self._create_pane_tabview()
            self._pane_boxes.append(_box)
            self.pane_tabviews.append(_tabview)
            self.pane_tab_bars.append(_tab_bar)
        self._pane_box_by_tabview = dict(zip(self.pane_tabviews, self._pane_boxes))
        self.active_pane = None
        self._set_active_pane(self.pane_tabviews[0])
        self._apply_pane_layout()

        self.connect("map", self.on_first_map)

        # ✨ Connect signals to update menu sensitivity
        self.tree_view.get_selection().connect("changed", self.update_menu_sensitivity)
        self.update_menu_sensitivity()

        # ✨ Add a global key controller for shortcuts like Ctrl+W / Ctrl+F.
        # CAPTURE phase so it sees the event on the way down, before it
        # reaches a descendant like Vte.Terminal — which otherwise consumes
        # keys like Ctrl+F itself (as terminal input) before they'd ever
        # bubble back up to a default-phase window controller.
        key_controller_window = Gtk.EventControllerKey.new()
        key_controller_window.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller_window.connect("key-pressed", self.on_window_key_pressed)
        self.add_controller(key_controller_window)

        # Build the header-bar's per-provider AI buttons now that
        # self.keyring, self.settings_manager and self.ai_panel all exist.
        self.refresh_ai_provider_buttons()

        self.sync_timer_id = None
        self.sync_in_progress = False
        self.refresh_sync_button_visibility()
        self.restart_sync_timer()

    def _load_window_state(self):
        try:
            with open(WINDOW_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            return {}

    def _save_window_state(self, state):
        """Atomic write (temp file + rename) so a crash mid-write can never
        leave a half-written, unparseable cache file behind."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = WINDOW_STATE_FILE.with_suffix(".tmp")
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, WINDOW_STATE_FILE)
        except OSError as e:
            logging.error(f"Failed to save window state: {e}")

    def _max_monitor_size(self):
        """Largest available width/height across all connected monitors, so
        a saved size can be clamped to fit instead of being discarded
        outright the moment it's too big for whichever monitor is
        current — e.g. after unplugging an external display. Falls back to
        the 1024x768 default if monitor info isn't available yet (can
        happen this early at startup)."""
        display = self.get_display()
        monitors = display.get_monitors() if display else None
        if monitors is None or monitors.get_n_items() == 0:
            return 1024, 768
        max_w = max(monitors.get_item(i).get_geometry().width for i in range(monitors.get_n_items()))
        max_h = max(monitors.get_item(i).get_geometry().height for i in range(monitors.get_n_items()))
        return max_w, max_h

    def _restore_window_geometry(self):
        """Restores the window size (and maximized state) saved when the
        window was last closed. The saved size is clamped — not discarded —
        if it no longer fits any current monitor, so it degrades gracefully
        instead of jumping back to a hardcoded default whenever the screen
        setup changes. Note: only size/maximized state is persisted, not
        position — GTK4 dropped window-position APIs entirely
        (gtk_window_move/get_position don't exist any more), since Wayland
        treats placement as the compositor's call, not the client's."""
        state = self._load_window_state()
        default_width, default_height = 1024, 768
        width = state.get("width") or default_width
        height = state.get("height") or default_height

        max_w, max_h = self._max_monitor_size()
        width = min(width, max_w)
        height = min(height, max_h)

        self.set_default_size(width, height)
        logging.debug(f"Window geometry restored: {width}x{height} (saved state: {state})")

        # Continuously track the last known *unmaximized* size — rather than
        # only reading it once at close time — since default-width/height
        # can lag behind an in-progress interactive resize; this way,
        # whatever the most recent settled value was is always on hand.
        self._last_normal_size = (width, height)
        def track_normal_size(*_args):
            if not self.is_maximized():
                self._last_normal_size = self.get_default_size()
                logging.debug(f"Tracked normal size: {self._last_normal_size}")
        self.connect("notify::default-width", track_normal_size)
        self.connect("notify::default-height", track_normal_size)
        # Also resync right on the maximized<->normal transition itself,
        # since that's the one moment GTK is guaranteed to have just
        # recomputed the "size to restore to".
        self.connect("notify::maximized", track_normal_size)

        if state.get("maximized"):
            self.maximize()

    def _on_close_request(self, *args):
        """Persists the window's current size and maximized state so the
        next launch reopens at the same geometry. Uses the continuously
        tracked _last_normal_size (see _restore_window_geometry) rather than
        querying get_default_size() fresh here, since that query has been
        observed to occasionally return a stale/default value right at
        close time."""
        width, height = getattr(self, "_last_normal_size", None) or self.get_default_size()
        maximized = self.is_maximized()
        state = {}
        if width > 0 and height > 0:
            state["width"] = width
            state["height"] = height
        state["maximized"] = maximized
        if getattr(self, "_last_left_panel_width", None):
            state["left_panel_width"] = self._last_left_panel_width
        if getattr(self, "_last_ai_panel_width", None):
            state["ai_panel_width"] = self._last_ai_panel_width
        if getattr(self, "_last_quickies_split_ratio", None):
            state["quickies_split_ratio"] = self._last_quickies_split_ratio
        logging.debug(f"Saving window state on close: {state}")
        self._save_window_state(state)
        return False # Allow the window to close

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
        .thongssh-active-pane {
            /* box-shadow (not border!) — a border adds to the widget's size
               requisition, which made the pane visibly grow/jump in the
               Paned the instant it became active. box-shadow is paint-only.
               Kept thin and low-opacity on purpose — this is meant to be a
               subtle hint of which pane is active, not a hard outline. */
            box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.35);
        }
        /* Watermark header-bar button's "on" look (set_split_button_active_
           style, widgets.py) — same subtle shade Adwaita itself uses for a
           checked flat button/split-button (its own stylesheet has this as
           color-mix(in srgb, currentColor 7%, transparent); alpha() here
           instead — functionally the same at this low a percentage, but
           understood by older GTK4 too, e.g. the AppImage's bundled 4.10.5,
           which the newer color-mix() syntax isn't). Deliberately NOT the
           accent-colored "suggested-action" class — this app's other
           toggle-style header buttons (Gtk.ToggleButton, whose real
           :checked state gets this shading for free) don't stand out with
           a bright color either, just this. */
        .watermark-toggle-active {
            background: alpha(currentColor, 0.07);
        }
        /* Compact tab headers — the theme's own default notebook-tab
           padding plus a full-size flat button for the close "x" adds up
           to a lot of dead space per tab, most visible once there are
           enough tabs to make the strip scrollable. These override that
           down to just enough to stay clickable/legible. */
        notebook > header > tabs > tab {
            padding: 2px 4px;
            min-height: 0;
        }
        .thongssh-tab-close {
            padding: 0;
            min-width: 20px;
            min-height: 20px;
        }
        /* Visible divider between the host tree and Quickies panels (see
           _build_left_panel_root) — two earlier attempts didn't read as a
           real divider: a bare margin-gap was too subtle (both panels
           share the same light background), and a single solid bar across
           the whole handle just looked like a drop-shadow cast by the
           panel above, not a deliberate boundary. A tiny linear-gradient
           on the handle itself (line/gap/line in one strip) didn't work
           out either — at only a few px tall it rendered as a blurred
           smear, not two crisp lines; GTK's gradient rendering isn't
           pixel-hard at that scale. What actually gives two distinct
           lines is simpler: a real border on each panel's own facing
           edge (thongssh-panel-divider-top/-bottom, applied in
           _build_left_panel_root to whichever box ends up on which side),
           with the handle's own natural thickness between them as the
           gap. alpha() (not color-mix()) for the same older-GTK4
           compatibility reason as .watermark-toggle-active above. */
        .thongssh-panel-divider-top {
            border-bottom: 1px solid alpha(currentColor, 0.25);
        }
        .thongssh-panel-divider-bottom {
            border-top: 1px solid alpha(currentColor, 0.25);
        }
        .terminal-watermark {
            /* Color/size/opacity are set per-label from Settings (they're
               dynamic values, not fixed classes) — this just keeps the text
               from picking up any background/border a plain label might
               otherwise inherit. */
            font-weight: 600;
            background: none;
        }
        .ai-bubble-user {
            background-color: alpha(@accent_color, 0.12);
            border-radius: 8px;
            padding: 4px 8px;
        }
        .ai-bubble-assistant {
            background-color: alpha(currentColor, 0.06);
            border-radius: 8px;
            padding: 4px 8px;
        }
        .ai-typing-indicator {
            background-color: alpha(@accent_color, 0.15);
            border-radius: 8px;
            padding: 8px 12px;
        }
        /* Per-message avatar, at the start of every chat bubble — a plain
           icon (user) or a small badge (assistant: real provider logo if
           provider_badges.py has one, else initials). ai-avatar sizes the
           square; ai-avatar-badge is only the text-initials fallback case,
           tinted via whichever .ai-provider-badge-* class is also applied
           (background-color here rides on "color" set by that class, so
           it stays in sync with the header-bar buttons with no per-family
           repetition needed). */
        .ai-avatar {
            min-width: 22px;
            min-height: 22px;
        }
        .ai-avatar-user {
            color: alpha(currentColor, 0.6);
        }
        .ai-avatar-badge {
            background-color: alpha(currentColor, 0.25);
            border-radius: 6px;
            font-weight: bold;
            font-size: 10px;
        }
        .markdown-code-block {
            background-color: alpha(currentColor, 0.08);
            border-radius: 6px;
            padding: 2px 6px;
            margin: 2px 0;
        }
        /* Every chat-message segment is a Gtk.TextView, whose default
           ".view" styling paints its own input-field-like background —
           without this override, each segment (plain text, list, code)
           showed up as its own separate white/boxed field instead of one
           flowing bubble. Both the "textview" node itself and its "text"
           sub-node (where GTK4 actually paints the content background)
           need the override. */
        textview.markdown-plain-text,
        textview.markdown-plain-text text {
            background-color: transparent;
        }
        textview.markdown-code-text,
        textview.markdown-code-text text {
            background-color: transparent;
            font-family: Monospace;
        }
        /* Same footprint as the plain icon buttons next to it (batch
           command, split view, ...) — was previously sized by its text
           label alone and looked oversized/inconsistent next to them. */
        .ai-provider-button {
            min-width: 22px;
            min-height: 22px;
            padding: 2px 4px;
            font-size: 11px;
            font-weight: bold;
        }
        /* Per-provider tint — some are a real logo (recolored symbolic
           icon), some fall back to plain initials (see provider_badges.py),
           either way just a distinguishing hue. Color-only when
           unchecked; a background fill only appears on :checked, so pressed
           vs. unpressed stays obvious instead of both looking identically
           tinted (that was the "can't tell if it's pressed" bug). */
        .ai-provider-badge-claude { color: #cc785c; }
        .ai-provider-badge-claude:checked { background-color: alpha(#cc785c, 0.35); }
        .ai-provider-badge-gemini { color: #4285f4; }
        .ai-provider-badge-gemini:checked { background-color: alpha(#4285f4, 0.35); }
        .ai-provider-badge-chatgpt { color: #10a37f; }
        .ai-provider-badge-chatgpt:checked { background-color: alpha(#10a37f, 0.35); }
        .ai-provider-badge-grok { color: #6b6b6b; }
        .ai-provider-badge-grok:checked { background-color: alpha(#6b6b6b, 0.35); }
        .ai-provider-badge-deepseek { color: #4d6bfe; }
        .ai-provider-badge-deepseek:checked { background-color: alpha(#4d6bfe, 0.35); }
        .ai-provider-badge-custom { color: @accent_color; }
        .ai-provider-badge-custom:checked { background-color: alpha(@accent_color, 0.35); }
        /* CLI-tool buttons (local subprocess, not a network API) — same
           color language as their API counterparts, distinguished only by
           tooltip/hue, not an extra marking on the button itself. */
        .ai-provider-badge-cli-claude { color: #cc785c; }
        .ai-provider-badge-cli-claude:checked { background-color: alpha(#cc785c, 0.35); }
        .ai-provider-badge-cli-codex { color: #9b59b6; }
        .ai-provider-badge-cli-codex:checked { background-color: alpha(#9b59b6, 0.35); }
        .ai-provider-badge-cli-custom { color: @accent_color; }
        .ai-provider-badge-cli-custom:checked { background-color: alpha(@accent_color, 0.35); }
        /* Quickies' Edit/Delete buttons — "flat" alone still keeps the
           theme's normal button min-size (taller than a plain text line);
           this shrinks the clickable box itself down to match. */
        .quicky-tiny-button {
            min-width: 20px;
            min-height: 20px;
            padding: 2px;
        }
        """
        if sys.platform == "darwin":
            # Modern macOS clips every NSWindow to a rounded rect on all
            # four corners (not just the top, unlike GNOME's traditional
            # CSD convention) — GTK's own painted background only rounds
            # the top two by default, so its square bottom corners fall
            # outside the OS's rounded mask and get cut away, letting
            # whatever's behind the window show through underneath. Rounding
            # the bottom here too keeps GTK's own background inside that
            # native mask everywhere, not just at the top.
            css_data += """
            window.background {
                border-radius: 10px;
            }
            """
        # load_from_data (not load_from_string, which needs GTK 4.12+) —
        # keeps this working on older GTK4 (e.g. the 4.10.5 the AppImage
        # bundles for Ubuntu 22.04, and Ubuntu 22.04/24.04's own system GTK).
        # Binding signature varies by GTK4/PyGObject version: newer ones take
        # a single bytes-like arg, older ones need an explicit length
        # (-1 = "null-terminated") alongside the original str.
        try:
            css_provider.load_from_data(css_data.encode("utf-8"))
        except TypeError:
            css_provider.load_from_data(css_data, -1)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def rebuild_config_and_save(self):
        """Parses the Gtk.TreeStore and saves it to hosts.json."""
        logging.debug("Saving tree to config...")

        def iter_tree(model, tree_iter):
            """Recursively parses the Gtk.TreeStore into a dict."""
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

    def _tree_row_cell_data_func(self, column, cell, model, tree_iter, data=None):
        """Applies the optional interface.tree_row_striping tint. Off by
        default — when on, every other row gets a barely-there accent-color
        wash so rows are easier to track by eye without turning into a hard
        highlight.

        Parity is the row's position among its own siblings (last path
        index), not a true flattened visual row number — GtkTreeView has no
        cheap way to compute "the Nth visible row" without walking the whole
        model on every redraw. For this tree's shape (a couple of levels of
        groups/hosts) that's an acceptable approximation of a zebra pattern,
        not a literal one."""
        if not self.settings_manager.get("interface.tree_row_striping"):
            cell.set_property("cell-background-set", False)
            return

        path = model.get_path(tree_iter)
        if path.get_indices()[-1] % 2 == 0:
            cell.set_property("cell-background-set", False)
            return

        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        tint = Gdk.RGBA()
        tint.red, tint.green, tint.blue, tint.alpha = accent.red, accent.green, accent.blue, 0.08
        cell.set_property("cell-background-rgba", tint)

    def populate_tree(self):
        self.main_tree_store.clear()

        # ✨ Synthetic "local machine" entry — always first (see sort_func),
        # never part of hosts.json / config_data, so it's neither saved by
        # rebuild_config_and_save() (which only understands "group"/"host"
        # nodes) nor editable/removable (guarded in on_remove_selected_clicked
        # and skipped in on_tree_right_click).
        # "local" is a fixed label, not translated — like "ThongSSH" itself,
        # it's meant to read the same in every language (user request).
        local_config = {"name": "local", "protocol": "local"}
        self.main_tree_store.append(None, [local_config["name"], "local", "computer-symbolic", local_config])

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
            if node_type in ("host", "local"):
                host_config = model.get_value(tree_iter, COL_DATA)
                logging.info(f"Connecting to: {host_config['name']}")
                self.start_session(host_config)
            elif node_type == "group":
                if tree_view.row_expanded(path):
                    tree_view.collapse_row(path)
                else:
                    tree_view.expand_row(path, False)

    def on_first_map(self, *args):
        """Set the initial position of the paned divider: the width dragged
        last session if one was cached, otherwise a width computed to just
        fit the longest name currently in the host tree."""
        state = self._load_window_state()
        width = state.get("left_panel_width") or self._compute_default_left_panel_width()
        self.paned.set_position(width)
        # Disconnect the handler so it only runs once
        self.disconnect_by_func(self.on_first_map)

    def _compute_default_left_panel_width(self):
        """Default sidebar width: just enough to fit the longest host/group
        name without truncation, plus a fixed allowance for the icon column
        and tree expander indentation. Only used when no width was dragged
        (and thus cached) in a previous session."""
        max_text_width = 0

        def walk(tree_iter):
            nonlocal max_text_width
            while tree_iter is not None:
                name = self.main_tree_store.get_value(tree_iter, COL_NAME)
                layout = self.tree_view.create_pango_layout(name)
                max_text_width = max(max_text_width, layout.get_pixel_size()[0])
                walk(self.main_tree_store.iter_children(tree_iter))
                tree_iter = self.main_tree_store.iter_next(tree_iter)

        walk(self.main_tree_store.get_iter_first())
        # Icon column + expander indent + row/window padding allowance.
        return max(200, min(600, max_text_width + 80))

    # --- Split-pane layout (up to 4 independent tab panes) ---
    #
    # Slots: 0=top-left (also "single"/"left"/"top"), 1=top-right (also
    # "right" in a 2-way vertical split), 2=bottom-left (also "bottom" in a
    # 2-way horizontal split), 3=bottom-right (grid only).
    #
    # Invariant: whenever split_mode is 'vertical' or 'horizontal' (a 2-way
    # split), the two live panes are always pane_tabviews[0] and [1] —
    # only the Paned orientation differs. This is what lets switching
    # between vertical/horizontal just re-orient the same two panes with no
    # tab movement at all.

    def _create_pane_tabview(self):
        """Builds one persistent tab 'pane': an Adw.TabBar over an
        Adw.TabView, wrapped in a plain Gtk.Box so the pair threads through
        _build_pane_layout_widget's Gtk.Paned tree as a single leaf widget.
        All 4 are created once in __init__ and only ever reparented/emptied
        — never destroyed — so widgets keyed in open_sessions/tab_data stay
        valid across layout changes. Returns (box, tabview, tab_bar)."""
        tabview = Adw.TabView()
        tabview.set_vexpand(True)
        tabview.set_hexpand(True)
        # Switching tabs within a pane can change which terminal is "the
        # active terminal" for watermark scope="active", independently of
        # _set_active_pane (which only tracks the active *pane*).
        tabview.connect("notify::selected-page", self._on_pane_page_changed)
        # Native Adw.TabView plumbing — drag-to-reorder within a pane and
        # drag-to-another-pane's-TabView both come for free from this (no
        # custom Gtk.DragSource/DropTarget needed any more; see
        # on_tabview_close_page/setup_menu/create_window/page_detached in
        # tab_window_base.py for what each signal does).
        tabview.connect("close-page", self.on_tabview_close_page)
        tabview.connect("setup-menu", self.on_tabview_setup_menu)
        tabview.connect("create-window", self.on_tabview_create_window)
        tabview.connect("page-detached", self.on_tabview_page_detached)
        tabview.set_menu_model(self.tab_menu_model)

        tab_bar = Adw.TabBar()
        tab_bar.set_view(tabview)
        tab_bar.set_autohide(False)

        # "+" new-local-terminal button, packed as the tab bar's own START
        # action widget — same "always at the left edge" placement the old
        # Gtk.Notebook action widget had.
        new_local_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_local_btn.set_tooltip_text(_("New local terminal"))
        new_local_btn.add_css_class("flat")
        new_local_btn.connect("clicked", self._on_new_local_terminal_clicked, tabview)
        tab_bar.set_start_action_widget(new_local_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # ✨ Small margin to prevent accidentally grabbing a paned handle
        box.set_margin_start(6)
        box.append(tab_bar)
        box.append(tabview)

        # Track "last interacted-with pane" as the active one — attached to
        # the whole pane box (tab bar + tabview) so clicking either counts,
        # same as a click anywhere on the old combined Gtk.Notebook widget
        # did. This used to rely solely on keyboard-focus "enter" (below),
        # acting here only for a click on a pane with zero pages (nothing
        # inside it to focus). But on GNOME/Wayland, rapidly alternating
        # clicks between two panes can make the focus-enter notification
        # lag or get dropped, leaving active_pane stuck on whichever pane
        # last reliably reported it — so this now fires on every press, in
        # capture phase, as a passive observer (never claims/denies the
        # sequence) that can't interfere with clicks meant for a tab, a
        # terminal, or anything else already inside the pane.
        click_controller = Gtk.GestureClick.new()
        click_controller.set_button(0)  # any button
        click_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        def on_pane_pressed(gesture, n_press, x, y, tv=tabview):
            self._set_active_pane(tv)
        click_controller.connect("pressed", on_pane_pressed)
        box.add_controller(click_controller)

        focus_controller = Gtk.EventControllerFocus.new()
        focus_controller.connect("enter", lambda c, tv=tabview: self._set_active_pane(tv))
        box.add_controller(focus_controller)

        return box, tabview, tab_bar

    def _on_pane_page_changed(self, tabview, pspec):
        self.update_menu_sensitivity()
        self.apply_watermark_settings_to_all()

    def _on_new_local_terminal_clicked(self, button, tabview):
        """Opens a new local-terminal tab in this pane. If
        terminal.inherit_cwd_for_new_local_tab is on (the default), starts
        in the same directory as this pane's currently active tab, IF that
        tab is itself a local terminal (read via /proc/<pid>/cwd — the
        actual live cwd of its shell, not just wherever it started out) —
        otherwise, or with the setting off, falls back to $HOME. Named
        "local: <dir>" (short form: "~" for home, or the last path
        component otherwise) so several local tabs stay distinguishable at
        a glance."""
        cwd = os.environ.get("HOME", os.path.expanduser("~"))
        current_page = tabview.get_selected_page()
        if current_page is not None and self.settings_manager.get("terminal.inherit_cwd_for_new_local_tab"):
            info = self.tab_data.get(current_page)
            if info and info.get("type") == "terminal" and info.get("config", {}).get("protocol") == "local":
                session = self.open_sessions.get(current_page)
                if session:
                    _terminal, pid = session
                    try:
                        cwd = os.readlink(f"/proc/{pid}/cwd")
                    except OSError:
                        pass  # e.g. macOS (no /proc), or the process already exited

        label = self._dir_short_label(cwd)
        self._set_active_pane(tabview)
        self.start_session({"name": f"local: {label}", "protocol": "local", "cwd": cwd})

    def _set_active_pane(self, tabview):
        if self.active_pane is tabview:
            return
        if self.active_pane is not None:
            self._pane_box_by_tabview[self.active_pane].remove_css_class("thongssh-active-pane")
        self.active_pane = tabview
        self._pane_box_by_tabview[tabview].add_css_class("thongssh-active-pane")
        self._sync_find_target_terminal()
        # Guarded: this fires once during __init__ before the watermark
        # toggle button exists yet, and open_sessions is always empty at
        # that point anyway.
        if hasattr(self, "watermark_toggle_button"):
            self.apply_watermark_settings_to_all()

    def _get_active_tabview(self):
        """The pane new tabs should open into / menu actions should target."""
        if self.active_pane not in self.pane_tabviews:
            self._set_active_pane(self.pane_tabviews[0])
        return self.active_pane

    def _find_tabview_for_page(self, page):
        """Which pane currently holds this tab's page, if any. Overrides
        TerminalPaneWindow's single-tabview default since this window has
        up to 4 — the target page may not be the one in the currently
        *active* pane."""
        for tv in self.pane_tabviews:
            if _tabview_has_page(tv, page):
                return tv
        return None

    def _pane_tabview_under_pointer(self, exclude):
        """Overrides TerminalPaneWindow's default (which has nothing else
        to check) — see on_tabview_create_window's macOS workaround for
        why this exists. Only considers panes actually part of the visible
        Gtk.Paned tree right now (get_mapped()) — with e.g. split_mode
        None, panes 1-3 still exist and are still valid Adw.TabView
        objects, just parked off-tree, so a stale translate_coordinates
        against one of those would be meaningless at best."""
        display = self.get_display()
        if display is None:
            return None
        seat = display.get_default_seat()
        pointer = seat.get_pointer() if seat else None
        surface = self.get_surface()
        if pointer is None or surface is None:
            return None
        ok, px, py, _mask = surface.get_device_position(pointer)
        if not ok:
            return None
        for tv in self.pane_tabviews:
            if tv is exclude:
                continue
            box = self._pane_box_by_tabview.get(tv)
            if box is None or not box.get_mapped():
                continue
            # translate_coordinates' return arity is inconsistent across
            # GTK/PyGObject versions in the wild — some drop the leading
            # success bool and just return (x, y), others keep (ok, x, y).
            # Handle both rather than assume one (this crashed here once
            # already — see on_tabview_create_window's docstring).
            result = box.translate_coordinates(self, 0, 0)
            if len(result) == 3:
                ok, bx, by = result
            else:
                bx, by = result
                ok = True
            if not ok:
                continue
            if bx <= px <= bx + box.get_width() and by <= py <= by + box.get_height():
                return tv
        return None

    def _get_region_options(self):
        """The (key, label) regions selectable in Batch Command's div
        filter: the current split-mode's panes, PLUS one entry per
        currently-open detached window. A detached tab is conceptually
        just another "div" the user split off (the user's own framing,
        after "it kind of breaks the Batch Command concept" — a detached
        tab visibly disappearing from Batch Command the moment ANY split
        filtering exists at all, rather than getting its own filterable
        entry, was a real bug, not the "acceptable emergent behavior" it
        was first assumed to be) — so it gets a real entry here instead of
        silently falling through _find_region_key_for_page as unfiltered/
        excluded. Empty only when there's a single pane AND no detached
        windows open (nothing at all to filter by)."""
        if self.split_mode == "vertical":
            options = [("left", _("Left")), ("right", _("Right"))]
        elif self.split_mode == "horizontal":
            options = [("top", _("Top")), ("bottom", _("Bottom"))]
        elif self.split_mode == "grid":
            options = [
                ("top-left", _("Top-Left")), ("top-right", _("Top-Right")),
                ("bottom-left", _("Bottom-Left")), ("bottom-right", _("Bottom-Right")),
            ]
        else:
            options = []
        detached_windows = self._get_detached_windows()
        if detached_windows:
            if not options:
                # No split active — the single unsplit pane needs its OWN
                # region entry now too, or its own tabs would have nowhere
                # to belong the moment a detached-window entry exists at
                # all (every open tab needs SOME matching checkbox, or
                # region filtering silently excludes it — exactly the bug
                # a detached tab itself used to hit, just shifted onto the
                # main pane's tabs instead).
                options.append(("main", _("Main Window")))
            for win in detached_windows:
                title = win.window_title.get_title() or _("Detached")
                options.append((f"detached:{id(win)}", _("Detached: {title}").format(title=title)))
        return options

    def _get_detached_windows(self):
        """Every currently-open DetachedTabWindow — used by
        _get_region_options/_find_region_key_for_page. Order isn't
        guaranteed beyond whatever Gtk.Application.get_windows() returns,
        which is fine here (each is labeled by its own live title, not by
        position)."""
        return [w for w in self.get_application().get_windows() if isinstance(w, DetachedTabWindow)]

    def _pane_region_label(self, tabview):
        """Maps one of THIS window's own pane TabViews to its region key
        under the current split mode (see _get_region_options) — "main"
        when there's no split active at all (only actually consulted when
        _get_region_options() emitted a "main" option too, which it only
        does once a detached window exists; harmless otherwise, since
        nothing looks at region labels while region_checks is empty).
        None if `tabview` isn't one of this window's 4 panes at all —
        callers that also need to recognize a detached window's own pane
        should use _find_region_key_for_page instead, which checks both."""
        if tabview is None:
            return None
        p0, p1, p2, p3 = self.pane_tabviews
        if self.split_mode == "vertical":
            return {p0: "left", p1: "right"}.get(tabview)
        elif self.split_mode == "horizontal":
            return {p0: "top", p1: "bottom"}.get(tabview)
        elif self.split_mode == "grid":
            return {p0: "top-left", p1: "top-right", p2: "bottom-left", p3: "bottom-right"}.get(tabview)
        elif tabview in (p0, p1, p2, p3):
            return "main"
        return None

    def _find_region_key_for_page(self, page):
        """Which region key (see _get_region_options) currently holds
        `page` — one of this window's own split panes, or a currently-open
        detached window's single pane. Used by BatchCommandDialog instead
        of the old _find_tabview_for_page()+_pane_region_label() combo,
        which had no way to represent "this tab lives in a detached
        window" at all and silently excluded it from every filtered
        result instead."""
        tabview = self._find_tabview_for_page(page)
        if tabview is not None:
            return self._pane_region_label(tabview)
        for win in self._get_detached_windows():
            if _tabview_has_page(win.tabview, page):
                return f"detached:{id(win)}"
        return None

    def _move_all_tabs(self, src, dest):
        """Moves every page from src to dest via Adw.TabView's own
        transfer_page — works for same-window TabView-to-TabView moves
        just as well as the cross-window case it's built for."""
        if src is dest:
            return
        while src.get_n_pages() > 0:
            page = src.get_nth_page(0)
            src.transfer_page(page, dest, dest.get_n_pages())
        if self.active_pane is src:
            self._set_active_pane(dest)

    def _unparent_pane_box(self, box):
        """Unparents a pane's box (tab bar + tabview column) from whatever
        Paned (or, in single-pane/no-split mode, self.terminal_overlay)
        currently holds it, so it can be reparented into a freshly-built
        layout tree. Renamed from the old _detach_pane to avoid confusion
        with the unrelated tab-DETACH-to-new-window feature (see
        TerminalPaneWindow.detach_tab_page)."""
        parent = box.get_parent()
        if parent is None:
            return
        if isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is box:
                parent.set_start_child(None)
            elif parent.get_end_child() is box:
                parent.set_end_child(None)
        elif isinstance(parent, Gtk.Overlay):
            if parent.get_child() is box:
                parent.set_child(None)

    def _build_pane_layout_widget(self, mode):
        """Builds the widget tree for the tab area for a given split mode.
        Always rebuilds from scratch (cheap: at most 4 pane boxes + 3
        Paned) — simpler and less bug-prone than patching an existing
        Paned tree."""
        b0, b1, b2, b3 = self._pane_boxes
        for box in self._pane_boxes:
            self._unparent_pane_box(box)

        if mode is None:
            return b0

        def make_paned(orientation, start, end):
            paned = Gtk.Paned(orientation=orientation, wide_handle=True, vexpand=True, hexpand=True)
            paned.set_start_child(start)
            paned.set_end_child(end)
            paned.set_resize_start_child(True)
            paned.set_resize_end_child(True)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            # A brand new Paned would otherwise size its two children off
            # their content (an empty, freshly-split pane has near-zero
            # natural size), squeezing it into a sliver at one edge instead
            # of an even 50/50 — force the divider to the midpoint once the
            # Paned actually has a real size. A single realize+idle_add shot
            # (as used for the SFTP local/remote split) isn't reliable here:
            # this Paned is spliced into an ALREADY-running, already-mapped
            # window on a button click, and one idle callback can easily run
            # before the next real size-allocate pass — so instead poll every
            # frame for up to ~0.5s until a nonzero size shows up.
            attempts = [0]
            def try_center():
                size = paned.get_width() if orientation == Gtk.Orientation.HORIZONTAL else paned.get_height()
                if size > 0:
                    paned.set_position(size // 2)
                    return False
                attempts[0] += 1
                return attempts[0] < 30
            GLib.timeout_add(16, try_center)
            return paned

        if mode == "vertical":  # side-by-side (left/right)
            return make_paned(Gtk.Orientation.HORIZONTAL, b0, b1)
        elif mode == "horizontal":  # stacked (top/bottom)
            return make_paned(Gtk.Orientation.VERTICAL, b0, b1)
        elif mode == "grid":  # 2x2
            left_col = make_paned(Gtk.Orientation.VERTICAL, b0, b2)
            right_col = make_paned(Gtk.Orientation.VERTICAL, b1, b3)
            return make_paned(Gtk.Orientation.HORIZONTAL, left_col, right_col)

        return b0

    def _apply_pane_layout(self):
        new_root = self._build_pane_layout_widget(self.split_mode)
        self.terminal_overlay.set_child(new_root)

    def _update_split_buttons_ui(self):
        """Highlights whichever split button matches the current mode."""
        active_button = {
            "vertical": self.split_vertical_btn,
            "horizontal": self.split_horizontal_btn,
            "grid": self.split_grid_btn,
        }.get(self.split_mode)
        for button in (self.split_vertical_btn, self.split_horizontal_btn, self.split_grid_btn):
            if button is active_button:
                button.add_css_class("suggested-action")
            else:
                button.remove_css_class("suggested-action")

    def on_split_button_clicked(self, target_mode):
        """Handles the vertical/horizontal/grid split buttons.

        Pressing the button for the CURRENTLY active mode cancels the split
        (all tabs move back into pane 0). Otherwise transitions to the
        target mode, merging tabs where panes are being removed:
        - grid -> vertical: bottom row moves up into the top row per column.
        - grid -> horizontal: right column moves left into the left column
          per row (then relabeled so the 2-way invariant pane0/pane1 holds).
        - vertical <-> horizontal: no tabs move, panes just re-orient.
        - single/2-way -> grid: nothing to move, new panes start empty.
        """
        if self.split_mode == target_mode:
            p0, p1, p2, p3 = self.pane_tabviews
            for nb in (p1, p2, p3):
                self._move_all_tabs(nb, p0)
            self.split_mode = None
        else:
            p0, p1, p2, p3 = self.pane_tabviews
            if self.split_mode == "grid":
                if target_mode == "vertical":
                    self._move_all_tabs(p2, p0)
                    self._move_all_tabs(p3, p1)
                elif target_mode == "horizontal":
                    self._move_all_tabs(p1, p0)
                    self._move_all_tabs(p3, p2)
                    self._move_all_tabs(p2, p1)
            self.split_mode = target_mode

        self._apply_pane_layout()
        self._update_split_buttons_ui()
        self.update_menu_sensitivity()
        self.apply_watermark_settings_to_all()  # split_mode changed — re-check "shrink in splits"

    def on_toggle_sidebar(self, button):
        """Collapses or expands the left sidebar."""
        is_active = button.get_active()
        self.left_panel.set_visible(is_active)
        if is_active:
            button.set_icon_name("go-previous-symbolic")
        else:
            button.set_icon_name("go-next-symbolic")

    def refresh_sync_button_visibility(self):
        """Shows/hides the header-bar sync button based on Settings ->
        Sync's enable switch. Called once at startup and again from
        SettingsDialog.on_apply, matching refresh_ai_provider_buttons'
        convention below."""
        self.sync_button.set_visible(bool(self.settings_manager.get("sync.enabled")))

    def restart_sync_timer(self):
        """(Re)starts the periodic sync timer at the configured interval
        (clamped to a 60s floor regardless of what's in settings — belt
        and suspenders alongside the Settings UI's own SpinRow floor).
        Safe to call anytime settings change; always cancels any existing
        timer first so repeated calls (e.g. from Settings' on_apply) never
        stack up duplicate timers. Same shape as sftp_widget.py's
        connection_check_timer_id — GLib.timeout_add_seconds, callback
        returns True to keep repeating, GLib.source_remove to cancel."""
        if self.sync_timer_id is not None:
            GLib.source_remove(self.sync_timer_id)
            self.sync_timer_id = None
        if not self.settings_manager.get("sync.enabled"):
            return
        interval = max(60, int(self.settings_manager.get("sync.interval_seconds") or 60))
        self.sync_timer_id = GLib.timeout_add_seconds(interval, self._on_sync_timer_tick)

    def _on_sync_timer_tick(self):
        # interactive=False: an archive-identity mismatch (see
        # force_sync_now/_on_sync_finished) should never pop a confirmation
        # dialog out of nowhere from an unattended background timer —
        # it's surfaced as a status message instead, and stays that way
        # until the user notices and clicks "Sync Now" by hand.
        self.force_sync_now(interactive=False)
        return True  # keep repeating

    def on_sync_button_clicked(self, button):
        self.force_sync_now()

    def on_sync_reset_clicked(self, action, param):
        """"Reset Sync State" from the sync button's dropdown — for
        pointing sync.folder at a genuinely new/empty share (a remounted
        drive, a fresh machine's folder) and wanting to seed it from this
        machine, which perform_sync's own safety check would otherwise
        correctly refuse to do on its own (see settings_sync.reset_sync_state)."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Reset Sync State?"),
            body=_(
                "The next sync will treat THIS machine's local hosts/settings/quickies as "
                "authoritative and push them into the current sync folder, instead of merging "
                "based on history.\n\nOnly do this after pointing Sync at a new or empty folder "
                "you want to seed — not because a normal sync failed."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset and Sync Now"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            if response == "reset":
                settings_sync.reset_sync_state()
                self.force_sync_now()

        dialog.connect("response", on_response)
        dialog.present()

    def force_sync_now(self, confirmed_sync_id=None, confirmed_new_archive=False, interactive=True):
        """Runs one sync pass on a background thread (file I/O against a
        possibly cloud-synced folder shouldn't block the UI) and marshals
        the resulting UI refresh back via GLib.idle_add — same shape as
        ai_providers.send_chat_request. A pass already in flight is never
        overlapped with another one.

        confirmed_sync_id/confirmed_new_archive: passed straight through
        to perform_sync — set one of these when re-running after the user
        has explicitly agreed to (respectively) connect to a different
        archive, or create/seed a new one (see _on_sync_finished/
        _prompt_sync_archive_switch below).
        interactive: whether _on_sync_finished is allowed to pop the
        archive-switch confirmation dialog if this pass needs one — False
        for the unattended periodic timer (see _on_sync_timer_tick), True
        for anything the user just clicked."""
        if self.sync_in_progress:
            return
        self.sync_in_progress = True
        self.sync_button.set_sensitive(False)
        self.sync_button.set_tooltip_text(_("Syncing…"))

        config_data = self.config_data

        def worker():
            result = settings_sync.perform_sync(
                self.settings_manager, config_data,
                confirmed_sync_id=confirmed_sync_id, confirmed_new_archive=confirmed_new_archive,
            )
            GLib.idle_add(self._on_sync_finished, result, interactive)

        threading.Thread(target=worker, daemon=True).start()

    def _on_sync_finished(self, result, interactive=True):
        self.sync_in_progress = False
        self.sync_button.set_sensitive(True)
        if result.ok:
            when = datetime.datetime.fromtimestamp(self.settings_manager.get("sync.last_sync_at")).strftime("%H:%M:%S")
            self.sync_button.set_tooltip_text(_("Sync now (last: {time})").format(time=when))
        elif result.needs_confirmation:
            self.sync_button.set_tooltip_text(_("Sync paused — archive identity needs confirmation"))
            if interactive:
                self._prompt_sync_archive_switch(result)
        else:
            self.sync_button.set_tooltip_text(_("Sync failed: {error}").format(error=result.error))
            logging.error(f"Sync failed: {result.error}")

        if result.new_config_data is not None:
            self.config_data = result.new_config_data
            self.populate_tree()
        if "quickies" in result.changed_categories:
            self.refresh_quickies_panel()
        if "terminal" in result.changed_categories:
            self.apply_terminal_color_scheme_to_all()
        if "terminal" in result.changed_categories or "general" in result.changed_categories:
            self.apply_watermark_settings_to_all()
        return False

    def _prompt_sync_archive_switch(self, result):
        """Shown when perform_sync refuses to proceed over an archive-
        identity question (see settings_sync.py's "Archive identity"
        docs) — one of two distinct cases, told apart by
        result.remote_sync_id:

        - Not None: the sync folder holds a DIFFERENT archive than the one
          this machine last used — sync.folder was repointed at something
          else, on purpose or by mistake.
        - None: NEITHER this machine nor the folder has an archive
          identity yet. Could genuinely be a fresh folder, or could be a
          cloud-synced folder (Dropbox/iCloud/...) whose client hasn't
          finished delivering an already-existing archive down to this
          machine yet — indistinguishable from here, which is exactly why
          this asks instead of just assuming "fresh" (a real, reproduced
          bug: a second machine's first-ever sync raced its cloud client
          and lost, minting a competing archive before the first
          machine's had actually arrived).

        Either way, declining leaves everything untouched, exactly as
        perform_sync already left it, and the button's tooltip (already
        set by the caller) as the only record something needs attention."""
        if result.remote_sync_id is None:
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=_("Create a New Sync Archive?"),
                body=_(
                    "No existing sync archive was found at this folder — neither this machine nor "
                    "the folder itself has one yet.\n\n"
                    "If this is genuinely a new or empty folder, continuing is safe: it seeds the "
                    "archive from this machine's current hosts/settings. But if this folder is "
                    "supposed to already have an archive (e.g. a cloud-synced folder that just "
                    "hasn't finished downloading yet), continuing now would create a SEPARATE, "
                    "competing archive instead of joining the real one — wait and try again instead."
                ),
            )
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("create", _("Create and Sync Now"))
            dialog.set_response_appearance("create", Adw.ResponseAppearance.DESTRUCTIVE)

            def on_response(dialog, response):
                if response == "create":
                    self.force_sync_now(confirmed_new_archive=True)

            dialog.connect("response", on_response)
            dialog.present()
            return

        info = result.remote_info or {}
        when = (datetime.datetime.fromtimestamp(info["version"]).strftime("%Y-%m-%d %H:%M:%S")
                if info.get("version") else _("unknown"))
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Connect to a Different Sync Archive?"),
            body=_(
                "The sync folder now contains a different archive than the one this machine last "
                "used (id {sync_id}, last synced {when}, {hosts} hosts, {quickies} Quickies).\n\n"
                "This can happen on purpose (a new shared folder, a teammate's archive) or by "
                "mistake (the wrong folder was chosen). Continuing merges this machine's local "
                "hosts/settings into it — nothing is deleted on either side, but from then on this "
                "machine treats that archive as the one it syncs with."
            ).format(
                sync_id=result.remote_sync_id, when=when,
                hosts=info.get("host_count", 0), quickies=info.get("quicky_count", 0),
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("connect", _("Connect and Sync Now"))
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            if response == "connect":
                self.force_sync_now(confirmed_sync_id=result.remote_sync_id)

        dialog.connect("response", on_response)
        dialog.present()

    def refresh_ai_provider_buttons(self):
        """Rebuilds the AI panel's per-provider toggle buttons (in its own
        header, see AiPanel.provider_button_box) from current keyring/
        settings state — one button per API provider with a usable key,
        plus one per CLI tool whose binary is actually found on PATH — and
        shows/hides the header-bar's single AI toggle button based on
        whether anything is actually configured. Called once at startup
        and again from SettingsDialog.on_apply whenever keys/commands
        change, so the UI reacts immediately with no restart needed."""
        for btn in self._ai_provider_buttons.values():
            self.ai_panel.provider_button_box.remove(btn)
        self._ai_provider_buttons = {}

        if self.settings_manager.get("ai.disabled"):
            # Settings -> AI's master switch: no header button, no keyring
            # lookups, no probing PATH for claude/codex — as close to "this
            # feature doesn't exist" as the app can guarantee, for anyone
            # who doesn't want the app touching AI in any way.
            if self.ai_panel.get_visible():
                self.ai_panel.set_visible(False)
            self.ai_toggle_button.set_visible(False)
            return

        configured = [
            (pid, label) for pid, label in AI_STANDARD_PROVIDERS
            if self.keyring.load_password(f"ai:{pid}")
        ]
        for cp in self.settings_manager.get("ai.custom_providers") or []:
            has_key = self.keyring.load_password(f"ai:custom:{cp['id']}")
            if has_key or not cp.get("has_key", True):
                configured.append((f"custom:{cp['id']}", cp.get("name") or _("Custom")))

        cli_overrides = self.settings_manager.get("cli.commands") or {}
        for cli_id, cli_label, default_command in CLI_STANDARD_PROVIDERS:
            command = cli_overrides.get(cli_id) or default_command
            if cli_is_available(command):
                configured.append((f"cli:{cli_id}", cli_label))
        for tool in self.settings_manager.get("cli.custom_tools") or []:
            if cli_is_available(tool.get("command", "")):
                configured.append((f"cli:custom:{tool['id']}", tool.get("name") or _("Custom CLI")))

        self.ai_toggle_button.set_visible(bool(configured))
        if not configured and self.ai_panel.get_visible():
            self.ai_panel.set_visible(False)

        for provider_id, label in configured:
            family = _badge_family(provider_id)
            icon_name = _icon_name_for(provider_id)
            if icon_name:
                image = Gtk.Image.new_from_icon_name(icon_name)
                image.set_pixel_size(16)  # matches the sibling icon buttons' default symbolic size
                button = Gtk.ToggleButton(child=image)
            else:
                button = Gtk.ToggleButton(label=_badge_text(label))
            # "ai-provider-button" sizes/shapes it like a plain icon button;
            # the per-family class tints it (text color for a label badge,
            # or the recolored symbolic icon itself), and only the
            # :checked state gets a background fill — so pressed vs.
            # unpressed stays visually obvious instead of both looking tinted.
            button.add_css_class("ai-provider-button")
            button.add_css_class(f"ai-provider-badge-{family}")
            button.set_tooltip_text(label)
            button.set_active(self.ai_panel.active_provider_id == provider_id)
            button.connect("toggled", self._on_ai_provider_button_toggled, provider_id)
            self.ai_panel.provider_button_box.append(button)
            self._ai_provider_buttons[provider_id] = button

        # Gtk.FlowBox's own natural-width request scales with
        # max-children-per-line *regardless of how many children it
        # actually has* (confirmed: leaving it at some fixed "big enough"
        # number like 999 made it request a natural width in the
        # thousands of pixels even with 2 buttons) — always wider than
        # available, so it always claimed the full row and halign=CENTER
        # (see AiPanel.__init__) had nothing to center. Matching it to the
        # real count keeps the natural-width request sane, while still
        # wrapping to a second row if there's genuinely not enough width
        # for all of them on one line.
        if configured:
            self.ai_panel.provider_button_box.set_max_children_per_line(len(configured))

        # No provider was ever active (first run) — default to the first
        # configured one so the panel always has *something* selected the
        # first time it's opened, rather than an empty "no provider" state.
        if configured and self.ai_panel.active_provider_id not in self._ai_provider_buttons:
            first_id = configured[0][0]
            self._ai_provider_buttons[first_id].set_active(True)

    def _on_ai_toggle_button_toggled(self, button):
        """The header-bar's single AI button — shows or collapses the
        panel. Conversation state and the active provider are untouched by
        hiding it; the "X provider buttons in the panel header (see
        _on_ai_provider_button_toggled) are what pick who gets the next
        message, not this."""
        self.ai_panel.set_visible(button.get_active())
        if not button.get_active():
            return
        self._apply_ai_panel_width()

    def _on_ai_provider_button_toggled(self, button, provider_id):
        # These act as a radio group, always exactly one pressed while any
        # provider is configured — the panel's own visibility is the
        # header AI button's job now (_on_ai_toggle_button_toggled), not
        # this button's. Clicking the already-active provider's button
        # would otherwise leave it un-pressed with nothing to switch to;
        # just force it back on instead of switching away from anything.
        if not button.get_active():
            button.handler_block_by_func(self._on_ai_provider_button_toggled)
            button.set_active(True)
            button.handler_unblock_by_func(self._on_ai_provider_button_toggled)
            return

        # Pressing a different provider's button: un-press every other
        # button without recursing back into this handler, then switch.
        for pid, other_button in self._ai_provider_buttons.items():
            if pid != provider_id and other_button.get_active():
                other_button.handler_block_by_func(self._on_ai_provider_button_toggled)
                other_button.set_active(False)
                other_button.handler_unblock_by_func(self._on_ai_provider_button_toggled)

        self.ai_panel.set_active_provider(provider_id)

    def _apply_ai_panel_width(self):
        """Enforces the cached/default width once the panel becomes
        visible — a just-shown pane with no natural size would otherwise
        get squeezed to a sliver (mirrors sftp_widget.py's realize-then-
        set_position pattern for the same problem). Capped attempts (not
        an unconditional retry-forever idle callback) — if the width
        somehow never becomes positive, give up quietly instead of
        spinning a CPU core indefinitely; see ai_panel.py's chat_paned
        setup for a real instance of that exact bug."""
        attempts = [0]
        def _apply_width():
            total = self.center_paned.get_width()
            if total <= 0:
                attempts[0] += 1
                return attempts[0] < 30  # ~0.5s ceiling, then give up
            # No cached width yet (first-ever open): default to ~25% of the
            # available space rather than a fixed pixel guess, so it scales
            # with the user's actual window size.
            width = self._last_ai_panel_width or int(total * 0.25)
            self.center_paned.set_position(max(0, total - width))
            return False
        GLib.idle_add(_apply_width)

    def on_toggle_search(self, *args):
        """Moves focus into the always-visible search entry, selecting any
        existing text so typing immediately replaces it."""
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def setup_search_signals(self):
        """Connects signals for the search widgets."""
        self.search_entry.connect("activate", self.on_search_activate)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_up_button.connect("clicked", self.on_search_nav_up)
        self.search_down_button.connect("clicked", self.on_search_nav_down)

        # Up/Down cycle search results without leaving the entry — clicking
        # the nav buttons instead would steal keyboard focus onto the
        # button, forcing a click back into the entry to keep typing.
        search_entry_key_controller = Gtk.EventControllerKey.new()
        search_entry_key_controller.connect("key-pressed", self.on_search_entry_key_pressed)
        self.search_entry.add_controller(search_entry_key_controller)

    def on_search_entry_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Up:
            self.on_search_nav_up(None)
            return True
        elif keyval == Gdk.KEY_Down:
            self.on_search_nav_down(None)
            return True
        return False

    def apply_search_bar_position(self):
        """Moves the host-tree search bar above or below the tree per the
        interface.host_search_position setting ("top" or "bottom")."""
        position = self.settings_manager.get("interface.host_search_position")
        if position == "top":
            self.hosts_box.reorder_child_after(self.search_bar, None)
        else:
            self.hosts_box.reorder_child_after(self.search_bar, self.tree_scrolled_window)

    def apply_quickies_search_position(self):
        """Moves the Quickies search row (search entry + Add button, now
        one and the same row — see _build_quickies_box) above or below the
        snippet list per quickies.search_position ("top"/"bottom") — same
        idea as apply_search_bar_position above, just relative to
        quickies_scroller instead of tree_scrolled_window.

        "top" already reads fine as-is: the row's own margin_top (see
        _build_quickies_box) gives it breathing room from whatever's above
        it, and quickies_box's own spacing puts a clean gap below it before
        the list. "bottom" had no equivalent on its trailing edge though —
        as the LAST child there, it sat flush against quickies_box's own
        bottom edge with nothing to give it room, unlike every other
        search box in the app. margin_bottom here (only in this branch,
        cleared in the other) is that missing edge; the leading edge
        already gets its gap for free from quickies_box's own spacing
        against the scroller above it."""
        position = self.settings_manager.get("quickies.search_position")
        if position == "top":
            self.quickies_box.reorder_child_after(self.quickies_search_row, None)
            self.quickies_search_row.set_margin_bottom(0)
        else:
            self.quickies_box.reorder_child_after(self.quickies_search_row, self.quickies_scroller)
            self.quickies_search_row.set_margin_bottom(6)

    def on_quickies_search_changed(self, entry):
        """Exact substring match (name or text, case-insensitive) first;
        only falls back to the one-typo-tolerant fuzzy match (on the name)
        if NOTHING in the list matched exactly — same two-tier approach as
        the host tree search, just evaluated per-row via GtkListBox's own
        filter mechanism instead of walking a tree."""
        query = entry.get_text().strip()
        self._quickies_search_query = query
        if query:
            query_lower = query.lower()
            self._quickies_search_use_fuzzy = not any(
                query_lower in f"{q.get('name', '')} {q.get('text', '')}".lower()
                for q in self.quickies_items
            )
        else:
            self._quickies_search_use_fuzzy = False
        self.quickies_listbox.invalidate_filter()

    def _quickies_filter_func(self, row):
        query = getattr(self, "_quickies_search_query", "")
        if not query:
            return True
        index = row.get_index()
        if index < 0 or index >= len(self.quickies_items):
            return True
        quicky = self.quickies_items[index]
        query_lower = query.lower()
        if query_lower in f"{quicky.get('name', '')} {quicky.get('text', '')}".lower():
            return True
        if getattr(self, "_quickies_search_use_fuzzy", False):
            threshold = self._fuzzy_search_threshold(len(query))
            if threshold > 0 and self._fuzzy_edit_distance(query_lower, quicky.get("name", "").lower()) <= threshold:
                return True
        return False

    def _build_quickies_box(self):
        """The 'Quickies' section — a search row (doubling as the section's
        only label, via its own placeholder text) above a scrollable list
        of pre-written snippets; clicking one inserts it into the active
        terminal (see on_quicky_row_activated)."""
        self.quickies_items = list(self.settings_manager.get("quickies.items") or [])

        self.quickies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.quickies_box.set_vexpand(True)

        # No standalone "Quickies" title row anymore — the search entry's
        # own placeholder text ("Quickies") IS the section label, and Add
        # moved from its own header button into this same row, packed at
        # the end. One row instead of two, same information.
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        # Mirror image of button_box's fix above: as quickies_box's FIRST
        # child, this naturally gets 0px gap from the Paned divider above it
        # (box spacing only applies *between* siblings) but a real 6px gap
        # below it (from quickies_box's own spacing, before the scroller) —
        # so it visually hugs the divider. Matching that same 6px on top
        # centers it the same way.
        search_row.set_margin_top(6)

        # Same look as the host search box: no magnifying-glass icon (its
        # own dimmed placeholder is cue enough), position configurable
        # (see apply_quickies_search_position), one-typo-tolerant fuzzy
        # fallback (see _quickies_filter_func) — same fuzzy helpers the
        # host tree search already uses. Placeholder reads "Quickies
        # search" instead of plain "Search" now that it's also standing
        # in for the removed title label.
        self.quickies_search_entry = Gtk.SearchEntry()
        self.quickies_search_entry.set_placeholder_text(_("Quickies search"))
        self.quickies_search_entry.set_hexpand(True)
        _quickies_search_leading_icon = self.quickies_search_entry.get_first_child()
        if _quickies_search_leading_icon is not None:
            _quickies_search_leading_icon.set_visible(False)
        self.quickies_search_entry.connect("search-changed", self.on_quickies_search_changed)
        search_row.append(self.quickies_search_entry)

        add_quicky_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_quicky_btn.set_tooltip_text(_("Add Quicky"))
        add_quicky_btn.set_valign(Gtk.Align.CENTER)
        add_quicky_btn.connect("clicked", self.on_add_quicky_clicked)
        search_row.append(add_quicky_btn)

        self.quickies_search_row = search_row
        self.quickies_box.append(search_row)

        self.quickies_listbox = Gtk.ListBox()
        self.quickies_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.quickies_listbox.connect("row-activated", self.on_quicky_row_activated)
        self.quickies_listbox.set_filter_func(self._quickies_filter_func)

        self.quickies_scroller = Gtk.ScrolledWindow()
        self.quickies_scroller.set_vexpand(True)
        self.quickies_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.quickies_scroller.set_child(self.quickies_listbox)
        self.quickies_box.append(self.quickies_scroller)

        self._refresh_quickies_listbox()
        self.apply_quickies_search_position()

    def _build_tiny_quicky_button(self, icon_name, tooltip):
        """A genuinely small icon button — "flat" alone (or even
        "flat"+"circular") still keeps GtkButton/Adwaita's normal min-size
        padding, taller than a plain text line. Building the child image
        directly (with an explicitly small pixel_size) and constraining the
        button itself via CSS is what actually shrinks it down to sit
        beside the Quicky name line instead of stretching that line to
        match the button."""
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("quicky-tiny-button")
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(12)
        button.set_child(image)
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        return button

    def _refresh_quickies_listbox(self):
        """Rebuilds quickies_listbox's rows from self.quickies_items. A
        plain Gtk.ListBoxRow with a hand-built child, not Adw.ActionRow —
        ActionRow always sizes its suffix buttons to the full row height
        (title+subtitle combined), and the ask here is specifically for the
        Edit/Delete buttons to sit compact, matching just the name line,
        with the command preview as its own full-width line underneath
        both the name AND the buttons — that preview line is optional
        (Settings -> Quickies -> "Show command preview", on by default)."""
        child = self.quickies_listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.quickies_listbox.remove(child)
            child = next_child

        show_command_preview = self.settings_manager.get("quickies.show_command_preview")
        for index, quicky in enumerate(self.quickies_items):
            preview = quicky.get("text", "").replace("\n", " ").strip()

            name_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

            # 1-based position — matches the quick-access shortcut (if any,
            # first 10 only, see Settings -> Shortcuts -> Quickies) that
            # runs/pastes this exact slot. Reordering is via the row's
            # right-click menu (Move Up/Move Down), not a button here —
            # same "not every action needs a permanently-visible button"
            # reasoning as Delete below.
            index_label = Gtk.Label(label=str(index + 1), xalign=0)
            index_label.add_css_class("dim-label")
            index_label.add_css_class("caption")
            index_label.set_valign(Gtk.Align.CENTER)
            name_line.append(index_label)

            name_label = Gtk.Label(label=quicky.get("name", ""), xalign=0)
            name_label.set_hexpand(True)
            name_label.set_wrap(True)
            name_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            name_label.add_css_class("heading")
            name_line.append(name_label)

            # No Delete button here on purpose — a single misclick would
            # silently drop a saved Quicky with no confirmation. Deleting is
            # still available, just moved to the right-click menu (see
            # on_quicky_right_click) instead of a one-click button.
            edit_btn = self._build_tiny_quicky_button("document-edit-symbolic", _("Edit"))
            edit_btn.connect("clicked", self.on_edit_quicky_clicked, index)
            name_line.append(edit_btn)

            # "Double" play icon (two triangles, like a fast-forward glyph)
            # for the send-AND-run action — visually distinct at a glance
            # from the single-triangle "just send" button next to it.
            run_btn = self._build_tiny_quicky_button("media-seek-forward-symbolic", _("Send and Run"))
            run_btn.connect("clicked", self.on_run_quicky_clicked, index)
            name_line.append(run_btn)

            insert_btn = self._build_tiny_quicky_button("media-playback-start-symbolic", _("Send"))
            insert_btn.connect("clicked", self.on_insert_quicky_clicked, index)
            name_line.append(insert_btn)

            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            row_box.set_margin_top(2)
            row_box.set_margin_bottom(2)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)
            row_box.append(name_line)

            if preview and show_command_preview:
                preview_label = Gtk.Label(label=preview, xalign=0)
                preview_label.set_wrap(True)
                preview_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                preview_label.add_css_class("caption")
                preview_label.add_css_class("dim-label")
                row_box.append(preview_label)

            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row.set_activatable(True)

            right_click_gesture = Gtk.GestureClick.new()
            right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
            right_click_gesture.connect("pressed", self._on_right_press_guard)
            right_click_gesture.connect("released", self.on_quicky_right_click)
            row.add_controller(right_click_gesture)

            self.quickies_listbox.append(row)

    def _build_left_panel_root(self):
        """hosts_box alone if Quickies is off; otherwise a Gtk.Paned(VERTICAL)
        with hosts_box/quickies_box ordered per quickies.position. The
        divider is freely draggable; its ratio is tracked continuously and
        restored (see self._last_quickies_split_ratio) both here — since
        this whole Paned is rebuilt from scratch on every Quickies off/on
        toggle, see _apply_left_panel_layout — and across app restarts."""
        if not self.settings_manager.get("quickies.enabled"):
            self.hosts_box.set_margin_bottom(0)
            self.hosts_box.remove_css_class("thongssh-panel-divider-top")
            self.hosts_box.remove_css_class("thongssh-panel-divider-bottom")
            return self.hosts_box

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_vexpand(True)
        # wide_handle gives the handle itself a bit more thickness, which
        # doubles as the "gap" between the two divider lines below.
        paned.set_wide_handle(True)

        # thongssh-panel-divider-top/-bottom (defined in setup_css) draw a
        # light line on whichever box's edge actually faces the divider —
        # applied to the whole box, not a specific child inside it, so
        # it's correct regardless of each box's own internal top/bottom
        # search-position setting (whatever ends up at that edge — the
        # host tree's Add/Remove buttons, or Quickies' search+Add row —
        # gets the line right after it). Both classes are cleared from
        # both boxes first since they're reused across position toggles.
        for box in (self.hosts_box, self.quickies_box):
            box.remove_css_class("thongssh-panel-divider-top")
            box.remove_css_class("thongssh-panel-divider-bottom")
            box.set_margin_bottom(0)

        if self.settings_manager.get("quickies.position") == "above":
            self.quickies_box.add_css_class("thongssh-panel-divider-top")
            self.hosts_box.add_css_class("thongssh-panel-divider-bottom")
            paned.set_start_child(self.quickies_box)
            paned.set_end_child(self.hosts_box)
        else:
            self.hosts_box.add_css_class("thongssh-panel-divider-top")
            self.quickies_box.add_css_class("thongssh-panel-divider-bottom")
            paned.set_start_child(self.hosts_box)
            paned.set_end_child(self.quickies_box)
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(True)
        paned.set_shrink_end_child(True)

        def _track_quickies_split_ratio(*_args):
            total = paned.get_height()
            if total > 0:
                self._last_quickies_split_ratio = paned.get_position() / total
        paned.connect("notify::position", _track_quickies_split_ratio)

        ratio = self._last_quickies_split_ratio
        if ratio is not None:
            # Same capped-retry-on-"map" pattern as ai_panel.py's
            # chat_paned uses for the identical "wait for a real height"
            # problem — an unbounded poll would spin a CPU core forever if
            # this Paned somehow never gets mapped.
            map_state = {"started": False}
            def _on_map(_widget):
                if map_state["started"]:
                    return
                map_state["started"] = True
                attempts = [0]
                def try_apply():
                    total = paned.get_height()
                    if total > 0:
                        paned.set_position(int(total * ratio))
                        return False
                    attempts[0] += 1
                    return attempts[0] < 30  # ~0.5s ceiling, then give up quietly
                GLib.timeout_add(16, try_apply)
            paned.connect("map", _on_map)

        return paned

    def _apply_left_panel_layout(self):
        """Rebuilds left_panel's single child from current Quickies
        settings — called on init, from the header toggle, and from
        Settings' on_apply. Same swap-the-child-tree idiom as
        _apply_pane_layout uses for the terminal split panes."""
        if self._left_panel_root is not None:
            if isinstance(self._left_panel_root, Gtk.Paned):
                # Detach both children first — otherwise they'd still be
                # parented to the about-to-be-discarded Paned and couldn't
                # be reused in the next root.
                self._left_panel_root.set_start_child(None)
                self._left_panel_root.set_end_child(None)
            self.left_panel.remove(self._left_panel_root)

        self._left_panel_root = self._build_left_panel_root()
        self.left_panel.append(self._left_panel_root)

    def on_add_quicky_clicked(self, button):
        self._open_quicky_dialog(None)

    def on_insert_quicky_clicked(self, button, index):
        self._insert_quicky_into_terminal(index, run=False)

    def on_run_quicky_clicked(self, button, index):
        self._insert_quicky_into_terminal(index, run=True)

    def on_edit_quicky_clicked(self, button, index):
        self._open_quicky_dialog(index)

    def on_delete_quicky_clicked(self, button, index):
        del self.quickies_items[index]
        self._save_quickies()

    def _move_quicky(self, index, direction):
        """direction: -1 for up, +1 for down. No-op at either end of the
        list rather than wrapping around — reordering past the edge isn't
        an obviously-meaningful action, so it's simplest to just ignore it."""
        new_index = index + direction
        if not (0 <= index < len(self.quickies_items)) or not (0 <= new_index < len(self.quickies_items)):
            return
        self.quickies_items[index], self.quickies_items[new_index] = (
            self.quickies_items[new_index],
            self.quickies_items[index],
        )
        self._save_quickies()

    def _open_quicky_dialog(self, index):
        existing = self.quickies_items[index] if index is not None else None
        dialog = QuickyDialog(self, existing)

        def on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                name, text = dialog.get_data()
                if name:
                    if index is None:
                        self.quickies_items.append({"name": name, "text": text})
                    else:
                        self.quickies_items[index] = {"name": name, "text": text}
                    self._save_quickies()

        dialog.connect("response", on_response)
        dialog.present()

    def _save_quickies(self):
        """Persists immediately — Quickies edited from the panel take
        effect right away, no separate Settings 'Apply' step (same as the
        header toggle buttons)."""
        self.settings_manager.set("quickies.items", self.quickies_items)
        self.settings_manager.save()
        self._refresh_quickies_listbox()

    def on_quicky_row_activated(self, listbox, row):
        self._insert_quicky_into_terminal(row.get_index(), run=False)

    def _insert_quicky_into_terminal(self, index, run):
        """Shared by double-click (row-activated, run=False — same as
        before) and the row's Insert/Run buttons and context-menu entries.
        run=True appends "\\n" so the command is fed to the shell and
        executes immediately, instead of just sitting in the prompt."""
        if index < 0 or index >= len(self.quickies_items):
            return
        terminal = self.get_active_terminal()
        if terminal is None:
            return
        scrolled_term = self.get_active_terminal_widget()
        host_config = self.tab_data.get(scrolled_term, {}).get("config", {})
        text = self._render_template_text(self.quickies_items[index].get("text", ""), host_config)
        if run:
            text += "\n"
        terminal.feed_child(text.encode("utf-8"))
        # Without this, focus stays on whatever Quickies-panel widget was
        # clicked (or wherever it was for a keyboard shortcut), so typing
        # right after Send/Run did nothing until the terminal was clicked
        # by hand — feed_child() injects text into the pty, it doesn't
        # touch focus at all.
        terminal.grab_focus()

    def on_quicky_right_click(self, gesture, n_press, x, y):
        """Shows the Quicky context menu — Insert/Run/Edit/Delete plus
        "Send to Batch Command". Connected to 'released', same as the tab
        and tree context menus, so the button is already up when the
        popover grabs it."""
        row = gesture.get_widget()
        index = row.get_index()
        if index < 0 or index >= len(self.quickies_items):
            return
        self.last_clicked_quicky_index = index

        translated_x, translated_y = row.translate_coordinates(self, x, y)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(translated_x), int(translated_y), 1, 1
        self.popover_quicky.set_pointing_to(rect)
        self.popover_quicky.popup()

    def on_menu_quicky_insert(self, action, param):
        if self.last_clicked_quicky_index is not None:
            self._insert_quicky_into_terminal(self.last_clicked_quicky_index, run=False)

    def on_menu_quicky_run(self, action, param):
        if self.last_clicked_quicky_index is not None:
            self._insert_quicky_into_terminal(self.last_clicked_quicky_index, run=True)

    def on_menu_quicky_edit(self, action, param):
        if self.last_clicked_quicky_index is not None:
            self._open_quicky_dialog(self.last_clicked_quicky_index)

    def on_menu_quicky_delete(self, action, param):
        if self.last_clicked_quicky_index is not None:
            self.on_delete_quicky_clicked(None, self.last_clicked_quicky_index)

    def on_menu_quicky_move_up(self, action, param):
        if self.last_clicked_quicky_index is not None:
            self._move_quicky(self.last_clicked_quicky_index, -1)

    def on_menu_quicky_move_down(self, action, param):
        if self.last_clicked_quicky_index is not None:
            self._move_quicky(self.last_clicked_quicky_index, 1)

    def on_menu_quicky_send_to_batch(self, action, param):
        """Opens Batch Command with this Quicky's (template-rendered)
        command already sitting in the command field, ready to send —
        rather than the user having to retype or copy-paste it there."""
        if self.last_clicked_quicky_index is None:
            return
        if not (0 <= self.last_clicked_quicky_index < len(self.quickies_items)):
            return
        scrolled_term = self.get_active_terminal_widget()
        host_config = self.tab_data.get(scrolled_term, {}).get("config", {})
        text = self._render_template_text(
            self.quickies_items[self.last_clicked_quicky_index].get("text", ""), host_config
        )
        dialog = BatchCommandDialog(self)
        dialog.command_buffer.set_text(text)
        dialog.present()

    def on_quickies_toggle_clicked(self, button):
        self.settings_manager.set("quickies.enabled", button.get_active())
        self.settings_manager.save()
        self._apply_left_panel_layout()

    def refresh_quickies_panel(self):
        """Called by Settings' on_apply after quickies.items/position/
        enabled change — rebuilds the live listbox and left_panel layout."""
        self.quickies_items = list(self.settings_manager.get("quickies.items") or [])
        self._refresh_quickies_listbox()
        self.apply_quickies_search_position()
        self._apply_left_panel_layout()

    def _fuzzy_edit_distance(self, query, text):
        """Minimum "optimal string alignment" distance between `query` and
        the best-matching substring of `text` — Levenshtein plus one extra
        move (swapping two adjacent characters counts as a single edit,
        not two substitutions), computed with free start/end alignment (the
        first row starts at all zeros instead of 0..len(query), and the
        answer is the smallest value seen in the last row rather than just
        its final cell). That free start/end is what lets a short typo'd
        query match part of a longer host name instead of only the name as
        a whole; the transposition move is what lets one threshold value
        cover both a single typed-out-of-order pair of letters (distance 1,
        same as a single missing/extra/wrong character) without also
        having to loosen the threshold to 2 — which would blur together
        with, and match half of, any block of similarly-numbered hosts
        (qb-fs021, qb-fs022, qb-fs023, ...)."""
        m = len(query)
        if m == 0:
            return 0
        prev2 = None
        prev = [0] * (m + 1)
        for j in range(1, m + 1):
            prev[j] = j
        best = prev[m]
        prev_ch = None
        for i, ch in enumerate(text):
            curr = [0] * (m + 1)
            for j in range(1, m + 1):
                cost = 0 if ch == query[j - 1] else 1
                value = min(
                    prev[j - 1] + cost,  # match/substitute
                    prev[j] + 1,         # skip a character of text
                    curr[j - 1] + 1,     # skip a character of query
                )
                if (prev2 is not None and j > 1 and prev_ch is not None
                        and ch == query[j - 2] and prev_ch == query[j - 1]):
                    value = min(value, prev2[j - 2] + 1)  # transpose
                curr[j] = value
            best = min(best, curr[m])
            prev2 = prev
            prev = curr
            prev_ch = ch
        return best

    def _fuzzy_search_threshold(self, query_len):
        """How many typo'd/missing/extra/swapped characters to tolerate,
        scaled to query length — 0 for very short queries (anything looser
        would match nearly everything) up to 1 once there's enough of the
        name typed for that to still be meaningful."""
        return 0 if query_len <= 2 else 1

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

        if not self.search_results:
            # No exact/regex hits — fall back to fuzzy matching, so a
            # missing, extra, or transposed character (typing "pmxx013"
            # for "pxmx013", or "qb-fs21" for "qb-fs021") still finds the
            # host instead of coming up empty. Ranked by closeness so the
            # best guess is what Enter/first-navigation lands on.
            threshold = self._fuzzy_search_threshold(len(query))
            if threshold > 0:
                query_lower = query.lower()
                scored = []

                def find_fuzzy(model, path, iter):
                    name = model.get_value(iter, COL_NAME)
                    distance = self._fuzzy_edit_distance(query_lower, name.lower())
                    if distance <= threshold:
                        scored.append((distance, path.copy()))

                self.main_tree_store.foreach(find_fuzzy)
                scored.sort(key=lambda item: item[0])
                self.search_results = [path for _distance, path in scored]

        if self.search_results:
            self.current_search_index = 0
            self.navigate_to_result(self.current_search_index)

        self.update_search_ui()

    def on_search_activate(self, entry):
        """Handler for Enter key press in the search entry: opens the
        currently selected search result, if it's a host."""
        if self.search_results and 0 <= self.current_search_index < len(self.search_results):
            path = self.search_results[self.current_search_index]
            model = self.tree_view.get_model()
            tree_iter = model.get_iter(path)
            node_type = model.get_value(tree_iter, COL_TYPE)
            if node_type == "host":
                self.on_tree_row_activated(self.tree_view, path, None)

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
        """Updates the state of the navigation buttons."""
        has_results = len(self.search_results) > 0
        self.search_up_button.set_sensitive(has_results)
        self.search_down_button.set_sensitive(has_results)

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

        # Ctrl+F is handled globally now (see on_window_key_pressed), which
        # fires first regardless of where focus is.

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
        """Handles global key presses for the window — close tab/focus
        search/find-in-terminal, all configurable (Settings -> General ->
        Keyboard Shortcuts, see _shortcut_matches).

        Resolved through _resolve_latin_letter (see on_terminal_key_pressed)
        rather than compared against keyval directly, same reasoning as
        there: these are physical-key shortcuts, and should fire the same
        way regardless of which character the active keyboard layout maps
        that key to."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK
        is_shift = modifier & Gdk.ModifierType.SHIFT_MASK
        letter = self._resolve_latin_letter(keyval, keycode) if is_ctrl else None

        if self._shortcut_matches("shortcuts.close_tab", is_ctrl, is_shift, letter):
            self.on_menu_close_tab(None, None)
            return True # Event handled
        # Checked before focus_search below since both commonly share
        # is_ctrl and the same letter (Ctrl+F / Ctrl+Shift+F by default).
        if self._shortcut_matches("shortcuts.find_in_terminal", is_ctrl, is_shift, letter):
            self.on_menu_find_in_terminal(None, None)
            return True
        # Focuses the search entry from anywhere — terminal, tree, or
        # elsewhere in the window (see the CAPTURE phase note above).
        if self._shortcut_matches("shortcuts.focus_search", is_ctrl, is_shift, letter):
            self.on_toggle_search()
            return True

        # Quick-access bindings for the first 10 Quickies (Settings ->
        # Shortcuts -> Quickies), by position — Ctrl+1..9,0 to paste,
        # Ctrl+Shift+1..9,0 to paste-and-run (slot 10 = the "0" key), same
        # digit-key-regardless-of-layout resolution as the letter
        # shortcuts above.
        digit = self._resolve_physical_digit(keyval, keycode) if is_ctrl else None
        if digit:
            for i in range(1, 11):
                if self._quicky_shortcut_matches(f"shortcuts.quicky_paste_{i}", is_ctrl, is_shift, digit):
                    self._insert_quicky_into_terminal(i - 1, run=False)
                    return True
                if self._quicky_shortcut_matches(f"shortcuts.quicky_run_{i}", is_ctrl, is_shift, digit):
                    self._insert_quicky_into_terminal(i - 1, run=True)
                    return True

        return False

    def setup_global_menu(self, header_bar):
        """Creates and configures the application's global menu."""
        # 1. Create GActions (actions)
        action_close_tab = Gio.SimpleAction.new("close-tab", None)
        action_close_tab.connect("activate", self.on_menu_close_tab)
        self.add_action(action_close_tab)

        action_quit = Gio.SimpleAction.new("quit", None)
        # self.close() (not get_application().quit()): quit() tears every
        # window down immediately without emitting "close-request" at all,
        # which is what _on_close_request relies on to save window geometry
        # — closing via this menu action silently skipped that save
        # entirely. close() fires close-request like the titlebar's own
        # close button does, and the app quits right after since this is
        # its only window.
        action_quit.connect("activate", lambda a, p: self.close())
        self.add_action(action_quit)

        action_settings = Gio.SimpleAction.new("settings", None)
        action_settings.connect("activate", self.on_menu_settings)
        self.add_action(action_settings)

        action_about = Gio.SimpleAction.new("about", None)
        action_about.connect("activate", self.on_menu_about)
        self.add_action(action_about)

        action_batch_command = Gio.SimpleAction.new("batch-command", None)
        action_batch_command.connect("activate", self.on_menu_batch_command)
        self.add_action(action_batch_command)

        action_sync_reset = Gio.SimpleAction.new("sync-reset", None)
        action_sync_reset.connect("activate", self.on_sync_reset_clicked)
        self.add_action(action_sync_reset)

        # 2. Create GMenu (model)
        main_menu_model = Gio.Menu()

        # "File" section
        file_section = Gio.Menu()
        file_section.append(_("Close Tab"), "win.close-tab")
        file_section.append(_("Quit"), "win.quit")
        main_menu_model.append_section(None, file_section)

        # "Batch" section
        batch_section = Gio.Menu()
        batch_section.append(_("Batch Command"), "win.batch-command")
        main_menu_model.append_section(None, batch_section)

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
        can_close_tab = any(tv.get_n_pages() > 0 for tv in self.pane_tabviews)
        self.lookup_action("close-tab").set_enabled(can_close_tab)

        # "Edit" and "Delete"
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        item_selected = tree_iter is not None

        self.lookup_action("edit-rename").set_enabled(item_selected)
        self.lookup_action("delete").set_enabled(item_selected)

        self._sync_find_target_terminal()


    # --- (GTK4 Menu) ---
    def setup_actions_and_popovers(self):
        """Creates GActions and Gtk.PopoverMenu for right-click. 100% GTK4."""

        # Window-scoped terminal-pane actions (win.close-tab, copy/paste-
        # clipboard, send-file, find-in-terminal, save-log-tab) + the
        # terminal right-click popover + this window's own tab_menu_model/
        # tab_copy_host_menu (native Adw.TabView tab-strip context menu,
        # app-scoped — see app.py) + the in-terminal find bar. Shared with
        # DetachedTabWindow — see tab_window_base.py.
        self._setup_terminal_pane_actions()

        # 1. Create GActions (actions) — host-tree / Quickies-panel scoped,
        # all window-local (win.*).

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

        # Host-tree-only — the tab context menu's own "Connect SFTP" is the
        # app-scoped app.open-sftp (see app.py / TerminalPaneWindow.
        # open_sftp_for_tab_page), unrelated to this action despite the
        # near-identical name; a menu inside a DetachedTabWindow can't
        # reach a win.* action registered only on the main window.
        action_open_sftp = Gio.SimpleAction.new("open-sftp", None)
        action_open_sftp.connect("activate", self.on_menu_open_sftp_from_tree)
        self.add_action(action_open_sftp)

        action_rename = Gio.SimpleAction.new("rename", None)
        action_rename.connect("activate", self.on_menu_rename_group)
        self.add_action(action_rename)

        action_delete = Gio.SimpleAction.new("delete", None)
        action_delete.connect("activate", self.on_remove_selected_clicked)
        self.add_action(action_delete)

        action_user_cmd = Gio.SimpleAction.new_stateful("user-command", GLib.VariantType.new('s'), GLib.Variant.new_string(""))
        action_user_cmd.connect("activate", self.on_menu_user_command)
        self.add_action(action_user_cmd)

        # Host-tree-only (same reasoning as "open-sftp" above) — the tab
        # menu's own Copy to Clipboard is the app-scoped app.copy-host-*
        # (see app.py / TerminalPaneWindow.copy_host_field_for_tab_page).
        # 3 separate actions, not 1 parametrized one, deliberately: a
        # GSimpleAction's "enabled" is per action name, not per (action,
        # parameter) pair, and "user@hostname" needs to grey out
        # independently of the other two when the target host has no
        # username (see on_tree_right_click).
        for field in ("name", "address", "userhost"):
            action = Gio.SimpleAction.new(f"copy-host-{field}", None)
            action.connect("activate", self.on_menu_copy_host_field, field)
            self.add_action(action)

        # Quicky row context menu — acts on self.last_clicked_quicky_index
        # (see on_quicky_right_click).
        action_quicky_insert = Gio.SimpleAction.new("quicky-insert", None)
        action_quicky_insert.connect("activate", self.on_menu_quicky_insert)
        self.add_action(action_quicky_insert)

        action_quicky_run = Gio.SimpleAction.new("quicky-run", None)
        action_quicky_run.connect("activate", self.on_menu_quicky_run)
        self.add_action(action_quicky_run)

        action_quicky_edit = Gio.SimpleAction.new("quicky-edit", None)
        action_quicky_edit.connect("activate", self.on_menu_quicky_edit)
        self.add_action(action_quicky_edit)

        action_quicky_send_to_batch = Gio.SimpleAction.new("quicky-send-to-batch", None)
        action_quicky_send_to_batch.connect("activate", self.on_menu_quicky_send_to_batch)
        self.add_action(action_quicky_send_to_batch)

        action_quicky_delete = Gio.SimpleAction.new("quicky-delete", None)
        action_quicky_delete.connect("activate", self.on_menu_quicky_delete)
        self.add_action(action_quicky_delete)

        action_quicky_move_up = Gio.SimpleAction.new("quicky-move-up", None)
        action_quicky_move_up.connect("activate", self.on_menu_quicky_move_up)
        self.add_action(action_quicky_move_up)

        action_quicky_move_down = Gio.SimpleAction.new("quicky-move-down", None)
        action_quicky_move_down.connect("activate", self.on_menu_quicky_move_down)
        self.add_action(action_quicky_move_down)

        self.last_clicked_quicky_index = None

        # 2. Create GMenu (models) — host-tree / Quickies-panel only. The
        # tab-strip's own context menu (tab_menu_model/tab_copy_host_menu)
        # was already built by _setup_terminal_pane_actions above.

        self.copy_host_menu = Gio.Menu()
        self._populate_copy_host_menu(None)

        # Menu for a HOST
        host_menu = Gio.Menu()
        host_menu.append(_("Connect"), "win.connect")
        host_menu.append(_("Edit..."), "win.edit") # "win." = window prefix
        host_menu.append(_("Clone"), "win.clone")
        host_menu.append(_("Connect SFTP"), "win.open-sftp")
        host_menu.append_submenu(_("Copy to Clipboard"), self.copy_host_menu)
        host_menu.append(_("Delete"), "win.delete")
        self.user_commands_menu_section = Gio.Menu()
        host_menu.append_section(None, self.user_commands_menu_section)

        # Menu for a GROUP
        group_menu = Gio.Menu()
        group_menu.append(_("Rename..."), "win.rename")
        group_menu.append(_("Delete"), "win.delete")

        quicky_menu = Gio.Menu()
        quicky_menu.append(_("Insert into Terminal"), "win.quicky-insert")
        quicky_menu.append(_("Run"), "win.quicky-run")
        quicky_menu.append(_("Edit..."), "win.quicky-edit")
        quicky_menu.append(_("Send to Batch Command"), "win.quicky-send-to-batch")
        quicky_menu.append(_("Move Up"), "win.quicky-move-up")
        quicky_menu.append(_("Move Down"), "win.quicky-move-down")
        quicky_menu.append(_("Delete"), "win.quicky-delete")

        # 3. Create Popover (widgets)
        self.popover_host = Gtk.PopoverMenu.new_from_model(host_menu)
        self.popover_group = Gtk.PopoverMenu.new_from_model(group_menu)
        self.popover_quicky = Gtk.PopoverMenu.new_from_model(quicky_menu)
        self.popover_host.set_parent(self) # Set parent once to the main window
        self.popover_quicky.set_parent(self)
        self.popover_group.set_parent(self) # Set parent once to the main window

        # Built once here — not lazily on the host tree's first right-click
        # (see on_tree_right_click's history) — so the section's one-time
        # jump from 0 items (freshly created above) to however many user
        # commands are configured happens now, well before any popover is
        # ever shown, instead of racing GtkPopoverMenu's own re-measure of
        # a Gio.MenuModel it's already bound to. Settings' on_apply calls
        # this again whenever the user commands list itself changes.
        self.build_user_commands_menu()

    def on_tree_right_click(self, gesture, n_press, x, y):
        """Right-click handler: Shows PopoverMenu (100% GTK4).
        Connected to 'released' so the button is already up when the popover
        opens — prevents the release event from activating the first menu item."""
        tree_view = gesture.get_widget()
        path_info = tree_view.get_path_at_pos(int(x), int(y))

        if path_info:
            path, col, cell_x, cell_y = path_info
            tree_view.get_selection().select_path(path)

            model = tree_view.get_model()
            tree_iter = model.get_iter(path)
            node_type = model.get_value(tree_iter, COL_TYPE)

            # The synthetic "local machine" row isn't a real host — no
            # edit/clone/remove/SFTP menu applies to it.
            if node_type == "local":
                return

            # Get the row's rectangle to "attach" the popover to
            rect = tree_view.get_cell_area(path, col)

            self.lookup_action("connect").set_enabled(True)
            self.lookup_action("open-sftp").set_enabled(True)
            self.lookup_action("edit").set_enabled(True)
            self.lookup_action("clone").set_enabled(True)

            if node_type == "host":
                host_config = model.get_value(tree_iter, COL_DATA)
                has_user = "@" in (host_config.get("host") or "")
                # Always real hosts here (the "local" node type returns
                # early above) — name/address are unconditionally
                # applicable; only re-enabling them (not just userhost)
                # matters because these actions are shared with the tab
                # menu, which may have last disabled them for a local tab.
                self.lookup_action("copy-host-name").set_enabled(True)
                self.lookup_action("copy-host-address").set_enabled(True)
                self.lookup_action("copy-host-userhost").set_enabled(has_user)
                self._populate_copy_host_menu(host_config)
                self.popover_host.set_pointing_to(rect)
                self.popover_host.popup()

            elif node_type == "group":
                self.popover_group.set_pointing_to(rect)
                self.popover_group.popup()

    def build_user_commands_menu(self):
        """(Re)populates the user commands section of the host context menu
        from current settings. Called once at startup (setup_actions_and_
        popovers) and again from Settings' on_apply whenever the list
        itself changes — deliberately NOT on every right-click anymore:
        mutating a Gio.MenuModel already bound to a live GtkPopoverMenu
        makes it re-measure itself asynchronously, so popping the menu up
        immediately after used to show it at its *previous* (smaller) size
        the very first time this section went from empty to populated —
        cut off, needing a scroll — even though every popup after that was
        already sized right (same item count as before, nothing to
        re-measure). Rebuilding ahead of time, off the interactive path,
        means that one-time jump in size never happens while a popover is
        actually open."""
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

    def get_terminal_context_snippet(self):
        """Read-only helper for the AI panel's "attach context" button:
        the active terminal's current selection if there is one, otherwise
        the last ~20 lines of its output. Returns None if there's no active
        terminal at all. Never called automatically — only on explicit
        user action, so the AI never sees terminal content unasked."""
        terminal = self.get_active_terminal()
        if terminal is None:
            return None
        if terminal.get_has_selection():
            return terminal.get_text_selected(Vte.Format.TEXT)
        return "\n".join(self._dump_terminal_text(terminal).splitlines()[-20:])

    def _prepare_command(self, command_template, host_config):
        """Replaces placeholders in a command template with values from host_config."""
        if not command_template:
            return ""

        # "host" can be a present-but-null key (not just a missing one) —
        # e.g. a host saved with an empty hostname field, or migrated from
        # HOST_CONFIG_TEMPLATE's own None default — so .get("host", "")'s
        # fallback alone doesn't catch it; `or ""` does.
        host_str = host_config.get("host") or ""
        user, _, host = host_str.rpartition('@')

        replacements = {
            "$name": host_config.get("name") or "",
            "$host": host,
            "$user": user
        }

        for placeholder, value in replacements.items():
            command_template = command_template.replace(placeholder, shlex.quote(value))
        return command_template
    # --- ---

    def on_menu_open_sftp_from_tree(self, action, param):
        """win.open-sftp — the host tree's "Connect SFTP" item only. The
        tab context menu's own "Connect SFTP" is the app-scoped
        app.open-sftp (see app.py / TerminalPaneWindow.open_sftp_for_tab_page)
        — a menu inside a DetachedTabWindow could never reach a win.*
        action registered only on the main window, which is why these are
        no longer the same action (they used to be, keyed off
        self.last_clicked_tab, before detachable tabs)."""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if not tree_iter:
            return
        host_config = model.get_value(tree_iter, COL_DATA)
        logging.info(f"Opening SFTP stub for: {host_config['name']}")
        self._open_sftp_tab(host_config)

    # --- Handlers for the global menu ---

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
        logging.debug("Settings dialog called.")
        dialog = SettingsDialog(self, self.settings_manager)
        dialog.present()

    def on_menu_batch_command(self, *args):
        """Shows the Batch Command window — sends one command to a chosen set of open terminal tabs."""
        dialog = BatchCommandDialog(self)
        dialog.present()

    def on_menu_about(self, action, param):
        """Shows the 'About' window."""
        dialog = Adw.AboutWindow(transient_for=self)
        dialog.set_application_name("ThongSSH")
        dialog.set_version(__version__)
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.set_comments(_("SSH client with a tree-like host structure"))
        current_year = datetime.datetime.now().year
        year_range = "2025" if current_year <= 2025 else f"2025–{current_year}"
        dialog.set_copyright(f"© {year_range} Mikhael Karpov")
        dialog.set_developers(["Gemini Code Assist", "Claude Code (Anthropic)"])
        dialog.set_designers(["Mikhael Karpov (lknsfos)"])
        dialog.set_application_icon(self.settings_manager.get("interface.icon"))
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

        # The synthetic "local machine" row can't be deleted (it isn't part
        # of hosts.json to begin with).
        if node_type == "local":
            return

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

    def _get_target_host_config(self):
        """The host config the tree's own context-menu actions
        (win.copy-host-*) should read from — whatever's currently selected
        in the host tree. (Before detachable tabs, this also handled the
        tab-menu case via self.last_clicked_tab; that's now the app-scoped
        app.copy-host-* / TerminalPaneWindow.copy_host_field_for_tab_page
        instead — see app.py.)"""
        selection = self.tree_view.get_selection()
        model, tree_iter = selection.get_selected()
        if tree_iter:
            return model.get_value(tree_iter, COL_DATA)
        return None

    def _populate_copy_host_menu(self, host_config):
        """Rewrites the tree's "Copy to Clipboard" submenu's 3 item labels
        to the given host's actual values, in place of a generic title —
        called from on_tree_right_click with whatever host is about to be
        right-clicked (host_config=None gives a safe generic fallback,
        used only for the one-time initial build before any real host has
        ever been clicked). Tree-only — see
        TerminalPaneWindow._populate_tab_copy_host_menu for the tab
        context menu's own (app-scoped) equivalent. Item count never
        changes — see this menu's own creation comment in
        setup_actions_and_popovers for why that specifically (not label
        text) is what a GtkPopoverMenu can't re-measure in time for its
        very next popup."""
        self.copy_host_menu.remove_all()
        host_str = (host_config or {}).get("host") or ""
        name = (host_config or {}).get("name") or ""
        user, _sep, address = host_str.rpartition('@')
        self.copy_host_menu.append(name or _("Server name"), "win.copy-host-name")
        self.copy_host_menu.append(address or _("Hostname/IP"), "win.copy-host-address")
        self.copy_host_menu.append(host_str if user else _("user@hostname"), "win.copy-host-userhost")

    def on_menu_copy_host_field(self, action, param, field):
        """win.copy-host-{name,address,userhost} — see copy_host_menu in
        setup_actions_and_popovers. Shared by the host tree and tab
        context menus alike (_get_target_host_config picks the right host
        either way). "field" is bound at connect() time, not a GAction
        parameter — see the actions' own creation comment for why."""
        host_config = self._get_target_host_config()
        if not host_config:
            return
        host_str = host_config.get("host") or ""
        user, _sep, address = host_str.rpartition('@')
        if field == "name":
            text = host_config.get("name") or ""
        elif field == "address":
            text = address
        elif field == "userhost":
            text = host_str if user else ""
        else:
            return
        if not text:
            return
        # Gdk.Clipboard.set() boxes the string into a GValue and relies on
        # GDK's built-in text serializer — reported to silently not reach
        # the system clipboard on Linux (see markdown_view.py's
        # _on_copy_code_clicked, which hit the same thing first).
        # new_for_bytes() sidesteps that entirely.
        #
        # X11/Wayland actually have TWO independent selections, and a
        # terminal's paste shortcuts split across them: Ctrl+V/Ctrl+Shift+V
        # read the CLIPBOARD selection, but Shift+Insert and (in VTE's
        # default menu) right-click-Paste read PRIMARY — the one that's
        # normally only updated by highlighting text with the mouse.
        # Setting just CLIPBOARD left PRIMARY stale, so those two pasted
        # whatever had last been *selected* somewhere instead of what was
        # just copied here. Both need the same content.
        display = self.get_display()
        for clipboard in (display.get_clipboard(), display.get_primary_clipboard()):
            provider = Gdk.ContentProvider.new_for_bytes(
                "text/plain;charset=utf-8", GLib.Bytes.new(text.encode("utf-8"))
            )
            clipboard.set_content(provider)

    # --- 6. Connection Logic (Terminal) ---

    def on_watermark_toggle_clicked(self, button):
        # "clicked", not "toggled" — button is a plain Adw.SplitButton
        # (momentary click, no on/off state of its own; see its creation
        # comment) — the flip happens here, against the actual state
        # (interface.watermark_enabled), not read off the button.
        new_state = not self.settings_manager.get("interface.watermark_enabled")
        self.settings_manager.set("interface.watermark_enabled", new_state)
        self.settings_manager.save()
        set_split_button_active_style(button, new_state)
        self.apply_watermark_settings_to_all()

    def _on_watermark_position_changed(self, position_id):
        """PositionGrid.connect_changed callback for the header-bar
        popover's own grid (see its creation, next to watermark_toggle_
        button)."""
        self.settings_manager.set("interface.watermark_position", position_id)
        self.settings_manager.save()
        self.apply_watermark_settings_to_all()

    def _resolve_physical_digit(self, keyval, keycode):
        """Digit-key counterpart to _resolve_latin_letter, same physical-
        key-regardless-of-layout approach — needed for the quicky_paste_N/
        quicky_run_N shortcuts (Ctrl+1..5 / Ctrl+Shift+1..5 by default)."""
        if Gdk.KEY_0 <= keyval <= Gdk.KEY_9:
            return chr(keyval)

        display = self.get_display()
        if display is None:
            return None
        success, _keys, keyvals = display.map_keycode(keycode)
        if not success:
            return None
        for kv in keyvals:
            if Gdk.KEY_0 <= kv <= Gdk.KEY_9:
                return chr(kv)
        return None

    def _quicky_shortcut_matches(self, settings_key, is_ctrl, is_shift, digit):
        """Digit counterpart to _shortcut_matches, for the quicky_paste_N/
        quicky_run_N shortcuts — see that method for the general shape."""
        accel = self.settings_manager.get(settings_key)
        if not accel:
            return False
        success, keyval, mods = Gtk.accelerator_parse(accel)
        if not success:
            return False
        want_ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        want_shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        want_digit = chr(keyval) if Gdk.KEY_0 <= keyval <= Gdk.KEY_9 else None
        return bool(is_ctrl) == want_ctrl and bool(is_shift) == want_shift and digit == want_digit
