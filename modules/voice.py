# ============================================
# Alcove — voice.py
# Voice channel management and TTS/STT
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import asyncio
import discord
import time
from config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_VOICE_MODEL
from . import state
from .utils import HTTP_PIN
from .voice_utils import mp3_to_pcm, play_pcm_on_voice_client

_ELEVENLABS_TIMEOUT = aiohttp.ClientTimeout(total=120)


async def get_elevenlabs_subscription():
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": ELEVENLABS_API_KEY, **HTTP_PIN},
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
        **HTTP_PIN,
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
        **HTTP_PIN,
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
        # Inactivity timeout (push-to-talk mode). Mirrors LiveVoiceSession's
        # _idle_watchdog: after LIVEVOICE_INACTIVITY_TIMEOUT_MIN minutes with
        # no successful TTS playback, auto-disconnect. Activity is tracked as
        # the timestamp of the last voice reply (play_audio() success).
        self.text_channel = None
        self._inactivity_task = None
        self._last_voice_reply_ts = 0.0

    async def join(self, channel, text_channel=None):
        # `text_channel` is the Discord text channel the !join command was
        # issued from; used to post the inactivity-disconnect notice. May be
        # None (programmatic joins) — in that case the watchdog still runs
        # but disconnects silently.
        try:
            guild_vc = channel.guild.voice_client

            if guild_vc and guild_vc.is_connected():
                self.voice_client = guild_vc
                if self.voice_client.channel == channel:
                    pass
                else:
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
                    pass
                else:
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

        # (Re)start the inactivity watchdog for this connection. A successful
        # join counts as activity, so the clock starts fresh here. Re-joins /
        # moves also reset the clock so a stale task from a prior session
        # doesn't fire prematurely.
        self.text_channel = text_channel
        self._last_voice_reply_ts = time.time()
        await self._restart_inactivity_watchdog()
        return True

    async def leave(self, guild=None):
        guild_vc = guild.voice_client if guild else None
        vc = self.voice_client if (self.voice_client and self.voice_client.is_connected()) else guild_vc
        # Cancel the inactivity watchdog regardless of whether a vc is found,
        # so a stale task from a torn-down connection doesn't linger.
        await self._cancel_inactivity_watchdog()
        self.text_channel = None
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

        ok = await play_pcm_on_voice_client(self.voice_client, wav_bytes)
        # A successful playback counts as a voice reply — reset the
        # inactivity clock so the watchdog doesn't fire right after the
        # bot spoke. Mirrors LiveVoiceSession._last_interaction_ts.
        if ok:
            self._last_voice_reply_ts = time.time()
        return ok

    async def _restart_inactivity_watchdog(self):
        await self._cancel_inactivity_watchdog()
        self._inactivity_task = asyncio.create_task(self._inactivity_watchdog())

    async def _cancel_inactivity_watchdog(self):
        task = self._inactivity_task
        self._inactivity_task = None
        if task is None:
            return
        try:
            task.cancel()
        except Exception:
            pass
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _inactivity_watchdog(self):
        # Auto-disconnect after LIVEVOICE_INACTIVITY_TIMEOUT_MIN of no voice
        # replies (i.e. no successful play_audio() calls). Mirrors
        # LiveVoiceSession._idle_watchdog (voice_live.py:252). Runs on the
        # event loop, so reading/writing _last_voice_reply_ts is safe.
        try:
            timeout_sec = getattr(state, "LIVEVOICE_INACTIVITY_TIMEOUT_MIN", 30) * 60
            while self.is_connected():
                await asyncio.sleep(15)
                idle_sec = time.time() - self._last_voice_reply_ts
                if idle_sec > timeout_sec:
                    print(f"⏰ [voice] inactive for {idle_sec:.0f}s — auto-disconnect")
                    if self.text_channel is not None:
                        try:
                            await self.text_channel.send(
                                f"*🎙️ Voice inactive for "
                                f"{getattr(state, 'LIVEVOICE_INACTIVITY_TIMEOUT_MIN', 30)} "
                                f"min — disconnected.*"
                            )
                        except Exception:
                            pass
                    # Null our own task reference BEFORE calling leave(), so
                    # leave()._cancel_inactivity_watchdog() sees None and
                    # returns immediately instead of trying to cancel/await
                    # the very task we're currently running in (which would
                    # deadlock).
                    self._inactivity_task = None
                    await self.leave()
                    return
        except asyncio.CancelledError:
            return

# Singleton instance
voice_manager = VoiceManager()
