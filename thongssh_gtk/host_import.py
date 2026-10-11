# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""Parsers for importing hosts from other SSH/terminal clients' config files
into ThongSSH's own host tree (see Settings -> Import Hosts, dialogs.py's
SettingsDialog, and _("Import Hosts") page).

Every parser below returns a plain list[dict] of top-level nodes in
ThongSSH's OWN on-disk node shape (see config.py):
  {"type": "group", "name": str, "expanded": True, "children": [...]}
  {"type": "host", "config": {...partial HOST_CONFIG_TEMPLATE fields...}}
Each host node only needs to carry the fields a given source format actually
has ("name", "host", "port", "protocol") — the caller runs the whole
imported subtree through config._recursive_migrate() once, which backfills
every other HOST_CONFIG_TEMPLATE field exactly the way a hand-added host
would get them (key_path, forward_x, post_connect_commands, ...).

Format-accuracy note: ssh_config, mRemoteNG's confCons.xml, MobaXterm's
.mxtsessions, and Asbru-CM's asbru.yml were confirmed against real
documentation/source. GNOME Connection Manager's and SecureCRT's exact file
layouts could NOT be independently verified (no official spec, sparse/
conflicting sources) — those two parsers are best-effort and marked as such
below. Every parser fails soft per-entry (skips what it can't confidently
read, appends to a running `skipped` counter) rather than raising, so one
wrong assumption about an obscure field degrades to "fewer hosts parsed,"
never a crash.
"""

import configparser
import glob
import logging
import os
import re
from pathlib import Path
from xml.etree import ElementTree

from .config import HOST_CONFIG_TEMPLATE


class ImportResult:
    """What a parser hands back: the parsed top-level nodes, plus counts for
    the Settings page's status label — "Parsed 12 hosts in 3 groups (2
    skipped)" needs both numbers, not just a flat node list."""
    def __init__(self):
        self.nodes = []       # list[node], top-level (groups and/or bare hosts)
        self.host_count = 0
        self.group_count = 0
        self.skipped_count = 0

    def add_top_level(self, node):
        self.nodes.append(node)

    def count_node(self, node):
        """Recursively tallies host_count/group_count for a node already
        added to the tree — called once per top-level node after it (and
        everything under it) is fully built."""
        if node.get("type") == "group":
            self.group_count += 1
            for child in node.get("children", []):
                self.count_node(child)
        else:
            self.host_count += 1


def _make_host_node(name, host, port=None, user=None, protocol="ssh"):
    """A minimal host node — only the fields this import actually knows.
    _recursive_migrate (called once by the caller on the whole imported
    subtree) fills in everything else."""
    full_host = f"{user}@{host}" if user else host
    config = dict(HOST_CONFIG_TEMPLATE)
    config["name"] = name or host
    config["host"] = full_host
    config["protocol"] = protocol
    if port:
        config["port"] = port
    return {"type": "host", "config": config}


def _make_group_node(name, children):
    return {"type": "group", "name": name, "expanded": True, "children": children}


# --- 1. ThongSSH's own export -------------------------------------------------

def parse_own(path):
    """A hosts.json-shaped file — another ThongSSH install's export, or a
    backup copy. Just validated and handed back as-is (already migrated by
    the caller's shared _recursive_migrate pass)."""
    import json
    result = ImportResult()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("type") != "group":
        raise ValueError("Not a ThongSSH hosts.json-shaped file (expected a root group node).")
    result.add_top_level(data)
    result.count_node(data)
    return result


# --- 2. ~/.ssh/config ---------------------------------------------------------

_SSH_INCLUDE_RE = re.compile(r"^\s*Include\s+(.+?)\s*$", re.IGNORECASE)
_SSH_HOST_RE = re.compile(r"^\s*Host\s+(.+?)\s*$", re.IGNORECASE)
_SSH_KEY_RE = re.compile(r"^\s*(\S+)\s+(.+?)\s*$")


def _read_ssh_config_lines(path, _seen=None):
    """Flattens Include directives (glob-expanded, relative to the
    including file's own directory, ~ expanded) into one line stream —
    recursive, with a _seen guard against an Include cycle."""
    if _seen is None:
        _seen = set()
    path = Path(path).expanduser().resolve()
    if path in _seen or not path.is_file():
        return []
    _seen.add(path)
    lines = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        include_match = _SSH_INCLUDE_RE.match(raw_line)
        if include_match:
            pattern = include_match.group(1)
            if not os.path.isabs(os.path.expanduser(pattern)):
                pattern = str(path.parent / pattern)
            for included in sorted(glob.glob(os.path.expanduser(pattern))):
                lines.extend(_read_ssh_config_lines(included, _seen))
        else:
            lines.append(raw_line)
    return lines


