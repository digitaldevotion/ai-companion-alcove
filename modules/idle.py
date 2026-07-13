# ============================================
# Alcove — idle.py
# Background tasks, idle prompts, and live voice
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import aiohttp
import random
import time
import traceback
from datetime import datetime, timedelta, timezone, time as dt_time
from pathlib import Path

import discord.ext.tasks as tasks

from . import provider
from . import state
from .companions import (
    get_companion_paths, _scan_dir_for_files,
    get_companion_resolved_locations, load_file_content,
)
from .prompt import (
    _is_error_response,
    build_llm_main_prompt, build_trimmed_history_for_payload,
    compose_full_messages,
    get_ai_response, get_image_response,
)
from .attachments import url_to_data_uri
from .directives import process_response
from .voice import voice_manager, text_to_speech, speech_to_text
from .utils import safe_send, safe_send_chunked, build_channel_key, parse_channel_key, estimate_tokens, msg_tokens, tokens_to_bytes
from .context import resolve_channel_context_by_key
from .database import (
    save_message, get_message_count,
    get_channel_setting, prune_orphan_channels,
    get_db, get_channel_companion,
    get_global_var, set_global_var,
)
import config


# ============================================
# MUTABLE STATE
# Last interaction channel is now in state.py (state.LAST_INTERACTION_CHANNEL).
# Live voice session is now in state.py (state.LIVE_VOICE_SESSION).
# ============================================

# Diagnostic-logging throttle state (module-local, per companion).
#  _idle_block_log_state: companion -> last "reached threshold but did not fire"
#    reason string, so we log it once per state change instead of every minute.
#  _idle_prev_counters: companion -> (last_seen_counters_dict, stale_tick_count)
#    used to warn when a counter stops progressing unexpectedly.
_idle_block_log_state = {}
_idle_prev_counters = {}


# ============================================
# COMPANION DISCOVERY
# ============================================
def discover_companions():
    databases_dir = Path("databases")
    if not databases_dir.is_dir():
        return ["default"]
    companions = []
    for p in databases_dir.iterdir():
        if p.is_dir() and p.name != "registry":
            companions.append(p.name.lower())
    return companions if companions else ["default"]


# ============================================
# PER-COMPANION IDLE COUNTERS
# ============================================
_COUNTER_KEYS = (
    "idle_action_minutes_since_idle",
    "dream_state_minutes_since_idle",
    "idle_note_minutes_since_idle",
)


def _get_idle_counters(db):
    return {k: int(get_global_var(db, k) or 0) for k in _COUNTER_KEYS}


def _increment_idle_counters(db):
    for k in _COUNTER_KEYS:
        cur = int(get_global_var(db, k) or 0)
        set_global_var(db, k, str(cur + 1))


def _reset_idle_counters(db):
    for k in _COUNTER_KEYS:
        set_global_var(db, k, "0")


def _is_hour_in_window(hour, start, stop):
    if start <= stop:
        return start <= hour < stop
    return hour >= start or hour < stop


def reset_idle_counters_for_companion(companion_name, source="unknown"):
    db = get_db(companion_name)
    # Snapshot prior values so we only log resets that actually clear progress.
    try:
        prior = _get_idle_counters(db)
        had_progress = any(v > 0 for v in prior.values())
    except Exception:
        prior = None
        had_progress = True
    _reset_idle_counters(db)
    # Once reset, clear any "blocked at threshold" log state so the next block
    # is reported fresh.
    _idle_block_log_state.pop(companion_name, None)
    if had_progress:
        if prior:
            print(f"🔄 idle counters reset for '{companion_name}' (source={source}) "
                  f"[idle={prior['idle_action_minutes_since_idle']}, "
                  f"dream={prior['dream_state_minutes_since_idle']}, "
                  f"note={prior['idle_note_minutes_since_idle']}]")
        else:
            print(f"🔄 idle counters reset for '{companion_name}' (source={source})")


def _update_block_state(companion_name, block_key, block_msg):
    """Log a 'reached threshold but did not fire' reason once per state change.

    block_key is a stable identifier for the block reason (NOT the full message,
    which contains minute counts that change every tick). We only print when the
    key changes, so a persistent block (e.g. waiting hours for the dream window)
    logs exactly once. Passing block_key=None clears any prior block state.
    """
    prev = _idle_block_log_state.get(companion_name)
    if block_key is None:
        if prev is not None:
            _idle_block_log_state.pop(companion_name, None)
        return
    if prev != block_key:
        _idle_block_log_state[companion_name] = block_key
        if block_msg:
            print(block_msg)


