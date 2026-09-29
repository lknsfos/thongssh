# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos
"""GTK integration checks: python3 -m unittest discover -s tests -v.

Requires PyGObject, GTK4/libadwaita and a display (or xvfb-run).
"""

import time
import unittest

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

from thongssh_gtk.tab_colors import TabColors


class TabColorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest("GTK display unavailable")
        Adw.init()

    def setUp(self):
        self.window = Adw.Window(default_width=600, default_height=160)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.set_content(self.box)
        self.data = {}
        self.views = []
        self.controllers = []
        for _ in range(2):
            view = Adw.TabView()
            bar = Adw.TabBar()
            bar.set_view(view)
            bar.set_autohide(False)
            self.box.append(bar)
            self.box.append(view)
            self.views.append(view)
            self.controllers.append(TabColors(bar, view, self.data))
        self.page = self.views[0].append(Gtk.Box())
        self.page.set_title("Same title")
        self.icon = Gio.ThemedIcon.new("utilities-terminal-symbolic")
        self.page.set_icon(self.icon)
        self.other = self.views[0].append(Gtk.Box())
        self.other.set_title("Same title")
        self.data[self.page] = {"tab_color": "#c01c28"}
        self.data[self.other] = {"tab_color": "#f9f06b"}
        self.window.present()
        self.pump()

    def tearDown(self):
        self.window.destroy()
        self.pump()

    def pump(self):
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            time.sleep(.005)

    def styles(self, controller, page):
        return [(widget, color) for widget, (color, _provider) in controller.styled.items()
                if widget.get_property("page") == page]

    def test_identity_survives_reorder_pin_and_transfer(self):
        first, second = self.controllers
        self.views[0].reorder_page(self.page, 1)
        self.pump()
        self.assertEqual(self.styles(first, self.page)[0][1], "#c01c28")
        self.views[0].set_page_pinned(self.page, True)
        self.pump()
        self.assertEqual(self.styles(first, self.page)[0][1], "#c01c28")
        self.views[0].transfer_page(self.page, self.views[1], 0)
        self.pump()
        self.assertFalse(self.styles(first, self.page))
        self.assertEqual(self.styles(second, self.page)[0][1], "#c01c28")
        self.assertTrue(self.page.get_icon().equal(self.icon))

    def test_contrast_and_reset(self):
        controller = self.controllers[0]
        dark_tab = self.styles(controller, self.page)[0][0]
        light_tab = self.styles(controller, self.other)[0][0]
        self.assertGreater(dark_tab.get_style_context().get_color().red, .9)
        self.assertLess(light_tab.get_style_context().get_color().red, .1)
        self.page.set_indicator_icon(Gio.ThemedIcon.new("network-offline-symbolic"))
        self.data[self.page]["tab_color"] = None
        controller.queue_refresh()
        self.pump()
        self.assertFalse(self.styles(controller, self.page))
        self.assertIsNotNone(self.page.get_indicator_icon())
        self.assertTrue(self.page.get_icon().equal(self.icon))

    def test_unmap_cancels_refresh_and_remap_restores_color(self):
        controller = self.controllers[0]
        controller.queue_refresh()
        self.window.set_visible(False)
        self.pump()
        self.assertFalse(controller.styled)
        self.assertEqual(controller.pending, 0)
        controller.queue_refresh()
        self.assertEqual(controller.pending, 0)
        self.window.present()
        self.pump()
        self.assertEqual(self.styles(controller, self.page)[0][1], "#c01c28")


if __name__ == "__main__":
    unittest.main()
