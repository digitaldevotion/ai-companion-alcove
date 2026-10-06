# ============================================
# Alcove — context.py
# Channel context resolution and message gating
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import random
import time
from dataclasses import dataclass, field
from typing import Any

import config
from . import provider
from . import state
from .database import (
    get_db, get_channel_companion, get_channel_setting,
    set_channel_setting, get_global_var,
    is_social_mode, get_social_mode_focus,
)
from .utils import build_channel_key, build_dm_key, parse_channel_key, strip_leading_bot_mention


@dataclass
class GateResult:
    allowed: bool
    save_context: bool = False
    # True iff the message was directly addressed to this bot via an explicit
    # mention or a reply to one of the bot's prior messages. Computed only
    # inside the social-mode / DIRECT_REPLIES_ONLY gate block; stays False
    # otherwise. Threading this out lets main.py inject an `@[you]` marker
    # into the user turn text sent to the LLM (and saved to DB history) so
    # the model can tell it was addressed even when the leading <@BOT_ID>
    # mention token has been stripped from content (see strip_leading_bot_mention).
    addressed_to_bot: bool = False


async def _resolve_settings(db, channel_name, memory_enabled, search_references_mode, auto_context=True):
    channel_memory_setting = get_channel_setting(
        db, channel_name, "memory_enabled",
        "1" if memory_enabled else "0"
    )
    channel_memory_enabled = channel_memory_setting == "1"
    effective_memory = channel_memory_enabled and not channel_name.endswith("-lo")

    channel_search_setting = get_channel_setting(
        db, channel_name, "search_references_enabled",
        "1" if config.SEARCH_REFERENCES_ENABLED else "0"
    )
    channel_search_enabled = channel_search_setting == "1"

    channel_anchors_setting = get_channel_setting(
        db, channel_name, "anchors_enabled", "1"
    )
    effective_anchors = channel_anchors_setting == "1"

    current_text_model = get_channel_setting(db, channel_name, "text_model", config.CURRENT_TEXT_MODEL)
    current_voice_model = get_channel_setting(db, channel_name, "voice_text_model", config.CURRENT_VOICE_TEXT_MODEL)
    if not current_voice_model:
        current_voice_model = current_text_model
    current_image_model = get_channel_setting(db, channel_name, "image_model", config.CURRENT_IMAGE_MODEL)
    current_video_model = get_channel_setting(db, channel_name, "video_model", config.CURRENT_VIDEO_MODEL)
    current_voice_id = get_channel_setting(
        db, channel_name, "elevenlabs_voice_id", config.ELEVENLABS_VOICE_ID
    )
    current_text_provider_lock = get_channel_setting(db, channel_name, "text_provider_lock")
    current_voice_text_provider_lock = get_channel_setting(db, channel_name, "voice_text_provider_lock")
    current_image_provider_lock = get_channel_setting(db, channel_name, "image_provider_lock")
    current_video_provider_lock = get_channel_setting(db, channel_name, "video_provider_lock")
    current_context_limit = await _resolve_context_limit(db, channel_name, current_text_model, auto_context)
    current_reasoning_effort = get_channel_setting(db, channel_name, "reasoning_effort", config.REASONING_LEVEL)
    _raw_temperature = get_channel_setting(db, channel_name, "temperature")
    current_temperature = float(_raw_temperature) if _raw_temperature is not None else config.TEMPERATURE
    _raw_top_k = get_channel_setting(db, channel_name, "top_k")
    current_top_k = int(_raw_top_k) if _raw_top_k is not None else getattr(config, "TOP_K", 0)

    channel_debug_setting = get_channel_setting(
        db, channel_name, "debug_enabled", "0"
    )
    debug_enabled = channel_debug_setting == "1"

    social_mode = is_social_mode(channel_name)

    return {
        "effective_memory": effective_memory,
        "effective_anchors": effective_anchors,
        "channel_search_enabled": channel_search_enabled,
        "current_text_model": current_text_model,
        "current_voice_model": current_voice_model,
        "current_image_model": current_image_model,
        "current_video_model": current_video_model,
        "current_voice_id": current_voice_id,
        "current_text_provider_lock": current_text_provider_lock,
        "current_voice_text_provider_lock": current_voice_text_provider_lock,
        "current_image_provider_lock": current_image_provider_lock,
        "current_video_provider_lock": current_video_provider_lock,
        "current_context_limit": current_context_limit,
        "current_reasoning_effort": current_reasoning_effort,
        "current_temperature": current_temperature,
        "current_top_k": current_top_k,
        "social_mode": social_mode,
        "debug_enabled": debug_enabled,
    }