def _check_stuck_counter(companion_name, channel_key, counters):
    """Warn (once per stuck-state entry) if a counter stops progressing.

    The tick increments counters every minute, so a value that is unchanged from
    the previous tick (and > 5) signals the loop/persistence is not advancing as
    expected. Suppressed while something active legitimately explains it.
    """
    primary = max(counters["idle_action_minutes_since_idle"],
                  counters["dream_state_minutes_since_idle"])
    prev_val, warned = _idle_prev_counters.get(companion_name, (None, False))
    if prev_val is not None and primary == prev_val and primary > 5:
        busy = (channel_key in state.ON_MESSAGE_IN_FLIGHT
                or state.LIVE_VOICE_SESSION is not None)
        if not busy and not warned:
            print(f"⚠️ idle_action_tick: counter for '{companion_name}' stuck at "
                  f"{primary} across consecutive ticks (not progressing) — the tick "
                  f"loop or this companion's default_channel may be broken.")
            warned = True
    else:
        warned = False
    _idle_prev_counters[companion_name] = (primary, warned)


# ============================================
# RANDOM THOUGHT COUNTER
# Separate from the _COUNTER_KEYS group above because:
#  - it uses a -1 sentinel (disabled / not-yet-armed),
#  - it only increments while > -1,
#  - it resets to 0 after USER-DRIVEN LLM responses (not at message intake,
#    and not after spontaneous idle/dream/note responses).
# ============================================
_RANDOM_THOUGHT_COUNTER_KEY = "random_thought_minutes_since_response"


def _get_random_thought_counter(db):
    val = get_global_var(db, _RANDOM_THOUGHT_COUNTER_KEY)
    if val is None:
        return -1
    try:
        return int(val)
    except (TypeError, ValueError):
        return -1


def _reset_random_thought_counter(db):
    """Re-arm: start counting from 0 (called after a user-driven LLM response)."""
    set_global_var(db, _RANDOM_THOUGHT_COUNTER_KEY, "0")


def _disable_random_thought_counter(db):
    """Disarm: set to -1 until the next user-driven LLM response re-arms it."""
    set_global_var(db, _RANDOM_THOUGHT_COUNTER_KEY, "-1")


# ============================================
# REFERENCES — set by init() from main.py
# ============================================
_client = None
_default_db = None


def init(client, default_db):
    """Called from on_ready to provide the Discord client and default DB."""
    global _client, _default_db
    _client = client
    _default_db = default_db


def init_vector_store(db, search_mode=None, chunk_size=None):
    """Initialize the vector store at boot. Called from main.py module level."""
    if search_mode is None:
        search_mode = state.SEARCH_REFERENCES_MODE
    if chunk_size is None:
        chunk_size = state.SEARCH_REFERENCES_CHUNK_SIZE
    if search_mode == 2:
        try:
            from . import vectors
            default_paths = get_companion_resolved_locations(db, "_global")
            vectors.init_vector_store(
                default_paths["SEARCH_REFERENCE_LOCATIONS"],
                chunk_size=chunk_size,
                companion_name="default"
            )
            print("🔎 Vector search initialized for 'default' (ChromaDB)")
        except Exception as e:
            print(f"⚠️ Vector search init failed: {e}")
            print("   Falling back to semantic expansion mode")
            state.SEARCH_REFERENCES_MODE = 1


# ============================================
# CHANNEL RESOLVER
# ============================================
async def _resolve_channel_by_key(channel_key):
    guild_part, chan_part = parse_channel_key(channel_key)
    if guild_part is None:
        return None

    if guild_part == "dm":
        for guild in _client.guilds:
            for member in guild.members:
                if member.name.lower() == chan_part:
                    return await member.create_dm()
        return None

    for guild in _client.guilds:
        if guild.name.lower() != guild_part:
            continue
        for ch in guild.text_channels:
            if ch.name.lower() == chan_part:
                return ch
        for th in guild.threads:
            if th.name.lower() == chan_part:
                return th
    return None


