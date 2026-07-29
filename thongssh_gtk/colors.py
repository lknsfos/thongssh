import json
import logging

from .paths import CONFIG_DIR

# Placeholder for future internationalization (i18n)
_ = lambda s: s

# Saved separately from settings.json (not inline) so it can hold a full
# 16-color palette + background/foreground without bloating/coupling to
# the flat settings dict, the same way hosts.json is kept apart from it.
CUSTOM_COLOR_SCHEME_FILE = CONFIG_DIR / "custom_color_scheme.json"

# --- Color Schemes ---

# Solarized Dark
SOLARIZED_DARK = {
    "background": "#002b36", "foreground": "#839496", "palette": [
    "#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5",
    "#002b36", "#cb4b16", "#586e75", "#657b83", "#839496", "#6c71c4", "#93a1a1", "#fdf6e3"
]}
# Solarized Light
SOLARIZED_LIGHT = {
    "background": "#fdf6e3", "foreground": "#657b83", "palette": [
    "#eee8d5", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#073642",
    "#fdf6e3", "#cb4b16", "#93a1a1", "#839496", "#657b83", "#6c71c4", "#586e75", "#002b36"
]}
# Gruvbox Dark
GRUVBOX_DARK = {
    "background": "#282828", "foreground": "#ebdbb2", "palette": [
    "#282828", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286", "#689d6a", "#a89984",
    "#928374", "#fb4934", "#b8bb26", "#fabd2f", "#83a598", "#d3869b", "#8ec07c", "#ebdbb2"
]}
# ✨ Atom One Light
ATOM_ONE_LIGHT = {
    "background": "#fafafa", "foreground": "#383a42", "palette": [
    "#000000", "#e45649", "#50a14f", "#c18401", "#0184bc", "#a626a4", "#0997b3", "#fafafa",
    "#5c5e64", "#e45649", "#50a14f", "#c18401", "#0184bc", "#a626a4", "#0997b3", "#ffffff"
]}
# ✨ Tango Light
TANGO_LIGHT = {
    "background": "#ffffff", "foreground": "#000000", "palette": [
    "#000000", "#cc0000", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec"
]}

# Classic xterm-style 16-color palette — used only as a starting point when
# custom colors are enabled on top of "Default" (which, unlike the other
# templates, has no colors of its own to seed from).
DEFAULT_FALLBACK_COLORS = {
    "background": "#000000", "foreground": "#ffffff", "palette": [
    "#000000", "#cc0000", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
]}

COLOR_SCHEMES = {
    "default": {"name": _("Default")},
    "solarized-dark": {"name": "Solarized Dark", "colors": SOLARIZED_DARK},
    "solarized-light": {"name": "Solarized Light", "colors": SOLARIZED_LIGHT},
    "gruvbox-dark": {"name": "Gruvbox Dark", "colors": GRUVBOX_DARK},
    "atom-one-light": {"name": "Atom One Light", "colors": ATOM_ONE_LIGHT},
    "tango-light": {"name": "Tango Light", "colors": TANGO_LIGHT},
    # No static "colors" here — unlike the built-in templates above, this
    # one's colors live in a separate user-editable file and are resolved
    # live by get_scheme_colors(), never baked into this dict.
    "custom": {"name": _("Custom")},
}


def load_custom_color_scheme():
    """Returns the user's saved custom {"background", "foreground",
    "palette"} dict, or None if one was never saved (or is unreadable)."""
    try:
        with open(CUSTOM_COLOR_SCHEME_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.debug(f"No usable custom color scheme at {CUSTOM_COLOR_SCHEME_FILE}: {e}")
        return None


def save_custom_color_scheme(colors):
    """colors is a {"background", "foreground", "palette"} dict, same shape
    as the built-in templates above."""
    CUSTOM_COLOR_SCHEME_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CUSTOM_COLOR_SCHEME_FILE, "w", encoding="utf-8") as f:
        json.dump(colors, f, indent=4)


def get_scheme_colors(scheme_key):
    """Returns the {"background", "foreground", "palette"} dict for a
    scheme id, or None for "default"/an unknown id/a not-yet-saved custom
    scheme (all of which mean "no override, leave the terminal's own
    default colors alone"). The one live lookup: "custom" isn't in
    COLOR_SCHEMES with real colors — it's read fresh from disk every call,
    so a change saved from Settings takes effect without needing this
    module reloaded."""
    if scheme_key == "custom":
        return load_custom_color_scheme()
    return COLOR_SCHEMES.get(scheme_key, {}).get("colors")