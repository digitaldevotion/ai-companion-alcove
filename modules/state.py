# ============================================
# Alcove — state.py
# Shared mutable runtime state extracted from main.py to break
# circular imports between main ↔ idle and main ↔ commands.
# This module must NOT import any other alcove module.
# ============================================

import threading
import time
from collections import defaultdict

import config

# --- Config-derived defaults (some mutable at runtime) ---

MEMORY_ENABLED = config.MEMORY_ENABLED

LIVE_JOURNAL_PROMPT = config.LIVE_JOURNAL_PROMPT
LIVE_JOURNAL_PERM_PROMPT = config.LIVE_JOURNAL_PERM_PROMPT

SEARCH_REFERENCES_MODE = 2  # 1 = semantic expansion, 2 = vector search (ChromaDB)
SEARCH_REFERENCES_CHUNK_SIZE = 2000  # chars per chunk for vector search embedding

IDLE_NOTE_TRIGGER_MIN_MINUTES = 0
IDLE_NOTE_TRIGGER_ALLOW_HOUR_START = 8
IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP = 22
IDLE_NOTE_TRIGGER_PROMPT = (
    "Time passes. If you would like, you can write me a little random love note "
    "and slip it into our default chat. What do you want to say? Write the love "
    "note as your response here."
)

LIVEVOICE_SILENCE_MS = 1500
LIVEVOICE_IDLE_TIMEOUT_MIN = 30
LIVEVOICE_SINGLE_USER = True
# Inactivity timeout for push-to-talk (!join) voice mode. Mirrors
# LIVEVOICE_IDLE_TIMEOUT_MIN above, which governs live voice mode. After this
# many minutes without a successful TTS playback (i.e. no voice replies), the
# bot auto-disconnects from the voice channel.
LIVEVOICE_INACTIVITY_TIMEOUT_MIN = 30
LIVEVOICE_SEND_PHRASE = getattr(config, "LIVE_VOICE_SEND_PHRASE", "")
LIVEVOICE_MIN_UTTERANCE_MS = getattr(config, "LIVE_VOICE_MIN_UTTERANCE_MS", 1000)

# --- Random Thoughts feature (experimental; see main.py experimental section) ---
# Defaults live here so both main.py and idle.py can read them without a circular
# import. main.py's experimental block sets these at import time.
RANDOM_THOUGHTS_ENABLED = True
RANDOM_THOUGHT_MAX_ROLL = 6
RANDOM_THOUGHT_MAX_WORDS = 200
RANDOM_THOUGHT_TRIGGER_MIN_MINUTES = 2
RANDOM_THOUGHT_TRIGGER_MAX_MINUTES = 5

# --- Runtime state ---

# Per-channel list of user-loaded "speciality" text files (via !load).
DYNAMIC_CONTEXT_FILE_LOCATIONS = {}

# Per-channel reference images from user attachments, used by <createimage use="reference">
_reference_images_by_channel = {}

# Per-channel reference videos from user attachments, used by <createvideo use="reference">
_reference_videos_by_channel = {}

# Per-channel accumulated image data URIs (accumulate-until-activation mode).
# Eagerly fetched when a non-trigger image message arrives during accumulation;
# injected as visual content into the trigger message's prompt so the model
# can see all images shared during the session, not just the trigger's batch.
# Capped at LIVEVOICE_ACCUMULATED_IMAGES_MAX per channel (FIFO drop) to bound
# memory and context-window cost.
LIVEVOICE_ACCUMULATED_IMAGES_MAX = 5
_accumulated_images_by_channel = defaultdict(list)

# Max number of video image snapshot files (from input/viz/) injected per
# live-voice trigger. Snapshots are text descriptions of the Discord video
# feed produced by an external vision process; this module only reads the
# most recent ones and assembles them into an ephemeral prompt block (never
# persisted to the messages table).
LIVEVOICE_VIZ_SNAPSHOTS_MAX = 10

# Per-channel bot cooldown counter (social mode feature).
# After SOCIAL_MODE_COOLDOWN_MAX consecutive bot-authored responses, further
# bot messages are dropped (context still saved) until the counter decays
# (bot_cooldown_tick_task decrements by 2 every minute).
SOCIAL_MODE_COOLDOWN_MAX = 10
_bot_cooldown_by_channel = defaultdict(int)