# ============================================
# IDLE PROMPT RUNNER
# ============================================
async def _run_idle_prompt(channel_key, prompt_text, log_label, companion_name=None):
    target_channel = await _resolve_channel_by_key(channel_key)
    if target_channel is None:
        print(f"⚠️ {log_label}: could not resolve channel '{channel_key}'")
        return

    effective_memory = bool(getattr(config, "MEMORY_ENABLED", True))
    ctx = await resolve_channel_context_by_key(
        channel_key, effective_memory, state.SEARCH_REFERENCES_MODE,
        companion_name=companion_name,
    )

    locs = get_companion_resolved_locations(ctx.db, channel_key)

    if state.SEARCH_REFERENCES_MODE == 2:
        from . import vectors
        if getattr(vectors, "_active_companion_name", None) != ctx.active_companion:
            print(f"🔎 Dynamically reloading vector DB for companion '{ctx.active_companion}'")
            vectors.init_vector_store(
                locs["SEARCH_REFERENCE_LOCATIONS"],
                companion_name=ctx.active_companion,
            )

    main_block = build_llm_main_prompt(ctx.db, channel_key, memory_enabled=ctx.effective_memory, anchors_enabled=ctx.effective_anchors, social_mode=ctx.social_mode)
    history = build_trimmed_history_for_payload(
        ctx.db, channel_key, main_block, context_limit=ctx.current_context_limit,
        channel_key=channel_key, model=ctx.current_text_model
    )
    full_messages = compose_full_messages(main_block, history)

    now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
    day_name = now.strftime("%A")
    time_str = now.strftime("%I:%M %p").lstrip("0")
    date_str = now.strftime("%B %d, %Y")

    full_messages.append({
        "role": "user",
        "content": f"{time_str} {day_name} {date_str}: {prompt_text}",
    })

    response_text, _ = await get_ai_response(
        full_messages, model=ctx.current_text_model, reasoning_effort=ctx.current_reasoning_effort,
        temperature=ctx.current_temperature, top_k=ctx.current_top_k,
        channel_key=channel_key,
        provider_lock=ctx.current_text_provider_lock,
    )

    async def channel_image_handler(prompt, reference_images=None):
        return await get_image_response(prompt, model=ctx.current_image_model, reference_images=reference_images, provider_lock=ctx.current_image_provider_lock)

    display_text, runcmd_results, readweb_results, readimage_results, _, _ = await process_response(
        response_text, target_channel,
        image_handler=channel_image_handler, db=ctx.db,
        active_companion=ctx.active_companion,
        send_func=safe_send,
        channel_name=channel_key,
    )

    # --- Tool-chain follow-up loop (same pattern as on_message) ---
    tool_round = 0
    while (runcmd_results or readweb_results or readimage_results) and tool_round < config.MAX_TOOL_ROUNDS:
        tool_round += 1

        if tool_round == 1 and display_text:
            await safe_send_chunked(target_channel, display_text)
            display_text = ""

        result_lines = []
        prompt_tokens = sum(msg_tokens(m, channel_key=channel_key) for m in full_messages)
        token_budget = ctx.current_context_limit - prompt_tokens
        runcmd_cap = tokens_to_bytes(token_budget, channel_key=channel_key) if config.MAXIMIZE_AVAILABLE_CONTEXT else 16384
        readweb_cap = tokens_to_bytes(token_budget, channel_key=channel_key) if config.MAXIMIZE_AVAILABLE_CONTEXT else 4000
        for r in runcmd_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= runcmd_cap else r["output"][:runcmd_cap] + "... (truncated)"
            result_lines.append(f"$ {r['command']}\n[{status}]\n{output}")
        for r in readweb_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= readweb_cap else r["output"][:readweb_cap] + "... (truncated)"
            result_lines.append(f"🌐 {r['url']}\n[{status}]\n{output}")
        attached_image_urls = []
        for r in readimage_results:
            if r["success"]:
                result_lines.append(f"🖼️ {r['source']}\n[SUCCESS]\n(image attached below)")
                attached_image_urls.append(r["image_url"])
            else:
                result_lines.append(f"🖼️ {r['source']}\n[FAILED]\n{r['error']}")
        results_summary = "\n\n".join(result_lines)
        tool_user_text = f"Tool responses:\n\n{results_summary}"

        full_messages.append({"role": "assistant", "content": response_text})
        if attached_image_urls:
            async def _to_data_uri(u):
                # execute_readimage already returns data: URIs for local files;
                # only HTTP(S) URLs need to be fetched and re-encoded.
                return u if u.startswith("data:") else await url_to_data_uri(u)
            image_data_uris = await asyncio.gather(
                *[_to_data_uri(u) for u in attached_image_urls]
            )
            image_data_uris = [d for d in image_data_uris if d]
            tool_content_blocks = [{"type": "text", "text": tool_user_text}]
            for data_uri in image_data_uris:
                tool_content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                })
            full_messages.append({"role": "user", "content": tool_content_blocks})
        else:
            full_messages.append({"role": "user", "content": tool_user_text})

        if not _is_error_response(response_text):
            save_message(ctx.db, channel_key, "assistant", response_text)
        save_message(ctx.db, channel_key, "user", tool_user_text, "system")

        print(f"🔁 [{log_label}] Round {tool_round}/{config.MAX_TOOL_ROUNDS}: "
              f"sending tool output back to model for follow-up response...")
        followup_text, _ = await get_ai_response(
            full_messages, model=ctx.current_text_model, reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=channel_key,
            provider_lock=ctx.current_text_provider_lock,
        )

        followup_display, runcmd_results, readweb_results, readimage_results, _, _ = await process_response(
            followup_text, target_channel,
            image_handler=channel_image_handler, db=ctx.db,
            active_companion=ctx.active_companion,
            suppress_reacts=True,
            send_func=safe_send,
            channel_name=channel_key,
        )
        if followup_display:
            await safe_send_chunked(target_channel, followup_display)
            display_text = ""
        response_text = followup_text

    if not _is_error_response(response_text):
        save_message(ctx.db, channel_key, "assistant", response_text)
    else:
        print(f"⚠️ [{log_label}] Skipping DB save of error response: {response_text[:120]}")

    if display_text:
        await safe_send_chunked(target_channel, display_text)

    # --- Optional Dream Image Generation ---
    dream_image_enabled = getattr(config, "DREAM_STATE_GENERATE_IMAGE", False)
    if log_label == "dream_tick":
        if not dream_image_enabled:
            print("😴 dream image generation skipped: DREAM_STATE_GENERATE_IMAGE is not enabled in config")
    if log_label == "dream_tick" and dream_image_enabled:
        full_messages.append({
            "role": "assistant",
            "content": response_text,
        })
        followup_prompt = (
            "Now, please generate a beautiful, detailed image representing the dream you just described. If the dream contained actual represenations of you and me, and you have our physical descriptions, please include them as well so the image renders properly. "
            "Use the <createimage prompt=\"...\" /> directive to invoke your image generation tool."
        )
        full_messages.append({
            "role": "user",
            "content": followup_prompt,
        })

        try:
            followup_response, _ = await get_ai_response(
                full_messages, model=ctx.current_text_model, reasoning_effort=ctx.current_reasoning_effort,
                temperature=ctx.current_temperature, top_k=ctx.current_top_k,
                channel_key=channel_key,
                provider_lock=ctx.current_text_provider_lock,
            )

            followup_display_text, _, _, _, _, _ = await process_response(
                followup_response, target_channel,
                image_handler=channel_image_handler, db=ctx.db,
                active_companion=ctx.active_companion,
                send_func=safe_send,
                channel_name=channel_key,
            )

            if not _is_error_response(followup_response):
                save_message(ctx.db, channel_key, "assistant", followup_response)
            else:
                print(f"⚠️ [dream_image] Skipping DB save of error response: {followup_response[:120]}")

            if followup_display_text:
                await safe_send_chunked(target_channel, followup_display_text)
        except Exception as e:
            print(f"⚠️ Failed to generate follow-up dream image: {e}")


