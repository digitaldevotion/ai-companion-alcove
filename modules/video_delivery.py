# ============================================
# Alcove — video_delivery.py
# Video delivery: local save + Discord / litterbox.catbox.moe upload
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import base64
import io
import os
import re
from datetime import datetime
from pathlib import Path

import aiohttp
import discord

import config
from .utils import HTTP_PIN

_PROJECT_ROOT = Path(__file__).parent.parent
_DISCORD_HARD_CEILING = 100 * 1024 * 1024  # 100 MB — Discord's absolute max on any boosted server


def _slugify(text, max_len=40):
    """Make a filesystem-safe slug from prompt text."""
    slug = re.sub(r"[^\w\s-]", "", text or "video").strip().lower()
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:max_len] or "video"


def _resolve_video_dir():
    """Resolve the output directory for saved videos."""
    configured = getattr(config, "VIDEO_OUTPUT_DIR", "")
    if configured and str(configured).strip():
        return Path(configured)
    return _PROJECT_ROOT / "output" / "videos"


def _save_local(video_bytes, slug):
    """Save video bytes to output/videos/<timestamp>_<slug>.mp4. Never overwrites."""
    out_dir = _resolve_video_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{slug}.mp4"
    path = out_dir / name
    # Avoid clobbering if two land in the same second
    i = 1
    while path.exists():
        path = out_dir / f"{ts}_{slug}_{i}.mp4"
        i += 1
    with open(path, "wb") as f:
        f.write(video_bytes)
    return path


async def _fetch_url_bytes(url, headers=None):
    """Download bytes from a URL (300s timeout for large videos).
    Pass auth headers if the URL requires authentication (e.g. OpenRouter content endpoint).
    The Accept-Encoding pin is always merged in so downloads never negotiate
    encodings this environment can't decode (broken brotli class of failures).
    """
    merged = dict(headers or {})
    merged.update(HTTP_PIN)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.get(url, headers=merged) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.read()


async def _upload_litterbox(video_bytes, slug):
    """
    Upload to litterbox.catbox.moe (temporary catbox host, no account required).
    Auto-deleted after VIDEO_LITTERBOX_EXPIRE (1h/12h/24h/72h).
    Returns {"url": "...", "error": None} or {"url": None, "error": "..."}.
    """
    expire = str(getattr(config, "VIDEO_LITTERBOX_EXPIRE", "72h"))
    valid = ("1h", "12h", "24h", "72h")
    if expire not in valid:
        print(f"⚠️ VIDEO_LITTERBOX_EXPIRE='{expire}' invalid; falling back to '72h'")
        expire = "72h"
    filename = f"{slug}.mp4"
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", expire)
    form.add_field("fileToUpload", video_bytes, filename=filename, content_type="video/mp4")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data=form,
                headers=HTTP_PIN,
            ) as resp:
                text = (await resp.text()).strip()
                if resp.status != 200:
                    return {"url": None, "error": f"litterbox upload failed ({resp.status}): {text[:200]}"}
                # Litterbox returns the direct file URL as plain text
                # (e.g. https://litterbox.catbox.moe/abc123.mp4)
                if not text.startswith("http"):
                    return {"url": None, "error": f"litterbox returned non-URL: {text[:200]}"}
                return {"url": text, "error": None}
    except Exception as e:
        return {"url": None, "error": f"litterbox upload failed: {e}"}


