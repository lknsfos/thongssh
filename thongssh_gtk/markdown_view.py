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


def render_markdown_into_box(box: Gtk.Box, markdown_text: str):
    """Replaces box's children with markdown_text, rendered as one widget
    per segment (plain-text Gtk.TextView, or a code-block widget with a
    Copy button)."""
    child = box.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        box.remove(child)
        child = next_child

    for kind, content in _split_segments(markdown_text):
        if kind == "code":
            box.append(_build_code_widget(content))
        elif content.strip():
            box.append(_build_text_widget(content))


def _build_text_widget(text_content):
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

    code_scroller = Gtk.ScrolledWindow(hexpand=True)
    code_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    code_scroller.set_propagate_natural_height(True)
    code_scroller.set_child(code_view)
    container.append(code_scroller)

    return container


def _on_copy_code_clicked(button, code_content):
    button.get_clipboard().set(code_content)
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
