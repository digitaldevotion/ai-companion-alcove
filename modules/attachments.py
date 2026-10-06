# ============================================
# Alcove — attachments.py
# Attachment processing (voice, images, audio, text files)
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import aiohttp
import base64
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import discord

import config
from .voice import speech_to_text
from .utils import safe_send, safe_send_chunked, HTTP_PIN


@dataclass
class AttachmentResult:
    is_voice_message: bool = False
    voice_transcription: str = ""
    text_attachment_content: str = ""
    image_attachments: list = field(default_factory=list)
    video_attachments: list = field(default_factory=list)
    audio_attachments: list = field(default_factory=list)
    should_return: bool = False


async def url_to_data_uri(url, timeout=30):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=HTTP_PIN) as resp:
                if resp.status != 200:
                    return None
                content_type = resp.headers.get("Content-Type", "image/png")
                if not content_type.startswith("image/"):
                    content_type = "image/png"
                raw = await resp.read()
                b64 = base64.b64encode(raw).decode("ascii")
                return f"data:{content_type};base64,{b64}"
    except Exception as e:
        print(f"⚠️ url_to_data_uri failed for {url}: {e}")
        return None


async def resolve_attachment_source(message, carry_prior_attachments,
                                    media_discord_msg_id=None):
    """Resolve which Discord message supplies this turn's attachments.

    Returns (attachment_source, carried):
      - attachment_source: the message whose attachments should be processed
      - carried: True when attachment_source is a prior message (regen media
        re-attach), False when it is the live message itself.

    Regens recover only text from the DB — media is re-attached by fetching
    the exact Discord message stored in the channel's
    last_media_discord_msg_id setting, written when the media turn was
    saved. No history scanning: a proximity scan can cross the !clear
    boundary and graft attachments from an unrelated turn onto the
    regenerated prompt. If the anchor is missing or the fetch fails, fall
    back to the live message (text-only regen).
    """
    attachment_source = message
    if carry_prior_attachments and not message.attachments:
        if not media_discord_msg_id:
            print("🔁 regen: no stored media message for this channel — "
                  "regenerating with text only")
            return attachment_source, False
        try:
            prev = await message.channel.fetch_message(int(media_discord_msg_id))
        except Exception as e:
            print(f"🔁 regen: couldn't fetch stored media message "
                  f"{media_discord_msg_id} ({e}) — regenerating with text only")
            return attachment_source, False
        if prev.author.bot or not prev.attachments:
            print("🔁 regen: stored media message has no usable attachments — "
                  "regenerating with text only")
            return attachment_source, False
        print(f"🔁 regen: reusing attachments from stored message "
              f"{media_discord_msg_id} ({len(prev.attachments)} file(s))")
        return prev, True
    return attachment_source, False


async def process_attachments(attachment_source, message, carried_prior=False):
    result = AttachmentResult()
    _msg_is_voice = getattr(message.flags, 'is_voice_message', False)

    for a in attachment_source.attachments:
        if a.content_type and a.content_type.startswith("audio/"):
            if _msg_is_voice:
                result.is_voice_message = True
                if a.duration_secs is not None and a.duration_secs < (config.STANDARD_VOICE_MIN_UTTERANCE_MS / 1000.0):
                    await safe_send(message.channel, "*I'm sorry, I didn't get that.*")
                    result.should_return = True
                    return result
                try:
                    audio_bytes = await a.read()
                    transcription = await speech_to_text(audio_bytes, a.filename)
                    if transcription:
                        result.voice_transcription += transcription + " "
                        print(f"🎤 Transcribed: {transcription[:100]}")
                    else:
                        await safe_send(message.channel, "*Couldn't transcribe voice message.*")
                except Exception as e:
                    print(f"⚠️ Failed to transcribe voice message: {e}")
            else:
                result.audio_attachments.append(a)
        elif a.content_type and a.content_type.startswith("text/"):
            if carried_prior:
                # Text files are prompt content, not reference media: their
                # text was already merged into the recovered regen prompt
                # when the original turn was saved, so re-reading them here
                # would duplicate it. Only a text file attached to the live
                # !regen message itself is treated as new input.
                print(f"🔁 regen: skipping text attachment from prior message "
                      f"({a.filename}) — already part of the prompt")
                continue
            try:
                file_bytes = await a.read()
                result.text_attachment_content += file_bytes.decode("utf-8", errors="replace") + "\n"
            except Exception as e:
                print(f"⚠️ Failed to read text attachment: {e}")
        elif a.content_type and a.content_type.startswith("image/"):
            result.image_attachments.append(a)
        elif a.content_type and a.content_type.startswith("video/"):
            result.video_attachments.append(a)

    if result.voice_transcription:
        await safe_send_chunked(message.channel, f"What I heard:\n{result.voice_transcription.strip()}")
        await safe_send(message.channel, "━━━━━━━━━━━━━━━━━━")

    return result


