# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""Small, deliberately-scoped Markdown -> Gtk widgets renderer for AI chat
bubbles. Not full CommonMark — supports fenced code blocks (each rendered as
its own widget with a Copy button), bullet/numbered list lines, and inline
**bold**/*italic*/`code` spans. Built fresh since no markdown rendering
exists anywhere else in the app.

A message is split into segments (plain-text runs vs. fenced code blocks)
and rendered into a caller-supplied Gtk.Box — one child widget per segment
— rather than a single flat Gtk.TextView, specifically so each code block
can carry its own Copy button (plain TextTag styling can't attach a
button to a sub-region). Non-code segments still use Gtk.TextView +
Gtk.TextTag for the same reasons as before: independently scrollable,
selectable, multi-paragraph text with possibly-overlapping emphasis ranges.
"""

import re

from gi.repository import Gtk, Gdk, GLib, Pango

_ = lambda s: s

_INLINE_PATTERN = re.compile(
    r"\*\*(?P<bold>.+?)\*\*|\*(?P<italic>[^*]+?)\*|`(?P<code>[^`]+?)`"
)
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")

_tag_table = None


def _tint(alpha):
    rgba = Gdk.RGBA()
    rgba.parse(f"rgba(0,0,0,{alpha})")
    return rgba


def _get_tag_table():
    """Tag tables are explicitly shareable across multiple Gtk.TextBuffers —
    build the styling once and reuse it for every chat bubble's text
    segments."""
    global _tag_table
    if _tag_table is not None:
        return _tag_table
    table = Gtk.TextTagTable()
    table.add(Gtk.TextTag(name="bold", weight=Pango.Weight.BOLD))
    table.add(Gtk.TextTag(name="italic", style=Pango.Style.ITALIC))
    table.add(Gtk.TextTag(name="inline-code", family="Monospace", background_rgba=_tint(0.08)))
    table.add(Gtk.TextTag(name="list-item", left_margin=18, indent=-12))
    _tag_table = table
    return table


def _split_segments(markdown_text):
    """Splits into [("text", str), ("code", str), ...], in source order.
    An unterminated trailing fence still flushes whatever was collected —
    better to render it as a (fenceless) code block than to silently drop
    it."""
    segments = []
    buf = []
    in_code = False
    for line in (markdown_text or "").split("\n"):
        if line.strip().startswith("```"):
            segments.append(_finalize_segment(in_code, buf))
            buf = []
            in_code = not in_code
            continue
        buf.append(line)
    segments.append(_finalize_segment(in_code, buf))
    return segments


def _finalize_segment(in_code, buf):
    """Trailing blank lines in a *text* segment are just the "\\n\\n"
    formatting separator before a following fence (or leftover from how the
    message was assembled, e.g. text + attached context) — not real
    content, so they're dropped rather than rendered as a stray blank
    paragraph. Code segments are left byte-for-byte as-is."""
    if not in_code:
        while buf and buf[-1] == "":
            buf = buf[:-1]
    return ("code" if in_code else "text", "\n".join(buf))


def render_markdown_into_box(box: Gtk.Box, markdown_text: str, leading_widget=None):
    """Replaces box's children with markdown_text, rendered as one widget
    per segment (plain-text Gtk.TextView, or a code-block widget with a
    Copy button). leading_widget, if given (the chat avatar), is anchored
    inline as the very first character of the very first text segment —
    see _build_text_widget — so it flows with the text like any other
    glyph instead of claiming a row/column of its own."""
    child = box.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        box.remove(child)
        child = next_child

    segments = _split_segments(markdown_text)
    used_leading = False
    for kind, content in segments:
        if kind == "code":
            if leading_widget is not None and not used_leading:
                # A code block can't hold the anchor itself (see
                # _build_code_widget) — give the avatar its own otherwise-
                # empty text line right before it instead of dropping it.
                box.append(_build_text_widget("", leading_widget=leading_widget))
                used_leading = True
            box.append(_build_code_widget(content))
        elif content.strip():
            box.append(_build_text_widget(content, leading_widget=None if used_leading else leading_widget))
            used_leading = used_leading or leading_widget is not None
    if leading_widget is not None and not used_leading:
        box.append(_build_text_widget("", leading_widget=leading_widget))


def _build_text_widget(text_content, leading_widget=None):
    text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
    text_view.set_editable(False)
    text_view.set_cursor_visible(False)
    # Explicit hexpand so GTK settles on the bubble's real width on the
    # first layout pass — without it, word-wrap can briefly measure against
    # a too-narrow guessed width (reporting a too-tall natural height) before
    # correcting itself once the real width propagates, which is what
    # showed up as the bubble visibly growing then shrinking right after
    # a reply/attachment landed.
    text_view.set_hexpand(True)
    # Gtk.TextView's default ".view" styling paints its own input-field-like
    # background — left as-is, every text segment showed up as its own
    # separate white/boxed field instead of blending into the chat bubble
    # around it. This class strips that (see window.py's setup_css).
    text_view.add_css_class("markdown-plain-text")
    buffer = Gtk.TextBuffer(tag_table=_get_tag_table())
    text_view.set_buffer(buffer)

    if leading_widget is not None:
        # A child anchor makes the avatar an inline "character" in the text
        # flow itself — it sits at the start of line one, and if that line
        # wraps, the continuation goes back to the left margin like it
        # would after any other letter, not hanging-indented under it.
        anchor = buffer.create_child_anchor(buffer.get_start_iter())
        text_view.add_child_at_anchor(leading_widget, anchor)
        buffer.insert(buffer.get_end_iter(), " ")

    for line in text_content.split("\n"):
        list_match = _LIST_ITEM_RE.match(line)
        line_start_offset = buffer.get_char_count()
        if list_match:
            buffer.insert(buffer.get_end_iter(), "• ")
        _insert_inline(buffer, list_match.group(3) if list_match else line)
        buffer.insert(buffer.get_end_iter(), "\n")
        if list_match:
            buffer.apply_tag_by_name(
                "list-item", buffer.get_iter_at_offset(line_start_offset), buffer.get_end_iter()
            )
    return text_view


def _build_code_widget(code_content):
    """A small "card": a Copy button in its own header row, the code
    itself below in a monospace, non-editable TextView (still mouse/
    keyboard selectable — the button is a one-click convenience on top of
    that, not a replacement for it)."""
    container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    container.add_css_class("markdown-code-block")

    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.set_halign(Gtk.Align.END)
    copy_button = Gtk.Button(icon_name="edit-copy-symbolic")
    copy_button.add_css_class("flat")
    copy_button.set_tooltip_text(_("Copy"))
    copy_button.connect("clicked", _on_copy_code_clicked, code_content)
    header.append(copy_button)
    container.append(header)

    # WrapMode.NONE, not WORD_CHAR: word-wrap needs a known allocated
    # *width* before it can compute a *height* (how many lines will this
    # wrap into?), which for a freshly-appended widget can take several
    # real GTK layout passes to settle — and until it does, both the
    # widget's own size *and* the transcript's scroll position (which
    # depends on that size) are unstable, which is exactly what showed up
    # as "stretched then shrinks after several seconds" and "doesn't
    # scroll to the true bottom". NONE mode's natural size is a single-pass,
    # width-independent computation (widest line's width, plain line-count
    # for height) — no iteration, no instability. Long lines get their own
    # horizontal scrollbar instead of wrapping, which also happens to be
    # the conventional way to display code/terminal output anyway.
    code_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE)
    code_view.set_editable(False)
    code_view.set_cursor_visible(False)
    code_view.add_css_class("markdown-code-text")
    code_view.add_css_class("monospace")  # GTK's built-in monospace style class, belt-and-suspenders
    code_view.get_buffer().set_text(code_content)

    # Forces a minimum width matching the widest line, computed from this
    # (still-unrealized) view's own Pango metrics — reliable immediately,
    # unlike measuring the view's actual *size*, which needs a real layout
    # pass first. This is what makes propagate_natural_height below behave:
    # asked for "natural height at width W", a WrapMode.NONE view answers
    # correctly only for W >= its content's natural width — asked for a
    # narrower W (e.g. this ~220px-wide AI panel against a long shell
    # command), it collapses to a near-zero height instead of reporting
    # "as tall as the content actually is, you'll need to scroll
    # sideways" — leaving most of a multi-line block invisible, sitting
    # below the sliver of allocated height with no vertical scrollbar to
    # reach it. Pre-empting that narrower-than-natural case entirely, by
    # never letting the view's own minimum width go below its content's
    # width, is simpler and more reliable than reacting after the fact.
    lines = code_content.split("\n")
    metrics = code_view.get_pango_context().get_metrics(None, None)
    char_width_px = metrics.get_approximate_char_width() / Pango.SCALE
    max_line_len = max((len(line) for line in lines), default=0)
    code_view.set_size_request(int(char_width_px * max_line_len) + 8, -1)

    # Bottom padding reserved for a horizontal scrollbar that might appear
    # — added directly on the view rather than via the scroller's own
    # non-overlay-scrolling space reservation (Gtk.ScrolledWindow.
    # set_overlay_scrolling(False)). That combination measures fine on its
    # own, but with propagate_natural_height below, the reserved strip
    # comes straight out of the view's own reported height instead of
    # adding to it — a multi-line block loses roughly one scrollbar's
    # worth of height off its bottom (the last line or so, cut off with no
    # vertical scrollbar to ever reach it). A margin baked into the view's
    # own measurement doesn't have that interaction: it's simply part of
    # what "natural height" already means, so the reserved strip only
    # covers blank padding, never real text, without shrinking anything.
    _hsb_min, hsb_natural, _hsb_mb, _hsb_nb = Gtk.Scrollbar(orientation=Gtk.Orientation.HORIZONTAL).measure(
        Gtk.Orientation.VERTICAL, -1
    )
    code_view.set_bottom_margin(code_view.get_bottom_margin() + hsb_natural)

    code_scroller = Gtk.ScrolledWindow(hexpand=True)
    code_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    code_scroller.set_propagate_natural_height(True)
    code_scroller.set_child(code_view)
    container.append(code_scroller)

    return container


def _on_copy_code_clicked(button, code_content):
    # Gdk.Clipboard.set() boxes the string into a GValue and relies on GDK's
    # built-in text serializer to turn *that* into an actual text/plain
    # clipboard offer — reported to silently not reach the system clipboard
    # on Linux. Gdk.ContentProvider.new_for_bytes() sidesteps that path
    # entirely: it hands the clipboard the encoded bytes directly, tagged
    # with an explicit MIME type, with no serializer registration involved.
    provider = Gdk.ContentProvider.new_for_bytes(
        "text/plain;charset=utf-8", GLib.Bytes.new(code_content.encode("utf-8"))
    )
    button.get_clipboard().set_content(provider)
    # Brief "copied" feedback, matching the pattern most chat UIs use —
    # revert automatically rather than requiring another click to dismiss.
    button.set_icon_name("object-select-symbolic")
    button.set_tooltip_text(_("Copied!"))

    def _revert():
        button.set_icon_name("edit-copy-symbolic")
        button.set_tooltip_text(_("Copy"))
        return False
    GLib.timeout_add(1200, _revert)


def _insert_inline(buffer, text):
    """Parses **bold**/*italic*/`code` spans in a single line, inserting the
    stripped (marker-free) content with the matching tag applied. Buffer
    offsets are always computed from what's actually inserted, never from
    the original markdown source (which still has the marker characters)."""
    pos = 0
    for match in _INLINE_PATTERN.finditer(text):
        if match.start() > pos:
            buffer.insert(buffer.get_end_iter(), text[pos:match.start()])

        if match.group("bold") is not None:
            tag_name, content = "bold", match.group("bold")
        elif match.group("italic") is not None:
            tag_name, content = "italic", match.group("italic")
        else:
            tag_name, content = "inline-code", match.group("code")

        start_offset = buffer.get_char_count()
        buffer.insert(buffer.get_end_iter(), content)
        buffer.apply_tag_by_name(tag_name, buffer.get_iter_at_offset(start_offset), buffer.get_end_iter())

        pos = match.end()

    if pos < len(text):
        buffer.insert(buffer.get_end_iter(), text[pos:])
