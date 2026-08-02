import json
import logging
import sys
from pathlib import Path

# Default config storage location — used unless overridden below.
_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "thongssh"

# Cache storage — regenerable/lower-stakes data that doesn't belong next to
# hosts.json/settings.json. Currently just AI chat history (see
# ai_chat_store.py): it lives on this machine only (no provider is asked to
# retain it, and several — CLI tools, some self-hosted endpoints — couldn't
# even if asked), so somewhere has to. Deliberately not affected by the
# .config_path pointer (that's about separating *configuration* between
# checkouts, not chat history) and not platform-specific like the config
# dir isn't either — same plain, unconditional layout on every OS.
CACHE_DIR = Path.home() / ".cache" / "thongssh"


def _app_root_dir():
    """Where the pointer file itself lives. Deliberately NOT inside
    _DEFAULT_CONFIG_DIR: the whole point of the pointer is to let a git
    checkout keep its configs separate from ~/.config/thongssh (e.g. a
    personal checkout vs. a work one on the same machine) — if the pointer
    lived inside the one shared ~/.config/thongssh, every checkout would
    read the same override and there'd be nothing to separate them. Instead
    it sits next to the app itself, so each checkout/install carries its
    own independent pointer."""
    if hasattr(sys, '_MEIPASS'):
        # Frozen (PyInstaller) build: next to the actual executable, not the
        # temp extraction dir — that's recreated every run and a user could
        # never find or edit it there.
        return Path(sys.executable).resolve().parent
    # Dev / git checkout: the project root, one level up from this
    # package's own directory (thongssh_gtk/).
    return Path(__file__).resolve().parent.parent


CONFIG_PATH_FILE = _app_root_dir() / ".config_path"


def _default_pointer_content():
    # Written with a literal "~" (expanded on read) rather than the
    # fully-resolved path to *this* machine's home directory — this file is
    # meant to be committed to the repo as a discoverable, ready-to-edit
    # template, so it has to resolve correctly for whoever else clones it
    # too, not just whoever's machine first generated it.
    default_dir = "~/.config/thongssh"
    return {
        "config_path": default_dir,
        # Used by resolve_log_dir() as the 2nd fallback tier, below the
        # client.log_dir setting and above CONFIG_DIR/logs. A "logs"
        # subdirectory, not the bare config dir — session logs shouldn't
        # sit directly alongside hosts.json/settings.json.
        "terminal_logs_path": default_dir + "/logs",
    }


def _read_pointer():
    """Reads .config_path once. Returns the parsed dict, or None if it's
    missing/unreadable (a packaged install's app dir is normally read-only,
    so failing to even create it there is expected and silent, not an
    error)."""
    if not CONFIG_PATH_FILE.exists():
        try:
            content = _default_pointer_content()
            with open(CONFIG_PATH_FILE, 'w', encoding='utf-8') as f:
                json.dump(content, f, indent=4)
            return content
        except OSError as e:
            logging.debug(f"Not creating {CONFIG_PATH_FILE} (likely a read-only/packaged install): {e}")
            return None

    try:
        with open(CONFIG_PATH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"Failed to read {CONFIG_PATH_FILE}: {e}. Using defaults.")
        return None


_POINTER = _read_pointer()


def _resolve_config_dir():
    """Where hosts.json/settings.json/etc. actually live. Normally that's
    _DEFAULT_CONFIG_DIR, unless .config_path exists (next to the app, see
    _app_root_dir) and points elsewhere. The pointer file is never
    overwritten once it exists, so an existing customization is always
    respected."""
    if _POINTER is None:
        return _DEFAULT_CONFIG_DIR

    configured = (_POINTER.get("config_path") or "").strip()
    if not configured:
        return _DEFAULT_CONFIG_DIR

    resolved = Path(configured).expanduser()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logging.error(f"Configured config_path '{resolved}' isn't usable ({e}); falling back to default.")
        return _DEFAULT_CONFIG_DIR

    if resolved != _DEFAULT_CONFIG_DIR:
        logging.info(f"Using redirected config directory: {resolved}")
    return resolved


CONFIG_DIR = _resolve_config_dir()


def resolve_log_dir(explicit_dir=None):
    """Fallback chain for where terminal session logs get stored:
    1. explicit_dir — the client.log_dir setting, if the caller passed a
       non-empty value (SettingsManager isn't imported here to avoid a
       needless coupling; the caller hands in whatever it already has).
    2. terminal_logs_path from the .config_path pointer file, if set.
    3. CONFIG_DIR/logs — a subdirectory, not hosts.json/settings.json's own
       directory directly.
    Always returns a Path that exists (falling back a tier further on any
    OSError, e.g. an unwritable configured directory)."""
    default_log_dir = CONFIG_DIR / "logs"

    candidate = (explicit_dir or "").strip()
    if not candidate and _POINTER is not None:
        candidate = (_POINTER.get("terminal_logs_path") or "").strip()

    resolved = Path(candidate).expanduser() if candidate else default_log_dir
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    except OSError as e:
        if resolved == default_log_dir:
            raise  # the default itself must already be usable at this point
        logging.error(f"Log directory '{resolved}' isn't usable ({e}); falling back to {default_log_dir}")
        default_log_dir.mkdir(parents=True, exist_ok=True)
        return default_log_dir
