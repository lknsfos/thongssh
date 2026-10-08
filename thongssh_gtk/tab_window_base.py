# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""TerminalPaneWindow — shared base for every top-level window that hosts
an Adw.TabView of terminal/SFTP tabs: the main ThongSSHWindow (with its
up-to-4-way split of panes) and DetachedTabWindow (a single pane, created
when a tab is dragged out into its own window — see app.py's
create_detached_window / Adw.TabView's "create-window" signal).

Everything here operates purely on "the active Adw.TabView of this window"
(via the overridable _get_active_tabview) plus the app-wide tab_data/
open_sessions dicts and settings_manager/keyring (both now real
singletons living on ThongSSHApp — see app.py) — zero dependency on the
host tree, sidebar, split buttons, or any other main-window-only chrome.
"""

import os
import sys
import signal
import shlex
import logging
import datetime
import re
import platform
import shutil
import subprocess

from gi.repository import Gtk, Adw, Gdk, GLib, Vte, Pango, Gio

from .config import CONFIG_DIR
from .paths import resolve_log_dir
from .dialogs import InputDialog, RenameTabDialog, TabColorDialog
from .send_file import SendFileDialog, guess_remote_cwd
from .colors import get_scheme_colors
from .i18n import _

# PCRE2 compile-option bits used for in-terminal search (Vte.Regex wraps
# PCRE2 directly and doesn't expose these as GI constants). Values are from
# pcre2.h and are part of PCRE2's stable ABI.
_PCRE2_CASELESS = 0x00000008
_PCRE2_MULTILINE = 0x00000400


def _tabview_has_page(tabview, page):
    """Whether `page` is currently one of `tabview`'s own pages — safe to
    call with a page that belongs to some OTHER view entirely (returns
    False), unlike Adw.TabView.get_page_position() itself: passing it a
    page that isn't one of its own doesn't return -1 the way its int
    return type suggests, it raises a loud (but non-fatal)
    'page_belongs_to_this_view' assertion CRITICAL — confirmed live while
    testing _find_tabview_for_page below scanning every pane for a page
    that only ever lives in one of them. Walking get_nth_page() instead
    never passes a foreign page into anything, so it can't trip that
    assertion."""
    for i in range(tabview.get_n_pages()):
        if tabview.get_nth_page(i) is page:
            return True
    return False

# WATERMARK_POSITIONS (constants.py) ids -> (halign, valign) for the
# terminal watermark overlay — every id there must have an entry here.
_WATERMARK_ALIGN = {
    "top-left": (Gtk.Align.START, Gtk.Align.START),
    "top-center": (Gtk.Align.CENTER, Gtk.Align.START),
    "top-right": (Gtk.Align.END, Gtk.Align.START),
    "center-left": (Gtk.Align.START, Gtk.Align.CENTER),
    "center": (Gtk.Align.CENTER, Gtk.Align.CENTER),
    "center-right": (Gtk.Align.END, Gtk.Align.CENTER),
    "bottom-left": (Gtk.Align.START, Gtk.Align.END),
    "bottom-center": (Gtk.Align.CENTER, Gtk.Align.END),
    "bottom-right": (Gtk.Align.END, Gtk.Align.END),
}

# Env vars build-appimage.sh's AppRun exports for its OWN bundled runtime's
# benefit — never meant to also apply to whatever the user runs *inside* a
# spawned terminal. See _wrap_cmd_for_appimage_env's docstring below.
_APPIMAGE_ENV_VARS_TO_CLEAN = [
    "LD_LIBRARY_PATH", "GI_TYPELIB_PATH", "GDK_PIXBUF_MODULE_FILE",
    "XDG_DATA_DIRS", "GSETTINGS_SCHEMA_DIR", "PYTHONHOME", "PYTHONPATH",
]


def _wrap_cmd_for_appimage_env(cmd, extra_env):
    """Wraps `cmd` in a tiny `/bin/sh -c '...; exec "$@"'` that restores
    the AppImage-polluted vars above to whatever they looked like before
    AppRun touched them (or removes them if they weren't set at all), and
    exports extra_env (the sshpass/SSHPASS case), before exec-ing into the
    real command — same process, same pid, VTE/the pty never notice.

    A *sibling* envv passed straight to spawn_sync would be the more
    obvious way to do this, and was the first thing tried here — but
    empirically, Vte.Terminal.spawn_sync's envv does NOT fully replace the
    child's environment the way GLib's own spawn functions do; it merges
    the given entries on top of the full inherited environment instead
    (confirmed live: a variable simply left out of envv still leaked
    through). A shell-level `unset`/`export`, run for real in the child
    right before exec, has no such ambiguity.

    Only called when there's actually something to do — see the call
    site's own `if` guard, which is what keeps a normal, non-AppImage run
    completely untouched (this function only runs at all once
    THONGSSH_RUNNING_FROM_APPIMAGE is set, which no other way of running
    this app ever sets)."""
    parts = ["unset " + " ".join(_APPIMAGE_ENV_VARS_TO_CLEAN)]
    for var in _APPIMAGE_ENV_VARS_TO_CLEAN:
        # THONGSSH_APPIMAGE_HAD_<var> is only ever exported (as "1") when
        # <var> genuinely had a value before AppRun touched it — see its
        # own stash loop — so a plain -n check is enough, no +x needed.
        parts.append(f'[ -n "$THONGSSH_APPIMAGE_HAD_{var}" ] && export {var}="$THONGSSH_APPIMAGE_ORIG_{var}"')
    bookkeeping = ["THONGSSH_RUNNING_FROM_APPIMAGE"]
    bookkeeping += [f"THONGSSH_APPIMAGE_HAD_{v}" for v in _APPIMAGE_ENV_VARS_TO_CLEAN]
    bookkeeping += [f"THONGSSH_APPIMAGE_ORIG_{v}" for v in _APPIMAGE_ENV_VARS_TO_CLEAN]
    parts.append("unset " + " ".join(bookkeeping))
    for key, value in extra_env.items():
        parts.append(f"export {key}={shlex.quote(value)}")
    parts.append('exec "$@"')
    return ["/bin/sh", "-c", "; ".join(parts), "sh"] + cmd


class TerminalPaneWindow(Adw.ApplicationWindow):
    """Not directly instantiated — subclassed by ThongSSHWindow and
    DetachedTabWindow. Subclasses MUST provide:
      - self.settings_manager / self.keyring / self.tab_data / self.open_sessions
        (the real ThongSSHApp-level singletons — see app.py)
      - self.terminal_overlay: a Gtk.Overlay the find bar/watermarks can
        anchor to (see _build_find_window)
      - _get_active_tabview(): the Adw.TabView new tabs / menu actions
        should target right now
    """

    # --- Tab identity / active-tab helpers ---

    def get_active_terminal(self):
        """Returns the active Vte.Terminal widget or None."""
        page = self.get_active_terminal_widget()
        if page is not None and page in self.open_sessions:
            terminal, _pid = self.open_sessions[page]
            return terminal
        return None

    def get_active_terminal_widget(self):
        """Returns the active tab's Adw.TabPage — the key used in
        open_sessions/tab_data. Named for its pre-migration role (the
        tab's root container widget was the dict key then, back when tabs
        lived in a Gtk.Notebook); the TabPage itself is now that stable
        per-tab identity, and it's what survives detach/reattach and
        cross-pane drags, so every existing call site that just treats
        this as an opaque dict key needs no further change."""
        tabview = self._get_active_tabview()
        if tabview is None:
            return None
        return tabview.get_selected_page()

    def _find_tab_widget_for(self, widget):
        """Walks up from any descendant widget (a Vte.Terminal, its
        watermark label, anything living inside a tab's own content) to
        the Adw.TabPage that owns it, if any. Stops one level BELOW the
        first ancestor whose own parent is an Adw.TabView: that ancestor
        (an internal Adw.Bin, not a widget this code ever created) sits
        between the TabView and the real "child" widget originally handed
        to TabView.append() — get_page() only recognizes that original
        child, and raises an Adwaita-CRITICAL assertion (silently caught
        here as "not found", since get_page() doesn't return None for a
        foreign widget the way its own docs/type signature suggest) for
        anything else, including that Bin wrapper itself. A previous
        version of this walk called get_page() one level too deep (on the
        Bin), which made it always fail — confirmed live: real symptom
        was "Send File" permanently greyed out, since this same helper
        backs the "can this tab send files" check."""
        node = widget
        prev = None
        while node is not None:
            parent = node.get_parent()
            if isinstance(parent, Adw.TabView):
                if prev is None:
                    return None
                page = parent.get_page(prev)
                if page in self.tab_data:
                    return page
                return None
            prev = node
            node = parent
        return None

    def _create_tab_page(self, tabview, content_widget, icon_name, title, select=True):
        """Appends content_widget as a new tab in tabview, with the given
        icon/title, optionally selecting it — replaces the old
        _create_tab_label + append_page + _mark_tab_draggable +
        set_current_page sequence (native Adw.TabView handles the label,
        close button, and drag-to-reorder/drag-to-another-view on its
        own)."""
        page = tabview.append(content_widget)
        page.set_title(title)
        page.set_icon(Gio.ThemedIcon.new(icon_name))
        if select:
            tabview.set_selected_page(page)
        return page

    def _effective_display_name(self, tab_info):
        """The tab's name before the "N: " position prefix and the
        disconnected strikethrough — tab_data["custom_title"] (a "Full
        Rename", see rename_tab_page) wins outright over ["base_title"]
        (the plain, auto-managed name — "local: ~", a host's own name,
        kept live-updated by e.g. _tick_local_cwd); an optional ["tag"] is
        appended in [brackets] on top of either one. Also what $name
        resolves to in watermarks/Quicky templates (see _render_tab_title
        syncing tab_data["config"]["name"] to this same value) — so a tag
        or rename shows up there too, not just in the tab strip."""
        name = tab_info.get("custom_title") or tab_info.get("base_title") or ""
        tag = tab_info.get("tag")
        if tag:
            name = f"{name} [{tag}]"
        return name

    def _render_tab_title(self, tabview, page, position=None):
        """Recomputes and applies one tab's displayed title from its
        stored pieces: _effective_display_name(tab_info), its 1-based
        position within tabview (so it lines up with Adw.TabView's own
        native Alt+1..9 switch-to-tab-N shortcuts), and whether it's
        currently marked disconnected. This is the one place all of those
        combine — every call site that used to call page.set_title(...)
        directly now updates the relevant piece of tab_data and calls this
        (or _renumber_tabview) instead.

        Also keeps tab_data["config"]["name"] (what $name resolves to
        elsewhere) in sync with the same name, refreshing the watermark
        when it actually changed — this is the one place THAT happens too
        now, so a rename/tag change is reflected there automatically
        without every caller needing to remember to do it separately.

        position=None (the default) means "figure it out by scanning
        tabview" — callers that already know it from their own loop (see
        _renumber_tabview) can skip that scan by passing it directly."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        if position is None and tabview is not None:
            position = next(
                (i for i in range(tabview.get_n_pages()) if tabview.get_nth_page(i) is page), None
            )
        name = self._effective_display_name(tab_info)
        title = f"{position + 1}: {name}" if position is not None else name
        if tab_info.get("disconnected"):
            title = "".join(ch + "̶" for ch in title)
        page.set_title(title)

        config = tab_info.get("config")
        if config is not None and config.get("name") != name:
            config["name"] = name
            self._update_watermark_for_tab(page)

    def _renumber_tabview(self, tabview, *_signal_args):
        """Re-renders every page's title in one TabView — needed whenever
        the pages themselves or their order changes (added, closed, drag-
        reordered, or transferred to/from another TabView), since the "N:"
        prefix _render_tab_title adds depends on each page's live position.
        Connected to every pane's/detached window's own TabView "page-
        attached"/"page-detached"/"page-reordered" signals (see
        _create_pane_tabview and DetachedTabWindow.__init__) — those all
        share this same (AdwTabPage, gint) signature, so it doubles as
        their handler directly."""
        if tabview is None:
            return
        for i in range(tabview.get_n_pages()):
            self._render_tab_title(tabview, tabview.get_nth_page(i), position=i)

    # --- Adw.TabView signal handlers (connected once per TabView, by
    # whichever subclass creates it) ---

    def on_tabview_close_page(self, tabview, page):
        """Handles Adw.TabView's "close-page" signal — fired whenever a
        page is asked to close, whether via the TabBar's own close button,
        Ctrl+W (on_menu_close_tab), or a tab-menu action. Folds together
        what used to be two separate paths (close_tab +
        on_tab_close_button_clicked): stop this tab's logging/cwd timers,
        drop it from tab_data/open_sessions, tell the TabView the close is
        confirmed, then SIGTERM (and, after a grace period, SIGKILL) its
        process — the tab itself disappears immediately; the process is
        cleaned up in the background so the UI never blocks on however
        long that takes (matters most for a "local" tab running the
        user's real login shell)."""
        self._stop_session_logging(page)
        self._stop_local_cwd_tracking(page)
        self._stop_command_watch(page)
        session = self.open_sessions.pop(page, None)
        self.tab_data.pop(page, None)
        tabview.close_page_finish(page, True)

        if session is not None:
            _terminal, pid = session
            logging.debug(f"Sending SIGTERM to process {pid}...")
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pid = None
            if pid is not None:
                def escalate_to_sigkill(pid=pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        logging.debug(f"Process {pid} ignored SIGTERM; sent SIGKILL.")
                    except ProcessLookupError:
                        pass
                    return False
                GLib.timeout_add_seconds(3, escalate_to_sigkill)

        def focus_active_terminal():
            active_terminal = self.get_active_terminal()
            if active_terminal:
                active_terminal.grab_focus()
        GLib.idle_add(focus_active_terminal)

        return True

    def _populate_tab_copy_host_menu(self, host_config):
        """Rewrites the tab context menu's "Copy to Clipboard" submenu (see
        _setup_terminal_pane_actions) to the given host's actual values —
        app-scoped counterpart of window.py's _populate_copy_host_menu
        (which stays win-scoped/tree-only). host_config=None gives a safe
        generic fallback."""
        self.tab_copy_host_menu.remove_all()
        host_str = (host_config or {}).get("host") or ""
        name = (host_config or {}).get("name") or ""
        user, _sep, address = host_str.rpartition('@')
        self.tab_copy_host_menu.append(name or _("Server name"), "app.copy-host-name")
        self.tab_copy_host_menu.append(address or _("Hostname/IP"), "app.copy-host-address")
        self.tab_copy_host_menu.append(host_str if user else _("user@hostname"), "app.copy-host-userhost")

    def on_tabview_setup_menu(self, tabview, page):
        """Handles Adw.TabView's "setup-menu" signal — fired right before
        the native tab context menu (see tab_menu_model in
        _setup_terminal_pane_actions) is shown for `page`, and again with
        page=None when it's dismissed. Tab-scoped actions are app-scoped
        (see app.py's "why win.* -> app.* at all") since this menu can be
        popped up from any window's TabView, so their enabled-state and
        the Copy-to-Clipboard submenu contents are set here rather than in
        a window-local lookup_action call."""
        app = self.get_application()
        app.last_clicked_tab_page = page

        sftp_action = app.lookup_action("open-sftp")
        ssh_action = app.lookup_action("open-ssh-from-tab")
        detach_action = app.lookup_action("tab-detach")
        copy_actions = {f: app.lookup_action(f"copy-host-{f}") for f in ("name", "address", "userhost")}

        tab_info = self.tab_data.get(page) if page is not None else None
        if tab_info is None:
            sftp_action.set_enabled(False)
            ssh_action.set_enabled(False)
            detach_action.set_enabled(False)
            for action in copy_actions.values():
                action.set_enabled(False)
            self._populate_tab_copy_host_menu(None)
            return

        is_sftp = tab_info["type"] == "sftp"
        host_config = tab_info.get("config", {})
        # The local-machine tab has no remote host to open an SFTP
        # connection to, or any of Server name/Hostname/IP/user@host to
        # copy.
        is_local = host_config.get("protocol") == "local"
        sftp_action.set_enabled(not is_sftp and not is_local)
        ssh_action.set_enabled(is_sftp)
        detach_action.set_enabled(True)
        has_user = "@" in (host_config.get("host") or "")
        copy_actions["name"].set_enabled(not is_local)
        copy_actions["address"].set_enabled(not is_local)
        copy_actions["userhost"].set_enabled(not is_local and has_user)
        self._populate_tab_copy_host_menu(None if is_local else host_config)

    def _pane_tabview_under_pointer(self, exclude):
        """Which of this window's OTHER live panes the pointer is currently
        over, if any — used by on_tabview_create_window's macOS workaround
        below. Default: none (a DetachedTabWindow only ever has the one
        pane, `exclude` itself, so there's never another one to land in).
        ThongSSHWindow overrides this to check its up-to-4 split panes."""
        return None

    def on_tabview_create_window(self, tabview):
        """Handles Adw.TabView's "create-window" signal — fired when a tab
        is dragged out of its tab strip far enough to tear off into a new
        top-level window. Returning the new window's own TabView is the
        entire contract; Adw.TabView itself performs the actual
        transfer_page() once this returns. The window must already be
        realized/mapped for the drag-out to complete visually, hence the
        present() here (not spelled out in adw_tab_view's own minimal
        signal contract, but needed in practice — an unpresented window
        has no surface for GTK to position/complete the native DnD onto).

        macOS workaround: AdwTabView's native cross-TabView drag hands the
        dragged AdwTabPage across as a GObject-typed content value during
        the underlying native (OS-level) drag round-trip. This project
        already hit the macOS-Quartz-backend version of this exact bug
        once before, with the old hand-rolled Gtk.Notebook panes (see git
        history for _create_pane_notebook / _on_pane_tab_drop — a
        GObject-typed DropTarget payload "didn't survive the drag
        round-trip on macOS's Quartz backend"). AdwTabView's own internal
        DnD hits the same wall: dropping a tab squarely onto one of this
        window's OTHER panes still fails to be recognized as a valid
        target there, and falls through to this "give up, tear off into a
        new window" signal instead. Detect that specific case (pointer
        genuinely over another live pane in the SAME window right now)
        and hand back that pane's own TabView instead of a freshly created
        one — wrapped defensively: this is returning something outside
        this signal's documented contract (a TabView from an *existing*
        window, not "a new window positioned as needed"), so if AdwTabView
        or the target pane ever reacts badly to that in some situation
        this hasn't been tested against, fall back to the always-worked
        plain-detach behavior rather than let a raised exception (or an
        unexpected no-op) take down the drag entirely — see the earlier,
        crash-in-this-exact-spot regression this went through before the
        fallback was added."""
        if sys.platform == "darwin":
            try:
                target = self._pane_tabview_under_pointer(exclude=tabview)
            except Exception:
                logging.exception("_pane_tabview_under_pointer failed — falling back to a new window")
                target = None
            if target is not None:
                return target
        new_window = self.get_application().create_detached_window()
        new_window.present()
        return new_window.tabview

    def _find_tabview_for_page(self, page):
        """Which of this window's live TabViews currently holds `page`, if
        any. Default implementation (correct as-is for DetachedTabWindow,
        which only ever has the one) just checks _get_active_tabview();
        ThongSSHWindow overrides this to search all 4 of its split panes,
        since the target page may not be in the currently-active one."""
        tabview = self._get_active_tabview()
        if tabview is not None and _tabview_has_page(tabview, page):
            return tabview
        return None

    def _open_sftp_tab(self, host_config):
        """Opens a new SFTP tab for host_config in this window's active
        pane — shared by the host tree's on_menu_open_sftp_from_tree
        (window.py), the tab menu's app.open-sftp (open_sftp_for_tab_page
        below), and tab duplication."""
        from .sftp_widget import SftpWidget
        sftp_view = SftpWidget(host_config)
        tabview = self._get_active_tabview()
        page = self._create_tab_page(tabview, sftp_view, "folder-remote-symbolic", host_config['name'])
        sftp_view.grab_focus()
        self.tab_data[page] = {"type": "sftp", "config": host_config, "base_title": host_config['name'], "tag": None}
        self._renumber_tabview(tabview)
        return page

    def open_sftp_for_tab_page(self, page):
        """app.open-sftp trampoline target (app.py) for the TAB context
        menu's "Connect SFTP" item — used only there; the host tree's own
        "Connect SFTP" stays win.open-sftp / on_menu_open_sftp_from_tree
        (window.py), unchanged."""
        if page is None or page not in self.tab_data:
            return
        host_config = self.tab_data[page].get("config")
        if host_config is None:
            return
        self._open_sftp_tab(host_config)

    def copy_host_field_for_tab_page(self, page, field):
        """app.copy-host-{name,address,userhost} trampoline target (app.py)
        for the TAB context menu's "Copy to Clipboard" submenu — app-scoped
        counterpart of window.py's on_menu_copy_host_field (tree-only)."""
        if page is None or page not in self.tab_data:
            return
        host_config = self.tab_data[page].get("config") or {}
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
        # See window.py's on_menu_copy_host_field for why both clipboards
        # (CLIPBOARD and PRIMARY) need to be set, and why new_for_bytes
        # (not a plain Gdk.Clipboard.set()) is used.
        display = self.get_display()
        for clipboard in (display.get_clipboard(), display.get_primary_clipboard()):
            provider = Gdk.ContentProvider.new_for_bytes(
                "text/plain;charset=utf-8", GLib.Bytes.new(text.encode("utf-8"))
            )
            clipboard.set_content(provider)

    def detach_tab_page(self, page):
        """app.tab-detach trampoline target (app.py) for the tab context
        menu's "Detach" item — pops a single tab out into its own new
        DetachedTabWindow. The same underlying operation
        (transfer_page + a freshly created window) that a native drag-out
        performs via Adw.TabView's "create-window" signal (see
        on_tabview_create_window above), just menu-driven instead of a
        drag gesture."""
        if page is None:
            return
        child = page.get_child()
        window = child.get_root() if child is not None else None
        src_tabview = window._find_tabview_for_page(page) if window is not None else None
        if src_tabview is None:
            return
        new_window = self.get_application().create_detached_window()
        src_tabview.transfer_page(page, new_window.tabview, 0)
        new_window.present()

    def rename_tab_page(self, page):
        """app.tab-rename trampoline target (app.py) for the tab context
        menu's "Rename Tab" item — RenameTabDialog either appends a short
        tag in [brackets] after the tab's own name (e.g. "myserver
        [prod-db]", the default) or replaces it outright, to tell apart
        several tabs that would otherwise look identical (the same server
        connected more than once, or the same local directory open in
        more than one tab). Persists in tab_data (not just the displayed
        title), so it survives whatever else re-renders the title later —
        cwd changes, reconnects, position renumbering — and is mirrored
        into tab_data["config"]["name"] (see _render_tab_title), so
        watermarks and Quicky templates pick it up too."""
        if page is None:
            return
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        dialog = RenameTabDialog(self, tab_info.get("tag"), tab_info.get("custom_title"))
        def on_response(result):
            if result is None:
                return  # cancelled
            mode, text = result
            if mode == "reset":
                tab_info["tag"] = None
                tab_info["custom_title"] = None
            elif mode == "tag":
                tab_info["tag"] = text or None
                tab_info["custom_title"] = None
            elif mode == "full":
                tab_info["custom_title"] = text or None
                tab_info["tag"] = None
            self._render_tab_title(self._find_tabview_for_page(page), page)
        dialog.run_async(on_response)

    def rename_active_tab(self):
        """Alt+R and the terminal right-click menu's own "Rename Tab" item
        (win.rename-tab) both mean "the currently active tab" — unlike the
        tab strip's own context menu item (app.tab-rename), which targets
        whichever tab was actually right-clicked, not necessarily the
        active one."""
        tabview = self._get_active_tabview()
        page = tabview.get_selected_page() if tabview is not None else None
        self.rename_tab_page(page)

    def color_tab_page(self, page):
        """Color belongs to the page, so transfers and reconnects retain it."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        dialog = TabColorDialog(self, tab_info.get("tab_color"))

        def on_response(result):
            if result is None or self.tab_data.get(page) is not tab_info:
                return
            self._apply_tab_color(page, result)

        dialog.run_async(on_response)

    def _apply_tab_color(self, page, color):
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Tab color must be #rrggbb")
        tab_info = self.tab_data[page]
        tab_info["tab_color"] = color or None
        child = page.get_child()
        owner = child.get_root() if child is not None else None
        if isinstance(owner, TerminalPaneWindow):
            view = owner._find_tabview_for_page(page)
            if view is not None:
                view._tab_colors.queue_refresh()

    def color_active_tab(self):
        tabview = self._get_active_tabview()
        self.color_tab_page(tabview.get_selected_page() if tabview is not None else None)

    def on_menu_tab_disconnect(self, action, param, page=None):
        """Closes the given tab (or the active one) — app.tab-disconnect /
        the tab menu's "Disconnect" item. For a terminal, this goes
        through the same tabview.close_page() -> on_tabview_close_page
        path as the tab's own native close button (graceful SIGTERM, then
        SIGKILL after a grace period); for SFTP, just removes the tab."""
        if page is None:
            tabview = self._get_active_tabview()
            page = tabview.get_selected_page() if tabview is not None else None
        if page is None:
            return
        tabview = self._find_tabview_for_page(page)
        if tabview is not None:
            tabview.close_page(page)

    def on_menu_tab_reconnect(self, action, param, page=None):
        """Reconnects the given tab (or the active one) without closing it."""
        if page is None:
            tabview = self._get_active_tabview()
            page = tabview.get_selected_page() if tabview is not None else None
        if page is None or page not in self.tab_data:
            return

        tab_info = self.tab_data[page]
        if tab_info["type"] == "terminal":
            logging.debug(f"Reconnecting terminal tab in place for config: {tab_info['config']['name']}")
            if page in self.open_sessions:
                terminal, _old_pid = self.open_sessions[page]
                # Reset terminal state
                terminal.reset(True, True)
                terminal.set_input_enabled(True)
                # Re-run the full session start logic to handle username prompts correctly
                self.start_session(tab_info['config'], existing_page=page)
            else:  # Fallback to old behavior if something is wrong
                self.on_menu_tab_disconnect(None, None, page=page)
                self.start_session(tab_info["config"])

        elif tab_info["type"] == "sftp":
            page_widget = page.get_child()
            if hasattr(page_widget, 'reconnect'):
                page_widget.reconnect()
            else:  # Fallback
                host_config = tab_info["config"]
                self.on_menu_tab_disconnect(None, None, page=page)
                self._open_sftp_tab(host_config)

    def on_menu_tab_duplicate(self, action, param, page=None):
        """Opens a new tab with the same config as the given (or active) tab."""
        if page is None:
            tabview = self._get_active_tabview()
            page = tabview.get_selected_page() if tabview is not None else None
        if page is None or page not in self.tab_data:
            return
        tab_info = self.tab_data[page]
        if tab_info["type"] == "sftp":
            self._open_sftp_tab(tab_info["config"])
        else:  # terminal
            self.start_session(tab_info["config"])

    def on_menu_open_ssh_from_tab(self, action, param, page=None):
        """Opens a terminal session based on the given (or active) SFTP tab's config."""
        if page is None:
            tabview = self._get_active_tabview()
            page = tabview.get_selected_page() if tabview is not None else None
        if page is None or page not in self.tab_data:
            return
        tab_info = self.tab_data[page]
        if tab_info["type"] == "sftp":
            self.start_session(tab_info["config"])

    def on_tabview_page_detached(self, tabview, page, position):
        """No-op by default (a pane in the main window losing a tab to a
        drag-out needs no special handling of its own); DetachedTabWindow
        overrides this to self-close once its one-and-only pane empties
        out."""
        pass

    def on_popover_terminal_closed(self, popover):
        """Gives focus back to the active terminal when the terminal
        content's right-click context menu is closed."""
        def refocus():
            terminal = self.get_active_terminal()
            if terminal:
                terminal.grab_focus()
        GLib.idle_add(refocus)

    def _setup_terminal_pane_actions(self):
        """Registers the window-scoped actions that mean "act on the
        active terminal *of this window*" (so genuinely window-scoped,
        unlike the app-scoped tab-menu actions in app.py) — close-tab,
        copy/paste-clipboard, send-file, find-in-terminal, save-log-tab —
        and builds the terminal-content right-click popover plus the
        native tab-strip context menu model (tab_menu_model/
        tab_copy_host_menu, referencing app.* actions — see app.py).
        Called by both ThongSSHWindow.setup_actions_and_popovers (first,
        before its own host-tree/quicky/tab-context setup) and
        DetachedTabWindow.__init__."""
        action_close_tab = Gio.SimpleAction.new("close-tab", None)
        action_close_tab.connect("activate", self.on_menu_close_tab)
        self.add_action(action_close_tab)

        action_copy = Gio.SimpleAction.new("copy-clipboard", None)
        action_copy.connect("activate", self.on_menu_copy)
        self.add_action(action_copy)

        action_paste = Gio.SimpleAction.new("paste-clipboard", None)
        action_paste.connect("activate", self.on_menu_paste)
        self.add_action(action_paste)

        action_send_file = Gio.SimpleAction.new("send-file", None)
        action_send_file.connect("activate", self.on_menu_send_file)
        self.add_action(action_send_file)

        action_find_in_terminal = Gio.SimpleAction.new("find-in-terminal", None)
        action_find_in_terminal.connect("activate", self.on_menu_find_in_terminal)
        self.add_action(action_find_in_terminal)

        action_rename_tab = Gio.SimpleAction.new("rename-tab", None)
        action_rename_tab.connect("activate", lambda a, p: self.rename_active_tab())
        self.add_action(action_rename_tab)

        action_color_tab = Gio.SimpleAction.new("color-tab", None)
        action_color_tab.connect("activate", lambda a, p: self.color_active_tab())
        self.add_action(action_color_tab)

        # Stateful (checkbox) action — see on_terminal_right_click for how
        # its state/enabled are kept in sync with the right-clicked tab.
        action_save_log_tab = Gio.SimpleAction.new_stateful("save-log-tab", None, GLib.Variant.new_boolean(False))
        action_save_log_tab.connect("activate", self.on_menu_toggle_log_tab)
        self.add_action(action_save_log_tab)

        terminal_menu = Gio.Menu()
        terminal_menu.append(_("Copy"), "win.copy-clipboard")
        terminal_menu.append(_("Paste"), "win.paste-clipboard")
        terminal_menu.append(_("Send File..."), "win.send-file")
        terminal_menu.append(_("Highlight and Find"), "win.find-in-terminal")
        terminal_menu.append(_("Rename Tab"), "win.rename-tab")
        terminal_menu.append(_("Tab Color…"), "win.color-tab")
        terminal_menu.append(_("Save log"), "win.save-log-tab")
        self.popover_terminal = Gtk.PopoverMenu.new_from_model(terminal_menu)
        self.popover_terminal.connect("closed", self.on_popover_terminal_closed)
        self.popover_terminal.set_parent(self)

        self.tab_menu_model = self._build_tab_menu_model()

    def _build_tab_menu_model(self):
        """Builds the native Adw.TabView tab-strip context menu model —
        app-scoped actions (see app.py's "why win.* -> app.* at all"), so
        the SAME model shape works whether it's attached to a main-window
        pane's TabView or a DetachedTabWindow's. Also builds this
        window's own tab_copy_host_menu submenu instance (mutated live per
        right-click by on_tabview_setup_menu/_populate_tab_copy_host_menu)."""
        tab_menu_model = Gio.Menu()
        tab_menu_model.append(_("Disconnect"), "app.tab-disconnect")
        tab_menu_model.append(_("Reconnect"), "app.tab-reconnect")
        tab_menu_model.append(_("Duplicate"), "app.tab-duplicate")
        tab_menu_model.append(_("Detach"), "app.tab-detach")
        tab_menu_model.append(_("Rename Tab"), "app.tab-rename")
        tab_menu_model.append(_("Tab Color…"), "app.tab-color")
        tab_menu_model.append(_("Connect SFTP"), "app.open-sftp")
        tab_menu_model.append(_("Connect SSH"), "app.open-ssh-from-tab")
        self.tab_copy_host_menu = Gio.Menu()
        self._populate_tab_copy_host_menu(None)
        tab_menu_model.append_submenu(_("Copy to Clipboard"), self.tab_copy_host_menu)
        return tab_menu_model

    # --- Generic keybinding / gesture helpers ---
    #
    # These have no host-tree/split/window-chrome dependency of their own,
    # but need to live here (not just on ThongSSHWindow) since base's own
    # _continue_session wires on_terminal_key_pressed/on_terminal_scroll/
    # _on_right_press_guard onto every new terminal regardless of which
    # window created it, and both ThongSSHWindow's and DetachedTabWindow's
    # own on_window_key_pressed use _shortcut_matches/_resolve_latin_letter
    # for the shortcuts they each handle.

    def on_menu_close_tab(self, action, param):
        """Closes the active tab in the active pane."""
        tabview = self._get_active_tabview()
        page = tabview.get_selected_page() if tabview is not None else None
        if page is None:
            return
        tabview.close_page(page)

    def _switch_tab(self, direction):
        """Alt+< / Alt+> — selects the previous (-1) or next (+1) tab
        within the active pane, wrapping around at either end. Adw.TabView
        has its own native Alt+1..9/Alt+0 (switch to tab by position) and
        Ctrl+Page Up/Down (previous/next) shortcuts already; this is an
        additional pair some users specifically asked for, not a
        replacement for those."""
        tabview = self._get_active_tabview()
        if tabview is None:
            return
        n = tabview.get_n_pages()
        if n < 2:
            return
        current = tabview.get_selected_page()
        index = next((i for i in range(n) if tabview.get_nth_page(i) is current), 0)
        tabview.set_selected_page(tabview.get_nth_page((index + direction) % n))

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

    def _resolve_latin_letter(self, keyval, keycode):
        """The Latin a-z letter for this physical key, even when a non-Latin
        layout (Cyrillic, etc.) is the active one.

        Ctrl+<letter> combos — both our own shortcuts and, more
        importantly, the raw control characters a shell/readline expects
        (Ctrl+C, Ctrl+D for EOF/logout, Ctrl+Z, ...) — are conventionally
        about the physical key, not whatever character the active keyboard
        layout happens to map it to. If the current keyval already is a
        Latin letter this is a no-op either way; if it isn't (e.g. the
        active layout produced a Cyrillic letter), this asks GDK for every
        other keyval this same physical key produces across all configured
        layouts/levels and returns the first Latin one it finds."""
        if Gdk.KEY_a <= keyval <= Gdk.KEY_z:
            return chr(keyval)
        if Gdk.KEY_A <= keyval <= Gdk.KEY_Z:
            return chr(keyval).lower()

        display = self.get_display()
        if display is None:
            return None
        success, _keys, keyvals = display.map_keycode(keycode)
        if not success:
            return None
        for kv in keyvals:
            if Gdk.KEY_a <= kv <= Gdk.KEY_z:
                return chr(kv)
            if Gdk.KEY_A <= kv <= Gdk.KEY_Z:
                return chr(kv).lower()
        return None

    def _shortcut_matches(self, settings_key, is_ctrl, is_shift, letter, is_alt=False):
        """Whether the just-pressed combination (already broken down into
        is_ctrl/is_shift/is_alt/the physical key's resolved Latin letter —
        see on_window_key_pressed/on_terminal_key_pressed) matches the
        configurable shortcut stored under settings_key (a Gtk accelerator
        name, e.g. "<Control>w" — see Settings -> General -> Keyboard
        Shortcuts). Re-reads and re-parses the setting on every call
        rather than caching: keypresses are infrequent enough for a plain
        string parse to be a non-issue, and this way a change made in
        Settings (or pulled in by Sync) takes effect on the very next
        keypress with no extra wiring needed to invalidate a cache.

        is_alt defaults to False rather than being read off `modifier`
        unconditionally at every call site — none of the shortcuts that
        predate Alt-based ones (close_tab, copy, paste, ...) ever bind Alt,
        so their callers simply don't pass it, same net effect with less
        churn at each of those call sites."""
        accel = self.settings_manager.get(settings_key)
        if not accel:
            return False
        success, keyval, mods = Gtk.accelerator_parse(accel)
        if not success:
            return False
        want_ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        want_shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        want_alt = bool(mods & Gdk.ModifierType.ALT_MASK)
        want_letter = None
        if Gdk.KEY_a <= keyval <= Gdk.KEY_z:
            want_letter = chr(keyval)
        elif Gdk.KEY_A <= keyval <= Gdk.KEY_Z:
            want_letter = chr(keyval).lower()
        return (bool(is_ctrl) == want_ctrl and bool(is_shift) == want_shift
                and bool(is_alt) == want_alt and letter == want_letter)

    def _keyval_shortcut_matches(self, settings_key, is_ctrl, is_alt, is_shift, keyval, keycode=None, check_shift=True):
        """Raw-keyval counterpart to _shortcut_matches, for shortcuts whose
        key isn't a plain Latin letter or digit — arrows, the backtick
        (`grave`) key, `<`/`>` — where physical-key/layout resolution isn't
        needed since these keyvals don't vary across layouts the way
        letters do. Shared here (not window.py) since DetachedTabWindow
        needs it too (tab_prev/tab_next).

        check_shift=False for symbol keys like `<`/`>` whose own keyval
        already IS the shifted one — GDK delivers the post-shift keyval
        (e.g. "less"), but whether producing it needs Shift physically
        held varies by keyboard layout (US: Shift+comma; some European
        layouts: an unshifted dedicated key). Comparing is_shift against a
        fixed want_shift derived from a hardcoded accelerator string
        would work on one kind of layout and silently fail on the other —
        a real, reported bug (Alt+</Alt+> never fired on this system's
        layout). Arrows/grave don't have this problem (Shift+Up is a
        distinct, real combo from plain Up, both sharing one keyval), so
        they keep the default of actually checking it.

        check_shift=False ALSO switches key matching from the delivered
        keyval to a physical-keycode resolution (needs `keycode`): holding
        Option on macOS does real Unicode composition on punctuation keys
        (Option+Shift+Comma can come out as some unrelated composed
        symbol, not plain "<"), unlike Linux/X11 where Alt is a pure
        modifier bit that never changes which character comma/period
        produce. Comparing the delivered keyval directly against "<"/">"
        would just never match on macOS — the key press falls through
        entirely to the terminal instead (a real, reported bug). Resolving
        by keycode instead asks "is this physical key even *capable* of
        producing '<' at some unmodified level", ignoring whatever the
        actual Option-composed keyval happened to be this time — the same
        keycode-based approach _resolve_latin_letter already uses for
        Ctrl+<letter>, generalized to punctuation."""
        accel = self.settings_manager.get(settings_key)
        if not accel:
            return False
        success, want_keyval, mods = Gtk.accelerator_parse(accel)
        if not success:
            return False
        want_ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        want_alt = bool(mods & Gdk.ModifierType.ALT_MASK)
        shift_ok = (bool(is_shift) == bool(mods & Gdk.ModifierType.SHIFT_MASK)) if check_shift else True
        if check_shift or keycode is None:
            key_ok = keyval == want_keyval
        else:
            key_ok = keyval == want_keyval or self._keycode_produces(keycode, want_keyval)
        return bool(is_ctrl) == want_ctrl and shift_ok and bool(is_alt) == want_alt and key_ok

    def _keycode_produces(self, keycode, target_keyval):
        """Whether the physical key at `keycode` is capable of producing
        `target_keyval` at some shift level/group of the active layout —
        see _keyval_shortcut_matches' check_shift=False branch above."""
        display = self.get_display()
        if display is None:
            return False
        success, _keys, keyvals = display.map_keycode(keycode)
        return success and target_keyval in keyvals

    def _close_all_tabs_in_tabview(self, tabview):
        """Closes (killing any live session, same as the tab's own "x")
        every open tab in one pane — shared by the "close all tabs in this
        pane" shortcut and the split shortcuts' "close others" variant
        (see window.py's _apply_split_mode). The page list is materialized
        up front since close_page shifts every later index as each one is
        removed."""
        if tabview is None:
            return
        for page in [tabview.get_nth_page(i) for i in range(tabview.get_n_pages())]:
            tabview.close_page(page)

    def on_terminal_key_pressed(self, controller, keyval, keycode, modifier):
        """Handles key presses directly on the Vte.Terminal widget."""
        is_ctrl = modifier & Gdk.ModifierType.CONTROL_MASK
        is_shift = modifier & Gdk.ModifierType.SHIFT_MASK
        already_latin = (Gdk.KEY_a <= keyval <= Gdk.KEY_z) or (Gdk.KEY_A <= keyval <= Gdk.KEY_Z)
        letter = self._resolve_latin_letter(keyval, keycode) if is_ctrl else None

        if self._shortcut_matches("shortcuts.close_tab", is_ctrl, is_shift, letter):
            self.on_menu_close_tab(None, None)
            return True  # Event handled, stop propagation

        # Copy/Paste: Vte only binds the classic Shift+Insert/Ctrl+Insert
        # copy-paste shortcuts itself, not the newer Ctrl+Shift+C/V
        # convention these default to, so it has to be wired up explicitly
        # here — both configurable (Settings -> General -> Keyboard
        # Shortcuts), same as close_tab above.
        if self._shortcut_matches("shortcuts.copy", is_ctrl, is_shift, letter):
            self.on_menu_copy(None, None)
            return True
        if self._shortcut_matches("shortcuts.paste", is_ctrl, is_shift, letter):
            self.on_menu_paste(None, None)
            return True

        # Any other Ctrl+<letter>: only step in when the active layout's own
        # keyval *wasn't* already Latin (i.e. only the genuinely-broken
        # case) — when it already was, leave it alone and let Vte's own
        # (already-correct) handling run, so there's no risk of
        # double-sending or subtly differing from it in the common case.
        if is_ctrl and not is_shift and letter and not already_latin:
            terminal = controller.get_widget()
            terminal.feed_child(bytes([ord(letter) - ord('a') + 1]))
            return True

        return False  # Not handled, allow terminal to process

    def on_terminal_scroll(self, controller, dx, dy):
        """Handles Ctrl+Scroll to change font size in the terminal."""
        modifiers = controller.get_current_event_state()
        if not (modifiers & Gdk.ModifierType.CONTROL_MASK):
            return False  # Propagate event if Ctrl is not held

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

        return True  # Event handled, stop propagation

    # --- Terminal content right-click menu + its actions ---

    def on_terminal_right_click(self, gesture, n_press, x, y):
        """Right-click handler for Vte.Terminal. Connected to 'released'."""
        terminal = gesture.get_widget()

        self.lookup_action("copy-clipboard").set_enabled(terminal.get_has_selection())
        self.lookup_action("paste-clipboard").set_enabled(True)

        # "Send File" only makes sense for SSH sessions (SFTP under the
        # hood) — telnet has no equivalent file-transfer sub-protocol.
        page = self._find_tab_widget_for(terminal)
        tab_info = self.tab_data.get(page)
        can_send_file = (
            tab_info is not None
            and tab_info.get("type") == "terminal"
            and tab_info.get("config", {}).get("protocol", "ssh") == "ssh"
        )
        self.lookup_action("send-file").set_enabled(can_send_file)

        is_logging = tab_info is not None and tab_info.get("log_path") is not None
        save_log_action = self.lookup_action("save-log-tab")
        save_log_action.set_state(GLib.Variant.new_boolean(is_logging))
        save_log_action.set_enabled(tab_info is not None)

        translated_x, translated_y = terminal.translate_coordinates(self, x, y)

        rect = Gdk.Rectangle()
        rect.x = int(translated_x)
        rect.y = int(translated_y)
        rect.width, rect.height = 1, 1

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

    def on_menu_send_file(self, action, param):
        """Opens the Send File dialog for the active terminal's remote host."""
        terminal = self.get_active_terminal()
        page = self.get_active_terminal_widget()
        if terminal is None or page is None:
            return
        tab_info = self.tab_data.get(page)
        if not tab_info or tab_info.get("type") != "terminal":
            return
        host_config = tab_info["config"]
        initial_dir = guess_remote_cwd(terminal)
        dialog = SendFileDialog(self, host_config, initial_dir, terminal=terminal)
        dialog.present()

    # --- In-terminal Find ---

    def _build_find_bar(self, overlay, tabview):
        """Builds one pane's own in-terminal find bar as an overlay pinned
        to the top-right of that pane's terminal area, just under its tab
        bar — not a separate window. GTK4 gives clients no way to place a
        top-level window at a specific spot (Wayland treats placement as
        purely the compositor's call), so a real window could never
        reliably land "top-right, under the header bar" the way this
        needs to; a Gtk.Overlay child, by contrast, is just anchored via
        halign/valign and paints above whatever's beneath it.

        One instance per pane (per Adw.TabView), stored in
        self._find_bars[tabview] — each of ThongSSHWindow's up-to-4 split
        panes gets its own independent bar/search state/target terminal,
        and DetachedTabWindow's one pane gets exactly one, the same as
        before this was made per-pane. Non-modal by construction (it's
        just a widget in the same window, not a dialog) — no focus is ever
        stolen from the terminal, and it stays open across tab switches
        within its OWN pane (see _sync_find_target_terminal) until closed
        by hand or reopened. Vte.Terminal owns the actual search state
        (compiled regex, wrap-around) so nothing here is per-tab; each
        bar's "target_terminal" entry just tracks which terminal within
        its pane it's currently acting on.

        Returns the bar's state dict (also kept in self._find_bars)."""
        if not hasattr(self, "_find_bars"):
            self._find_bars = {}

        bar = {"target_terminal": None, "current_index": 0, "match_count": 0}

        widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        widget.add_css_class("card")
        widget.set_margin_top(8)
        widget.set_margin_end(8)
        widget.set_halign(Gtk.Align.END)
        widget.set_valign(Gtk.Align.START)
        widget.set_visible(False)
        bar["widget"] = widget
        bar["css_provider"] = Gtk.CssProvider()
        widget.get_style_context().add_provider(bar["css_provider"], Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        widget.append(box)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.SearchEntry()
        entry.set_hexpand(True)
        entry.set_width_chars(24)
        entry_row.append(entry)
        bar["entry"] = entry

        prev_button = Gtk.Button(icon_name="go-up-symbolic")
        prev_button.set_tooltip_text(_("Previous match"))
        next_button = Gtk.Button(icon_name="go-down-symbolic")
        next_button.set_tooltip_text(_("Next match"))
        close_button = Gtk.Button(icon_name="window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.set_tooltip_text(_("Close"))
        close_button.connect("clicked", lambda b: widget.set_visible(False))
        entry_row.append(prev_button)
        entry_row.append(next_button)
        entry_row.append(close_button)
        box.append(entry_row)

        options_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        case_toggle = Gtk.CheckButton(label=_("Case sensitive"))
        regex_toggle = Gtk.CheckButton(label=_("Regular expression"))
        highlight_toggle = Gtk.CheckButton(label=_("Highlight all"))
        wrap_toggle = Gtk.ToggleButton(icon_name="view-refresh-symbolic")
        wrap_toggle.set_tooltip_text(_("Wrap around"))
        wrap_toggle.set_active(True)
        options_row.append(case_toggle)
        options_row.append(regex_toggle)
        options_row.append(highlight_toggle)
        options_row.append(wrap_toggle)
        bar["case_toggle"] = case_toggle
        bar["regex_toggle"] = regex_toggle
        bar["highlight_toggle"] = highlight_toggle
        bar["wrap_toggle"] = wrap_toggle

        status_label = Gtk.Label(label="")
        status_label.add_css_class("dim-label")
        status_label.set_hexpand(True)
        status_label.set_halign(Gtk.Align.END)
        options_row.append(status_label)
        box.append(options_row)
        bar["status_label"] = status_label

        # Opacity slider for the find bar's OWN card background — not the
        # "Highlight all" terminal tint (that stays a fixed, fairly subtle
        # alpha, see _draw_search_highlights). Starts from Settings ->
        # Interface's configured default (interface.find_bar_opacity, a
        # 20-100 percentage) each time the bar is built; adjusting it here
        # is a per-pane, session-only override of that default, not
        # written back to Settings — mirrors how wrap/case/regex are also
        # plain per-bar toggles with no persistence of their own.
        #
        # Deliberately NOT Gtk.Widget.set_opacity: that fades the whole
        # widget (including its own text/buttons) as one compositing layer
        # against whatever is behind it, and confirmed live/by the user
        # that even at 1.0 the card still read as see-through over a
        # terminal — VTE's own drawing doesn't participate in normal GTK
        # backdrop compositing the way another widget would, so the
        # "opaque" end of that approach was never actually opaque. Painting
        # a real, solid background color with its own alpha via a
        # per-instance Gtk.CssProvider (see _apply_find_bar_opacity) is
        # genuinely opaque at 100% regardless of what's underneath, since
        # it's the bar's background paint itself, not a transparency
        # composited over it.
        opacity_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        opacity_row.append(Gtk.Label(label=_("Opacity")))
        default_opacity = self.settings_manager.get("interface.find_bar_opacity") / 100.0
        opacity_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(value=default_opacity, lower=0.2, upper=1.0, step_increment=0.05),
        )
        opacity_scale.set_hexpand(True)
        opacity_scale.set_draw_value(False)
        opacity_scale.set_tooltip_text(_("Find bar opacity"))
        opacity_row.append(opacity_scale)
        box.append(opacity_row)
        bar["opacity_scale"] = opacity_scale
        # Deferred to "realize": called this early, the widget isn't part
        # of any window's widget tree yet (overlay.add_overlay happens
        # below), so its style context can't resolve a real theme color
        # yet and lookup_color("card_bg_color") silently falls back to
        # GTK's generic default (stark white, confirmed live — the exact
        # "white box" the user saw at 100%, in a dark-themed window).
        # "realize" fires once the widget is actually attached under a
        # real Gtk.Root, by which point the theme/dark-mode is resolvable.
        def _reapply_opacity(*_args, bar=bar):
            self._apply_find_bar_opacity(bar, bar["opacity_scale"].get_value())
        widget.connect("realize", _reapply_opacity)
        # Also re-resolve the card color if the user flips light/dark
        # theme while this bar already exists — the color is baked into a
        # one-shot rgba() at the time we look it up (see
        # _apply_find_bar_opacity), so unlike plain CSS it won't track a
        # live theme change on its own.
        Adw.StyleManager.get_default().connect("notify::dark", _reapply_opacity)

        entry.connect("search-changed", lambda e, b=bar: self._on_find_text_changed(b))
        entry.connect("activate", lambda e, b=bar: self._find_next(b))
        prev_button.connect("clicked", lambda b, bar=bar: self._find_previous(bar))
        next_button.connect("clicked", lambda b, bar=bar: self._find_next(bar))
        case_toggle.connect("toggled", lambda b, bar=bar: self._on_find_text_changed(bar))
        regex_toggle.connect("toggled", lambda b, bar=bar: self._on_find_text_changed(bar))

        highlight_toggle.connect("toggled", lambda b: self._queue_highlight_redraw_all())
        wrap_toggle.connect("toggled", lambda b, bar=bar: self._apply_find_wrap_option(bar))
        opacity_scale.connect(
            "value-changed", lambda s, bar=bar: self._apply_find_bar_opacity(bar, s.get_value())
        )

        overlay.add_overlay(widget)
        self._find_bars[tabview] = bar
        return bar

    def _apply_find_bar_opacity(self, bar, alpha):
        """Repaints a find bar's own background as a solid color at the
        given alpha (0.2-1.0), via the per-instance Gtk.CssProvider set up
        in _build_find_bar — see that method's own comment for why this
        replaces a plain Gtk.Widget.set_opacity call.

        Uses the theme's "window_bg_color", NOT "card_bg_color" — confirmed
        live that the latter resolves to flat white (1, 1, 1) even in dark
        mode. Adwaita's real "card" look isn't one flat named color at all:
        it's window_bg_color with a separate translucent tint/gradient
        layered on top via background-image (see _build_find_bar's own
        comment on needing `background-image: none` for exactly this
        reason), and "card_bg_color" is just the raw tint color, not the
        resolved on-screen appearance — using it directly here was the
        actual cause of the find bar rendering as a plain white box
        regardless of slider position. window_bg_color, by contrast, IS a
        genuine solid color that already tracks light/dark correctly
        (confirmed live against several other named colors), so it's a
        more faithful "base surface" to tint than copying card styling.
        Falls back to a plain gray only if the named color is somehow
        undefined at all, e.g. a non-Adwaita theme."""
        found, rgba = bar["widget"].get_style_context().lookup_color("window_bg_color")
        if not found:
            is_dark = Adw.StyleManager.get_default().get_dark()
            rgba = Gdk.RGBA()
            rgba.parse("#3a3a3a" if is_dark else "#fafafa")
        r, g, b = (round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))
        # background-image: none is required alongside background-color —
        # the theme's own ".card" rule layers a separate translucent
        # gradient/image on top for its elevation effect (confirmed live:
        # without clearing it, a chunk of that leftover translucency still
        # showed through even at alpha=1.0/0.2 passed here); same reset
        # tab_colors.py already needs for the same reason on tab chrome.
        css = f"box {{ background-color: rgba({r}, {g}, {b}, {alpha}); background-image: none; }}"
        bar["css_provider"].load_from_data(css.encode())

    def on_menu_find_in_terminal(self, action, param):
        """Shows (or re-focuses, if already shown) the active pane's find
        bar, targeting its active terminal. Bound to the terminal context
        menu's "Highlight and Find" item and to Ctrl+Shift+F (see Settings
        -> Shortcuts for the binding — deliberately not spelled out in the
        menu label itself, so the two don't drift out of sync)."""
        terminal = self.get_active_terminal()
        if terminal is None:
            return
        bar = self._find_bars.get(self._get_active_tabview())
        if bar is None:
            return
        bar["target_terminal"] = terminal
        # Re-apply whatever's already in the entry to *this* terminal — the
        # bar is shared across every tab within this one pane, so if it's
        # reopened with leftover text from a previous tab, that terminal
        # has never had a regex set on it yet.
        self._on_find_text_changed(bar)

        bar["widget"].set_visible(True)
        bar["entry"].grab_focus()
        bar["entry"].select_region(0, -1)
        self._queue_highlight_redraw_all()

    def _sync_find_target_terminal(self, tabview=None):
        """Keeps a pane's find bar target in sync with whichever terminal
        is currently selected in THAT SAME pane. Needed because a find bar
        no longer hides itself when you switch tabs within its pane (see
        _build_find_bar) — without this it would keep silently searching
        whatever terminal was active when it was opened, no matter which
        tab you'd since switched to. A no-op while that pane's bar is
        hidden or doesn't exist yet; on_menu_find_in_terminal already
        re-resolves the active terminal fresh the next time it's shown.
        tabview=None resolves to the currently active pane."""
        if tabview is None:
            tabview = self._get_active_tabview()
        bar = getattr(self, "_find_bars", {}).get(tabview)
        if bar is None or not bar["widget"].get_visible():
            return
        page = tabview.get_selected_page()
        session = self.open_sessions.get(page)
        terminal = session[0] if session else None
        if terminal is None or terminal is bar["target_terminal"]:
            return
        bar["target_terminal"] = terminal
        self._on_find_text_changed(bar)
        self._queue_highlight_redraw_all()

    def _apply_find_wrap_option(self, bar):
        if bar["target_terminal"] is not None:
            bar["target_terminal"].search_set_wrap_around(bar["wrap_toggle"].get_active())

    def _compile_find_regex(self, bar, pattern):
        """Returns a compiled Vte.Regex for pattern, or False if it's an
        invalid regex (only possible when the regex toggle is on — literal
        text can't fail to compile once escaped).

        Vte.Regex.new_for_search requires the PCRE2_MULTILINE bit to be set
        or Vte refuses the regex outright (confirmed via a runtime check in
        vte_terminal_search_set_regex) — easy to miss since it's not
        documented in the Python bindings. Plain-text (non-regex) search
        escapes the pattern rather than using PCRE2_LITERAL, since that flag
        can't be combined with Vte.REGEX_FLAGS_DEFAULT's other option bits."""
        is_regex = bar["regex_toggle"].get_active()
        text = pattern if is_regex else GLib.regex_escape_string(pattern, -1)
        flags = Vte.REGEX_FLAGS_DEFAULT | _PCRE2_MULTILINE
        if not bar["case_toggle"].get_active():
            flags |= _PCRE2_CASELESS
        try:
            return Vte.Regex.new_for_search(text, -1, flags)
        except GLib.GError:
            return False

    def _compile_find_regex_python(self, bar, pattern):
        """Plain Python re.Pattern counterpart to _compile_find_regex,
        mirroring the exact same case-sensitivity/regex-vs-literal logic —
        that method stays Vte.Regex/PCRE2-based since that's what
        search_find_next/previous actually need; this one exists purely so
        the match COUNT and the "Highlight all" overlay (_draw_search_
        highlights) can locate every occurrence themselves, something
        Vte's own search API has no way to report (confirmed: it can only
        jump to one match at a time, never enumerate or highlight all of
        them — see the "Highlight all" checkbox's own design). Returns None
        for an invalid regex, same cases _compile_find_regex would fail on."""
        is_regex = bar["regex_toggle"].get_active()
        text = pattern if is_regex else re.escape(pattern)
        flags = re.MULTILINE  # matches _compile_find_regex forcing PCRE2_MULTILINE
        if not bar["case_toggle"].get_active():
            flags |= re.IGNORECASE
        try:
            return re.compile(text, flags)
        except re.error:
            return None

    def _recompute_find_match_count(self, bar, terminal, pattern):
        """Total match count across the whole buffer (scrollback included)
        — what "N of M" reports, and what Next/Previous actually cycle
        through via Vte's own wrap-around search. Deliberately the FULL
        buffer, not just the visible screen _draw_search_highlights paints
        — those are two different, intentionally-scoped things (see that
        method's own docstring)."""
        compiled = self._compile_find_regex_python(bar, pattern)
        if compiled is None:
            bar["match_count"] = 0
            return
        text = self._dump_terminal_text(terminal)
        bar["match_count"] = sum(1 for _m in compiled.finditer(text))

    def _set_find_status(self, bar, found):
        """Updates a bar's status label from its current found/count/index
        state — "3 of 17", "Not found", "Invalid pattern", or blank for an
        empty search box."""
        if not bar["entry"].get_text():
            bar["status_label"].set_text("")
        elif bar["entry"].has_css_class("error"):
            bar["status_label"].set_text(_("Invalid pattern"))
        elif not found or bar["match_count"] == 0:
            bar["status_label"].set_text(_("Not found"))
        else:
            bar["status_label"].set_text(
                _("{current} of {total}").format(current=bar["current_index"], total=bar["match_count"])
            )

    def _queue_highlight_redraw_all(self):
        """Nudges every open terminal's highlight overlay to repaint —
        cheap to call broadly (each one's own draw func is a fast no-op
        unless it's actually its own pane's find-bar target with
        "Highlight all" on), so callers don't need to track down which
        specific overlay belongs to the active tab themselves."""
        for page, session in self.open_sessions.items():
            tab_info = self.tab_data.get(page)
            area = tab_info.get("highlight_area") if tab_info else None
            if area is not None:
                area.queue_draw()

    def _on_find_text_changed(self, bar):
        terminal = bar["target_terminal"]
        if terminal is None:
            return

        pattern = bar["entry"].get_text()
        if not pattern:
            terminal.search_set_regex(None, 0)
            bar["entry"].remove_css_class("error")
            bar["match_count"] = 0
            bar["current_index"] = 0
            bar["status_label"].set_text("")
            self._queue_highlight_redraw_all()
            return

        regex = self._compile_find_regex(bar, pattern)
        if regex is False:
            bar["entry"].add_css_class("error")
            bar["match_count"] = 0
            bar["current_index"] = 0
            bar["status_label"].set_text(_("Invalid pattern"))
            terminal.search_set_regex(None, 0)
            self._queue_highlight_redraw_all()
            return

        bar["entry"].remove_css_class("error")
        terminal.search_set_regex(regex, 0)
        self._apply_find_wrap_option(bar)
        self._recompute_find_match_count(bar, terminal, pattern)
        # search_find_next() resumes *after* the end of whatever's currently
        # selected — so as the pattern grows (still matching the same spot),
        # it skips right past that match instead of re-checking it, and the
        # highlight creeps forward one match per keystroke. Clearing the
        # selection first makes every keystroke re-search from the top, so
        # it lands back on the same (nearest) match instead of marching on.
        terminal.unselect_all()
        found = terminal.search_find_next()
        bar["current_index"] = 1 if found else 0
        self._set_find_status(bar, found)
        self._queue_highlight_redraw_all()

    def _find_next(self, bar):
        terminal = bar["target_terminal"]
        if terminal is None or not bar["entry"].get_text():
            return
        found = terminal.search_find_next()
        if found and bar["match_count"]:
            bar["current_index"] = (bar["current_index"] % bar["match_count"]) + 1
        self._set_find_status(bar, found)

    def _find_previous(self, bar):
        terminal = bar["target_terminal"]
        if terminal is None or not bar["entry"].get_text():
            return
        found = terminal.search_find_previous()
        if found and bar["match_count"]:
            bar["current_index"] = bar["current_index"] - 1 if bar["current_index"] > 1 else bar["match_count"]
        self._set_find_status(bar, found)

    def _draw_search_highlights(self, area, cr, width, height, terminal, tabview):
        """Gtk.DrawingArea draw func for "Highlight all" — paints a
        semi-transparent tint over every match CURRENTLY ON SCREEN (like
        `less`'s own `/pattern` search), not the whole scrollback. VTE has
        no API to highlight more than one match at a time on its own
        (confirmed against its public header — only sequential find/
        previous exist), so this is a from-scratch overlay: drawn on a
        separate click-through Gtk.DrawingArea stacked above the terminal
        (see its construction next to the watermark label), one per tab,
        redrawn on "contents-changed"/scroll/search-state changes.

        Deliberately screen-scoped, not full-buffer: computing/painting
        every match across a potentially huge scrollback on every redraw
        would be both wasteful and pointless (nobody can see it anyway) —
        the "N of M" counter (_recompute_find_match_count) is what covers
        the full buffer, as a plain number rather than a render.

        The fill is translucent, not opaque, specifically so it blends
        with whatever VTE itself is already painting underneath — most
        importantly its own native selection highlight on the CURRENT
        match (from search_find_next/previous), which this draws on top
        of rather than replacing. That blend is what makes the current
        match still visually stand out from the other, plain-tinted ones,
        without this needing to separately track which specific match is
        "the current one" itself.

        Positions come from Vte.Terminal.get_text_format(Format.TEXT) — the
        WHOLE-buffer dump, not get_text_range_format(Format.TEXT, first_row,
        0, ...) with an explicit row range, even though the latter looks
        like the more direct fit. Confirmed live: get_text_range_format
        silently mis-addresses rows while VTE's alternate screen buffer is
        active (i.e. whenever a full-screen program like `less`, `vim`,
        `top`, etc. is running) — it returns a blank string for every row,
        even though get_vadjustment()/get_row_count() keep reporting
        correct values and get_text_format() keeps returning the real
        on-screen content. Since there's no scrollback at all while the
        alternate screen is active (confirmed: the vadjustment's upper
        bound equals its page_size in that state), slicing the visible
        window out of the whole-buffer dump ourselves
        (lines[first_row:first_row + num_rows]) sidesteps the broken
        row-range call entirely and works in both modes. Also NOT using
        the richer get_text_range()/get_text() pair that return
        per-character Vte.CharAttributes (which would have made
        row/column lookup exact even across double-width characters):
        confirmed live on the installed VTE (3.91) that the moment
        Python's binding passes the (mandatory, automatic) GArray for
        those methods' attributes out-param, VTE's own internal assertion
        (`attributes == nullptr`) fails and they return None instead of
        text — a real, present-day break in the already-deprecated
        get_text_range/get_text, not a future risk. Splitting the
        whole-buffer dump on "\\n" recovers (row, column) well enough for
        this purpose instead — at the cost of a double-width character
        counting as one column instead of two, a cosmetic inaccuracy only
        for CJK/emoji text, not a correctness issue for the feature
        itself. A match spanning two on-screen rows (wrapped mid-match)
        also won't be found here, since each row is searched
        independently — VTE's own search (used for actual navigation)
        isn't limited this way, only this visual overlay is.

        `tabview` identifies which pane's own find bar/state governs this
        terminal (see _build_find_bar — one bar per pane, not one global
        bar), bound as an extra closure argument on this draw func
        alongside `terminal` itself when the tab was created."""
        bar = self._find_bars.get(tabview)
        if (bar is None or not bar["widget"].get_visible()
                or not bar["highlight_toggle"].get_active()
                or terminal is not bar["target_terminal"]):
            return
        pattern = bar["entry"].get_text()
        if not pattern:
            return
        compiled = self._compile_find_regex_python(bar, pattern)
        if compiled is None:
            return

        vadjustment = terminal.get_vadjustment()
        first_row = int(vadjustment.get_value())
        num_rows = terminal.get_row_count()
        try:
            whole_text = terminal.get_text_format(Vte.Format.TEXT)
        except GLib.GError:
            return
        if not whole_text:
            return
        visible_lines = whole_text.split("\n")[first_row:first_row + num_rows]

        char_width = terminal.get_char_width()
        char_height = terminal.get_char_height()
        cr.set_source_rgba(1, 1, 0, 0.35)

        for row_offset, line in enumerate(visible_lines):
            for match in compiled.finditer(line):
                col0, col1 = match.start(), match.end()
                if col0 == col1:
                    continue
                cr.rectangle(col0 * char_width, row_offset * char_height,
                             (col1 - col0) * char_width, char_height)
                cr.fill()

    # --- Session logging ("Save session log" on a host, or "Save log" from
    # an open tab's right-click menu) ---
    #
    # See window.py's original comment block (preserved in git history) for
    # the full rationale: this polls VTE's own rendered scrollback rather
    # than wrapping the spawned command with `script`, and skips
    # full-screen-TUI-app redraw bursts (log_skip_interactive_screens).

    def on_menu_toggle_log_tab(self, action, param):
        """Activates the terminal context menu's "Save log" checkbox item —
        starts logging if it wasn't running, stops (and closes out the log
        file) if it was."""
        page = self.get_active_terminal_widget()
        if page is None:
            return
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        if tab_info.get("log_path") is not None:
            self._stop_session_logging(page)
            tab_info["log_path"] = None
        else:
            self._start_session_logging(page)
        action.set_state(GLib.Variant.new_boolean(tab_info.get("log_path") is not None))

    def _start_session_logging(self, page):
        tab_info = self.tab_data.get(page)
        if tab_info is None or tab_info.get("log_path") is not None:
            return  # already logging, or not a real tab
        terminal, _pid = self.open_sessions.get(page, (None, None))
        if terminal is None:
            return

        config = tab_info.get("config", {})
        log_path = self._compute_log_path(config.get("host"), config.get("name"))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            initial_text = self._dump_terminal_text(terminal)
            log_file = open(log_path, "w", encoding="utf-8")
            log_file.write(initial_text)
            log_file.flush()
        except (OSError, GLib.GError) as e:
            logging.error(f"Failed to start session log at {log_path}: {e}")
            return

        tab_info["log_path"] = log_path
        tab_info["_log_file"] = log_file
        tab_info["_log_last_text"] = initial_text
        # Reset every time logging (re)starts — see the "Full-screen TUI
        # apps" comment in _tick_session_log below.
        tab_info["_log_redraw_streak"] = 0
        tab_info["_log_suspended"] = False
        tab_info["_log_timeout_id"] = GLib.timeout_add(500, self._tick_session_log, page)

    def _dump_terminal_text(self, terminal):
        """Current full buffer (scrollback + screen) as plain rendered text
        (no escape sequences), with trailing blank lines stripped.

        The strip matters for the tick-to-tick diffing in _tick_session_log:
        VTE's dump always includes the cursor's current (often still-blank)
        row, whose position shifts down as more output arrives. Left in,
        that makes the previous dump a non-prefix of the next one purely
        because of where the blank tail happened to fall — not because
        anything was actually rewritten — which broke the append-only diff.
        Stripping it keeps the comparison anchored to actual printed content
        instead of a moving blank tail."""
        stream = Gio.MemoryOutputStream.new_resizable()
        terminal.write_contents_sync(stream, Vte.WriteFlags.DEFAULT)
        stream.close(None)  # steal_as_bytes() asserts the stream is closed first
        text = bytes(stream.steal_as_bytes().get_data()).decode("utf-8", errors="replace")
        return text.rstrip("\n")

    def _tick_session_log(self, page):
        """Recurring GLib.timeout_add callback — returning False cancels it,
        which doubles as automatic cleanup once the tab closes (tab_data
        stops having an entry for it, or logging was otherwise stopped)."""
        tab_info = self.tab_data.get(page)
        if tab_info is None or tab_info.get("log_path") is None:
            return False
        terminal, _pid = self.open_sessions.get(page, (None, None))
        if terminal is None:
            return False

        try:
            current_text = self._dump_terminal_text(terminal)
        except GLib.GError as e:
            logging.debug(f"Session log poll failed, will retry: {e}")
            return True

        last_text = tab_info.get("_log_last_text", "")
        if current_text == last_text:
            return True

        is_append = current_text.startswith(last_text)
        log_file = tab_info.get("_log_file")

        if not is_append and self.settings_manager.get("terminal.log_skip_interactive_screens"):
            # Two non-append diffs in a row -> treat this as a full-screen
            # TUI app redrawing in place, not a one-off scrollback-overflow
            # burst, and stop logging its screens until a real appended
            # diff shows up again. The first occurrence alone isn't enough
            # to tell the two apart, so it still falls through to the
            # normal "older output lost" handling below.
            streak = tab_info.get("_log_redraw_streak", 0) + 1
            tab_info["_log_redraw_streak"] = streak
            if streak >= 2:
                if not tab_info.get("_log_suspended") and log_file:
                    tab_info["_log_suspended"] = True
                    log_file.write("\n--- (interactive screen redraws not logged — "
                                    "see Settings → Terminal → Logging) ---\n")
                    log_file.flush()
                tab_info["_log_last_text"] = current_text
                return True
        else:
            tab_info["_log_redraw_streak"] = 0
            if tab_info.get("_log_suspended"):
                tab_info["_log_suspended"] = False
                if log_file:
                    log_file.write("--- (resuming log) ---\n")

        if is_append:
            new_part = current_text[len(last_text):]
        else:
            # The common-prefix invariant broke — scrollback evicted
            # content before we got a chance to log it. Can't recover
            # the gap, so just note it and carry on from here.
            new_part = "\n--- (older output lost; scrollback limit reached) ---\n" + current_text
        if log_file and new_part:
            log_file.write(new_part)
            log_file.flush()
        tab_info["_log_last_text"] = current_text
        return True

    def _stop_session_logging(self, page):
        """Closes out any active log bookkeeping for `page`. Safe to call
        even if none was active. Does NOT touch tab_info["log_path"]
        itself — callers that mean to fully clear logging state (as
        opposed to e.g. replacing it on reconnect) should set that
        separately."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        timeout_id = tab_info.pop("_log_timeout_id", None)
        if timeout_id is not None:
            GLib.source_remove(timeout_id)

    def _start_command_watch(self, page, config):
        """Starts polling a terminal tab for its host's Post-connection/
        Regexp Commands (Host dialog -> Commands) — a no-op if the host has
        neither configured. Independent of session logging (same
        GLib.timeout_add(500, ...) + _dump_terminal_text diffing idiom as
        _start_session_logging/_tick_session_log, but its own timer: must
        keep working whether or not logging is on for this tab).

        There's no "connection is actually authenticated" signal anywhere
        in this codebase (_continue_session spawns via spawn_sync, with no
        async completion callback; the only process-lifecycle signal,
        child-exited, fires on death, not readiness) — post-connect
        commands fire on the first tick that sees any output at all, not a
        fixed delay. Documented as a tradeoff in the Host dialog's own note
        label, not hidden: this will also feed an interactive password or
        host-key-confirmation prompt if one appears first."""
        post_connect = [c for c in (config.get("post_connect_commands") or []) if c.strip()]
        regexp_rules = config.get("regexp_commands") or []

        compiled_rules = []
        for rule in regexp_rules:
            pattern, command = rule.get("pattern", ""), rule.get("command", "")
            if not pattern.strip() or not command.strip():
                continue
            try:
                compiled_rules.append((re.compile(pattern), command))
            except re.error as e:
                logging.warning(f"Regexp Commands: skipping invalid pattern {pattern!r}: {e}")

        if not post_connect and not compiled_rules:
            return  # nothing configured for this host — don't bother polling at all

        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        tab_info["_connect_cmds"] = post_connect
        tab_info["_connect_cmds_sent"] = not bool(post_connect)
        tab_info["_compiled_regexp_cmds"] = compiled_rules
        tab_info["_cmdwatch_last_text"] = ""
        tab_info["_cmdwatch_timeout_id"] = GLib.timeout_add(500, self._tick_command_watch, page)

    def _tick_command_watch(self, page):
        """Recurring GLib.timeout_add callback — same shape as
        _tick_session_log (returning False cancels it, doubling as cleanup
        once the tab closes or the process dies)."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return False
        terminal, _pid = self.open_sessions.get(page, (None, None))
        if terminal is None:
            return False

        try:
            current_text = self._dump_terminal_text(terminal)
        except GLib.GError as e:
            logging.debug(f"Command watch poll failed, will retry: {e}")
            return True

        last_text = tab_info.get("_cmdwatch_last_text", "")
        if current_text == last_text:
            return True
        new_part = current_text[len(last_text):] if current_text.startswith(last_text) else current_text
        tab_info["_cmdwatch_last_text"] = current_text

        if not tab_info.get("_connect_cmds_sent"):
            if not current_text:
                return True  # still waiting for the very first output
            tab_info["_connect_cmds_sent"] = True
            host_config = tab_info.get("config", {})
            for cmd in tab_info.get("_connect_cmds", []):
                rendered = self._render_template_text(cmd, host_config)
                terminal.feed_child((rendered + "\n").encode("utf-8"))
            if not tab_info.get("_compiled_regexp_cmds"):
                tab_info.pop("_cmdwatch_timeout_id", None)
                return False  # nothing left to watch for
            return True

        host_config = tab_info.get("config", {})
        for regex, command in tab_info.get("_compiled_regexp_cmds", []):
            if regex.search(new_part):
                rendered = self._render_template_text(command, host_config)
                terminal.feed_child((rendered + "\n").encode("utf-8"))
        return True

    def _stop_command_watch(self, page):
        """Closes out any active Post-connection/Regexp Commands polling
        for `page` — same shape as _stop_session_logging. Safe to call even
        if none was active."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        timeout_id = tab_info.pop("_cmdwatch_timeout_id", None)
        if timeout_id is not None:
            GLib.source_remove(timeout_id)

    def _compute_log_path(self, host_str, name):
        """Where a new session log should be written — directory per the
        client.log_dir -> .config_path -> CONFIG_DIR/logs fallback chain
        (see paths.resolve_log_dir), filename "user@NAME-YYYYMMDD-hh:mm:ss"
        where NAME is the host's config name (the tree's friendly label),
        not its hostname/IP — the username comes off of host_str (the
        already-resolved "[user@]hostname" string, with any
        interactively-prompted username merged in) since that's the only
        place it lives."""
        log_dir = resolve_log_dir(self.settings_manager.get("client.log_dir"))
        if host_str and "@" in host_str:
            username, _sep, _hostname = host_str.partition("@")
            label = f"{username}@{name}" if name else username
        else:
            label = name or host_str or "session"
        label = label.replace("/", "_")
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H:%M:%S")
        return log_dir / f"{label}-{timestamp}"

    # --- Local-terminal cwd tracking ---

    def _dir_short_label(self, path):
        """"~" for $HOME, otherwise just the last path component — the
        display form used for "local: <dir>" tab names."""
        home = os.environ.get("HOME", os.path.expanduser("~"))
        return "~" if path == home else (os.path.basename(path.rstrip("/")) or path)

    def _get_process_cwd(self, pid):
        """Cross-platform "what directory is this process currently in".
        Linux gets this for free via /proc/<pid>/cwd. macOS and the BSDs
        have no /proc, so they fall back to a tool that already ships
        with the OS itself: lsof on macOS (and any BSD that happens to
        have it), procstat(1) on FreeBSD's base system. Returns None if
        the cwd genuinely can't be determined (missing tool, permissions,
        ...) — distinct from "process is gone", which callers check
        separately."""
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass

        def _via_lsof():
            try:
                result = subprocess.run(
                    ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            for line in result.stdout.splitlines():
                if line.startswith("n"):
                    return line[1:]
            return None

        def _via_procstat():
            try:
                result = subprocess.run(
                    ["procstat", "-f", str(pid)],
                    capture_output=True, text=True, timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "cwd":
                    return parts[-1]
            return None

        if platform.system() == "Darwin":
            return _via_lsof()
        if shutil.which("procstat"):
            return _via_procstat()
        if shutil.which("lsof"):
            return _via_lsof()
        return None

    def _start_local_cwd_tracking(self, page):
        """Keeps a local-terminal tab's title (and, if the watermark
        template references $name, its watermark too — see
        _tick_local_cwd) live-updated to its shell's actual current
        directory ("local: ~", "local: .config", ...) by polling the
        shell process's cwd once a second (see _get_process_cwd).
        Local-protocol tabs only: an ssh/telnet tab's pid is the local ssh
        client, whose own cwd never reflects anything happening on the
        remote session. Stops and restarts cleanly on reconnect (see
        _continue_session) so there's never more than one timer running
        per tab."""
        self._stop_local_cwd_tracking(page)
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        tab_info["_cwd_last"] = None
        tab_info["_cwd_timeout_id"] = GLib.timeout_add_seconds(1, self._tick_local_cwd, page)

    def _tick_local_cwd(self, page):
        """Recurring GLib.timeout_add_seconds callback — returning False
        cancels it, doubling as cleanup once the tab closes, its shell
        process dies, or its cwd simply can't be determined on this
        platform (no point polling forever for an answer that will never
        come)."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return False
        session = self.open_sessions.get(page)
        if session is None:
            return False
        _terminal, pid = session
        cwd = self._get_process_cwd(pid)
        if cwd is None:
            try:
                os.kill(pid, 0)
            except OSError:
                return False  # process is gone
            return False  # process alive, but cwd unavailable — nothing will change

        if cwd != tab_info.get("_cwd_last"):
            tab_info["_cwd_last"] = cwd
            new_name = f"local: {self._dir_short_label(cwd)}"
            tab_page = tab_info.get("tab_page")
            if tab_page is not None:
                tab_info["base_title"] = new_name
                # Also keeps config["name"] (what $name in the watermark/
                # Quicky templates reads) and the watermark itself in sync
                # — see _render_tab_title/_effective_display_name. A tag or
                # full rename (tab_data["tag"]/["custom_title"]) still wins
                # over this plain cwd-derived name there, exactly like it
                # already does in the tab strip itself.
                self._render_tab_title(self._find_tabview_for_page(tab_page), tab_page)
        return True

    def _stop_local_cwd_tracking(self, page):
        """Safe to call even if no tracking was active."""
        tab_info = self.tab_data.get(page)
        if tab_info is None:
            return
        timeout_id = tab_info.pop("_cwd_timeout_id", None)
        if timeout_id is not None:
            GLib.source_remove(timeout_id)
        log_file = tab_info.pop("_log_file", None)
        if log_file:
            try:
                log_file.write("\n")
                log_file.close()
            except OSError:
                pass
        tab_info.pop("_log_last_text", None)

    # --- Watermark / color-scheme apply ---

    def _apply_color_scheme_to_terminal(self, terminal, scheme_key=None):
        """Applies a color scheme (or the currently-configured one) to a
        single Vte.Terminal. "default"/an unresolved custom scheme both
        mean "no override" — explicitly reset to VTE's own built-in colors
        rather than silently leaving whatever the terminal already had."""
        if scheme_key is None:
            scheme_key = self.settings_manager.get("terminal.color_scheme")
        colors = get_scheme_colors(scheme_key)
        if not colors:
            terminal.set_colors(None, None, None)
            return

        def parse_color(spec):
            rgba = Gdk.RGBA()
            rgba.parse(spec)
            return rgba

        terminal.set_colors(
            foreground=parse_color(colors["foreground"]),
            background=parse_color(colors["background"]),
            palette=[parse_color(c) for c in colors["palette"]],
        )

    def apply_terminal_color_scheme_to_all(self):
        """Re-applies the current color scheme to every already-open
        terminal — called right after Settings are applied, so a change
        takes effect immediately instead of only affecting the next new
        tab. open_sessions is the shared, app-wide dict (see app.py), so
        this naturally covers every window's terminals, not just this
        one's."""
        scheme_key = self.settings_manager.get("terminal.color_scheme")
        for terminal, _pid in self.open_sessions.values():
            self._apply_color_scheme_to_terminal(terminal, scheme_key)

    def _render_template_text(self, template, host_config):
        """$name/$host/$user substitution — used for plain text (watermark
        labels, inserted Quickies), never a shell command line."""
        if not template:
            return ""
        host_str = host_config.get("host") or ""  # "host" can be present-but-null
        user, _sep, host = host_str.rpartition('@')
        for placeholder, value in {"$name": host_config.get("name", "") or "", "$host": host, "$user": user}.items():
            template = template.replace(placeholder, value)
        return template

    def _update_watermark_for_tab(self, page):
        """Refreshes one terminal tab's watermark label — text, position,
        styling, and visibility — from current settings."""
        tab_info = self.tab_data.get(page)
        if not tab_info or tab_info.get("type") != "terminal":
            return
        label = tab_info.get("watermark_label")
        if label is None:
            return

        if not self.settings_manager.get("interface.watermark_enabled"):
            label.set_visible(False)
            return

        if self.settings_manager.get("interface.watermark_scope") == "active" \
                and page is not self.get_active_terminal_widget():
            label.set_visible(False)
            return

        text = self._render_template_text(
            self.settings_manager.get("interface.watermark_text"), tab_info["config"]
        )
        if not text:
            label.set_visible(False)
            return

        halign, valign = _WATERMARK_ALIGN.get(
            self.settings_manager.get("interface.watermark_position"), (Gtk.Align.CENTER, Gtk.Align.CENTER)
        )
        label.set_halign(halign)
        label.set_valign(valign)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.set_margin_top(12)
        label.set_margin_bottom(12)

        font_size = self.settings_manager.get("interface.watermark_font_size")
        shrink_percent = self.settings_manager.get("interface.watermark_shrink_percent") or 100
        # split_mode is a main-window-only concept (no split panes in a
        # DetachedTabWindow) — getattr guards this method's use in the
        # shared base for a subclass that has none.
        if shrink_percent < 100 and getattr(self, "split_mode", None) is not None:
            font_size = max(1, int(font_size * shrink_percent / 100))

        color, opacity = self._resolve_watermark_color_and_opacity(text)

        rgba = Gdk.RGBA()
        rgba.parse(color)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_size_new(font_size * Pango.SCALE))
        font_family = self.settings_manager.get("interface.watermark_font_family")
        if font_family:
            attrs.insert(Pango.attr_family_new(font_family))
        attrs.insert(Pango.attr_foreground_new(
            int(rgba.red * 65535), int(rgba.green * 65535), int(rgba.blue * 65535)
        ))
        label.set_attributes(attrs)
        label.set_text(text)
        label.set_opacity(opacity / 100)
        label.set_visible(True)

    def _resolve_watermark_color_and_opacity(self, text):
        """Adaptive watermarks: interface.watermark_rules is an ORDERED
        list of {"pattern", "color", "opacity"} — the first (topmost) rule
        whose regex matches the rendered watermark text wins, overriding
        the global watermark_color/watermark_opacity. No match (or no
        rules at all) falls back to the plain global settings. Malformed
        regex in a rule is skipped silently rather than breaking every
        watermark in the app."""
        for rule in self.settings_manager.get("interface.watermark_rules") or []:
            pattern = rule.get("pattern")
            if not pattern:
                continue
            try:
                if re.search(pattern, text):
                    return rule.get("color") or self.settings_manager.get("interface.watermark_color"), \
                        rule.get("opacity") or self.settings_manager.get("interface.watermark_opacity")
            except re.error:
                continue
        return self.settings_manager.get("interface.watermark_color"), self.settings_manager.get("interface.watermark_opacity")

    def apply_watermark_settings_to_all(self):
        """Re-applies watermark settings to every open tab immediately,
        whether it's the toggle button, a Settings change, or the active
        pane/tab/split layout changing which tab(s) should show one.
        open_sessions is shared app-wide (see app.py), so this already
        covers every window's tabs, not just this one's.
        watermark_position_grid is a main-window-only header-bar widget —
        guarded here for the shared base's sake."""
        watermark_position_grid = getattr(self, "watermark_position_grid", None)
        if watermark_position_grid is not None:
            watermark_position_grid.set_selected(self.settings_manager.get("interface.watermark_position"))
        for page in list(self.open_sessions.keys()):
            self._update_watermark_for_tab(page)

    # --- Connection logic ---

    def start_session(self, config, existing_page=None):
        """Starts a terminal session based on the host config (SSH, Telnet, or local)."""
        if config.get("protocol") == "local":
            self._continue_session(config, None, existing_page)
            return

        host_str = config.get('host')
        if not host_str:
            logging.warning("Error: host is not set in the config.")
            return

        # Reconnecting (not opening fresh) to a tab whose session already
        # ended (see on_ssh_process_exited/terminal.close_on_disconnect) —
        # normally config['host'] already has a resolved "user@host" baked
        # in by now (see _continue_session) and this would skip straight
        # to reusing it forever. terminal.reconnect_prompt_username opts
        # into re-asking here too, same as the very first connection.
        is_reconnect_to_dead_session = (
            existing_page is not None
            and self.tab_data.get(existing_page, {}).get("disconnected", False)
        )
        ask_username_again = (
            is_reconnect_to_dead_session
            and self.settings_manager.get("terminal.reconnect_prompt_username")
        )

        # Only ask for a username if it's an SSH connection and either no
        # user is specified yet, or the reconnect setting above asks again.
        if config.get("protocol", "ssh") == "ssh" and ("@" not in host_str or ask_username_again):
            prev_user, _sep, bare_host = host_str.rpartition('@')
            dialog = InputDialog(
                self,
                title=_("Username Required"),
                message=_("Enter username for {host_str}").format(host_str=bare_host),
                default_text=prev_user,
            )
            # Run asynchronously to not block the UI
            dialog.run_async(lambda username: self._continue_session(config, username, existing_page))
        else:
            self._continue_session(config, None, existing_page)

    def _continue_session(self, config, username_from_prompt, existing_page=None):
        """Second part of the logic, called AFTER getting the username."""

        protocol = config.get("protocol", "ssh")

        # If "Cancel" was pressed in the dialog for an SSH connection that needs a username
        if protocol == "ssh" and username_from_prompt is None and "@" not in config.get('host'):
            logging.info("Connection canceled (no username provided).")
            return

        host_str = config.get('host')
        if username_from_prompt:
            # rpartition, not a plain prepend — config['host'] may already
            # be "olduser@host" (a reconnect re-prompting per
            # terminal.reconnect_prompt_username, see start_session), and
            # prepending onto that unconditionally would produce
            # "newuser@olduser@host" instead of replacing it.
            _old_user, _sep, bare_host = host_str.rpartition('@')
            host_str = f"{username_from_prompt}@{bare_host}"

        # Captured before the telnet branch below strips the user@ part back
        # off — this is what actually gets used to auth this session, so
        # it's what tab_data should remember (e.g. for "Send File" later),
        # not the original config which may have had no username at all.
        resolved_host_str = host_str

        cmd = []
        password = None
        # Only set for the sshpass branch below (SSHPASS env var, "-e") —
        # every other case spawns with an unmodified environment.
        extra_env = {}

        if protocol == "ssh":
            # Check for a password in the keyring
            password = self.keyring.load_password(config.get("name"))

            # Build the SSH command
            if password and "@" in host_str:
                # Use sshpass if a password is set. "-e" (read the
                # password from the SSHPASS env var), not "-p <password>"
                # — an argv element is visible to any local user for the
                # whole life of the process via `ps aux`/`/proc/<pid>/
                # cmdline`; SSHPASS itself is still readable via
                # /proc/<pid>/environ, but that's a deliberate, targeted
                # read by someone with the same access level as reading
                # this process's own memory, not a passive `ps aux` glance.
                # The actual env var is set below, alongside the rest of
                # the spawned process's environment (see spawn_sync's envv).
                sshpass_path = self.settings_manager.get("client.sshpass_path")
                cmd = [sshpass_path, "-e", self.settings_manager.get("client.ssh_path")]
                # StrictHostKeyChecking=no + UserKnownHostsFile=/dev/null
                # used to be added here "because sshpass can't handle host
                # key prompts" — but that's only true for a HOST KEY
                # CHANGE/unknown-host confirmation prompt, which sshpass
                # was never asked to answer anyway (it only intercepts the
                # password prompt); a *known* host authenticates with no
                # such prompt at all. Dropping both: an already-known host
                # works exactly the same, and a genuinely new/changed host
                # key now correctly stops and asks — in this same
                # interactive terminal, since VTE's own pty is what's
                # actually connected here — instead of silently accepting
                # it, which is exactly the case that should ask.
                extra_env["SSHPASS"] = password
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
            # Build the Telnet command
            cmd = [self.settings_manager.get("client.telnet_path")]
            # Telnet usually takes host and port as separate arguments
            if "@" in host_str:
                host_str = host_str.split("@", 1)[1]  # Telnet doesn't use user@host format
            cmd.append(host_str)
            if config.get('port'):
                cmd.append(str(config['port']))

        elif protocol == "local":
            # No remote host at all — just the user's own login shell.
            cmd = [os.environ.get("SHELL", "/bin/bash")]

        else:
            logging.error(f"Unknown protocol: {protocol}")
            return

        logging.debug(f"Assembled command: {' '.join(cmd)}")

        # ✨ Log command to file in config directory
        # No password-masking needed here: sshpass now gets the password
        # via "-e"/the SSHPASS env var (see above), never as a "cmd" argv
        # element, so there's nothing sensitive in "cmd" to mask.
        try:
            log_file_path = CONFIG_DIR / "session_commands.log"
            with open(log_file_path, "a", encoding="utf-8") as f:
                timestamp = datetime.datetime.now().isoformat()
                f.write(f"[{timestamp}] {' '.join(cmd)}\n")
        except Exception as e:
            logging.error(f"Failed to write to command log file: {e}")

        # --- Terminal Launch ---
        try:
            # If we are reconnecting, reuse the existing terminal. Otherwise, create a new one.
            if existing_page and existing_page in self.open_sessions:
                terminal, old_pid = self.open_sessions[existing_page]
                logging.debug(f"Reusing existing terminal widget. Old PID: {old_pid}")
            else:
                terminal = Vte.Terminal()

            scrollback = self.settings_manager.get("terminal.scrollback_lines")
            font_str = self.settings_manager.get("terminal.font")

            terminal.set_scrollback_lines(scrollback)
            terminal.set_font(Pango.FontDescription.from_string(font_str))
            self._apply_color_scheme_to_terminal(terminal)

            # THONGSSH_RUNNING_FROM_APPIMAGE is only ever set by our own
            # build-appimage.sh's AppRun — absent for every other way this
            # app runs, so the wrapped-cmd branch is a guaranteed no-op
            # outside the AppImage; everything below it is the exact,
            # untouched original behavior for every other way this app
            # runs (a git checkout, .deb/.rpm install, macOS .app, ...).
            if os.environ.get("THONGSSH_RUNNING_FROM_APPIMAGE"):
                # Handles restoring the AppImage-polluted vars AND
                # exporting extra_env (SSHPASS) itself, via a real shell
                # unset/export — see its own docstring for why envv isn't
                # used for either purpose here.
                cmd = _wrap_cmd_for_appimage_env(cmd, extra_env)
                envv = []
            else:
                # An empty envv here (the common case — extra_env is only
                # ever populated for the sshpass/SSHPASS case above) makes
                # VTE spawn with an unmodified inherited environment, same
                # as before; confirmed live (TERM/PATH/HOME all still
                # present) rather than assumed, since passing a NON-empty
                # envv fully *replaces* the child's environment instead of
                # extending it — so the sshpass case has to build the full
                # list itself, not just the one extra SSHPASS var.
                envv = [f"{k}={v}" for k, v in os.environ.items()] if extra_env else []
                for key, value in extra_env.items():
                    envv.append(f"{key}={value}")

            success, pid = terminal.spawn_sync(
                Vte.PtyFlags.DEFAULT,
                config.get('cwd') or os.environ['HOME'],
                cmd, envv, GLib.SpawnFlags.DEFAULT,  # Use DEFAULT instead of DO_NOT_REAP_CHILD
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
            if not existing_page:
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
                # Direct child, not wrapped in the Overlay below — Vte.Terminal
                # implements Gtk.Scrollable, which is what lets a
                # Gtk.ScrolledWindow delegate straight to its own scroll
                # adjustments and show a real scrollbar. Putting the Overlay
                # in between used to make the ScrolledWindow's actual child a
                # plain (non-Scrollable) container, so GTK4 silently fell
                # back to auto-wrapping it in a Gtk.Viewport instead — that
                # scrolls the *Overlay's own natural size*, not the
                # terminal's real scrollback, which is why the scrollbar
                # itself disappeared even though the terminal's own
                # keyboard-driven scrolling (Shift+PageUp, etc.) kept working.
                scrolled_term.set_child(terminal)

                # The watermark label sits in its own Gtk.Overlay *above* the
                # ScrolledWindow — not inside it — so it stays fixed in the
                # viewport instead of scrolling away with the terminal's own
                # content. can_target(False) makes it click-through: pointer
                # events fall straight through to the terminal underneath.
                watermark_label = Gtk.Label()
                watermark_label.set_can_target(False)
                watermark_label.add_css_class("terminal-watermark")
                term_overlay = Gtk.Overlay()
                term_overlay.set_child(scrolled_term)
                term_overlay.add_overlay(watermark_label)

                # term_overlay (not scrolled_term) is the actual tab
                # content widget from here on — appended into whichever
                # TabView is currently active, becoming the AdwTabPage
                # that's the real key in open_sessions/tab_data.
                tabview = self._get_active_tabview()

                # Same click-through overlay trick as the watermark label
                # above, for the "Highlight all" find feature — see
                # _draw_search_highlights. One per terminal (not the
                # per-pane find bar's own overlay) so it's always sized/
                # positioned to exactly match THIS terminal; `tabview` is
                # bound alongside `terminal` so the draw func knows which
                # pane's find bar/state governs it.
                highlight_area = Gtk.DrawingArea()
                highlight_area.set_can_target(False)
                highlight_area.set_draw_func(self._draw_search_highlights, terminal, tabview)
                term_overlay.add_overlay(highlight_area)
                terminal.connect("contents-changed", lambda t, a=highlight_area: a.queue_draw())
                terminal.get_vadjustment().connect(
                    "value-changed", lambda a, area=highlight_area: area.queue_draw()
                )

                page = self._create_tab_page(tabview, term_overlay, "utilities-terminal-symbolic", config['name'])
                terminal.grab_focus()

                resolved_config = dict(config)
                resolved_config['host'] = resolved_host_str

                self.open_sessions[page] = (terminal, pid)
                self.tab_data[page] = {
                    "type": "terminal", "config": resolved_config, "log_path": None,
                    "watermark_label": watermark_label, "highlight_area": highlight_area, "tab_page": page,
                    "disconnected": False, "base_title": config['name'], "tag": None,
                    # The host's own configured default (Host dialog -> Tab
                    # Color) — just the SEED value here; the tab's own
                    # right-click "Tab Color…" (_apply_tab_color) overwrites
                    # this same key later and naturally wins, no separate
                    # "is this an override" bookkeeping needed.
                    "tab_color": config.get("tab_color"),
                }
                self._renumber_tabview(tabview)
                self._start_command_watch(page, config)
                terminal.connect("child-exited", self.on_ssh_process_exited, page)
                # Per-host "save_log" (set on the host's own edit page) wins
                # when present; terminal.auto_save_log covers everything
                # else — including the "local" entry and "+"-button tabs,
                # which have no per-host page of their own to carry it.
                if config.get("save_log", False) or self.settings_manager.get("terminal.auto_save_log"):
                    self._start_session_logging(page)
                if protocol == "local":
                    self._start_local_cwd_tracking(page)
                self.apply_watermark_settings_to_all()
            else:  # This is a reconnect, just update the PID
                self.open_sessions[existing_page] = (terminal, pid)
                self._stop_session_logging(existing_page)
                tab_info = self.tab_data.get(existing_page)
                if tab_info is not None:
                    tab_info["log_path"] = None
                    tab_info["config"]["host"] = resolved_host_str
                    # Alive again — clear the disconnected indicator applied
                    # in on_ssh_process_exited, if any.
                    tab_info["disconnected"] = False
                    self._set_tab_page_disconnected(existing_page, False)
                # Per-host "save_log" (set on the host's own edit page) wins
                # when present; terminal.auto_save_log covers everything
                # else — including the "local" entry and "+"-button tabs,
                # which have no per-host page of their own to carry it.
                if config.get("save_log", False) or self.settings_manager.get("terminal.auto_save_log"):
                    self._start_session_logging(existing_page)
                if protocol == "local":
                    self._start_local_cwd_tracking(existing_page)
                self._start_command_watch(existing_page, config)
                terminal.grab_focus()
                self.apply_watermark_settings_to_all()

        except Exception as e:
            logging.critical(f"Critical error spawning VTE: {e}")
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=_("SSH Launch Error"),
                body=_("Failed to start the process. Make sure /usr/bin/ssh exists.\n\nError: {error}").format(error=e),
            )
            dialog.add_response("ok", _("OK"))
            dialog.present()

    def _set_tab_page_disconnected(self, page, disconnected):
        """Toggles the "disconnected" visual cue on a terminal tab's page —
        its session has ended but the tab itself was kept open (see
        terminal.close_on_disconnect). Adw.TabPage.set_title is a plain
        string, no Pango markup/attributes (confirmed against libadwaita's
        own adw-tab.ui template — the title is an AdwFadingLabel bound
        straight to AdwTabPage.title with no use-markup), so real
        strikethrough isn't available through the title property itself.
        _render_tab_title applies Unicode combining strikethrough (U+0336)
        over the composed (numbered + tagged) title instead — just flip
        the flag here and re-render, no separate clean-title stash needed
        (tab_data["base_title"]/["tag"] already ARE that clean source).
        The indicator icon stays too as a second, distinct cue (a glyph,
        not a replacement for the strikethrough)."""
        if page is None:
            return
        tab_info = self.tab_data.get(page)
        if tab_info is not None:
            tab_info["disconnected"] = disconnected
        if disconnected:
            page.set_indicator_icon(Gio.ThemedIcon.new("network-offline-symbolic"))
            page.set_indicator_tooltip(_("Disconnected"))
        else:
            page.set_indicator_icon(None)
            page.set_indicator_tooltip("")
        self._render_tab_title(self._find_tabview_for_page(page), page)

    def on_ssh_process_exited(self, terminal, status, page):
        """Handles the 'child-exited' signal from Vte.Terminal.

        Resolves the CURRENT owning window via page.get_child().get_root()
        rather than relying on `self` — this handler is connected once, at
        session-start time, to whichever window created the tab; if that
        tab is later dragged out into (or back from) a DetachedTabWindow,
        `self` here would otherwise still be the ORIGINAL window, which no
        longer necessarily has this page in any of its own TabViews."""
        logging.debug(f"VTE child process exited with status {status} for page {page}.")

        if page not in self.tab_data:
            return  # already closed via on_tabview_close_page

        if self.settings_manager.get("terminal.close_on_disconnect"):
            child = page.get_child()
            window = child.get_root() if child is not None else None
            tabview = window._find_tabview_for_page(page) if window is not None else None
            if tabview is not None:
                tabview.close_page(page)
            else:
                # Page's current window/tabview couldn't be resolved
                # (shouldn't normally happen) — tear the bookkeeping down
                # directly so it's not silently leaked.
                self._stop_session_logging(page)
                self._stop_local_cwd_tracking(page)
                self._stop_command_watch(page)
                self.open_sessions.pop(page, None)
                self.tab_data.pop(page, None)
        else:
            # Keep the tab open and show a message
            exit_message = _("\n\n--- Session finished with exit code: {status} ---").format(status=status)
            terminal.feed_child(exit_message.encode('utf-8'))
            # Make the terminal read-only
            terminal.set_input_enabled(False)
            tab_info = self.tab_data[page]
            tab_info["disconnected"] = True
            self._set_tab_page_disconnected(page, True)
            # Dead pid — no more /proc/<pid>/cwd to poll until a reconnect
            # starts a fresh timer (see _continue_session).
            self._stop_local_cwd_tracking(page)
