# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos
"""GTK check: python3 -m unittest discover -s tests -v.

Issue #12: Ctrl+Shift+C/V must act on the terminal that got the key,
not on the handler-owning window's active terminal (stale after detach).
Uses a Vte.Terminal subclass that records calls, so the real clipboard is
never touched."""
import unittest
from types import SimpleNamespace

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, Gdk, Vte

from thongssh_gtk.tab_window_base import TerminalPaneWindow
from thongssh_gtk.settings import DEFAULT_SETTINGS


class RecordingTerminal(Vte.Terminal):
    def __init__(self):
        super().__init__()
        self.calls = []

    def copy_clipboard_format(self, fmt):
        self.calls.append("copy")

    def paste_clipboard(self):
        self.calls.append("paste")


def make_owner(active_terminal):
    """Stands in for the MAIN window: its active terminal is some other tab."""
    owner = SimpleNamespace(
        settings_manager=SimpleNamespace(get=DEFAULT_SETTINGS.get),
        get_display=Gdk.Display.get_default,
        get_active_terminal=lambda: active_terminal,
    )
    for name in ("_shortcut_matches", "_resolve_latin_letter",
                 "on_menu_copy", "on_menu_paste"):
        setattr(owner, name, getattr(TerminalPaneWindow, name).__get__(owner))
    return owner


class DetachedCopyPasteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest("GTK display unavailable")

    def setUp(self):
        self.detached = RecordingTerminal()
        self.main_active = RecordingTerminal()
        self.controller = Gtk.EventControllerKey.new()
        self.detached.add_controller(self.controller)
        self.owner = make_owner(self.main_active)

    def press(self, keyval):
        mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        return TerminalPaneWindow.on_terminal_key_pressed(
            self.owner, self.controller, keyval, 0, mods)

    def test_copy_uses_key_terminal(self):
        self.assertTrue(self.press(Gdk.KEY_C))
        self.assertEqual(self.detached.calls, ["copy"])
        self.assertEqual(self.main_active.calls, [])

    def test_paste_uses_key_terminal(self):
        self.assertTrue(self.press(Gdk.KEY_V))
        self.assertEqual(self.detached.calls, ["paste"])
        self.assertEqual(self.main_active.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
