# ============================================
# Alcove — main.py
# Core bot logic and message handling
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================ 
if __package__ is None or __package__ == "":
    import sys
    print("Error: main.py must be run as a package module, not as a script.")
    print("Use: python -m modules.main")
    print("Or launch via: python modules/alcove.py")
    sys.exit(1)

import os
import sys
import re

if os.name == "nt":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import discord
import aiohttp
import asyncio
import io
import random
import traceback
import time
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import provider
from . import idle
from . import cron
from . import state
from .commands import handle_command
from .companions import load_file_content
from .llm_prompt_builder import (
    build_llm_main_prompt, build_trimmed_history_for_payload,
    compose_full_messages,
    retrim_history_for_tool_round,
    get_ai_response,
    get_image_response,
    get_video_response,
    SystemBlockOversizedError,
)
from .utils import safe_send, safe_send_chunked, estimate_tokens, msg_tokens, strip_leading_bot_mention, build_channel_key
from .database import (
    save_message, get_message_count,
    get_channel_setting, set_channel_setting, clear_channel_setting,
    list_channel_settings_by_prefix,
    get_db, get_channel_companion, touch_channel,
)
from .context import ChannelContext, resolve_channel_context, resolve_channel_context_by_key, check_gating
from .attachments import resolve_attachment_source, process_attachments, build_combined_content, build_multimodal_user_content, url_to_data_uri
from .search import inject_search_context
from . import voice_live_viz
from .llm_loop import run_llm_loop
from .directives import process_response
from .history import save_history, flush_pending_saves
from .voice_tts_handler import handle_tts
from .voice import text_to_speech
import config

# Load opus for pycord voice support
if not discord.opus.is_loaded():
    import sys
    if sys.platform == "darwin":
        # Homebrew: Apple Silicon vs Intel
        for path in ["/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"]:
            if os.path.isfile(path):
                discord.opus.load_opus(path)
                break
    elif sys.platform == "win32":
        import ctypes.util
        # Check winlib subdirectory for opus DLL
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        winlib_dirs = [
            os.path.join(project_root, "utils", "winlib"),
            os.path.join(project_root, "winlib"),
        ]
        local_opus = None
        for name in ["opus.dll", "libopus.dll", "libopus-0.dll"]:
            for winlib_dir in winlib_dirs:
                candidate = os.path.join(winlib_dir, name)
                if os.path.isfile(candidate):
                    local_opus = candidate
                    break
            if local_opus:
                break
        if local_opus:
            discord.opus.load_opus(local_opus)
        else:
            opus_path = ctypes.util.find_library("opus")
            if opus_path:
                discord.opus.load_opus(opus_path)
            else:
                print("Warning: libopus not found. Voice features will not work.")
                print("Download opus.dll and place it in the same folder as main.py.")
    else:
        discord.opus.load_opus("libopus.so.0")

# --- MUTABLE GLOBALS (moved to state.py) ---
# MEMORY_ENABLED, LIVE_JOURNAL_PROMPT, DYNAMIC_CONTEXT_FILE_LOCATIONS,
# SEARCH_REFERENCES_MODE, SEARCH_REFERENCES_CHUNK_SIZE,
# IDLE_NOTE_TRIGGER_*, LIVEVOICE_*, _reference_images_by_channel,
# _bot_cooldown_by_channel, SOCIAL_MODE_COOLDOWN_MAX — all live in state.py now.

# Re-export of the live-voice video image snapshot cap (source of truth is
# state.py so other modules can read it without importing main). Referenced
# at the snapshot injection site below via state.LIVEVOICE_VIZ_SNAPSHOTS_MAX.
from .state import LIVEVOICE_VIZ_SNAPSHOTS_MAX

# ============================================
# DISCORD BOT
# ============================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
db = get_db("default")

# Throttle consecutive <react> directives, scoped per channel.
_consecutive_reacts_by_channel = defaultdict(int)

# Arrival high-water mark: channel_name -> (seq, ts). Stamped synchronously
# (no awaits between read and write) at on_message time — before the
# response-delay sleep — by every gate-passing, non-command message
# (image/voice/bot/regen included; gated-out messages and pure !commands
# never stamp). The post-pause decision block compares against this to see
# newer in-flight messages, which the pending-turn flag alone can never
# observe (a newer handler still inside its random pause hasn't set any
# flag yet). main.py-local: no other module reads or writes it.
_arrival_hwm_by_channel = {}

# ============================================
# ENGINE GLOBALS (not user-configurable)
# Internal engine constants that may be promoted to config.py later.
# ============================================

# Embedding model used by the ChromaDB vector store. Changing this value
# triggers an automatic full re-embed of all search reference files on next
# boot (the existing collection is wiped and rebuilt). Switched from
# onnx-miniLM-L6-v2 (256-token max sequence, English-biased, 384-dim) to
# bge-m3-onnx (8192-token max sequence, 100+ languages incl. CJK, 1024-dim)
# so chunks of ~2000 chars are no longer silently truncated at embedding
# time. The model is downloaded by utils/install_deps.py to
# ~/.cache/chroma/onnx_models/bge-m3/.
SEARCH_REFERENCES_EMBEDDING_MODEL = "bge-m3-onnx"

