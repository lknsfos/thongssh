import json
import os
import shutil
import logging
from pathlib import Path

# --- Global Constants ---
CONFIG_DIR = Path.home() / ".config" / "thongssh"
CONFIG_FILE     = CONFIG_DIR / "hosts.json"
CONFIG_BACKUP_1 = CONFIG_DIR / "hosts.json.bak1"
CONFIG_BACKUP_2 = CONFIG_DIR / "hosts.json.bak2"
CONFIG_BACKUP_3 = CONFIG_DIR / "hosts.json.bak3"
CONFIG_TEMP     = CONFIG_DIR / "hosts.json.tmp"
from gi.repository import GLib

# Default template for a new config file
DEFAULT_CONFIG_DATA = {
    "type": "group",
    "name": "Root",
    "children": [
        {
            "type": "host",
            "config": {
                "name": "Example Host",
                "host": "user@example.com",
                "port": None,
                "key_path": None,
                "compat_old_systems": False,
                "ssh_options": None,
                "forward_x": False,
                "forward_agent": False
            }
        }
    ]
}

# Template for migration. Add ALL new fields here!
HOST_CONFIG_TEMPLATE = {
    "protocol": "ssh",
    "name": None,
    "host": None,
    "port": None,
    "key_path": None,
    "compat_old_systems": False,
    "ssh_options": None,
    "forward_x": False,
    "forward_agent": False,
    "telnet_binary": False,
    "telnet_local_echo": False,
}


def _recursive_migrate(node):
    needs_save = False
    if node.get("type") == "host":
        if "config" not in node:
            node["config"] = {}
            needs_save = True
        for key, default_value in HOST_CONFIG_TEMPLATE.items():
            if key not in node["config"]:
                node["config"][key] = default_value
                needs_save = True
    elif node.get("type") == "group":
        # ✨ Add 'expanded' field for groups if it doesn't exist
        if "expanded" not in node:
            node["expanded"] = True  # Groups are expanded by default
            needs_save = True

        if "children" in node:
            migrated_children = []
            for child in node["children"]:
                migrated_child, child_needs_save = _recursive_migrate(child)
                migrated_children.append(migrated_child)
                if child_needs_save:
                    needs_save = True
            node["children"] = migrated_children
    return node, needs_save


def _try_load_json(path):
    """Returns parsed JSON from path, or None if missing/corrupted."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_and_migrate_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Try main file, then backups in order
    candidates = [CONFIG_FILE, CONFIG_BACKUP_1, CONFIG_BACKUP_2, CONFIG_BACKUP_3]
    data = None
    loaded_from = None
    for candidate in candidates:
        result = _try_load_json(candidate)
        if result is not None:
            data = result
            loaded_from = candidate
            break

    if data is None:
        logging.info("No valid config found, creating default...")
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG_DATA, f, indent=4)
        return DEFAULT_CONFIG_DATA

    if loaded_from != CONFIG_FILE:
        logging.warning(f"Main config missing/corrupted; restored from {loaded_from.name}")
        try:
            shutil.copy2(loaded_from, CONFIG_FILE)
        except Exception as e:
            logging.error(f"Failed to restore config from backup: {e}")

    needs_save_after_wrap = False
    if isinstance(data, list):
        logging.info("Old config format (list) detected, wrapping in Root...")
        data = {"type": "group", "name": "Root", "children": data}
        needs_save_after_wrap = True

    migrated_data, needs_migration_save = _recursive_migrate(data)

    if needs_save_after_wrap or needs_migration_save:
        logging.info("Updating config file (migration)...")
        save_config(migrated_data)

    return migrated_data


def save_config(config_data):
    """Saves config atomically with 3-file backup rotation.

    Write order ensures the main file is never absent or half-written:
    1. Write to .tmp (crash here leaves main file intact)
    2. Rotate bak2→bak3, bak1→bak2 (os.replace is atomic)
    3. Copy main→bak1 (copy so main is still readable during this step)
    4. os.replace .tmp→main (atomic rename)
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_TEMP, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        if CONFIG_BACKUP_2.exists():
            os.replace(CONFIG_BACKUP_2, CONFIG_BACKUP_3)
        if CONFIG_BACKUP_1.exists():
            os.replace(CONFIG_BACKUP_1, CONFIG_BACKUP_2)
        if CONFIG_FILE.exists():
            shutil.copy2(CONFIG_FILE, CONFIG_BACKUP_1)

        os.replace(CONFIG_TEMP, CONFIG_FILE)
    except Exception as e:
        logging.error(f"Failed to save config: {e}")