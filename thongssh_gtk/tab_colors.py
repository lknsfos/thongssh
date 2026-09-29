# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos
"""Per-page backgrounds for native Adw.TabBar tabs.

Libadwaita has no public per-page background API. Keep the small adapter
to its internal `tab` widgets here, and identify them by their page
property rather than position or title (both change during transfers).
"""

from gi.repository import Gtk, GLib


class TabColors:
    def __init__(self, bar, view, tab_data):
        self.bar = bar
        self.view = view
        self.tab_data = tab_data
        self.pending = 0
        self.styled = {}
        view._tab_colors = self
        for signal in ("page-attached", "page-detached", "page-reordered",
                       "notify::n-pinned-pages"):
            view.connect(signal, self.queue_refresh)
        bar.connect("map", self.queue_refresh)
        bar.connect("unmap", self._unmap)

    def _unmap(self, *_args):
        if self.pending:
            GLib.source_remove(self.pending)
            self.pending = 0
        for widget, (_color, provider) in self.styled.items():
            widget.get_style_context().remove_provider(provider)
        self.styled.clear()

    def queue_refresh(self, *_args):
        # TabBar creates/reparents its internal tabs in the same signals.
        # Defer until all of those handlers have completed.
        if self.bar.get_mapped() and not self.pending:
            self.pending = GLib.idle_add(self.refresh)

    def _tabs(self, widget):
        if widget.get_css_name() == "tab" and widget.find_property("page"):
            yield widget
        child = widget.get_first_child()
        while child is not None:
            yield from self._tabs(child)
            child = child.get_next_sibling()

    def refresh(self):
        self.pending = 0
        if not self.bar.get_mapped():
            return GLib.SOURCE_REMOVE
        tabs = set(self._tabs(self.bar))
        pages = {self.view.get_nth_page(i) for i in range(self.view.get_n_pages())}
        for widget in tabs | set(self.styled):
            page = widget.get_property("page") if widget in tabs else None
            # A departing tab can remain in the widget tree during its
            # animation. Do not retain its provider/page after a transfer.
            color = self.tab_data.get(page, {}).get("tab_color") if page in pages else None
            previous, provider = self.styled.get(widget, (None, None))
            if previous == color:
                continue
            context = widget.get_style_context()
            if provider is not None:
                context.remove_provider(provider)
                del self.styled[widget]
            if not color:
                continue
            # Choose black or white using WCAG relative luminance, keeping
            # labels and symbolic icons readable even for very pale colors.
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4
                      for c in channels]
            luminance = sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
            foreground = "#000000" if luminance > .179 else "#ffffff"
            # Shade away from the foreground, never toward it: a white
            # overlay under white text can otherwise erase its contrast.
            shade = "#ffffff" if foreground == "#000000" else "#000000"
            css = f"""
                tab {{ background-color: {color}; background-image: none;
                       color: {foreground}; }}
                tab:hover {{ background-image: linear-gradient(
                    alpha({shade}, .10), alpha({shade}, .10)); }}
                tab:selected, tab:checked {{
                    box-shadow: inset 0 -3px {foreground};
                    background-image: linear-gradient(
                        alpha({shade}, .16), alpha({shade}, .16));
                }}
            """
            provider = Gtk.CssProvider()
            provider.load_from_data(css.encode())
            context.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            self.styled[widget] = (color, provider)
        return GLib.SOURCE_REMOVE