def parse_ssh_config(path):
    """Flat list of hosts — ssh_config has no native grouping. A `Host`
    block whose only pattern contains a wildcard (`Host *`, `Host *.corp`)
    or that never sets HostName is skipped — it's a defaults block, not a
    real, connectable host."""
    result = ImportResult()
    lines = _read_ssh_config_lines(path)

    blocks = []  # [(patterns: list[str], {key.lower(): value}), ...]
    current = None
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        host_match = _SSH_HOST_RE.match(raw_line)
        if host_match:
            current = (host_match.group(1).split(), {})
            blocks.append(current)
            continue
        if current is None:
            continue
        key_match = _SSH_KEY_RE.match(line)
        if key_match:
            current[1][key_match.group(1).lower()] = key_match.group(2).strip('"')

    for patterns, fields in blocks:
        real_patterns = [p for p in patterns if "*" not in p and "?" not in p]
        if not real_patterns:
            result.skipped_count += 1
            continue
        hostname = fields.get("hostname")
        if not hostname:
            result.skipped_count += 1
            continue
        for name in real_patterns:
            port = None
            if fields.get("port", "").isdigit():
                port = int(fields["port"])
            node = _make_host_node(name, hostname, port=port, user=fields.get("user"))
            if fields.get("identityfile"):
                node["config"]["key_path"] = os.path.expanduser(fields["identityfile"])
            result.add_top_level(node)
            result.count_node(node)
    return result


# --- 3. GNOME Connection Manager (best-effort — see module docstring) --------

def parse_gcm(path):
    """GNOME Connection Manager stores connections in a ConfigParser-style
    ini file (traditionally ~/.gcrc), one [Section] per host, with a
    `group=` value using "/" as a path separator for nested folders. This
    exact layout could not be independently verified against an official
    spec — best-effort, skips anything that doesn't look like a real host
    section rather than guessing."""
    result = ImportResult()
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise ValueError(f"Could not parse as an ini-style GCM config: {e}")

    groups = {}  # "A/B" -> group node, already linked into its own parent

    def get_group(path_str):
        if not path_str:
            return None
        if path_str in groups:
            return groups[path_str]
        parent_path, _, leaf_name = path_str.rpartition("/")
        node = _make_group_node(leaf_name, [])
        groups[path_str] = node
        parent = get_group(parent_path) if parent_path else None
        if parent is not None:
            parent["children"].append(node)
        else:
            result.add_top_level(node)
        return node

    for section in parser.sections():
        fields = parser[section]
        hostname = fields.get("hostname") or fields.get("host")
        if not hostname:
            result.skipped_count += 1
            continue
        port = None
        if (fields.get("port") or "").isdigit():
            port = int(fields["port"])
        protocol = "telnet" if (fields.get("protocol") or "").lower() == "telnet" else "ssh"
        node = _make_host_node(section, hostname, port=port, user=fields.get("user"), protocol=protocol)
        group_node = get_group(fields.get("group", "").strip("/"))
        if group_node is not None:
            group_node["children"].append(node)
        else:
            result.add_top_level(node)

    for group_node in result.nodes:
        result.count_node(group_node)
    return result


# --- 4. Asbru-CM --------------------------------------------------------------

