# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""Small reusable GTK widgets shared across window.py and dialogs.py.

A separate module (not window.py) specifically because dialogs.py already
gets imported BY window.py (InputDialog, HostDialog, ...) — anything meant
to be shared with dialogs.py can't live in window.py itself without a
circular import.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GObject

from .constants import WATERMARK_POSITIONS

from .i18n import _

# Ignored on their own while listening for a new shortcut — a bare modifier
# press isn't a combination yet, just wait for the actual key that goes
# with it. (Caps/Num/Scroll Lock deliberately excluded from this list even
# though they're technically modifiers too — capturing e.g. "Ctrl+W" while
# Caps Lock happens to be on shouldn't require it stay on forever after.)
_MODIFIER_ONLY_KEYVALS = {
    Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
}


class ShortcutPicker(Gtk.Button):
    """A button that captures a new keyboard shortcut when clicked: click
    it, then press the desired key combination (Escape cancels, leaving
    the previous value in place). Shows the current shortcut as its own
    label (e.g. "Ctrl+W"). Read/written as a Gtk accelerator name (e.g.
    "<Control>w", see get_accelerator/set_accelerator) — the same format
    Gtk.accelerator_parse/_name/_get_label already understand, so nothing
    bespoke needs to be invented for storage or for turning it back into a
    display label.

    Used by Settings -> General -> Keyboard Shortcuts, where every
    combination this app used to hardcode (Ctrl+W to close a tab, etc.)
    is configurable instead — several collide with standard shell
    keybindings on some setups (Ctrl+W deletes the last word in bash/
    zsh's own line editing, for one)."""

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, accel=None):
        super().__init__()
        self.add_css_class("flat")
        self._accel = None
        self._listening = False
        self.set_accelerator(accel)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        self.connect("clicked", self._on_clicked)

    def _on_clicked(self, *_args):
        self._listening = True
        self.set_label(_("Press a key combination…"))
        self.grab_focus()

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        if not self._listening:
            return False
        if keyval == Gdk.KEY_Escape:
            self._listening = False
            self._refresh_label()
            return True
        if keyval in _MODIFIER_ONLY_KEYVALS:
            return True  # not a full combination yet — keep listening
        mods = state & Gtk.accelerator_get_default_mod_mask()
        self._listening = False
        self.set_accelerator(Gtk.accelerator_name(keyval, mods))
        return True

    def set_accelerator(self, accel):
        """accel: a Gtk accelerator name (e.g. "<Control>w"), or
        None/"" to clear. Fires "changed" — including when called
        programmatically (e.g. Settings' on_reset), same as PositionGrid's
        set_selected, so callers never need a separate no-signal path."""
        self._accel = accel or None
        self._refresh_label()
        self.emit("changed")

    def get_accelerator(self):
        return self._accel

    def _refresh_label(self):
        if self._accel:
            success, keyval, mods = Gtk.accelerator_parse(self._accel)
            if success:
                self.set_label(Gtk.accelerator_get_label(keyval, mods))
                return
        self.set_label(_("(none)"))


def set_split_button_active_style(split_button, active):
    """Visually marks an Adw.SplitButton as "on" or "off" — the same
    subtle background shading every other toggle-like control in this app
    already gets for free (the header bar's plain Gtk.ToggleButtons, e.g.
    quickies/sidebar), not an accent-colored highlight.

    AdwSplitButton's main segment is a momentary Gtk.Button ("clicked"),
    not a persistent on/off Gtk.ToggleButton — and it's a *final* GObject
    type (confirmed: attempting to subclass it raises "could not create
    new GType"), so it has no real :checked state of its own to reflect
    on/off.

    Two things were tried and rejected before this one:
    - The "suggested-action" style class: a real toggle, just the wrong
      look — bright theme-accent blue, much more prominent than a checked
      toggle button anywhere else in the app.
    - Gtk.StateFlags.CHECKED set directly: gets the exact right look for
      free (Adwaita ships a real "splitbutton.flat:checked" rule, a
      subtle color-mix(currentColor 7%) shade — no custom CSS needed) and
      DOES survive pack_start()+realize when tested in isolation... but
      not reliably once this app's full startup sequence runs around it —
      confirmed live: watermark already enabled at launch (inherited from
      settings, no click involved) left the button unshaded, while
      clicking it during the same session shaded it correctly every time.
      A raw forced state flag isn't something GTK's style/state machinery
      treats as sticky the way a real GtkToggleButton protects its own
      :checked internally — any later, unrelated style recalculation
      elsewhere in the tree (focus, backdrop, notebook page switches, ...)
      can silently clear it, and evidently something during this app's
      real startup does.

    A style CLASS doesn't have that problem — add/remove_css_class is
    stable regardless of what else recalculates around it — so this
    copies Adwaita's own recipe onto a class we control instead of
    relying on the state-flag shortcut."""
    if active:
        split_button.add_css_class("watermark-toggle-active")
    else:
        split_button.remove_css_class("watermark-toggle-active")


class PositionGrid(Gtk.Grid):
    """A 3x3 grid of toggle buttons for picking one of the terminal
    watermark's 9 anchor positions (see constants.WATERMARK_POSITIONS) —
    used both as the header-bar watermark button's quick-picker popover
    (window.py) and as the Settings dialog's inline replacement for the
    old position dropdown (dialogs.py). One shared implementation so the
    two never drift out of sync with each other or with
    constants.WATERMARK_POSITIONS' own id list/ordering."""

    def __init__(self, selected_id=None):
        super().__init__(row_spacing=4, column_spacing=4)
        self.set_row_homogeneous(True)
        self.set_column_homogeneous(True)

        self._buttons = {}
        self._selected_id = selected_id
        self._handlers = []  # callables(position_id), see connect_changed

        first_button = None
        for index, (pos_id, label) in enumerate(WATERMARK_POSITIONS):
            row, col = divmod(index, 3)
            button = Gtk.ToggleButton()
            button.set_size_request(32, 32)
            button.set_tooltip_text(_(label))
            if first_button is None:
                first_button = button
            else:
                # Radio behavior — Gtk.ToggleButton has no built-in
                # exclusivity of its own, set_group() is what makes
                # activating one deactivate the rest.
                button.set_group(first_button)
            button.set_active(pos_id == selected_id)
            button.connect("toggled", self._on_button_toggled, pos_id)
            self._buttons[pos_id] = button
            self.attach(button, col, row, 1, 1)

    def _on_button_toggled(self, button, pos_id):
        if not button.get_active():
            # Fires for the button being deactivated too (set_group's
            # radio behavior) — only the one turning ON should report a
            # change, or every click would report twice (once per button).
            return
        self._selected_id = pos_id
        for handler in self._handlers:
            handler(pos_id)

    def get_selected(self):
        return self._selected_id

    def set_selected(self, pos_id):
        """Set the current position programmatically (e.g. Settings'
        on_reset) — fires connect_changed callbacks same as a real click,
        so callers don't need a separate no-signal code path."""
        button = self._buttons.get(pos_id)
        if button is not None:
            button.set_active(True)

    def connect_changed(self, callback):
        """callback(position_id) — called every time the selected
        position changes, whether by user click or set_selected()."""
        self._handlers.append(callback)
