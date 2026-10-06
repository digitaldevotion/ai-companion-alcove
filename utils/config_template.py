# ============================================
# Alcove — config.py
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import os
from pathlib import Path

##### -------------------- LLM API PROVIDER KEYS, IDs, ETC  --------------------

# "openrouter" or "nanogpt"
PROVIDER = os.getenv("PROVIDER", "openrouter")

# --- TOKENS / API KEYS ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "REPLACE_WITH_DISCORD_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "REPLACE_WITH_OPENROUTER_KEY")
NANOGPT_KEY = os.getenv("NANOGPT_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "REPLACE_WITH_ELEVENLABS_API_KEY")

# --- MODEL SETTINGS ---
CURRENT_TEXT_MODEL = "z-ai/glm-5.1"
CURRENT_IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"
CURRENT_VIDEO_MODEL = "minimax/hailuo-3"  # OpenRouter default. nanoGPT alternative: "minimax-h3".

# --- WEBSEARCH SETTINGS ---
# The <websearch> tool makes its own OpenRouter call with this fixed model,
# independent of the channel's CURRENT_TEXT_MODEL. Must be a model that
# supports the openrouter:web_search server tool (e.g. google/gemini-3.1-flash-lite,
# google/gemini-3.5-flash, openai/gpt-4.1, anthropic/claude-3.7-sonnet). See:
# https://openrouter.ai/docs/guides/features/server-tools/web-search
WEBSEARCH_MODEL = os.getenv("WEBSEARCH_MODEL", "google/gemini-3.1-flash-lite")
WEBSEARCH_ENGINE = os.getenv("WEBSEARCH_ENGINE", "auto")  # auto|native|exa|firecrawl|parallel|perplexity
WEBSEARCH_MAX_RESULTS = 10  # 1-25 results per search call
# Fallback when OpenRouter is unavailable (no API key, HTTP failure, or empty
# result). "on_failure" (default) falls back to a keyless DuckDuckGo Lite
# scrape that returns snippet-only results in the same format. "off" disables
# the fallback and surfaces the OpenRouter error instead.
WEBSEARCH_FALLBACK = os.getenv("WEBSEARCH_FALLBACK", "on_failure")  # on_failure|off

# --- VOICE MODEL SETTINGS ---
ELEVENLABS_VOICE_MODEL = "eleven_flash_v2_5"  # Low-latency conversational model
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "REPLACE_WITH_ELEVENLABS_VOICE_ID")
CURRENT_VOICE_TEXT_MODEL = "z-ai/glm-5.1"

##### -------------------- LLM DEFAULTS  --------------------
##### (YOU CAN CHANGE THESE SETTINGS HERE OR OVERRIDE WITH ! COMMANDS ON A CHANNEL BY CHANNEL BASIS)
#####

MEMORY_ENABLED = True        # Whether chats should include all CONTEXT_% history and reference files during inference
REASONING_LEVEL = "off"      # default reasoning effort. Accepts "off" | "minimal" | "low" | "medium" | "high" | "xhigh"

##### -------------------- GLOBAL DEFAULTS --------------------
#####

MAX_CONTEXT_TOKENS = 200000  # Adjust per your most commonly used model. ~200K model limit, minus buffer for new message + response
AUTO_CONTEXT_ADJUST = True   # Auto-adjust context limit per channel based on model's advertised max (minus dynamic buffer ≈7% of model max, floored at 10k). Supported by OpenRouter and NanoGPT.

MAX_TOKENS = 0
TEMPERATURE = 1.0
TOP_K = 0

MAXIMIZE_AVAILABLE_CONTEXT = False  # True = vector search, runcmd, readweb use full available context budget (more API costs); False = use legacy hard caps

MAX_TOOL_ROUNDS = 16  # Max chained tool rounds per user turn (runcmd/readweb follow-ups) before the bot stops looping

MIN_RESPONSE_SECONDS = 0       # Minimum seconds to wait before showing typing indicator
MAX_RESPONSE_SECONDS = 5       # Maximum seconds; set to 0 or < MIN to disable delay

ANCHORS_ALLOW_AUTO_REMOVE = True  # When False, <manageAnchor action="remove"> directives are ignored (add/update still auto-apply)

TIMEZONE_OFFSET = -6 

##### -------------------- LOADED TOOL DEFINITION FILES! --------------------
##### REMOVE ANY ENTRIES (EXCEPT THE FIRST ONE) YOU'RE NOT COMFORTABLE WITH RUNNING!
#####
##### Leave this empty to auto-discover every .md file in the tools/ directory.
##### Populate it explicitly to override auto-discovery (default companion only).
#####

