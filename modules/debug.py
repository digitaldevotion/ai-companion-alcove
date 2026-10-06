# ============================================
# Alcove — debug.py
# Debug prompt/response capture helpers (!debug / !noDebug)
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
#
# When !debug is enabled for a channel, provider.chat_completion() writes
# the fully-augmented prompt payload and the raw provider response JSON
# to <repo>/diag/<uuid>_prompt.txt and <uuid>_response.txt respectively.
#
# Prompt file layout:
#   1. Diagnostic header block (models, context usage, sampling params, etc.)
#   2. Readable dump of the messages list (role-by-role, non-text blocks
#      replaced with one-line placeholders)
#   3. Raw JSON payload (the literal sanitized messages list sent upstream)
#
# Response file layout:
#   The full raw JSON dict returned by the provider (success, error envelope,
#   or upstream error shape) — pretty-printed.

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


DEBUG_DIR = Path(__file__).parent.parent / "diag"


def _ensure_dir():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


_ensure_dir()


def is_debug_enabled(db, channel_key):
    """Read the per-channel debug flag. Kept for parity / future use;
    the active code path threads ctx.debug_enabled through call sites
    instead of re-reading the DB inside provider.chat_completion."""
    if db is None or channel_key is None:
        return False
    try:
        from .database import get_channel_setting
        return get_channel_setting(db, channel_key, "debug_enabled") == "1"
    except Exception:
        return False


def _fmt_value(v):
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def build_header_block(info):
    """Render the diagnostic header as a #-prefixed block.

    `info` is a dict assembled by provider.chat_completion with keys:
    uuid, timestamp, channel_key, companion, provider, text_model,
    image_model, video_model, voice_text_model, context_limit, prompt_tokens,
    headroom_tokens, reasoning_effort, temperature, top_k,
    text_provider_lock, message_count, multimodal
    """
    keys_order = [
        ("uuid",              "uuid:"),
        ("timestamp",         "timestamp:"),
        ("channel_key",       "channel:"),
        ("companion",         "companion:"),
        ("provider",          "provider:"),
    ]
    models_order = [
        ("text_model",          "text_model:"),
        ("image_model",         "image_model:"),
        ("video_model",          "video_model:"),
        ("voice_text_model",    "voice_text_model:"),
    ]
    context_order = [
        ("context_limit",    "context_limit:"),
        ("prompt_tokens",    "prompt_tokens:"),
        ("headroom_tokens",  "headroom_tokens:"),
    ]
    sampling_order = [
        ("reasoning_effort",    "reasoning_effort:"),
        ("temperature",         "temperature:"),
        ("top_k",               "top_k:"),
        ("text_provider_lock",  "text_provider_lock:"),
    ]
    payload_order = [
        ("message_count", "message_count:"),
        ("multimodal",     "multimodal:"),
    ]

    def _section(title, pairs):
        lines = [f"# --- {title} ---"]
        for k, label in pairs:
            lines.append(f"# {label:<22s} {_fmt_value(info.get(k))}")
        return lines

    lines = ["# Alcove debug dump"]
    lines += _section("identity", keys_order)
    lines += _section("models", models_order)
    lines += _section("context", context_order)
    lines += _section("sampling", sampling_order)
    lines += _section("payload", payload_order)
    return "\n".join(lines)


def _render_content(content):
    """Render a single message's content for the readable dump.

    Strings render verbatim. Lists of blocks render text blocks as their
    text and all other block types as a one-line placeholder (the full
    base64 payload is preserved in the JSON section of the file).
    Returns a list of string lines (caller joins with \n).
    """
    if content is None:
        return ["(null content)"]
    if isinstance(content, str):
        return [content] if content else ["(empty string)"]
    if isinstance(content, list):
        out = []
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                out.append(f"[block {i}: non-dict {type(block).__name__}]")
                continue
            btype = block.get("type", "?")
            if btype == "text":
                txt = block.get("text", "")
                out.append(txt if txt else f"[block {i}: text block, empty]")
            else:
                # Estimate byte size for image_url / input_audio / inline_data
                size = 0
                payload = block.get("image_url") or block.get("input_audio") or block.get("data") or block
                try:
                    size = len(json.dumps(payload, default=str))
                except (TypeError, ValueError):
                    size = 0
                out.append(f"[block {i}: type={btype}, ~{size} bytes]")
        return out or ["(empty block list)"]
    return [f"(unexpected content type: {type(content).__name__})"]


def build_readable_dump(messages):
    """Render the messages list role-by-role for the readable section."""
    if not messages:
        return "(no messages)"
    parts = []
    bar = "─" * 30
    for msg in messages:
        if not isinstance(msg, dict):
            parts.append(f"{bar} (non-dict message) {bar}\n{msg!r}")
            continue
        role = msg.get("role", "?")
        parts.append(f"{bar} role: {role} {bar}")
        content_lines = _render_content(msg.get("content"))
        parts.append("\n".join(content_lines))
    return "\n\n".join(parts)


def _write_sync(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


async def write_prompt_file(debug_id, header_block, messages):
    """Write diag/<uuid>_prompt.txt = header + readable dump + raw JSON."""
    sep = "=" * 72
    readable = build_readable_dump(messages)
    try:
        raw_json = json.dumps(messages, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        raw_json = f"(failed to serialize messages: {e})"

    text = (
        f"{header_block}\n\n"
        f"{sep}\n"
        f"# READABLE DUMP\n"
        f"{sep}\n\n"
        f"{readable}\n\n"
        f"{sep}\n"
        f"# RAW JSON PAYLOAD (messages list)\n"
        f"{sep}\n\n"
        f"{raw_json}\n"
    )
    path = DEBUG_DIR / f"{debug_id}_prompt.txt"
    try:
        await asyncio.to_thread(_write_sync, path, text)
    except Exception as e:
        print(f"⚠️ [debug] failed to write prompt file {path.name}: {e}")


async def write_response_file(debug_id, data):
    """Write diag/<uuid>_response.txt = full raw provider JSON."""
    try:
        raw_json = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        raw_json = f"(failed to serialize response: {e})"
    path = DEBUG_DIR / f"{debug_id}_response.txt"
    try:
        await asyncio.to_thread(_write_sync, path, raw_json + "\n")
    except Exception as e:
        print(f"⚠️ [debug] failed to write response file {path.name}: {e}")


def now_iso_utc():
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