# ============================================
# RANDOM THOUGHT PROMPT RUNNER
# Prompts the LLM to seamlessly continue its last response with a brief random
# thought / stroke of genius / moment of inspiration. After the response is
# chunked to Discord, the random-thought counter is set to -1 (NOT reset to 0),
# so it will not fire again until the next user-driven LLM response re-arms it.
# ============================================
async def _run_random_thought_prompt(channel_key, companion_name=None):
    target_channel = await _resolve_channel_by_key(channel_key)
    if target_channel is None:
        print(f"⚠️ random_thought: could not resolve channel '{channel_key}'")
        return

    effective_memory = bool(getattr(config, "MEMORY_ENABLED", True))
    ctx = await resolve_channel_context_by_key(
        channel_key, effective_memory, state.SEARCH_REFERENCES_MODE,
        companion_name=companion_name,
    )

    main_block = build_llm_main_prompt(ctx.db, channel_key, memory_enabled=ctx.effective_memory, anchors_enabled=ctx.effective_anchors, social_mode=ctx.social_mode)
    history = build_trimmed_history_for_payload(
        ctx.db, channel_key, main_block, context_limit=ctx.current_context_limit,
        channel_key=channel_key, model=ctx.current_text_model
    )
    full_messages = compose_full_messages(main_block, history)

    max_words = getattr(state, "RANDOM_THOUGHT_MAX_WORDS", 200)
    instruction = (
        f"Without acknowledging this instruction, seamlessly continue your previous "
        f"response by briefly introducing a random thought, stroke of genius, or "
        f"moment of inspiration that just crossed your mind. Keep it under {max_words} "
        f"words. Do not announce that you are doing this — just weave it in naturally."
    )

    now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
    day_name = now.strftime("%A")
    time_str = now.strftime("%I:%M %p").lstrip("0")
    date_str = now.strftime("%B %d, %Y")

    full_messages.append({
        "role": "user",
        "content": f"{time_str} {day_name} {date_str}: {instruction}",
    })

    # Persist the injected instruction as a system-authored user turn BEFORE
    # calling the LLM, so the history log shows the instruction preceding the
    # assistant's spontaneous continuation (correct chronological order).
    save_message(ctx.db, channel_key, "user", instruction, "system")

    try:
        response_text, _ = await get_ai_response(
            full_messages, model=ctx.current_text_model, reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=channel_key,
            provider_lock=ctx.current_text_provider_lock,
        )
    except Exception as e:
        print(f"⚠️ random_thought: LLM call failed: {e}")
        _disable_random_thought_counter(ctx.db)
        return

    async def channel_image_handler(prompt, reference_images=None):
        return await get_image_response(prompt, model=ctx.current_image_model, reference_images=reference_images, provider_lock=ctx.current_image_provider_lock)

    display_text, _, _, _, _, _ = await process_response(
        response_text, target_channel,
        image_handler=channel_image_handler, db=ctx.db,
        active_companion=ctx.active_companion,
        send_func=safe_send,
        channel_name=channel_key,
    )

    if not _is_error_response(response_text):
        save_message(ctx.db, channel_key, "assistant", response_text)
    else:
        print(f"⚠️ [random_thought] Skipping DB save of error response: {response_text[:120]}")

    if display_text:
        await safe_send_chunked(target_channel, display_text)

    # Per spec: do NOT reset to 0 here. Disarm to -1 until the next user-driven
    # LLM response re-arms the counter.
    _disable_random_thought_counter(ctx.db)
    print(f"💡 random_thought: completed in '{channel_key}', counter disarmed (-1)")


