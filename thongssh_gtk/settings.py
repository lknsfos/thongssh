# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

import json
import logging
from pathlib import Path
import shutil
from .colors import COLOR_SCHEMES
from .paths import CONFIG_DIR

# Placeholder for future internationalization (i18n)
_ = lambda s: s

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
    "interface.icon": "thongssh", # "thongssh" (Safe) or "thongssh_orig" (Original)
    "interface.tree_row_striping": False,
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
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Failed to load settings: {e}. Using defaults.")
            if SETTINGS_FILE.exists():
                SETTINGS_FILE.rename(f"{SETTINGS_FILE}.bak")

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
