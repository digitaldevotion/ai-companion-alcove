# ============================================
# Alcove — voice_live.py
# Live (hands-free) voice chat support via pycord's native Sink API.
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt
# file for more information.
# ============================================
#
# This module is intentionally isolated from the rest of the codebase: the
# main bot continues to use pycord's standard VoiceClient, and only this
# file uses the Sink-based listening API. The seam is the LiveVoiceSession
# class — main.py constructs one when !joinLive runs and calls .stop()
# on !leave.
#
# Audio path:
#   pycord VoiceClient.start_listening() → PacketRouter → opus decode →
#   _LiveSink.write() (called from the PacketRouter thread with decoded
#   48kHz stereo s16le PCM frames) → per-user buffer + webrtcvad-based
#   end-of-utterance detection → on silence-after-speech we schedule the
#   supplied async callback on the bot's event loop with a wav-formatted
#   blob of the captured speech. The callback (provided by main.py) handles
#   STT → LLM → TTS → playback through the same voice client we used to
#   listen.
#
# DAVE STATUS: Voice reception is currently broken due to Discord's DAVE
# (End-to-End Encryption) protocol. pycord's start_listening() emits a
# RuntimeWarning about this. Tracked at:
#   https://github.com/Pycord-Development/pycord/issues/3139
# When that is resolved, this module should work without further changes.
#
# v1 limitations: single-user only, no barge-in, no wake word, no streaming
# STT/TTS. See the design notes in the !joinLive command for what's planned.
import asyncio
import io
import struct
import time
import traceback
import wave

try:
    import discord
    from discord.sinks import Sink
    _HAS_PYCORD = True
except Exception:
    discord = None
    Sink = None
    _HAS_PYCORD = False

try:
    import webrtcvad
    _HAS_WEBRTCVAD = True
except Exception:
    webrtcvad = None
    _HAS_WEBRTCVAD = False

from .voice_utils import mp3_to_pcm, play_pcm_on_voice_client


def dependencies_available():
    return _HAS_PYCORD and _HAS_WEBRTCVAD


def missing_dependencies():
    missing = []
    if not _HAS_PYCORD:
        missing.append("py-cord[voice]")
    if not _HAS_WEBRTCVAD:
        missing.append("webrtcvad")
    return missing


# Discord voice frames are 20ms of 48kHz stereo s16le PCM = 3840 bytes.
_FRAME_MS = 20
_INPUT_RATE = 48000
_INPUT_CHANNELS = 2
_INPUT_SAMPLE_BYTES = 2

# webrtcvad operates on mono 16-bit PCM at 8/16/32 kHz. We downmix + decimate
# 3:1 in pure Python — fine for 20ms frames in single-user mode. Aliasing
# from the unfiltered decimation is acceptable since VAD only cares about
# voiced/unvoiced energy, not faithful audio.
_VAD_RATE = 16000
_VAD_FRAME_BYTES = int(_VAD_RATE * _FRAME_MS / 1000) * 2  # 640 bytes / 20ms

# Minimum utterance length before we bother running STT. Filters out coughs,
# clicks, and stray short blips that webrtcvad sometimes flags as speech.
_MIN_UTTERANCE_MS = 300

# webrtcvad aggressiveness: 0 (least aggressive about silence) to 3 (most).
# 2 is a sensible default for a noisy room without false-triggering on
# every breath.
_VAD_AGGRESSIVENESS = 2


def _pcm48k_stereo_to_16k_mono(pcm_bytes):
    # 48kHz stereo s16le -> 16kHz mono s16le. Downmix by averaging L+R, then
    # decimate 3:1. Inputs are always whole frames (multiple of 4 bytes).
    n_pairs = len(pcm_bytes) // 4  # samples per channel
    if n_pairs == 0:
        return b""
    samples = struct.unpack(f"<{n_pairs * 2}h", pcm_bytes)
    mono = [(samples[i * 2] + samples[i * 2 + 1]) >> 1 for i in range(n_pairs)]
    out = mono[::3]
    return struct.pack(f"<{len(out)}h", *out)


def _pcm_to_wav_bytes(pcm_bytes, channels=_INPUT_CHANNELS, rate=_INPUT_RATE,
                     sample_width=_INPUT_SAMPLE_BYTES):
    # Wrap raw PCM in a WAV container so STT services accept it. We send the
    # original 48kHz stereo to STT (it's fine with that) — only the VAD path
    # needs the 16kHz mono version.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class _UtteranceState:
    # Per-user rolling state for the VAD state machine. Single-user mode
    # only ever has one of these, but we keep it keyed by user id so adding
    # multi-user later is just a matter of removing the user-id filter.
    __slots__ = ("buffer_48k", "speech_started", "silent_frame_count",
                 "speech_frame_count", "last_speech_ts")

    def __init__(self):
        self.buffer_48k = bytearray()
        self.speech_started = False
        self.silent_frame_count = 0
        self.speech_frame_count = 0
        self.last_speech_ts = 0.0


