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
import importlib
import io
import re


# ── HTTP content-encoding pin ─────────────────────────────────────────────
# aiohttp advertises "br"/"zstd" in Accept-Encoding whenever brotli/zstd
# modules are IMPORTABLE in the interpreter — but an importable-yet-broken
# install (e.g. conda base's old brotlipy shadowing Google's Brotli, or a
# clobbered C extension) then fails on real payloads with
# "Can not decode content-encoding: br", breaking provider connectivity
# for metadata endpoints (credits, model lists, auto-context lookups).
# Pinning Accept-Encoding to codecs aiohttp can ALWAYS decode natively
# (zlib is compiled into CPython itself) makes Alcove's HTTP behavior
# identical in every environment, regardless of what optional packages
# other software has left in site-packages.
HTTP_PIN = {"Accept-Encoding": "gzip, deflate"}


def _probe_compression_module(names):
    """Classify an optional HTTP-compression backend.

    Returns (state, module_name) where state is:
      "ok"     — importable and passes a compress→decompress round-trip
      "broken" — importable but the round-trip fails (the state that makes
                 aiohttp negotiate encodings it cannot actually decode)
      "absent" — not importable (aiohttp never advertises it; harmless)
    """
    for name in names:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        try:
            sample = b"alcove-compression-probe" * 4
            compress = getattr(mod, "compress", None)
            decompress = getattr(mod, "decompress", None)
            if callable(compress) and callable(decompress):
                ok = decompress(compress(sample)) == sample
                return ("ok" if ok else "broken"), name
            if hasattr(mod, "ZstdCompressor") and hasattr(mod, "ZstdDecompressor"):
                # zstandard/stdlib-style API (no module-level compress/decompress)
                obj = mod.ZstdDecompressor().decompressobj()
                out = obj.decompress(mod.ZstdCompressor().compress(sample))
                ok = (out + obj.flush()) == sample
                return ("ok" if ok else "broken"), name
            # Importable but no recognizable round-trip API — report presence
            # without claiming it is broken (avoids false warnings).
            return "present", name
        except Exception:
            return "broken", name
    return "absent", None


def compression_env_state():
    """Fingerprint of the runtime's HTTP compression environment for !diag.

    Never raises — every probe is exception-guarded so diagnostics cannot
    themselves take the bot down on a machine with exotic breakage.
    """
    brotli_state, brotli_name = _probe_compression_module(("brotli", "brotlicffi"))
    zstd_state, zstd_name = _probe_compression_module(
        ("compression.zstd", "backports.zstd", "zstandard")
    )
    try:
        import aiohttp
        aiohttp_ver = aiohttp.__version__
    except Exception:
        aiohttp_ver = "?"
    return {
        "brotli": brotli_state, "brotli_module": brotli_name,
        "zstd": zstd_state, "zstd_module": zstd_name,
        "aiohttp": aiohttp_ver,
    }


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


def update_token_calibration(channel_key, prompt_bytes, actual_tokens, force=False):
    """Blend a bytes-per-token observation into the channel's running average.

    force=True REPLACES the average instead of blending (n reset to 1) —
    used by context-overflow self-heal, where the provider has just
    reported the exact token count for the failing payload and the
    running average may be polluted by prior bad samples.
    """
    if not channel_key or actual_tokens <= 0:
        return
    observed = prompt_bytes / actual_tokens
    if force or channel_key not in _channel_calibration:
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
        total = 0
        for b in c:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                total += estimate_tokens(b.get("text", ""), channel_key=channel_key)
            elif btype == "image_url":
                url = b.get("image_url", {}).get("url", "")
                payload = url.split(",", 1)[1] if "," in url else url
                total += max(85, len(payload) // 1333)
            elif btype == "input_audio":
                data = b.get("input_audio", {}).get("data", "")
                total += max(100, len(data) // 1333)
        return total
    return 0


def msg_content_bytes(msg):
    """Content-only UTF-8 byte length of a message — the exact basis
    estimate_tokens/msg_tokens measure. Used for token-ratio calibration
    (see provider.chat_completion) so the calibrated bytes-per-token ratio
    is derived from the same byte counts the estimator uses, instead of
    JSON-serialized bytes (role labels, quotes, escaping) the estimator
    never sees. Text blocks only; multimodal payloads are excluded
    (calibration is gated to pure-text prompts anyway).
    """
    c = msg.get("content", "")
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c.encode("utf-8"))
    if isinstance(c, list):
        total = 0
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                total += len(b.get("text", "").encode("utf-8"))
        return total
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