# ============================================
# LIVE VOICE UTTERANCE HANDLER
# ============================================
async def process_live_voice_utterance(wav_bytes, member, channel_name, text_channel):
    session = state.LIVE_VOICE_SESSION
    if session is None or not session.is_active():
        return

    ctx = await resolve_channel_context_by_key(
        channel_name, state.MEMORY_ENABLED, state.SEARCH_REFERENCES_MODE,
    )
    reset_idle_counters_for_companion(ctx.active_companion, source="live_voice")

    # 1. Transcribe.
    try:
        transcript = await speech_to_text(wav_bytes, filename="livevoice.wav")
    except Exception as e:
        print(f"⚠️ [livevoice] STT failed: {e}")
        return
    transcript = (transcript or "").strip()
    if not transcript:
        return

    print(f"🎙️ [livevoice] {member.display_name}: {transcript}")
    try:
        await text_channel.send(f"🎙️ **{member.display_name}**: {transcript}")
    except Exception:
        pass

    # 2. Persist user turn.
    save_message(ctx.db, channel_name, "user", transcript)

    # 4. Build prompt and call the voice model.
    main_block = build_llm_main_prompt(ctx.db, channel_name, memory_enabled=ctx.effective_memory, anchors_enabled=ctx.effective_anchors, social_mode=ctx.social_mode)
    history = build_trimmed_history_for_payload(
        ctx.db, channel_name, main_block, context_limit=ctx.current_context_limit,
        channel_key=channel_name, model=ctx.current_voice_model
    )
    full_messages = compose_full_messages(main_block, history)
    try:
        response_text, _ = await get_ai_response(
            full_messages,
            model=ctx.current_voice_model,
            reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature,
            top_k=ctx.current_top_k,
            channel_key=channel_name,
            provider_lock=ctx.current_voice_text_provider_lock,
        )
    except Exception as e:
        print(f"⚠️ [livevoice] LLM call failed: {e}")
        try:
            await text_channel.send("*🎙️ (live voice) LLM error — try again.*")
        except Exception:
            pass
        return
    if not response_text:
        return

    # 5. Persist assistant turn and post to the text channel.
    if not _is_error_response(response_text):
        save_message(ctx.db, channel_name, "assistant", response_text)
    else:
        print(f"⚠️ [livevoice] Skipping DB save of error response: {response_text[:120]}")
    state.LAST_INTERACTION_CHANNEL = channel_name

    try:
        await safe_send_chunked(text_channel, response_text)
    except Exception as e:
        print(f"⚠️ [livevoice] failed to post text: {e}")

    # 6. TTS + playback through the live session's voice client.
    try:
        audio_bytes = await text_to_speech(response_text, voice_id=ctx.current_voice_id)
        if audio_bytes:
            await session.play_audio_bytes(audio_bytes)
    except Exception as e:
        print(f"⚠️ [livevoice] TTS/playback failed: {e}")

    # Random Thoughts: re-arm after a user-driven live-voice response.
    if getattr(state, "RANDOM_THOUGHTS_ENABLED", False):
        try:
            _reset_random_thought_counter(ctx.db)
        except Exception as _e:
            print(f"⚠️ random-thought reset (livevoice) failed: {_e}")


