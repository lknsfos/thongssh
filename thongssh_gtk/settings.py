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
    "interface.icon": "thongssh", # "thongssh" (Safe) or "thongssh_orig" (Original)
    "interface.tree_row_striping": False,
    "interface.debug_mode": False, # Verbose debug logging to the console; off by default
    "interface.host_search_position": "bottom", # "top" or "bottom" — where the host-tree search bar sits
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
