import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Gdk

from .ai_providers import send_chat_request, resolve_provider_config
from .cli_providers import run_cli_chat, resolve_cli_config
from .markdown_view import render_markdown_into_box
from .provider_badges import icon_name_for, badge_family, badge_text

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
        self._pending_context = None
        self._typing_indicator = None
        self._pinned_to_bottom = True
        self._scroll_reinforce_generation = 0
        self._scroll_reinforce_ticks_left = 0

        self.set_size_request(220, -1)
        self.add_css_class("ai-panel")

        # --- Header ---
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(6)
        header.set_margin_start(6)
        header.set_margin_end(6)
        header.set_margin_bottom(6)
        self.provider_label = Gtk.Label(label=_("AI Chat"), xalign=0)
        self.provider_label.add_css_class("heading")
        self.provider_label.set_hexpand(True)
        header.append(self.provider_label)

        self.clear_chat_button = Gtk.Button(icon_name="edit-clear-all-symbolic")
        self.clear_chat_button.set_tooltip_text(_("Clear chat"))
        self.clear_chat_button.add_css_class("flat")
        self.clear_chat_button.connect("clicked", self._on_clear_chat_clicked)
        header.append(self.clear_chat_button)

        self.append(header)
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
        self.append(self.transcript_scroller)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Context preview — only visible once "Attach context" is toggled on ---
        self.context_preview = Gtk.Label(xalign=0, wrap=True)
        self.context_preview.add_css_class("dim-label")
        self.context_preview.add_css_class("caption")
        self.context_preview.set_visible(False)
        self.context_preview.set_margin_start(6)
        self.context_preview.set_margin_end(6)
        self.context_preview.set_margin_top(6)
        self.append(self.context_preview)

        # --- Input row ---
        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_row.set_margin_top(10)
        input_row.set_margin_bottom(6)
        input_row.set_margin_start(6)
        input_row.set_margin_end(6)

        self.attach_context_button = Gtk.ToggleButton(icon_name="edit-select-all-symbolic")
        self.attach_context_button.set_tooltip_text(
            _("Attach terminal context (current selection, or last 20 lines of output)")
        )
        self.attach_context_button.connect("toggled", self._on_attach_context_toggled)
        input_row.append(self.attach_context_button)

        self.input_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, hexpand=True)
        input_scroller = Gtk.ScrolledWindow(hexpand=True)
        input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroller.set_child(self.input_view)
        input_scroller.set_max_content_height(120)
        input_scroller.set_propagate_natural_height(True)
        input_row.append(input_scroller)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self._on_input_key_pressed)
        self.input_view.add_controller(key_controller)

        self.send_button = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_button.set_tooltip_text(_("Send"))
        self.send_button.add_css_class("suggested-action")
        self.send_button.connect("clicked", self._on_send_clicked)
        input_row.append(self.send_button)

        self.append(input_row)

    # --- Provider switching ---

    def set_active_provider(self, provider_id):
        """Switches which provider handles the *next* message. Deliberately
        does not touch self.history — the conversation continues across
        provider switches, per the confirmed product behavior. provider_id
        prefixed "cli:" routes to a local CLI tool instead of an HTTP API
        (see cli_providers.py)."""
        self.active_provider_id = provider_id
        if provider_id.startswith("cli:"):
            config = resolve_cli_config(provider_id, self.settings_manager)
        else:
            config = resolve_provider_config(provider_id, self.settings_manager, self.keyring)
        self.provider_label.set_label(config["label"] if config else provider_id)
        self.settings_manager.set("ai.active_provider", provider_id)
        self.settings_manager.save()

    # --- Reset ---

    def _on_clear_chat_clicked(self, _button):
        """Wipes the conversation — both the in-memory history sent to the
        provider on the next message, and the visible transcript."""
        self.history = []
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
            self._remove_typing_indicator()
            self.history.append({"role": "assistant", "content": reply_text, "provider_id": answering_provider_id})
            self._append_bubble("assistant", reply_text, provider_id=answering_provider_id)
            self.send_button.set_sensitive(True)
            return False

        def on_error(message):
            self._remove_typing_indicator()
            self._append_error_bubble(message)
            self.send_button.set_sensitive(True)
            return False

        if is_cli:
            # Each CLI call is still a fresh, memory-less process, but the
            # full history is replayed as a formatted transcript every
            # time (see cli_providers._format_conversation) so it doesn't
            # "forget" what was just being discussed — same continuity the
            # API path already gets via list(self.history) below.
            run_cli_chat(
                self.active_provider_id, config["command"], system_prompt, list(self.history),
                on_success, on_error, timeout=timeout, model=config.get("model"),
            )
        else:
            send_chat_request(
                self.active_provider_id, config["api_key"], config["base_url"], config["model"],
                system_prompt, list(self.history), on_success, on_error, timeout=timeout,
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
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_valign(Gtk.Align.START)
        avatar = self._build_avatar(role, provider_id)
        avatar.set_valign(Gtk.Align.START)
        row.append(avatar)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.set_hexpand(True)
        bubble.add_css_class("ai-bubble-user" if role == "user" else "ai-bubble-assistant")
        render_markdown_into_box(bubble, content)
        row.append(bubble)

        self.transcript_box.append(row)
        # "map" fires exactly when GTK first gives this row a real
        # allocation — a more precisely-timed hook than guessing a delay,
        # on top of the belt-and-suspenders polling in _scroll_to_bottom.
        row.connect("map", self._on_new_message_mapped)
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
