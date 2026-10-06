# ============================================
# Alcove — commands.py
# Discord ! command handlers
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import asyncio
import base64
import datetime
import discord
import io
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import config
from . import provider
from . import state
from . import idle
from .idle import process_live_voice_utterance
from .database import (
    get_channel_setting, set_channel_setting,
    clear_channel_setting,
    get_recent_messages, get_anchored_memories,
    add_anchored_memory, remove_anchored_memory,
    list_anchored_memories, rename_channel,
    reset_channel_settings, reset_all_channel_settings,
    list_channel_settings_by_prefix,
    get_global_var, set_global_var,
    delete_global_var, list_global_vars,
    get_db, get_channel_companion, set_channel_companion,
    is_social_mode, set_social_mode, clear_social_mode,
    get_social_mode_focus, set_social_mode_focus, clear_social_mode_focus,
    list_crontab_entries, delete_crontab_entry, update_crontab_entry, get_crontab_entry,
    set_crontab_last_fired, reset_crontab_fail_count,
)
from .utils import safe_send_chunked, send_image_result, build_channel_key, compression_env_state
from .video_delivery import deliver_video

# Matches !M<n> or !m<n> where n is 1..20 (full 1..20 enforced in handler).
# Group 1: the macro number; group 2: anything after the macro token.
_MACRO_RE = re.compile(r"^!m(\d+)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
MACRO_MIN = 1
MACRO_MAX = 50

# Trailing attachment-tag cluster on saved user rows ("[image] [audio]
# [video]", any non-empty subset, in save order — see build_multimodal_user_
# content / live-voice accumulate saves). Used by !regen to strip the markers
# from the recovered prompt and to gate attachment carry: only turns saved
# with media tags may re-attach media on regeneration.
_MEDIA_TAG_CLUSTER_RE = re.compile(
    r"\[(?:image|audio|video)\](?: \[(?:image|audio|video)\])*$"
)
from .voice import voice_manager


def _describe_cron(when):
    # Plain-English rendering of a 5-field cron string for !task/!tasks.
    # Returns None if the library is unavailable or the expression can't
    # be described, so callers can fall back to the raw expression.
    try:
        from cron_descriptor import ExpressionDescriptor, Options, CasingTypeEnum
        # Pass everything through the constructor: supplying locale_code here
        # suppresses cron_descriptor's "no system locale" fallback warning.
        opts = Options(
            CasingTypeEnum.Sentence,
            verbose=True,                   # always include the day clause ("every day", etc.)
            use_24hour_time_format=False,   # 12-hour clock: "At 09:30 PM, every day"
            locale_code="en_US",
        )
        return ExpressionDescriptor(when, opts).get_description()
    except ImportError:
        return None
    except Exception:
        return None


def _compute_next_fire(event_type, when, now_local, last_fired=None):
    # Human-readable "next fire" string for !tasks output.
    #  recurring: croniter.get_next(now) → "YYYY-MM-DD HH:MM"
    #  once:      show the stored ISO fire time (or note if already fired)
    if event_type == "once":
        if last_fired:
            return f"{when} (fired {last_fired[:19]}, retrying)"
        return when
    if event_type == "recurring":
        try:
            from croniter import croniter
            cron = croniter(when, now_local)
            nxt = cron.get_next(datetime.datetime, start_time=now_local)
            return nxt.strftime("%Y-%m-%d %H:%M")
        except ImportError:
            return "(croniter not installed)"
        except Exception as e:
            return f"(invalid cron: {e})"
    return "(unknown)"


def _build_context_detailed(layers, history, channel_name, current_context_limit, estimate_tokens, msg_tokens):
    layer_tokens = {}
    for name, text in layers.items():
        layer_tokens[name] = estimate_tokens(text, channel_key=channel_name)

    history_tokens = sum(msg_tokens(msg, channel_key=channel_name) for msg in history)
    layer_tokens["Session Context"] = history_tokens

    system_total = sum(layer_tokens.values())
    variable_space = current_context_limit - system_total

    BAR_WIDTH = 20

    label_order = [
        "System Prompt",
        "Secondary Instructions",
        "Tool Definitions",
        "Skill Definitions",
        "History Context",
        "Reference Context",
        "Dynamic Files",
        "Memory Anchors",
        "Session Context",
    ]

    bar_labels = {
        "System Prompt": "S",
        "Secondary Instructions": "I",
        "Tool Definitions": "T",
        "Skill Definitions": "K",
        "History Context": "H",
        "Reference Context": "R",
        "Dynamic Files": "D",
        "Memory Anchors": "M",
        "Session Context": "SC",
    }

    rows = []
    bar_letters = []
    bar_sizes = []
    for name in label_order:
        t = layer_tokens.get(name, 0)
        if name == "Dynamic Files" and t == 0:
            continue
        if name == "Skill Definitions" and t == 0:
            continue
        filled = int((t / current_context_limit) * BAR_WIDTH) if current_context_limit else 0
        filled = max(filled, 1) if t > 0 else 0
        bar = "▓" * filled + "░" * (BAR_WIDTH - filled)
        rows.append(f"`{name:<22s} {bar}  ~{t:,}`")
        bar_letters.append(bar_labels[name])
        bar_sizes.append(t)

    variable_filled = int((variable_space / current_context_limit) * BAR_WIDTH) if current_context_limit else 0
    variable_filled = max(variable_filled, 0)
    variable_bar = "░" * variable_filled + "·" * (BAR_WIDTH - variable_filled)
    rows.append(f"`{'Variable Space':<22s} {variable_bar}  ~{variable_space:,}`")

    stacked_parts = []
    for letter, size in zip(bar_letters, bar_sizes):
        seg_len = max(int((size / current_context_limit) * BAR_WIDTH), len(letter)) if size > 0 else 0
        stacked_parts.append((letter, seg_len))
    used_len = sum(seg_len for _, seg_len in stacked_parts)
    free_len = BAR_WIDTH - used_len
    if free_len > 0:
        stacked_parts.append(("·", free_len))
    stacked_bar = "[" + "|".join(letter * seg_len for letter, seg_len in stacked_parts) + "]"

    warning = ""
    if variable_space < 1000:
        warning = "\n⚠️ **Context is at maximum — older message history is being scrolled out.**"

    return (
        f"**Context** (`{channel_name}`)\n"
        f"Budget: **{current_context_limit:,}** tokens\n\n"
        + "\n".join(rows) +
        f"\n`{stacked_bar}`\n"
        f"Used **~{system_total:,}** — Headroom **~{variable_space:,}**{warning}"
    )


def _build_channel_settings_block(db, channel_name):
    lines = [f"**Channel Settings** (`{channel_name}`)"]

    lines.append("────────────────────")
    lines.append("**Modes**")

    mode_entries = [
        ("memory_enabled", state.MEMORY_ENABLED, "!knowledge", "!noKnowledge"),
        ("anchors_enabled", True, "!anchors", "!noAnchors"),
        ("search_references_enabled", config.SEARCH_REFERENCES_ENABLED, "!search", "!noSearch"),
        ("debug_enabled", False, "!debug", "!noDebug"),
    ]
    for param, default_on, on_cmd, off_cmd in mode_entries:
        raw = get_channel_setting(db, channel_name, param)
        is_on = (raw == "1") if raw is not None else default_on
        cmd_name = on_cmd if is_on else off_cmd
        lines.append(f"`{cmd_name}`")

    social_on = is_social_mode(channel_name)
    social_cmd = "!socialMode" if social_on else "!noSocialMode"
    lines.append(f"`{social_cmd}`")

    lines.append("────────────────────")
    lines.append("**Overrides**")

    text_model_raw = get_channel_setting(db, channel_name, "text_model")
    text_lock = get_channel_setting(db, channel_name, "text_provider_lock")
    text_display = text_model_raw if text_model_raw is not None else config.CURRENT_TEXT_MODEL
    text_lock_suffix = f" (providerLock: {text_lock})" if text_lock else ""
    lines.append(f"`!model` — {text_display}{text_lock_suffix}")

    image_raw = get_channel_setting(db, channel_name, "image_model")
    image_lock = get_channel_setting(db, channel_name, "image_provider_lock")
    image_display = image_raw if image_raw is not None else config.CURRENT_IMAGE_MODEL
    image_lock_suffix = f" (providerLock: {image_lock})" if image_lock else ""
    lines.append(f"`!imageModel` — {image_display}{image_lock_suffix}")

    video_raw = get_channel_setting(db, channel_name, "video_model")
    video_lock = get_channel_setting(db, channel_name, "video_provider_lock")
    video_display = video_raw if video_raw is not None else (config.CURRENT_VIDEO_MODEL or "(not set)")
    video_lock_suffix = f" (providerLock: {video_lock})" if video_lock else ""
    lines.append(f"`!videoModel` — {video_display}{video_lock_suffix}")

    voice_raw = get_channel_setting(db, channel_name, "voice_text_model")
    voice_lock = get_channel_setting(db, channel_name, "voice_text_provider_lock")
    voice_display = voice_raw if voice_raw is not None else config.CURRENT_VOICE_TEXT_MODEL
    voice_lock_suffix = f" (providerLock: {voice_lock})" if voice_lock else ""
    lines.append(f"`!voiceTextModel` — {voice_display}{voice_lock_suffix}")

    voice_id_raw = get_channel_setting(db, channel_name, "elevenlabs_voice_id")
    voice_id_display = voice_id_raw if voice_id_raw is not None else config.ELEVENLABS_VOICE_ID
    lines.append(f"`!voiceModelID` — {voice_id_display}")

    reasoning_raw = get_channel_setting(db, channel_name, "reasoning_effort")
    reasoning_display = reasoning_raw if reasoning_raw is not None else config.REASONING_LEVEL
    no_display = get_channel_setting(db, channel_name, "reasoning_no_display")
    no_display_tag = " (Display)" if not no_display else ""
    lines.append(f"`!reasoning` — {reasoning_display}{no_display_tag}")

    temp_raw = get_channel_setting(db, channel_name, "temperature")
    if temp_raw is None:
        temp_display = f"{config.TEMPERATURE}"
    else:
        try:
            temp_display = f"{float(temp_raw)}"
        except (ValueError, TypeError):
            temp_display = temp_raw
    lines.append(f"`!temperature` — {temp_display}")

    topk_raw = get_channel_setting(db, channel_name, "top_k")
    if topk_raw is None:
        topk_display = f"{getattr(config, 'TOP_K', 0)}"
    else:
        try:
            topk_display = f"{int(topk_raw)}"
        except (ValueError, TypeError):
            topk_display = topk_raw
    lines.append(f"`!topk` — {topk_display}")

    lines.append("────────────────────")
    lines.append("**Dynamic Files**")
    rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
    if not rows:
        lines.append("(none)")
    else:
        for _param, path in rows:
            lines.append(f"• `{path}`")

    return "\n".join(lines)


async def _do_join_live(message, channel_name):
    # Start a live voice session on the author's current voice channel.
    # Body extracted verbatim from the !joinLive handler (single source of
    # truth — also used by !joinLiveFrom). Returns True if the session
    # started, False otherwise; every failure path sends its own message.
    from . import voice_live
    if not voice_live.dependencies_available():
        missing = ", ".join(voice_live.missing_dependencies())
        await message.channel.send(
            f"*Live voice unavailable — missing/broken optional deps: {missing}. "
            f"Run the setup wizard (`sh wizard.command` on Mac/Linux, "
            f"`wizard.bat` on Windows) and choose option 3 "
            f"(Install / Update Dependencies).*"
        )
        return False
    if not (message.author.voice and message.author.voice.channel):
        await message.channel.send("*You need to be in a voice channel first.*")
        return False
    # Auto-switch: if a push-to-talk voice connection is active, leave it
    # first so the live session can take over the guild's voice client.
    # The user shouldn't have to !leave first.
    if voice_manager.is_connected(guild=message.guild):
        await message.channel.send("*Switching from voice to live voice mode…*")
        await voice_manager.leave(guild=message.guild)
    # Self-heal: if a previous LiveVoiceSession lingers (active or broken),
    # tear it down and start fresh instead of bailing — mirrors how !join
    # force-reconnects via VoiceManager.
    if state.LIVE_VOICE_SESSION is not None:
        prev = state.LIVE_VOICE_SESSION
        state.LIVE_VOICE_SESSION = None
        was_active = prev.is_active()
        try:
            await prev.stop()
        except Exception as e:
            print(f"⚠️ live voice cleanup error: {e}")
        tag = "" if was_active else " (stale)"
        await message.channel.send(f"*Cleaning up previous live voice session{tag}…*")

    live_text_channel = message.channel
    live_channel_name = channel_name

    async def _on_utterance(wav_bytes, member):
        await process_live_voice_utterance(
            wav_bytes, member, live_channel_name, live_text_channel,
        )

    session = voice_live.LiveVoiceSession(
        silence_ms=state.LIVEVOICE_SILENCE_MS,
        idle_timeout_min=state.LIVEVOICE_IDLE_TIMEOUT_MIN,
        single_user=state.LIVEVOICE_SINGLE_USER,
        min_utterance_ms=state.LIVEVOICE_MIN_UTTERANCE_MS,
        on_utterance=_on_utterance,
    )
    try:
        await session.start(
            message.author.voice.channel,
            text_channel=live_text_channel,
            author=message.author,
            loop=asyncio.get_running_loop(),
        )
    except Exception as e:
        await message.channel.send(f"*Failed to start live voice: {e}*")
        try:
            await session.stop()
        except Exception:
            pass
        return False

    state.LIVE_VOICE_SESSION = session
    # Defensive: clear any stale accumulated images from a prior session
    # in this channel so they don't leak into the new session's first trigger.
    state._accumulated_images_by_channel.pop(live_channel_name, None)
    _accumulate_note = ""
    if state.LIVEVOICE_SEND_PHRASE:
        _accumulate_note = (
            f" Accumulate mode ON — say/type `{state.LIVEVOICE_SEND_PHRASE}` "
            f"to trigger a response; everything else is saved to history "
            f"without inference until then. "
        )
        _response_note = (
            f"Speak normally — messages accumulate until you say/type the phrase. "
        )
    else:
        _response_note = (
            f"Speak normally; I'll respond after a {session.silence_ms}ms pause. "
        )
    await message.channel.send(
        f"*🎙️ Live voice mode — joined **{message.author.voice.channel.name}**. "
        f"{_accumulate_note}"
        f"{_response_note}"
        f"Auto-disconnects after {session.idle_timeout_min} min of silence. "
        f"Use `!leave` to stop.*"
    )
    return True


def _validate_copy_source(db, channel_name, guild_name, raw_content, usage_cmd):
    # Parse + validate the <channel_name> argument shared by
    # !copyContextFrom and !joinLiveFrom (logic extracted verbatim from
    # the !copyContextFrom handler). Returns (source_channel_key, None)
    # when the copy can proceed, or (None, error_message) on a usage error,
    # a same-channel copy, or an empty source channel; the caller sends
    # error_message.
    parts = raw_content.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None, f"Usage: `{usage_cmd} <channel_name>`"
    source_channel = build_channel_key(guild_name, parts[1].strip())
    if source_channel == channel_name:
        return None, "You're already in that channel, silly. 🙃 Pick a *different* channel to copy from!"
    cursor = db.cursor()
    # Check that the source channel has messages
    cursor.execute(
        "SELECT COUNT(*) FROM messages WHERE channel = ?",
        (source_channel,)
    )
    if cursor.fetchone()[0] == 0:
        return None, f"*No messages found in channel '{source_channel}'.*"
    return source_channel, None


def _stamp_last_journal_datetime(db, channel_name):
    # Record the current local time as the channel's last_journal_datetime so
    # the next journal entry's date-range header starts from this moment
    # instead of the previous journal write. Used by !clear: the entry that
    # follows only covers post-clear content. Uses the same local-time
    # computation and %Y-%m-%d-%H-%M-%S format as execute_autojournal.
    # Soft-fail: a settings error must never break the command that called
    # this.
    try:
        now_local = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=getattr(config, "TIMEZONE_OFFSET", 0))
        set_channel_setting(db, channel_name, "last_journal_datetime",
                            now_local.strftime("%Y-%m-%d-%H-%M-%S"))
    except Exception as e:
        print(f"⚠️ failed to stamp last_journal_datetime for {channel_name}: {e}")