def context_limit_for_model_max(model_ctx):
    """Effective context limit for a model's advertised maximum context
    window: model max minus a dynamic buffer (≈7% of the max, floored at
    10k tokens) reserved for the response plus estimator drift.

    Single source of truth for the formula — used by auto-adjust seeding in
    _resolve_context_limit AND by context-overflow self-heal
    (prompt.attempt_context_recovery) so the two paths can never drift.
    """
    _reserve = max(10000, int(model_ctx * 0.07))
    return model_ctx - _reserve


async def _resolve_context_limit(db, channel_name, text_model, auto_context=True):
    _max = config.MAX_CONTEXT_TOKENS

    # Engine globals live in main to avoid bloating config.py. Lazy-imported
    # here (inside the function) to avoid the main <-> context circular import
    # at module load time — same pattern vectors.py uses for the embedding
    # model name.
    try:
        from . import main as _main
        _ttl_seconds = getattr(_main, "CONTEXT_LIMIT_REFRESH_TTL_SECONDS", 7 * 24 * 3600)
    except Exception:
        _ttl_seconds = 7 * 24 * 3600

    # Feature disabled entirely → MAX wins, ignore any persisted auto-cache row.
    if not config.AUTO_CONTEXT_ADJUST:
        return _max

    # Feature enabled. A persisted row from a prior successful lookup may
    # still be valid. Validate it against TWO gates before trusting it:
    #   1. Model fingerprint — the stored limit was computed for THIS model.
    #      A mismatch (config default change, companion switch, re-enable
    #      after a feature toggle) invalidates the row so it self-heals.
    #   2. TTL — even with a matching model, force a fresh provider lookup
    #      after CONTEXT_LIMIT_REFRESH_TTL_SECONDS to catch provider-side
    #      context-window changes (deprecation, vendor downgrade, etc.).
    _raw_context_limit = get_channel_setting(db, channel_name, "context_token_limit")
    _raw_context_model = get_channel_setting(db, channel_name, "context_token_model")
    _raw_refreshed_at = get_channel_setting(db, channel_name, "context_limit_refreshed_at")

    if _raw_context_limit is not None and _raw_context_model == text_model:
        try:
            _limit = int(_raw_context_limit)
        except (TypeError, ValueError):
            _limit = None
        if _limit is not None:
            # Fingerprint matches. Check TTL.
            _refreshed_ok = True
            if _raw_refreshed_at is not None:
                try:
                    _age = int(time.time()) - int(_raw_refreshed_at)
                    if _age > _ttl_seconds:
                        _refreshed_ok = False
                        print(f"📐 [{channel_name}] context_token_limit stale (TTL "
                              f"exceeded: {_age}s > {_ttl_seconds}s), re-seeding")
                except (TypeError, ValueError):
                    pass  # unparseable timestamp → trust the row
            if _refreshed_ok:
                return _limit
            # else: fall through to re-seed + lookup
        # else: unparseable limit → fall through
    elif _raw_context_limit is not None and _raw_context_model != text_model:
        print(f"📐 [{channel_name}] context_token_limit stale (model changed "
              f"'{_raw_context_model}' → '{text_model}'), re-seeding")

    # No usable persisted row, OR fingerprint mismatch, OR TTL expired.
    # Callers that opt out of network lookups (idle / voice timer paths)
    # fall back to MAX_CONTEXT_TOKENS — the same value the interactive path
    # (auto_context=True) uses when a provider lookup fails. This keeps the
    # two paths consistent: both trust the operator-configured global when
    # the true per-model limit is unknown. If MAX exceeds the model's actual
    # context window, the downstream API will return a 400 and the error-
    # surfacing layer (idle/llm_loop/provider) will log + notify Discord.
    if not auto_context:
        if _raw_context_limit is not None and _raw_context_model == text_model:
            # (Already handled and returned above when the row was usable.)
            pass
        print(f"⚠️ [{channel_name}] auto_context=False with no usable persisted limit — "
              f"using MAX_CONTEXT_TOKENS ({_max:,}); will error if MAX exceeds "
              f"model {text_model} true context window")
        return _max

    # Caller allows a lookup — seed from the fallback and refine from the
    # provider if it reports a context length.
    current_context_limit = _max
    try:
        _model_ctx = await provider.get_model_context_length(text_model)
        if _model_ctx and _model_ctx > 10000:
            current_context_limit = context_limit_for_model_max(_model_ctx)
            set_channel_setting(db, channel_name, "context_token_limit", str(current_context_limit))
            set_channel_setting(db, channel_name, "context_token_model", text_model)
            set_channel_setting(db, channel_name, "context_limit_refreshed_at", str(int(time.time())))
            _applied_reserve = _model_ctx - current_context_limit
            print(f"📐 [{channel_name}] Auto-set context limit to {current_context_limit:,} "
                  f"(model {text_model} max {_model_ctx:,} − {_applied_reserve:,} buffer)")
    except Exception as _e:
        print(f"⚠️ [{channel_name}] Auto-context lookup failed: {_e}")
    return current_context_limit


