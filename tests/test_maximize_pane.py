# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos
"""GTK check: python3 -m unittest discover -s tests -v.

Ctrl+Shift+X and the pane button (toggle maximize active pane): hides every other pane in the
split's Gtk.Paned tree, and the second press shows them again, keeping the
divider positions.
"""
import unittest
from types import SimpleNamespace

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

from thongssh_gtk.window import ThongSSHWindow


def paned(orientation, start, end):
    p = Gtk.Paned(orientation=orientation)
    p.set_start_child(start)
    p.set_end_child(end)
    return p


def make_window(split_mode, active_index):
    """Stand-in for ThongSSHWindow with only what the toggle uses."""
    boxes = [Gtk.Box() for _ in range(4)]
    tabviews = [object() for _ in range(4)]
    win = SimpleNamespace(
        split_mode=split_mode,
        _maximize_hidden=[],
        _pane_maximize_buttons=[Gtk.Button()],
        _pane_box_by_tabview=dict(zip(tabviews, boxes)),
        _get_active_tabview=lambda: tabviews[active_index],
    )
    for name in ("_toggle_maximize_pane", "_restore_maximized_pane", "_update_maximize_buttons"):
        setattr(win, name, getattr(ThongSSHWindow, name).__get__(win))
    return win, boxes


class MaximizePaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest("GTK display unavailable")

    def visible(self, widgets):
        return [w.get_visible() for w in widgets]

    def test_grid_maximize_and_restore(self):
        win, (b0, b1, b2, b3) = make_window("grid", active_index=3)
        left = paned(Gtk.Orientation.VERTICAL, b0, b2)
        right = paned(Gtk.Orientation.VERTICAL, b1, b3)
        root = paned(Gtk.Orientation.HORIZONTAL, left, right)
        root.set_position(123)

        win._toggle_maximize_pane()
        self.assertEqual(self.visible([b0, b1, b2, b3]), [True, False, True, True])
        self.assertFalse(left.get_visible())  # whole left column hidden
        self.assertTrue(right.get_visible())

        win._toggle_maximize_pane()
        self.assertEqual(self.visible([b0, b1, b2, b3, left, right]), [True] * 6)
        self.assertEqual(win._maximize_hidden, [])
        self.assertEqual(root.get_position(), 123)

    def test_two_way_split(self):
        win, (b0, b1, _b2, _b3) = make_window("vertical", active_index=0)
        root = paned(Gtk.Orientation.HORIZONTAL, b0, b1)  # noqa: F841 — keep the tree alive
        button = win._pane_maximize_buttons[0]
        win._toggle_maximize_pane()
        self.assertEqual(self.visible([b0, b1]), [True, False])
        self.assertEqual(button.get_icon_name(), "view-restore-symbolic")
        win._toggle_maximize_pane()
        self.assertEqual(self.visible([b0, b1]), [True, True])
        self.assertEqual(button.get_icon_name(), "view-fullscreen-symbolic")
        self.assertTrue(button.get_visible())

    def test_no_split_is_noop(self):
        win, (b0, *_rest) = make_window(None, active_index=0)
        win._toggle_maximize_pane()
        self.assertTrue(b0.get_visible())
        self.assertEqual(win._maximize_hidden, [])
        win._update_maximize_buttons()
        self.assertFalse(win._pane_maximize_buttons[0].get_visible())


if __name__ == "__main__":
    unittest.main()