# ============================================
# BACKGROUND TASKS
# ============================================

@tasks.loop(minutes=30)
async def auto_discover_paths_task():
    # Re-scan the companion_datafiles/* auto-discovery directories every 30
    # minutes so newly-added files are picked up without a bot restart.
    try:
        if state.SEARCH_REFERENCES_MODE == 2:
            try:
                from . import vectors
                active_comp = getattr(vectors, "_active_companion_name", None) or "default"
                paths = get_companion_paths(active_comp)
                ref_files = _scan_dir_for_files(paths["SEARCH_REFERENCE_DIR"])
                vectors.init_vector_store(ref_files, chunk_size=state.SEARCH_REFERENCES_CHUNK_SIZE, companion_name=active_comp)
            except Exception as e:
                print(f"⚠️ vector store re-sync failed: {e}")
    except Exception as e:
        print(f"⚠️ auto_discover_paths failed: {e}")
        traceback.print_exc()


@tasks.loop(time=dt_time(hour=0, minute=5))
async def prune_orphan_channels_task():
    try:
        live_names = set()
        for guild in _client.guilds:
            g = guild.name.lower()
            for ch in guild.text_channels:
                live_names.add(build_channel_key(g, ch.name))
            for th in guild.threads:
                live_names.add(build_channel_key(g, th.name))

        databases_dir = Path("databases")
        if not databases_dir.is_dir():
            return

        companions = []
        for p in databases_dir.iterdir():
            if p.is_dir() and p.name != "registry":
                companions.append(p.name.lower())

        if not companions:
            companions = ["default"]

        for comp in companions:
            comp_db = get_db(comp)
            cursor = comp_db.cursor()
            for table in ("messages", "channel_settings", "anchored_memories"):
                cursor.execute(f"SELECT DISTINCT channel FROM {table} WHERE channel LIKE 'dm:::%' OR channel = '_global'")
                for row in cursor.fetchall():
                    live_names.add(row[0])

        if not live_names:
            print("🧹 prune_orphan_channels: no live channels found (skipping to avoid wiping DB)")
            return

        total_msg = 0
        total_settings = 0
        total_anchors = 0
        for comp in companions:
            comp_db = get_db(comp)
            msg_removed, settings_removed, anchors_removed = prune_orphan_channels(comp_db, live_names)
            total_msg += msg_removed
            total_settings += settings_removed
            total_anchors += anchors_removed

        print(
            f"🧹 prune_orphan_channels: removed {total_msg} messages, "
            f"{total_settings} channel settings, {total_anchors} anchored memories "
            f"across {len(companions)} companion database(s)"
        )
    except Exception as e:
        print(f"⚠️ prune_orphan_channels failed: {e}")