@dataclass
class ChannelContext:
    channel_name: str
    guild_name: str
    db: Any
    active_companion: str
    effective_memory: bool
    effective_anchors: bool
    channel_search_enabled: bool
    current_text_model: str
    current_voice_model: str
    current_image_model: str
    current_video_model: str
    current_voice_id: str
    current_context_limit: int
    current_reasoning_effort: str
    current_temperature: float
    current_top_k: int
    current_text_provider_lock: str = None
    current_voice_text_provider_lock: str = None
    current_image_provider_lock: str = None
    current_video_provider_lock: str = None
    is_dm: bool = False
    social_mode: bool = False
    debug_enabled: bool = False


async def resolve_channel_context(message, auth_user, memory_enabled, search_references_mode):
    _auth_user = auth_user.lower()
    if message.guild is None:
        if not _auth_user or message.author.name.lower() != _auth_user:
            return None
        guild_name = "dm"
        channel_name = build_dm_key(message.author.name)
    else:
        guild_name = message.guild.name.lower()
        channel_name = build_channel_key(guild_name, str(message.channel))

    active_companion = get_channel_companion(channel_name)
    db = get_db(active_companion)

    settings = await _resolve_settings(db, channel_name, memory_enabled, search_references_mode)

    return ChannelContext(
        channel_name=channel_name,
        guild_name=guild_name,
        db=db,
        active_companion=active_companion,
        is_dm=(message.guild is None),
        **settings,
    )


async def resolve_channel_context_by_key(channel_key, memory_enabled, search_references_mode, companion_name=None):
    if companion_name is None:
        companion_name = get_channel_companion(channel_key)
    db = get_db(companion_name)
    guild_name, _ = parse_channel_key(channel_key)
    is_dm = (guild_name == "dm")

    settings = await _resolve_settings(db, channel_key, memory_enabled, search_references_mode, auto_context=False)

    return ChannelContext(
        channel_name=channel_key,
        guild_name=guild_name or "",
        db=db,
        active_companion=companion_name,
        is_dm=is_dm,
        **settings,
    )