# Per-channel consecutive bot-to-bot reply counter (social mode).
# Incremented when our bot replies to a bot; reset to 0 on any human message.
# After BOT_TO_BOT_MAX_EXCHANGES replies, further bot-to-bot messages are
# dropped (context still saved) until a human intervenes.
# bot_cooldown_tick_task also decays this by 1 every minute so stale chains
# don't inherit a high count after a pause.
BOT_TO_BOT_MAX_EXCHANGES = 5
_bot_to_bot_counter_by_channel = defaultdict(int)

# Active LiveVoiceSession (set by commands.py on !joinLive, cleared on !leave).
LIVE_VOICE_SESSION = None

# Last channel that had an interaction (set by llm_loop.py and idle.py).
LAST_INTERACTION_CHANNEL = None

# Per-channel flag: True iff the most recent user-driven assistant reply
# in this channel was spoken via TTS (i.e. the user sent a voice message
# and the bot was connected to a voice channel at reply time). Read by
# idle._run_random_thought_prompt to decide whether to also speak the
# random-thought follow-up via TTS.
LAST_REPLY_WAS_VOICE_BY_CHANNEL = {}

# --- Typing detection (shared between main.py and idle.py to avoid a circular import) ---
# channel_id -> {user_id: timestamp (seconds since epoch)}.
_active_typers = {}

def _is_anyone_typing(channel_id, current_time):
    """Prune stale typers (>10s old) and return True if anyone is still typing in the channel."""
    if channel_id not in _active_typers:
        return False
    _active_typers[channel_id] = {
        uid: ts for uid, ts in _active_typers[channel_id].items()
        if current_time - ts < 10
    }
    return len(_active_typers[channel_id]) > 0

# --- on_message in-flight tracking (shared between main.py and idle.py) ---
# Set of channel keys with an on_message handler currently being processed.
# Used by idle.py's random-thought trigger to avoid interrupting an in-progress response.
ON_MESSAGE_IN_FLIGHT = set()

# --- Pending-turn inference flag (shared between main.py and idle.py) ---
# channel_key -> row id of the newest saved-but-unprocessed user turn.
# SET by main.on_message after saving a text-only turn AND passing the
# arrival-HWM supersede gate (so only the newest in-flight message ever
# holds it). A deferred handler may run
# inference only if the flag still equals its own row id. Every chat
# inference that reads channel history pops the flag at its start — it
# consumes the pending turn, because its history load includes it (no
# before_id cutoff, or a cutoff above the pending row's id). Pop sites:
# main.py (before its typing()/inference stretch — covers text/image/voice/
# regen/social), idle._run_idle_prompt (dream/idle action/note/cron),
# idle._run_random_thought_prompt, the live-voice utterance handler, and
# anchor_review.run_anchor_review. commands.py runs no chat inference.
NEEDS_INFERENCE = {}

# --- Skills context (populated by main._load_skills_context at boot + refresh) ---
# Concatenated YAML front-matter bodies of skills/*/skill.md files, each
# followed by the file's absolute path, wrapped in START/END markers. Read
# by prompt.build_llm_main_prompt and injected into the system block right
# after the Tool Definitions block. Empty string when no skills are present
# (block is omitted entirely in that case).
SKILLS_CONTEXT = ""

# --- Vector store initialization state (shared across main/idle/commands/search/vectors) ---
# True while the ChromaDB vector store is being (re)built (embedding in
# progress). Read by search.inject_search_context to skip the search step
# so the bot stays responsive to prompts while embeddings run in the
# background. Set/cleared by vectors.init_vector_store under
# _vector_init_lock. The lock serializes inits across the boot thread,
# !switchCompanion, auto_discover_paths_task, and search.py's dynamic
# companion-mismatch reload so two inits never overlap (which would
# corrupt ChromaDB state). threading is stdlib, so importing it here
# does not violate state.py's "must not import any other alcove module"
# rule.
SEARCH_INITIALIZING = False
_vector_init_lock = threading.Lock()
