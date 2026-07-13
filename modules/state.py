# ============================================
# Alcove — state.py
# Shared mutable runtime state extracted from main.py to break
# circular imports between main ↔ idle and main ↔ commands.
# This module must NOT import any other alcove module.
# ============================================

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

LIVEVOICE_SILENCE_MS = 700
LIVEVOICE_IDLE_TIMEOUT_MIN = 5
LIVEVOICE_SINGLE_USER = True

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