class LiveVoiceSession:
    # Owns the voice connection for the lifetime of a !joinLive session.
    # main.py calls .start(channel, ...) to connect and begin listening,
    # and .stop() to tear it all down.
    #
    # The on_utterance callback is invoked once per detected end-of-utterance
    # with (wav_bytes, member). It must be a coroutine function — it'll run
    # on the bot's event loop, scheduled via run_coroutine_threadsafe from
    # the audio worker thread.

    def __init__(self, *, silence_ms, idle_timeout_min, single_user,
                 on_utterance):
        self.silence_ms = int(silence_ms)
        self.idle_timeout_min = int(idle_timeout_min)
        self.single_user = bool(single_user)
        self.on_utterance = on_utterance

        # How many trailing silent frames before we finalize an utterance.
        self._silence_frames_threshold = max(1, self.silence_ms // _FRAME_MS)
        # Same metric for the minimum-utterance filter.
        self._min_speech_frames = max(1, _MIN_UTTERANCE_MS // _FRAME_MS)

        self.voice_client = None  # pycord VoiceClient once connected
        self.text_channel = None  # discord.TextChannel for status messages
        self.target_user_id = None  # only used when single_user is True
        self.target_member = None
        self._loop = None
        self._sink = None
        self._idle_check_task = None
        self._last_recognized_speech_ts = 0.0
        self._stopped = False

    async def start(self, voice_channel, *, text_channel, author, loop):
        # Connect to the voice channel using pycord's standard VoiceClient
        # (which supports native voice receiving via start_listening).
        # Caller is responsible for ensuring no other VoiceClient is currently
        # connected — we don't try to coexist with the push-to-talk
        # voice_manager.
        if not dependencies_available():
            raise RuntimeError(
                f"livevoice missing dependencies: {missing_dependencies()}"
            )

        self.text_channel = text_channel
        self.target_member = author
        self.target_user_id = author.id if self.single_user else None
        self._loop = loop

        self.voice_client = await voice_channel.connect()
        print(f"🔊 [livevoice] Connected to {voice_channel.name} "
              f"(target user: {author.display_name if self.single_user else 'all'})")

        self._sink = _LiveSink(self)
        self.voice_client.start_listening(self._sink)
        self._last_recognized_speech_ts = time.time()
        self._idle_check_task = asyncio.create_task(self._idle_watchdog())

    async def stop(self):
        # Idempotent: safe to call multiple times. Stops the recording sink,
        # cancels the idle watchdog, and disconnects.
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._idle_check_task is not None:
                self._idle_check_task.cancel()
        except Exception:
            pass
        try:
            if self.voice_client is not None and self.voice_client.is_connected():
                try:
                    self.voice_client.stop_listening()
                except Exception:
                    pass
                await self.voice_client.disconnect()
                print("🔇 [livevoice] Disconnected")
        except Exception as e:
            print(f"⚠️ [livevoice] error during stop: {e}")
            traceback.print_exc()

    def is_active(self):
        return (
            not self._stopped
            and self.voice_client is not None
            and self.voice_client.is_connected()
        )

    async def play_audio_bytes(self, mp3_bytes):
        if not self.is_active():
            return False
        wav_bytes = mp3_to_pcm(mp3_bytes)
        if wav_bytes is None:
            return False
        return await play_pcm_on_voice_client(self.voice_client, wav_bytes, log_prefix="[livevoice] ")

    async def _idle_watchdog(self):
        # Auto-disconnect after IDLE_TIMEOUT_MIN of no recognized speech.
        try:
            while self.is_active():
                await asyncio.sleep(15)
                idle_sec = time.time() - self._last_recognized_speech_ts
                if idle_sec > self.idle_timeout_min * 60:
                    print(f"⏰ [livevoice] idle for {idle_sec:.0f}s — auto-disconnect")
                    if self.text_channel is not None:
                        try:
                            await self.text_channel.send(
                                f"*🎙️ Live voice idle for "
                                f"{self.idle_timeout_min} min — disconnected.*"
                            )
                        except Exception:
                            pass
                    await self.stop()
                    return
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"⚠️ [livevoice] idle watchdog error: {e}")

    # --- Sink callback bridge ---------------------------------------------
    # _LiveSink runs on the PacketRouter worker thread. These methods are
    # how it talks back to the session. They must be thread-safe — anything
    # that needs to touch the event loop has to go through
    # run_coroutine_threadsafe.

    def _on_utterance_ready_threadsafe(self, pcm_48k_stereo, member):
        # Called from the audio thread once VAD decides an utterance is
        # complete. Schedule the async callback on the bot's event loop.
        if self._stopped or self._loop is None:
            return
        self._last_recognized_speech_ts = time.time()
        try:
            wav_bytes = _pcm_to_wav_bytes(pcm_48k_stereo)
        except Exception as e:
            print(f"⚠️ [livevoice] wav-encode failed: {e}")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._dispatch_utterance(wav_bytes, member),
                self._loop,
            )
        except Exception as e:
            print(f"⚠️ [livevoice] schedule utterance failed: {e}")

    async def _dispatch_utterance(self, wav_bytes, member):
        # Async wrapper so any exceptions in the user-supplied callback
        # surface in the bot's normal log path instead of a thread crash.
        try:
            await self.on_utterance(wav_bytes, member)
        except Exception as e:
            print(f"⚠️ [livevoice] on_utterance callback raised: {e}")
            traceback.print_exc()


