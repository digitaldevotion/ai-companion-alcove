# ============================================
# Alcove — history.py
# Message history persistence
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

from .prompt import _is_error_response
from .database import save_message


def save_history(pending_saves, response_text, db, channel_name):
    _i = 0
    while _i < len(pending_saves):
        _role, _body, _name = pending_saves[_i]
        if _role == "assistant" and _is_error_response(_body):
            print(f"⚠️ Skipping DB save of error response: {_body[:120]}")
            pending_saves.pop(_i)
            continue
        try:
            save_message(db, channel_name, _role, _body, _name)
            pending_saves.pop(_i)
        except Exception as _save_err:
            print(f"⚠️ save_history: save_message failed (item kept for flush): {_save_err}")
            _i += 1
    if not _is_error_response(response_text):
        save_message(db, channel_name, "assistant", response_text)
    else:
        print(f"⚠️ Skipping DB save of final error response: {response_text[:120]}")


def flush_pending_saves(pending_saves, db, channel_name):
    while pending_saves:
        try:
            _role, _body, _name = pending_saves.pop(0)
            save_message(db, channel_name, _role, _body, _name)
        except Exception as _flush_err:
            print(f"⚠️ pending_saves flush error: {_flush_err}")