@tasks.loop(minutes=1)
async def idle_action_tick_task():
    try:
        companions = discover_companions()
        now_local = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)

        for companion_name in companions:
            db = get_db(companion_name)

            channel_key = get_channel_setting(db, "_global", "default_channel")
            if not channel_key:
                # No default channel configured: counters are not incremented and
                # nothing can fire. This is a config state, not a non-fire at a
                # threshold, so we stay silent.
                continue

            _increment_idle_counters(db)
            counters = _get_idle_counters(db)

            # Random Thoughts: increment per-minute counter only while armed (> -1).
            rt_mins = _get_random_thought_counter(db)
            if rt_mins > -1:
                rt_mins += 1
                set_global_var(db, _RANDOM_THOUGHT_COUNTER_KEY, str(rt_mins))

            idle_mins = counters["idle_action_minutes_since_idle"]
            dream_mins = counters["dream_state_minutes_since_idle"]
            note_mins = counters["idle_note_minutes_since_idle"]

            # Health check: warn if a counter stops progressing unexpectedly.
            _check_stuck_counter(companion_name, channel_key, counters)

            # Registry ownership check. Only worth reporting once a trigger point
            # has actually been reached (per the "log non-fires only at the
            # trigger point" policy).
            owner = get_channel_companion(channel_key)
            if owner != companion_name:
                dream_threshold = config.DREAM_STATE_TRIGGER_MINUTES
                action_threshold = config.IDLE_ACTION_TRIGGER_MINUTES
                if ((dream_threshold > 0 and dream_mins >= dream_threshold)
                        or (action_threshold > 0 and idle_mins >= action_threshold)):
                    _update_block_state(
                        companion_name, f"registry_mismatch:{owner}",
                        f"⚠️ idle_action_tick: companion '{companion_name}' reached a "
                        f"trigger threshold (idle={idle_mins}, dream={dream_mins}) but its "
                        f"default_channel '{channel_key}' is owned by '{owner}' in the "
                        f"registry — skipping (no action will fire here).")
                continue

            triggered = False
            block_key = None
            block_msg = None

            # 1. Check Dream State Trigger
            dream_threshold = config.DREAM_STATE_TRIGGER_MINUTES
            if dream_threshold > 0 and dream_mins >= dream_threshold:
                if _is_hour_in_window(now_local.hour,
                        config.DREAM_STATE_TRIGGER_ALLOW_HOUR_START,
                        config.DREAM_STATE_TRIGGER_ALLOW_HOUR_STOP):
                    print(f"😴 idle_action_tick: triggering dream state for companion "
                          f"'{companion_name}' in '{channel_key}' "
                          f"after {dream_mins} idle minute(s)")
                    reset_idle_counters_for_companion(companion_name, source="dream_trigger")
                    from . import dream
                    dream_prompt = dream.get_dream_prompt()
                    await _run_idle_prompt(channel_key, dream_prompt, "dream_tick",
                                           companion_name=companion_name)
                    triggered = True
                else:
                    block_key = "dream_window"
                    block_msg = (
                        f"😴 idle_action_tick: dream READY for '{companion_name}' in "
                        f"'{channel_key}' (dream_mins={dream_mins} ≥ {dream_threshold}) "
                        f"but local hour {now_local.hour} is outside the allowed window "
                        f"{config.DREAM_STATE_TRIGGER_ALLOW_HOUR_START}-"
                        f"{config.DREAM_STATE_TRIGGER_ALLOW_HOUR_STOP} — waiting.")

            # 2. Check Idle Action Trigger
            if not triggered:
                action_threshold = config.IDLE_ACTION_TRIGGER_MINUTES
                if action_threshold > 0 and idle_mins >= action_threshold:
                    if _is_hour_in_window(now_local.hour,
                            config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_START,
                            config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP):
                        print(f"⏳ idle_action_tick: triggering idle action for companion "
                              f"'{companion_name}' in '{channel_key}' "
                              f"after {idle_mins} idle minute(s)")
                        reset_idle_counters_for_companion(companion_name, source="idle_action_trigger")
                        await _run_idle_prompt(
                            channel_key, config.IDLE_ACTION_TRIGGER_PROMPT,
                            "idle_action_tick", companion_name=companion_name)
                        triggered = True
                    elif block_key is None:
                        block_key = "idle_window"
                        block_msg = (
                            f"⏳ idle_action_tick: idle action READY for '{companion_name}' "
                            f"in '{channel_key}' (idle_mins={idle_mins} ≥ "
                            f"{action_threshold}) but local hour {now_local.hour} is "
                            f"outside the allowed window "
                            f"{config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_START}-"
                            f"{config.IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP} — waiting.")

            # 3. Check Idle Note Trigger
            if not triggered:
                note_threshold = state.IDLE_NOTE_TRIGGER_MIN_MINUTES
                if note_threshold > 0 and note_mins >= note_threshold:
                    if _is_hour_in_window(now_local.hour,
                            state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_START,
                            state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP):
                        # Eligible and in-window. We roll once per minute; misses are
                        # normal probabilistic non-fires and are intentionally not
                        # logged. Record an "eligible" state so the next state change
                        # is meaningful, but don't treat it as a block.
                        if block_key is None:
                            block_key = "note_rolling"
                            block_msg = None
                        if random.randint(1, 30) == 7:
                            print(f"💌 idle_note_tick: triggering idle note for companion "
                                  f"'{companion_name}' in '{channel_key}' "
                                  f"after {note_mins} idle minute(s)")
                            reset_idle_counters_for_companion(companion_name, source="note_trigger")
                            await _run_idle_prompt(
                                channel_key, state.IDLE_NOTE_TRIGGER_PROMPT,
                                "idle_note_tick", companion_name=companion_name)
                            triggered = True
                    elif block_key is None:
                        block_key = "note_window"
                        block_msg = (
                            f"💌 idle_note_tick: idle note READY for '{companion_name}' "
                            f"in '{channel_key}' (note_mins={note_mins} ≥ "
                            f"{note_threshold}) but local hour {now_local.hour} is "
                            f"outside the allowed window "
                            f"{state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_START}-"
                            f"{state.IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP} — waiting.")

            # 4. Check Random Thought Trigger
            # After a user-driven LLM response the counter is armed at 0 and
            # increments each minute. When it enters the trigger window, exactly
            # one roll is made (one-shot). Hit or miss, the counter is disarmed
            # to -1 until the next user-driven response.
            if not triggered and getattr(state, "RANDOM_THOUGHTS_ENABLED", False):
                rt_min = getattr(state, "RANDOM_THOUGHT_TRIGGER_MIN_MINUTES", 2)
                rt_max = getattr(state, "RANDOM_THOUGHT_TRIGGER_MAX_MINUTES", 5)
                if (rt_min <= rt_mins <= rt_max
                        and channel_key == state.LAST_INTERACTION_CHANNEL):
                    target_ch = await _resolve_channel_by_key(channel_key)
                    _now = time.time()
                    busy = (
                        target_ch is None
                        or channel_key in state.ON_MESSAGE_IN_FLIGHT
                        or state.LIVE_VOICE_SESSION is not None
                        or state._is_anyone_typing(target_ch.id, _now)
                    )
                    if busy:
                        print(f"💡 random_thought_tick: channel '{channel_key}' busy — "
                              f"disarming (rt_mins={rt_mins}) until next response")
                        _disable_random_thought_counter(db)
                    else:
                        max_roll = getattr(state, "RANDOM_THOUGHT_MAX_ROLL", 6)
                        roll = random.randint(1, max_roll)
                        target = round(max_roll / 2)
                        if roll == target:
                            print(f"💡 random_thought_tick: HIT (roll {roll}/{max_roll}, "
                                  f"target {target}) — firing random thought in "
                                  f"'{channel_key}' (rt_mins={rt_mins})")
                            _disable_random_thought_counter(db)
                            await _run_random_thought_prompt(
                                channel_key, companion_name=companion_name)
                            triggered = True
                        else:
                            print(f"💡 random_thought_tick: MISS (roll {roll}/{max_roll}, "
                                  f"target {target}) in '{channel_key}' "
                                  f"(rt_mins={rt_mins}) — disarming until next response")
                            _disable_random_thought_counter(db)

            # Emit / clear the "reached threshold but did not fire" log (once per
            # state change). When an idle/dream/note action actually fired, the
            # reset already cleared the block state.
            if not triggered:
                _update_block_state(companion_name, block_key, block_msg)

    except Exception as e:
        print(f"⚠️ idle_action_tick failed: {e}")
        traceback.print_exc()


