import sys
import os

__version__ = "0.7.2"

APP_ID = "com.example.thongssh"

def resource_path(relative_path):
    """
    Get absolute path to resource, works for dev and for PyInstaller.
    """
    # PyInstaller creates a temp folder and stores path in _MEIPASS
    if hasattr(sys, '_MEIPASS'):
        # When bundled, the 'thongssh_gtk' folder is at the root.
        return os.path.join(sys._MEIPASS, 'thongssh_gtk', relative_path)

    # In development, the path is relative to the thongssh_gtk directory
    return os.path.join(os.path.dirname(__file__), relative_path)

# TreeStore columns: name, type, icon, data object (config/node)
(
    COL_NAME,
    COL_TYPE,
    COL_ICON,
    COL_DATA
) = range(4)

# The 5 AI providers with a plain per-key REST chat API (id, display label).
# Copilot is deliberately excluded — it has no public key+chat REST API for
# third-party apps, unlike the others.
AI_STANDARD_PROVIDERS = [
    ("claude", "Claude"),
    ("gemini", "Gemini"),
    ("chatgpt", "ChatGPT"),
    ("grok", "Grok"),
    ("deepseek", "DeepSeek"),
]

# Locally-installed CLI tools the chat panel can talk to instead of a REST
# API (id, display label, default command template). "{message}" and
# "{system_prompt}" are each substituted as their own single argv element —
# never string-concatenated into a shell command — so arbitrary content can
# never break out into shell syntax. The user installs these themselves
# (npm, etc.); we only care that we can pipe a message in and read a reply
# back out. Claude's template feeds the shared system prompt straight to
# --append-system-prompt (confirmed via `claude --help`), giving it real
# system-level priority instead of being buried in the user-turn text,
# which is what let it "forget" the no-local-execution instruction after a
# few turns. Codex has no known equivalent flag, so it falls back to the
# generic prepend-into-message behavior (see cli_providers._build_argv).
CLI_STANDARD_PROVIDERS = [
    ("claude", "Claude Code", "claude -p --append-system-prompt {system_prompt} {message}"),
    ("codex", "Codex", "codex exec {message}"),
]

# Suggested values for Settings -> AI -> CLI Client's Model picker — there's
# no "list models" API for a local CLI tool the way there is for an HTTP
# provider (see ai_providers.list_models), so this is just a starting point;
# the field is always free-text too. Left empty for a tool with no confirmed
# model aliases, rather than guessing — a wrong preset is worse than none.
CLI_MODEL_PRESETS = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": [],
}