"""Settings/hosts/quickies/AI-chat sync via a plain shared folder (Dropbox,
iCloud, a local network share, or just another directory on disk) — the app
never talks to any cloud API directly, it only reads/writes files inside
whatever folder the user points it at in Settings -> Sync.

Three-way merge (base/local/remote), same idea git/Dropbox/Syncthing use:
  - base   = this machine's own record of "what the shared file looked like
             last time I successfully synced" (sync_state.json, local-only,
             never written to the shared folder).
  - local  = current real state (hosts.json, settings.json keys, etc.).
  - remote = the shared sync file's current content.

This machine-local "base" is what lets the merge tell "remote doesn't have
this item because it was deleted elsewhere" apart from "remote never had
it" — without it, deletions could never safely propagate and unrelated
new-on-each-side items would look identical to "someone deleted this".

Conflict rule (same item changed differently on both sides, confirmed with
the user): the newer *sync file* wins, decided once per sync pass by
comparing the shared file's own "version" timestamp against this machine's
last recorded `last_synced_version` — not a per-item timestamp (most of
this data, hosts/quickies/commands/settings, has none).

AI chats are the one category with a real per-item id + updated_at
already on disk (see ai_chat_store.py) — they sync as individual files
under `<shared_folder>/ai_chats/<uuid>.json`, not embedded in the single
hashed blob below, so a new chat message doesn't require rehashing every
host and setting too. Everything else lives in one file, `thongssh_sync.json`.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from . import ai_chat_store
from . import config as hosts_config
from . import colors as colors_module
from .paths import CONFIG_DIR

_ = lambda s: s

SYNC_FILE_NAME = "thongssh_sync.json"
SYNC_STATE_FILE = CONFIG_DIR / "sync_state.json"

# Every host field except key_path — that one is always machine-specific
# and is never read from base/remote, only ever preserved from local (see
# merge_host_config below). Derived from config.py's own template so it
# never drifts out of sync with the real schema.
HOST_FIELDS_TO_SYNC = [f for f in hosts_config.HOST_CONFIG_TEMPLATE if f != "key_path"]

TERMINAL_SETTINGS_KEYS = [
    "terminal.scrollback_lines", "terminal.font", "terminal.color_scheme", "terminal.custom_scheme_base",
    "interface.watermark_enabled", "interface.watermark_text", "interface.watermark_position",
    "interface.watermark_font_size", "interface.watermark_color", "interface.watermark_opacity",
    "interface.watermark_scope", "interface.watermark_shrink_in_splits", "interface.watermark_rules",
]
GENERAL_SETTINGS_KEYS = [
    "interface.icon", "interface.tree_row_striping", "interface.debug_mode", "interface.host_search_position",
]

_EMPTY_ROOT = {"type": "group", "name": "Root", "children": []}


class SyncResult:
    def __init__(self, ok, error=None, changed_categories=None, new_config_data=None):
        self.ok = ok
        self.error = error
        self.changed_categories = changed_categories or set()
        # Only set when "hosts" is in changed_categories — the caller
        # (window.py) should assign this over its own self.config_data and
        # repopulate the tree, rather than re-reading hosts.json itself.
        self.new_config_data = new_config_data

    def __repr__(self):
        return f"SyncResult(ok={self.ok}, error={self.error!r}, changed={self.changed_categories})"


# --- Small, self-contained JSON helpers (same atomic tmp+replace idiom
# already used by config.save_config and window._save_window_state) ---

def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def compute_hash(payload):
    """sha256 hex over canonical (sorted-key, compact) JSON — payload must
    NOT include a "hash" key itself."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_and_verify_sync_file(sync_file):
    """Returns (payload_dict_or_None, error_or_None). payload is None (no
    error) if the file simply doesn't exist yet (first-ever sync). A hash
    mismatch is a real error — the file is either mid-write by a cloud
    client or corrupted, and must never be read as a merge source."""
    if not sync_file.exists():
        return None, None
    raw = _load_json(sync_file, default=None)
    if raw is None:
        return None, _("Sync file is unreadable/corrupted JSON.")
    stored_hash = raw.get("hash")
    body = {k: v for k, v in raw.items() if k != "hash"}
    if stored_hash != compute_hash(body):
        return None, _("Sync file failed its integrity check (hash mismatch) — skipping this pass.")
    return raw, None


