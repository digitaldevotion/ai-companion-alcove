# ============================================
# Alcove — voice.py
# Voice channel management and TTS/STT
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import asyncio
import discord
from config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_VOICE_MODEL
from .voice_utils import mp3_to_pcm, play_pcm_on_voice_client

_ELEVENLABS_TIMEOUT = aiohttp.ClientTimeout(total=120)


async def get_elevenlabs_subscription():
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    used = int(data.get("character_count", 0))
                    limit = int(data.get("character_limit", 0))
                    remaining = max(limit - used, 0)
                    tier = data.get("tier", "?")
                    return {
                        "tier": tier,
                        "used": used,
                        "limit": limit,
                        "remaining": remaining,
                        "error": None,
                    }
                else:
                    body = (await resp.text())[:120]
                    return {
                        "tier": None, "used": 0, "limit": 0, "remaining": 0,
                        "error": f"error {resp.status} — {body}",
                    }
    except Exception as e:
        return {
            "tier": None, "used": 0, "limit": 0, "remaining": 0,
            "error": f"request failed — {e}",
        }

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

    async with aiohttp.ClientSession(timeout=_ELEVENLABS_TIMEOUT) as session:
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
    form.add_field("model_id", "scribe_v2")
    form.add_field("tag_audio_events", "true")

    async with aiohttp.ClientSession(timeout=_ELEVENLABS_TIMEOUT) as session:
        async with session.post(url, headers=headers, data=form) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("text", "")
            else:
                error = await response.text()
                print(f"⚠️ ElevenLabs STT error {response.status}: {error[:300]}")
                return None

# ============================================
# VOICE CHANNEL MANAGEMENT
# ============================================
class VoiceManager:
    # Manages the bot's voice channel connection and audio playback.

    def __init__(self):
        self.voice_client = None

    async def join(self, channel):
        try:
            guild_vc = channel.guild.voice_client

            if guild_vc and guild_vc.is_connected():
                self.voice_client = guild_vc
                if self.voice_client.channel == channel:
                    return True
                try:
                    await self.voice_client.move_to(channel)
                except Exception:
                    await guild_vc.disconnect(force=True)
                    self.voice_client = None
                    self.voice_client = await channel.connect()

            elif guild_vc and not guild_vc.is_connected():
                await guild_vc.disconnect(force=True)
                self.voice_client = None
                self.voice_client = await channel.connect()

            elif self.voice_client and self.voice_client.is_connected():
                if self.voice_client.channel == channel:
                    return True
                await self.voice_client.move_to(channel)

            else:
                self.voice_client = await channel.connect()

        except discord.ClientException as e:
            if "Already connected" in str(e):
                guild_vc = channel.guild.voice_client
                if guild_vc:
                    await guild_vc.disconnect(force=True)
                self.voice_client = None
                self.voice_client = await channel.connect()
            else:
                import traceback
                print(f"⚠️ Failed to join voice channel: {e}")
                traceback.print_exc()
                return False
        except Exception as e:
            import traceback
            print(f"⚠️ Failed to join voice channel: {e}")
            traceback.print_exc()
            return False
        print(f"🔊 Joined voice channel: {channel.name}")
        return True

    async def leave(self, guild=None):
        guild_vc = guild.voice_client if guild else None
        vc = self.voice_client if (self.voice_client and self.voice_client.is_connected()) else guild_vc
        if vc:
            channel_name = getattr(vc.channel, 'name', 'unknown')
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            self.voice_client = None
            print(f"🔇 Left voice channel: {channel_name}")
            return True
        self.voice_client = None
        return False

    def is_connected(self, guild=None):
        if self.voice_client and self.voice_client.is_connected():
            return True
        if guild and guild.voice_client and guild.voice_client.is_connected():
            self.voice_client = guild.voice_client
            return True
        return False

    async def play_audio(self, mp3_bytes):
        if not self.is_connected():
            print("⚠️ Not connected to a voice channel")
            return False

        wav_bytes = mp3_to_pcm(mp3_bytes)
        if wav_bytes is None:
            return False

        return await play_pcm_on_voice_client(self.voice_client, wav_bytes)

# Singleton instance
voice_manager = VoiceManager()
