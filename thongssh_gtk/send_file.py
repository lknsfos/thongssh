import os
import re
import uuid
import logging
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Adw, GLib, Vte

from .dialogs import InputDialog
from . import host_keys

IS_PARAMIKO_AVAILABLE = False
try:
    import paramiko
    IS_PARAMIKO_AVAILABLE = True
except ImportError:
    logging.warning("Send File is disabled. Please install 'paramiko' (`pip install paramiko`).")

# Placeholder for future internationalization (i18n)
_ = lambda s: s


def guess_remote_cwd(terminal):
    """
    Best-effort guess of the remote shell's current directory, via VTE's
    OSC-7 tracking (Vte.Terminal.get_current_directory_uri()).

    This only works if the REMOTE shell explicitly emits OSC-7 escape
    sequences — most servers don't have that configured out of the box (the
    common vte.sh integration checks $VTE_VERSION, which is a local env var
    that SSH doesn't forward to the remote shell). So this is a convenience
    guess, not a guarantee — falls back to "~", editable by the user before
    sending.
    """
    uri = terminal.get_current_directory_uri()
    if not uri:
        return "~"
    try:
        path, _host = GLib.filename_from_uri(uri)
        return path or "~"
    except GLib.Error:
        return "~"


def probe_remote_cwd_async(terminal, callback, timeout_ms=2500):
    """
    Types `pwd` into the terminal, wrapped in a unique marker on its own
    line before and after, then reads it back off the screen to get the
    REAL remote cwd — works regardless of OSC-7 support, since it's just
    running a real shell command.

    WARNING: this literally feeds keystrokes into the terminal. If the
    session isn't sitting at a plain shell prompt (e.g. it's inside vim,
    less, an editor, a REPL...), those keystrokes go to THAT program
    instead — e.g. in vim's normal mode, "pwd" + Enter can delete a line.
    Only call this when the terminal is known to be at a shell prompt;
    that's why it's wired to an explicit "Detect" button, never automatic.

    callback(path_or_None) is invoked once, either with the captured path
    or None on timeout (markers never showed up within timeout_ms).
    """
    marker = f"__thongssh_cwd_{uuid.uuid4().hex}__"
    command = f"printf '%s\\n' '{marker}'; pwd; printf '%s\\n' '{marker}'\n"
    pattern = re.compile(rf"^{re.escape(marker)}$", re.MULTILINE)

    terminal.feed_child(command.encode("utf-8"))
    start_time = GLib.get_monotonic_time()

    def poll():
        text = terminal.get_text_format(Vte.Format.TEXT) or ""
        matches = list(pattern.finditer(text))
        if len(matches) >= 2:
            captured = text[matches[0].end():matches[1].start()].strip()
            callback(captured or None)
            return False
        elapsed_ms = (GLib.get_monotonic_time() - start_time) / 1000
        if elapsed_ms > timeout_ms:
            callback(None)
            return False
        return True

    GLib.timeout_add(150, poll)