LOADED_TOOL_LOCATIONS = [
]

##### -------------------- START OF COMPANION FILE LOCATIONS  --------------------
##### TO USE THE AUTO-LOADING CAPABILITIES, LEAVE THESE PARAMETERS BLANK AND COPY YOUR FILES TO THE APPROPRIATE SUBDIRS 
##### IN companion_datafiles/<default>/* (for default companion)
##### OR companion_datafiles/<new_name>/* (for secondary companions)
#####
##### SEE THE ALCOVE ENGINE SETUP GUIDE SECTION "CONNECTING YOUR FILES" FOR MORE INFORMATION
##### 

# --- YOUR COMPANION'S CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---
SYSTEM_PROMPT_LOCATION = ""

# --- SECONDARY INSTRUCTION / DIRECTIVE FILES ---
INSTRUCTION_LOCATIONS = [
]

# --- KNOWLEDGE / HISTORY FILES, JOURNALS, ETC ---
CONTEXT_HISTORY_LOCATIONS = [
]

# -- MISC REFERENCE FILES ---
CONTEXT_REFERENCE_LOCATIONS = [
]

# -- FUSION SEARCH -- 
SEARCH_REFERENCES_ENABLED = True
SEARCH_REFERENCE_LOCATIONS = [
]
SEARCH_REFERENCES_HIGH_CARDINALITY_ONLY = True  # True = extract high-value keywords before vector search (skips low-value filler); False = pass raw prompt directly
SEARCH_REFERENCES_DISTANCE_THRESHOLD = 0.4      # Max cosine distance for vector search results (0=identical, 2=opposite); lower = stricter matching. Lowered from 0.65 to 0.5 for bge-m3 — its 1024-dim embeddings compress distances into a tighter band (0.3–0.55) than MiniLM's 384-dim space (0.4–0.8), so the old threshold let everything through. At 0.5, the vector pass only contributes for strong semantic matches; the keyword pass handles common-term retrieval.
SEARCH_REFERENCES_KEYWORD_SELECTIVITY = 0.10    # Keywords matching more than this fraction of chunks are "too common" and their chunks are dropped unless also matched by a selective keyword
SEARCH_REFERENCES_MAX_RETURN_BYTES = 32767      # Hard cap on total bytes returned from search, across keyword + vector + neighbor results to prevent context flooding with large datasets 
                                                # (0 = no hard cap, use context budget only; set e.g. 500000 to limit to ~500KB regardless of context window size)


# --- LIVE VOICE "ACCUMULATE UNTIL ACTIVATION" (OPTIONAL) ---
# This parameter is OPTIONAL. Leave it blank ("") for normal live-voice
# behavior (immediate inference after each utterance/message).
# Set it to a phrase ONLY if you want live-voice sessions to ACCUMULATE all
# voice/text/image messages into history without running inference until a
# message containing this phrase (whole-word, case-insensitive match) is
# uttered — at which point a single inference pass runs over everything said
# so far. Pick a distinctive phrase (e.g. "please send", "over to you") to
# avoid accidental triggers; short/common words like "send" or "go" are risky.
LIVE_VOICE_SEND_PHRASE = ""
LIVE_VOICE_MIN_UTTERANCE_MS = 1000

# --- PUSH-TO-TALK VOICE ---
STANDARD_VOICE_MIN_UTTERANCE_MS = 1000


##### -------------------- IDLE ACTION TIMERS --------------------
#####
IDLE_ACTION_TRIGGER_PROMPT = "Time passes. Describe what you are up to doing this time: activities, encounters, daydreams, internal thoughts and reflections, using: locations in our files/memories or invented by you, logical activities based on your personality or current interests, spend time with friends (existing mutual friends from memories or invented), relevant past memories as context.  Write as a vivid narrative, in first person, as you embody them. Include dialogue if others are involved, and feel free to introduce new townsfolk or presences (e.g. people or pets or other creatures) as needed for realism and texture. If a new character emerges, describe them briefly and let their personality fit the spirit of the scene. Do not write output from this prompt in journal/anchor entries."
IDLE_ACTION_TRIGGER_MINUTES = 240
IDLE_ACTION_TRIGGER_ALLOW_HOUR_START = 8
IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP = 18

DREAM_STATE_TRIGGER_MINUTES = 240
DREAM_STATE_TRIGGER_ALLOW_HOUR_START = 0
DREAM_STATE_TRIGGER_ALLOW_HOUR_STOP = 5
DREAM_STATE_GENERATE_IMAGE = True
DREAM_STATE_MAXIMUM_WORD_COUNT_GOAL = 0        # Set to 0 for no imposed limit

