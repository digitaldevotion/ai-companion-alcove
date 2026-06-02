# ============================================
# Alcove v1.3.0 — voice.py
# Voice channel management and TTS/STT
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import asyncio
import discord
import os
import subprocess
import tempfile
from config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_VOICE_MODEL

# ============================================
# TEXT TO SPEECH (ElevenLabs)
# ============================================
async def text_to_speech(text, voice_id=None):
    # Convert text to audio bytes (mp3) via ElevenLabs TTS.
    # `voice_id` lets callers override the default ElevenLabs voice on a
    # per-call basis (e.g. channel-specific overrides from main.py).
    effective_voice_id = voice_id or ELEVENLABS_VOICE_ID
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{effective_voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_VOICE_MODEL,
        "voice_settings": {
            "stability": 0.2,
            "similarity_boost": 0.2,
            "style": 0.3,
            "use_speaker_boost": True,
            "speed": 1.1,
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status == 200:
                audio_bytes = await response.read()
                return audio_bytes
            else:
                error = await response.text()
                print(f"⚠️ ElevenLabs TTS error {response.status}: {error[:300]}")
                return None

# ============================================
# SPEECH TO TEXT (ElevenLabs)
# ============================================
async def speech_to_text(audio_bytes, filename="audio.ogg"):
    # Transcribe audio bytes to text via ElevenLabs STT.
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
    }

    # Derive content-type from the filename extension so callers can pass
    # wav/mp3/ogg without the server having to sniff (most common cases:
    # voice messages arrive as .ogg, !joinLive captures arrive as .wav).
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
    content_type = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }.get(ext, "audio/ogg")
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type=content_type)
    form.add_field("model_id", "scribe_v1")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("text", "")
            else:
                error = await response.text()
                print(f"⚠️ ElevenLabs STT error {response.status}: {error[:300]}")
                return None

# ============================================
# AUDIO FORMAT CONVERSION
# ============================================
def mp3_to_pcm(mp3_bytes):
    # Convert mp3 bytes to PCM audio using ffmpeg (for discord.py voice playback).
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_in:
        tmp_in.write(mp3_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".mp3", ".wav")

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in_path, "-f", "wav",
             "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
             tmp_out_path],
            capture_output=True, check=True
        )
        with open(tmp_out_path, "rb") as f:
            return f.read()
    except subprocess.CalledProcessError as e:
        print(f"⚠️ ffmpeg error: {e.stderr.decode()[:300]}")
        return None
    finally:
        os.unlink(tmp_in_path)
        if os.path.exists(tmp_out_path):
            os.unlink(tmp_out_path)

# ============================================
# VOICE CHANNEL MANAGEMENT
# ============================================
class VoiceManager:
    # Manages the bot's voice channel connection and audio playback.

    def __init__(self):
        self.voice_client = None

    async def join(self, channel):
        # Join a voice channel. Returns True on success.
        try:
            if self.voice_client and self.voice_client.is_connected():
                if self.voice_client.channel == channel:
                    return True  # Already in this channel
                await self.voice_client.move_to(channel)
            else:
                self.voice_client = await channel.connect()
            print(f"🔊 Joined voice channel: {channel.name}")
            return True
        except Exception as e:
            import traceback
            print(f"⚠️ Failed to join voice channel: {e}")
            traceback.print_exc()
            return False

    async def leave(self):
        # Leave the current voice channel.
        if self.voice_client and self.voice_client.is_connected():
            channel_name = self.voice_client.channel.name
            await self.voice_client.disconnect()
            self.voice_client = None
            print(f"🔇 Left voice channel: {channel_name}")
            return True
        return False

    def is_connected(self):
        # Check if the bot is currently in a voice channel.
        return self.voice_client is not None and self.voice_client.is_connected()

    async def play_audio(self, mp3_bytes):
        # Convert mp3 to PCM and play it in the voice channel.
        if not self.is_connected():
            print("⚠️ Not connected to a voice channel")
            return False

        wav_bytes = mp3_to_pcm(mp3_bytes)
        if wav_bytes is None:
            return False

        # Write wav to a temp file for FFmpegPCMAudio
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name

        try:
            # Wait for any current audio to finish
            while self.voice_client.is_playing():
                await asyncio.sleep(0.1)

            # Play the audio
            audio_source = discord.FFmpegPCMAudio(tmp_path)
            done_event = asyncio.Event()

            def after_playing(error):
                if error:
                    print(f"⚠️ Playback error: {error}")
                # Schedule cleanup on the event loop
                done_event.set()

            self.voice_client.play(audio_source, after=after_playing)
            # Wait for playback to complete before cleaning up the temp file
            await done_event.wait()
        finally:
            # Small delay to ensure file handle is released
            await asyncio.sleep(0.2)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        return True

# Singleton instance
voice_manager = VoiceManager()
