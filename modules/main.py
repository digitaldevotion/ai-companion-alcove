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

if os.name == "nt":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import discord
import aiohttp
import asyncio
import random
import traceback
import time
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import provider
from . import idle
from . import state
from .commands import handle_command
from .companions import load_file_content
from .prompt import (
    build_llm_main_prompt, build_llm_main_prompt_detailed, build_trimmed_history_for_payload,
    compose_full_messages,
    get_ai_response,
    get_image_response,
)
from .utils import safe_send, safe_send_chunked, estimate_tokens, msg_tokens, strip_leading_bot_mention
from .database import (
    save_message, get_message_count,
    get_channel_setting, clear_channel_setting,
    list_channel_settings_by_prefix,
    list_anchored_memories,
    get_db,
)
from .context import ChannelContext, resolve_channel_context, resolve_channel_context_by_key, check_gating
from .attachments import resolve_attachment_source, process_attachments, build_combined_content, build_multimodal_user_content
from .search import inject_search_context
from .llm_loop import run_llm_loop
from .directives import process_response
from .history import save_history, flush_pending_saves
from .voice_tts_handler import handle_tts
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

# ============================================
# DISCORD BOT
# ============================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
db = get_db("default")

# Throttle consecutive <react> directives, scoped per channel.
_consecutive_reacts_by_channel = defaultdict(int)

# ============================================
# TEST / EXPERIMENTAL GLOBALS
# These will eventually be promoted to config.py once finalized.
# ============================================

MIN_BOT_RESPONSE_SECONDS = 300
MAX_BOT_RESPONSE_SECONDS = 600

GROUPCHAT_TURN_DELAY_MIN = 1
GROUPCHAT_TURN_DELAY_MAX = 4

_ANCHOR_REVIEW_PROMPT = """You just finished writing a journal entry. Below is the complete current list of your global anchored memories, each prefixed by its id in the form [id:N].

Please review these anchored memories in light of everything that has happened since you last reviewed them. Use <manageAnchor> directives to keep them accurate and current:

* <manageAnchor action="update" id="N"> with revised content, when a memory needs updating because facts have changed, evolved, or been refined.
* <manageAnchor action="remove" id="N"> for memories that are no longer relevant, no longer accurate, or have been superseded by another memory.
* <manageAnchor action="add"> for small but important new details that shouldn't be lost — things that are too specific for the journal but worth anchoring long-term.

Be selective. Do not grow memories larger than 256 characters. Do not churn memories that are still accurate and relevant. Do not duplicate content already captured by another anchor. If nothing needs changing, say so and emit no directives. Be sure the match the tool definition syntax for the closing tag."""

# Track who is typing in which channel: channel_id -> {user_id: timestamp}.
# Moved to state.py as state._active_typers so idle.py can read it without a
# circular import.

# Interjected replies: when DIRECT_REPLIES_ONLY is on, the bot may still
# randomly respond to non-addressed messages. 1-in-INTERJECTED_REPLIES_ODDS
# chance per qualifying message (uses round(ODDS/2) as the target).
INTERJECTED_REPLIES = True
INTERJECTED_REPLIES_ODDS = 5 

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
# MAIN GLOBALS
# These will eventually be promoted to config.py once finalized.
# (Moved to state.py: SEARCH_REFERENCES_MODE, SEARCH_REFERENCES_CHUNK_SIZE,
#  IDLE_NOTE_TRIGGER_*, LIVEVOICE_*)
# ============================================

# Initialize vector store at boot
idle.init_vector_store(db, state.SEARCH_REFERENCES_MODE, state.SEARCH_REFERENCES_CHUNK_SIZE)


