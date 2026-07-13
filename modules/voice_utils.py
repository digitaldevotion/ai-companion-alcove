# ============================================
# Alcove — voice_utils.py
# Shared audio conversion and playback utilities
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import os
import subprocess
import tempfile

import discord


def mp3_to_pcm(mp3_bytes):
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


async def play_pcm_on_voice_client(voice_client, wav_bytes, log_prefix=""):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name

    try:
        waited = 0
        while voice_client.is_playing():
            await asyncio.sleep(0.1)
            waited += 0.1
            if waited > 300:
                print(f"⚠️ {log_prefix}play_audio: timed out waiting for prior playback to finish")
                return False

        audio_source = discord.FFmpegPCMAudio(tmp_path)
        done_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after_playing(error):
            if error:
                print(f"⚠️ {log_prefix}playback error: {error}")
            try:
                loop.call_soon_threadsafe(done_event.set)
            except RuntimeError:
                pass

        voice_client.play(audio_source, after=_after_playing)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            print(f"⚠️ {log_prefix}play_audio: timed out waiting for playback to complete (after callback never fired)")
            return False
    finally:
        await asyncio.sleep(0.2)
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass

    return True