# ---------- Sink implementation -------------------------------------------
# Defined conditionally because pycord may not have the Sink API available
# on installs that don't have the voice deps. dependencies_available()
# gates the only code path that constructs this.
if _HAS_PYCORD and Sink is not None:

    class _LiveSink(Sink):
        # Per-user buffering + webrtcvad state machine. write() is invoked
        # from the PacketRouter worker thread for every decoded audio frame
        # pycord sends us, so everything in here must be cheap and
        # thread-safe.
        #
        # pycord's PacketRouter calls sink.write(data, user) where data is a
        # VoiceData object with a .pcm attribute (decoded 48kHz stereo s16le
        # bytes) and .source is the Member. The user parameter is the same
        # Member/Object.

        def __init__(self, session):
            super().__init__()
            self.session = session
            self._states = {}  # user_id -> _UtteranceState
            self._vad = webrtcvad.Vad(_VAD_AGGRESSIVENESS) if _HAS_WEBRTCVAD else None

        def is_opus(self):
            # We want decoded PCM, not opus packets — VAD needs raw samples
            # and STT wants wav.
            return False

        def write(self, data, user):
            # pycord passes VoiceData as `data` and Member/User/Object as
            # `user`. data.pcm is 48kHz stereo s16le (or empty if opus).
            # `user` may be a discord.Object with just an .id if SSRC
            # mapping hasn't resolved yet.
            if user is None or data is None:
                return
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            # Single-user filter.
            if self.session.single_user:
                if self.session.target_user_id is None:
                    return
                user_id = getattr(user, "id", None)
                if user_id is None or user_id != self.session.target_user_id:
                    return
            try:
                self._process_frame(user, pcm)
            except Exception as e:
                # Never let an exception escape into the audio thread —
                # pycord's PacketRouter tends to tear down the sink if it
                # does, killing the whole session.
                print(f"⚠️ [livevoice] sink frame error: {e}")

        def _process_frame(self, user, pcm_48k_stereo):
            user_id = getattr(user, "id", None)
            if user_id is None:
                return
            state = self._states.get(user_id)
            if state is None:
                state = _UtteranceState()
                self._states[user_id] = state

            # VAD on a downsampled mono copy. We ALSO keep the original
            # 48kHz stereo bytes in the buffer so we can hand a faithful wav
            # to STT — only VAD needs the 16k mono.
            try:
                pcm_16k_mono = _pcm48k_stereo_to_16k_mono(pcm_48k_stereo)
            except Exception:
                return
            if len(pcm_16k_mono) != _VAD_FRAME_BYTES:
                # Should be exactly 640 bytes for a 20ms frame; bail safely
                # if we ever get an off-size frame.
                return
            try:
                is_speech = self._vad.is_speech(pcm_16k_mono, _VAD_RATE)
            except Exception:
                return

            if is_speech:
                state.buffer_48k.extend(pcm_48k_stereo)
                state.speech_frame_count += 1
                state.silent_frame_count = 0
                state.speech_started = True
                state.last_speech_ts = time.time()
            else:
                if state.speech_started:
                    # Keep a little trailing silence for natural-sounding STT
                    # input; webrtcvad is choppy and STT engines are fine
                    # with short silences embedded in the audio.
                    state.buffer_48k.extend(pcm_48k_stereo)
                    state.silent_frame_count += 1
                    if state.silent_frame_count >= self.session._silence_frames_threshold:
                        # Utterance complete.
                        if state.speech_frame_count >= self.session._min_speech_frames:
                            self.session._on_utterance_ready_threadsafe(
                                bytes(state.buffer_48k), user,
                            )
                        # Reset whether or not we forwarded — short blips
                        # get dropped silently.
                        self._states[user_id] = _UtteranceState()

        def cleanup(self):
            # Called by pycord on disconnect. Flush any in-flight
            # utterance so we don't lose the last thing the user said.
            for user_id, state in list(self._states.items()):
                if state.speech_started and state.speech_frame_count >= self.session._min_speech_frames:
                    # We don't have a Member object here readily; skip the
                    # final flush for now. Future improvement: cache the
                    # member reference alongside the state.
                    pass
            self._states.clear()

else:
    # Stub so type-checkers don't complain — never instantiated when the
    # optional deps are missing because dependencies_available() returns
    # False and !joinLive bails before getting here.
    class _LiveSink:  # pragma: no cover
        def __init__(self, *_a, **_kw):
            raise RuntimeError("pycord voice receiving not available")