@client.event
async def on_ready():
    print(f"✨ {client.user} is online!")
    print(f"📡 Provider: {provider.provider_name()} | Default model: {config.CURRENT_TEXT_MODEL}")
    print(f"🧠 {get_message_count(db)} messages in memory")

    # Provide Discord client and default DB to the idle module
    idle.init(client, db)

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

    ctx = await resolve_channel_context(message, config.AUTHORIZED_HUMAN_DISCORD_USERNAME, state.MEMORY_ENABLED, state.SEARCH_REFERENCES_MODE)
    if ctx is None:
        return

    state.ON_MESSAGE_IN_FLIGHT.add(ctx.channel_name)

    try:
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
      _pending_followup = None
      _reply_reference = None

      if cmd.startswith("!"):
          handled = await handle_command(
              message, cmd, content, ctx.channel_name, ctx.guild_name,
              ctx.db, ctx.effective_memory, ctx.effective_anchors,
              ctx.current_text_model,
              ctx.current_voice_model, ctx.current_image_model,
              ctx.current_voice_id, ctx.current_context_limit,
              ctx.current_reasoning_effort,
              ctx.current_temperature, ctx.current_top_k,
              build_llm_main_prompt=build_llm_main_prompt,
              build_llm_main_prompt_detailed=build_llm_main_prompt_detailed,
              build_trimmed_history_for_payload=build_trimmed_history_for_payload,
              compose_full_messages=compose_full_messages,
              estimate_tokens=estimate_tokens,
              msg_tokens=msg_tokens,
              load_file_content=load_file_content,
              get_image_response=get_image_response,
              auto_context_adjust=config.AUTO_CONTEXT_ADJUST,
              client=client,
          )
          if isinstance(handled, dict) and handled.get("action") == "regen":
              content = handled["prompt"]
              _regen_mode = True
              _regen_save_user = handled.get("save_user_prompt", True)
              _carry_prior_attachments = handled.get("carry_prior_attachments", False)
          elif isinstance(handled, dict) and handled.get("action") == "execute_with_followup":
              content = handled["prompt"]
              _regen_mode = True
              _regen_save_user = True
              _pending_followup = handled.get("followup")
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
        elif not _is_voice_msg and config.MAX_RESPONSE_SECONDS > 0 and config.MAX_RESPONSE_SECONDS >= config.MIN_RESPONSE_SECONDS:
            await asyncio.sleep(random.uniform(config.MIN_RESPONSE_SECONDS, config.MAX_RESPONSE_SECONDS))

        has_image_or_voice = _is_voice_msg or any(
            a.content_type and (a.content_type.startswith("image/") or a.content_type.startswith("audio/"))
            for a in message.attachments
        )

        attachment_source = await resolve_attachment_source(message, _carry_prior_attachments)
        att_result = await process_attachments(attachment_source, message)
        if att_result.should_return:
            return
        combined_content = build_combined_content(content, att_result.voice_transcription, att_result.text_attachment_content)

        # Tracks the row id of the current user turn when it is saved to the
        # DB before prompt assembly (text path). Pass to build_trimmed_history
        # so the history load excludes rows with id >= this one — i.e. the
        # current turn itself (avoiding a duplicate with the append below)
        # and any message that arrived concurrently afterwards (preserving
        # correct ordering: each turn sees the conversation as-of its arrival).
        _latest_user_msg_id = None

        if not has_image_or_voice:
            if not (_regen_mode and not _regen_save_user):
                if not (_regen_mode and (att_result.image_attachments or att_result.audio_attachments)):
                    _latest_user_msg_id = save_message(ctx.db, ctx.channel_name, "user", combined_content, message.author.display_name)
            if not _regen_mode and not ctx.social_mode:
                _now = time.time()
                if state._is_anyone_typing(message.channel.id, _now):
                    print(f"⏭️ [{ctx.channel_name}] Message from {message.author.display_name} dropped — someone typing")
                    return

        async with message.channel.typing():

          main_block = build_llm_main_prompt(ctx.db, ctx.channel_name, memory_enabled=ctx.effective_memory, anchors_enabled=ctx.effective_anchors, social_mode=ctx.social_mode)
          history = build_trimmed_history_for_payload(
              ctx.db, ctx.channel_name, main_block, context_limit=ctx.current_context_limit,
              channel_key=ctx.channel_name, model=ctx.current_text_model,
              before_id=_latest_user_msg_id,
          )
          full_messages = compose_full_messages(main_block, history)

          now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
          day_name = now.strftime("%A")
          time_str = now.strftime("%I:%M %p").lstrip("0")
          date_str = now.strftime("%B %d, %Y")

          if _regen_mode and not _regen_save_user:
              pass
          else:
              if att_result.image_attachments or att_result.audio_attachments:
                  user_content, image_data_uris, save_text = await build_multimodal_user_content(
                      att_result, combined_content, message.author.display_name,
                      time_str, day_name, date_str,
                  )
                  if image_data_uris:
                      state._reference_images_by_channel[ctx.channel_name] = image_data_uris
                  full_messages.append({
                      "role": "user", "content": user_content
                  })
                  save_message(
                      ctx.db, ctx.channel_name, "user",
                      save_text,
                      message.author.display_name
                  )
              else:
                  full_messages.append({
                      "role": "user",
                      "content": f"{time_str} {day_name} {date_str}:{message.author.display_name}: {combined_content}"
                  })

          full_messages = await inject_search_context(full_messages, combined_content, ctx, state.SEARCH_REFERENCES_MODE)

          # Reset idle counters only now that we are committed to a genuine LLM
          # response. (Pure !commands, gated/dropped messages, and "someone is
          # typing" drops return earlier and intentionally do NOT reset.)
          idle.reset_idle_counters_for_companion(ctx.active_companion, source="user_message")

          llm_result = await run_llm_loop(
              full_messages, ctx, message, att_result.is_voice_message,
              state._reference_images_by_channel, _consecutive_reacts_by_channel, client
          )

          if ctx.social_mode:
              state._bot_cooldown_by_channel[ctx.channel_name] += 1
              if message.author.bot:
                  state._bot_to_bot_counter_by_channel[ctx.channel_name] += 1

          pending_saves = llm_result.pending_saves

          save_history(pending_saves, llm_result.response_text, ctx.db, ctx.channel_name)

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

          if _pending_followup == "anchor_review":
              try:
                  await safe_send_chunked(message.channel, "\u200B")
                  await safe_send_chunked(message.channel, "*📌 Reviewing and updating anchored memories…*")
                  anchors = list_anchored_memories(ctx.db, "global")
                  if not anchors:
                      await safe_send_chunked(message.channel, "*📌 No global anchored memories to review.*")
                  else:
                      anchor_list = "\n".join(f"[id:{row[0]}] {row[2]}" for row in anchors)
                      review_prompt = (
                          f"{_ANCHOR_REVIEW_PROMPT}\n\n"
                          f"Current global anchored memories:\n{anchor_list}"
                      )

                      review_block = build_llm_main_prompt(ctx.db, ctx.channel_name,
                          memory_enabled=ctx.effective_memory, anchors_enabled=ctx.effective_anchors,
                          social_mode=ctx.social_mode)
                      review_history = build_trimmed_history_for_payload(
                          ctx.db, ctx.channel_name, review_block,
                          context_limit=ctx.current_context_limit,
                          channel_key=ctx.channel_name, model=ctx.current_text_model
                      )
                      review_messages = compose_full_messages(review_block, review_history)

                      review_messages.append({"role": "assistant", "content": llm_result.response_text})
                      review_messages.append({"role": "user", "content": review_prompt})

                      review_text, _ = await get_ai_response(
                          review_messages, model=ctx.current_text_model,
                          reasoning_effort=ctx.current_reasoning_effort,
                          temperature=ctx.current_temperature, top_k=ctx.current_top_k,
                          channel_key=ctx.channel_name,
                          provider_lock=ctx.current_text_provider_lock,
                      )

                      review_display, _, _, _, _, _ = await process_response(
                          review_text, message.channel,
                          token_budget=ctx.current_context_limit - estimate_tokens(review_text, channel_key=ctx.channel_name),
                          db=ctx.db, user_message=message,
                          consecutive_reacts=_consecutive_reacts_by_channel[ctx.channel_name],
                          suppress_reacts=True, active_companion=ctx.active_companion,
                          send_func=safe_send, channel_name=ctx.channel_name,
                      )
                      if review_display:
                          await safe_send_chunked(message.channel, review_display)

                      save_message(ctx.db, ctx.channel_name, "user", review_prompt, "system")
                      save_message(ctx.db, ctx.channel_name, "assistant", review_text, ctx.active_companion or "assistant")
              except Exception as e:
                  print(f"⚠️ Anchor review follow-up failed: {e}")
                  traceback.print_exc()

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