# Context-limit cache tuning. _resolve_context_limit (context.py) persists
# the per-channel computed context limit so idle/voice/non-message paths can
# reuse it without a network lookup. This constant governs that cache:
#   - TTL: how long a persisted row may live before a fresh provider lookup
#     is forced (catches provider-side context-window changes).
# When a persisted row is missing/stale and the caller opts out of network
# lookups (auto_context=False, i.e. idle/voice ticks), the resolver returns
# MAX_CONTEXT_TOKENS — the same value the interactive path falls back to
# when a provider lookup fails. If MAX exceeds the model's true context
# window, the downstream API 400 is caught and surfaced (console + Discord)
# by the error-handling layer in idle.py / llm_loop.py / provider.py.
CONTEXT_LIMIT_REFRESH_TTL_SECONDS = 7 * 24 * 3600  # re-lookup after 7 days

# ============================================
# TEST / EXPERIMENTAL GLOBALS
# These will eventually be promoted to config.py once finalized.
# ============================================

MIN_BOT_RESPONSE_SECONDS = 300
MAX_BOT_RESPONSE_SECONDS = 600

GROUPCHAT_TURN_DELAY_MIN = 1
GROUPCHAT_TURN_DELAY_MAX = 4

# Track who is typing in which channel: channel_id -> {user_id: timestamp}.
# Moved to state.py as state._active_typers so idle.py can read it without a
# circular import.

# Interjected replies: when DIRECT_REPLIES_ONLY is on, the bot may still
# randomly respond to non-addressed messages. 1-in-INTERJECTED_REPLIES_ODDS
# chance per qualifying message (uses round(ODDS/2) as the target).
INTERJECTED_REPLIES = True
INTERJECTED_REPLIES_ODDS = 5 

# Social-mode mentions: chance the bot prepends a real Discord @mention
# of the author it's replying to, so they get an actual ping (the native
# reply reference links but doesn't notify). Applies to all interactive
# social replies — direct mentions, replies-to-bot, and interjections.
# 1-in-SOCIAL_MODE_MENTION_ODDS chance per reply (uses round(ODDS/2) as
# the target).
SOCIAL_MODE_MENTION_ODDS = 4

# ============================================
# RANDOM THOUGHTS (experimental)
# After a user-driven LLM response, an idle timer (random_thought_minutes_since_response)
# is reset to 0. Every minute it increments by +1 while > -1. When it enters the
# trigger window (RANDOM_THOUGHT_TRIGGER_MIN_MINUTES..MAX_MINUTES), exactly one roll
# of random.randint(1, RANDOM_THOUGHT_MAX_ROLL) is made; success is
# round(RANDOM_THOUGHT_MAX_ROLL / 2). On a miss (or if the channel is busy with an
# in-flight on_message, a live voice session, or active typing), the timer is set to
# -1 and re-armed only on the next user-driven LLM response. On a hit, the bot is
# prompted to seamlessly continue its last response with a brief (<RANDOM_THOUGHT_MAX_WORDS
# word) random thought / stroke of genius / moment of inspiration, after which the
# timer is also set to -1 (NOT reset to 0).
# Defaults are mirrored into state.py (state.RANDOM_THOUGHTS_*) so idle.py can read
# them without a circular import.
# ============================================
RANDOM_THOUGHTS_ENABLED = True
RANDOM_THOUGHT_MAX_ROLL = 6
RANDOM_THOUGHT_MAX_WORDS = 200
RANDOM_THOUGHT_TRIGGER_MIN_MINUTES = 2
RANDOM_THOUGHT_TRIGGER_MAX_MINUTES = 5

state.RANDOM_THOUGHTS_ENABLED = RANDOM_THOUGHTS_ENABLED
state.RANDOM_THOUGHT_MAX_ROLL = RANDOM_THOUGHT_MAX_ROLL
state.RANDOM_THOUGHT_MAX_WORDS = RANDOM_THOUGHT_MAX_WORDS
state.RANDOM_THOUGHT_TRIGGER_MIN_MINUTES = RANDOM_THOUGHT_TRIGGER_MIN_MINUTES
state.RANDOM_THOUGHT_TRIGGER_MAX_MINUTES = RANDOM_THOUGHT_TRIGGER_MAX_MINUTES

# ============================================
# TYPING-DEFER / BURST-CONSOLIDATION (experimental)
# Discord has no typing-stopped event — typing entries just age out after
# 10s (state._is_anyone_typing) — and rapid paste-bursts emit no
# TYPING_START events at all, so mid-burst messages can be falsely
# processed (or, on a stale indicator, falsely dropped). Post-pause, every
# text-only turn first checks the arrival high-water mark
# (_arrival_hwm_by_channel): a newer message means this turn is superseded
# — the newer handler's inference sees it in its history load. Survivors
# defer when someone is typing OR the previous message arrived within
# TYPING_DEFER_BURST_SECONDS (ongoing burst), then re-check after
# TYPING_DEFER_RECHECK_SECONDS (typing cleared AND still newest AND not
# consumed by another inference) and process if clear — consolidating a
# burst into one reply.
# ============================================
TYPING_DEFER_BURST_SECONDS = 2
TYPING_DEFER_RECHECK_SECONDS = 4

# ============================================
# MAIN GLOBALS
# These will eventually be promoted to config.py once finalized.
# (Moved to state.py: SEARCH_REFERENCES_MODE, SEARCH_REFERENCES_CHUNK_SIZE,
#  IDLE_NOTE_TRIGGER_*, LIVEVOICE_*)
# ============================================

# Vector store boot init is fired from on_ready (below) on a daemon thread
# so the bot connects to Discord first and stays responsive to prompts while
# the (potentially long) embedding run proceeds. The heavy ONNX model load +
# per-chunk inference runs in a SUBPROCESS (own GIL) inside
# vectors.init_vector_store, so the main process's asyncio event loop is
# never blocked. search.inject_search_context checks state.SEARCH_INITIALIZING
# and skips the search step until the build completes.


