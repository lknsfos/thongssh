# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""Real gettext-based translation setup. Every other module imports `_`
from here (`from .i18n import _`) instead of defining its own no-op
`_ = lambda s: s` placeholder — this is the one place that actually wires
`_` up to a real translation catalog. Import this before any other
thongssh_gtk module that calls `_(...)` at import/definition time (in
practice: thongssh_gtk/app.py imports it first, before window/dialogs/etc).

Standard GNU gettext layout, so this works exactly like every other
Linux/Unix desktop app: message catalogs are compiled .mo files, one per
language, under thongssh_gtk/locale/<lang>/LC_MESSAGES/thongssh.mo — the
same directory shape as /usr/share/locale, just bundled alongside the
package itself instead of installed system-wide, so it works unmodified
from a dev checkout, a .deb/.rpm install, or an AppImage/macOS bundle
(they all copy the whole thongssh_gtk/ directory as one unit — see
build-packages/*.sh).

Language selection:
- Settings -> General -> Language, stored as interface.language.
- "system" (the default) means "let gettext figure it out the normal
  way" — its own automatic LANGUAGE/LC_ALL/LC_MESSAGES/LANG lookup,
  exactly like every other gettext-using program on the system.
- Anything else is a specific language code (see LANGUAGES below),
  forcing that translation regardless of the system locale.
"""

import gettext
import os
import sys

DOMAIN = "thongssh"

# code -> native display name, for the Settings dropdown. Each language
# names itself, in its own script — the standard convention (a Japanese
# speaker shouldn't have to already read English to find "Japanese" in a
# list) — not translated into whatever the current UI language is.
LANGUAGES = {
    "system": "System Default",
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "ru": "Русский",
    "uk": "Українська",
    "he": "עברית",
    "ar": "العربية",
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
    "ja": "日本語",
}

# Text-direction-only info — GTK's own automatic RTL detection is driven
# by the *process* locale (LC_CTYPE/LANG as seen by Pango), which forcing
# a translation via gettext.translation(languages=[...]) does NOT also
# change. Forcing Arabic/Hebrew from Settings needs this spelled out
# explicitly (see apply_language_direction below) rather than relying on
# it falling out of the system locale for free.
RTL_LANGUAGES = {"ar", "he"}


def _locale_dir():
    """thongssh_gtk/locale, resolved the same way resource_path() in
    constants.py resolves everything else — relative to this file's own
    location, which every packaging script already carries along
    unmodified as part of the whole thongssh_gtk/ directory."""
    return os.path.join(os.path.dirname(__file__), "locale")


def _forced_language():
    """The interface.language setting, if it names a real language
    (not "system" and not empty/missing). A tiny, self-contained JSON
    read rather than importing SettingsManager from settings.py — i18n
    needs to be ready before most other modules, and this avoids having
    to reason about import order/circularity with the rest of the
    settings machinery for the sake of one string."""
    try:
        from .paths import CONFIG_DIR
        import json
        settings_file = CONFIG_DIR / "settings.json"
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        lang = data.get("interface.language")
    except (OSError, ValueError, ImportError):
        return None
    if lang and lang != "system" and lang in LANGUAGES:
        return lang
    return None


def _resolve_system_language_code():
    """Which language the underlying OS/desktop session is actually
    running in — deliberately ignoring interface.language's forced
    override (see apply_language_direction for why). Used only to decide
    RTL vs LTR, never for the actual translation lookup (gettext.translation
    below handles that separately, including honoring the forced choice).

    On macOS, env vars like LANG are often unset for a GUI-launched app
    (no login shell in the picture), so the real signal is Cocoa's own
    preferred-language list instead."""
    if sys.platform == "darwin":
        try:
            from Foundation import NSLocale
            langs = NSLocale.preferredLanguages()
            if langs:
                return str(langs[0]).split("-")[0].split("_")[0].lower()
        except Exception:
            pass
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return value.split(":")[0].split(".")[0].split("_")[0].lower()
    return None


def apply_language_direction():
    """Sets the whole app's default text direction to RTL for Arabic/
    Hebrew, LTR otherwise. Call once at startup, after the window system
    is initialized (app.py does this right before creating the window).

    Deliberately keyed off the *system's* language, not interface.language's
    forced override: forcing a translation from Settings -> Language is a
    "show me this UI in Hebrew" choice, not "my desktop is a native RTL
    environment" — mirroring the whole widget/menu order for the former
    has no real benefit (nothing else on the system is mirrored to match)
    and, on macOS specifically, actively breaks the header bar (see below).
    If the system itself is genuinely RTL, we mirror regardless of
    interface.language, since then the OS's own conventions already
    expect it — a translated-but-unmirrored UI would be the odd one out.

    Known issue on macOS when the system itself is RTL: the header bar's
    leading content overlaps the native traffic-light buttons. Confirmed
    via direct introspection (comparing AdwHeaderBar's internal
    GtkWindowControls allocation against the real NSWindow button frame
    read through AppKit) that libadwaita's macOS-native-controls support
    (added in 1.8) mirrors its *reserved* space for RTL, while the real
    buttons — fixed by AppKit, never mirrored — don't move; no per-widget
    or per-window direction override changes this, since the placement
    math reads this same process-wide default direction directly rather
    than any widget's resolved direction. Not fixable from window.py's
    header bar setup; would need an upstream libadwaita fix. Left as-is
    for a genuinely RTL system (matching the rest of the OS wins out over
    working around this one widget), but no longer triggered just by
    forcing Hebrew/Arabic from Settings on an otherwise LTR system."""
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    code = _resolve_system_language_code()
    direction = Gtk.TextDirection.RTL if code in RTL_LANGUAGES else Gtk.TextDirection.LTR
    Gtk.Widget.set_default_direction(direction)


_forced = _forced_language()
_translation = gettext.translation(
    DOMAIN,
    localedir=_locale_dir(),
    languages=[_forced] if _forced else None,
    fallback=True,
)
_ = _translation.gettext
ngettext = _translation.ngettext
