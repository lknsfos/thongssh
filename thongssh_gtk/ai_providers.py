"""HTTP glue for the AI chat panel. Stdlib-only (urllib) on purpose — no new
third-party dependency, to avoid repeating the py2app native-extension
packaging pain paramiko/cryptography caused for the macOS build.

Every provider family exposes the same shape: build a JSON request, POST it,
parse the reply into a plain string. All I/O happens on a background thread;
results are marshaled back to the GTK main loop via GLib.idle_add, since GTK4
widgets must only ever be touched from the main thread.
"""

import json
import logging
import threading
import urllib.request
import urllib.error

from gi.repository import GLib

from .constants import AI_STANDARD_PROVIDERS

_ = lambda s: s

# Overridable via the "ai.request_timeout_seconds" setting (see dialogs.py) —
# this is only the fallback if a caller doesn't pass one explicitly. Kept
# generous by default since local/self-hosted models on modest hardware can
# take a while to respond.
DEFAULT_REQUEST_TIMEOUT = 120

# Listing models is a quick metadata call, not a generation request — always
# a short fixed timeout regardless of the user's (possibly very generous)
# "ai.request_timeout_seconds", which is tuned for slow local models actually
# replying, not for this.
MODELS_FETCH_TIMEOUT = 15

# Reasonable current defaults — deliberately overridable per-provider from
# Settings (see dialogs.py), since model names go stale and shouldn't need a
# code change to fix.
DEFAULT_MODELS = {
    "claude": "claude-sonnet-5",
    "gemini": "gemini-2.5-flash",
    "chatgpt": "gpt-4o-mini",
    "grok": "grok-2-latest",
    "deepseek": "deepseek-chat",
}

# OpenAI-compatible providers include "/v1" already, so the request builder
# can uniformly append "/chat/completions" for chatgpt/grok/deepseek/custom.
DEFAULT_BASE_URLS = {
    "claude": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "chatgpt": "https://api.openai.com/v1",
    "grok": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
}

_STANDARD_LABELS = dict(AI_STANDARD_PROVIDERS)


class ProviderError(Exception):
    pass


def resolve_provider_config(provider_id, settings_manager, keyring):
    """Returns {"label", "base_url", "model", "api_key"} for a provider id,
    resolving standard ("claude", ...) vs custom ("custom:<uuid>") providers
    uniformly. Returns None if the id doesn't resolve to anything configured
    (e.g. a custom provider that was since removed from settings)."""
    overrides = settings_manager.get("ai.provider_models") or {}

    if provider_id.startswith("custom:"):
        custom_id = provider_id.split(":", 1)[1]
        for cp in settings_manager.get("ai.custom_providers") or []:
            if cp.get("id") == custom_id:
                return {
                    "label": cp.get("name") or _("Custom"),
                    "base_url": cp.get("base_url", ""),
                    "model": overrides.get(provider_id) or "gpt-3.5-turbo",
                    "api_key": keyring.load_password(f"ai:custom:{custom_id}") or "",
                }
        return None

    if provider_id not in _STANDARD_LABELS:
        return None
    return {
        "label": _STANDARD_LABELS[provider_id],
        "base_url": DEFAULT_BASE_URLS.get(provider_id, ""),
        "model": overrides.get(provider_id) or DEFAULT_MODELS.get(provider_id),
        "api_key": keyring.load_password(f"ai:{provider_id}") or "",
    }


def send_chat_request(provider_id, api_key, base_url, model, system_prompt, messages,
                       on_success, on_error, timeout=None):
    """Spawns a daemon thread to perform the request. on_success(reply_text)
    and on_error(message) are always invoked via GLib.idle_add — never called
    directly from the worker thread. timeout (seconds) overrides
    DEFAULT_REQUEST_TIMEOUT — pass the user's "ai.request_timeout_seconds"
    setting here."""

    def worker():
        try:
            reply = _dispatch(provider_id, api_key, base_url, model, system_prompt, messages, timeout)
        except ProviderError as e:
            GLib.idle_add(on_error, str(e))
            return
        except Exception as e:
            logging.error(f"AI provider '{provider_id}' request failed: {e}")
            GLib.idle_add(on_error, str(e))
            return
        GLib.idle_add(on_success, reply)

    threading.Thread(target=worker, daemon=True).start()


def _dispatch(provider_id, api_key, base_url, model, system_prompt, messages, timeout):
    family = provider_id.split(":", 1)[0]
    if family == "claude":
        return _claude_request(api_key, base_url, model, system_prompt, messages, timeout)
    if family == "gemini":
        return _gemini_request(api_key, base_url, model, system_prompt, messages, timeout)
    # chatgpt, grok, deepseek, custom -> all OpenAI chat-completions compatible
    return _openai_compatible_request(base_url, api_key, model, system_prompt, messages, timeout)


