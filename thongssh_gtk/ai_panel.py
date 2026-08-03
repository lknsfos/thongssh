import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Gdk, Pango

from .ai_providers import send_chat_request, resolve_provider_config
from .cli_providers import run_cli_chat, resolve_cli_config
from .markdown_view import render_markdown_into_box
from .provider_badges import icon_name_for, badge_family, badge_text
from .ai_chat_store import new_chat_id, save_chat, load_chat, delete_chat, list_chats, search_chats

# Placeholder for future internationalization (i18n)
_ = lambda s: s


class AiPanel(Gtk.Box):
    """Shared AI chat panel — a single conversation, switchable between
    whichever configured provider is currently active (switching provider
    changes who handles the *next* message, the conversation continues).

    Built once in ThongSSHWindow.__init__ and never destroyed — hiding it
    (the header button "un-press" collapse) only toggles set_visible(),
    so self.history and self.active_provider_id survive untouched.
    """

    def __init__(self, parent_window, settings_manager, keyring):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.parent_window = parent_window
        self.settings_manager = settings_manager
        self.keyring = keyring

        self.active_provider_id = None
        self.history = []  # [{"role": "user"|"assistant", "content": str}] — survives hide/show
        # None until the first message of a chat is actually sent (see
        # _on_send_clicked) — an opened-but-unused chat is never written to
        # disk at all.
        self.current_chat_id = None
        self.current_chat_title = ""
        # chat_ids with a request currently in flight — lets a chat you've
        # navigated away from (still "Thinking…") show that same state
        # again if you come back to it before the reply lands, instead of
        # looking like nothing was ever asked (see _load_chat).
        self._pending_request_chat_ids = set()
        # chat_ids deleted while their own request was still in flight —
        # the reply lands after, with nothing left to attach it to; this
        # stops that late arrival from re-creating the file Delete just
        # removed.
        self._deleted_chat_ids = set()
        self._pending_context = None
        self._typing_indicator = None
        self._pinned_to_bottom = True
        self._scroll_reinforce_generation = 0
        self._scroll_reinforce_ticks_left = 0

        self.set_size_request(220, -1)
        self.add_css_class("ai-panel")

        # --- Header ---
        # Provider-picker buttons live here now (moved from the main
        # window's header bar — see window.py's refresh_ai_provider_buttons)
        # instead of just naming the active one in a label. A FlowBox
        # wraps onto a second row on its own once enough providers are
        # configured to not fit one line — the header (and the row above
        # the transcript) just grows to fit, no special-casing a specific
        # count.
        # Gtk.CenterBox, not a plain Gtk.Box: a Box only honors a child's
        # halign/valign for its *cross* axis — along its own orientation
        # (horizontal here, matching the child below), a non-expanding
        # child is simply packed at the start, halign=CENTER or not.
        # CenterBox's whole job is centering its one middle widget along
        # that main axis, which a plain Box structurally can't do.
        header = Gtk.CenterBox(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_margin_top(6)
        header.set_margin_start(6)
        header.set_margin_end(6)
        header.set_margin_bottom(6)

        self.provider_button_box = Gtk.FlowBox()
        self.provider_button_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.provider_button_box.set_valign(Gtk.Align.CENTER)
        self.provider_button_box.set_row_spacing(4)
        self.provider_button_box.set_column_spacing(4)
        self.provider_button_box.set_homogeneous(False)
        # Placeholder until refresh_ai_provider_buttons (window.py) sets
        # this to the real button count right after (re)populating this
        # box — Gtk.FlowBox's *natural* width request scales with
        # max-children-per-line regardless of how many children actually
        # exist, so leaving it high (or "unlimited") makes it request far
        # more width than available, which left it filling the whole
        # CenterBox instead of sitting at its own natural (small) size.
        self.provider_button_box.set_max_children_per_line(1)
        header.set_center_widget(self.provider_button_box)

        self.append(header)

        # --- Chat toolbar: new/delete the current chat, switch to a past
        # one. Separate row from the provider buttons above — that one
        # already grows to 2 lines on its own with enough providers
        # configured, this row shouldn't be shuffled by that.
        chat_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chat_toolbar.set_margin_start(6)
        chat_toolbar.set_margin_end(6)
        chat_toolbar.set_margin_bottom(6)

        self.new_chat_button = Gtk.Button(icon_name="list-add-symbolic")
        self.new_chat_button.set_tooltip_text(_("New chat"))
        self.new_chat_button.connect("clicked", self._on_new_chat_clicked)
        chat_toolbar.append(self.new_chat_button)

        self.delete_chat_button = Gtk.Button(icon_name="list-remove-symbolic")
        self.delete_chat_button.set_tooltip_text(_("Delete this chat"))
        self.delete_chat_button.connect("clicked", self._on_delete_chat_clicked)
        chat_toolbar.append(self.delete_chat_button)

        # Doubles as the "current chat" indicator (its label) and the
        # trigger for the history/search popover — clicking it is how you
        # get back to an earlier chat.
        self.chat_picker_button = Gtk.MenuButton()
        self.chat_picker_button.set_hexpand(True)
        picker_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        picker_content.append(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
        self.chat_picker_label = Gtk.Label(label=_("New chat"), xalign=0)
        self.chat_picker_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.chat_picker_label.set_hexpand(True)
        picker_content.append(self.chat_picker_label)
        self.chat_picker_button.set_child(picker_content)
        chat_toolbar.append(self.chat_picker_button)

        self._build_chat_history_popover()

        self.append(chat_toolbar)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Transcript ---
        self.transcript_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.transcript_box.set_margin_top(6)
        self.transcript_box.set_margin_bottom(12)  # extra room so the last bubble isn't flush against the input row
        self.transcript_box.set_margin_start(6)
        self.transcript_box.set_margin_end(6)

        self.transcript_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.transcript_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.transcript_scroller.set_child(self.transcript_box)
        # "Pinned to bottom" pattern, not a fixed time window: a newly-
        # appended bubble's real height is only known once GTK's layout
        # pass actually runs, which can land arbitrarily late for a big
        # reply (long text, a code block) — a fixed timeout guessing how
        # long that takes will eventually guess wrong and undershoot. So
        # instead: keep re-snapping to the bottom on every extent change
        # for as long as we're "pinned" (started there and never manually
        # scrolled away), with no expiry — only unpinning (on
        # "value-changed") when the resulting value is no longer at the
        # bottom, i.e. the user actually scrolled up themselves.
        vadj = self.transcript_scroller.get_vadjustment()
        vadj.connect("changed", self._on_transcript_extent_changed)
        vadj.connect("value-changed", self._on_transcript_value_changed)

        # --- Context preview — only visible once "Attach context" is toggled on ---
        self.context_preview = Gtk.Label(xalign=0, wrap=True)
        self.context_preview.add_css_class("dim-label")
        self.context_preview.add_css_class("caption")
        self.context_preview.set_visible(False)
        self.context_preview.set_margin_start(6)
        self.context_preview.set_margin_end(6)
        self.context_preview.set_margin_top(6)

        # --- Input row ---
        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_row.set_vexpand(True)
        input_row.set_margin_top(10)
        input_row.set_margin_bottom(6)
        input_row.set_margin_start(6)
        input_row.set_margin_end(6)

        self.attach_context_button = Gtk.ToggleButton(icon_name="edit-select-all-symbolic")
        self.attach_context_button.set_tooltip_text(
            _("Attach terminal context (current selection, or last 20 lines of output)")
        )
        self.attach_context_button.set_valign(Gtk.Align.END)
        self.attach_context_button.connect("toggled", self._on_attach_context_toggled)
        input_row.append(self.attach_context_button)

        self.input_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, hexpand=True, vexpand=True)
        input_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroller.set_child(self.input_view)
        # Deliberately no set_max_content_height/set_propagate_natural_height
        # here — the input area no longer grows with what you type. Its
        # height is fixed at whatever the chat_paned handle below is set
        # to (dragged by the user, or the default set right after this),
        # and a ScrolledWindow takes over once content exceeds that.
        input_row.append(input_scroller)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self._on_input_key_pressed)
        self.input_view.add_controller(key_controller)

        self.send_button = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_button.set_tooltip_text(_("Send"))
        self.send_button.set_valign(Gtk.Align.END)
        self.send_button.connect("clicked", self._on_send_clicked)
        input_row.append(self.send_button)

        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom_box.append(self.context_preview)
        bottom_box.append(input_row)

        # Transcript above, input below, with a draggable handle between
        # them instead of the input area auto-growing with its content —
        # resize it by hand if you want more room, it stays put otherwise.
        self.chat_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.chat_paned.set_vexpand(True)
        self.chat_paned.set_start_child(self.transcript_scroller)
        self.chat_paned.set_resize_start_child(True)
        self.chat_paned.set_shrink_start_child(True)
        self.chat_paned.set_end_child(bottom_box)
        self.chat_paned.set_resize_end_child(False)
        self.chat_paned.set_shrink_end_child(False)
        self.append(self.chat_paned)

        # A fresh Paned splits 50/50 by default, which would squeeze an
        # empty transcript down to make room for an oversized blank input
        # area — give the input area a sensible fixed starting height
        # instead, the first (and only the first) time the panel actually
        # becomes visible. The panel starts hidden (see window.py) and may
        # stay that way for the whole session, so this waits for "map"
        # rather than trying unconditionally from __init__ — an unbounded
        # GLib.idle_add polling a height that never becomes positive
        # because the widget is never shown would just spin forever,
        # pegging a CPU core. The retry-with-a-cap poll below (same
        # pattern as _build_pane_layout_widget's try_center in window.py)
        # is only a short-lived fallback for the rare case "map" fires a
        # frame before the real height is settled.
        default_input_height = 96
        paned_position_state = {"initialized": False}
        def _on_chat_paned_map(_widget):
            if paned_position_state["initialized"]:
                return
            paned_position_state["initialized"] = True
            attempts = [0]
            def try_apply():
                total = self.chat_paned.get_height()
                if total > 0:
                    self.chat_paned.set_position(max(0, total - default_input_height))
                    return False
                attempts[0] += 1
                return attempts[0] < 30  # ~0.5s ceiling, then give up quietly
            GLib.timeout_add(16, try_apply)
        self.chat_paned.connect("map", _on_chat_paned_map)

    # --- Provider switching ---

    def set_active_provider(self, provider_id):
        """Switches which provider handles the *next* message. Deliberately
        does not touch self.history — the conversation continues across
        provider switches, per the confirmed product behavior. provider_id
        prefixed "cli:" routes to a local CLI tool instead of an HTTP API
        (see cli_providers.py). Which provider is active is shown by which
        button in provider_button_box is pressed (see
        window.py's _on_ai_provider_button_toggled) — nothing to reflect
        here beyond the id itself."""
        self.active_provider_id = provider_id
        self.settings_manager.set("ai.active_provider", provider_id)
        self.settings_manager.save()

    # --- Chat management (new / delete / switch / persist) ---

    def _build_chat_history_popover(self):
        """The chat_picker_button's popover: a search entry (plain regex —
        see ai_chat_store.search_chats, no AI involved) over a scrollable
        list of saved chats, newest first. Repopulated fresh every time
        it's opened, and again on every keystroke in the search entry."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_size_request(260, -1)

        self._chat_search_entry = Gtk.SearchEntry()
        self._chat_search_entry.set_placeholder_text(_("Search chats"))
        self._chat_search_entry.connect("search-changed", self._on_chat_search_changed)
        box.append(self._chat_search_entry)

        self._chat_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(320)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(self._chat_list_box)
        box.append(scroller)

        self._chat_history_popover = Gtk.Popover()
        self._chat_history_popover.set_child(box)
        self._chat_history_popover.connect("show", self._on_chat_history_popover_show)
        self.chat_picker_button.set_popover(self._chat_history_popover)

    def _on_chat_history_popover_show(self, _popover):
        self._chat_search_entry.set_text("")
        self._chat_search_entry.grab_focus()
        self._populate_chat_history_list(list_chats())

    def _on_chat_search_changed(self, entry):
        query = entry.get_text().strip()
        self._populate_chat_history_list(list_chats() if not query else search_chats(query))

    def _populate_chat_history_list(self, chats):
        child = self._chat_list_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._chat_list_box.remove(child)
            child = next_child

        if not chats:
            self._chat_list_box.append(Gtk.Label(label=_("No chats found."), xalign=0))
            return

        for chat in chats:
            row_button = Gtk.Button()
            row_button.add_css_class("flat")
            label = Gtk.Label(label=chat["title"] or _("Untitled chat"), xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            row_button.set_child(label)

            def on_pick(_b, chat_id=chat["id"]):
                self._chat_history_popover.popdown()
                self._load_chat(chat_id)
            row_button.connect("clicked", on_pick)
            self._chat_list_box.append(row_button)

    def _clear_transcript(self):
        child = self.transcript_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.transcript_box.remove(child)
            child = next_child
        # Already gone along with everything else above — drop the
        # reference so a reply that arrives later doesn't try to remove an
        # already-removed widget.
        self._typing_indicator = None
        self.attach_context_button.set_active(False)
        # Whatever chat we're switching to/from has no request in flight
        # *for it* right now (on_send_clicked always disables this only
        # for the chat active at send time, and re-enables it when that
        # chat's reply lands while it's still the current one) — so it
        # should never be stuck disabled just because a different chat's
        # request happens to still be pending in the background.
        self.send_button.set_sensitive(True)

    def _refresh_chat_picker_label(self):
        self.chat_picker_label.set_label(self.current_chat_title or _("New chat"))

    def _start_new_chat(self):
        """Resets to a fresh, unsaved chat — nothing is written to disk
        until (and unless) a first exchange actually completes."""
        self.current_chat_id = None
        self.current_chat_title = ""
        self.history = []
        self._clear_transcript()
        self._refresh_chat_picker_label()

    def _on_new_chat_clicked(self, _button):
        self._start_new_chat()

    def _on_delete_chat_clicked(self, _button):
        """Deletes the current chat's file (if it was ever saved) and
        opens a fresh one — never just clears the screen with the file
        left behind."""
        if self.current_chat_id:
            delete_chat(self.current_chat_id)
            if self.current_chat_id in self._pending_request_chat_ids:
                # Its own request is still out there and will try to save
                # a reply once it lands — make sure that doesn't quietly
                # bring the just-deleted file back.
                self._deleted_chat_ids.add(self.current_chat_id)
        self._start_new_chat()

    def _load_chat(self, chat_id):
        data = load_chat(chat_id)
        if not data:
            return
        self.current_chat_id = data.get("id") or chat_id
        self.current_chat_title = data.get("title") or ""
        self.history = data.get("messages") or []
        self._clear_transcript()
        for message in self.history:
            self._append_bubble(message.get("role"), message.get("content", ""),
                                 provider_id=message.get("provider_id"))
        self._refresh_chat_picker_label()
        if self.current_chat_id in self._pending_request_chat_ids:
            # Still waiting on a reply from before we navigated away —
            # show that again rather than looking like nothing was asked.
            self._typing_indicator = self._build_typing_indicator()
            self.transcript_box.append(self._typing_indicator)
            self._typing_indicator.connect("map", self._on_new_message_mapped)
            self._force_relayout()
            self._scroll_to_bottom()

    def _sanitize_title(self, text):
        title = (text or "").strip().strip("\"'").strip()
        title = title.split("\n")[0].strip()
        if len(title) > 60:
            title = title[:60].rstrip() + "…"
        return title

    def _maybe_generate_title_for(self, chat_id, title, messages, provider_id):
        """Fires a one-off, side-channel request asking the provider that
        just answered for a short title — never touches self.history or
        the visible transcript, just the given chat's saved title. Takes
        an explicit chat_id/title/messages (rather than reading
        self.current_chat_id/self.history) since the exchange that
        triggers this may belong to a chat the user has since navigated
        away from — this still has to name the right one. Only once per
        chat (right after its first exchange); silently does nothing if
        it fails, since an untitled chat is a cosmetic issue, not a
        functional one."""
        if title or len(messages) < 2:
            return

        title_request = _(
            "Reply with ONLY a short 3-6 word title summarizing what this conversation "
            "is about. No quotes, no trailing punctuation, no explanation — just the title."
        )
        transcript_for_title = list(messages) + [{"role": "user", "content": title_request}]
        system_prompt = self.settings_manager.get("ai.system_prompt")
        timeout = self.settings_manager.get("ai.request_timeout_seconds")

        def on_title_success(text):
            new_title = self._sanitize_title(text)
            if not new_title:
                return False
            # Re-read from disk rather than trusting `messages` is still
            # the full/latest content — harmless either way if it is, and
            # correct if more exchanges completed for this chat (current
            # or not) while this title request was in flight.
            saved = load_chat(chat_id)
            latest_messages = (saved or {}).get("messages") or messages
            save_chat(chat_id, new_title, latest_messages)
            if chat_id == self.current_chat_id:
                self.current_chat_title = new_title
                self._refresh_chat_picker_label()
            return False

        def on_title_error(_message):
            return False  # untitled is fine — not worth surfacing as an error bubble

        if provider_id.startswith("cli:"):
            config = resolve_cli_config(provider_id, self.settings_manager)
            if config:
                run_cli_chat(provider_id, config["command"], system_prompt, transcript_for_title,
                             on_title_success, on_title_error, timeout=timeout, model=config.get("model"))
        else:
            config = resolve_provider_config(provider_id, self.settings_manager, self.keyring)
            if config:
                send_chat_request(provider_id, config["api_key"], config["base_url"], config["model"],
                                   system_prompt, transcript_for_title, on_title_success, on_title_error,
                                   timeout=timeout)

    # --- Attach-context ---

    def _on_attach_context_toggled(self, button):
        if not button.get_active():
            self._pending_context = None
            self.context_preview.set_visible(False)
            return
        snippet = self.parent_window.get_terminal_context_snippet()
        if not snippet:
            self._pending_context = None
            self.context_preview.set_visible(False)
            button.set_active(False)
            return
        self._pending_context = snippet
        preview = snippet if len(snippet) < 200 else snippet[:200] + "…"
        self.context_preview.set_label(_("Context to attach:") + "\n" + preview)
        self.context_preview.set_visible(True)

    # --- Sending ---

    def _on_input_key_pressed(self, controller, keyval, keycode, state):
        # Enter sends; Shift+Enter inserts a newline.
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (state & Gdk.ModifierType.SHIFT_MASK):
            self._on_send_clicked(None)
            return True
        return False

    def _on_send_clicked(self, _button):
        buf = self.input_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        if not text and not self._pending_context:
            return
        if not self.active_provider_id:
            self._append_error_bubble(_("No AI provider selected."))
            return

        is_cli = self.active_provider_id.startswith("cli:")
        if is_cli:
            config = resolve_cli_config(self.active_provider_id, self.settings_manager)
        else:
            config = resolve_provider_config(self.active_provider_id, self.settings_manager, self.keyring)
        if not config:
            self._append_error_bubble(_("This provider is no longer configured."))
            return
        if not is_cli and not config["api_key"] and not self.active_provider_id.startswith("custom:"):
            self._append_error_bubble(_("No API key configured for {label}.").format(label=config["label"]))
            return

        outgoing = text
        if self._pending_context:
            block = f"```\n{self._pending_context}\n```"
            outgoing = f"{text}\n\n{block}" if text else block

        self._append_bubble("user", outgoing)
        self.history.append({"role": "user", "content": outgoing})

        # Assigned now (not lazily once the reply arrives) and captured
        # into locals the callbacks close over — the user is free to
        # switch to a different chat (or start a new one) while this
        # request is still in flight, and self.current_chat_id/
        # self.history/self.current_chat_title will then point at
        # whatever they switched *to* by the time the reply lands. Without
        # a stable snapshot of "which chat, and what was in it, when this
        # was actually sent", a reply could land in the wrong chat's file,
        # get appended to the wrong on-screen transcript, or (having
        # nothing left to record it against) just be lost the moment the
        # user navigated away before it arrived.
        if self.current_chat_id is None:
            self.current_chat_id = new_chat_id()
        target_chat_id = self.current_chat_id
        target_chat_title_snapshot = self.current_chat_title
        outgoing_history_snapshot = list(self.history)

        # Saved right away, with just the question — not only once the
        # reply comes back. Otherwise a brand-new chat has no file at all
        # while its first request is in flight, so switching away from it
        # ("Thinking…") and then trying to come back via history finds
        # nothing to open until the reply happens to land.
        save_chat(target_chat_id, target_chat_title_snapshot, outgoing_history_snapshot)
        self._pending_request_chat_ids.add(target_chat_id)

        buf.set_text("")
        self.attach_context_button.set_active(False)
        self.send_button.set_sensitive(False)

        self._typing_indicator = self._build_typing_indicator()
        self.transcript_box.append(self._typing_indicator)
        self._typing_indicator.connect("map", self._on_new_message_mapped)
        self._force_relayout()
        self._scroll_to_bottom()

        system_prompt = self.settings_manager.get("ai.system_prompt")
        timeout = self.settings_manager.get("ai.request_timeout_seconds")

        # Captured now, not re-read from self.active_provider_id inside the
        # callback — the conversation is shared across providers, and the
        # user can switch to a different one while this request is still in
        # flight. The avatar on the reply must reflect whichever provider
        # actually answered *this* message.
        answering_provider_id = self.active_provider_id

        def on_success(reply_text):
            self._pending_request_chat_ids.discard(target_chat_id)
            if target_chat_id in self._deleted_chat_ids:
                self._deleted_chat_ids.discard(target_chat_id)
                return False  # deleted while its own request was still in flight — don't resurrect it
            reply_message = {"role": "assistant", "content": reply_text, "provider_id": answering_provider_id}
            is_current = target_chat_id == self.current_chat_id
            if is_current:
                self._remove_typing_indicator()
                self.history.append(reply_message)
                self._append_bubble("assistant", reply_text, provider_id=answering_provider_id)
                self.send_button.set_sensitive(True)
                save_chat(target_chat_id, self.current_chat_title, self.history)
                self._maybe_generate_title_for(
                    target_chat_id, self.current_chat_title, self.history, answering_provider_id
                )
            else:
                # The user has since switched away — persist quietly to
                # *that* chat's file without touching whatever's on screen
                # now. Re-opening it from history later shows the
                # completed exchange, same as if it had been there all
                # along.
                full_messages = outgoing_history_snapshot + [reply_message]
                save_chat(target_chat_id, target_chat_title_snapshot, full_messages)
                self._maybe_generate_title_for(
                    target_chat_id, target_chat_title_snapshot, full_messages, answering_provider_id
                )
            return False

        def on_error(message):
            self._pending_request_chat_ids.discard(target_chat_id)
            self._deleted_chat_ids.discard(target_chat_id)
            if target_chat_id == self.current_chat_id:
                self._remove_typing_indicator()
                self._append_error_bubble(message)
                self.send_button.set_sensitive(True)
            # Nothing to persist for an away chat — errors aren't part of
            # the saved message history either way.
            return False

        if is_cli:
            # Each CLI call is still a fresh, memory-less process, but the
            # full history is replayed as a formatted transcript every
            # time (see cli_providers._format_conversation) so it doesn't
            # "forget" what was just being discussed — same continuity the
            # API path already gets via outgoing_history_snapshot below.
            run_cli_chat(
                self.active_provider_id, config["command"], system_prompt, outgoing_history_snapshot,
                on_success, on_error, timeout=timeout, model=config.get("model"),
            )
        else:
            send_chat_request(
                self.active_provider_id, config["api_key"], config["base_url"], config["model"],
                system_prompt, outgoing_history_snapshot, on_success, on_error, timeout=timeout,
            )

    # --- Transcript rendering ---

    def _build_typing_indicator(self):
        # Deliberately more prominent than a regular reply bubble (bigger
        # spinner, bold non-dimmed label, stronger background) — it's a
        # transient status, not conversation content, and needs to read as
        # "something is happening" at a glance, not blend into the flow.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.add_css_class("ai-typing-indicator")
        box.set_halign(Gtk.Align.START)
        spinner = Gtk.Spinner()
        spinner.set_size_request(20, 20)
        spinner.start()
        box.append(spinner)
        label = Gtk.Label(label=_("Thinking…"))
        label.add_css_class("heading")
        box.append(label)
        return box

    def _remove_typing_indicator(self):
        if self._typing_indicator is not None:
            self.transcript_box.remove(self._typing_indicator)
            self._typing_indicator = None

    def _resolve_provider_label(self, provider_id):
        if not provider_id:
            return None
        if provider_id.startswith("cli:"):
            config = resolve_cli_config(provider_id, self.settings_manager)
        else:
            config = resolve_provider_config(provider_id, self.settings_manager, self.keyring)
        return config["label"] if config else provider_id

    def _build_avatar(self, role, provider_id=None):
        """A small identity marker for the start of a message bubble: a
        generic person icon for the user, or — since this is one shared
        conversation that can span several providers — whichever provider
        actually answered *this specific* message (real logo if
        provider_badges.py has one, otherwise bold initials on a tinted
        background, same visual language as the header-bar buttons)."""
        if role == "user":
            avatar = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
            avatar.set_pixel_size(18)
            avatar.add_css_class("ai-avatar")
            avatar.add_css_class("ai-avatar-user")
            return avatar

        icon_name = icon_name_for(provider_id) if provider_id else None
        family = badge_family(provider_id) if provider_id else "custom"
        if icon_name:
            avatar = Gtk.Image.new_from_icon_name(icon_name)
            avatar.set_pixel_size(18)
            avatar.add_css_class("ai-avatar")
            avatar.add_css_class(f"ai-provider-badge-{family}")
            return avatar

        label = self._resolve_provider_label(provider_id) or "AI"
        avatar = Gtk.Label(label=badge_text(label))
        avatar.add_css_class("ai-avatar")
        avatar.add_css_class("ai-avatar-badge")
        avatar.add_css_class(f"ai-provider-badge-{family}")
        return avatar

    def _append_bubble(self, role, content, provider_id=None):
        # The avatar used to sit in its own column to the left of the
        # bubble (min 22px + 8px spacing) — a fixed ~30px tax on every
        # single message in a panel that's often only ~220px wide to
        # begin with. It's now anchored inline as the first character of
        # the message's own first line (see render_markdown_into_box's
        # leading_widget), so it costs neither an extra column nor an
        # extra row — text wraps back to the left margin exactly as it
        # would after any other leading character.
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.set_hexpand(True)
        bubble.add_css_class("ai-bubble-user" if role == "user" else "ai-bubble-assistant")

        avatar = self._build_avatar(role, provider_id)
        render_markdown_into_box(bubble, content, leading_widget=avatar)

        self.transcript_box.append(bubble)
        # "map" fires exactly when GTK first gives this bubble a real
        # allocation — a more precisely-timed hook than guessing a delay,
        # on top of the belt-and-suspenders polling in _scroll_to_bottom.
        bubble.connect("map", self._on_new_message_mapped)
        self._force_relayout()
        self._scroll_to_bottom()

    def _append_error_bubble(self, message):
        label = Gtk.Label(label=message, xalign=0, wrap=True)
        label.add_css_class("error")
        self.transcript_box.append(label)
        label.connect("map", self._on_new_message_mapped)
        self._force_relayout()
        self._scroll_to_bottom()

    def _on_new_message_mapped(self, widget):
        if self._pinned_to_bottom:
            self._snap_to_bottom_now()

    def _force_relayout(self):
        """Manually dragging the panel's divider (an actual external width
        change) reliably fixes both a stray oversized gap in a freshly-
        appended bubble AND a scroll position stuck mid-message — GTK
        clearly recomputes correctly once genuinely asked to, it just
        doesn't always seem to ask itself to on a plain child-append. This
        explicitly requests exactly that same full re-measure/re-allocate
        GTK does for a real resize, so new content doesn't have to wait
        for the user to nudge the divider to render correctly."""
        self.transcript_box.queue_resize()
        self.queue_resize()

    def _on_transcript_extent_changed(self, adjustment):
        """Fires whenever the scrollable extent itself changes (a bubble's
        real height finally getting negotiated, possibly several layout
        passes after it was appended). No expiry — keeps snapping to the
        new bottom for as long as we're pinned there."""
        if self._pinned_to_bottom:
            self._snap_to_bottom_now()

    def _on_transcript_value_changed(self, adjustment):
        """Fires on ANY scroll position change — ours or the user's. Used
        purely to detect whether the user has since scrolled away from the
        bottom (in which case we stop yanking them back down on the next
        extent change) or is still at/returned to the bottom."""
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        self._pinned_to_bottom = adjustment.get_value() >= bottom - 2  # small epsilon for float rounding

    def _snap_to_bottom_now(self):
        adj = self.transcript_scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def _scroll_to_bottom(self):
        """Re-pins to the bottom (e.g. after the user sends a new message,
        even if they'd scrolled up to read earlier history). Jumps there
        immediately for the common case where the extent is already
        settled; _on_transcript_extent_changed keeps re-snapping
        indefinitely afterward for as long as still pinned; and each new
        row's own "map" signal (_on_new_message_mapped) does the same the
        instant GTK actually gives it a real allocation — a more precisely
        -timed hook than any guessed delay.

        On top of all that: a reply with a lot of content (long text,
        several paragraphs, a code block — everything nested one level
        deeper now that each message is an [avatar, bubble] row) can take
        *several* real GTK layout passes before its height is truly final,
        since word-wrap needs an allocated width before it can even
        compute a height, and a scrollbar appearing/disappearing shifts
        that available width again. So on every send, also kick off a
        short-lived continuous poll (~50ms ticks, ~15s ceiling) that keeps
        re-snapping regardless — belt, suspenders, and a spare belt."""
        self._pinned_to_bottom = True
        self._snap_to_bottom_now()
        self._scroll_reinforce_generation = getattr(self, "_scroll_reinforce_generation", 0) + 1
        self._scroll_reinforce_ticks_left = 300
        GLib.timeout_add(50, self._reinforce_scroll_tick, self._scroll_reinforce_generation)

    def _reinforce_scroll_tick(self, generation):
        if generation != self._scroll_reinforce_generation:
            return False  # a newer _scroll_to_bottom() call has taken over
        if not self._pinned_to_bottom:
            return False  # the user scrolled away — stop chasing them
        if self._scroll_reinforce_ticks_left > 290:  # first ~500ms only — see _force_relayout
            self._force_relayout()
        self._snap_to_bottom_now()
        self._scroll_reinforce_ticks_left -= 1
        return self._scroll_reinforce_ticks_left > 0
