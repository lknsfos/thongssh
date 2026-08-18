# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 lknsfos

"""Persists AI chat conversations as one JSON file per chat under the
user's cache directory (see paths.CACHE_DIR) — history lives here instead
of on any provider's servers, since the active provider is swappable
mid-conversation and several (local CLI tools, some self-hosted endpoints)
retain no history of their own at all regardless.

A chat with no messages yet is never written to disk (see AiPanel — a
chat file is only created once the first exchange actually completes), so
listing/searching only ever sees real conversations, never empty
scratch state from a panel that was opened and closed without being used.
"""

import json
import logging
import re
import time
import uuid

from .paths import CACHE_DIR

_ = lambda s: s

CHATS_DIR = CACHE_DIR / "ai_chats"


def new_chat_id():
    return str(uuid.uuid4())


def _chat_path(chat_id):
    return CHATS_DIR / f"{chat_id}.json"


def save_chat(chat_id, title, messages):
    """Overwrites the chat's file with its current title and full message
    list — called after every completed exchange, so the file on disk is
    never more than one reply behind what's on screen."""
    try:
        CHATS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "id": chat_id,
            "title": title or "",
            "updated_at": time.time(),
            "messages": messages,
        }
        tmp_path = _chat_path(chat_id).with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(_chat_path(chat_id))
    except OSError as e:
        logging.error(f"Failed to save AI chat {chat_id}: {e}")


def load_chat(chat_id):
    """Returns {"id", "title", "messages"} or None if it's missing/unreadable."""
    try:
        with open(_chat_path(chat_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"Failed to load AI chat {chat_id}: {e}")
        return None


def delete_chat(chat_id):
    try:
        _chat_path(chat_id).unlink(missing_ok=True)
    except OSError as e:
        logging.error(f"Failed to delete AI chat {chat_id}: {e}")


def _read_summaries():
    """Every saved chat's {"id", "title", "updated_at"} plus its raw
    message text (for search only — not returned), read fresh from disk
    every call. Simple linear scan: chat counts are realistically small
    (dozens to low hundreds), nowhere near needing an index."""
    if not CHATS_DIR.is_dir():
        return []
    entries = []
    for path in CHATS_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entries.append(data)
    return entries


def list_chats():
    """[{"id", "title", "updated_at"}, ...], newest-first — everything the
    history picker needs without loading full message lists it won't show."""
    chats = [
        {"id": c.get("id"), "title": c.get("title") or "", "updated_at": c.get("updated_at") or 0}
        for c in _read_summaries()
    ]
    chats.sort(key=lambda c: c["updated_at"], reverse=True)
    return chats


def search_chats(query):
    """Plain regex over saved chats' titles and message content —
    deliberately no AI involved, this needs to filter on every keystroke.
    Case-insensitive; an invalid (still-being-typed) regex just matches
    nothing rather than raising. Newest-first, like list_chats()."""
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        return []
    results = []
    for data in _read_summaries():
        title = data.get("title") or ""
        haystack = title + "\n" + "\n".join(m.get("content", "") for m in data.get("messages", []))
        if pattern.search(haystack):
            results.append({"id": data.get("id"), "title": title, "updated_at": data.get("updated_at") or 0})
    results.sort(key=lambda c: c["updated_at"], reverse=True)
    return results