def _urlopen_json(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout or DEFAULT_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise ProviderError(f"HTTP {e.code}: {body_text or e.reason}") from e
    except urllib.error.URLError as e:
        raise ProviderError(f"Network error: {e.reason}") from e
    except UnicodeError as e:
        # http.client encodes header VALUES with latin-1 — this fires if any
        # header (most likely the API key field) has a non-ASCII character
        # in it, e.g. pasted/typed incorrectly. Without this, it surfaces as
        # a cryptic raw "'latin-1' codec can't encode..." message instead.
        raise ProviderError(
            _("Request header contains a non-ASCII character (double-check the API key was pasted "
              "correctly): {error}").format(error=e)
        ) from e


def _post_json(url, headers, body, timeout):
    data = json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    return _urlopen_json(req, timeout)


def _get_json(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return _urlopen_json(req, timeout)


def _claude_request(api_key, base_url, model, system_prompt, messages, timeout):
    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    if system_prompt:
        body["system"] = system_prompt
    response = _post_json(url, headers, body, timeout)
    try:
        return response["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"Unexpected Claude response shape: {response}") from e


def _gemini_request(api_key, base_url, model, system_prompt, messages, timeout):
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent?key={api_key or ''}"
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    body = {"contents": contents}
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    response = _post_json(url, {}, body, timeout)
    try:
        return response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"Unexpected Gemini response shape: {response}") from e


def _openai_compatible_request(base_url, api_key, model, system_prompt, messages, timeout):
    base = base_url.rstrip('/')
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    chat_messages = []
    if system_prompt:
        chat_messages.append({"role": "system", "content": system_prompt})
    chat_messages.extend({"role": m["role"], "content": m["content"]} for m in messages)
    body = {"model": model, "messages": chat_messages}
    response = _post_json(url, headers, body, timeout)
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"Unexpected response shape: {response}") from e


# --- Model listing (Settings -> AI's "choose from available models" picker) ---
#
# Best-effort: not every provider is confirmed to have a list-models
# endpoint, and reachability/auth can fail for all sorts of reasons that have
# nothing to do with the chat endpoint working fine. Callers always get a
# graceful ProviderError instead of a crash either way — manual model entry
# never depends on this succeeding.

def _claude_list_models(api_key, base_url, timeout):
    url = f"{base_url.rstrip('/')}/v1/models?limit=100"
    headers = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
    response = _get_json(url, headers, timeout)
    try:
        return [m["id"] for m in response.get("data", [])]
    except (KeyError, TypeError) as e:
        raise ProviderError(f"Unexpected models response shape: {response}") from e


def _gemini_list_models(api_key, base_url, timeout):
    url = f"{base_url.rstrip('/')}/v1beta/models?key={api_key or ''}&pageSize=200"
    response = _get_json(url, {}, timeout)
    models = []
    try:
        for m in response.get("models", []):
            # Filter to text-chat-capable models — the same listing also
            # includes embedding/vision-only/etc. models this chat panel
            # can't use.
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = m.get("name", "")
            models.append(name.split("/", 1)[1] if "/" in name else name)
    except (KeyError, TypeError) as e:
        raise ProviderError(f"Unexpected models response shape: {response}") from e
    return models


def _openai_compatible_list_models(base_url, api_key, timeout):
    base = base_url.rstrip('/')
    url = base if base.endswith("/models") else f"{base}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = _get_json(url, headers, timeout)
    try:
        return [m["id"] for m in response.get("data", [])]
    except (KeyError, TypeError) as e:
        raise ProviderError(f"Unexpected models response shape: {response}") from e


def list_models(family, api_key, base_url, timeout=None):
    """family is a provider id ("claude", "gemini", ...) or literally
    "custom" — same dispatch shape as _dispatch(), minus the ":<uuid>"
    suffix custom provider ids carry elsewhere, since the caller already
    knows the actual base_url to use for a custom endpoint."""
    if family == "claude":
        return _claude_list_models(api_key, base_url, timeout)
    if family == "gemini":
        return _gemini_list_models(api_key, base_url, timeout)
    return _openai_compatible_list_models(base_url, api_key, timeout)


def fetch_models(family, api_key, base_url, on_success, on_error, timeout=None):
    """Spawns a daemon thread to list available models. on_success(models)
    and on_error(message) are always invoked via GLib.idle_add."""

    def worker():
        try:
            models = list_models(family, api_key, base_url, timeout or MODELS_FETCH_TIMEOUT)
        except ProviderError as e:
            GLib.idle_add(on_error, str(e))
            return
        except Exception as e:
            logging.error(f"Model list fetch for '{family}' failed: {e}")
            GLib.idle_add(on_error, str(e))
            return
        GLib.idle_add(on_success, models)

    threading.Thread(target=worker, daemon=True).start()
