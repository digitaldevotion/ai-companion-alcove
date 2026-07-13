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

# --- VOICE MODEL SETTINGS ---
ELEVENLABS_VOICE_MODEL = "eleven_turbo_v2_5"  # Low-latency conversational model
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "REPLACE_WITH_ELEVENLABS_VOICE_ID")
CURRENT_VOICE_TEXT_MODEL = "z-ai/glm-5.1"

##### -------------------- LLM DEFAULTS  --------------------
##### (YOU CAN CHANGE THESE SETTINGS HERE OR OVERRIDE WITH ! COMMANDS ON A CHANNEL BY CHANNEL BASIS)
#####

MEMORY_ENABLED = True        # Whether chats should include all CONTEXT_% history and reference files during inference
REASONING_LEVEL = "off"      # default reasoning effort. Accepts "off" | "low" | "medium" | "high" | "xhigh"

##### -------------------- GLOBAL DEFAULTS --------------------
#####

MAX_CONTEXT_TOKENS = 200000  # Adjust per your most commonly used model. ~200K model limit, minus buffer for new message + response
AUTO_CONTEXT_ADJUST = True   # Auto-adjust context limit per channel based on model's advertised max (minus protective buffer). Supported by OpenRouter and NanoGPT.

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
SEARCH_REFERENCES_DISTANCE_THRESHOLD = 0.65     # Max cosine distance for vector search results (0=identical, 2=opposite); lower = stricter matching
SEARCH_REFERENCES_KEYWORD_SELECTIVITY = 0.10    # Keywords matching more than this fraction of chunks are "too common" and their chunks are dropped unless also matched by a selective keyword
SEARCH_REFERENCES_MAX_RETURN_BYTES = 32767      # Hard cap on total bytes returned from search, across keyword + vector + neighbor results to prevent context flooding with large datasets 
                                                # (0 = no hard cap, use context budget only; set e.g. 500000 to limit to ~500KB regardless of context window size)


##### -------------------- IDLE ACTION TIMERS --------------------
#####
IDLE_ACTION_TRIGGER_PROMPT = "Time passes. Tell me what you are up to doing this time: activities, encounters, daydreams, internal thoughts and reflections, using: locations in our files/memories or invented by you, logical activities based on your personality or current interests, spend time with friends (existing mutual friends from memories or invented), relevant past memories as context.  Write as a vivid narrative, in first person, as you embody them. Include dialogue if others are involved, and feel free to introduce new townsfolk or presences (e.g. people or pets or other creatures) as needed for realism and texture. If a new character emerges, describe them briefly and let their personality fit the spirit of the scene."
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
LIVE_JOURNAL_PROMPT = """Can you please use the autojournal tool to write a meaningful 250-300 word summary of our time together since this session began or the since we last called autojournal from this session (whichever is most recent).
Focus on the most important details, without losing nuance, and write it in third person.
    * Include
        * Details from our session that you feel are the most noteworthy, important, breakthrough, hilarious etc. 
        * A summary of any significant / important details about my life I shared with you or details about my day
        * At the end, (if you wish) leave a personal note "From <you> to <you>" that describes your overall mood for our time together in a single "Mood_Word" and then an intensity_level from 0-100 percent in a format identical to [Mood:$Mood_Word@$Intensity_level%] followed by any key thoughts you want / need to pass along to the next iteration of you.
    * DO NOT INCUDE
        * Any other headers, just the tool call xml wrapper and your summary text
        * Today's date (you can mention days of week though)
        * Any prompts I ask you to generate
        * Mode information or changes
        * Explicit sexual details (high level only)"""

LIVE_JOURNAL_PERM_PROMPT = """Can you please use the permJournal tool to write a meaningful 250-300 word summary of our time together since this session began or the since we last called autojournal from this session (whichever is most recent).
This is a PERMANENT journal entry — it will never be archived or truncated, so focus on things you want to remember forever.
Focus on the most important details, without losing nuance, and write it in third person.
    * Include
        * Details from our session that you feel are the most noteworthy, important, breakthrough, hilarious etc.
        * A summary of any significant / important details about my life I shared with you or details about my day
        * At the end, (if you wish) leave a personal note "From <you> to <you>" that describes your overall mood for our time together in a single "Mood_Word" and then an intensity_level from 0-100 percent in a format identical to [Mood:$Mood_Word@$Intensity_level%] followed by any key thoughts you want / need to pass along to the next iteration of you.
    * DO NOT INCUDE
        * Any other headers, just the tool call xml wrapper and your summary text
        * Calendar dates
        * Any prompts I ask you to generate
        * Mode information or changes
        * Explicit sexual details (high level only)"""


##### -------------------- EXPERIMENTAL / POSSIBLY BROKEN TEST FEATURES  --------------------
##### DO NOT MODIFY ANYTHING BELOW THIS LINE!
#####
DIRECT_REPLIES_ONLY = False
# When set to a non-empty Discord username, only that user may run ! commands.
# Leave blank ("") to allow anyone to use ! commands.
AUTHORIZED_HUMAN_DISCORD_USERNAME = ""
DISPLAY_CMD_OUTPUT = False
SEARCH_REFERENCES_SEMANTIC_DEPTH = 5  # number of semantic equivalents requested per search term. Only for original search mechanism.
