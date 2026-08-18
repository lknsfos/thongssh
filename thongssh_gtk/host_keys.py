# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""SSH host key verification — shared by sftp_widget.py and send_file.py,
the two places this app opens a paramiko.SSHClient of its own (regular
terminal sessions shell out to the real `ssh` binary instead, which
already does its own proper host key checking via ~/.ssh/known_hosts).

Both used to construct their SSHClient with paramiko.AutoAddPolicy(),
which silently trusts *any* host key on first connect and never re-checks
it later — for a tool whose whole point is secure remote access, that's
giving up the one guarantee SSH actually provides (never noticing a
machine-in-the-middle, on first connect or ever). make_ssh_client() below
is the fix: known hosts are loaded from ~/.ssh/known_hosts same as
OpenSSH's own `ssh` binary would (so a host key that *changes* later
makes paramiko itself raise BadHostKeyException, unconditionally,
regardless of any policy), and a genuinely unknown host raises
UnknownHostKeyError instead of being trusted or refused outright — the
caller catches that and asks the user via confirm_unknown_host_key,
same TOFU (trust-on-first-use) flow the real `ssh` CLI uses, before
retrying with the now-trusted key.
"""
import os
import logging

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .dialogs import MessageDialog

_ = lambda s: s

IS_PARAMIKO_AVAILABLE = False
try:
    import paramiko
    IS_PARAMIKO_AVAILABLE = True
except ImportError:
    pass


if IS_PARAMIKO_AVAILABLE:
    class UnknownHostKeyError(Exception):
        """Raised by InteractiveHostKeyPolicy instead of either trusting an
        unknown host key silently (paramiko.AutoAddPolicy, the previous
        behavior here) or refusing it with no way to proceed at all
        (paramiko.RejectPolicy) — carries what confirm_unknown_host_key
        needs to ask the user and, if trusted, persist the key."""
        def __init__(self, hostname, key):
            self.hostname = hostname
            self.key = key
            super().__init__(f"Unknown host key for {hostname}")

    class InteractiveHostKeyPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            raise UnknownHostKeyError(hostname, key)


def make_ssh_client():
    """A paramiko.SSHClient pre-configured with this app's host key
    policy — see module docstring. Callers should still wrap connect()
    in a try/except for UnknownHostKeyError (see confirm_unknown_host_key)
    and let paramiko.BadHostKeyException propagate as a regular,
    clearly-surfaced connection error (a known host's key changing is
    exactly the case that should stop and alarm, not offer a casual
    "trust and continue" — resolving that legitimately means editing
    ~/.ssh/known_hosts by hand, same as it would for a real `ssh` CLI
    user hitting the same warning)."""
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(InteractiveHostKeyPolicy())
    return client


def _known_hosts_path():
    return os.path.expanduser("~/.ssh/known_hosts")


def trust_host_key(hostname, key):
    """Appends hostname+key to ~/.ssh/known_hosts — the same file (and
    format) OpenSSH's own `ssh`/`ssh-keyscan` use, so this app's "trust
    this key" decision is indistinguishable from having done it with the
    real CLI, and a genuine `ssh` session to the same host benefits too,
    not just this app's own future connections to it."""
    path = _known_hosts_path()
    host_keys = paramiko.HostKeys()
    try:
        host_keys.load(path)
    except IOError:
        pass  # no known_hosts file yet — fine, this'll create it
    host_keys.add(hostname, key.get_name(), key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    host_keys.save(path)
    logging.info(f"Trusted new host key for {hostname} ({key.fingerprint}), saved to {path}")


def confirm_unknown_host_key(parent_widget, error, on_decided):
    """Shows a confirmation dialog for the UnknownHostKeyError just caught
    (from a background thread — this schedules itself onto the main
    thread via GLib.idle_add, so it's safe to call directly from a
    connect() except clause). on_decided(trusted: bool) runs on the main
    thread once the user answers; on trusted=True, the key has already
    been saved to ~/.ssh/known_hosts (via trust_host_key) by the time it's
    called — the caller just needs to retry its connection attempt, which
    will now succeed against the just-trusted key.

    Deliberately never auto-decides anything itself — every unknown host
    key stops and asks, exactly the point of not using AutoAddPolicy."""
    def show_dialog():
        dialog = MessageDialog(
            parent_widget.get_root() if hasattr(parent_widget, "get_root") else parent_widget,
            heading=_("Unknown Host Key"),
            body=_(
                "The authenticity of host '{host}' can't be established.\n\n"
                "{key_type} key fingerprint:\n{fingerprint}\n\n"
                "Are you sure you want to continue connecting? This will save the key to "
                "~/.ssh/known_hosts, trusting it for future connections too."
            ).format(host=error.hostname, key_type=error.key.get_name(), fingerprint=error.key.fingerprint),
            buttons=[(_("Cancel"), Gtk.ResponseType.CANCEL), (_("Trust and Connect"), Gtk.ResponseType.OK)],
        )
        def on_response(_dialog, response_id):
            trusted = response_id == Gtk.ResponseType.OK
            if trusted:
                trust_host_key(error.hostname, error.key)
            on_decided(trusted)
        dialog.run_async(on_response)
        return False
    GLib.idle_add(show_dialog)
