import json
import logging
from pathlib import Path
import shutil
from .colors import COLOR_SCHEMES

# Placeholder for future internationalization (i18n)
_ = lambda s: s

CONFIG_DIR = Path.home() / ".config" / "thongssh"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "terminal.scrollback_lines": 8192,
    "terminal.font": "Monospace 10",
    "terminal.color_scheme": "default",
    "client.ssh_path": shutil.which("ssh") or "/usr/bin/ssh",
    "client.telnet_path": shutil.which("telnet") or "/usr/bin/telnet",
    "client.sshpass_path": shutil.which("sshpass") or "/usr/bin/sshpass",
    "user_commands": [],
    "sftp.local_default_path": "~/Downloads",
    "sftp.local_default_sort_column": "name", # name, size, date
    "sftp.local_default_sort_direction": "asc", # asc, desc
    "sftp.remote_default_sort_column": "name", # name, size, date
    "sftp.remote_default_sort_direction": "asc", # asc, desc
    "terminal.close_on_disconnect": True, # ✨ NEW: Whether to close tab on disconnect
    "interface.icon": "thongssh", # "thongssh" (Safe) or "thongssh_orig" (Original)
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
            # Обновляем только существующие ключи, чтобы не потерять новые при обновлении
            for key in self.settings:
                if key in loaded_settings:
                    self.settings[key] = loaded_settings[key]
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Failed to load settings: {e}. Using defaults.")
            # В случае ошибки, можно создать бэкап
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