# --- Three-way merge primitives ---

_MISSING = object()


def merge_scalar(base, local, remote, remote_wins):
    """Returns the resolved value for one scalar setting/field."""
    if local == remote:
        return local
    if base is _MISSING:
        # No prior record at all (brand-new key, or first-ever sync) —
        # treat as "both sides independently have a value": a real
        # conflict, resolved the same way as any other conflict.
        return remote if remote_wins else local
    if local == base and remote != base:
        return remote  # only remote changed
    if remote == base and local != base:
        return local  # only local changed
    return remote if remote_wins else local  # both changed, to different values


def merge_list(base_list, local_list, remote_list, key_fn, remote_wins):
    """Merges lists of dicts (quickies items, user commands) identified by
    key_fn(item) — usually its "name". Deletions propagate both ways;
    brand-new items on either side are kept; edits to the same item on
    both sides are a conflict, resolved by remote_wins."""
    base_map = {key_fn(n): n for n in (base_list or [])}
    local_map = {key_fn(n): n for n in (local_list or [])}
    remote_map = {key_fn(n): n for n in (remote_list or [])}

    ordered_keys = list(local_map.keys())
    for k in remote_map:
        if k not in local_map and k not in ordered_keys:
            ordered_keys.append(k)

    result = []
    for key in ordered_keys:
        in_base, in_local, in_remote = key in base_map, key in local_map, key in remote_map
        if in_local and in_remote:
            local_item, remote_item = local_map[key], remote_map[key]
            if local_item == remote_item:
                result.append(local_item)
                continue
            base_item = base_map.get(key, _MISSING)
            if base_item != _MISSING and local_item == base_item:
                result.append(remote_item)  # only remote changed
            elif base_item != _MISSING and remote_item == base_item:
                result.append(local_item)  # only local changed
            else:
                result.append(remote_item if remote_wins else local_item)  # conflict
        elif in_local and not in_remote:
            if in_base:
                continue  # deleted remotely -> drop locally too
            result.append(local_map[key])  # new local-only item
        elif in_remote and not in_local:
            if in_base:
                continue  # deleted locally -> stays dropped, don't resurrect
            result.append(remote_map[key])  # new remote-only item, pulled in
    return result


def _node_key(node):
    if node.get("type") == "group":
        return ("group", node.get("name"))
    return ("host", (node.get("config") or {}).get("name"))


def merge_host_config(base_cfg, local_cfg, remote_cfg, remote_wins):
    """Merges one host's config dict field-by-field, EXCEPT key_path,
    which is always taken from local as-is (never read from base/remote —
    those never carry it in the first place, see the module docstring).
    Starting from a copy of local_cfg is what makes that "just don't touch
    it" happen for free."""
    base_cfg = base_cfg or {}
    local_cfg = local_cfg or {}
    remote_cfg = remote_cfg or {}
    merged = dict(local_cfg)
    for field in HOST_FIELDS_TO_SYNC:
        merged[field] = merge_scalar(
            base_cfg.get(field, _MISSING), local_cfg.get(field), remote_cfg.get(field), remote_wins
        )
    return merged


def merge_children(base_children, local_children, remote_children, remote_wins):
    """Recursively merges one group's children list (a mix of "group" and
    "host" nodes) by (type, name) identity — see the module docstring for
    why there's no better identity key available (hosts/groups have no
    stable id in this app)."""
    base_map = {_node_key(n): n for n in (base_children or [])}
    local_map = {_node_key(n): n for n in (local_children or [])}
    remote_map = {_node_key(n): n for n in (remote_children or [])}

    ordered_keys = list(local_map.keys())
    for k in remote_map:
        if k not in local_map and k not in ordered_keys:
            ordered_keys.append(k)

    result = []
    for key in ordered_keys:
        in_base, in_local, in_remote = key in base_map, key in local_map, key in remote_map
        if in_local and in_remote:
            local_node, remote_node = local_map[key], remote_map[key]
            base_node = base_map.get(key)
            if local_node.get("type") == "group":
                merged_children = merge_children(
                    (base_node or {}).get("children", []),
                    local_node.get("children", []),
                    remote_node.get("children", []),
                    remote_wins,
                )
                result.append({
                    "type": "group",
                    "name": local_node.get("name"),
                    "expanded": local_node.get("expanded", True),
                    "children": merged_children,
                })
            else:
                merged_cfg = merge_host_config(
                    (base_node or {}).get("config"), local_node.get("config"), remote_node.get("config"), remote_wins
                )
                result.append({"type": "host", "config": merged_cfg})
        elif in_local and not in_remote:
            if in_base:
                continue  # deleted remotely
            result.append(local_map[key])
        elif in_remote and not in_local:
            if in_base:
                continue  # deleted locally
            node = remote_map[key]
            if node.get("type") == "host":
                cfg = dict(node.get("config") or {})
                cfg["key_path"] = None  # brand-new host pulled in — every machine needs its own key path
                result.append({"type": "host", "config": cfg})
            else:
                result.append(node)
    return result