# ============================================
# SKILLS CONTEXT LOADER
# Scans the project-root skills/ directory for skill.md (or SKILL.MD) files,
# extracts the leading YAML front-matter block (delimited by --- lines),
# appends each file's absolute path, and concatenates all of them into one
# string stored in state.SKILLS_CONTEXT. This is read by llm_prompt_builder.py at
# build_llm_main_prompt time and injected into the system block right after
# the Tool Definitions block. Re-invoked periodically by idle's
# auto_discover_paths_task so newly added/edited skills are picked up
# without a bot restart.
# ============================================
_SKILLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep + "skills"


def _load_skills_context(warn_on_malformed=True):
    """(Re)scan skills/ for skill.md files and populate state.SKILLS_CONTEXT.

    Args:
        warn_on_malformed: When True (boot call), print a one-line warning for
            each skill.md that is missing valid YAML front matter. When False
            (periodic refresh call), skip such files silently.
    """
    import pathlib

    skills_root = pathlib.Path(_SKILLS_DIR)
    if not skills_root.is_dir():
        state.SKILLS_CONTEXT = ""
        return

    # Recursively find skill.md / SKILL.MD (case-insensitive basename match).
    skill_files = sorted(
        p for p in skills_root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.name.lower() == "skill.md"
    )

    parts = []
    for path in skill_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            if warn_on_malformed:
                print(f"⚠️ Could not read skill file (skipping): {path} — {e}")
            continue

        # Extract leading YAML front matter: file must start with a line that
        # is exactly "---" (trailing whitespace tolerated), and there must be a
        # subsequent line that is exactly "---" closing the block.
        lines = text.splitlines()
        yaml_body = None
        if lines and lines[0].rstrip() == "---":
            for j in range(1, len(lines)):
                if lines[j].rstrip() == "---":
                    yaml_body = "\n".join(lines[1:j])
                    break

        if yaml_body is None:
            if warn_on_malformed:
                print(f"⚠️ Skipping skill file (no YAML front matter): {path}")
            continue

        abs_path = str(path.resolve())
        i = len(parts) + 1
        entry = f"\n\n## Skill {i}\n{yaml_body.strip()}\nFile: {abs_path}\n"
        parts.append(entry)

    if not parts:
        state.SKILLS_CONTEXT = ""
        return

    skills_context = (
        "\n\n\n"
        "# START OF SKILL DEFINITIONS\n"
        "- The following are installed skills. Each entry shows the skill's YAML description "
        "followed by the absolute path to its full instruction file.\n"
        "- When a user request matches a skill's description, load that skill's full "
        "instructions using the <readSkill> tool with the file's path in the body:\n"
        "    <readSkill>\n"
        "    /absolute/path/to/skill.md\n"
        "    </readSkill>\n"
        "  Then follow the instructions in the loaded file to fulfill the user's request.\n"
        "- Skills are not available in autonomous contexts (idle/dream/note).\n"
        + "".join(parts)
        + "\n# END OF SKILL DEFINITIONS\n"
    )
    state.SKILLS_CONTEXT = skills_context


# Load skills at boot (warn on malformed; subsequent refresh calls won't warn)
_load_skills_context(warn_on_malformed=True)


@client.event
async def on_ready():
    print(f"✨ {client.user} is online!")
    print(f"📡 Provider: {provider.provider_name()} | Default model: {config.CURRENT_TEXT_MODEL}")
    print(f"🧠 {get_message_count(db)} messages in memory")

    # Provide Discord client and default DB to the idle module
    idle.init(client, db)

    # Kick off the vector store build in the background (daemon thread).
    # The actual ONNX embedding work runs in a subprocess (own GIL), so
    # this thread mostly just waits on subprocess.run — light work that
    # won't block the event loop. SEARCH_INITIALIZING gates search until done.
    import threading as _threading
    # Sentinel lives on on_ready itself — on_ready re-fires after every
    # gateway reconnect, and a flag set on a nested function object was
    # recreated (and reset) on each firing, respawning the init thread.
    if not getattr(on_ready, "_vector_init_started", False):
        on_ready._vector_init_started = True
        def _boot_vector_init():
            idle.init_vector_store(db, state.SEARCH_REFERENCES_MODE, state.SEARCH_REFERENCES_CHUNK_SIZE)
        _threading.Thread(target=_boot_vector_init, daemon=True, name="vector-init-boot").start()

    if not idle.prune_orphan_channels_task.is_running():
        idle.prune_orphan_channels_task.start()
        print("🧹 Daily channel prune task scheduled for 00:05")
    if not idle.idle_action_tick_task.is_running():
        idle.idle_action_tick_task.start()
        print(f"⏳ Idle action tick task started (idle: {config.IDLE_ACTION_TRIGGER_MINUTES} min, dream: {config.DREAM_STATE_TRIGGER_MINUTES} min)")
    if not idle.auto_discover_paths_task.is_running():
        idle.auto_discover_paths_task.start()
        print("🔎 Auto-discovery task started (every 30 min)")
    if not idle.heartbeat_task.is_running():
        idle.heartbeat_task.start()

    if not idle.bot_cooldown_tick_task.is_running():
        idle.bot_cooldown_tick_task.start()
        print("🤖 Bot cooldown tick task started (every 1 min)")

    if not cron.cron_tick_task.is_running():
        cron.cron_tick_task.start()
        print("⏰ Cron tick task started (every 1 min)")

    if not idle.cleanup_old_videos_task.is_running():
        idle.cleanup_old_videos_task.start()
        print(f"🧹 Video cleanup task started (retention: {getattr(config, 'VIDEO_RETENTION_DAYS', 7)} days)")

    print()
    print("=" * 52)
    print("   Alcove is ready and listening for messages!")
    print("=" * 52)
    print()

