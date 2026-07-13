# ============================================
# Alcove — voice_tts_handler.py
# Post-response TTS playback
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

from .voice import voice_manager, text_to_speech
from .utils import safe_send


async def handle_tts(tts_text, tts_voice_id, message):
    if not tts_text:
        return
    try:
        audio_bytes = await text_to_speech(tts_text, voice_id=tts_voice_id)
        if audio_bytes:
            await voice_manager.play_audio(audio_bytes)
        else:
            await safe_send(message.channel, "*Couldn't generate voice response.*")
    except Exception as e:
        print(f"⚠️ TTS playback after typing failed: {e}")
