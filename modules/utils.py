# ============================================
# Alcove — utils.py
# Shared helper utilities (e.g. Discord sending & chunking)
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import base64
from datetime import datetime
import discord
import io
import re


# Matches a leading Discord mention of a specific user id: <@123> or <@!123>.
_LEADING_MENTION_RE = re.compile(r"^\s*<@!?(\d+)>")


def strip_leading_bot_mention(content, bot_user_id):
    """If content starts with a Discord mention of bot_user_id, remove it.

    Returns the stripped content (with leading whitespace trimmed). If the
    leading mention does not target this bot, returns the original content
    unchanged (so a message addressing another bot is not mangled).
    """
    if not content or bot_user_id is None:
        return content
    m = _LEADING_MENTION_RE.match(content)
    if not m:
        return content
    if int(m.group(1)) != int(bot_user_id):
        return content
    return content[m.end():].lstrip()


BYTES_PER_TOKEN = 4

_channel_calibration = {}


def update_token_calibration(channel_key, prompt_bytes, actual_tokens):
    if not channel_key or actual_tokens <= 0:
        return
    observed = prompt_bytes / actual_tokens
    if channel_key not in _channel_calibration:
        _channel_calibration[channel_key] = (observed, 1)
    else:
        avg, n = _channel_calibration[channel_key]
        new_n = n + 1
        new_avg = avg + (observed - avg) / new_n
        _channel_calibration[channel_key] = (new_avg, new_n)


def get_bytes_per_token(channel_key=None):
    if channel_key and channel_key in _channel_calibration:
        return _channel_calibration[channel_key][0]
    return BYTES_PER_TOKEN


def tokens_to_bytes(tokens, channel_key=None):
    return int(tokens * get_bytes_per_token(channel_key))


def bytes_to_tokens(byte_len, channel_key=None):
    return int(byte_len / get_bytes_per_token(channel_key))


def estimate_tokens(text, channel_key=None):
    if text is None:
        return 0
    return bytes_to_tokens(len(text.encode("utf-8")), channel_key=channel_key)


def msg_tokens(msg, channel_key=None):
    c = msg.get("content", "")
    if isinstance(c, str):
        return estimate_tokens(c, channel_key=channel_key)
    if isinstance(c, list):
        return sum(estimate_tokens(b.get("text", ""), channel_key=channel_key) for b in c if isinstance(b, dict))
    return 0


def build_channel_key(guild_name, channel_name):
    return f"{guild_name.lower()}:::{channel_name.lower()}"


def build_dm_key(username):
    return f"dm:::{username.lower()}"


def parse_channel_key(channel_key):
    if not channel_key or ":::" not in channel_key:
        return None, None
    return channel_key.split(":::", 1)


async def safe_send(channel, content, timeout=30, **kwargs):
    try:
        return await asyncio.wait_for(channel.send(content, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[safe_send] timed out ({timeout}s) to #{getattr(channel, 'name', '?')} "
              f"at {datetime.now():%H:%M:%S} -- gateway may be disconnected")
        return None


def _chunk_text(text, limit):
    if not text:
        return
    if len(text) <= limit:
        yield text
        return
    remaining = text
    while len(remaining) > limit:
        sp = remaining[:limit].rfind('\n')
        if sp == -1:
            sp = remaining[:limit].rfind(' ')
        if sp == -1:
            sp = limit
        yield remaining[:sp]
        remaining = remaining[sp:].lstrip()
    if remaining:
        yield remaining


async def safe_send_chunked(channel, text, limit=2000, timeout=30, **kwargs):
    first = True
    for chunk in _chunk_text(text, limit):
        if not first:
            kwargs.pop("reference", None)
        await safe_send(channel, chunk, timeout=timeout, **kwargs)
        first = False


async def send_spoilered_code_blocks(channel, text, header=None, limit=2000, timeout=30):
    if not text:
        return
    if header:
        await safe_send(channel, header, timeout=timeout)
    chunk_budget = limit - 11
    for chunk in _chunk_text(text, chunk_budget):
        await safe_send(channel, f"||```\n{chunk}\n```||", timeout=timeout)


def send_chunked(channel, text, limit=2000, timeout=30):
    import warnings
    warnings.warn(
        "send_chunked is deprecated; use safe_send_chunked instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return safe_send_chunked(channel, text, limit=limit, timeout=timeout)


async def send_image_result(channel, result, send_func=None):
    if send_func is None:
        send_func = channel.send
    if result is None:
        await send_func("*No response from image model. Check console for details.*")
    elif result["type"] == "base64":
        image_bytes = base64.b64decode(result["data"])
        file = discord.File(io.BytesIO(image_bytes), filename="image.png")
        await send_func(file=file)
    elif result["type"] == "url":
        await send_func(result["url"])
    elif result["type"] == "error":
        await send_func(result["text"])
    else:
        await send_func(result.get("text", "*No image generated.*"))
