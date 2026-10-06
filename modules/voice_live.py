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
import threading
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
    # webrtcvad-wheels is the maintained fork (prebuilt wheels, no `import
    # pkg_resources`); the abandoned `webrtcvad` 2.0.10 breaks on setuptools
    # 81+. Both publish the same `webrtcvad` import name + API, so this import
    # is satisfied by whichever is installed. install_deps.py ensures the fork.
    import webrtcvad
    _HAS_WEBRTCVAD = True
    _WEBRTCVAD_IMPORT_ERROR = None
except Exception as _webrtcvad_import_err:
    webrtcvad = None
    _HAS_WEBRTCVAD = False
    # Preserve the real failure so the !joinLive message can distinguish
    # "not installed" from "installed but broken" (e.g. a pkg_resources
    # ImportError) instead of always reporting "missing".
    _WEBRTCVAD_IMPORT_ERROR = _webrtcvad_import_err

from .voice_utils import mp3_to_pcm, play_pcm_on_voice_client


def dependencies_available():
    return _HAS_PYCORD and _HAS_WEBRTCVAD


def missing_dependencies():
    missing = []
    if not _HAS_PYCORD:
        missing.append("py-cord[voice]")
    if not _HAS_WEBRTCVAD:
        if _WEBRTCVAD_IMPORT_ERROR is not None:
            missing.append(
                f"webrtcvad (import failed: "
                f"{type(_WEBRTCVAD_IMPORT_ERROR).__name__}: "
                f"{_WEBRTCVAD_IMPORT_ERROR})"
            )
        else:
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

# webrtcvad aggressiveness: 0 (least aggressive about silence) to 3 (most).
# 3 was chosen after logs showed level 2 classifying breath/mouth noise as
# speech (an ~800ms noise fragment once fired a barge-in that killed an
# in-flight response). Trade-off: very soft speech onsets may be missed —
# if quiet utterances start getting clipped, drop back to 2.
_VAD_AGGRESSIVENESS = 3

# Barge-in: how much sustained speech during bot inference or playback
# before we interrupt the bot. There is no separate constant — the threshold
# is DERIVED from min_utterance_ms (see LiveVoiceSession.__init__) so the
# interrupt bar can never sit below the system's own definition of a valid
# utterance. A previous fixed 500ms here vs. the 1000ms minimum utterance
# left a "kill zone": 500–1000ms fragments cancelled inference and were then
# dropped as TOO SHORT, killing a response with nothing to replace it.

