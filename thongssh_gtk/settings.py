# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

import json
import logging
import sys
from pathlib import Path
import shutil
from .colors import COLOR_SCHEMES
from .paths import CONFIG_DIR

from .i18n import _

SETTINGS_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "terminal.scrollback_lines": 8192,
    "terminal.font": "Monospace 10",
    "terminal.color_scheme": "default",
    "terminal.custom_scheme_base": "default", # which built-in template's dropdown selection/palette last seeded the custom colors — only meaningful while terminal.color_scheme == "custom"
    "client.ssh_path": shutil.which("ssh") or "/usr/bin/ssh",
    "client.telnet_path": shutil.which("telnet") or "/usr/bin/telnet",
    "client.sshpass_path": shutil.which("sshpass") or "/usr/bin/sshpass",
    "client.log_dir": "", # empty = fall back to .config_path's terminal_logs_path, then CONFIG_DIR (see paths.resolve_log_dir)
    "user_commands": [],
    "sftp.local_default_path": "~/Downloads",
    "sftp.local_default_sort_column": "name", # name, size, date
    "sftp.local_default_sort_direction": "asc", # asc, desc
    "sftp.remote_default_sort_column": "name", # name, size, date
    "sftp.remote_default_sort_direction": "asc", # asc, desc
    "terminal.close_on_disconnect": True, # ✨ NEW: Whether to close tab on disconnect
    "terminal.reconnect_prompt_username": False, # Only relevant when the above is off — ask again instead of reusing the last username when reconnecting to a disconnected tab
    "terminal.inherit_cwd_for_new_local_tab": True, # Whether the "+" new-local-terminal button starts in the current tab's working directory instead of always $HOME
    "terminal.auto_save_log": False, # Whether every new terminal connection (host or local) starts with session logging already on, instead of needing the per-host switch or the terminal's own "Save log" menu item by hand
    "terminal.log_skip_interactive_screens": True, # Whether full-screen TUI redraws (vim, mc, tmux, htop, less, ...) are detected and left out of the session log instead of dumping the whole screen on every keystroke — see _tick_session_log's heuristic in window.py
    "interface.theme": "system", # "system", "light", or "dark" — the app's own libadwaita theme (Adw.StyleManager), not to be confused with terminal.color_scheme (the terminal's own text/background palette)
    "interface.icon": "thongssh", # "thongssh" (Safe) or "thongssh_orig" (Original)
    "interface.language": "system", # "system" (follow the OS/locale, the normal gettext default) or a code from i18n.LANGUAGES — takes effect on next launch, see i18n.py
    "interface.tree_row_striping": False,
    "interface.tabbar_height": 26, # px, target height of the tab strip (Adw.TabBar) — see window.py's setup_css, everything in the "tabbar ..." CSS block scales off this
    "interface.find_bar_opacity": 90, # percent (20-100) — default opacity of the in-terminal find bar's own card background (not the "Highlight all" terminal tint, which is a fixed alpha); each find bar's own slider (see TerminalPaneWindow._build_find_bar) starts here but can be adjusted per-pane for that session without changing this default
    "interface.debug_mode": False, # Verbose debug logging to the console; off by default
    "interface.host_search_position": "bottom", # "top" or "bottom" — where the host-tree search bar sits
    # Gtk accelerator names (Gtk.accelerator_parse/_name understand them
    # directly, e.g. "<Control>w") for every keyboard shortcut this app
    # used to hardcode — several collide with standard shell keybindings
    # (Ctrl+W deletes the last word in bash/zsh's own line editing, for
    # one), hence configurable rather than fixed. See window.py's
    # _shortcut_matches.
    "shortcuts.close_tab": "<Control><Shift>w", # plain Ctrl+W deletes the last word in bash/zsh's own (readline) line editing
    "shortcuts.focus_search": "<Control>f",
    "shortcuts.find_in_terminal": "<Control><Shift>f",
    "shortcuts.copy": "<Control><Shift>c",
    "shortcuts.paste": "<Control><Shift>v",
    "shortcuts.toggle_side_panel": "<Control>grave", # Alt+` was the original choice, but that's GNOME's own window-switcher shortcut on some setups — Ctrl+` doesn't collide
    "shortcuts.batch_command": "<Alt>b",
    "shortcuts.detach_tab": "<Alt>d",
    "shortcuts.attach_tab": "<Alt>a", # only acts inside a detached tab window — see DetachedTabWindow.on_window_key_pressed
    "shortcuts.rename_tab": "<Alt>r",
    "shortcuts.tab_prev": "<Alt>less", # additional to Adw.TabView's own native Ctrl+Page Up/Down and Alt+1..9/Alt+0
    "shortcuts.tab_next": "<Alt>greater",
    # Split-layout shortcuts. The plain ones (Alt+Shift+1..4) match the
    # split buttons exactly — merging tabs from panes being removed into
    # the pane that survives, same as clicking the button by hand. The
    # Alt+Super+1..4 variants close those tabs instead of relocating them
    # (split_close_4 has nothing to close — going to 4 panes only ever
    # adds empty ones — but is still its own bindable shortcut for a
    # consistent group). See window.py's _apply_split_mode. Plain Alt+1..4
    # and Ctrl+Alt+1..4 were the original choices; both were reported to
    # collide (Alt+1..4 with an existing tab-switch-by-position binding,
    # Ctrl+Alt+1..4 with the quicky_paste_N shortcuts just below, since
    # is_alt wasn't being checked for those yet either — now fixed, see
    # _quicky_shortcut_matches).
    "shortcuts.split_1": "<Alt><Shift>1", # single pane
    "shortcuts.split_2": "<Alt><Shift>2", # vertical (2 panes, left/right)
    "shortcuts.split_3": "<Alt><Shift>3", # horizontal (2 panes, top/bottom)
    "shortcuts.split_4": "<Alt><Shift>4", # grid (4 panes)
    "shortcuts.split_close_1": "<Alt><Super>1",
    "shortcuts.split_close_2": "<Alt><Super>2",
    "shortcuts.split_close_3": "<Alt><Super>3",
    "shortcuts.split_close_4": "<Alt><Super>4",
    "shortcuts.focus_pane_up": "<Alt>Up",
    "shortcuts.focus_pane_down": "<Alt>Down",
    "shortcuts.focus_pane_left": "<Alt>Left",
    "shortcuts.focus_pane_right": "<Alt>Right",
    "shortcuts.close_div": "<Control><Alt>q", # closes every tab in the active pane; in a detached tab window, closes the window itself
    # Quick-access bindings for the first 10 Quickies, by position (see the
    # Quickies panel) — "paste" inserts the snippet without running it
    # (same as the panel's own Send ▶ button), "run" inserts and executes
    # immediately (same as Send and Run ⏩). Slot 10 uses the "0" key, same
    # convention as most terminal emulators' Alt+0-9 tab switching.
    "shortcuts.quicky_paste_1": "<Control>1",
    "shortcuts.quicky_paste_2": "<Control>2",
    "shortcuts.quicky_paste_3": "<Control>3",
    "shortcuts.quicky_paste_4": "<Control>4",
    "shortcuts.quicky_paste_5": "<Control>5",
    "shortcuts.quicky_paste_6": "<Control>6",
    "shortcuts.quicky_paste_7": "<Control>7",
    "shortcuts.quicky_paste_8": "<Control>8",
    "shortcuts.quicky_paste_9": "<Control>9",
    "shortcuts.quicky_paste_10": "<Control>0",
    "shortcuts.quicky_run_1": "<Control><Shift>1",
    "shortcuts.quicky_run_2": "<Control><Shift>2",
    "shortcuts.quicky_run_3": "<Control><Shift>3",
    "shortcuts.quicky_run_4": "<Control><Shift>4",
    "shortcuts.quicky_run_5": "<Control><Shift>5",
    "shortcuts.quicky_run_6": "<Control><Shift>6",
    "shortcuts.quicky_run_7": "<Control><Shift>7",
    "shortcuts.quicky_run_8": "<Control><Shift>8",
    "shortcuts.quicky_run_9": "<Control><Shift>9",
    "shortcuts.quicky_run_10": "<Control><Shift>0",
    "interface.watermark_enabled": False, # mirrored by the header toggle button, not a Settings-page switch
    "interface.watermark_text": "$user@$host", # see constants.py's _prepare_command note: $name, $host, $user
    "interface.watermark_position": "center", # one of constants.WATERMARK_POSITIONS' ids
    "interface.watermark_font_size": 24,
    "interface.watermark_font_family": "Sans", # deliberately excluded from Sync — see settings_sync.py's TERMINAL_SETTINGS_KEYS, same reasoning as terminal.font (a font on one machine often just isn't installed on another)
    "interface.watermark_color": "#ffffff",
    "interface.watermark_opacity": 15, # percent, 1-100
    "interface.watermark_scope": "active", # "active" (focused terminal only) or "all" (every open pane)
    "interface.watermark_shrink_percent": 100, # 100 = off (no shrink); 90/80/.../10 = shrink to that % of the base size while any split layout is active
    "interface.watermark_rules": [], # [{"pattern": regex_str, "color": "#rrggbb", "opacity": int(1-100)}, ...], ordered — first (topmost) match against the rendered watermark text wins, overriding watermark_color/watermark_opacity above; no match falls back to those global defaults
    "quickies.enabled": False, # mirrored by the header toggle button, not a Settings-page-only switch
    "quickies.position": "below", # "above" or "below" the host tree, within the left panel
    "quickies.items": [], # [{"name": str, "text": str}, ...] — inserted (not executed) into the active terminal
    "quickies.search_position": "bottom", # "top" or "bottom" — where the Quickies search box sits, relative to the snippet list
    "quickies.show_command_preview": True, # Whether each Quicky's command text shows as a second line under its name in the panel
    "ai.system_prompt": (
        "You are a read-only analysis assistant for a terminal session. You have no shell, "
        "tool, or network access of your own — never attempt to run, execute, connect to, "
        "or reproduce anything, whether locally or on any remote host, real or hypothetical. "
        "Only analyze the terminal output, commands, or context the user includes in their "
        "message; if you need more information, ask the user to run a command and paste the "
        "result back instead of trying to obtain it yourself. Be concise and minimal: no long "
        "explanations or preambles. Reply in the same language the question was asked in."
    ), # shared system/initial prompt, applies to every provider — dialogs.py's "reset to default" button reads this same DEFAULT_SETTINGS entry.
    # The old wording ("you are connected to a remote terminal session... only the
    # connected remote host is relevant") read as a literal task description to the
    # Claude Code CLI specifically (constants.py's CLI_STANDARD_PROVIDERS "claude"
    # template hands this straight to --append-system-prompt on a real agentic CLI
    # with its own bash/tool-use loop) — it would go try to locate/reach "the
    # connected remote host" itself instead of just answering, hanging for the
    # full request timeout. The API providers (ai_providers.py) never had this
    # problem since they only ever receive it as an inert prompt string with no
    # tool-use capability behind it.
    "ai.disabled": False, # master switch — when True, no header-bar buttons, no CLI PATH probing, no keyring reads, no requests of any kind
    "ai.active_provider": "", # last active provider id ("claude", "custom:<uuid>", ...), or "" if never used
    "ai.provider_models": {}, # {provider_id: model_string} — only holds entries the user overrode from default
    "ai.custom_providers": [], # [{"id": uuid, "name": str, "base_url": str, "has_key": bool}] — never the raw key
    "ai.request_timeout_seconds": 120, # generous default — local/self-hosted models on modest hardware can be slow; shared by API and CLI providers alike
    "cli.commands": {}, # {provider_id: command_template} — overrides for the standard CLI tools (claude/codex)
    "cli.custom_tools": [], # [{"id": uuid, "name": str, "command": str}] — user-added local CLI tools
    "cli.provider_models": {}, # {provider_id: model_string} — empty/absent means "no --model flag at all", not an empty one
    "sync.enabled": False, # master switch — mirrors the header sync button's visibility, not just a Settings-page toggle
    "sync.folder": "", # any plain directory — a Dropbox/iCloud/local-network folder, or just a local path; the app never talks to a cloud API directly
    "sync.interval_seconds": 300, # enforced minimum of 60 both in the Settings SpinRow and defensively wherever the timer is (re)started
    "sync.sync_hosts": True,
    "sync.sync_quickies": True,
    "sync.sync_ai_chats": True,
    "sync.sync_user_commands": True,
    "sync.sync_general": True,
    "sync.sync_shortcuts": True, # separate from sync_general — a keybinding chosen for one OS/keyboard layout (e.g. Mac) is often deliberately not what you want elsewhere
    "sync.sync_terminal": True, # color scheme (incl. custom_color_scheme.json) + watermark settings
    "sync.last_sync_at": 0, # epoch seconds; 0 = never synced yet
    "sync.last_sync_error": "", # empty = last sync attempt was clean
}