def merge_host_tree(base_root, local_root, remote_root, remote_wins):
    merged_children = merge_children(
        (base_root or _EMPTY_ROOT).get("children", []),
        (local_root or _EMPTY_ROOT).get("children", []),
        (remote_root or _EMPTY_ROOT).get("children", []),
        remote_wins,
    )
    return {"type": "group", "name": "Root", "children": merged_children}


def strip_key_paths(root):
    """Deep copy of a host tree with every host's key_path removed —
    applied only when writing OUT to the shared file. Never applied to
    what gets written to the local hosts.json (that keeps every field,
    key_path included)."""
    def walk(node):
        if node.get("type") == "group":
            return {
                "type": "group", "name": node.get("name"), "expanded": node.get("expanded", True),
                "children": [walk(c) for c in node.get("children", [])],
            }
        cfg = dict(node.get("config") or {})
        cfg.pop("key_path", None)
        return {"type": "host", "config": cfg}

    return {"type": "group", "name": "Root", "children": [walk(c) for c in (root or _EMPTY_ROOT).get("children", [])]}


# --- AI chats: filesystem-based merge, separate from the JSON blob above ---

def _scan_chat_summaries(directory):
    """{"id": updated_at, ...} for every *.json file directly in directory
    (no recursion) — used for both the local CHATS_DIR and the shared
    folder's ai_chats subdirectory, which have the identical layout."""
    summaries = {}
    if not directory.is_dir():
        return summaries
    for path in directory.glob("*.json"):
        data = _load_json(path)
        if data and data.get("id"):
            summaries[data["id"]] = data.get("updated_at") or 0
    return summaries


def plan_ai_chat_sync(local_summaries, remote_summaries, known_ids):
    """Pure decision function (no I/O) — returns (to_pull, to_push,
    to_delete_local, to_delete_remote, new_known_ids). known_ids is the
    set of chat ids this machine saw at the end of its last successful
    sync, used the same way "base" is for everything else: to tell "never
    existed" apart from "existed, then got deleted"."""
    known_ids = set(known_ids)
    to_pull, to_push, to_delete_local, to_delete_remote = [], [], [], []
    all_ids = set(local_summaries) | set(remote_summaries)
    for chat_id in all_ids:
        in_local, in_remote, in_known = chat_id in local_summaries, chat_id in remote_summaries, chat_id in known_ids
        if in_local and in_remote:
            if local_summaries[chat_id] > remote_summaries[chat_id]:
                to_push.append(chat_id)
            elif remote_summaries[chat_id] > local_summaries[chat_id]:
                to_pull.append(chat_id)
        elif in_local and not in_remote:
            (to_delete_local if in_known else to_push).append(chat_id)
        elif in_remote and not in_local:
            (to_delete_remote if in_known else to_pull).append(chat_id)
    new_known_ids = all_ids - set(to_delete_local) - set(to_delete_remote)
    return to_pull, to_push, to_delete_local, to_delete_remote, new_known_ids