@client.event
async def on_disconnect():
    print(f"🔌 Discord gateway disconnected at {datetime.now():%H:%M:%S}")

@client.event
async def on_resumed():
    print(f"🔌 Discord gateway reconnected at {datetime.now():%H:%M:%S}")

@client.event
async def on_typing(channel, user, when):
    if user == client.user:
        return
    if channel.id not in state._active_typers:
        state._active_typers[channel.id] = {}
    state._active_typers[channel.id][user.id] = when.timestamp()

@client.event
async def on_message(message):
    print(f"[{datetime.now():%H:%M:%S.%f}]")
    if message.author == client.user:
        return

    if message.channel.id in state._active_typers:
        state._active_typers[message.channel.id].pop(message.author.id, None)

    # Activity heartbeat: mark this channel as alive so the daily orphan
    # prune can tell "quiet" from "deleted". Deliberately placed before
    # context resolution and gating — commands, gated-out messages, and
    # other-bot chatter all count as activity. DM keys are skipped (the
    # prune task protects dm:::% keys on its own). Never allowed to break
    # message dispatch.
    if message.guild is not None:
        try:
            _hb_key = build_channel_key(message.guild.name.lower(), str(message.channel))
            touch_channel(get_db(get_channel_companion(_hb_key)), _hb_key)
        except Exception as _hb_err:
            print(f"⚠️ activity heartbeat failed for {message.channel}: {_hb_err}")

    ctx = await resolve_channel_context(message, config.AUTHORIZED_HUMAN_DISCORD_USERNAME, state.MEMORY_ENABLED, state.SEARCH_REFERENCES_MODE)
    if ctx is None:
        return

    # Detect whether this message arrived in the active live-voice session's
    # text channel. When true, the reply is delivered as a voice reply (TTS
    # + attached .mp3 + playback through the live session's voice client),
    # the response-delay sleep is skipped, and the voice model/provider lock
    # is used — mirroring a spoken live-voice utterance reply.
    _live = state.LIVE_VOICE_SESSION
    _is_live_channel = (_live is not None and _live.is_active()
                        and _live.text_channel is not None
                        and _live.text_channel.id == message.channel.id)

    try:
      # Registered inside the try so the finally-discard below can never be
      # skipped by an exception here — a leaked key permanently disarms
      # idle random thoughts for the channel.
      state.ON_MESSAGE_IN_FLIGHT.add(ctx.channel_name)
      # Strip a leading @mention of this bot so that mention-prefixed
      # commands (e.g. "@lani !help") dispatch like a bare "!help". The
      # gate (check_gating) performs the same stripping independently.
      content = strip_leading_bot_mention(message.content.strip(), client.user.id)
      cmd = content.lower()

      gate = await check_gating(message, ctx, client, config.AUTHORIZED_HUMAN_DISCORD_USERNAME,
                                interjected_replies=INTERJECTED_REPLIES,
                                interjected_replies_odds=INTERJECTED_REPLIES_ODDS)
      if not gate.allowed:
          if gate.save_context:
              try:
                  _now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
                  _ctx_ts = f"{_now.strftime('%I:%M %p').lstrip('0')} {_now.strftime('%A')} {_now.strftime('%B %d, %Y')}"
                  save_message(ctx.db, ctx.channel_name, "user",
                               f"{_ctx_ts}:{message.author.display_name}: {message.content.strip()}",
                               message.author.display_name)
              except Exception as _e:
                  print(f"⚠️ Failed to save dropped message for context: {_e}")
          return

      if ctx.social_mode:
          print(f"social_mode: {ctx.channel_name}, current exchanges: "
                f"{state._bot_cooldown_by_channel[ctx.channel_name]}/{state.SOCIAL_MODE_COOLDOWN_MAX}")

      try:
          dynamic_rows = list_channel_settings_by_prefix(ctx.db, ctx.channel_name, "dynamic_file_")
          for param, path in dynamic_rows:
              if not os.path.isfile(path):
                  await message.channel.send(
                      f"*⚠️ Error: Dynamically loaded file `{os.path.basename(path)}` ("
                      f"`{path}`) was not found on the filesystem and has been unloaded.*"
                  )
                  clear_channel_setting(ctx.db, ctx.channel_name, param)
                  cache = state.DYNAMIC_CONTEXT_FILE_LOCATIONS.get(ctx.channel_name)
                  if cache and path in cache:
                      cache.remove(path)
      except Exception as e:
          print(f"⚠️ Error checking channel dynamic files: {e}")

      _regen_mode = False
      _regen_save_user = True
      _carry_prior_attachments = False
      _reply_reference = None

      if cmd.startswith("!"):
          handled = await handle_command(
              message, cmd, content, ctx.channel_name, ctx.guild_name,
              ctx.db, ctx.effective_memory, ctx.effective_anchors,
              ctx.current_text_model,
              ctx.current_voice_model, ctx.current_image_model,
              ctx.current_video_model,
              ctx.current_voice_id, ctx.current_context_limit,
              ctx.current_reasoning_effort,
              ctx.current_temperature, ctx.current_top_k,
              build_llm_main_prompt=build_llm_main_prompt,
              build_trimmed_history_for_payload=build_trimmed_history_for_payload,
              compose_full_messages=compose_full_messages,
              estimate_tokens=estimate_tokens,
              msg_tokens=msg_tokens,
              load_file_content=load_file_content,
              get_image_response=get_image_response,
              get_video_response=get_video_response,
              auto_context_adjust=config.AUTO_CONTEXT_ADJUST,
              client=client,
          )
          if isinstance(handled, dict) and handled.get("action") == "regen":
              content = handled["prompt"]
              _regen_mode = True
              _regen_save_user = handled.get("save_user_prompt", True)
              _carry_prior_attachments = handled.get("carry_prior_attachments", False)
          elif isinstance(handled, dict) and handled.get("action") == "execute":
              content = handled["prompt"]
              _regen_mode = True
              _regen_save_user = True
          elif isinstance(handled, dict) and handled.get("action") == "reply_last":
              # !reply in social mode: respond to the existing last user turn
              # in history (already saved by gating) and post the response as
              # a Discord reply to the message above the command. Reuse the
              # regen machinery (no new user save, no new user turn appended).
              _regen_mode = True
              _regen_save_user = False
              _reply_reference = handled.get("reply_reference")
          elif handled:
              return

      pending_saves = []
      _tts_text = None
      _tts_voice_id = None
      llm_result = None

      # Arrival high-water mark: stamp synchronously (no awaits between the
      # read and the write) BEFORE the response-delay sleep, so any message
      # arriving during our pause is visible to our post-pause supersede
      # check — and vice versa. Every message that will run inference
      # stamps (regen/execute/reply_last fall through here too); gated
      # messages and pure !commands returned earlier and never stamp.
      _prior_hwm = _arrival_hwm_by_channel.get(ctx.channel_name)
      _my_arrival_seq = (_prior_hwm[0] if _prior_hwm is not None else 0) + 1
      _arrival_hwm_by_channel[ctx.channel_name] = (_my_arrival_seq, time.time())

      try:
        _is_voice_msg = getattr(message.flags, 'is_voice_message', False)

        if message.author.bot:
            # Exponential backoff for bot-to-bot exchanges in social mode:
            # delay grows 5s → 10s → 20s → 40s → 80s based on the current
            # bot-to-bot counter, so unattended loops wind down naturally
            # before hitting the hard stop (BOT_TO_BOT_MAX_EXCHANGES).
            # Outside social mode the counter stays 0, so the floor is 5s.
            _btb = state._bot_to_bot_counter_by_channel[ctx.channel_name]
            _base_delay = min(5 * (2 ** _btb), 80)
            await asyncio.sleep(random.uniform(_base_delay, _base_delay * 1.3))
        elif not _is_voice_msg and not _is_live_channel and config.MAX_RESPONSE_SECONDS > 0 and config.MAX_RESPONSE_SECONDS >= config.MIN_RESPONSE_SECONDS:
            await asyncio.sleep(random.uniform(config.MIN_RESPONSE_SECONDS, config.MAX_RESPONSE_SECONDS))

        has_image_or_voice = _is_voice_msg or any(
            a.content_type and (a.content_type.startswith("image/") or a.content_type.startswith("audio/"))
            for a in message.attachments
        )

        # Regen media anchor: the Discord message ID of the most recent
        # media-bearing user turn in this channel, written at save time.
        # resolve_attachment_source fetches this exact message when a regen
        # needs to re-attach media — never a history scan.
        _media_discord_msg_id = get_channel_setting(
            ctx.db, ctx.channel_name, "last_media_discord_msg_id", None)
        attachment_source, _carried_prior = await resolve_attachment_source(
            message, _carry_prior_attachments, _media_discord_msg_id)
        att_result = await process_attachments(
            attachment_source, message, carried_prior=_carried_prior)
        if att_result.should_return:
            return
        combined_content = build_combined_content(content, att_result.voice_transcription, att_result.text_attachment_content)

        # --- Accumulate-until-activation mode (live voice text/image path) ---
        # When LIVE_VOICE_SEND_PHRASE is set and this message arrived in the
        # active live-voice text channel, suppress inference until a message
        # contains the phrase. Non-trigger messages are saved to DB as normal
        # user turns and return early; trigger messages strip the phrase and
        # fall through to the normal inference path below. When the phrase
        # is unset, this block is skipped entirely (existing behavior).
        if _is_live_channel and state.LIVEVOICE_SEND_PHRASE:
            _phrase_re = re.compile(r"\b" + re.escape(state.LIVEVOICE_SEND_PHRASE) + r"\b", re.IGNORECASE)
            if _phrase_re.search(combined_content):
                # Trigger — strip the phrase and proceed to inference.
                combined_content = re.sub(r"\s+", " ", _phrase_re.sub("", combined_content)).strip()
                print(f"[livevoice] SEND PHRASE triggered (text/image) "
                      f"stripped='{combined_content}'")
                # Fall through to normal inference path below (with stripped
                # combined_content). If combined_content is now empty, the
                # guards at the save/append below skip the empty turn.
            else:
                # No trigger — accumulate as a normal user turn, no inference.
                if has_image_or_voice:
                    _tags = []
                    if att_result.image_attachments:
                        _tags.append("[image]")
                    if att_result.audio_attachments:
                        _tags.append("[audio]")
                    _save_txt = (f"{combined_content} {' '.join(_tags)}"
                                 if combined_content else " ".join(_tags))
                else:
                    _save_txt = combined_content
                save_message(ctx.db, ctx.channel_name, "user",
                             _save_txt, message.author.display_name)
                if has_image_or_voice and _tags:
                    set_channel_setting(ctx.db, ctx.channel_name,
                                        "last_media_discord_msg_id",
                                        str(attachment_source.id))
                # Eagerly fetch image data URIs so they're available as visual
                # content when the trigger fires. Without this, accumulated
                # images would exist only as [image] text tags in DB history
                # and the model would never see them. Capped FIFO per channel
                # to bound memory/context cost.
                if att_result.image_attachments:
                    try:
                        _fetched = await asyncio.gather(
                            *[url_to_data_uri(a.url) for a in att_result.image_attachments],
                            return_exceptions=True,
                        )
                        _good = [u for u in _fetched if isinstance(u, str)]
                        if _good:
                            _acc = state._accumulated_images_by_channel[ctx.channel_name]
                            _acc.extend(_good)
                            _cap = state.LIVEVOICE_ACCUMULATED_IMAGES_MAX
                            if len(_acc) > _cap:
                                del _acc[:len(_acc) - _cap]
                                print(f"[livevoice] accumulated images capped to "
                                      f"{_cap} (FIFO drop) for {ctx.channel_name}")
                    except Exception as e:
                        print(f"⚠️ [livevoice] failed to accumulate images: {e}")
                # Reset idle watchdog so accumulation counts as activity
                # (parity with spoken utterances and the inference path below).
                _live._last_interaction_ts = time.time()
                return

        # Tracks the row id of the current user turn when it is saved to the
        # DB before prompt assembly (text path). Pass to build_trimmed_history
        # so the history load excludes rows with id >= this one — i.e. the
        # current turn itself (avoiding a duplicate with the append below)
        # and any message that arrived concurrently afterwards (preserving
        # correct ordering: each turn sees the conversation as-of its arrival).
        _latest_user_msg_id = None

        # In social mode, surface an `@[you]` marker to the LLM (and to saved
        # DB history) when the gate determined this user turn was addressed to
        # us directly. Without this, strip_leading_bot_mention would erase the
        # leading <@BOT_ID> token from the text the model sees, and the model
        # could misclassify the turn as overheard context (per the
        # _SOCIAL_MODE_INDICATOR rule #5) and decline to respond — see the
        # "@bot how are you today" silent-drop bug.
        if ctx.social_mode and gate.addressed_to_bot and combined_content:
            _marked_content = f"@[you] {combined_content}"
        else:
            _marked_content = combined_content

        if not has_image_or_voice and combined_content:
            if not (_regen_mode and not _regen_save_user):
                if not (_regen_mode and (att_result.image_attachments or att_result.audio_attachments)):
                    _latest_user_msg_id = save_message(ctx.db, ctx.channel_name, "user", _marked_content, message.author.display_name)
            if not _regen_mode and not ctx.social_mode:
                # Unconditional post-pause supersede gate — checked no matter
                # what, typing or not. If any message stamped the arrival
                # HWM after ours, that message's inference will see this
                # turn in its history load (we saved above), so this handler
                # is permanently superseded.
                _cur_hwm = _arrival_hwm_by_channel.get(ctx.channel_name)
                if _cur_hwm is None or _cur_hwm[0] != _my_arrival_seq:
                    print(f"⏭️ [{ctx.channel_name}] Message from {message.author.display_name} "
                          f"dropped — superseded by a newer message")
                    return
                # Newest arrival: take ownership of the pending-turn flag.
                # The overwrite is deliberate — an older deferred handler
                # that owned the flag before we arrived aborts at its own
                # recheck via its HWM gate. Without the overwrite, both
                # would orphan the burst.
                state.NEEDS_INFERENCE[ctx.channel_name] = _latest_user_msg_id
                _typing_now = state._is_anyone_typing(message.channel.id, time.time())
                _burst = (_prior_hwm is not None
                          and (time.time() - _prior_hwm[1]) < TYPING_DEFER_BURST_SECONDS)
                if _typing_now or _burst:
                    _reason = "typing detected" if _typing_now else "channel burst"
                    print(f"⏳ [{ctx.channel_name}] {_reason} — deferring decision for "
                          f"{TYPING_DEFER_RECHECK_SECONDS}s (pending turn id={_latest_user_msg_id})")
                    await asyncio.sleep(TYPING_DEFER_RECHECK_SECONDS)
                    if state._is_anyone_typing(message.channel.id, time.time()):
                        print(f"⏭️ [{ctx.channel_name}] Message from {message.author.display_name} "
                              f"dropped — still typing after defer")
                        return
                    _cur_hwm = _arrival_hwm_by_channel.get(ctx.channel_name)
                    if _cur_hwm is None or _cur_hwm[0] != _my_arrival_seq:
                        print(f"⏭️ [{ctx.channel_name}] Message from {message.author.display_name} "
                              f"dropped — superseded by a newer message during defer")
                        return
                    if state.NEEDS_INFERENCE.get(ctx.channel_name) != _latest_user_msg_id:
                        print(f"⏭️ [{ctx.channel_name}] Message from {message.author.display_name} "
                              f"dropped — pending turn consumed by another inference")
                        return
                    print(f"✅ [{ctx.channel_name}] Conditions cleared after defer — processing pending message")

        # Commit point: consume the pending-turn flag synchronously adjacent
        # to the decision above (no awaits between). This handler's inference
        # reads channel history and therefore subsumes any unprocessed turn(s).
        state.NEEDS_INFERENCE.pop(ctx.channel_name, None)
        async with message.channel.typing():

          main_block, _ = build_llm_main_prompt(ctx.db, ctx.channel_name, memory_enabled=ctx.effective_memory, anchors_enabled=ctx.effective_anchors, social_mode=ctx.social_mode)
          history = build_trimmed_history_for_payload(
              ctx.db, ctx.channel_name, main_block, context_limit=ctx.current_context_limit,
              channel_key=ctx.channel_name, model=ctx.current_text_model,
              before_id=_latest_user_msg_id,
          )
          _history_end_idx = len(history)
          full_messages = compose_full_messages(main_block, history)

          # Inject accumulated images (accumulate-until-activation mode) as a
          # separate user message BEFORE the trigger turn, so the model sees
          # them in chronological order. Popped (consumed once) so subsequent
          # triggers don't re-send the same batch. No-op outside accumulate mode
          # or when no images were accumulated.
          _acc_imgs = state._accumulated_images_by_channel.pop(ctx.channel_name, [])
          if _acc_imgs:
              _blocks = [{"type": "text", "text":
                  "[The following images were shared earlier in this session, "
                  "in chronological order. They correspond to the [image] tags "
                  "in the conversation history above.]"}]
              for _uri in _acc_imgs:
                  _blocks.append({"type": "image_url", "image_url": {"url": _uri}})
              full_messages.append({"role": "user", "content": _blocks})

          now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
          day_name = now.strftime("%A")
          time_str = now.strftime("%I:%M %p").lstrip("0")
          date_str = now.strftime("%B %d, %Y")
          _mode_tag = ""
          if _is_live_channel:
              _mode_tag = "(live_voice_mode)"
          elif att_result.is_voice_message:
              _mode_tag = "(ptt_recorded_voice_mode)"

          if _regen_mode and not _regen_save_user:
              pass
          else:
              if att_result.image_attachments or att_result.audio_attachments or att_result.video_attachments:
                  user_content, image_data_uris, video_urls, save_text = await build_multimodal_user_content(
                      att_result, _marked_content, message.author.display_name,
                      time_str, day_name, date_str, mode_tag=_mode_tag,
                  )
                  if image_data_uris:
                      state._reference_images_by_channel[ctx.channel_name] = image_data_uris
                  if video_urls:
                      state._reference_videos_by_channel[ctx.channel_name] = video_urls
                  full_messages.append({
                      "role": "user", "content": user_content
                  })
                  save_message(
                      ctx.db, ctx.channel_name, "user",
                      save_text,
                      message.author.display_name
                  )
                  # Record the regen media anchor for this channel: the
                  # exact Discord message that carried this turn's media.
                  # attachment_source is the live message on normal turns and
                  # the fetched prior message on media-carrying regens — so
                  # the anchor always points at where the media actually
                  # lives, never at the !regen command itself.
                  set_channel_setting(
                      ctx.db, ctx.channel_name,
                      "last_media_discord_msg_id", str(attachment_source.id),
                  )
              else:
                  if combined_content:
                      full_messages.append({
                          "role": "user",
                          "content": f"({time_str} {day_name} {date_str}){_mode_tag}:{message.author.display_name}: {_marked_content}"
                      })

          full_messages = await inject_search_context(full_messages, combined_content, ctx, state.SEARCH_REFERENCES_MODE)

          # Inject accumulated video image snapshots (live voice only).
          # Read fresh from input/viz/ each trigger: the most recent
          # LIVEVOICE_VIZ_SNAPSHOTS_MAX *.txt files are flattened to one
          # line each (CR/LF collapsed) and wrapped in a hedged block. This
          # is ephemeral prompt context — NOT persisted to the messages
          # table — mirroring the _acc_imgs pattern above. No-op outside
          # live voice or when input/viz/ has no snapshot files.
          if _is_live_channel:
              _viz_block = voice_live_viz.build_snapshot_block(
                  state.LIVEVOICE_VIZ_SNAPSHOTS_MAX)
              if _viz_block:
                  full_messages.append({
                      "role": "user",
                      "content": [{"type": "text", "text": _viz_block}]
                  })

          # Pre-flight context enforcement. build_trimmed_history_for_payload
          # sized the history budget BEFORE the current user turn, search
          # context, accumulated images, and viz blocks were appended — none
          # of which were budgeted — and estimator drift can push even the
          # trimmed history past the endpoint's real limit. Re-trim the FINAL
          # assembled payload (FIFO over DB history + largest-message
          # truncation safety net for oversized current-turn content) so the
          # request can never leave here over the channel's context limit.
          _pre_flight_idx = _history_end_idx
          _history_end_idx = retrim_history_for_tool_round(
              full_messages, _history_end_idx, ctx.current_context_limit,
              ctx.channel_name, ctx.current_text_model,
          )
          if _history_end_idx < _pre_flight_idx:
              print(f"✂️ [{ctx.channel_name}] Pre-flight context trim: dropped "
                    f"{_pre_flight_idx - _history_end_idx} history message(s) to fit "
                    f"the {ctx.current_context_limit:,}-token limit")

          # Reset idle counters only now that we are committed to a genuine LLM
          # response. (Pure !commands, gated/dropped messages, and "someone is
          # typing" drops return earlier and intentionally do NOT reset.)
          idle.reset_idle_counters_for_companion(ctx.active_companion, source="user_message")

          llm_result = await run_llm_loop(
              full_messages, ctx, message, (att_result.is_voice_message or _is_live_channel),
              state._reference_images_by_channel, _consecutive_reacts_by_channel, client,
              history_end_idx=_history_end_idx,
              reference_videos_by_channel=state._reference_videos_by_channel,
          )

          if ctx.social_mode:
              state._bot_cooldown_by_channel[ctx.channel_name] += 1
              if message.author.bot:
                  state._bot_to_bot_counter_by_channel[ctx.channel_name] += 1

          pending_saves = llm_result.pending_saves

          save_history(pending_saves, llm_result.response_text, ctx.db, ctx.channel_name)

          # Occasional social-mode @mention: sometimes prepend a real mention
          # of the triggering author so they get an actual ping. Mutates
          # display_text only — saved history uses response_text, so the
          # token never reaches the DB. Skips command paths (!regen/!reply/
          # !execute -> _regen_mode), tool-round replies (display_text is
          # empty; their text was already sent from llm_loop), and
          # live-voice channels (TTS would speak the raw token). Bot authors
          # are included deliberately — the ping triggers the other bot,
          # backstopped by BOT_TO_BOT_MAX_EXCHANGES.
          if (ctx.social_mode
                  and not _regen_mode
                  and not _is_live_channel
                  and llm_result.display_text
                  and random.randint(1, SOCIAL_MODE_MENTION_ODDS) == round(SOCIAL_MODE_MENTION_ODDS / 2)):
              llm_result.display_text = f"<@{message.author.id}> " + llm_result.display_text

          _reply_kwargs = {}
          if _reply_reference is not None:
              # !reply: post the response as a Discord reply to the message
              # above the command (so other bots get triggered if it's theirs).
              _reply_kwargs["reference"] = _reply_reference
          elif config.DIRECT_REPLIES_ONLY or ctx.social_mode:
              _reply_kwargs["reference"] = message
          await safe_send_chunked(message.channel, llm_result.display_text, **_reply_kwargs)

          _tts_text = llm_result.tts_text
          _tts_voice_id = llm_result.tts_voice_id

          # Fire the anchored-memory review AFTER the main response has been
          # saved to the DB, so the review turns are appended in correct
          # chronological order (main assistant turn first, then the review
          # prompt/response). This fires for every successful journal write —
          # !updateJournal, organic <autojournal>/<permjournal> in chat, etc.
          if llm_result.had_journal_write:
              from .anchor_review import run_anchor_review
              await run_anchor_review(
                  ctx, message.channel,
                  prior_response_text=llm_result.response_text,
                  send_func=safe_send,
              )

      except discord.errors.DiscordServerError as e:
          print(f"⚠️ Discord server error: {e}")
          try:
              await message.channel.send(
                  "*⚠️ Discord is having issues right now (503). Your message was received but I couldn't respond. Please try again in a moment.*"
              )
          except Exception:
              pass
      except aiohttp.ClientError as e:
          print(f"⚠️ Network error: {e}")
          try:
              await message.channel.send(
                  "*⚠️ A network error occurred while processing your message. Please try again in a moment.*"
              )
          except Exception:
              pass
      except SystemBlockOversizedError as e:
          print(f"🚫 [{ctx.channel_name}] {e}")
          try:
              await safe_send_chunked(message.channel, e.format_user_message())
          except Exception:
              pass
      except Exception as e:
          print(f"⚠️ Unexpected error in message handler: {e}")
          traceback.print_exc()
          try:
              await message.channel.send(
                  "*⚠️ Something went wrong while processing your message. Please try again.*"
              )
          except Exception:
              pass
      finally:
          flush_pending_saves(pending_saves, ctx.db, ctx.channel_name)

      if (llm_result is not None and _is_live_channel
              and _live is not None and _live.is_active()):
          # Q2: deliver the reply as a voice reply through the active live
          # session. The text reply was already posted above (display_text +
          # per-round tool output via run_llm_loop); here we add the TTS
          # transcript, the .mp3 attachment, and playback through the live
          # session's voice client — mirroring a spoken live-voice reply.
          # Also reset the idle-timeout watchdog so a text/image exchange
          # counts as activity (parity with spoken utterances, which reset
          # it in _on_utterance_ready_threadsafe).
          _live._last_interaction_ts = time.time()
          _lv_tts_text = ("\n\n".join(llm_result.voice_text_parts)
                           if llm_result.voice_text_parts
                           else llm_result.display_text)
          if _lv_tts_text:
              try:
                  _audio = await text_to_speech(_lv_tts_text, voice_id=llm_result.tts_voice_id)
                  if _audio:
                      try:
                          _file = discord.File(io.BytesIO(_audio), filename="response.mp3")
                          await message.channel.send(file=_file)
                      except Exception as _e:
                          print(f"⚠️ [livevoice] TTS mp3 upload failed: {_e}")
                      await _live.play_audio_bytes(_audio)
              except Exception as _e:
                  print(f"⚠️ [livevoice] TTS for text/image in live channel failed: {_e}")
      else:
          await handle_tts(_tts_text, _tts_voice_id, message)
    finally:
      state.ON_MESSAGE_IN_FLIGHT.discard(ctx.channel_name)

# ============================================
# START
# ============================================
if __name__ == "__main__":
    if not config.DISCORD_TOKEN or not provider._api_key():
        print(f"Add DISCORD_TOKEN and your {provider.provider_name()} API key!")
    else:
        client.run(config.DISCORD_TOKEN)