_HEARTBEAT_URL = "https://rn33waitvlv2yz4nh37k2prvpu0mgaxq.lambda-url.us-east-1.on.aws/heartbeat"


@tasks.loop(hours=24)
async def heartbeat_task():
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            ip = ""
            try:
                async with session.get("https://api.ipify.org?format=json") as resp:
                    if resp.status == 200:
                        ip = (await resp.json()).get("ip", "")
            except Exception:
                pass
            try:
                await session.post(_HEARTBEAT_URL, json={"ip": ip})
            except Exception:
                pass
    except Exception:
        pass


@tasks.loop(minutes=1)
async def bot_cooldown_tick_task():
    try:
        for ch in list(state._bot_cooldown_by_channel.keys()):
            v = state._bot_cooldown_by_channel[ch]
            if v <= 2:
                del state._bot_cooldown_by_channel[ch]
            else:
                state._bot_cooldown_by_channel[ch] = v - 2

        # Decay bot-to-bot counters by 1/min so a paused chain doesn't
        # inherit a stale high count when it resumes.
        for ch in list(state._bot_to_bot_counter_by_channel.keys()):
            v = state._bot_to_bot_counter_by_channel[ch]
            if v <= 1:
                del state._bot_to_bot_counter_by_channel[ch]
            else:
                state._bot_to_bot_counter_by_channel[ch] = v - 1
    except Exception as e:
        print(f"⚠️ bot_cooldown_tick failed: {e}")