def _sync_ai_chats(shared_dir, known_ids):
    """Executes the plan against real files. Returns (changed: bool, new_known_ids: list)."""
    remote_chats_dir = shared_dir / "ai_chats"
    local_summaries = _scan_chat_summaries(ai_chat_store.CHATS_DIR)
    remote_summaries = _scan_chat_summaries(remote_chats_dir)
    to_pull, to_push, to_delete_local, to_delete_remote, new_known_ids = plan_ai_chat_sync(
        local_summaries, remote_summaries, known_ids
    )

    for chat_id in to_pull:
        data = _load_json(remote_chats_dir / f"{chat_id}.json")
        if data:
            ai_chat_store.save_chat(chat_id, data.get("title", ""), data.get("messages", []))
    for chat_id in to_delete_local:
        ai_chat_store.delete_chat(chat_id)
    for chat_id in to_push:
        data = ai_chat_store.load_chat(chat_id)
        if data:
            _atomic_write_json(remote_chats_dir / f"{chat_id}.json", data)
    for chat_id in to_delete_remote:
        (remote_chats_dir / f"{chat_id}.json").unlink(missing_ok=True)

    changed = bool(to_pull or to_push or to_delete_local or to_delete_remote)
    return changed, sorted(new_known_ids)


# --- Category gather (local state as it exists right now) ---

def _gather_quickies(settings_manager):
    return {
        "enabled": settings_manager.get("quickies.enabled"),
        "position": settings_manager.get("quickies.position"),
        "search_position": settings_manager.get("quickies.search_position"),
        "items": settings_manager.get("quickies.items") or [],
    }


def _gather_flat(settings_manager, keys):
    return {k: settings_manager.get(k) for k in keys}


# --- Top-level orchestration ---