if sys.platform == "darwin":
    # No arrow key combination reaches GTK at all on macOS, with or
    # without modifiers — confirmed two different ways: a CAPTURE-phase
    # window-level key controller (which reliably sees every other
    # Alt-combo) logs zero output for Option+Arrow, and ShortcutPicker's
    # own window-level capture (install_shortcut_capture, widgets.py)
    # still can't record a bare arrow press either. That first, narrower
    # theory (Option+Arrow specifically claimed by Cocoa's default text
    # word/paragraph-navigation key bindings — the same interpretKeyEvents:
    # machinery that makes Option-key Unicode composition work in the
    # first place, see i18n.py/tab_window_base.py for the other bugs it
    # caused) doesn't actually explain a PLAIN, unmodified arrow also
    # going unseen, so the real interception must sit even earlier than
    # that — before GDK generates a key event at all, not just before our
    # own widgets see one. No amount of GTK-level controller/phase
    # wrangling can reach a key event that's never generated to begin
    # with. Arrow keys are simply unusable for custom shortcuts on macOS
    # in this app; these defaults use the classic vim h/j/k/l directional
    # letters instead, to sidestep the problem entirely, rather than a
    # Control+Alt+Arrow variant that (also confirmed) is just as
    # unrecordable. Deliberately NOT e/i/u/n/` (grave) — those are the
    # standard US Mac layout's dead-key prefixes (´ˆ¨~`, e.g. Option+i
    # starts a circumflex, waiting for a vowel to combine with), so Option
    # held with any of THOSE doesn't produce a plain letter keyval either,
    # same underlying trap as the arrow keys just in a narrower, letter-
    # only form. h/j/k/l aren't in that set. This is exactly the kind of
    # per-OS keybinding difference sync.sync_shortcuts (above) is already
    # meant to keep from clobbering a Linux machine's plain Alt+Arrow, or
    # vice versa.
    DEFAULT_SETTINGS["shortcuts.focus_pane_up"] = "<Alt>k"
    DEFAULT_SETTINGS["shortcuts.focus_pane_down"] = "<Alt>j"
    DEFAULT_SETTINGS["shortcuts.focus_pane_left"] = "<Alt>h"
    DEFAULT_SETTINGS["shortcuts.focus_pane_right"] = "<Alt>l"

