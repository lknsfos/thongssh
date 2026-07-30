"""Local CLI-tool glue for the AI chat panel — an alternative to the HTTP
API providers (see ai_providers.py) for talking to locally-installed CLI
tools (Claude Code CLI, OpenAI Codex CLI, or anything else the user points
at, installed however they like — npm, a package manager, a raw binary).

Runs a single one-shot subprocess per message: build argv from the command
template, run it, capture stdout as the reply. No shell is ever invoked —
"{message}" is substituted as its own argv element (never string-
concatenated into a command line), so arbitrary message content can never
break out into shell syntax regardless of what it contains.

Each invocation is still a fresh, memory-less process (no --continue/
--resume session juggling — that would tie this generic module to one
specific tool's session semantics). Conversational continuity instead
comes from replaying the full visible history as a formatted transcript
in the prompt text on every call, the same way the HTTP API providers
already get the full message list every time (see ai_providers.py) — the
tool has no memory of its own, but the app hands it the whole
conversation-so-far each time, which is what actually made "it forgot
what we were just talking about" go away.
"""

import logging
import shlex
import shutil
import subprocess
import tempfile
import threading

from gi.repository import GLib

from .constants import CLI_STANDARD_PROVIDERS

_ = lambda s: s

DEFAULT_TIMEOUT = 120

_STANDARD_TOOLS = {pid: (label, default_cmd) for pid, label, default_cmd in CLI_STANDARD_PROVIDERS}

# Without an explicit cwd, subprocess.run() inherits ThongSSH's own working
# directory — which, run from a source checkout, is a real project (this
# one!). Agentic coding CLIs like Claude Code auto-detect "am I inside a
# project?" from cwd and start answering as if the question is about
# *that* codebase, which is exactly what a plain chat question has nothing
# to do with. A dedicated, permanently-empty directory guarantees there's
# never any project for the tool to (mis)discover.
_neutral_cwd = None


def _get_neutral_cwd():
    global _neutral_cwd
    if _neutral_cwd is None:
        _neutral_cwd = tempfile.mkdtemp(prefix="thongssh-ai-cli-")
    return _neutral_cwd


class CliError(Exception):
    pass


def resolve_cli_config(provider_id, settings_manager):
    """Returns {"label", "command", "model"} for a "cli:..." provider id,
    resolving standard ("cli:claude") vs custom ("cli:custom:<uuid>") tools
    uniformly. Returns None if it doesn't resolve to anything configured.
    "model" is "" (the default) unless the user picked one in Settings —
    see _build_argv for what an empty model actually means (no extra flag
    at all, not an empty --model argument)."""
    if not provider_id.startswith("cli:"):
        return None
    rest = provider_id[len("cli:"):]
    model_overrides = settings_manager.get("cli.provider_models") or {}

    if rest.startswith("custom:"):
        custom_id = rest.split(":", 1)[1]
        for tool in settings_manager.get("cli.custom_tools") or []:
            if tool.get("id") == custom_id:
                return {
                    "label": tool.get("name") or _("Custom CLI"),
                    "command": tool.get("command", ""),
                    "model": model_overrides.get(rest, ""),
                }
        return None

    if rest not in _STANDARD_TOOLS:
        return None
    label, default_command = _STANDARD_TOOLS[rest]
    overrides = settings_manager.get("cli.commands") or {}
    return {
        "label": label,
        "command": overrides.get(rest) or default_command,
        "model": model_overrides.get(rest, ""),
    }


def is_available(command_template):
    """Whether the template's binary can actually be found on PATH right
    now — used to decide whether a header-bar button should appear at all,
    the CLI-provider equivalent of "does this API provider have a key"."""
    try:
        tokens = shlex.split(command_template or "")
    except ValueError:
        return False
    return bool(tokens) and shutil.which(tokens[0]) is not None


