"""Shared visual identity for AI providers — which real logo (if any)
represents a provider id, the text-initials fallback for providers with
none, and the CSS "family" class used to tint both consistently. Used by
window.py's header-bar buttons and ai_panel.py's per-message avatars, so a
given provider looks the same wherever it shows up.
"""

# Real provider logos, sourced from Simple Icons (CC0-licensed,
# https://simpleicons.org — maintained specifically for representing
# third-party brands/services in your own UI) as plain monochrome SVGs
# saved under thongssh_gtk/icons/ with a "-symbolic" suffix, so GTK's icon
# theme loads and recolors them like any other symbolic icon. Keyed by
# "family" (see badge_family). Providers with no entry here (Grok has no
# logo in Simple Icons yet, and any custom provider) fall back to a
# text-initials badge via badge_text().
PROVIDER_ICON_NAMES = {
    "claude": "ai-claude-symbolic",
    "gemini": "ai-gemini-symbolic",
    "chatgpt": "ai-chatgpt-symbolic",
    "deepseek": "ai-deepseek-symbolic",
    "cli-claude": "ai-claude-symbolic",
    "cli-codex": "ai-codex-symbolic",
}

_KNOWN_BADGE_TEXT = {
    "Claude": "C", "Gemini": "G", "ChatGPT": "GPT", "Grok": "Gr", "DeepSeek": "Dp",
    "Claude Code": "CC", "Codex": "Cx",
}


def badge_family(provider_id):
    """CSS class suffix for a provider id — kept distinct per standard
    provider (for its brand-ish color) but collapsed to one shared suffix
    for any custom entry (API or CLI), since there's no fixed color to
    give an arbitrary user-named provider."""
    if provider_id.startswith("cli:custom:"):
        return "cli-custom"
    if provider_id.startswith("cli:"):
        return f"cli-{provider_id[len('cli:'):]}"
    if provider_id.startswith("custom:"):
        return "custom"
    return provider_id


def badge_text(label):
    """Short initials fallback for a provider with no icon in
    PROVIDER_ICON_NAMES."""
    if label in _KNOWN_BADGE_TEXT:
        return _KNOWN_BADGE_TEXT[label]
    words = label.split()
    return "".join(w[0] for w in words[:2]).upper() or "AI"


def icon_name_for(provider_id):
    """Real logo icon name for a provider id, or None if it should fall
    back to a text-initials badge instead."""
    return PROVIDER_ICON_NAMES.get(badge_family(provider_id))