class SettingsManager:
    def __init__(self):
        self.settings = DEFAULT_SETTINGS.copy()
        self.load()

    def load(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not SETTINGS_FILE.exists():
            self.save()
            return

        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
            # Only update existing keys, so new defaults added by an app update aren't lost
            for key in self.settings:
                if key in loaded_settings:
                    self.settings[key] = loaded_settings[key]
            self._restore_null_settings()
            self._migrate_macos_focus_pane_shortcuts(loaded_settings)
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Failed to load settings: {e}. Using defaults.")
            if SETTINGS_FILE.exists():
                SETTINGS_FILE.rename(f"{SETTINGS_FILE}.bak")

    def _restore_null_settings(self):
        """Replaces any setting that ended up `null` with its real default —
        a real, reported crash: `interface.find_bar_opacity` (a percentage
        int consumed as `.../100.0` in tab_window_base.py's find bar setup)
        turned up `None` in a real settings.json, taking the whole app down
        at startup (`TypeError: unsupported operand type(s) for /:
        'NoneType' and 'float'`) before a single window could ever open —
        no settings page, no way to fix it from inside the app at all. How
        it got there isn't fully pinned down (interface.find_bar_opacity is
        also one of sync.sync_terminal's synced keys — settings_sync.py —
        so a merge against an older machine that pre-dates this setting is
        one real candidate), but regardless of cause, no DEFAULT_SETTINGS
        entry is ever legitimately None itself (confirmed: none of them
        are), so a stored null is never a deliberate value to preserve —
        always a sign this exact thing happened, for whichever key. Runs
        for every key, not just this one, since the same class of
        corruption could in principle hit any of them, and a crash this
        early (before the main window, before Settings is reachable) is
        unrecoverable without editing the JSON file by hand otherwise."""
        changed = False
        for key, default in DEFAULT_SETTINGS.items():
            if self.settings.get(key) is None and default is not None:
                self.settings[key] = default
                changed = True
        if changed:
            self.save()

    def _migrate_macos_focus_pane_shortcuts(self, loaded_settings):
        """A settings.json saved before the current h/j/k/l macOS default
        (see DEFAULT_SETTINGS' darwin block above) already has an older,
        since-discovered-broken value baked in from its very first save —
        the "only update existing keys" load loop above just faithfully
        copies that stale value back in, so the new platform default never
        gets a chance to apply on its own. Two prior defaults existed
        before this one (both turned out to never reach GTK on macOS at
        all — plain arrows, then Control+Alt+Arrow), hence a list, not a
        single value. Only touches a value that's *exactly* one of the
        old hardcoded defaults, on the assumption that an exact match
        means "never customized" rather than "user deliberately chose
        this" — a real customization is left alone either way. Linux/
        Windows are unaffected: DEFAULT_SETTINGS never had a darwin-only
        override for these keys there, so none of old_defaults below is
        ever darwin's actual current default off this platform and the
        comparison always misses."""
        if sys.platform != "darwin":
            return
        old_defaults = {
            "shortcuts.focus_pane_up": ("<Alt>Up", "<Control><Alt>Up"),
            "shortcuts.focus_pane_down": ("<Alt>Down", "<Control><Alt>Down"),
            "shortcuts.focus_pane_left": ("<Alt>Left", "<Control><Alt>Left"),
            "shortcuts.focus_pane_right": ("<Alt>Right", "<Control><Alt>Right"),
        }
        changed = False
        for key, candidates in old_defaults.items():
            # DEFAULT_SETTINGS[key] is guaranteed to already be the new
            # h/j/k/l value here (the darwin block above patches it in at
            # import time, before any SettingsManager exists), so this
            # can't accidentally "migrate" a value that's already current.
            if loaded_settings.get(key) in candidates:
                self.settings[key] = DEFAULT_SETTINGS[key]
                changed = True
        if changed:
            self.save()

    def save(self):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4)
        except IOError as e:
            logging.error(f"Failed to save settings: {e}")

    def get(self, key):
        return self.settings.get(key)

    def set(self, key, value):
        self.settings[key] = value
