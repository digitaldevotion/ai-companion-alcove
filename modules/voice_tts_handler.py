# ============================================
# Alcove — voice_tts_handler.py
# Post-response TTS playback
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import io

import discord

from .voice import voice_manager, text_to_speech
from .utils import safe_send


async def handle_tts(tts_text, tts_voice_id, message=None, fallback_channel=None):
    if not tts_text:
        return
    fallback_ch = getattr(message, "channel", None) or fallback_channel
    try:
        audio_bytes = await text_to_speech(tts_text, voice_id=tts_voice_id)
        if audio_bytes:
            if fallback_ch is not None:
                try:
                    file = discord.File(io.BytesIO(audio_bytes), filename="response.mp3")
                    await fallback_ch.send(file=file)
                except Exception as e:
                    print(f"⚠️ TTS mp3 upload failed: {e}")
            await voice_manager.play_audio(audio_bytes)
        elif fallback_ch is not None:
            await safe_send(fallback_ch, "*Couldn't generate voice response.*")
    except Exception as e:
        print(f"⚠️ TTS playback after typing failed: {e}")