def build_combined_content(content, voice_transcription, text_attachment_content):
    combined_content = content
    if voice_transcription:
        combined_content = f"{content} {voice_transcription}".strip()
    if text_attachment_content:
        combined_content = f"{combined_content}\n{text_attachment_content}".strip()
    return combined_content


async def build_multimodal_user_content(att_result, combined_content, author_name,
                                         time_str, day_name, date_str, mode_tag=""):
    """Build multi-part user content for image/audio/video attachments.

    Returns (user_content_list, image_data_uris, video_urls, save_text) where:
      - user_content_list: list of content blocks for the LLM message
      - image_data_uris: list of data URI strings for reference images
      - video_urls: list of Discord CDN URLs for reference videos
      - save_text: the text to save to DB (with [image]/[audio]/[video] tags)
    """
    user_content = []
    image_data_uris = []
    video_urls = []

    if att_result.image_attachments:
        _fetched = await asyncio.gather(
            *[url_to_data_uri(a.url) for a in att_result.image_attachments],
            return_exceptions=True,
        )
        image_data_uris = [u for u in _fetched if isinstance(u, str)]

    if att_result.video_attachments:
        video_urls = [a.url for a in att_result.video_attachments]

    audio_blocks = []
    if att_result.audio_attachments:
        for a in att_result.audio_attachments:
            audio_bytes = await a.read()
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            ext = a.filename.rsplit(".", 1)[-1].lower() if "." in a.filename else "ogg"
            audio_blocks.append({
                "type": "input_audio",
                "input_audio": {"data": audio_b64, "format": ext}
            })
            print(f"🎵 Audio attachment: {a.filename} ({ext}, {len(audio_bytes)} bytes)")

    _has_images = bool(att_result.image_attachments)
    _has_audio = bool(att_result.audio_attachments)
    _has_videos = bool(att_result.video_attachments)
    text = (
        f"({time_str} {day_name} {date_str}){mode_tag}:{author_name}: {combined_content}"
        if combined_content
        else f"({time_str} {day_name} {date_str}){mode_tag}:{author_name} sent an attachment"
    )
    if _has_images:
        text += '\n[Reference image attached — use <createimage use="reference"> to generate an image incorporating it. The image(s) are already embedded in this message as visual content; do NOT use the <readimage> tool to access them.]'
    if _has_videos:
        text += '\n[Reference video attached — use <createvideo use="reference"> to generate a video incorporating it. The video URL(s) are noted below for the video generation tool; do NOT use the <readimage> tool to access them.]'
    user_content.append({"type": "text", "text": text})
    if _has_videos:
        for vurl in video_urls:
            user_content.append({
                "type": "text",
                "text": f"[Reference video URL: {vurl}]"
            })
    if att_result.image_attachments:
        for data_uri in image_data_uris:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": data_uri}
            })
    if att_result.audio_attachments:
        user_content.extend(audio_blocks)

    _tags = []
    if _has_images:
        _tags.append("[image]")
    if _has_audio:
        _tags.append("[audio]")
    if _has_videos:
        _tags.append("[video]")
    save_text = (
        f"{combined_content} {' '.join(_tags)}" if combined_content else " ".join(_tags)
    )

    return user_content, image_data_uris, video_urls, save_text
