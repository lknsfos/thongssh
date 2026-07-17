import os
import json
import logging

from .constants import APP_ID
from .settings import CONFIG_DIR

# The 'keyring' package picks the native backend per OS at runtime:
# macOS Keychain, Windows Credential Locker, Linux/BSD Secret Service or
# KWallet. Unlike libsecret via GObject Introspection, it has no GTK/DBus
# hard dependency and degrades gracefully when no backend is found.
try:
    import keyring as _keyring
    HAS_KEYRING = True
except ImportError:
    _keyring = None
    HAS_KEYRING = False
    logging.warning("Keyring: 'keyring' package not installed; using local encrypted fallback only.")

try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logging.warning("Keyring: 'cryptography' package not installed; fallback password storage is disabled.")

_FALLBACK_KEY_FILE = CONFIG_DIR / ".secret.key"
_FALLBACK_STORE_FILE = CONFIG_DIR / "secrets.enc.json"


class KeyringManager:
    """
    Stores, retrieves, and deletes per-host passwords.

    Tries the OS-native keyring first. If none is available (headless
    Linux/FreeBSD without a keyring daemon, or the 'keyring' package
    missing), falls back to a locally encrypted file so password
    features keep working and the app never crashes because of this.
    """

    def save_password(self, host_name, password):
        if not host_name or not password:
            logging.warning("Keyring: Attempted to save password with empty host_name or password.")
            return

        if HAS_KEYRING:
            try:
                _keyring.set_password(APP_ID, host_name, password)
                self._fallback_clear(host_name)
                logging.info(f"Keyring: Password for '{host_name}' saved via OS keyring.")
                return
            except Exception as e:
                logging.warning(f"Keyring: OS backend unavailable ({e}); using local encrypted fallback.")

        self._fallback_save(host_name, password)

    def load_password(self, host_name):
        if not host_name:
            return None

        if HAS_KEYRING:
            try:
                password = _keyring.get_password(APP_ID, host_name)
                if password is not None:
                    return password
            except Exception as e:
                logging.warning(f"Keyring: OS backend read failed ({e}); checking local encrypted fallback.")

        return self._fallback_load(host_name)

    def clear_password(self, host_name):
        if not host_name:
            return

        if HAS_KEYRING:
            try:
                _keyring.delete_password(APP_ID, host_name)
                logging.info(f"Keyring: Password for '{host_name}' cleared from OS keyring.")
            except Exception as e:
                logging.debug(f"Keyring: OS backend clear skipped ({e}).")

        self._fallback_clear(host_name)

    # --- Local encrypted fallback, used when no OS keyring backend is available ---

    def _fallback_fernet(self):
        if not HAS_CRYPTO:
            return None
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if _FALLBACK_KEY_FILE.exists():
                key = _FALLBACK_KEY_FILE.read_bytes()
            else:
                key = Fernet.generate_key()
                _FALLBACK_KEY_FILE.write_bytes(key)
                try:
                    os.chmod(_FALLBACK_KEY_FILE, 0o600)
                except OSError:
                    pass
            return Fernet(key)
        except Exception as e:
            logging.error(f"Keyring: could not initialize local fallback encryption: {e}")
            return None

    def _fallback_read_store(self):
        if not _FALLBACK_STORE_FILE.exists():
            return {}
        try:
            with open(_FALLBACK_STORE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Keyring: could not read local fallback store: {e}")
            return {}

    def _fallback_write_store(self, data):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(_FALLBACK_STORE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            try:
                os.chmod(_FALLBACK_STORE_FILE, 0o600)
            except OSError:
                pass
        except IOError as e:
            logging.error(f"Keyring: could not write local fallback store: {e}")

    def _fallback_save(self, host_name, password):
        fernet = self._fallback_fernet()
        if fernet is None:
            logging.error(f"Keyring: no storage backend available; password for '{host_name}' was NOT saved.")
            return
        token = fernet.encrypt(password.encode('utf-8')).decode('ascii')
        data = self._fallback_read_store()
        data[host_name] = token
        self._fallback_write_store(data)
        logging.info(f"Keyring: Password for '{host_name}' saved via local encrypted fallback.")

    def _fallback_load(self, host_name):
        fernet = self._fallback_fernet()
        if fernet is None:
            return None
        data = self._fallback_read_store()
        token = data.get(host_name)
        if not token:
            return None
        try:
            return fernet.decrypt(token.encode('ascii')).decode('utf-8')
        except (InvalidToken, ValueError) as e:
            logging.error(f"Keyring: could not decrypt stored password for '{host_name}': {e}")
            return None

    def _fallback_clear(self, host_name):
        data = self._fallback_read_store()
        if host_name in data:
            del data[host_name]
            self._fallback_write_store(data)
