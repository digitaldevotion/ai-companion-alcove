# ============================================
# Alcove — config.py
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import os
from pathlib import Path

# --- LLM PROVIDER ---
# "openrouter" or "nanogpt"
PROVIDER = os.getenv("PROVIDER", "openrouter")

# --- TOKENS / API KEYS ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "REPLACE_WITH_DISCORD_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "REPLACE_WITH_OPENROUTER_KEY")
NANOGPT_KEY = os.getenv("NANOGPT_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "REPLACE_WITH_ELEVENLABS_API_KEY")

# --- MODEL SETTINGS ---
CURRENT_TEXT_MODEL = "anthropic/claude-sonnet-4.6"
CURRENT_IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"

# --- VOICE MODEL SETTINGS ---
ELEVENLABS_VOICE_MODEL = "eleven_turbo_v2_5"  # Low-latency conversational model
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "REPLACE_WITH_ELEVENLABS_VOICE_ID")
CURRENT_VOICE_TEXT_MODEL = "anthropic/claude-sonnet-4.6"

# --- SYSTEM-WIDE LLM DEFAULTS (YOU CAN CHANGE THEM HERE OR OVERRIDE WITH ! COMMANDS ON A CHANNEL BY CHANNEL BASIS) ---
MAX_CONTEXT_TOKENS = 128000  # Adjust per your most commonly used model. ~200K model limit, minus buffer for new message + response
AUTO_CONTEXT_ADJUST = True   # Auto-adjust context limit per channel based on model's advertised max (minus 10k buffer)

MEMORY_ENABLED = True        # Whether chats should include all CONTEXT_% history and reference files during inference
MAXIMIZE_AVAILABLE_CONTEXT = False  # True = vector search, runcmd, readweb use full available context budget (more API costs); False = use legacy hard caps
REASONING_LEVEL = "off"      # default reasoning effort. Accepts "off" | "low" | "medium" | "high"

# --- OTHER DEFAULTS
MAX_TOKENS = 0
TEMPERATURE = 1.0
MAX_TOOL_ROUNDS = 16  # Max chained tool rounds per user turn (runcmd/readweb follow-ups) before the bot stops looping
MIN_RESPONSE_SECONDS = 0       # Minimum seconds to wait before showing typing indicator
MAX_RESPONSE_SECONDS = 5       # Maximum seconds; set to 0 or < MIN to disable delay

# --- MISC ---
TIMEZONE_OFFSET = -6 

# --- YOUR COMPANION'S CUSTOM INSTRUCTIONS / SYSTEM PROMPT ---
SYSTEM_PROMPT_LOCATION = Path(__file__).parent / "companion_datafiles/your_persona_here.md"

# --- SECONDARY INSTRUCTION / DIRECTIVE FILES ---
INSTRUCTION_LOCATIONS = [
    Path(__file__).parent / "companion_datafiles/example_file_1",
    Path(__file__).parent / "companion_datafiles/example_file_2",
    Path(__file__).parent / "companion_datafiles/example_file_3"
]

# --- LOADED TOOL DEFINITION FILES! REMOVE ANY ENTRIES (EXCEPT THE FIRST ONE) YOU'RE NOT COMFORTABLE WITH RUNNING!
LOADED_TOOL_LOCATIONS = [
        Path(__file__).parent / "tools/tool_header.md",         # This must always be present & at the top of this list
        Path(__file__).parent / "tools/file_output.md",         # Allows for writing of responses directly to files. It cannot overwrite existing files.
        Path(__file__).parent / "tools/run_cmd.md",             # Permits ANY operating system command from being accessed by your companion. Use extreme caution!
        Path(__file__).parent / "tools/create_image.md",        # Enables your companion to directly create images
        Path(__file__).parent / "tools/read_image.md",          # reads an image from a specified location
        Path(__file__).parent / "tools/read_web.md",            # Fetch and extract readable content from web pages
        Path(__file__).parent / "tools/save_anchor.md",         # Allow the LLM to save global anchored memories
        Path(__file__).parent / "tools/react.md"                # Allow the LLM to react to your messages
]

# --- KNOWLEDGE / HISTORY FILES, JOURNALS, ETC ---
CONTEXT_HISTORY_LOCATIONS = [
    Path(__file__).parent / "companion_datafiles/example_file_1",
    Path(__file__).parent / "companion_datafiles/example_file_2",
    Path(__file__).parent / "companion_datafiles/example_file_3",
    Path(__file__).parent / "companion_datafiles/example_file_4"
]

# -- MISC REFERENCE FILES ---
CONTEXT_REFERENCE_LOCATIONS = [
    Path(__file__).parent / "companion_datafiles/example_file_1",
    Path(__file__).parent / "companion_datafiles/example_file_2",
    Path(__file__).parent / "companion_datafiles/example_file_3"
]

# -- FUSION SEARCH -- 
SEARCH_REFERENCES_ENABLED = True
SEARCH_REFERENCE_LOCATIONS = [
]
SEARCH_REFERENCES_HIGH_CARDINALITY_ONLY = True  # True = extract high-value keywords before vector search (skips low-value filler); False = pass raw prompt directly
SEARCH_REFERENCES_DISTANCE_THRESHOLD = 0.65  # Max cosine distance for vector search results (0=identical, 2=opposite); lower = stricter matching
SEARCH_REFERENCES_KEYWORD_SELECTIVITY = 0.10  # Keywords matching more than this fraction of chunks are "too common" and their chunks are dropped unless also matched by a selective keyword


################# DO NOT MODIFY THIS LINE OR ANYTHING BELOW! #################################

# --- EXPERIMENTAL FEATURES (DO NOT USE!) ---
DIRECT_REPLIES_ONLY = False
DISPLAY_CMD_OUTPUT = False  
SEARCH_REFERENCES_SEMANTIC_DEPTH = 5  # number of semantic equivalents requested per search term