async def check_gating(message, ctx, client, auth_user,
                        interjected_replies=False,
                        interjected_replies_odds=10):
    _auth_user = auth_user.lower()
    # Strip a leading @mention of this bot so that mention-prefixed
    # commands (e.g. "@lani !help") are treated like bare "!help" for
    # gating and authorization purposes. message.mentions is still
    # populated by Discord, so social-mode mention detection is unaffected.
    content = strip_leading_bot_mention(message.content.strip(), client.user.id)
    cmd = content.lower()

    _default_db = get_db("default")
    only_chat_channel = get_global_var(_default_db, "only_chat_channel")
    if only_chat_channel and only_chat_channel != ctx.channel_name:
        if not cmd.startswith("!onlychathere"):
            return GateResult(allowed=False)

    # Social-mode focus: when a channel on a guild is set as the social-mode
    # focus, all other channels on that guild are silenced. The focus channel
    # keeps its normal social_mode behavior (mentions/replies only). The owner
    # may still issue !socialmode/!nosocialmode from any channel on the guild
    # to manage state.
    if message.guild is not None and ctx.guild_name:
        _focus = get_social_mode_focus(ctx.guild_name)
        if _focus and _focus != ctx.channel_name:
            if cmd not in ("!socialmode", "!nosocialmode"):
                return GateResult(allowed=False, save_context=True)

    if (config.DIRECT_REPLIES_ONLY or ctx.social_mode) and not content.startswith("!"):
        is_mentioned = client.user in message.mentions
        is_reply_to_bot = False
        ref = None
        if message.reference is not None:
            ref = message.reference.resolved
            if ref is None:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    ref = None
            if ref is not None and ref.author.id == client.user.id:
                is_reply_to_bot = True
        if not (is_mentioned or is_reply_to_bot):
            _addresses_other = bool(message.mentions) or (ref is not None)
            _odds = interjected_replies_odds * 2 if _addresses_other else interjected_replies_odds
            if interjected_replies and random.randint(1, _odds) == round(_odds / 2):
                pass
            else:
                return GateResult(allowed=False, save_context=True)
        # Reached only when the message was addressed to us (or an
        # interjected-reply pass allowed it through). Threading this out so
        # main.py can surface an `@[you]` marker to the LLM in social mode
        # — without it, the leading <@BOT_ID> mention stripped by
        # strip_leading_bot_mention would be invisible to the model, and it
        # would sometimes treat the turn as overheard context (see
        # _SOCIAL_MODE_INDICATOR rule #5) and not respond.
        _addressed_to_bot = is_mentioned or is_reply_to_bot
    else:
        _addressed_to_bot = False

    _other_bot_mentioned = any(
        m.bot and m.id != client.user.id for m in message.mentions
    )
    _we_are_mentioned = client.user in message.mentions

    channel_bot_to_bot_setting = get_channel_setting(
        ctx.db, ctx.channel_name, "bot_to_bot_chat_enabled", "0"
    )
    channel_bot_to_bot_enabled = channel_bot_to_bot_setting == "1"

    if _auth_user and cmd.startswith("!") and message.author.name.lower() != _auth_user:
        # !amIAuthorized is intentionally open to everyone — it only reports
        # whether the caller is the configured authorized user.
        _first_token = cmd.split(maxsplit=1)[0]
        if _first_token != "!amiauthorized":
            return GateResult(allowed=False)

    if ctx.social_mode:
        # Bot-to-bot backoff: a human message resets the counter (their
        # presence re-engages the bot). After BOT_TO_BOT_MAX_EXCHANGES
        # consecutive bot-to-bot replies, further bot messages are dropped
        # (context still saved) until a human intervenes.
        if not message.author.bot:
            state._bot_to_bot_counter_by_channel[ctx.channel_name] = 0
        elif state._bot_to_bot_counter_by_channel[ctx.channel_name] >= state.BOT_TO_BOT_MAX_EXCHANGES:
            print(f"🛑 [{ctx.channel_name}] Bot-to-bot chain stopped after "
                  f"{state._bot_to_bot_counter_by_channel[ctx.channel_name]} exchanges "
                  f"(author={message.author.display_name}, waiting for human intervention)")
            return GateResult(allowed=False, save_context=True)

        if state._bot_cooldown_by_channel[ctx.channel_name] > state.SOCIAL_MODE_COOLDOWN_MAX:
            print(f"🧊 [{ctx.channel_name}] Message dropped — social-mode cooldown "
                  f"(counter={state._bot_cooldown_by_channel[ctx.channel_name]}, "
                  f"author={message.author.display_name})")
            return GateResult(allowed=False, save_context=True)

    return GateResult(allowed=True, addressed_to_bot=_addressed_to_bot)