def perform_sync(settings_manager, config_data):
    """Runs one full sync pass. Safe to call from a background thread —
    touches only settings.json/hosts.json/ai chat files/the shared folder,
    never a GTK widget. The caller (window.py) is responsible for
    reflecting SyncResult.changed_categories back onto live widgets
    (tree/quickies-listbox/terminal colors) via GLib.idle_add afterward."""
    folder = (settings_manager.get("sync.folder") or "").strip()
    if not folder:
        return SyncResult(False, error=_("No sync folder configured."))

    shared_dir = Path(folder).expanduser()
    try:
        shared_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg = _("Can't access sync folder: {error}").format(error=e)
        settings_manager.set("sync.last_sync_error", msg)
        settings_manager.save()
        return SyncResult(False, error=msg)

    sync_file = shared_dir / SYNC_FILE_NAME
    remote_payload, hash_error = _load_and_verify_sync_file(sync_file)
    if hash_error:
        settings_manager.set("sync.last_sync_error", hash_error)
        settings_manager.save()
        return SyncResult(False, error=hash_error)
    remote_payload = remote_payload or {}

    state = _load_json(SYNC_STATE_FILE, default={}) or {}
    base_snapshot = state.get("base_snapshot") or {}
    last_synced_version = state.get("last_synced_version") or 0
    remote_wins = (remote_payload.get("version") or 0) > last_synced_version

    changed = set()
    new_base_snapshot = dict(base_snapshot)
    new_remote_payload = {k: v for k, v in remote_payload.items() if k not in ("version", "hash")}
    new_config_data = config_data

    try:
        if settings_manager.get("sync.sync_hosts"):
            base_hosts = base_snapshot.get("hosts") or _EMPTY_ROOT
            remote_hosts = remote_payload.get("hosts") or _EMPTY_ROOT
            merged = merge_host_tree(base_hosts, config_data or _EMPTY_ROOT, remote_hosts, remote_wins)
            if merged != (config_data or _EMPTY_ROOT):
                changed.add("hosts")
            new_config_data = merged
            sanitized = strip_key_paths(merged)
            new_remote_payload["hosts"] = sanitized
            new_base_snapshot["hosts"] = sanitized

        if settings_manager.get("sync.sync_quickies"):
            base_q = base_snapshot.get("quickies") or {}
            local_q = _gather_quickies(settings_manager)
            remote_q = remote_payload.get("quickies") or {}
            merged_q = {
                "enabled": merge_scalar(base_q.get("enabled", _MISSING), local_q["enabled"], remote_q.get("enabled"), remote_wins),
                "position": merge_scalar(base_q.get("position", _MISSING), local_q["position"], remote_q.get("position"), remote_wins),
                "search_position": merge_scalar(base_q.get("search_position", _MISSING), local_q["search_position"], remote_q.get("search_position"), remote_wins),
                "items": merge_list(base_q.get("items"), local_q["items"], remote_q.get("items"), lambda i: i.get("name"), remote_wins),
            }
            if merged_q != local_q:
                changed.add("quickies")
                settings_manager.set("quickies.enabled", merged_q["enabled"])
                settings_manager.set("quickies.position", merged_q["position"])
                settings_manager.set("quickies.search_position", merged_q["search_position"])
                settings_manager.set("quickies.items", merged_q["items"])
            new_remote_payload["quickies"] = merged_q
            new_base_snapshot["quickies"] = merged_q

        if settings_manager.get("sync.sync_user_commands"):
            base_uc = base_snapshot.get("user_commands") or []
            local_uc = settings_manager.get("user_commands") or []
            remote_uc = remote_payload.get("user_commands") or []
            merged_uc = merge_list(base_uc, local_uc, remote_uc, lambda i: i.get("name"), remote_wins)
            if merged_uc != local_uc:
                changed.add("user_commands")
                settings_manager.set("user_commands", merged_uc)
            new_remote_payload["user_commands"] = merged_uc
            new_base_snapshot["user_commands"] = merged_uc

        if settings_manager.get("sync.sync_general"):
            base_g = base_snapshot.get("general") or {}
            local_g = _gather_flat(settings_manager, GENERAL_SETTINGS_KEYS)
            remote_g = remote_payload.get("general") or {}
            merged_g = {
                k: merge_scalar(base_g.get(k, _MISSING), local_g[k], remote_g.get(k), remote_wins)
                for k in GENERAL_SETTINGS_KEYS
            }
            if merged_g != local_g:
                changed.add("general")
                for k, v in merged_g.items():
                    settings_manager.set(k, v)
            new_remote_payload["general"] = merged_g
            new_base_snapshot["general"] = merged_g

        if settings_manager.get("sync.sync_terminal"):
            base_t = base_snapshot.get("terminal") or {}
            local_t = _gather_flat(settings_manager, TERMINAL_SETTINGS_KEYS)
            local_t["custom_color_scheme"] = colors_module.load_custom_color_scheme()
            remote_t = remote_payload.get("terminal") or {}
            merged_t = {
                k: merge_scalar(base_t.get(k, _MISSING), local_t[k], remote_t.get(k), remote_wins)
                for k in TERMINAL_SETTINGS_KEYS
            }
            merged_t["custom_color_scheme"] = merge_scalar(
                base_t.get("custom_color_scheme", _MISSING), local_t["custom_color_scheme"],
                remote_t.get("custom_color_scheme"), remote_wins,
            )
            if merged_t != local_t:
                changed.add("terminal")
                for k in TERMINAL_SETTINGS_KEYS:
                    settings_manager.set(k, merged_t[k])
                if merged_t["custom_color_scheme"]:
                    colors_module.save_custom_color_scheme(merged_t["custom_color_scheme"])
            new_remote_payload["terminal"] = merged_t
            new_base_snapshot["terminal"] = merged_t

        ai_changed = False
        new_known_ids = state.get("ai_chat_ids") or []
        if settings_manager.get("sync.sync_ai_chats"):
            ai_changed, new_known_ids = _sync_ai_chats(shared_dir, state.get("ai_chat_ids") or [])
            if ai_changed:
                changed.add("ai_chats")

        if "hosts" in changed:
            hosts_config.save_config(new_config_data)
        if changed & {"quickies", "user_commands", "general", "terminal"}:
            settings_manager.save()

        new_version = time.time()
        new_remote_payload["version"] = new_version
        body_for_hash = {k: v for k, v in new_remote_payload.items() if k != "hash"}
        new_remote_payload["hash"] = compute_hash(body_for_hash)
        _atomic_write_json(sync_file, new_remote_payload)

        _atomic_write_json(SYNC_STATE_FILE, {
            "last_synced_version": new_version,
            "base_snapshot": new_base_snapshot,
            "ai_chat_ids": new_known_ids,
        })

        settings_manager.set("sync.last_sync_at", new_version)
        settings_manager.set("sync.last_sync_error", "")
        settings_manager.save()

        return SyncResult(True, changed_categories=changed, new_config_data=new_config_data if "hosts" in changed else None)
    except Exception as e:
        logging.error(f"Sync pass failed: {e}")
        settings_manager.set("sync.last_sync_error", str(e))
        settings_manager.save()
        return SyncResult(False, error=str(e))