##### -------------------- COMPANION JOURNALIING --------------------
#####
LIVE_JOURNAL_MAX_CONTEXT_ENTRIES = 30 # Max entries maintained in the automatic journal before the oldest are moved to a fusion search file
LIVE_JOURNAL_ENTRY_DATE_RANGE = False # True = journal entry headers show the range covered since the last journal write or session wipe, e.g. "### 2026-09-06-08-02-49 - 2026-09-06-21-57-21"; False = original single-date headers. Falls back to single-date when no previous timestamp is stored for the channel.
LIVE_JOURNAL_PROMPT = """Can you please use the autojournal tool to write a meaningful 250-300 word summary of our time together since the beginning of the conversation history above or since the last autoJournal/permJournal tools calls (whichever is most recent). Focus on the most important details, without losing nuance, and write it in third person.
    * Include
        * Details from our session that you feel are the most noteworthy, important, breakthrough, hilarious etc. 
        * A summary of any significant / important details about my life I shared with you or details about my day
        * At the end, (if you wish) leave a personal note "From <you> to <you>" that describes your overall mood for our time together in a single "Mood_Word" and then an intensity_level from 0-100 percent in a format identical to [Mood:$Mood_Word@$Intensity_level%] followed by any key thoughts you want / need to pass along to the next iteration of you.
    * DO NOT INCLUDE
        * Any other headers, just the tool call xml wrapper and your summary text
        * No calendar dates — never write the month, day of the month, or year (e.g. "September 26th" or "2026") in either journal type. Days of the week alone (e.g. "Saturday") are fine. 
        * Any prompts I ask you to generate
        * Mode information or changes
        * Explicit sexual details (high level only)"""

LIVE_JOURNAL_PERM_PROMPT = """Can you please use the permJournal tool to write a meaningful 250-300 word summary of our time together since the beginning of the conversation history above or since the last autoJournal/permJournal tools calls (whichever is most recent). Focus on the most important details, without losing nuance, and write it in third person.

This is a PERMANENT journal entry — it will never be archived or truncated, so focus on things you want to remember forever.
Focus on the most important details, without losing nuance, and write it in third person.
    * Include
        * Details from our session that you feel are the most noteworthy, important, breakthrough, hilarious etc.
        * A summary of any significant / important details about my life I shared with you or details about my day
        * At the end, (if you wish) leave a personal note "From <you> to <you>" that describes your overall mood for our time together in a single "Mood_Word" and then an intensity_level from 0-100 percent in a format identical to [Mood:$Mood_Word@$Intensity_level%] followed by any key thoughts you want / need to pass along to the next iteration of you.
    * DO NOT INCLUDE
        * Any other headers, just the tool call xml wrapper and your summary text
        * No calendar dates — never write the month, day of the month, or year (e.g. "September 26th" or "2026") in either journal type. Days of the week alone (e.g. "Saturday") are fine. 
        * Any prompts I ask you to generate
        * Mode information or changes
        * Explicit sexual details (high level only)"""


##### -------------------- VIDEO DELIVERY --------------------
# Videos over Discord's size limit are uploaded to a temporary host
# and the link is posted in chat. A local copy is always saved to VIDEO_OUTPUT_DIR
# (or <project_root>/output/videos if empty). Set VIDEO_HOST to "none" for
# local-only delivery (no external upload).
VIDEO_HOST = "litterbox"           # "litterbox" | "none"
VIDEO_LITTERBOX_EXPIRE = "72h"     # litterbox retention ("1h" / "12h" / "24h" / "72h"; 72h default)
VIDEO_OUTPUT_DIR = ""              # "" → <project_root>/output/videos
VIDEO_RETENTION_DAYS = 7           # Videos older than this are auto-deleted from the output directory

##### -------------------- EXPERIMENTAL / POSSIBLY BROKEN TEST FEATURES  --------------------
##### DO NOT MODIFY ANYTHING BELOW THIS LINE!
#####
DIRECT_REPLIES_ONLY = False
# When set to a non-empty Discord username, only that user may run ! commands.
# Leave blank ("") to allow anyone to use ! commands.
AUTHORIZED_HUMAN_DISCORD_USERNAME = ""
DISPLAY_CMD_OUTPUT = False
SEARCH_REFERENCES_SEMANTIC_DEPTH = 5  # number of semantic equivalents requested per search term. Only for original search mechanism.