def parse_asbru(path):
    """asbru.yml: a flat dict of environments keyed by UUID, each with a
    "parent" UUID ("__PAC__ROOT__" at the top) and "_is_group" flag — the
    tree is reconstructed by grouping on "parent" rather than trusting the
    separate per-node "children" map, which is more robust to a partially
    corrupt file."""
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML is not installed — run 'pip install pyyaml' (or your distro's "
                           "python3-yaml/python3-pyyaml/python-yaml package) to import Asbru-CM configs.")

    result = ImportResult()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    environments = (data or {}).get("environments") or {}

    children_by_parent = {}
    for uuid, entry in environments.items():
        if not isinstance(entry, dict):
            continue
        parent = entry.get("parent") or "__PAC__ROOT__"
        children_by_parent.setdefault(parent, []).append((uuid, entry))

    def build(uuid, entry):
        name = entry.get("name") or "Unnamed"
        if entry.get("_is_group"):
            children = [
                build(child_uuid, child_entry)
                for child_uuid, child_entry in children_by_parent.get(uuid, [])
            ]
            return _make_group_node(name, [c for c in children if c is not None])
        method = (entry.get("method") or "ssh").lower()
        if method not in ("ssh", "telnet"):
            result.skipped_count += 1
            return None
        host = entry.get("ip") or entry.get("host")
        if not host:
            result.skipped_count += 1
            return None
        port = entry.get("port") if str(entry.get("port") or "").isdigit() else None
        return _make_host_node(name, host, port=port, user=entry.get("user"), protocol=method)

    for uuid, entry in children_by_parent.get("__PAC__ROOT__", []):
        node = build(uuid, entry)
        if node is not None:
            result.add_top_level(node)
            result.count_node(node)
    return result


# --- 5. SecureCRT (best-effort — see module docstring) ------------------------

_SECURECRT_LINE_RE = re.compile(r'^(S|D):"(.+?)"=(.*)$')


def _parse_securecrt_ini(file_path):
    """One SecureCRT session .ini file -> flat {key: str_or_int} dict.
    D: values are hex-encoded decimals (confirmed: port 00000016 == 22)."""
    fields = {}
    for raw_line in Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SECURECRT_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        kind, key, value = match.groups()
        fields[key] = int(value, 16) if kind == "D" else value
    return fields


def parse_securecrt(folder):
    """SecureCRT (Linux/macOS port) stores one .ini file per session under
    a Sessions/ folder, with subfolders mirroring the session-group
    hierarchy shown in its Connect dialog. The exact per-field layout
    (S:"Key"=value / D:"Key"=hex) is confirmed from VanDyke's own scripting
    docs; which protocol-specific port key accompanies which "Protocol
    Name" value is best-effort."""
    result = ImportResult()
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"{folder} is not a folder — SecureCRT sessions are a folder of .ini files.")

    def build_group(dir_path):
        children = []
        for entry in sorted(dir_path.iterdir()):
            if entry.is_dir():
                sub = build_group(entry)
                if sub["children"]:
                    children.append(sub)
            elif entry.suffix.lower() == ".ini":
                host_node = build_host(entry)
                if host_node is not None:
                    children.append(host_node)
        return _make_group_node(dir_path.name, children)

    def build_host(ini_path):
        try:
            fields = _parse_securecrt_ini(ini_path)
        except OSError as e:
            logging.warning(f"Import Hosts (SecureCRT): couldn't read {ini_path}: {e}")
            result.skipped_count += 1
            return None
        hostname = fields.get("Hostname")
        if not hostname:
            result.skipped_count += 1
            return None
        protocol_name = (fields.get("Protocol Name") or "").upper()
        if protocol_name in ("SSH1", "SSH2"):
            protocol = "ssh"
        elif protocol_name == "TELNET":
            protocol = "telnet"
        else:
            result.skipped_count += 1
            return None
        port = fields.get(f"[{protocol_name}] Port") or fields.get("Port")
        return _make_host_node(
            ini_path.stem, hostname, port=port if isinstance(port, int) else None,
            user=fields.get("Username"), protocol=protocol,
        )

    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            group_node = build_group(entry)
            if group_node["children"]:
                result.add_top_level(group_node)
        elif entry.suffix.lower() == ".ini":
            host_node = build_host(entry)
            if host_node is not None:
                result.add_top_level(host_node)

    for node in result.nodes:
        result.count_node(node)
    return result


# --- 6. mRemoteNG --------------------------------------------------------------