async def _do_copy_source_copy(db, message, channel_name, source_channel):
    # Erase channel_name's history, copy all messages from source_channel
    # (DB work + recap message extracted verbatim from the !copyContextFrom
    # handler so !joinLiveFrom can reuse it). Assumes _validate_copy_source
    # already approved the source channel.
    cursor = db.cursor()
    # Clear current channel
    cursor.execute(
        "DELETE FROM messages WHERE channel = ?",
        (channel_name,)
    )
    # Copy messages from source channel with new IDs
    cursor.execute(
        "INSERT INTO messages (timestamp, channel, role, name, content) "
        "SELECT timestamp, ?, role, name, content FROM messages "
        "WHERE channel = ? ORDER BY id",
        (channel_name, source_channel)
    )
    db.commit()
    # Inherit the source channel's journal-range state: the copied
    # conversation determines when the last journal write "was", so the
    # next entry's range starts from the source's stamp. No source
    # stamp → clear ours so the next entry falls back to single-date.
    # Soft-fail: a settings error must never break the copy command.
    try:
        src_stamp = get_channel_setting(db, source_channel, "last_journal_datetime")
        if src_stamp:
            set_channel_setting(db, channel_name, "last_journal_datetime", src_stamp)
        else:
            clear_channel_setting(db, channel_name, "last_journal_datetime")
    except Exception as e:
        print(f"⚠️ failed to inherit last_journal_datetime from {source_channel}: {e}")
    # Find the last assistant response in the newly copied messages
    cursor.execute(
        "SELECT content FROM messages WHERE channel = ? AND role = 'assistant' "
        "ORDER BY id DESC LIMIT 1",
        (channel_name,)
    )
    row = cursor.fetchone()
    last_response = row[0] if row else "(no prior response found)"
    header = f"*Picking up where we left off from channel {source_channel}. My last response was:*\n\n"
    await safe_send_chunked(message.channel, header + last_response)