def _fmt_size(n):
    """Human-readable byte size."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


async def deliver_video(channel, result, prompt_slug, send_func=None):
    """
    Deliver a generated video to Discord. Always saves a local copy; uploads
    to litterbox.catbox.moe (or Discord directly if ≤25 MB) per config.VIDEO_HOST.

    result = {"type":"base64","data":...} | {"type":"url","url":...} | {"type":"error","text":...}
    """
    if send_func is None:
        send_func = channel.send

    # Error — pass through
    if result is None:
        await send_func("*No response from video model. Check console for details.*")
        return
    if result.get("type") == "error":
        await send_func(result.get("text", "*Video generation failed.*"))
        return

    # Resolve video bytes
    try:
        if result["type"] == "base64":
            video_bytes = base64.b64decode(result["data"])
        elif result["type"] == "url":
            url = result["url"]
            await send_func("*⬇️ Downloading video...*")
            # OpenRouter content URLs need auth headers
            dl_headers = None
            if "openrouter.ai" in url:
                dl_headers = {"Authorization": f"Bearer {config.OPENROUTER_KEY}"}
            video_bytes = await _fetch_url_bytes(url, headers=dl_headers)
        else:
            await send_func(f"*Unexpected video result type: {result.get('type')}*")
            return
    except Exception as e:
        await send_func(f"*Failed to retrieve video data: {e}*")
        return

    if not video_bytes:
        await send_func("*Video model returned empty data.*")
        return

    # Always save locally
    slug = _slugify(prompt_slug)
    try:
        local_path = _save_local(video_bytes, slug)
        print(f"🎬 video saved locally: {local_path} ({_fmt_size(len(video_bytes))})")
    except Exception as e:
        print(f"⚠️ Failed to save video locally: {e}")
        local_path = None

    size_mb = len(video_bytes) / (1024 * 1024)
    fallback_reason = None  # set when direct Discord upload fails or is skipped

    # Always try Discord direct upload first (up to a hard ceiling to avoid
    # pointless uploads Discord can never accept, even on maximally boosted
    # servers). On any failure, fall back to the external host.
    if len(video_bytes) <= _DISCORD_HARD_CEILING:
        try:
            file = discord.File(io.BytesIO(video_bytes), filename="video.mp4")
            await send_func(file=file)
            if local_path:
                await send_func(f"*💾 Saved to output directory: `{local_path.name}` in `{local_path.parent}` (retained for {getattr(config, 'VIDEO_RETENTION_DAYS', 7)} days)*")
            return
        except Exception as e:
            print(f"⚠️ Discord direct upload failed ({_fmt_size(len(video_bytes))}): {e}")
            print(f"⚠️ Falling back to litterbox upload after Discord rejection")
            await send_func(f"*⚠️ Discord upload failed ({_fmt_size(len(video_bytes))}): {e}*")
            fallback_reason = "Discord upload failed"
    else:
        fallback_reason = "too large for Discord"

    # External host upload (too large for Discord, or direct upload failed)
    host = getattr(config, "VIDEO_HOST", "litterbox").lower()

    if fallback_reason == "too large for Discord":
        lines = [f"🎬 Generated video — too large for Discord ({_fmt_size(len(video_bytes))})."]
    else:
        lines = [f"🎬 Generated video — Discord upload failed ({_fmt_size(len(video_bytes))})."]

    if not host or host == "none":
        lines.append("*External upload disabled (VIDEO_HOST=\"none\").*")
    else:
        lines.append("*⬆️ Uploading to temporary host...*")
        await send_func("\n".join(lines))
        lines = []

        if host == "litterbox":
            upload = await _upload_litterbox(video_bytes, slug)
        else:
            upload = {"url": None, "error": f"Unknown VIDEO_HOST: {host}"}

        if upload["url"]:
            expire = str(getattr(config, "VIDEO_LITTERBOX_EXPIRE", "72h"))
            # Send the direct .mp4 URL as its own message so Discord auto-embeds
            # it and renders an inline player. Burying the URL inside a multi-
            # line block suppresses unfurling, so the metadata goes separately.
            await send_func(upload["url"])
            lines.append(f"*(litterbox link, auto-deleted after {expire})*")
        else:
            lines.append(f"*⚠️ Upload failed: {upload['error']}*")

    if local_path:
        lines.append(f"💾 Saved to output directory: `{local_path.name}` in `{local_path.parent}` (retained for {getattr(config, 'VIDEO_RETENTION_DAYS', 7)} days)")

    await send_func("\n".join(lines))