# Hard cap on a single utterance's buffered audio. VAD-classified speech with
# pauses shorter than silence_ms never finalizes on its own; without a cap,
# a noisy room / music / chatty speaker grows buffer_48k at ~192 KB/s until
# RAM is exhausted. At the cap we force-finalize whatever is buffered (same
# path as normal silence completion). 300s ≈ 55 MB of 48kHz stereo s16le PCM.
_MAX_UTTERANCE_SECONDS = 300
_MAX_UTTERANCE_BYTES = (
    int(_MAX_UTTERANCE_SECONDS * 1000 // _FRAME_MS)
    * (_INPUT_RATE * _INPUT_CHANNELS * _INPUT_SAMPLE_BYTES)
)


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
                 "speech_frame_count", "last_speech_ts", "last_member")

    def __init__(self):
        self.buffer_48k = bytearray()
        self.speech_started = False
        self.silent_frame_count = 0
        self.speech_frame_count = 0
        self.last_speech_ts = 0.0
        self.last_member = None


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
                 min_utterance_ms, on_utterance):
        self.silence_ms = int(silence_ms)
        self.idle_timeout_min = int(idle_timeout_min)
        self.single_user = bool(single_user)
        self.min_utterance_ms = int(min_utterance_ms)
        self.on_utterance = on_utterance

        # How many trailing silent frames before we finalize an utterance.
        self._silence_frames_threshold = max(1, self.silence_ms // _FRAME_MS)
        # Same metric for the minimum-utterance filter.
        self._min_speech_frames = max(1, self.min_utterance_ms // _FRAME_MS)
        # Barge-in: sustained-speech frame threshold during bot inference/playback.
        # Derived from min_utterance_ms so the interrupt bar can never sit
        # below the valid-utterance bar (see the note near _VAD_AGGRESSIVENESS).
        self._barge_in_frame_threshold = max(1, self.min_utterance_ms // _FRAME_MS)

        self.voice_client = None  # pycord VoiceClient once connected
        self.text_channel = None  # discord.TextChannel for status messages
        self.target_user_id = None  # only used when single_user is True
        self.target_member = None
        self._loop = None
        self._sink = None
        self._idle_check_task = None
        self._silence_watchdog = None
        self._last_interaction_ts = 0.0
        self._stopped = False
        # Guards against duplicate dead-connection teardown when both
        # watchdogs notice the lost connection around the same time.
        self._disconnect_handled = False
        # Serializes utterance processing: overlapping dispatches (two
        # speakers, or VAD splitting one speaker) previously ran concurrently,
        # clobbering _inference_task (breaking barge-in), interleaving TTS
        # playback, and racing history writes.
        self.utterance_lock = asyncio.Lock()
        # Barge-in state — read by the sink from the audio thread, set/cleared
        # from the event loop. Reading an object reference is atomic; setting
        # to None on a completed task before cancel() is a no-op. Safe.
        self._inference_task = None     # asyncio.Task during get_ai_response
        self._was_interrupted = False  # set by sink during playback barge-in

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
        self._last_interaction_ts = time.time()
        self._idle_check_task = asyncio.create_task(self._idle_watchdog())
        self._silence_watchdog = asyncio.create_task(self._silence_timeout_watchdog())

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
            if self._silence_watchdog is not None:
                self._silence_watchdog.cancel()
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

    async def _handle_unexpected_disconnect(self):
        # The watchdog loops exit normally whenever is_active() goes False.
        # If the session wasn't intentionally stopped, the voice connection
        # died (network blip, gateway kick, DAVE resume failure) — previously
        # this left the session marked active forever: visibly "connected",
        # permanently deaf, with no log, notification, or cleanup.
        if self._stopped or self._disconnect_handled:
            return
        self._disconnect_handled = True
        print("⚠️ [livevoice] voice connection lost unexpectedly — cleaning up session")
        try:
            from . import state
            if state.LIVE_VOICE_SESSION is self:
                state.LIVE_VOICE_SESSION = None
        except Exception:
            pass
        if self.text_channel is not None:
            try:
                await self.text_channel.send(
                    "*🎙️ Live voice connection was lost — disconnected. "
                    "Use `!joinLive` to reconnect.*"
                )
            except Exception:
                pass
        await self.stop()

    async def _idle_watchdog(self):
        # Auto-disconnect after IDLE_TIMEOUT_MIN of no recognized speech.
        try:
            while self.is_active():
                await asyncio.sleep(15)
                idle_sec = time.time() - self._last_interaction_ts
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
        await self._handle_unexpected_disconnect()

    async def _silence_timeout_watchdog(self):
        # Fallback end-of-utterance detector. Discord's client-side voice
        # activity gate stops transmitting packets shortly after the user
        # stops speaking, so the silent-frame counter in _process_frame can
        # never reach its threshold — the sink simply stops getting called.
        # This watchdog polls per-user state every 150ms and finalizes any
        # utterance whose last voiced frame is older than silence_ms.
        try:
            while self.is_active():
                await asyncio.sleep(0.15)
                if self._sink is None:
                    continue
                now = time.time()
                with self._sink._lock:
                    for user_id, state in list(self._sink._states.items()):
                        if not state.speech_started:
                            continue
                        gap_ms = (now - state.last_speech_ts) * 1000
                        if gap_ms < self.silence_ms:
                            continue
                        # Re-check under lock: silent-frame path may have
                        # already reset this state between iterations.
                        if not state.speech_started:
                            continue
                        speech_frames = state.speech_frame_count
                        dur_ms = speech_frames * _FRAME_MS
                        member = state.last_member
                        buffer_copy = bytes(state.buffer_48k)
                        self._sink._states[user_id] = _UtteranceState()
                        if speech_frames >= self._min_speech_frames:
                            print(f"[livevoice] utterance TIMEOUT user={user_id} "
                                  f"dur_ms={dur_ms} gap_ms={gap_ms:.0f}")
                            self._on_utterance_ready_threadsafe(buffer_copy, member)
                        else:
                            print(f"[livevoice] utterance TOO SHORT dropped "
                                  f"user={user_id} frames={speech_frames}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"⚠️ [livevoice] silence watchdog error: {e}")
        await self._handle_unexpected_disconnect()

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
        self._last_interaction_ts = time.time()
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
        # The lock serializes processing: a second utterance completing while
        # one is still being answered queues here instead of running
        # concurrently (which previously clobbered _inference_task so
        # barge-in cancelled the wrong task, and interleaved TTS/DB writes).
        try:
            async with self.utterance_lock:
                await self.on_utterance(wav_bytes, member)
        except asyncio.CancelledError:
            raise  # barge-in cancellation must propagate untouched
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
            self._lock = threading.Lock()
            self._vad = webrtcvad.Vad(_VAD_AGGRESSIVENESS) if _HAS_WEBRTCVAD else None
            # --- heartbeat diagnostics (removable once reception is confirmed) ---
            self._frame_count = 0
            self._empty_count = 0
            self._filter_drop_count = 0
            self._heartbeat_ts = time.time()
            self._probed = False
            # --- barge-in detection state ---
            self._barge_in_frames = 0
            self._barge_in_fired = False

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
            # --- heartbeat: confirms write() is being called and what pcm looks like ---
            if not self._probed:
                self._probed = True
                has_pcm = hasattr(data, "pcm")
                print(f"[livevoice] write FIRST CALL type={type(data).__name__} "
                      f"has_pcm={has_pcm} user_type={type(user).__name__}")
            self._frame_count += 1
            now = time.time()
            if now - self._heartbeat_ts >= 5.0:
                print(f"[livevoice] recv {self._frame_count} frames/5s "
                      f"(pcm_empty={self._empty_count}, "
                      f"filter_drop={self._filter_drop_count})")
                self._frame_count = 0
                self._empty_count = 0
                self._filter_drop_count = 0
                self._heartbeat_ts = now
            pcm = getattr(data, "pcm", None)
            if not pcm:
                self._empty_count += 1
                return
            # Single-user filter.
            if self.session.single_user:
                if self.session.target_user_id is None:
                    self._filter_drop_count += 1
                    return
                user_id = getattr(user, "id", None)
                if user_id is None or user_id != self.session.target_user_id:
                    self._filter_drop_count += 1
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

            # --- Barge-in: interrupt bot during inference or playback ---
            # Runs alongside the normal VAD/utterance path. Requires sustained
            # speech (min_utterance_ms-derived threshold) to fire, filtering
            # coughs and brief noises.
            bot_playing = (self.session.voice_client is not None
                           and self.session.voice_client.is_playing())
            inference_running = self.session._inference_task is not None
            barge_window = bot_playing or inference_running
            if barge_window and is_speech:
                self._barge_in_frames += 1
                if (self._barge_in_frames >= self.session._barge_in_frame_threshold
                        and not self._barge_in_fired):
                    self._barge_in_fired = True
                    if inference_running:
                        task = self.session._inference_task
                        if task is not None and self.session._loop is not None:
                            self.session._loop.call_soon_threadsafe(task.cancel)
                        print(f"[livevoice] BARGE-IN: cancelling inference "
                              f"(user={user_id})")
                    elif bot_playing:
                        self.session._was_interrupted = True
                        try:
                            # Stop ONLY the AudioPlayer, not the listener.
                            # pycord's VoiceClient.stop() also tears down
                            # _reader (the audio receiver), which would
                            # permanently deafen the bot. _player.stop()
                            # signals the player thread to exit, fires the
                            # after-callback (unblocking play_pcm_on_voice_client),
                            # and leaves the listener intact.
                            player = self.session.voice_client._player
                            if player is not None:
                                player.stop()
                        except Exception:
                            pass
                        print(f"[livevoice] BARGE-IN: stopping playback "
                              f"(user={user_id})")
            elif not barge_window:
                self._barge_in_frames = 0
                self._barge_in_fired = False
            elif not is_speech:
                # Reset accumulator on silent frame during barge window so
                # only *sustained* speech triggers the interrupt. Also reset
                # the latch so barge-in can fire again if the user pauses
                # briefly then resumes speaking — the min-utterance-derived
                # threshold still filters coughs and brief noises on each
                # re-trigger.
                self._barge_in_frames = 0
                self._barge_in_fired = False

            with self._lock:
                state = self._states.get(user_id)
                if state is None:
                    state = _UtteranceState()
                    self._states[user_id] = state

                if is_speech:
                    was_started = state.speech_started
                    state.buffer_48k.extend(pcm_48k_stereo)
                    state.speech_frame_count += 1
                    state.silent_frame_count = 0
                    state.speech_started = True
                    state.last_speech_ts = time.time()
                    if not was_started:
                        state.last_member = user
                        print(f"[livevoice] speech START user={user_id}")
                    # Cap check: unbounded growth protection. Force-finalize
                    # mid-speech rather than buffering without limit.
                    if len(state.buffer_48k) >= _MAX_UTTERANCE_BYTES:
                        print(f"[livevoice] utterance CAP HIT user={user_id} "
                              f"({len(state.buffer_48k)} bytes) — force-finalizing")
                        self._finalize_utterance_locked(user_id, state, reason="CAPPED")
                        return
                else:
                    if state.speech_started:
                        # Keep a little trailing silence for natural-sounding STT
                        # input; webrtcvad is choppy and STT engines are fine
                        # with short silences embedded in the audio.
                        state.buffer_48k.extend(pcm_48k_stereo)
                        state.silent_frame_count += 1
                        # Cap check covers the trailing-silence path too (a
                        # long utterance can exceed the cap during its pause).
                        if len(state.buffer_48k) >= _MAX_UTTERANCE_BYTES:
                            print(f"[livevoice] utterance CAP HIT user={user_id} "
                                  f"({len(state.buffer_48k)} bytes) — force-finalizing")
                            self._finalize_utterance_locked(user_id, state, reason="CAPPED")
                            return
                        if state.silent_frame_count >= self.session._silence_frames_threshold:
                            # Utterance complete.
                            self._finalize_utterance_locked(user_id, state)

        def _finalize_utterance_locked(self, user_id, state, reason="DONE"):
            # Shared finalize path for normal silence-completion and the
            # hard-cap force-finalize. MUST be called with self._lock held.
            dur_ms = state.speech_frame_count * _FRAME_MS
            if state.speech_frame_count >= self.session._min_speech_frames:
                print(f"[livevoice] utterance {reason} user={user_id} dur_ms={dur_ms}")
                self.session._on_utterance_ready_threadsafe(
                    bytes(state.buffer_48k), state.last_member,
                )
            else:
                print(f"[livevoice] utterance TOO SHORT dropped user={user_id} frames={state.speech_frame_count}")
            # Reset whether or not we forwarded — short blips get dropped silently.
            self._states[user_id] = _UtteranceState()

        def cleanup(self):
            # Called by pycord on disconnect. Flush any in-flight
            # utterance so we don't lose the last thing the user said.
            with self._lock:
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