def _build_argv(command_template, system_prompt, message, model=None):
    """Builds argv from the template. Three placeholders are supported:
    "{message}" (the outgoing text), "{system_prompt}" (passed as its own
    argv element — e.g. the default Claude template feeds it straight to
    --append-system-prompt, giving it real system-level priority instead of
    just being more text the model might deprioritize in a long prompt),
    and "{model}". If a template doesn't know about "{system_prompt}" at
    all (custom tools with no such concept), it's prepended into the
    message text instead, same as before — nothing is silently dropped.

    "model" (from Settings -> AI -> CLI Client's Model field, empty by
    default) is deliberately NOT substituted into the command at all when
    unset — that's what makes "default = no extra model flag" actually
    true rather than passing an empty --model value that could confuse the
    tool. If a model IS chosen and the template has no explicit "{model}"
    placeholder, "--model <value>" is inserted right after the binary name
    — the conventional spot for a global flag on most CLIs (works for both
    standard tools' own --model flag). A template author who needs the
    flag somewhere else, or a different flag name entirely, can place
    "{model}" explicitly instead."""
    try:
        tokens = shlex.split(command_template)
    except ValueError as e:
        raise CliError(_("Invalid command template: {error}").format(error=e)) from e
    if not tokens:
        raise CliError(_("Command template is empty."))

    has_system_placeholder = "{system_prompt}" in tokens
    has_message_placeholder = "{message}" in tokens
    has_model_placeholder = "{model}" in tokens
    full_message = message if (has_system_placeholder or not system_prompt) else f"{system_prompt}\n\n{message}"

    def substitute(token):
        if token == "{message}":
            return full_message
        if token == "{system_prompt}":
            return system_prompt or ""
        if token == "{model}":
            return model or ""
        return token

    argv = [substitute(t) for t in tokens]
    if not has_message_placeholder:
        # No placeholder present — append the message as a trailing
        # argument so the template still does something useful instead of
        # silently ignoring whatever the user typed.
        argv.append(full_message)
    if model and not has_model_placeholder:
        argv[1:1] = ["--model", model]
    return argv


def _format_conversation(messages):
    """Flattens the app's [{"role", "content"}, ...] history into a plain
    text transcript — the closest a one-shot CLI's single "-p <text>"
    argument can get to real multi-turn structure. The last entry is
    always the new user message; everything before it is prior turns for
    context."""
    parts = []
    for m in messages:
        speaker = "User" if m.get("role") == "user" else "Assistant"
        parts.append(f"{speaker}: {m.get('content', '')}")
    return "\n\n".join(parts)


def run_cli_chat(provider_id, command_template, system_prompt, messages, on_success, on_error,
                  timeout=None, model=None):
    """Spawns a daemon thread to run the command. messages is the full
    [{"role", "content"}, ...] history (including the just-added latest
    user message, same shape ai_providers.send_chat_request expects) —
    replayed as a formatted transcript so the tool has context on every
    call despite each invocation being a fresh process. on_success
    (reply_text) and on_error(message) are always invoked via
    GLib.idle_add — never called directly from the worker thread."""

    def worker():
        try:
            conversation_text = _format_conversation(messages)
            argv = _build_argv(command_template, system_prompt, conversation_text, model=model)
            try:
                result = subprocess.run(
                    argv, capture_output=True, text=True,
                    timeout=timeout or DEFAULT_TIMEOUT,
                    cwd=_get_neutral_cwd(),
                )
            except FileNotFoundError as e:
                raise CliError(_("Command not found: {cmd}").format(cmd=argv[0])) from e
            except subprocess.TimeoutExpired:
                raise CliError(_("Timed out after {seconds}s").format(seconds=timeout or DEFAULT_TIMEOUT))
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()[:500]
                raise CliError(
                    _("'{cmd}' exited with status {code}: {detail}").format(
                        cmd=argv[0], code=result.returncode, detail=detail
                    )
                )
            reply = result.stdout.strip()
            if not reply:
                raise CliError(_("Command produced no output."))
        except CliError as e:
            GLib.idle_add(on_error, str(e))
            return
        except Exception as e:
            logging.error(f"CLI provider '{provider_id}' failed: {e}")
            GLib.idle_add(on_error, str(e))
            return
        GLib.idle_add(on_success, reply)

    threading.Thread(target=worker, daemon=True).start()