def parse_mremoteng(path):
    """confCons.xml: nested <Node Type="Container"|"Connection"> elements.
    Password is always AES-encrypted in the source file (with a passphrase
    this app has no access to) and is never read here."""
    result = ImportResult()
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as e:
        raise ValueError(f"Not a valid mRemoteNG confCons.xml file: {e}")
    root = tree.getroot()

    def build(element):
        node_type = element.get("Type")
        name = element.get("Name") or "Unnamed"
        if node_type == "Container":
            children = [build(child) for child in element if child.tag == "Node"]
            return _make_group_node(name, [c for c in children if c is not None])
        if node_type == "Connection":
            protocol_raw = (element.get("Protocol") or "").upper()
            if protocol_raw.startswith("SSH"):
                protocol = "ssh"
            elif protocol_raw == "TELNET":
                protocol = "telnet"
            else:
                result.skipped_count += 1
                return None
            hostname = element.get("Hostname")
            if not hostname:
                result.skipped_count += 1
                return None
            port = element.get("Port")
            return _make_host_node(
                name, hostname, port=int(port) if (port or "").isdigit() else None,
                user=element.get("Username"), protocol=protocol,
            )
        return None

    for child in root:
        if child.tag != "Node":
            continue
        node = build(child)
        if node is not None:
            result.add_top_level(node)
            result.count_node(node)
    return result


# --- 7. MobaXterm --------------------------------------------------------------

_MOBAXTERM_PROTOCOL_MAP = {"109": "ssh", "98": "telnet"}


def _parse_mobaxterm_entry(value):
    """`#<protocol_id>#<subtype>%host%port%user%...` -> (protocol, host,
    port, user) or None if the protocol isn't ssh/telnet or the string
    doesn't match the expected shape. The field right after the second "#"
    (confirmed live against a real sample: "0" in "#109#0%<ip>%22%root%...")
    is some kind of connection-subtype flag, NOT the host — host/port/user
    start at the NEXT "%"-separated field after it."""
    hash_parts = value.split("#")
    if len(hash_parts) < 2:
        return None
    protocol = _MOBAXTERM_PROTOCOL_MAP.get(hash_parts[1])
    if protocol is None or len(hash_parts) < 3:
        return None
    percent_parts = hash_parts[2].split("%")
    if len(percent_parts) < 4:
        return None
    host, port_str, user = percent_parts[1], percent_parts[2], percent_parts[3]
    if not host:
        return None
    port = int(port_str) if port_str.isdigit() else None
    return protocol, host, port, (user or None)


def parse_mobaxterm(path):
    """.mxtsessions: ini-like [Bookmarks]/[Bookmarks_N] sections. A SubRep
    key in a section (backslash-separated, e.g. "Azure\\PRE") nests every
    OTHER entry in that same section under those groups; everything else
    is one session per key=value line."""
    result = ImportResult()
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise ValueError(f"Could not parse as an ini-style MobaXterm sessions file: {e}")

    groups = {}  # "A\\B" -> group node already linked to its parent/top-level

    def get_group(path_str):
        if not path_str:
            return None
        if path_str in groups:
            return groups[path_str]
        parent_path, _, leaf_name = path_str.rpartition("\\")
        node = _make_group_node(leaf_name, [])
        groups[path_str] = node
        parent = get_group(parent_path) if parent_path else None
        if parent is not None:
            parent["children"].append(node)
        else:
            result.add_top_level(node)
        return node

    for section in parser.sections():
        if not section.lower().startswith("bookmarks"):
            continue
        fields = parser[section]
        sub_rep = fields.get("SubRep", "").strip("\\")
        group_node = get_group(sub_rep) if sub_rep else None
        for key, value in fields.items():
            if key.lower() in ("subrep", "imgnum"):
                continue
            parsed = _parse_mobaxterm_entry(value)
            if parsed is None:
                result.skipped_count += 1
                continue
            protocol, host, port, user = parsed
            node = _make_host_node(key, host, port=port, user=user, protocol=protocol)
            if group_node is not None:
                group_node["children"].append(node)
            else:
                result.add_top_level(node)

    for node in result.nodes:
        result.count_node(node)
    return result


# --- Dispatch table ------------------------------------------------------------

# (key, display label, parser fn, chooser mode "file"|"folder", default path or None)
SOURCE_FORMATS = [
    ("own", "ThongSSH export", parse_own, "file", None),
    ("ssh_config", "SSH config", parse_ssh_config, "file", "~/.ssh/config"),
    ("gcm", "GNOME Connection Manager", parse_gcm, "file", "~/.gcrc"),
    ("asbru", "Asbru-CM", parse_asbru, "file", "~/.config/asbru/asbru.yml"),
    ("securecrt", "SecureCRT", parse_securecrt, "folder", None),
    ("mremoteng", "mRemoteNG", parse_mremoteng, "file", None),
    ("mobaxterm", "MobaXterm", parse_mobaxterm, "file", None),
]