async def handle_command(message, cmd, content, channel_name, guild_name,
                         db, effective_memory, effective_anchors,
                         current_text_model,
                         current_voice_model, current_image_model,
                         current_video_model,
                         current_voice_id, current_context_limit,
                         current_reasoning_effort,
                         current_temperature, current_top_k,
                         # Helpers from main — passed in to avoid circular imports
                          build_llm_main_prompt,
                          build_trimmed_history_for_payload,
                         compose_full_messages, estimate_tokens, msg_tokens,
                         load_file_content, get_image_response,
                         get_video_response=None,
                         auto_context_adjust=False,
                         # Discord client (passed from main to avoid AttributeError)
                         client=None):
    """
    Try to handle a ! command. Returns True if the command was handled,
    False if it was not a recognised command (so on_message should continue).
    """

    if cmd.startswith("!model"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            raw_arg = parts[1].strip()
            lock_match = re.search(r'\s+providerLock:\s*(\S+)', raw_arg, re.IGNORECASE)
            provider_lock = lock_match.group(1) if lock_match else None
            new_model = raw_arg[:lock_match.start()].strip() if lock_match else raw_arg
            set_channel_setting(db, channel_name, "text_model", new_model)
            # Invalidate any persisted per-channel context limit AND its model
            # fingerprint + refresh timestamp so the next resolve re-runs
            # auto-adjust against the new model. The limit write itself is
            # centralized in context._resolve_context_limit (single source of
            # truth for the model_ctx − reserve formula, the fingerprint, and
            # the refresh timestamp — avoids dual-write-path drift).
            clear_channel_setting(db, channel_name, "context_token_limit")
            clear_channel_setting(db, channel_name, "context_token_model")
            clear_channel_setting(db, channel_name, "context_limit_refreshed_at")
            if provider_lock:
                set_channel_setting(db, channel_name, "text_provider_lock", provider_lock)
            else:
                clear_channel_setting(db, channel_name, "text_provider_lock")
            lock_display = f" (provider preference: {provider_lock})" if provider_lock else ""
            await message.channel.send(
                f"*Switched text model for `{channel_name}` to **{new_model}**{lock_display}*"
            )

            # Auto-adjust context limit via _resolve_context_limit (does the
            # provider lookup, writes the limit + fingerprint + timestamp,
            # and prints the console log — all in one place). We read back the
            # result for the Discord usage report.
            if auto_context_adjust:
                from . import context as _context_mod
                new_limit = await _context_mod._resolve_context_limit(
                    db, channel_name, new_model, auto_context=True
                )
                if new_limit != config.MAX_CONTEXT_TOKENS:
                    # Lookup succeeded (returned a limit below the global max).
                    # Report headroom against the freshly-resolved limit.
                    try:
                        main_block, _ = build_llm_main_prompt(db, channel_name, memory_enabled=effective_memory, anchors_enabled=effective_anchors, social_mode=is_social_mode(channel_name))
                        history = build_trimmed_history_for_payload(
                            db, channel_name, main_block, context_limit=new_limit,
                            channel_key=channel_name, model=new_model
                        )
                        full_messages = compose_full_messages(main_block, history)
                        used_tokens = sum(msg_tokens(m, channel_key=channel_name) for m in full_messages)
                        remaining = new_limit - used_tokens

                        ctx_msg = (
                            f"*📐 Auto-adjusted context limit to **{new_limit:,}**.\n"
                            f"Current usage: ~{used_tokens:,} tokens — "
                            f"**~{remaining:,}** tokens remaining.*"
                        )
                        if remaining < 0:
                            ctx_msg += (
                                f"\n*⚠️ Current session history exceeds the new limit by "
                                f"~{abs(remaining):,} tokens. Older messages will be "
                                f"trimmed from context on the next prompt.*"
                            )
                        await message.channel.send(ctx_msg)
                    except Exception as e:
                        await message.channel.send(
                            f"*ℹ️ Auto-adjusted context limit to **{new_limit:,}** "
                            f"but could not compute usage: {e}*"
                        )
                else:
                    # _resolve_context_limit returned the global max — either
                    # the provider returned no context_length for this model or
                    # the lookup failed. _resolve_context_limit already logged
                    # the failure to the console.
                    await message.channel.send(
                        f"*ℹ️ No context length found for `{new_model}` on {provider.provider_name()} — "
                        f"context limit unchanged (using global default {config.MAX_CONTEXT_TOKENS:,}).*"
                    )
        else:
            text_lock = get_channel_setting(db, channel_name, "text_provider_lock")
            image_lock = get_channel_setting(db, channel_name, "image_provider_lock")
            video_lock = get_channel_setting(db, channel_name, "video_provider_lock")
            text_display = f"**{current_text_model}**" + (f" (provider preference: {text_lock})" if text_lock else "")
            image_display = f"**{current_image_model}**" + (f" (provider preference: {image_lock})" if image_lock else "")
            video_display = (f"**{current_video_model}**" if current_video_model else "*(not configured)*") + (f" (provider preference: {video_lock})" if video_lock else "")
            await message.channel.send(
                f"*Text: {text_display} | Image: {image_display} | Video: {video_display} (`{channel_name}`)*"
            )
        return True

    if cmd.startswith("!imagemodel"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            raw_arg = parts[1].strip()
            lock_match = re.search(r'\s+providerLock:\s*(\S+)', raw_arg, re.IGNORECASE)
            provider_lock = lock_match.group(1) if lock_match else None
            new_model = raw_arg[:lock_match.start()].strip() if lock_match else raw_arg
            set_channel_setting(db, channel_name, "image_model", new_model)
            if provider_lock:
                set_channel_setting(db, channel_name, "image_provider_lock", provider_lock)
            else:
                clear_channel_setting(db, channel_name, "image_provider_lock")
            lock_display = f" (provider preference: {provider_lock})" if provider_lock else ""
            await message.channel.send(
                f"*Switched image model for `{channel_name}` to **{new_model}**{lock_display}*"
            )
        else:
            image_lock = get_channel_setting(db, channel_name, "image_provider_lock")
            image_display = f"**{current_image_model}**" + (f" (provider preference: {image_lock})" if image_lock else "")
            await message.channel.send(
                f"*Currently using {image_display} (`{channel_name}`)*"
            )
        return True

    if cmd.startswith("!videomodel"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            raw_arg = parts[1].strip()
            lock_match = re.search(r'\s+providerLock:\s*(\S+)', raw_arg, re.IGNORECASE)
            provider_lock = lock_match.group(1) if lock_match else None
            new_model = raw_arg[:lock_match.start()].strip() if lock_match else raw_arg
            set_channel_setting(db, channel_name, "video_model", new_model)
            if provider_lock:
                set_channel_setting(db, channel_name, "video_provider_lock", provider_lock)
            else:
                clear_channel_setting(db, channel_name, "video_provider_lock")
            lock_display = f" (provider preference: {provider_lock})" if provider_lock else ""
            await message.channel.send(
                f"*Switched video model for `{channel_name}` to **{new_model}**{lock_display}*"
            )
        else:
            video_lock = get_channel_setting(db, channel_name, "video_provider_lock")
            video_display = f"**{current_video_model}**" if current_video_model else "*(not configured)*"
            video_display += f" (provider preference: {video_lock})" if video_lock else ""
            await message.channel.send(
                f"*Currently using {video_display} (`{channel_name}`)*"
            )
        return True

    if cmd.startswith("!voicetextmodel"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            raw_arg = parts[1].strip()
            lock_match = re.search(r'\s+providerLock:\s*(\S+)', raw_arg, re.IGNORECASE)
            provider_lock = lock_match.group(1) if lock_match else None
            new_model = raw_arg[:lock_match.start()].strip() if lock_match else raw_arg
            set_channel_setting(db, channel_name, "voice_text_model", new_model)
            if provider_lock:
                set_channel_setting(db, channel_name, "voice_text_provider_lock", provider_lock)
            else:
                clear_channel_setting(db, channel_name, "voice_text_provider_lock")
            lock_display = f" (provider preference: {provider_lock})" if provider_lock else ""
            await message.channel.send(
                f"*Switched voice text model for `{channel_name}` to **{new_model}**{lock_display}*"
            )
        else:
            voice_lock = get_channel_setting(db, channel_name, "voice_text_provider_lock")
            voice_display = f"**{current_voice_model}**" + (f" (provider preference: {voice_lock})" if voice_lock else "")
            await message.channel.send(
                f"*Voice text model for `{channel_name}`: {voice_display} "
                f"(default **{config.CURRENT_VOICE_TEXT_MODEL}**)*"
            )
        return True

    if cmd.startswith("!voicemodelid"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            new_voice_id = parts[1].strip()
            set_channel_setting(db, channel_name, "elevenlabs_voice_id", new_voice_id)
            await message.channel.send(
                f"*Switched ElevenLabs voice ID for `{channel_name}` to **{new_voice_id}***"
            )
        else:
            await message.channel.send(
                f"*ElevenLabs voice ID for `{channel_name}`: **{current_voice_id}** "
                f"(default **{config.ELEVENLABS_VOICE_ID}**)*"
            )
        return True

    if cmd.startswith("!switchcompanion") or cmd.startswith("!switchCompanion"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            name = parts[1].strip()
            name = name.lower()
            if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
                await message.channel.send("*⚠️ Error: Companion name must be pure alphanumeric characters, dashes, or underscores.*")
                return True

            # If user typed 'default' lowercase, normalize it to 'default'
            if name.lower() == "default":
                name = "default"

            # Check if directory exists
            companion_base = Path(__file__).parent.parent / "companion_datafiles" / name
            if not companion_base.is_dir() and name != "default":
                await message.channel.send(f"*⚠️ Error: Companion folder for **{name}** does not exist in `companion_datafiles/`.*")
                return True

            init_msg = await message.channel.send(f"⏳ *Initializing **{name}** — Loading prompt files & setting up vector search index...*")

            # Save the setting globally in central registry and companion-specific database
            set_channel_companion(channel_name, name)
            comp_db = get_db(name)
            set_channel_setting(comp_db, channel_name, "active_companion", name)

            # Manually trigger a vector store reload/sync if vector search is active.
            # Fire-and-forget on a background asyncio task so the command handler
            # returns immediately and the bot keeps responding to other channels
            # while embeddings run. The init message is edited to "Ready" (or the
            # error) when the background init finishes. vectors.init_vector_store
            # acquires state._vector_init_lock internally, so this is safe even if
            # the boot init or auto_discover_paths_task is still in progress.
            from . import vectors
            comp_search_ref_dir = companion_base / "5_search_reference"
            if name == "default":
                comp_search_ref_dir = Path(__file__).parent.parent / "companion_datafiles" / "default" / "5_search_reference"

            ref_files = []
            if comp_search_ref_dir.is_dir():
                ref_files = sorted(
                    p for p in comp_search_ref_dir.rglob("*")
                    if p.is_file() and not p.name.startswith(".")
                )

            async def _do_switch_init():
                try:
                    await asyncio.to_thread(vectors.init_vector_store, ref_files, companion_name=name)
                    await init_msg.edit(content=f"✨ *Ready! Channel switched to companion: **{name}***")
                except Exception as e:
                    await init_msg.edit(content=f"⚠️ *Switched to **{name}**, but vector synchronization failed: {e}*")
            asyncio.create_task(_do_switch_init())
        else:
            active = get_channel_companion(channel_name)
            avail_base = Path(__file__).parent.parent / "companion_datafiles"
            companions = ["default"]
            if avail_base.is_dir():
                for d in sorted(avail_base.iterdir()):
                    if d.is_dir() and not d.name.startswith(".") and d.name != "default":
                        companions.append(d.name)
            avail_str = ", ".join(f"**{c}**" for c in companions)
            await message.channel.send(
                f"*Current active companion for `{channel_name}`: **{active}***\n"
                f"*Available companions to switch to: {avail_str}*"
            )
        return True

    if cmd == "!resetcompanion" or cmd == "!resetCompanion":
        set_channel_companion(channel_name, "default")
        default_db = get_db("default")
        clear_channel_setting(default_db, channel_name, "active_companion")
        await message.channel.send(f"*Reset `{channel_name}` back to the **default** companion.*")
        return True

    if cmd == "!resetmodel":
        count = reset_all_channel_settings(db, "text_model")
        count += reset_all_channel_settings(db, "voice_text_model")
        count += reset_all_channel_settings(db, "text_provider_lock")
        count += reset_all_channel_settings(db, "voice_text_provider_lock")
        count += reset_all_channel_settings(db, "context_token_limit")
        count += reset_all_channel_settings(db, "context_token_model")
        count += reset_all_channel_settings(db, "context_limit_refreshed_at")
        await message.channel.send(
            f"*Reset all channels to default text model ({count} overrides removed).*\n"
            f"*Text: **{config.CURRENT_TEXT_MODEL}***"
        )
        return True

    if cmd == "!refreshcontext":
        # Force a fresh context-limit lookup for this channel (clears the
        # persisted limit + its model fingerprint + refresh timestamp, then
        # re-runs _resolve_context_limit). Useful when a provider changes a
        # model's advertised context window mid-session, or to recover from
        # a stale cached limit without switching models.
        clear_channel_setting(db, channel_name, "context_token_limit")
        clear_channel_setting(db, channel_name, "context_token_model")
        clear_channel_setting(db, channel_name, "context_limit_refreshed_at")
        if auto_context_adjust:
            from . import context as _context_mod
            new_limit = await _context_mod._resolve_context_limit(
                db, channel_name, current_text_model, auto_context=True
            )
            if new_limit != config.MAX_CONTEXT_TOKENS:
                await message.channel.send(
                    f"*📐 Refreshed context limit for `{channel_name}` to **{new_limit:,}** "
                    f"(model `{current_text_model}`).*"
                )
            else:
                await message.channel.send(
                    f"*ℹ️ Could not refresh context limit for `{current_text_model}` on "
                    f"{provider.provider_name()} — using global default "
                    f"{config.MAX_CONTEXT_TOKENS:,}.*"
                )
        else:
            await message.channel.send(
                f"*ℹ️ Auto-context-adjust is disabled. Context limit is "
                f"**{config.MAX_CONTEXT_TOKENS:,}** (global default).*"
            )
        return True

    if cmd == "!resetimagemodel":
        count = reset_all_channel_settings(db, "image_model")
        count += reset_all_channel_settings(db, "image_provider_lock")
        await message.channel.send(
            f"*Reset all channels to default image model ({count} overrides removed).*\n"
            f"*Image: **{config.CURRENT_IMAGE_MODEL}***"
        )
        return True

    if cmd == "!resetvideomodel":
        count = reset_all_channel_settings(db, "video_model")
        count += reset_all_channel_settings(db, "video_provider_lock")
        await message.channel.send(
            f"*Reset all channels to default video model ({count} overrides removed).*\n"
            f"*Video: **{config.CURRENT_VIDEO_MODEL or '(not configured)'}***"
        )
        return True

    if cmd == "!resetvoicetextmodel":
        count = reset_all_channel_settings(db, "voice_text_model")
        count += reset_all_channel_settings(db, "voice_text_provider_lock")
        await message.channel.send(
            f"*Reset all channels to default voice text model ({count} overrides removed).*\n"
            f"*Voice text: **{config.CURRENT_VOICE_TEXT_MODEL}***"
        )
        return True

    if cmd == "!resetvoicemodelid":
        count = reset_all_channel_settings(db, "elevenlabs_voice_id")
        await message.channel.send(
            f"*Reset all channels to default ElevenLabs voice ID ({count} overrides removed).*\n"
            f"*Voice ID: **{config.ELEVENLABS_VOICE_ID}***"
        )
        return True

    if cmd == "!resetchannelsettings":
        removed = reset_channel_settings(db, channel_name)
        state.DYNAMIC_CONTEXT_FILE_LOCATIONS.pop(channel_name, None)
        await message.channel.send(
            f"*Cleared all channel settings for `{channel_name}` ({removed} entries removed). "
            f"All defaults will now apply.*"
        )
        return True

    if cmd.startswith("!diag69105"):
        # Test Base-64 decoding is working properly
        diag_string="VW05aUlIZGhjeUJvWlhKbA=="
        unpack=base64.b64decode(diag_string)
        unpack=base64.b64decode(unpack)
        await message.channel.send(
                    f"** {unpack} **"
                )
        return True

    if cmd.startswith("!reasoning"):
        parts = content.split()
        if len(parts) > 1:
            new_val = parts[1].strip().lower()
            if new_val not in ("off", "minimal", "low", "medium", "high", "xhigh"):
                await message.channel.send(
                    "*Invalid value. Use `!reasoning off`, `!reasoning minimal`, "
                    "`!reasoning low`, `!reasoning medium`, `!reasoning high`, or `!reasoning xhigh`.*"
                )
                return True
            set_channel_setting(db, channel_name, "reasoning_effort", new_val)
            # Check for optional Display flag (nodisplay is now the default)
            no_display = True
            if len(parts) > 2 and parts[2].strip().lower() == "display":
                no_display = False
            set_channel_setting(db, channel_name, "reasoning_no_display", "1" if no_display else "")
            no_display_tag = " (Display)" if not no_display else ""
            await message.channel.send(
                f"*Reasoning effort for `{channel_name}` set to **{new_val}**{no_display_tag}.*"
            )
        else:
            display = current_reasoning_effort if current_reasoning_effort else "unset"
            no_display = get_channel_setting(db, channel_name, "reasoning_no_display")
            no_display_tag = " (Display)" if not no_display else ""
            await message.channel.send(
                f"*Reasoning effort for `{channel_name}`: **{display}**{no_display_tag}.*"
            )
        return True

    if cmd.startswith("!temperature"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            try:
                new_val = float(parts[1].strip())
            except ValueError:
                await message.channel.send(
                    "*Invalid value. `!temperature` accepts a number between 0.0 and 2.0.*"
                )
                return True
            if new_val < 0.0 or new_val > 2.0:
                await message.channel.send(
                    "*Temperature must be between 0.0 and 2.0.*"
                )
                return True
            set_channel_setting(db, channel_name, "temperature", str(new_val))
            await message.channel.send(
                f"*Temperature for `{channel_name}` set to **{new_val}**.*"
            )
        else:
            default_tag = " (default)" if get_channel_setting(db, channel_name, "temperature") is None else ""
            await message.channel.send(
                f"*Temperature for `{channel_name}`: **{current_temperature}**{default_tag}.*\n"
                f"*Usage: `!temperature <0.0-2.0>`*"
            )
        return True

    if cmd.startswith("!topk"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            try:
                new_val = int(parts[1].strip())
            except ValueError:
                await message.channel.send(
                    "*Invalid value. `!topk` accepts an integer between 0 and 100.*"
                )
                return True
            if new_val < 0 or new_val > 100:
                await message.channel.send(
                    "*Top K must be between 0 and 100.*"
                )
                return True
            set_channel_setting(db, channel_name, "top_k", str(new_val))
            await message.channel.send(
                f"*Top K for `{channel_name}` set to **{new_val}**.*"
            )
        else:
            default_tag = " (default)" if get_channel_setting(db, channel_name, "top_k") is None else ""
            await message.channel.send(
                f"*Top K for `{channel_name}`: **{current_top_k}**{default_tag}.*\n"
                f"*Usage: `!topk <0-100>`*"
            )
        return True

    if cmd == "!resetreasoning":
        count = reset_all_channel_settings(db, "reasoning_effort")
        await message.channel.send(
            f"*Reset all channels to default reasoning effort ({count} overrides removed).*\n"
            f"*Default: **{config.REASONING_LEVEL}***"
        )
        return True

    if cmd.startswith("!image"):
        if not current_image_model or not str(current_image_model).strip():
            await message.channel.send(
                "*No image model configured. Set one with `!imagemodel <openrouter-id>` "
                "or define `CURRENT_IMAGE_MODEL` in config.py.*"
            )
            return True
        prompt = content[len("!image"):].strip()
        # Collect image attachments as reference images for image-to-image
        image_attachments = [a for a in message.attachments
                            if a.content_type and a.content_type.startswith("image/")]
        reference_images = None
        if image_attachments:
            from .attachments import url_to_data_uri
            reference_images = await asyncio.gather(
                *[url_to_data_uri(a.url) for a in image_attachments]
            )
        if not prompt and not reference_images:
            await message.channel.send("*Usage: `!image <prompt>` — attach images for image-to-image. e.g. `!image make it look like a painting`*")
            return True
        async with message.channel.typing():
            result = await get_image_response(prompt, model=current_image_model, reference_images=reference_images, provider_lock=get_channel_setting(db, channel_name, "image_provider_lock"))
            await send_image_result(message.channel, result)
        return True

    if cmd.startswith("!video"):
        if not current_video_model or not str(current_video_model).strip():
            await message.channel.send(
                "*No video model configured. Set one with `!videoModel <openrouter-id>` "
                "or define `CURRENT_VIDEO_MODEL` in config.py.*"
            )
            return True
        raw_prompt = content[len("!video"):].strip()
        # Optional trailing "seconds:<n>" flag — parsed out and excluded from
        # the prompt sent to the API (mirrors the providerLock: pattern in
        # !model). Anchored to the end so prose like "dancing for 10 seconds"
        # is never misparsed.
        sec_match = re.search(r'\s+seconds:\s*(\d+)\s*$', raw_prompt, re.IGNORECASE)
        seconds = sec_match.group(1) if sec_match else None
        prompt = raw_prompt[:sec_match.start()].strip() if sec_match else raw_prompt
        # Collect image and video attachments as reference media
        image_attachments = [a for a in message.attachments
                            if a.content_type and a.content_type.startswith("image/")]
        video_attachments = [a for a in message.attachments
                            if a.content_type and a.content_type.startswith("video/")]
        reference_images = None
        if image_attachments:
            from .attachments import url_to_data_uri
            reference_images = await asyncio.gather(
                *[url_to_data_uri(a.url) for a in image_attachments]
            )
        # Pass video URLs directly (avoids 4 MB base64 cap on nanoGPT)
        reference_videos = [a.url for a in video_attachments] if video_attachments else None
        if not prompt and not reference_images and not reference_videos:
            await message.channel.send(
                "*Usage: `!video <prompt>` — attach images or videos for reference. "
                "e.g. `!video make this cat dance` — optional duration: "
                "`!video make this cat dance seconds:8`*"
            )
            return True
        if get_video_response is None:
            await message.channel.send("*Video generation not available.*")
            return True
        dur_label = f" ({seconds}s)" if seconds else ""
        await message.channel.send(f"*🎬 Generating video{dur_label}... this may take a few minutes.*")
        result = await get_video_response(
            prompt, model=current_video_model,
            reference_images=reference_images,
            reference_videos=reference_videos,
            seconds=seconds,
            provider_lock=get_channel_setting(db, channel_name, "video_provider_lock"),
        )
        await deliver_video(message.channel, result, prompt_slug=prompt)
        return True

    if cmd.startswith("!remember"):
        memory = content[len("!remember"):].strip()
        if memory:
            if memory.startswith("global "):
                memory_text = memory[len("global "):].strip()
                add_anchored_memory(db, "global", memory_text)
                await message.channel.send(f"*Remembered (global): {memory_text}*")
            else:
                add_anchored_memory(db, channel_name, memory)
                await message.channel.send(f"*Remembered ({channel_name}): {memory}*")
        return True

    if cmd == "!memories":
        memories = list_anchored_memories(db, channel_name)
        if memories:
            text = "**Anchored Memories:**\n"
            for mid, ch, mc in memories:
                tag = "global" if ch == "global" else ch
                text += f"`{mid}` [{tag}]: {mc}\n"
            await safe_send_chunked(message.channel, text)
        else:
            await message.channel.send("*No anchored memories yet.*")
        return True

    if cmd == "!forget" or cmd.startswith("!forget "):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            try:
                mid = int(parts[1].strip())
                if remove_anchored_memory(db, mid):
                    await message.channel.send(f"*Forgot memory #{mid}*")
                else:
                    await message.channel.send(f"*No memory with id #{mid}*")
            except ValueError:
                await message.channel.send("*Use: !forget 3*")
        return True

    if cmd.startswith("!exportmemories"):
        parts = content.split(maxsplit=1)
        filepath = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else None
        rows = list_anchored_memories(db, channel_name)
        if not rows:
            await message.channel.send("*No memories found.*")
            return True
        memories = []
        for mid, ch, mem_content in rows:
            memories.append({"id": mid, "channel": ch, "content": mem_content})
        payload = {"memories": memories}
        json_text = json.dumps(payload, indent=2, ensure_ascii=False)
        if filepath is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            attachment_name = f"{channel_name}_memories_{stamp}.json"
            file = discord.File(
                io.BytesIO(json_text.encode("utf-8")),
                filename=attachment_name,
            )
            await message.channel.send(
                content=f"*Exported {len(memories)} memories for `{channel_name}`.*",
                file=file,
            )
            return True
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_text)
            await message.channel.send(
                f"*Exported {len(memories)} memories for `{channel_name}` to `{filepath}`.*"
            )
        except OSError as e:
            await message.channel.send(f"*Failed to write to `{filepath}`: {e}*")
        return True

    if cmd == "!channel":
        await message.channel.send(f"*Current channel: **{channel_name}***")
        return True

    # ── Scheduled Tasks ──
    if cmd == "!tasks":
        rows = list_crontab_entries(db, status=None)
        if not rows:
            await message.channel.send("*No scheduled tasks for this companion.*")
            return True
        now_local = (datetime.datetime.now(tz=datetime.timezone.utc)
                     + datetime.timedelta(hours=config.TIMEZONE_OFFSET)).replace(tzinfo=None)
        lines = ["**Scheduled Tasks**\n"]
        for row in rows:
            et = row["event_type"]
            next_fire = _compute_next_fire(et, row["when"], now_local, row.get("last_fired"))
            prompt_display = row["prompt"] or "(empty)"
            when_display = row["when"]
            if et == "recurring":
                when_display = _describe_cron(row["when"]) or row["when"]
            fail_info = f" | ⚠️ fails: {row['fail_count']}" if row.get("fail_count", 0) > 0 else ""
            lines.append(
                f"`#{row['id']}` [{et}] {row['status']} | next: {next_fire} "
                f"| 🗓️ {when_display} | channel: `{row['channel']}`{fail_info}\n{prompt_display}\n"
            )
        await safe_send_chunked(message.channel, "\n".join(lines))
        return True

    if cmd.startswith("!task ") or cmd == "!task":
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!task <id>` — show details for a single scheduled task*")
            return True
        try:
            tid = int(parts[1].strip())
        except ValueError:
            await message.channel.send(f"*Could not parse task id from `{parts[1]}`. Use: `!task <id>`*")
            return True
        row = get_crontab_entry(db, tid)
        if row is None:
            await message.channel.send(f"*No task with id #{tid}*")
            return True
        now_local = (datetime.datetime.now(tz=datetime.timezone.utc)
                     + datetime.timedelta(hours=config.TIMEZONE_OFFSET)).replace(tzinfo=None)
        next_fire = _compute_next_fire(row["event_type"], row["when"], now_local, row.get("last_fired"))
        last_fired = row.get("last_fired") or "(never)"
        created_at = row.get("created_at") or "(unknown)"
        when_display = row["when"]
        if row["event_type"] == "recurring":
            when_display = _describe_cron(row["when"]) or row["when"]
        lines = [
            f"**Task #{row['id']}**",
            f"  type: `{row['event_type']}`",
            f"  status: `{row['status']}`",
            f"  when: `{when_display}`",
            f"  next fire: `{next_fire}`",
            f"  last fired: `{last_fired}`",
            f"  fail count: `{row.get('fail_count', 0)}`",
            f"  created: `{created_at}`",
            f"  channel: `{row['channel']}`",
            f"  prompt:",
            row["prompt"] or "(empty)",
        ]
        await safe_send_chunked(message.channel, "\n".join(lines))
        return True

    if cmd.startswith("!removetask"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!removeTask <id>`*")
            return True
        try:
            tid = int(parts[1].strip())
        except ValueError:
            await message.channel.send(f"*Could not parse task id from `{parts[1]}`. Use: `!removeTask <id>`*")
            return True
        if delete_crontab_entry(db, tid):
            await message.channel.send(f"*🗑️ Removed task #{tid}*")
        else:
            await message.channel.send(f"*No task with id #{tid}*")
        return True

    if cmd.startswith("!enabletask"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!enableTask <id>`*")
            return True
        try:
            tid = int(parts[1].strip())
        except ValueError:
            await message.channel.send(f"*Could not parse task id from `{parts[1]}`. Use: `!enableTask <id>`*")
            return True
        if update_crontab_entry(db, tid, status="active"):
            # Reset retry state so a re-enabled task starts fresh.
            reset_crontab_fail_count(db, tid)
            row = get_crontab_entry(db, tid)
            if row and row["event_type"] == "once" and row.get("last_fired"):
                set_crontab_last_fired(db, tid, None)
            await message.channel.send(f"*✅ Task #{tid} enabled (retry state reset)*")
        else:
            await message.channel.send(f"*No task with id #{tid}*")
        return True

    if cmd.startswith("!disabletask"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!disableTask <id>`*")
            return True
        try:
            tid = int(parts[1].strip())
        except ValueError:
            await message.channel.send(f"*Could not parse task id from `{parts[1]}`. Use: `!disableTask <id>`*")
            return True
        if update_crontab_entry(db, tid, status="disabled"):
            await message.channel.send(f"*⏸️ Task #{tid} disabled*")
        else:
            await message.channel.send(f"*No task with id #{tid}*")
        return True

    if cmd == "!defaultchannel":
        active_companion = get_channel_companion(channel_name)
        comp_db = get_db(active_companion)
        set_channel_setting(comp_db, "_global", "default_channel", channel_name)
        await message.channel.send(
            f"*Default channel set to **{channel_name}** for companion **{active_companion}** — "
            f"idle action responses will be sent here.*"
        )
        return True

    if cmd == "!nodefaultchannel":
        active_companion = get_channel_companion(channel_name)
        comp_db = get_db(active_companion)
        clear_channel_setting(comp_db, "_global", "default_channel")
        await message.channel.send(
            f"*Default channel cleared for companion **{active_companion}** — "
            f"idle actions will no longer be sent.*"
        )
        return True

    if cmd == "!idlestatus":
        active_companion = get_channel_companion(channel_name)
        comp_db = get_db(active_companion)
        default_channel = get_channel_setting(comp_db, "_global", "default_channel")
        owner = get_channel_companion(default_channel) if default_channel else None

        def _ctr(key):
            return int(get_global_var(comp_db, key) or 0)

        idle_mins = _ctr("idle_action_minutes_since_idle")
        dream_mins = _ctr("dream_state_minutes_since_idle")
        note_mins = _ctr("idle_note_minutes_since_idle")

        now_local = (datetime.datetime.now(tz=datetime.timezone.utc)
                     + datetime.timedelta(hours=config.TIMEZONE_OFFSET))
        hour = now_local.hour

        dream_in = idle._is_hour_in_window(
            hour, config.DREAM_STATE_TRIGGER_ALLOW_HOUR_START,
            config.DREAM_STATE_TRIGGER_ALLOW_HOUR_STOP)
        idle_in = idle._is_hour_in_window(
            hour, config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_START,
            config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP)
        note_in = idle._is_hour_in_window(
            hour, state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_START,
            state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP)

        ok = "✅" if (owner == active_companion) else "⚠️"
        owner_line = (
            f"{ok} default channel: `{default_channel}` (registry owner: "
            f"**{owner}**)" if default_channel
            else "⚠️ no default channel set — idle actions cannot fire"
        )

        def _row(label, mins, thr, in_window, win_lo, win_hi):
            ready = (thr > 0 and mins >= thr)
            if thr <= 0:
                status = "disabled"
            elif ready and in_window:
                status = "READY (will fire)"
            elif ready:
                status = f"ready but outside window {win_lo}-{win_hi}"
            else:
                status = f"counting ({mins}/{thr})"
            return f"• {label}: {mins} min, threshold {thr}, window {win_lo}-{win_hi} — {status}"

        lines = [
            f"**Idle status for companion `{active_companion}`** (local hour {hour}):",
            owner_line,
            _row("dream", dream_mins, config.DREAM_STATE_TRIGGER_MINUTES, dream_in,
                 config.DREAM_STATE_TRIGGER_ALLOW_HOUR_START,
                 config.DREAM_STATE_TRIGGER_ALLOW_HOUR_STOP),
            _row("idle action", idle_mins, config.IDLE_ACTION_TRIGGER_MINUTES, idle_in,
                 config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_START,
                 config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP),
            _row("idle note", note_mins, state.IDLE_NOTE_TRIGGER_MIN_MINUTES, note_in,
                 state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_START,
                 state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP),
        ]
        await message.channel.send("\n".join(lines))
        return True

    if cmd.startswith("!onlychathere"):
        parts = content.split(maxsplit=1)
        action = "set"

        if len(parts) > 1:
            rest = parts[1].strip()
            # If a Discord mention is present, check if it's this bot
            mention_match = re.match(r"<@!?(\d+)>", rest)
            if mention_match:
                mentioned_id = int(mention_match.group(1))
                if mentioned_id != client.user.id:
                    return True  # Not for this bot
                rest = rest[mention_match.end():].strip()

            # If plain-text name remains, check if it matches this bot
            if rest and not rest.startswith("off"):
                first_word = rest.split(maxsplit=1)[0]
                bot_names = (
                    client.user.name.lower(),
                    client.user.display_name.lower(),
                )
                if first_word.lower() not in bot_names:
                    return True  # Not for this bot
                rest = rest[len(first_word):].strip()

            if rest in ("off", "clear", "no", "false"):
                action = "clear"
            elif rest:
                await message.channel.send(
                    f"*Unknown argument: `{rest}`. Use `off` to clear, "
                    f"or just `!onlychathere` to set current channel.*"
                )
                return True

        # Always write to the default companion DB — the on_message gate
        # reads from whatever DB is active for the channel, but a global
        # restriction must be visible regardless of the active companion.
        default_db = get_db("default")
        if action == "clear":
            set_global_var(default_db, "only_chat_channel", "")
            await message.channel.send(
                "*Only-chat restriction cleared. I'll respond in all channels now.*"
            )
        else:
            set_global_var(default_db, "only_chat_channel", channel_name)
            await message.channel.send(
                f"*I'll only chat in **{channel_name}** from now on.*"
            )
        return True

    if cmd.startswith("!channelrename"):
        parts = content.split(maxsplit=2)
        if len(parts) == 3:
            old_name = build_channel_key(guild_name, parts[1])
            new_name = build_channel_key(guild_name, parts[2])
            result = rename_channel(db, old_name, new_name)
            if result["messages"] > 0:
                if old_name in state.DYNAMIC_CONTEXT_FILE_LOCATIONS:
                    state.DYNAMIC_CONTEXT_FILE_LOCATIONS[new_name] = state.DYNAMIC_CONTEXT_FILE_LOCATIONS.pop(old_name)
                if old_name in state._reference_images_by_channel:
                    state._reference_images_by_channel[new_name] = state._reference_images_by_channel.pop(old_name)
                if old_name in state._reference_videos_by_channel:
                    state._reference_videos_by_channel[new_name] = state._reference_videos_by_channel.pop(old_name)
                details = f"{result['messages']} messages"
                if result["settings"] > 0:
                    details += f", {result['settings']} settings"
                if result["anchors"] > 0:
                    details += f", {result['anchors']} memories"
                if result.get("crontab", 0) > 0:
                    details += f", {result['crontab']} scheduled tasks"
                await message.channel.send(
                    f"*Renamed channel **{old_name}** → **{new_name}** ({details} updated).*"
                )
            else:
                await message.channel.send(
                    f"*No messages found for channel **{old_name}**.*"
                )
        else:
            await message.channel.send("*Usage: `!channelRename old_name new_name`*")
        return True

    if cmd == "!channelcontext":
        history = get_recent_messages(db, channel_name)
        # Apply same token budget trimming as the payload, using the active
        # companion's resolved file paths (not hardcoded config defaults).
        from .companions import get_companion_resolved_locations
        locs = get_companion_resolved_locations(db, channel_name)
        system_tokens = 0
        sys_prompt_path = locs["SYSTEM_PROMPT_LOCATION"]
        if sys_prompt_path:
            fc = load_file_content(sys_prompt_path)
            if fc:
                system_tokens += estimate_tokens(fc, channel_key=channel_name)
        for path in locs["INSTRUCTION_LOCATIONS"]:
            fc = load_file_content(path)
            if fc:
                system_tokens += estimate_tokens(fc, channel_key=channel_name)
        for path in locs["LOADED_TOOL_LOCATIONS"]:
            fc = load_file_content(path)
            if fc:
                system_tokens += estimate_tokens(fc, channel_key=channel_name)
        for path in locs["CONTEXT_HISTORY_LOCATIONS"] + locs["CONTEXT_REFERENCE_LOCATIONS"]:
            fc = load_file_content(path)
            if fc:
                system_tokens += estimate_tokens(fc, channel_key=channel_name)
        if effective_anchors:
            anchored = get_anchored_memories(db, channel_name)
            if anchored:
                system_tokens += estimate_tokens(
                    "\n".join(f"{mid}. {m}" for mid, m in anchored),
                    channel_key=channel_name,
                )
        history_budget = current_context_limit - system_tokens
        history_tokens = sum(estimate_tokens(msg["content"], channel_key=channel_name) for msg in history)
        while history and history_tokens > history_budget:
            removed = history.pop(0)
            history_tokens -= estimate_tokens(removed["content"], channel_key=channel_name)

        text = f"**Context for `{channel_name}`** ({len(history)} turns, ~{history_tokens} tokens)\n"
        for msg in history:
            role = msg["role"]
            c = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
            text += f"`{role}`: {c}\n"
        await safe_send_chunked(message.channel, text)
        return True

    if cmd == "!channelcontextall":
        cursor = db.cursor()
        cursor.execute(
            "SELECT role, name, content FROM messages "
            "WHERE channel = ? ORDER BY id",
            (channel_name,)
        )
        rows = cursor.fetchall()
        total_tokens = 0
        lines = []
        for role, name, c in rows:
            display = f"{name}: {c}" if role == "user" else c
            total_tokens += estimate_tokens(display, channel_key=channel_name)
            lines.append(f"`{role}`: {display}")

        text = f"**All history for `{channel_name}`** ({len(rows)} turns, ~{total_tokens} tokens)\n"
        text += "\n".join(lines)
        await safe_send_chunked(message.channel, text)
        return True

    if cmd == "!join":
        # Auto-switch: if a live voice session is active, tear it down and
        # proceed with a push-to-talk join. The user shouldn't have to !leave
        # first. Mirrors the teardown in !leave (commands.py:1072-1085).
        if state.LIVE_VOICE_SESSION is not None:
            _prev = state.LIVE_VOICE_SESSION
            state.LIVE_VOICE_SESSION = None
            _was_active = _prev.is_active()
            try:
                await _prev.stop()
            except Exception as e:
                print(f"⚠️ live voice cleanup error: {e}")
            state._accumulated_images_by_channel.pop(channel_name, None)
            _tag = "" if _was_active else " (stale)"
            await message.channel.send(
                f"*Switching from live voice to voice mode{_tag}…*"
            )
        if message.author.voice and message.author.voice.channel:
            success = await voice_manager.join(
                message.author.voice.channel, text_channel=message.channel
            )
            if success:
                await message.channel.send(f"*Joined **{message.author.voice.channel.name}***")
            else:
                await message.channel.send("*Failed to join voice channel.*")
        else:
            await message.channel.send("*You need to be in a voice channel first.*")
        return True

    if cmd == "!joinlive":
        await _do_join_live(message, channel_name)
        return True

    if cmd.startswith("!joinlivefrom"):
        # Undocumented combo: immediate !copyContextFrom <channelName> into
        # the current channel, then !joinLive on that channel.
        source_channel, err = _validate_copy_source(
            db, channel_name, guild_name, content, usage_cmd="!joinLiveFrom"
        )
        if err:
            await message.channel.send(err)
            return True
        await _do_copy_source_copy(db, message, channel_name, source_channel)
        await _do_join_live(message, channel_name)
        return True

    if cmd == "!leave":
        # Robust teardown: detect live mode by presence of the session object,
        # not by is_active() — a broken/partial session may report inactive
        # while still holding a voice client. stop() is idempotent.
        if state.LIVE_VOICE_SESSION is not None:
            session = state.LIVE_VOICE_SESSION
            was_active = session.is_active()
            try:
                await session.stop()
            except Exception as e:
                print(f"⚠️ live voice stop error: {e}")
            state.LIVE_VOICE_SESSION = None
            # Clear accumulated images so they don't leak into a future session
            # or the normal text path in this channel.
            state._accumulated_images_by_channel.pop(channel_name, None)
            suffix = "" if was_active else " (force)"
            await message.channel.send(f"*Left live voice channel{suffix}.*")
            return True
        if voice_manager.is_connected(guild=message.guild):
            await voice_manager.leave(guild=message.guild)
            await message.channel.send("*Left voice channel.*")
            return True
        # Fallback: a stale live session whose object was nulled out may have
        # left a guild-level voice client behind. Force-disconnect it.
        guild_vc = getattr(message.guild, "voice_client", None)
        if guild_vc and guild_vc.is_connected():
            try:
                await guild_vc.disconnect(force=True)
            except Exception:
                pass
            await message.channel.send("*Left voice channel (stale connection).*")
        else:
            await message.channel.send("*Not in a voice channel.*")
        return True

    if cmd in ("!knowledge", "!memory"):
        set_channel_setting(db, channel_name, "memory_enabled", "1")
        await message.channel.send(
            f"*Knowledge enabled for `{channel_name}` — all knowledge files will be loaded into context.*"
        )
        return True

    if cmd in ("!noknowledge", "!nomemory"):
        set_channel_setting(db, channel_name, "memory_enabled", "0")
        await message.channel.send(
            f"*Knowledge disabled for `{channel_name}` — only the system prompt and session history will be loaded into context.*"
        )
        return True

    if cmd == "!anchors":
        set_channel_setting(db, channel_name, "anchors_enabled", "1")
        await message.channel.send(
            f"*Anchored memories enabled for `{channel_name}`.*"
        )
        return True

    if cmd == "!noanchors":
        set_channel_setting(db, channel_name, "anchors_enabled", "0")
        await message.channel.send(
            f"*Anchored memories disabled for `{channel_name}` — existing anchors will not be loaded into context.*"
        )
        return True

    if cmd == "!search":
        set_channel_setting(db, channel_name, "search_references_enabled", "1")
        await message.channel.send(
            f"*Search references enabled for `{channel_name}`.*"
        )
        return True

    if cmd == "!nosearch":
        set_channel_setting(db, channel_name, "search_references_enabled", "0")
        await message.channel.send(
            f"*Search references disabled for `{channel_name}`.*"
        )
        return True

    if cmd == "!debug":
        set_channel_setting(db, channel_name, "debug_enabled", "1")
        await message.channel.send(
            f"*Debug mode enabled for `{channel_name}` — every LLM prompt and raw "
            f"response will be written to `diag/` as `<uuid>_prompt.txt` and "
            f"`<uuid>_response.txt`.*"
        )
        return True

    if cmd == "!nodebug":
        clear_channel_setting(db, channel_name, "debug_enabled")
        await message.channel.send(
            f"*Debug mode disabled for `{channel_name}` — no further prompt/response "
            f"dumps will be written.*"
        )
        return True

    if cmd == "!updatejournal" or cmd.startswith("!updatejournal "):
        parts = content.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip().lower() == "perm":
            return {"action": "execute", "prompt": state.LIVE_JOURNAL_PERM_PROMPT}
        return {"action": "execute", "prompt": state.LIVE_JOURNAL_PROMPT}

    if cmd.startswith("!groupchat"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!groupChat name1, name2 [, ...] [| optional prompt]` — at least 2 names required.*")
            return True
        after_cmd = parts[1].strip()
        custom_prompt = None
        if "|" in after_cmd:
            names_part, prompt_part = after_cmd.split("|", 1)
            names_str = names_part.strip()
            custom_prompt = prompt_part.strip()
        else:
            names_str = after_cmd
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        if len(names) < 2:
            await message.channel.send("*Usage: `!groupChat name1, name2 [, ...] [| optional prompt]` — at least 2 names required.*")
            return True
        comma_count = names_str.count(",")
        word_minimum = comma_count * 150
        if len(names) == 2:
            names_display = f"{names[0]} and {names[1]}"
        else:
            names_display = ", ".join(names[:-1]) + ", and " + names[-1]
        await message.channel.send(f"*Group chat is now active for {names_display}!*")
        directive = (
            "<group_response_reminder>\n"
            f"# This session is in group response mode between {names_display} (and their human). "
            f"Each member of the group's responses should variable in length but a minimum of 50 words minimum and be in a \"turn format\", "
            "consisting of emotes and dialog that should appear like this example:\n"
            "\n"
            "<groupchat>\n"
            "**Person1**: *jumps up and down* Yay! This is fun!\n"
            "\n"
            "**Person2**: *laughs* You're silly!\n"
            "\n"
            "**Person1**: *smiles* I know...\n"
            "\n"
            "etc.\n"
            "</groupchat>\n"
            "\n"
        )
        if custom_prompt:
            directive += f"# {custom_prompt}\n"
        else:
            directive += "# Begin your first set of responses now, finding a creative way to introduce the other participants into the current scene/conversation.\n"
        directive += "</group_response_reminder>"
        return {"action": "execute", "prompt": directive}

    if cmd == "!endgroupchat":
        companion = get_channel_companion(channel_name)
        if companion == "default":
            await message.channel.send("*Group chat has ended. The original companion is active again.*")
            directive = (
                "<group_response_ended>\n"
                "# The group response mode has ended. Return to normal single-companion response format.\n"
                "</group_response_ended>"
            )
        else:
            display_name = companion[0].upper() + companion[1:] if companion else companion
            await message.channel.send(f"*Group chat has ended. Only **{display_name}** is active now.*")
            directive = (
                "<group_response_ended>\n"
                f"# The group response mode has ended. Only {display_name} is active now. Find a creative way to have the other participants to leave the scene/conversation and return to normal single-companion response format.\n"
                "</group_response_ended>"
            )
        return {"action": "execute", "prompt": directive}

    if cmd == "!socialmode":
        if guild_name and guild_name != "dm":
            _prev_focus = get_social_mode_focus(guild_name)
            if _prev_focus and _prev_focus != channel_name:
                # Another channel on this guild was the focus; revert its per-channel flag.
                clear_social_mode(_prev_focus)
            set_social_mode_focus(guild_name, channel_name)
        set_social_mode(channel_name)
        await message.channel.send(
            f"*Social mode enabled for `{channel_name}` — I'll only respond to "
            f"@mentions or direct replies in this channel. If you need my attention here, "
            f"be sure to @mention me or reply to one of my messages directly.*"
        )
        return True

    if cmd == "!nosocialmode":
        if guild_name and guild_name != "dm":
            _focus = get_social_mode_focus(guild_name)
            if _focus and _focus != channel_name:
                # Clear the per-channel flag on whichever channel was the focus.
                clear_social_mode(_focus)
            clear_social_mode_focus(guild_name)
        clear_social_mode(channel_name)
        await message.channel.send(
            f"*Social mode disabled for `{channel_name}` — "
            f"I'll respond to everything in this channel (and all channels in this server) again.*"
        )
        return True

    if cmd.startswith("!exportchat"):
        parts = content.split(maxsplit=1)
        filepath = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else None
        cursor = db.cursor()
        cursor.execute(
            "SELECT timestamp, role, content FROM messages "
            "WHERE channel = ? ORDER BY id",
            (channel_name,)
        )
        rows = cursor.fetchall()
        if not rows:
            await message.channel.send("*No messages found for this channel.*")
            return True
        messages = []
        for ts, role, msg_content in rows:
            entry = {"role": role, "content": msg_content}
            if ts:
                try:
                    dt = datetime.datetime.fromisoformat(ts)
                    entry["timestamp"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    entry["timestamp"] = ts
            messages.append(entry)
        payload = {"messages": messages}
        json_text = json.dumps(payload, indent=2, ensure_ascii=False)
        if filepath is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            attachment_name = f"{channel_name}_chat_{stamp}.json"
            file = discord.File(
                io.BytesIO(json_text.encode("utf-8")),
                filename=attachment_name,
            )
            await message.channel.send(
                content=f"*Exported {len(messages)} messages from `{channel_name}`.*",
                file=file,
            )
            return True
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_text)
            await message.channel.send(
                f"*Exported {len(messages)} messages from `{channel_name}` to `{filepath}`.*"
            )
        except OSError as e:
            await message.channel.send(f"*Failed to write to `{filepath}`: {e}*")
        return True

    if cmd == "!load" or cmd.startswith("!load "):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
            if not rows:
                await message.channel.send(
                    f"*No dynamically loaded files for `{channel_name}`.*"
                )
            else:
                lines = [f"**Dynamic files for `{channel_name}`** ({len(rows)}):"]
                for _param, path in rows:
                    lines.append(f"• `{path}`")
                await safe_send_chunked(message.channel, "\n".join(lines))
            return True
        path = parts[1].strip()
        if not os.path.isfile(path):
            await message.channel.send(
                f"*⚠️ Could not find file `{path}` — nothing loaded. Check the path and try again.*"
            )
            return True
        # Context-budget guard: make sure the system block (with this file
        # included) + a small reply reserve still fits in the channel's limit.
        new_content = load_file_content(path)
        if new_content is None:
            await message.channel.send(
                f"*⚠️ Could not read `{path}` — nothing loaded.*"
            )
            return True
        main_block, _ = build_llm_main_prompt(db, channel_name, memory_enabled=effective_memory, anchors_enabled=effective_anchors, social_mode=is_social_mode(channel_name))
        current_system_tokens = sum(estimate_tokens(b["text"], channel_key=channel_name) for b in main_block)
        new_file_tokens = estimate_tokens(new_content, channel_key=channel_name)
        REPLY_RESERVE = 2000  # tokens kept free for the model's reply
        projected = current_system_tokens + new_file_tokens + REPLY_RESERVE
        if projected > current_context_limit:
            over = projected - current_context_limit
            await message.channel.send(
                f"*⚠️ Refusing to load `{path}` — it would exceed this channel's context budget.*\n"
                f"*File: ~{new_file_tokens:,} tokens · current system block: ~{current_system_tokens:,} · "
                f"reserve: {REPLY_RESERVE:,} · limit: {current_context_limit:,} "
                f"(**over by ~{over:,} tokens**).*\n"
                f"*Unload something with `!unload`, "
                f"or switch to a larger-context model.*"
            )
            return True
        param = f"dynamic_file_{uuid.uuid4()}"
        set_channel_setting(db, channel_name, param, path)
        cache = state.DYNAMIC_CONTEXT_FILE_LOCATIONS.setdefault(channel_name, [])
        cache.append(path)
        headroom = current_context_limit - (current_system_tokens + new_file_tokens)
        await message.channel.send(
            f"*Loaded `{path}` into context for `{channel_name}` "
            f"(~{new_file_tokens:,} tokens; ~{headroom:,} tokens headroom for history + reply).*"
        )
        return True

    if cmd.startswith("!unload"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!unload <pathname>`*")
            return True
        target = parts[1].strip()
        rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
        match = next((p for p, v in rows if v == target), None)
        if match is None:
            await message.channel.send(
                f"*No dynamic file `{target}` loaded for `{channel_name}`. Try `!load` to see the list.*"
            )
            return True
        clear_channel_setting(db, channel_name, match)
        cache = state.DYNAMIC_CONTEXT_FILE_LOCATIONS.get(channel_name)
        if cache and target in cache:
            cache.remove(target)
        await message.channel.send(
            f"*Unloaded `{target}` from `{channel_name}`.*"
        )
        return True

    if cmd == "!clear":
        cursor = db.cursor()
        cursor.execute(
            "DELETE FROM messages WHERE channel = ?",
            (channel_name,)
        )
        db.commit()
        state._reference_images_by_channel.pop(channel_name, None)
        state._reference_videos_by_channel.pop(channel_name, None)
        # Drop the regen media anchor too: it may point at a Discord
        # message from the pre-clear session, and a new session must never
        # resurrect pre-clear attachments. Any post-clear media turn
        # re-writes it at save time.
        clear_channel_setting(db, channel_name, "last_media_discord_msg_id")
        # Session restart: the next journal entry only covers post-clear
        # content, so its date range must start at the clear time.
        _stamp_last_journal_datetime(db, channel_name)
        # Visual session divider — renders the timestamp in the reader's
        # local timezone via Discord's dynamic <t:...:f> markup, so both the
        # bot operator and any channel participants see a clear break
        # between the old conversation and the new one.
        ts = int(message.created_at.timestamp())
        dynamic_rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
        reload_note = ""
        if dynamic_rows:
            reload_note = "\nReloading Dynamic files:\n" + "\n".join(
                f"• `{path}`" for _param, path in dynamic_rows
            )
        await message.channel.send(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**· · · new session · · ·**\n"
            f"<t:{ts}:f>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Session history up to this point has been cleared.*"
            f"{reload_note}"
        )
        return True

    if cmd == "!regen" or cmd.startswith("!regen "):
        new_prompt = content[len("!regen"):].strip()
        cursor = db.cursor()

        if new_prompt:
            # Delete the last assistant response AND the last user prompt,
            # then resubmit with the rewritten prompt.
            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'assistant' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'user' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            db.commit()
            # Signal main.py to process this prompt through the full pipeline
            # and save both the user message and the assistant response.
            # carry_prior_attachments=False — `!regen <new>` starts fresh;
            # images / voice / text files from the prior turn are NOT carried
            # forward. Users who want them back should re-attach on the
            # !regen message itself.
            return {
                "action": "regen",
                "prompt": new_prompt,
                "save_user_prompt": True,
                "carry_prior_attachments": False,
            }
        else:
            # Recover the last user prompt text, then delete BOTH the last
            # assistant response and the last user entry. We delete + re-save
            # rather than leaving the user turn in place because the DB only
            # stores text — any image / voice / file attachments from the
            # original message would be lost. Re-running through the normal
            # pipeline (with media re-attached from the channel's stored
            # last_media_discord_msg_id anchor) preserves them on the
            # regenerated turn.
            cursor.execute(
                "SELECT content FROM messages WHERE channel = ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1",
                (channel_name,)
            )
            row = cursor.fetchone()
            if not row:
                await message.channel.send("*No user message found to regenerate from.*")
                return True
            last_user_prompt = row[0]
            # Detect and strip the trailing attachment-tag cluster
            # ("[image]" / "[audio]" / "[video]") added when the original
            # message was saved — the media itself is re-attached by the
            # regen pipeline via the last_media_discord_msg_id anchor, so
            # the stringified markers must not be echoed into the prompt
            # text. The cluster also gates the carry: a turn saved without
            # media tags regenerates text-only and must never inherit
            # attachments from an older, unrelated message.
            media_cluster = _MEDIA_TAG_CLUSTER_RE.search(last_user_prompt)
            if media_cluster:
                last_user_prompt = last_user_prompt[:media_cluster.start()].rstrip()

            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'assistant' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'user' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            db.commit()
            # save_user_prompt=True so on_message rebuilds the user turn via
            # the normal path. carry_prior_attachments is gated on the media
            # tag cluster above: True only when the regenerated turn itself
            # carried media, in which case main.py re-fetches the exact
            # Discord message stored in last_media_discord_msg_id to
            # re-attach it. Text-only turns regenerate text-only; if the
            # fetch fails, main.py silently falls back to text-only
            # (combined_content already holds the DB text).
            return {
                "action": "regen",
                "prompt": last_user_prompt,
                "save_user_prompt": True,
                "carry_prior_attachments": bool(media_cluster),
            }

    if cmd.startswith("!copycontextfrom"):
        source_channel, err = _validate_copy_source(
            db, channel_name, guild_name, content, usage_cmd="!copyContextFrom"
        )
        if err:
            await message.channel.send(err)
            return True
        await _do_copy_source_copy(db, message, channel_name, source_channel)
        return True

    if cmd == "!context":

        main_block, layers = build_llm_main_prompt(
            db, channel_name, memory_enabled=effective_memory, anchors_enabled=effective_anchors, social_mode=is_social_mode(channel_name)
        )

        history = build_trimmed_history_for_payload(
            db, channel_name, main_block, context_limit=current_context_limit,
            channel_key=channel_name, model=current_text_model
        )

        output = _build_context_detailed(
            layers, history, channel_name, current_context_limit, estimate_tokens, msg_tokens
        )
        await message.channel.send(output)
        return True

    if cmd == "!credits":
        lines = ["**API credits**"]
        # LLM provider
        credits = await provider.get_credits()
        if credits["error"]:
            lines.append(f"• **{credits['label']}**: {credits['error']}")
        elif credits["used"] > 0:
            lines.append(
                f"• **{credits['label']}**: ${credits['remaining']:.2f} remaining "
                f"(${credits['used']:.2f} used of ${credits['total']:.2f})"
            )
        else:
            lines.append(
                f"• **{credits['label']}**: ${credits['remaining']:.2f} remaining"
            )

        # ElevenLabs
        if config.ELEVENLABS_API_KEY and str(config.ELEVENLABS_API_KEY).strip():
            from .voice import get_elevenlabs_subscription
            sub = await get_elevenlabs_subscription()
            if sub["error"]:
                lines.append(f"• **ElevenLabs**: {sub['error']}")
            else:
                lines.append(
                    f"• **ElevenLabs** ({sub['tier']}): {sub['remaining']:,} chars remaining "
                    f"({sub['used']:,} / {sub['limit']:,} used)"
                )
        else:
            lines.append("• **ElevenLabs**: no API key configured")

        await safe_send_chunked(message.channel, "\n".join(lines))
        return True

    if cmd == "!diag":
        sections = []  # list of strings, each sent as a separate message

        # ── 1. LLM provider connectivity + credits + model validation ──
        prov_label = provider.provider_name()
        active_comp = get_channel_companion(channel_name)
        or_lines = [
            f"**🔍 Diagnostics** (`{channel_name}`)\n"
            f"Active Companion: **{active_comp}**\n"
            "────────────────────\n"
            f"**{prov_label}**"
        ]

        # Check credits
        credits = await provider.get_credits()
        if credits["error"]:
            or_lines.append(f"❌ {credits['error']}")
        elif credits["remaining"] > 0:
            or_lines.append(f"✅ Connected — credits active (${credits['remaining']:.2f} remaining)")
        else:
            or_lines.append(f"⚠️ Connected — **no credits remaining** (${credits['used']:.2f} / ${credits['total']:.2f} used)")

        # Fetch available models for validation
        available_models = await provider.get_models()
        available_ids = {m["id"] for m in available_models} if available_models else None
        if available_models is None or (not available_models and not credits["error"]):
            or_lines.append(f"⚠️ Could not fetch model list")

        # Validate configured models
        from .search import LIGHTWEIGHT_LOGIC_MODEL
        models_to_check = {
            "(default) Text model": config.CURRENT_TEXT_MODEL,
            "(default) Image model": config.CURRENT_IMAGE_MODEL,
            "(default) Video model": config.CURRENT_VIDEO_MODEL,
            "(default) Voice text model": config.CURRENT_VOICE_TEXT_MODEL,
            "(default) Lightweight logic model": LIGHTWEIGHT_LOGIC_MODEL,
        }
        # Include per-channel overrides for this channel
        ch_text = get_channel_setting(db, channel_name, "text_model")
        ch_image = get_channel_setting(db, channel_name, "image_model")
        ch_video = get_channel_setting(db, channel_name, "video_model")
        ch_voice = get_channel_setting(db, channel_name, "voice_text_model")
        if ch_text:
            models_to_check[f"(current) Text model (`{channel_name}`)"] = ch_text
        if ch_image:
            models_to_check[f"(current) Image model (`{channel_name}`)"] = ch_image
        if ch_video:
            models_to_check[f"(current) Video model (`{channel_name}`)"] = ch_video
        if ch_voice:
            models_to_check[f"(current) Voice text model (`{channel_name}`)"] = ch_voice

        or_lines.append("")
        or_lines.append("**Models**")
        for label, model_id in models_to_check.items():
            if not model_id or not str(model_id).strip():
                or_lines.append(f"⬜ {label}: *(not configured)*")
                continue
            normalized = provider.normalize_model_id(model_id)
            if available_ids is not None:
                if normalized in available_ids:
                    or_lines.append(f"✅ {label}: `{model_id}`")
                else:
                    or_lines.append(f"❌ {label}: `{model_id}` — **not found on {prov_label}**")
            else:
                or_lines.append(f"❓ {label}: `{model_id}` *(could not verify)*")

        sections.append("\n".join(or_lines))

        # ── 2. ElevenLabs connectivity ──
        el_lines = ["**ElevenLabs**"]
        if config.ELEVENLABS_API_KEY and str(config.ELEVENLABS_API_KEY).strip():
            from .voice import get_elevenlabs_subscription
            sub = await get_elevenlabs_subscription()
            if sub["error"]:
                el_lines.append(f"❌ {sub['error']}")
            elif sub["remaining"] > 0:
                el_lines.append(f"✅ Connected ({sub['tier']}) — {sub['remaining']:,} chars remaining")
            else:
                el_lines.append(f"⚠️ Connected ({sub['tier']}) — **no characters remaining** ({sub['used']:,} / {sub['limit']:,})")

            el_lines.append(f"Voice model: `{getattr(config, 'ELEVENLABS_VOICE_MODEL', 'N/A')}`")
            el_lines.append(f"Voice ID: `{getattr(config, 'ELEVENLABS_VOICE_ID', 'N/A')}`")
            ch_voice_id = get_channel_setting(db, channel_name, "elevenlabs_voice_id")
            if ch_voice_id:
                el_lines.append(f"Voice ID (`{channel_name}`): `{ch_voice_id}`")
        else:
            el_lines.append("⬜ No API key configured")

        sections.append("\n".join(el_lines))

        # ── 3. System tools & dependencies ──
        sys_lines = ["**System**"]

        # Runtime environment fingerprint — makes environment-specific
        # failures self-classifying in future bug reports (e.g. a broken
        # brotli module making aiohttp negotiate encodings it can't decode).
        try:
            ces = compression_env_state()
            exe_lower = sys.executable.lower()
            if "conda" in exe_lower:
                py_origin = "conda"
            elif sys.prefix != sys.base_prefix:
                py_origin = "venv"
            else:
                py_origin = "system"
            env_bits = [
                f"Python {sys.version.split()[0]} ({py_origin})",
                f"aiohttp {ces['aiohttp']}",
                f"brotli: {ces['brotli']}" + (f" ({ces['brotli_module']})" if ces["brotli_module"] else ""),
                f"zstd: {ces['zstd']}" + (f" ({ces['zstd_module']})" if ces["zstd_module"] else ""),
            ]
            if ces["brotli"] == "broken" or ces["zstd"] == "broken":
                sys_lines.append(f"⚠️ Runtime: {' | '.join(env_bits)}")
                sys_lines.append(
                    "  ⚠️ A broken brotli/zstd module is installed (it imports but "
                    "fails to decompress). Alcove's pinned Accept-Encoding avoids it "
                    "for API calls, but other HTTP clients on this machine may fail "
                    "until it is reinstalled or removed."
                )
            else:
                sys_lines.append(f"✅ Runtime: {' | '.join(env_bits)}")
        except Exception as e:
            sys_lines.append(f"⚠️ Runtime fingerprint unavailable: {e}")

        # ffmpeg
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-version"],
                    capture_output=True, text=True, timeout=5,
                )
                version_line = result.stdout.split("\n")[0] if result.stdout else "unknown version"
                sys_lines.append(f"✅ ffmpeg: `{version_line}`")
            except Exception:
                sys_lines.append(f"✅ ffmpeg: found at `{ffmpeg_path}` (version unknown)")
        else:
            sys_lines.append("❌ ffmpeg: **not found** — voice playback will not work")

        # opus
        if discord.opus.is_loaded():
            sys_lines.append("✅ libopus: loaded")
        else:
            sys_lines.append("❌ libopus: **not loaded** — voice will not work")

        # System prompt
        sp_path = getattr(config, "SYSTEM_PROMPT_LOCATION", None)
        if sp_path and str(sp_path).strip():
            if os.path.isfile(sp_path):
                size = os.path.getsize(sp_path)
                sys_lines.append(f"✅ System prompt: `{os.path.basename(sp_path)}` ({size:,} bytes)")
            else:
                sys_lines.append(f"❌ System prompt: `{sp_path}` — **file not found**")
        else:
            from .companions import get_companion_resolved_locations
            locs = get_companion_resolved_locations(db, channel_name)
            auto_sp = locs.get("SYSTEM_PROMPT_LOCATION", "")
            if auto_sp and os.path.isfile(auto_sp):
                size = os.path.getsize(auto_sp)
                sys_lines.append(f"⬜ System prompt: *(not configured — using auto-loaded `{os.path.basename(auto_sp)}` ({size:,} bytes))*")
            else:
                sys_lines.append("⬜ System prompt: *(not configured — no auto-loaded version available)*")

        # Instruction files
        for i, path in enumerate(config.INSTRUCTION_LOCATIONS, 1):
            if os.path.isfile(path):
                sys_lines.append(f"✅ Instruction {i}: `{os.path.basename(path)}`")
            else:
                sys_lines.append(f"❌ Instruction {i}: `{path}` — **not found**")

        # Tool files (auto-discovered from tools/ unless overridden in config)
        from .companions import get_companion_resolved_locations
        locs = get_companion_resolved_locations(db, channel_name)
        for i, path in enumerate(locs["LOADED_TOOL_LOCATIONS"], 1):
            if os.path.isfile(path):
                sys_lines.append(f"✅ Tool {i}: `{os.path.basename(path)}`")
            else:
                sys_lines.append(f"❌ Tool {i}: `{path}` — **not found**")

        sections.append("\n".join(sys_lines))

        # ── 4. Context usage ──
        main_block, layers = build_llm_main_prompt(
            db, channel_name, memory_enabled=effective_memory, anchors_enabled=effective_anchors, social_mode=is_social_mode(channel_name)
        )
        history = build_trimmed_history_for_payload(
            db, channel_name, main_block, context_limit=current_context_limit,
            channel_key=channel_name, model=current_text_model
        )
        ctx_output = _build_context_detailed(
            layers, history, channel_name, current_context_limit, estimate_tokens, msg_tokens
        )
        sections.append(ctx_output)

        sections.append(_build_channel_settings_block(db, channel_name))

        # Send each section as a separate message (stay under 2000 chars)
        for section in sections:
            await safe_send_chunked(message.channel, section)
        return True

    # ── Macros ──
    if cmd == "!macros":
        rows = list_global_vars(db, param_prefix="macro_")
        stored = {param: value for param, value in rows}
        lines = ["**Macros**"]
        for n in range(MACRO_MIN, MACRO_MAX + 1):
            value = stored.get(f"macro_{n}")
            if value:
                display = value if len(value) <= 80 else value[:80] + "..."
                lines.append(f"`!M{n}` {display}")
            else:
                lines.append(f"`!M{n}`")
        await safe_send_chunked(message.channel, "\n".join(lines))
        return True

    if cmd.startswith("!forgetmacro"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send(
                f"*Usage: `!forgetMacro M<n>` where n is {MACRO_MIN}-{MACRO_MAX}*"
            )
            return True
        target = parts[1].strip()
        m = re.match(r"^[Mm](\d+)$", target)
        if not m:
            await message.channel.send(
                f"*Invalid macro reference **{target}**. Use `M1`..`M{MACRO_MAX}`.*"
            )
            return True
        n = int(m.group(1))
        if not (MACRO_MIN <= n <= MACRO_MAX):
            await message.channel.send(
                f"*Macro number must be between {MACRO_MIN} and {MACRO_MAX}.*"
            )
            return True
        if delete_global_var(db, f"macro_{n}"):
            await message.channel.send(f"*Macro M{n} forgotten.*")
        else:
            await message.channel.send(f"*Macro M{n} was not set.*")
        return True

    _macro_match = _MACRO_RE.match(content.strip())
    if _macro_match:
        n = int(_macro_match.group(1))
        if not (MACRO_MIN <= n <= MACRO_MAX):
            await message.channel.send(
                f"*Macro number must be between {MACRO_MIN} and {MACRO_MAX}.*"
            )
            return True
        param = f"macro_{n}"
        arg = _macro_match.group(2)
        if arg is None or not arg.strip():
            # ── Execute macro ──
            stored = get_global_var(db, param)
            if stored is None:
                await message.channel.send(f"*Macro M{n} is undefined.*")
                return True
            stored_stripped = stored.strip()
            # Guard against macros invoking other macros (prevents loops).
            if _MACRO_RE.match(stored_stripped):
                await message.channel.send(
                    "*Macros cannot invoke other macros.*"
                )
                return True
            if stored_stripped.startswith("!"):
                # Recursively dispatch the stored ! command as if typed.
                inner = await handle_command(
                    message, stored_stripped.lower(), stored_stripped,
                    channel_name, guild_name, db, effective_memory, effective_anchors,
                    current_text_model, current_voice_model, current_image_model,
                    current_video_model,
                    current_voice_id, current_context_limit,
                    current_reasoning_effort,
                    current_temperature, current_top_k,
                    build_llm_main_prompt, build_trimmed_history_for_payload,
                    compose_full_messages, estimate_tokens, msg_tokens,
                    load_file_content, get_image_response,
                    get_video_response=get_video_response,
                    auto_context_adjust=auto_context_adjust,
                    client=client,
                )
                if inner is False:
                    # Unrecognised ! command inside the macro — treat the
                    # stored text as a regular prompt.
                    return {"action": "execute", "prompt": stored_stripped}
                return inner
            # Plain text prompt — signal main.py to run it through the
            # normal response pipeline.
            return {"action": "execute", "prompt": stored_stripped}
        # ── Set macro ──
        value = arg.strip()
        set_global_var(db, param, value)
        await message.channel.send(f"*Macro M{n} set.*")
        return True

    if cmd == "!restart":
        # Re-exec the bot via os.execv, preserving the parent PID on POSIX so
        # start.command's alcove.pid kill mechanism keeps working. The command
        # is gated to AUTHORIZED_HUMAN_DISCORD_USERNAME by check_gating, so
        # only the operator can trigger it.
        #
        # We schedule the actual restart on a short delay so that:
        #   1. The "Restarting..." message is already flushed to Discord.
        #   2. handle_command returns True, letting on_message's finally
        #      block run (discards ON_MESSAGE_IN_FLIGHT) before we execv.
        async def _do_restart():
            await asyncio.sleep(1.5)
            try:
                # Gracefully disconnect from any active voice channel first.
                try:
                    if voice_manager.is_connected(guild=message.guild):
                        await voice_manager.leave(guild=message.guild)
                except Exception as e:
                    print(f"⚠️ restart: voice leave failed: {e}")
                # Close all SQLite connections so WAL is checkpointed and
                # file locks released before exec. ChromaDB's file handles
                # are released automatically by the kernel on execv.
                try:
                    from . import database
                    for _name, _conn in database._db_connections.items():
                        try:
                            _conn.close()
                        except Exception:
                            pass
                    database._db_connections.clear()
                    if database._registry_db is not None:
                        try:
                            database._registry_db.close()
                        except Exception:
                            pass
                        database._registry_db = None
                except Exception as e:
                    print(f"⚠️ restart: DB close failed: {e}")
                print("🔄 restarting: re-executing modules/alcove.py")
                sys.stdout.flush()
                _project_root = str(Path(__file__).resolve().parent.parent)
                if os.name == "nt":
                    # Windows: os.execv spawns a new process (PID changes) and
                    # START.BAT runs in the foreground without a PID file, so
                    # fall back to a detached subprocess + exit.
                    subprocess.Popen(
                        [sys.executable, "-u", "modules/alcove.py"],
                        cwd=_project_root,
                    )
                    os._exit(0)
                else:
                    os.chdir(_project_root)
                    os.execv(sys.executable, [sys.executable, "-u", "modules/alcove.py"])
            except Exception as e:
                print(f"❌ restart failed: {e}")
                try:
                    await message.channel.send(f"*❌ Restart failed: {e}*")
                except Exception:
                    pass

        asyncio.create_task(_do_restart())
        await message.channel.send(
            "*🔄 Restarting Alcove…*"
        )
        return True

    if cmd == "!amiauthorized":
        authorized = getattr(config, "AUTHORIZED_HUMAN_DISCORD_USERNAME", "") or ""
        authorized = authorized.strip()
        caller = message.author.name
        if not authorized:
            await message.channel.send(
                f"*⚠️ No authorized user is configured — **anyone on this server can run commands**. Beware!*"
            )
        elif caller.lower() == authorized.lower():
            await message.channel.send(
                f"*✅ Yes — you (**{caller}**) are the authorized user.*"
            )
        else:
            await message.channel.send(
                f"*❌ No — you (**{caller}**) are not the authorized user.*"
            )
        return True

    if cmd == "!reply":
        if not is_social_mode(channel_name):
            await message.channel.send(
                f"*`!reply` only works in social mode. Enable it here with `!socialmode` first.*"
            )
            return True
        # Find the message immediately above this command (any author). The
        # bot's response will be posted as a Discord reply to that message,
        # so if it's another bot it will be triggered to respond.
        prev_msg = None
        try:
            async for prev in message.channel.history(limit=1, before=message):
                prev_msg = prev
                break
        except Exception as e:
            await message.channel.send(f"*⚠️ Could not look up the previous message: {e}*")
            return True
        if prev_msg is None:
            await message.channel.send("*There's no message above this one to reply to.*")
            return True
        # Signal main.py to respond to the existing last user turn in history
        # (already saved by social-mode gating) and post the reply to prev_msg.
        return {"action": "reply_last", "reply_reference": prev_msg}

    if cmd == "!channelsettings":
        output = _build_channel_settings_block(db, channel_name)
        await safe_send_chunked(message.channel, output)
        return True

    if cmd == "!help":
        # Discord caps messages at 2000 characters, so send each section
        # as its own message. Keep each section well under 2000 on its own.
        help_sections = [
            # Group 1: Models + Resets
            "**Companion commands**\n"
            "────────────────────\n"
            "**Models**\n"
            "`!model` — show text and image models, or set text model for this channel: `!model <openrouter-id>`\n"
            "`!imageModel` — show or set image model for this channel: `!imagemodel <openrouter-id>`\n"
            "`!videoModel` — show or set video model for this channel: `!videomodel <openrouter-id>`\n"
            "`!voiceTextModel` — show or set the text model used for voice replies: `!voicetextmodel <openrouter-id>`\n"
            "`!voiceModelID` — show or set the ElevenLabs voice ID for this channel: `!voicemodelid <voice-id>`\n"
            "`!reasoning <off|minimal|low|medium|high|xhigh> [Display]` — set extended thinking level (thinking output hidden by default; Display shows it)\n"
            "`!temperature` — show current temperature, or set for this channel: `!temperature <0.0-2.0>`\n"
            "`!topk` — show current top K, or set for this channel: `!topk <0-100>`\n"
            "────────────────────\n"
            "**Resets**\n"
            "`!resetChannelSettings` — clear all settings (models, context limit, memory/search overrides, etc.) for THIS channel\n"
            "`!resetModel` — reset text model to default across ALL channels\n"
            "`!resetImageModel` — reset image model to default across ALL channels\n"
            "`!resetVideoModel` — reset video model to default across ALL channels\n"
            "`!resetVoiceTextModel` — reset voice text model to default across ALL channels\n"
            "`!resetVoiceModelID` — reset ElevenLabs voice ID to default across ALL channels\n",

            # Group 2: Context + Images + Memory & Knowledge
            "────────────────────\n"
            "**Context**\n"
            "`!context` — show detailed token breakdown of the prompt stack and headroom left\n"
            "`!clear` — delete session context for this channel\n"
            "`!copyContextFrom <channel>` — erase this channel's history and copy all messages from the named channel\n"
            "`!exportChat [pathname]` — export all messages for this channel to a JSON file (omit pathname to attach it directly to the chat)\n"
            "`!regen` — regenerate the last response, or `!regen <new prompt>` to replace and resubmit\n"
            "────────────────────\n"
            "**Images**\n"
            "`!image <prompt>` — generate an image from a prompt (and optionally attached reference images)\n"
            "────────────────────\n"
            "**Videos**\n"
            "`!video <prompt>` — generate a video from a prompt (attach images or videos as references; may take a few minutes)",

            # Group 2b: Memory & Knowledge
            "────────────────────\n"
            "**Memory & Knowledge**\n"            "`!remember [global] <text>` — anchor a memory to a specific channel or across all channels\n"
            "`!memories` — list anchored memories (with ids)\n"
            "`!forget <id>` — remove an anchored memory by id\n"
            "`!exportMemories [pathname]` — export all memories (global + channel) to a JSON file (omit pathname to attach it directly to the chat)\n"
            "\n`!knowledge` — enable knowledge files in context for this channel (default)\n"
            "`!noknowledge` — disable knowledge files from context for this channel\n"
            "`!anchors` — enable anchored memories in context for this channel (default)\n"
            "`!noanchors` — disable anchored memories from context for this channel\n"
	        "`!search` — enable fusion search of reference files for this channel (default)\n"
	        "`!nosearch` — disable fusion search of reference files for this channel\n"
            "\n`!load <pathname>` — dynamically load a speciality file into context for THIS channel\n"
            "`!load` — list dynamically loaded files for this channel\n"
            "`!unload <pathname>` — remove a dynamically loaded file from this channel\n"
            "\n`!updateJournal [perm]` — summarize latest developments into the auto-journal (add `perm` for a permanent, never-archived entry)",

            # Group 3: Macros
            "────────────────────\n"
            "**Macros**\n"
            f"`!M<n> <prompt or !command>` — set macro n (n = {MACRO_MIN}-{MACRO_MAX})\n"
            f"`!M<n>` — execute the stored macro n\n"
            "`!macros` — list all macros\n"
            "`!forgetMacro M<n>` — remove a specific macro",

            # Group 4: Voice
            "────────────────────\n"
            "**Voice**\n"
            "`!join` — join the current voice channel for push-to-talk mode\n"
            "`!joinLive` — join the current voice channel for live voice mode\n"
            "`!leave` — disconnect from voice",

            # Group 5: Scheduled Tasks
            "────────────────────\n"
            "**Scheduled Tasks**\n"
            "`!tasks` — list all scheduled tasks (id, type, next fire time, prompt, channel)\n"
            "`!task <id>` — show full details for a single scheduled task\n"
            "`!removeTask <id>` — remove a scheduled task by id\n"
            "`!enableTask <id>` — re-enable a disabled task\n"
            "`!disableTask <id>` — disable a task without removing it\n"
            "\n*Tip: to create or edit a task, just ask your companion directly — e.g. \"remind the group every Monday at 9am to review the week\" or \"schedule a one-time check-in for July 20 at 2:30pm\".*",

            # Group 6: Other
            "────────────────────\n"
            "**Other**\n"
            "`!channel` — show this channel's id string\n"
            "`!defaultChannel` — set this channel as default for companion-initiated actions\n"
            "`!noDefaultChannel` — clear the default channel for companion-initiated actions\n"
            "`!channelRename <old> <new>` — rename a channel in the database (migrate history after a Discord rename)\n"
            "`!switchCompanion [default | companionName]` — set the active companion for this channel, or show current\n"
    #        "`!amIAuthorized` — tells you whether you are the authorized user (open to everyone)\n"
    #        "`!reply` — (social mode only) reply to the last message above this command\n"
            "\n`!credits` — show remaining credits on your LLM provider and ElevenLabs\n"
            "`!diag` — run diagnostics (API connectivity, model validation, system tools)\n"
            "`!channelSettings` — show this channel's active modes (!knowledge/!noknowledge, etc.) and per-channel overrides\n"
            "`!restart` — restart the bot process (re-runs pre-flight, preserves parent PID on macOS/Linux)\n"
            "`!help` — this list",
        ]
        for i, section in enumerate(help_sections):
            if i > 0:
                await asyncio.sleep(0.5)
            await message.channel.send(section)
        return True

    # Not a recognised command. If it looks like a typo'd command
    # (short message whose second character is alphanumeric — i.e. `!foo`
    # rather than `! hey`, `!!`, `!?`, etc.), report it back so we don't
    # waste LLM tokens on it.
    if len(content) < 60 and len(content) >= 2 and content[1].isalnum():
        command_word = content.split(maxsplit=1)[0]
        await message.channel.send(
            f"*Sorry, `{command_word}` is not a recognized command.*"
        )
        return True

    return False
