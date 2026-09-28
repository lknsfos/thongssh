# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""DetachedTabWindow — a minimal top-level window created when a tab is
dragged out of any tab strip (main window or another detached window) far
enough to tear off, or via the tab context menu's "Detach" item (see
app.py's create_detached_window / TerminalPaneWindow.detach_tab_page).

Per-pane widget shape is the same for every pane, main window included —
that uniformity is what makes native drag/reattach work both ways for
free, and lets a user drag a *second* tab into an already floating window,
growing it (a natural side effect of Adw.TabView's own drag machinery, not
extra code here).
"""

import os
import logging

from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GObject

from .tab_window_base import TerminalPaneWindow
from .i18n import _


class DetachedTabWindow(TerminalPaneWindow):
    """Title only — no sidebar/batch/watermark/quickies/split buttons, no
    hamburger menu. Just a header bar (native close/min/max) over a single
    Adw.TabBar + Adw.TabView."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.set_default_size(900, 600)

        # Real ThongSSHApp-level singletons (see app.py) — same dict/
        # object identity as every other window's, not copies, so a tab
        # dragged in or out of this window is instantly visible to (and
        # torn down correctly by) whichever window it ends up in.
        app = self.get_application()
        self.settings_manager = app.settings_manager
        self.keyring = app.keyring
        self.tab_data = app.tab_data
        self.open_sessions = app.open_sessions

        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer_box)

        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True)  # native close/min/max
        self.window_title = Adw.WindowTitle(title="ThongSSH")
        header_bar.set_title_widget(self.window_title)
        # Adw.TabBar's own native drag-to-detach/drag-to-reattach only ever
        # engages when the SOURCE view has more than one page (confirmed
        # against libadwaita's own source — adw-tab-box.c's drag gesture is
        # gated on `adw_tab_view_get_n_pages (view) > 1`, specifically so a
        # window's only remaining tab can't be dragged away and leave it
        # empty). A DetachedTabWindow's tabview is, by construction, almost
        # always down to exactly one page — meaning the very drag gesture
        # this window most needs (dragging its lone tab back onto the main
        # window) is the one case libadwaita's own TabBar refuses to start
        # at all. This button is the explicit, drag-independent way around
        # that — the same relationship "Detach" in the tab menu already has
        # to the *other* direction.
        attach_btn = Gtk.Button(icon_name="go-up-symbolic")
        attach_btn.set_tooltip_text(_("Attach to Main Window"))
        attach_btn.connect("clicked", self.on_attach_to_main_clicked)
        header_bar.pack_start(attach_btn)
        outer_box.append(header_bar)
        self._title_binding = None

        # self.terminal_overlay: same role as the main window's own —
        # anchors the in-terminal find bar (see
        # TerminalPaneWindow._build_find_window), a Gtk.Overlay wraps the
        # tab-bar+tabview column rather than sitting inside the structural
        # box tree itself.
        self.terminal_overlay = Gtk.Overlay()
        self.terminal_overlay.set_hexpand(True)
        self.terminal_overlay.set_vexpand(True)
        outer_box.append(self.terminal_overlay)

        pane_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_overlay.set_child(pane_box)

        self.tabview = Adw.TabView()
        self.tabview.set_vexpand(True)
        self.tabview.set_hexpand(True)
        self.tabview.connect("close-page", self.on_tabview_close_page)
        self.tabview.connect("setup-menu", self.on_tabview_setup_menu)
        self.tabview.connect("create-window", self.on_tabview_create_window)
        self.tabview.connect("page-detached", self.on_tabview_page_detached)
        self.tabview.connect("notify::selected-page", self._on_selected_page_changed)

        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tabview)
        self.tab_bar.set_autohide(False)
        self.tab_bar.set_expand_tabs(False)  # compact, left-aligned — old Notebook look

        new_local_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_local_btn.set_tooltip_text(_("New local terminal"))
        new_local_btn.add_css_class("flat")
        new_local_btn.connect("clicked", self._on_new_local_terminal_clicked)
        self.tab_bar.set_start_action_widget(new_local_btn)

        pane_box.append(self.tab_bar)
        pane_box.append(self.tabview)

        # win.close-tab/win.copy-clipboard/win.paste-clipboard/win.send-file/
        # win.find-in-terminal/win.save-log-tab + the terminal right-click
        # popover + this window's own tab_menu_model/tab_copy_host_menu +
        # the find bar — everything a terminal pane genuinely needs, with
        # zero host-tree/split/AI/sync chrome.
        self._setup_terminal_pane_actions()
        self.tabview.set_menu_model(self.tab_menu_model)

        # Global key controller — only the two shortcuts that make sense
        # in a window with no host tree/Quickies panel to target.
        key_controller = Gtk.EventControllerKey.new()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self.on_window_key_pressed)
        self.add_controller(key_controller)

    def _get_active_tabview(self):
        """No split concept in a detached window — always the one TabView."""
        return self.tabview

    def _on_selected_page_changed(self, tabview, pspec):
        """Keeps the header bar's title live-bound to whichever page is
        currently selected — bind_property (not a one-shot set_title) so
        it also tracks that page's OWN title changing later (a local
        tab's "local: <dir>" cwd-tracking rename, a reconnect, ...) with
        no extra wiring. Re-bound here on every selection change since a
        GObject property binding is tied to one specific source object
        (the previously-selected page), not "whichever page happens to be
        selected" generically."""
        if self._title_binding is not None:
            self._title_binding.unbind()
            self._title_binding = None
        page = tabview.get_selected_page()
        if page is not None:
            self._title_binding = page.bind_property(
                "title", self.window_title, "title", GObject.BindingFlags.SYNC_CREATE
            )
        else:
            self.window_title.set_title("ThongSSH")
        self._sync_find_target_terminal()

    def _on_new_local_terminal_clicked(self, button):
        """Mirrors ThongSSHWindow's own "+" button — opens a new local
        terminal in this window's one pane."""
        cwd = os.environ.get("HOME", os.path.expanduser("~"))
        label = self._dir_short_label(cwd)
        self.start_session({"name": f"local: {label}", "protocol": "local", "cwd": cwd})

    def on_attach_to_main_clicked(self, button):
        """The explicit, always-working counterpart to "Detach" — see the
        header-bar button's own construction comment for why dragging a
        lone tab (the state this window is almost always in) doesn't work
        through Adw.TabBar's native mechanism at all. Moves the currently
        selected page into whichever pane is the main window's own active
        one, via the exact same transfer_page() primitive a working drag
        would have used — this window auto-closes afterward via
        on_tabview_page_detached below, same as a successful drag."""
        page = self.tabview.get_selected_page()
        if page is None:
            return
        main_window = next(
            (w for w in self.get_application().get_windows() if not isinstance(w, DetachedTabWindow)),
            None,
        )
        if main_window is None:
            logging.warning("Attach to Main Window: no main window found (only detached windows are open?).")
            return
        dest_tabview = main_window._get_active_tabview()
        self.tabview.transfer_page(page, dest_tabview, dest_tabview.get_n_pages())
        main_window.present()

    def on_tabview_page_detached(self, tabview, page, position):
        """Once this window's one-and-only pane has lost its last tab
        (dragged back to the main window, or to another detached window),
        the now-empty window has no reason to stick around. idle_add: this
        fires mid-drag/mid-transfer, before GTK has necessarily finished
        the operation that emitted it — closing synchronously here has been
        a source of the exact kind of "widget torn down mid-signal" bugs
        idle_add exists to sidestep elsewhere in this codebase (see
        e.g. on_tab_close_button_clicked's own history)."""
        if tabview.get_n_pages() == 0:
            GLib.idle_add(self.close)

    def on_window_key_pressed(self, controller, keyval, keycode, modifier):
        """Only the two shortcuts that make sense here — no host-search/
        Quickies shortcuts, nothing in this window to target."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK
        is_shift = modifier & Gdk.ModifierType.SHIFT_MASK
        letter = self._resolve_latin_letter(keyval, keycode) if is_ctrl else None

        if self._shortcut_matches("shortcuts.close_tab", is_ctrl, is_shift, letter):
            self.on_menu_close_tab(None, None)
            return True
        if self._shortcut_matches("shortcuts.find_in_terminal", is_ctrl, is_shift, letter):
            self.on_menu_find_in_terminal(None, None)
            return True
        return False