class SendFileDialog(Adw.Window):
    """
    Sends one local file to the current terminal's remote host over SFTP —
    reusing the same host config (user/port/key) and the same auth cascade
    (saved password -> SSH key -> interactive password prompt) the SFTP
    panel itself uses, so "Send File" needs no separate login.
    """
    def __init__(self, parent_window, host_config, initial_remote_dir, terminal=None):
        super().__init__(transient_for=parent_window, modal=True)
        self.parent_window = parent_window
        self.host_config = host_config
        self.terminal = terminal
        self.local_path = None

        self.set_title(_("Send File"))
        self.set_default_size(440, -1)

        header_bar = Adw.HeaderBar()
        self.send_button = Gtk.Button(label=_("Send"), css_classes=["suggested-action"])
        self.send_button.set_sensitive(False)
        self.send_button.connect("clicked", self.on_send_clicked)
        header_bar.pack_end(self.send_button)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(header_bar)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        content_box.append(main_box)

        file_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.file_label = Gtk.Label(label=_("No file selected"), hexpand=True, halign=Gtk.Align.START)
        choose_button = Gtk.Button(icon_name="document-open-symbolic")
        choose_button.set_tooltip_text(_("Choose local file"))
        choose_button.connect("clicked", self.on_choose_file_clicked)
        file_row.append(self.file_label)
        file_row.append(choose_button)
        main_box.append(file_row)

        dest_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dest_row.append(Gtk.Label(label=_("Remote directory:")))
        self.remote_dir_entry = Gtk.Entry(text=initial_remote_dir, hexpand=True)
        dest_row.append(self.remote_dir_entry)
        if self.terminal is not None:
            detect_button = Gtk.Button(icon_name="find-location-symbolic")
            detect_button.set_tooltip_text(_(
                "Detect from terminal — runs 'pwd' there. Only use this when "
                "the terminal is at a plain shell prompt, not inside an editor "
                "or other full-screen program."
            ))
            detect_button.connect("clicked", self.on_detect_cwd_clicked)
            dest_row.append(detect_button)
        main_box.append(dest_row)

        self.status_label = Gtk.Label(label="", halign=Gtk.Align.START, wrap=True, css_classes=["dim-label"])
        main_box.append(self.status_label)

        self.set_content(content_box)

    def on_choose_file_clicked(self, button):
        file_chooser = Gtk.FileChooserDialog(
            title=_("Select File to Send"),
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN
        )
        file_chooser.add_button(_("Select"), Gtk.ResponseType.OK)
        file_chooser.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        file_chooser.connect("response", self.on_file_chosen)
        file_chooser.present()

    def on_file_chosen(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            gfile = dialog.get_file()
            if gfile:
                self.local_path = gfile.get_path()
                self.file_label.set_text(os.path.basename(self.local_path))
                self.send_button.set_sensitive(True)
        dialog.destroy()

    def on_detect_cwd_clicked(self, button):
        button.set_sensitive(False)
        self.status_label.set_text(_("Detecting current directory (make sure the terminal is at a shell prompt)..."))

        def on_detected(path):
            button.set_sensitive(True)
            if path:
                self.remote_dir_entry.set_text(path)
                self.status_label.set_text(_("Detected: {path}").format(path=path))
            else:
                self.status_label.set_text(_("Could not detect the current directory."))

        probe_remote_cwd_async(self.terminal, on_detected)

    def on_send_clicked(self, *args):
        if not self.local_path:
            return
        if not IS_PARAMIKO_AVAILABLE:
            self.status_label.set_text(_("Paramiko is not installed."))
            return
        remote_dir = self.remote_dir_entry.get_text().strip() or "~"
        self.send_button.set_sensitive(False)

        if '@' not in (self.host_config.get("host") or ""):
            self._prompt_username_then_send(remote_dir)
            return

        self.status_label.set_text(_("Connecting..."))
        _start_send(self, self.host_config, self.local_path, remote_dir)

    def _prompt_username_then_send(self, remote_dir):
        """
        The tab's host config had no username on it (shouldn't normally
        happen anymore now that terminal sessions remember the username
        they actually connected with — see window.py's _continue_session —
        but this covers any tab left over from before that fix, or any
        other edge case). Ask for one, then continue into the normal auth
        cascade (key -> saved password -> interactive password prompt).
        """
        input_dialog = InputDialog(
            self,
            title=_("Username Required"),
            message=_("Enter username for {host}").format(host=self.host_config.get("host", "")),
        )
        def on_username(username):
            if not username:
                self.status_label.set_text(_("Canceled."))
                self.send_button.set_sensitive(True)
                return
            resolved_config = dict(self.host_config)
            resolved_config["host"] = f"{username}@{self.host_config.get('host', '')}"
            self.status_label.set_text(_("Connecting..."))
            _start_send(self, resolved_config, self.local_path, remote_dir)
        input_dialog.run_async(on_username)


def _start_send(dialog, host_config, local_path, remote_dir, key_passphrase=None, auth_password=None):
    thread = threading.Thread(
        target=_send_worker,
        args=(dialog, host_config, local_path, remote_dir, key_passphrase, auth_password),
        daemon=True,
    )
    thread.start()


def _send_worker(dialog, host_config, local_path, remote_dir, key_passphrase, auth_password):
    host_str = host_config.get("host") or ""
    if '@' not in host_str:
        GLib.idle_add(_finish_with_error, dialog, _("Username missing from host config."))
        return
    user, host = host_str.split('@', 1)

    port = int(host_config.get("port") or 22)
    key_filename = host_config.get("key_path")
    remote_name = os.path.basename(local_path)

    ssh_client = host_keys.make_ssh_client()

    try:
        if auth_password:
            GLib.idle_add(dialog.status_label.set_text, _("Connecting with provided password..."))
            ssh_client.connect(host, port=port, username=user, password=auth_password,
                                timeout=10, allow_agent=False, look_for_keys=False)
        elif key_filename:
            GLib.idle_add(dialog.status_label.set_text, _("Connecting with SSH key..."))
            ssh_client.connect(host, port=port, username=user, password=key_passphrase,
                                key_filename=key_filename, timeout=10)
        else:
            password_from_keyring = dialog.parent_window.keyring.load_password(host_config.get("name"))
            if password_from_keyring:
                GLib.idle_add(dialog.status_label.set_text, _("Connecting with saved password..."))
                ssh_client.connect(host, port=port, username=user, password=password_from_keyring,
                                    timeout=10, allow_agent=False, look_for_keys=False)
            else:
                GLib.idle_add(dialog.status_label.set_text, _("Connecting..."))
                ssh_client.connect(host, port=port, username=user, timeout=10)
    except paramiko.PasswordRequiredException:
        GLib.idle_add(_prompt_key_passphrase, dialog, host_config, local_path, remote_dir, key_filename)
        return
    except paramiko.AuthenticationException:
        if not auth_password:
            GLib.idle_add(_prompt_password, dialog, host_config, local_path, remote_dir)
            return
        GLib.idle_add(_finish_with_error, dialog, _("Authentication failed."))
        return
    except host_keys.UnknownHostKeyError as e:
        def on_decided(trusted):
            if trusted:
                # Key's now in ~/.ssh/known_hosts — retry the exact same
                # attempt (same args), which spins up a fresh SSHClient
                # that will find it there this time.
                _start_send(dialog, host_config, local_path, remote_dir, key_passphrase, auth_password)
            else:
                _finish_with_error(dialog, _("Host key not trusted — connection canceled."))
        host_keys.confirm_unknown_host_key(dialog, e, on_decided)
        return
    except Exception as e:
        GLib.idle_add(_finish_with_error, dialog, _("Connection failed: {e}").format(e=e))
        return

    try:
        sftp = ssh_client.open_sftp()
        target_dir = sftp.normalize('.') if remote_dir in ("~", "") else remote_dir
        remote_full_path = target_dir.rstrip('/') + '/' + remote_name
        GLib.idle_add(dialog.status_label.set_text, _("Uploading {name}...").format(name=remote_name))
        sftp.put(local_path, remote_full_path)
        sftp.close()
        GLib.idle_add(_finish_with_success, dialog, remote_full_path)
    except Exception as e:
        GLib.idle_add(_finish_with_error, dialog, _("Upload failed: {e}").format(e=e))
    finally:
        ssh_client.close()


def _prompt_key_passphrase(dialog, host_config, local_path, remote_dir, key_filename):
    input_dialog = InputDialog(
        dialog,
        title=_("SSH Key Passphrase"),
        message=_("Enter passphrase for key '{key}'").format(key=os.path.basename(key_filename)),
        is_password=True
    )
    def on_passphrase(passphrase):
        if passphrase is None:
            dialog.status_label.set_text(_("Canceled."))
            dialog.send_button.set_sensitive(True)
            return
        dialog.status_label.set_text(_("Connecting..."))
        _start_send(dialog, host_config, local_path, remote_dir, key_passphrase=passphrase)
    input_dialog.run_async(on_passphrase)


def _prompt_password(dialog, host_config, local_path, remote_dir):
    input_dialog = InputDialog(
        dialog,
        title=_("Password Required"),
        message=_("Enter password for {host}").format(host=host_config.get("host", "")),
        is_password=True
    )
    def on_password(password):
        if password is None:
            dialog.status_label.set_text(_("Canceled."))
            dialog.send_button.set_sensitive(True)
            return
        dialog.status_label.set_text(_("Connecting..."))
        _start_send(dialog, host_config, local_path, remote_dir, auth_password=password)
    input_dialog.run_async(on_password)


def _finish_with_success(dialog, remote_full_path):
    dialog.status_label.set_text(_("Sent to {path}").format(path=remote_full_path))
    GLib.timeout_add(1200, lambda: (dialog.close(), False)[-1])


def _finish_with_error(dialog, message):
    dialog.status_label.set_text(message)
    dialog.send_button.set_sensitive(True)
