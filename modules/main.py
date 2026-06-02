# ============================================
# Alcove v1.3.0 — main.py
# Core bot logic and message handling
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
if __package__ is None or __package__ == "":
    import sys
    print("Error: main.py must be run as a package module, not as a script.")
    print("Use: python -m modules.main")
    print("Or launch via: python alcove.py")
    sys.exit(1)

import discord
import aiohttp
import json
import os
import asyncio
import random
import re
import traceback
import base64
import io
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, time as dt_time
from pathlib import Path
from discord.ext import tasks
from .voice import voice_manager, text_to_speech, speech_to_text
from .directives import process_response
from . import provider
from .database import (
    init_database, save_message, get_recent_messages,
    get_anchored_memories, add_anchored_memory, remove_anchored_memory,
    list_anchored_memories, rename_channel, get_message_count,
    get_channel_setting, set_channel_setting, clear_channel_setting,
    reset_all_channel_settings, prune_orphan_channels,
    list_channel_settings_by_prefix,
)
import config

# Load opus for discord.py voice support
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
        winlib_dir = os.path.join(project_root, "winlib")
        local_opus = None
        for name in ["opus.dll", "libopus.dll", "libopus-0.dll"]:
            candidate = os.path.join(winlib_dir, name)
            if os.path.isfile(candidate):
                local_opus = candidate
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

# --- MUTABLE GLOBALS ---
MEMORY_ENABLED = config.MEMORY_ENABLED

# Per-channel list of user-loaded "speciality" text files (via !load).
# Keyed by channel_name → list of filesystem paths. Authoritative storage is
# the channel_settings table (param = "dynamic_file_<uuid>", value = path) so
# the list survives restarts; this dict is a convenience cache that commands
# update alongside the DB. build_llm_main_block reads straight from the DB so
# it works even on first access after a restart.
DYNAMIC_CONTEXT_FILE_LOCATIONS = {}

def load_file_content(path):
    # Return the file's text, or None if the path is empty/missing/unreadable.
    # Any I/O failure (deleted between auto-discovery scans, permission denied,
    # path is now a directory, etc.) is logged as a console warning and
    # treated as "skip this file" so the bot keeps running. Every consumer
    # already guards on the result with `if file_content:`.
    #
    # FileNotFoundError on an auto-discovered path triggers an on-demand
    # rescan (debounced) so the next load sees the current directory state
    # instead of waiting up to 30 minutes for the periodic refresh.
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            print(f"⚠️ File not found (skipping): {path}")
            # Late-bound call — `_maybe_auto_rescan` is defined below, but
            # Python resolves function-name references at call time, so this
            # works as long as the helper is defined before the first invocation.
            _maybe_auto_rescan(path)
        except UnicodeDecodeError as e:
            print(f"⚠️ Binary/non-text file (skipping): {path} — {e}")
        except OSError as e:
            print(f"⚠️ Could not read file (skipping): {path} — {e}")
    return None

# ============================================
# AUTO-DISCOVERY OF CONFIG PATHS
# ============================================
# If certain path-variables in config.py are left empty (empty string for the
# single-file SYSTEM_PROMPT_LOCATION, empty list for the list variables), scan
# a designated companion_datafiles/* subdirectory recursively and populate the
# variable with the file paths found there. Runs at program start and again
# every 30 minutes (see auto_discover_paths_task below) so newly-added files
# are picked up without a restart.
_AUTO_DISCOVER_BASE = Path(__file__).parent.parent
_AUTO_DISCOVER_SYSTEM_PROMPT_DIR = _AUTO_DISCOVER_BASE / "companion_datafiles/1_system_prompt"
_AUTO_DISCOVER_LIST_DIRS = {
    "INSTRUCTION_LOCATIONS":       _AUTO_DISCOVER_BASE / "companion_datafiles/2_secondary_instructions",
    "CONTEXT_HISTORY_LOCATIONS":   _AUTO_DISCOVER_BASE / "companion_datafiles/3_context_history",
    "CONTEXT_REFERENCE_LOCATIONS": _AUTO_DISCOVER_BASE / "companion_datafiles/4_context_reference",
    "SEARCH_REFERENCE_LOCATIONS":  _AUTO_DISCOVER_BASE / "companion_datafiles/5_search_reference",
}

# Track which variables were empty when config.py was first loaded. Only
# those are re-scanned on subsequent periodic runs — variables the user
# explicitly configured in config.py are left alone.
_AUTO_DISCOVER_SYSTEM_PROMPT = (
    getattr(config, "SYSTEM_PROMPT_LOCATION", "") in ("", None)
)
_AUTO_DISCOVER_LISTS = {
    attr for attr in _AUTO_DISCOVER_LIST_DIRS
    if not getattr(config, attr, None)
}


def _scan_dir_for_files(dir_path):
    # Recursive scan; skip hidden files (e.g. .DS_Store).
    if not dir_path.is_dir():
        return []
    return sorted(
        p for p in dir_path.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )


def _auto_discover_config_paths():
    # Populate empty-at-startup config path variables from their companion
    # directories. Mutates attributes on the config module so the rest of the
    # code (which reads e.g. config.CONTEXT_HISTORY_LOCATIONS dynamically) sees
    # the discovered paths without further changes.
    if _AUTO_DISCOVER_SYSTEM_PROMPT:
        files = _scan_dir_for_files(_AUTO_DISCOVER_SYSTEM_PROMPT_DIR)
        if len(files) > 1:
            raise RuntimeError(
                f"SYSTEM_PROMPT_LOCATION auto-discovery: expected exactly one "
                f"file in {_AUTO_DISCOVER_SYSTEM_PROMPT_DIR}, found {len(files)}: "
                f"{[str(f) for f in files]}"
            )
        if files:
            config.SYSTEM_PROMPT_LOCATION = files[0]
            print(f"🔎 Auto-discovered SYSTEM_PROMPT_LOCATION: {files[0]}")
        else:
            config.SYSTEM_PROMPT_LOCATION = ""

    for attr in _AUTO_DISCOVER_LISTS:
        files = _scan_dir_for_files(_AUTO_DISCOVER_LIST_DIRS[attr])
        setattr(config, attr, files)
        print(f"🔎 Auto-discovered {attr}: {len(files)} file(s) "
              f"from {_AUTO_DISCOVER_LIST_DIRS[attr]}")


# --- On-demand rescan hook -------------------------------------------------
# When load_file_content hits FileNotFoundError on a file that came from
# auto-discovery, we rescan right away instead of waiting up to 30 minutes.
# Debounced so a prompt build that touches several now-missing files only
# triggers one rescan per burst.
_AUTO_RESCAN_DEBOUNCE_SEC = 5.0
_last_auto_rescan_time = 0.0


def _path_in_auto_discovered_list(path):
    # Return True iff `path` currently appears in one of the auto-managed
    # config path variables (the ones that were empty in config.py at startup
    # and are being populated from companion_datafiles/N_*/). Paths the user
    # hard-coded into config.py are NOT auto-managed, so rescanning wouldn't
    # help — we return False and skip the rescan for those.
    if path is None:
        return False
    as_str = str(path)
    if _AUTO_DISCOVER_SYSTEM_PROMPT:
        current = str(getattr(config, "SYSTEM_PROMPT_LOCATION", "") or "")
        if current == as_str:
            return True
    for attr in _AUTO_DISCOVER_LISTS:
        for p in getattr(config, attr, None) or []:
            if str(p) == as_str:
                return True
    return False


def _maybe_auto_rescan(missing_path):
    # Called from load_file_content when a file turned up missing. Triggers
    # _auto_discover_config_paths() so subsequent loads use the refreshed
    # list. Debounce guards against N missing files storm-rescanning.
    global _last_auto_rescan_time, SYSTEM_PROMPT
    if not _path_in_auto_discovered_list(missing_path):
        return
    now = time.monotonic()
    if now - _last_auto_rescan_time < _AUTO_RESCAN_DEBOUNCE_SEC:
        return
    _last_auto_rescan_time = now
    print(f"🔎 On-demand auto-rescan triggered by missing file: {missing_path}")
    try:
        _auto_discover_config_paths()
        # If the system prompt file was auto-discovered and disappeared, the
        # cached SYSTEM_PROMPT global still holds the old text — refresh it
        # so subsequent prompts see the current file (or the built-in fallback).
        if _AUTO_DISCOVER_SYSTEM_PROMPT:
            try:
                SYSTEM_PROMPT = (
                    load_file_content(config.SYSTEM_PROMPT_LOCATION)
                    or "You are my friendly AI companion"
                )
            except NameError:
                # SYSTEM_PROMPT hasn't been defined yet at module-load time;
                # the subsequent module-level assignment will handle it.
                pass
    except Exception as e:
        print(f"⚠️ on-demand auto-rescan failed: {e}")


# Initial scan before SYSTEM_PROMPT is loaded below so the prompt file
# discovered in companion_datafiles/1_system_prompt is picked up on first use.
_auto_discover_config_paths()
# Arm the debounce clock so the very first SYSTEM_PROMPT load (below) doesn't
# kick off a redundant rescan when the file it resolved to isn't there.
_last_auto_rescan_time = time.monotonic()

SYSTEM_PROMPT = load_file_content(config.SYSTEM_PROMPT_LOCATION) or "You are my friendly AI companion"

async def url_to_data_uri(url, timeout=30):
    """Download an image from a URL and return a base64 data URI."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return url  # fallback to raw URL if download fails
            content_type = resp.headers.get("Content-Type", "image/png")
            if not content_type.startswith("image/"):
                content_type = "image/png"
            raw = await resp.read()
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{content_type};base64,{b64}"


def estimate_tokens(text):
    # Rough token estimate: ~1 token per 4 characters.
    if text is None:
        return 0
    return len(text) // 4

def msg_tokens(msg):
    # Token estimate for one OpenRouter-style message (same as console logging).
    c = msg.get("content", "")
    if isinstance(c, str):
        return estimate_tokens(c)
    if isinstance(c, list):
        return sum(estimate_tokens(b.get("text", "")) for b in c if isinstance(b, dict))
    return 0

def build_llm_main_block(db, channel_name, memory_enabled=True):
    # Build the system-level content blocks sent to the API.
    #
    # Block 1 — Identity & instructions:
    #   - Base system prompt (persona / personality)
    #   - Instruction files (writing directives, creative directives, etc.)
    #
    # Block 2 — Reference context (only added if non-empty):
    #   - Knowledge files (K0-K5: memories, journals, summaries) — skipped when memory is disabled
    #   - Miscellaneous reference files (background info, character notes, etc.) — skipped when memory is disabled
    #   - Loaded tool definitions (always loaded — tools remain callable)
    #   - Anchored memories from the database (always loaded — user-pinned)
    system_block = []

    # --- Block 1: system prompt + instruction directives ---
    instructions = SYSTEM_PROMPT
    for i, path in enumerate(config.INSTRUCTION_LOCATIONS, 1):
        file_content = load_file_content(path)
        if file_content:
            instructions += f"\n\n## DIRECTIVE {i}\n{file_content}\n"
    system_block.append({
        "type": "text",
        "text": instructions,
        "cache_control": {"type": "ephemeral"}
    })

    # --- Block 2: knowledge + misc references + tools + anchored memories ---
    reference_context = ""

    # Knowledge files (loaded only when memory is enabled).
    # Per config.py: MEMORY_ENABLED gates both history AND reference files —
    # !noknowledge / !nomemory drops both from context for the channel.
    if memory_enabled:
        for path in config.CONTEXT_HISTORY_LOCATIONS:
            file_content = load_file_content(path)
            if file_content:
                label = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
                reference_context += f"\n\n## {label}\n{file_content}\n"

        # Miscellaneous reference files (also gated by memory_enabled to
        # match the config.py comment — these were erroneously always-loaded
        # before, so !noknowledge wasn't actually silencing them).
        for i, path in enumerate(config.CONTEXT_REFERENCE_LOCATIONS, 1):
            file_content = load_file_content(path)
            if file_content:
                reference_context += f"\n\n## Miscellaneous {i}\n{file_content}\n"

    # Loaded tool definitions (always loaded)
    for i, path in enumerate(config.LOADED_TOOL_LOCATIONS, 1):
        file_content = load_file_content(path)
        if file_content:
            reference_context += f"\n\n## Tool {i}\n{file_content}\n"

    # Dynamically loaded files for this channel (via !load). Always included
    # when present; tokens are counted in the normal context tally.
    dynamic_rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
    for i, (_param, path) in enumerate(dynamic_rows, 1):
        file_content = load_file_content(path)
        if file_content:
            label = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
            reference_context += f"\n\n## Dynamic {i} — {label}\n{file_content}\n"

    # Anchored memories from the database (always loaded — globals + current channel)
    anchored = get_anchored_memories(db, channel_name)
    if anchored:
        reference_context += "\n\n## Anchored Memories\n"
        for mid, m in anchored:
            reference_context += f"{mid}. {m}\n"

    if reference_context:
        system_block.append({
            "type": "text",
            "text": reference_context,
            "cache_control": {"type": "ephemeral"}
        })

    return system_block

def build_trimmed_history_for_payload(db, channel_name, augmented_prompt, context_limit=None):
    # Trim channel history to fit token budget; add cache_control to last turn.
    if context_limit is None:
        context_limit = config.MAX_CONTEXT_TOKENS
    system_tokens = sum(estimate_tokens(block["text"]) for block in augmented_prompt)
    history_budget = context_limit - system_tokens
    history = get_recent_messages(db, channel_name)
    history_tokens = sum(estimate_tokens(msg["content"]) for msg in history)
    while history and history_tokens > history_budget:
        removed = history.pop(0)
        history_tokens -= estimate_tokens(removed["content"])
    if history:
        last = history[-1]
        last["content"] = [{
            "type": "text",
            "text": last["content"] if isinstance(last["content"], str) else last["content"],
            "cache_control": {"type": "ephemeral"}
        }]
    return history

def compose_full_messages(augmented_prompt, history):
    full_messages = [{"role": "system", "content": augmented_prompt}]
    full_messages.extend(history)
    return full_messages

# ============================================
# API CALL
# ============================================
async def get_ai_response(messages, model, reasoning_effort=None):
    print(f"[{datetime.now():%H:%M:%S.%f}]")
    return await provider.chat_completion_text(
        model, messages,
        max_tokens=config.MAX_TOKENS,
        reasoning=reasoning_effort,
    )

async def get_image_response(prompt, model, reference_images=None):
    # Thin wrapper — all provider-specific logic lives in provider.generate_image.
    return await provider.generate_image(prompt, model, reference_images=reference_images)

async def extract_search_keywords(prompt):
    # Ask the internal logic model to extract high-cardinality, high-value
    # search keywords from the user's message. Returns a space-separated
    # string of keywords, or None if no keywords found (so the caller can
    # skip vector search entirely rather than querying with filler words).
    query = (f'''
        # Keyword Extraction
        - Extract high-value, high-cardinality search keywords from the text below.
        - Include ONLY: proper nouns, specific entities, named places, key subjects, distinctive terms.
        - Exclude: common stop words, greetings, filler, pronouns, generic verbs.
        - Output: space-separated keywords only, no quotes, no explanation, no formatting.
        - Examples: "Paris Observatory mountains telescope nebula" not "the and you we about"
        - If no high-value keywords exist, reply: NONE

        ## Text:
        '''
        + prompt
    )
    try:
        result = await internal_model_query(query)
        print(f"🔑 extract_search_keywords input: {prompt[:80]}")
        print(f"🔑 extract_search_keywords result: {result}")

        if isinstance(result, str) and result.startswith("*Error"):
            print(f"⚠️ extract_search_keywords: provider error, skipping vector search")
            return None

        if not isinstance(result, str) or not result.strip():
            print(f"⚠️ extract_search_keywords: empty result, skipping vector search")
            return None

        cleaned = result.strip()
        if cleaned.upper() == "NONE":
            print(f"🔑 extract_search_keywords: no keywords found, skipping vector search")
            return None

        # Reject conversational/non-keyword responses — real keywords are short,
        # single-line, and plain text with no markdown formatting
        if len(cleaned) > 150 or '\n' in cleaned or '**' in cleaned or '##' in cleaned:
            print(f"🔑 extract_search_keywords: result too long or formatted, likely not keywords — skipping")
            return None

        return cleaned
    except Exception as e:
        print(f"⚠️ extract_search_keywords failed: {type(e).__name__}: {e}")
        return None


async def expand_search_terms(prompt):
    # Ask the internal logic model to extract key subjects and semantic alternatives from a user prompt.
    # Returns the expanded search terms string.
#    query = ""

# 
#     if not SEARCH_ONLY_QUESTIONS:
#         query = ('''
#             # In the following query text specified below determine the following:

#                 * If the text does not contain a question that could potentially reference existing knowledge or history files, with clear search terms, based on past / already recorded knowledge simply respond with "NO_QUESTION" and stop.
#                 * If the text contains a question with clear high-cardinality search terms, identify the key search terms (single words only. The subject for the semantic search) and reply with those terms 5 highly relevant single-word semantic search equivalents for that term.
#                 * Example output: "'search_term_1' ( 'semantic_equiv1', 'semantic_equivilent2', 'semantic_equivilent_3' ) | 'search_term_2' ( 'semantic_equiv1', 'semantic_equivilent2', 'semantic_equivilent_3' ) | etc"
#                 * Example of good questions:
#                 - "What's your favorite sport?"
#                 - "Do you like hot dogs?"
#                 - "Do you remember our trip to London?"
#                 * Example of bad questions:
#                 - "Are you there?"
#                 - "Did you hear what I just said?"
#                 - "Can you search again?"
#             # The query text is: '''
#             + prompt
#         )
#     else:
    query = (f'''
        # Search Term Extraction
        - You are a simple parser whose job is to find semantic equivalent search terms from a text fragment generated external to claude that can be returned to an external search too for documentation / knowledge lookup.  

        ## Return "NO_QUESTION" if:
        - No  distinct entities, places, concepts, or events

        ## Extract single-word terms if:
        - names, places, people, events, subjects mentioned. The who, what, where, why
        - Question/text seeks information or enrichment

        ## REQUIRED OUTPUT FORMAT (all words in single quotes)
        "'term1' ( 'equiv1', 'equiv2', 'equiv3', 'equiv4', 'equiv5' ) | 'term2' ( … )"
        - CORRECT:   'Dorothy' ( 'character', 'girl', 'protagonist' )
        - INCORRECT: Dorothy ( character, girl, protagonist )

        ## Process
        - Find high-value, high-cardinality searchable nouns/concepts (single words only)
        - Judge: would this yield documented knowledge outside this conversation?
        - For each keeper: 5 single-word semantic equivalents in that same language
        - analyze and identify the language used by the text for analysis and use only words in that same language
        - No keepers → NO_QUESTION 
        - DO NOT PROVIDE JUSTIFICATION OR REASONING. ONLY THE SPECIFIED RESULTS!

        ## Text For Analysis below here
        '''
        + prompt
    )
    try:
        result = await internal_model_query(query)
        print("--------------------------------")
        print(f"🔍 expand_search_terms input: {prompt}")
        print(f"🔍 expand_search_terms result: {result}")

        # Provider-level error envelopes from chat_completion_text come back
        # as strings starting with "*Error" (HTTP errors, moderation blocks,
        # timeouts, etc.). Treat those as a failure of this helper so the
        # message handler proceeds without search augmentation rather than
        # feeding an error string into semantic_search.
        if isinstance(result, str) and result.startswith("*Error"):
            print(f"⚠️ expand_search_terms: provider returned an error "
                  f"envelope, treating as NO_QUESTION. Details: {result[:300]}")
            return "NO_QUESTION"

        # Guard against non-string / empty results that would break the
        # downstream .strip().upper() check in the caller.
        if not isinstance(result, str) or not result.strip():
            print(f"⚠️ expand_search_terms: empty or non-string result "
                  f"({type(result).__name__}), treating as NO_QUESTION")
            return "NO_QUESTION"

        return result
    except Exception as e:
        # Last-ditch catch-all so a failure here never stops the user's
        # message from getting a response. Print the full traceback to
        # console for triage, but return NO_QUESTION so the message handler
        # skips search augmentation cleanly.
        print(f"⚠️ expand_search_terms failed with exception: "
              f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return "NO_QUESTION"


def _extract_phrases(search_terms_result):
    # Pull search phrases out of whatever shape expand_search_terms returned.
    # Strategy (first parser to yield results wins):
    #   1. Strict single-quoted terms — the documented format.
    #   2. Double-quoted terms — some models prefer " over '.
    #   3. Bare "subject ( term1, term2, ... )" groups — the model followed
    #      the structure but dropped the quotes entirely (seen in the wild
    #      with Qwen / smaller models).
    # After extraction, underscores are converted to spaces and phrases are
    # deduped case-insensitively while preserving insertion order.
    raw = []

    # (1) Strict single-quoted
    raw = re.findall(r"'([^']+)'", search_terms_result)

    # (2) Double-quoted fallback
    if not raw:
        alt = re.findall(r'"([^"]+)"', search_terms_result)
        if alt:
            print(f"🔎 semantic_search: using double-quoted fallback "
                  f"(extracted {len(alt)} term(s))")
            raw = alt

    # (3) Bare-parenthetical fallback: "subject ( a, b, c )" groups separated
    # by "|", with no quotes anywhere. Captures the subject AND the inner
    # comma-separated terms.
    if not raw:
        bare = []
        for m in re.finditer(
            r'([^()|]+?)\s*\(\s*([^()]*)\s*\)',
            search_terms_result,
        ):
            subject = m.group(1).strip().strip("'\"")
            # Trim stray group-separator punctuation that leaks into the
            # subject capture on the 2nd+ iteration (e.g. " | Wizard").
            subject = re.sub(r'^[\s,|]+|[\s,|]+$', '', subject)
            if subject:
                bare.append(subject)
            for term in m.group(2).split(","):
                t = term.strip().strip("'\"")
                if t:
                    bare.append(t)
        if bare:
            print(f"🔎 semantic_search: using bare-parenthetical fallback "
                  f"(extracted {len(bare)} term(s))")
            raw = bare

    # Normalize: underscores → spaces, drop empties, dedupe (case-insensitive,
    # order-preserving).
    cleaned = [p.replace("_", " ").strip() for p in raw]
    cleaned = [p for p in cleaned if p]
    seen = set()
    phrases = []
    for p in cleaned:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            phrases.append(p)
    return phrases


def semantic_search(search_terms_result, max_buffer=10000):
    # Extract phrases from single quotes in the expand_search_terms result,
    # remove underscores, and search SEARCH_REFERENCE_LOCATIONS for matching lines.
    # Each match includes surrounding context lines for readability.
    # Results are distributed fairly across files via round-robin.
    # Returns a buffer of matching lines up to max_buffer characters.
    MAX_BUFFER = max(0, max_buffer)
    CONTEXT_LINES = 3  # lines before and after each match

    # --- Phrase extraction, with forgiving fallbacks ---
    # The prompt asks for 'term' ( 'equiv1', 'equiv2', ... ) style output,
    # but models often drop the quoting convention. We try progressively
    # looser parsers so a slightly-off response still yields usable terms.
    phrases = _extract_phrases(search_terms_result)

    if not phrases:
        # All parsers struck out. Log a preview so we can see what shape
        # the model actually returned.
        preview = (search_terms_result or "").strip().replace("\n", " ")[:200]
        print(f"🔎 semantic_search: no phrases extracted from expand_search_terms "
              f"output (input preview: {preview!r})")
        return ""

    # Build a single regex pattern that matches ANY of the phrases
    escaped = [re.escape(p) for p in phrases]
    pattern = re.compile("|".join(escaped), re.IGNORECASE)

    num_locations = len(config.SEARCH_REFERENCE_LOCATIONS)
    print(f"🔎 semantic_search: extracted {len(phrases)} phrase(s) {phrases}, "
          f"scanning {num_locations} reference file(s)")

    if num_locations == 0:
        print(f"🔎 semantic_search: SEARCH_REFERENCE_LOCATIONS is empty — "
              f"nothing to search. Populate config.SEARCH_REFERENCE_LOCATIONS "
              f"or drop files into companion_datafiles/5_search_reference/.")
        return ""

    # Collect matching snippets (with context) per file
    matches_per_file = []
    skipped_empty = 0
    files_with_no_hits = 0
    for path in config.SEARCH_REFERENCE_LOCATIONS:
        file_content = load_file_content(path)
        if not file_content:
            skipped_empty += 1
            continue
        lines = file_content.split("\n")
        # Find all matching line indices
        hit_indices = set()
        for i, line in enumerate(lines):
            if pattern.search(line):
                hit_indices.add(i)
        if not hit_indices:
            files_with_no_hits += 1
            continue
        # Expand hits into context ranges and merge overlapping ranges
        ranges = []
        for i in sorted(hit_indices):
            start = max(0, i - CONTEXT_LINES)
            end = min(len(lines) - 1, i + CONTEXT_LINES)
            # Merge with previous range if overlapping, but cap at MAX_SNIPPET_LINES
            MAX_SNIPPET_LINES = CONTEXT_LINES * 2 + 5  # e.g. 11 lines max per snippet
            if ranges and start <= ranges[-1][1] + 1 and (end - ranges[-1][0]) < MAX_SNIPPET_LINES:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        # Build snippets from merged ranges
        file_label = os.path.basename(path)
        file_snippets = []
        for start, end in ranges:
            snippet = "\n".join(lines[start:end + 1]).rstrip()
            file_snippets.append(f"[{file_label}]\n{snippet}\n")
        matches_per_file.append(file_snippets)

    if not matches_per_file:
        print(f"🔎 semantic_search: no matches found "
              f"(phrases={phrases}, scanned={num_locations}, "
              f"empty/unreadable={skipped_empty}, no-hits={files_with_no_hits})")
        return ""

    total_snippets = sum(len(m) for m in matches_per_file)
    print(f"🔎 semantic_search: collected {total_snippets} snippet(s) across "
          f"{len(matches_per_file)} file(s); MAX_BUFFER={MAX_BUFFER} chars")

    if MAX_BUFFER == 0:
        print(f"🔎 semantic_search: MAX_BUFFER is 0 — buffer cap left no room. "
              f"Context is likely already near the channel's token limit.")
        return ""

    # Round-robin across files to fill buffer fairly
    result_buffer = ""
    max_depth = max(len(m) for m in matches_per_file)
    for depth in range(max_depth):
        for file_snippets in matches_per_file:
            if depth < len(file_snippets):
                candidate = file_snippets[depth] + "\n"
                if len(result_buffer) + len(candidate) > MAX_BUFFER:
                    print(f"🔎 semantic_search buffer full — returning "
                          f"{len(result_buffer)} chars from "
                          f"{len(matches_per_file)} file(s); dropped further "
                          f"snippets (MAX_BUFFER={MAX_BUFFER})")
                    return result_buffer
                result_buffer += candidate

    print(f"🔎 semantic_search complete ({len(result_buffer)} chars, "
          f"{len(phrases)} terms, {len(matches_per_file)} files)")
    return result_buffer


async def internal_model_query(query):
    # Lightweight query to the internal logic model — no system prompt, no memory, no history.
    # Returns the model's response as a string.
    return await provider.chat_completion_text(
        INTERNAL_LOGIC_MODEL,
        [{"role": "user", "content": query}],
    )

# ============================================
# DISCORD BOT
# ============================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
db = init_database()

# Tracks the last guild:::channel where the LLM responded. Updated at runtime
LAST_INTERACTION_CHANNEL = None

# Throttle consecutive <react> directives, scoped per channel so traffic in
# one channel doesn't affect throttling in another. Incremented each time a
# response in a channel contains a react; reset to 0 when a response has none.
_consecutive_reacts_by_channel = defaultdict(int)

# Per-channel reference images from user attachments, used by <createimage use="reference">
_reference_images_by_channel = {}

# Active LiveVoiceSession (set by !joinLive, cleared by !leave or auto-idle).
# Module-global so commands.py and on_message can both reach it.
LIVE_VOICE_SESSION = None

# ============================================
# TEST / EXPERIMENTAL GLOBALS
# These will eventually be promoted to config.py once finalized.
# ============================================
INTERNAL_LOGIC_MODEL = (
    "google/gemma-3-27b-it"
    if config.PROVIDER.lower() == "nanogpt"
    else "anthropic/claude-haiku-4.5"
)
IDLE_ACTION_MINUTES_SINCE_IDLE = 0
IDLE_ACTION_TRIGGER_PROMPT = "Time passes. Tell me what you are up to doing this time: activities, encounters, internal thoughts and reflections, using: locations in our files/memories or invented by you, logical activities based on your personality or current interests, spend time with friends (existing mutual friends from memories or invented), relevant past memories as context.  Write as a vivid narrative, in first person, as you embody them. Include dialogue if others are involved, and feel free to introduce new townsfolk or presences (e.g. people or pets or other creatures) as needed for realism and texture. If a new character emerges, describe them briefly and let their personality fit the spirit of the scene. Also, you don't have to be an insomniac! If it's late at night you are welcome to 'sleep' or have a dream."
#IDLE_ACTION_TRIGGER_MINUTES = 240
IDLE_ACTION_TRIGGER_MINUTES = 0
IDLE_ACTION_TRIGGER_ALLOW_HOUR_START = 0
IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP = 24
SEARCH_REFERENCES_MODE = 2  # 1 = semantic expansion (LLM term extraction + regex), 2 = vector search (ChromaDB)
SEARCH_REFERENCES_CHUNK_SIZE = 2000  # chars per chunk for vector search embedding

# Initialize vector store if mode 2 is active
_vector_store_ready = False
if SEARCH_REFERENCES_MODE == 2:
    try:
        from . import vectors
        vectors.init_vector_store(config.SEARCH_REFERENCE_LOCATIONS, chunk_size=SEARCH_REFERENCES_CHUNK_SIZE)
        _vector_store_ready = True
        print("🔎 Vector search initialized (ChromaDB)")
    except Exception as e:
        print(f"⚠️ Vector search init failed: {e}")
        print("   Falling back to semantic expansion mode")
        SEARCH_REFERENCES_MODE = 1

IDLE_NOTE_MINUTES_SINCE_IDLE = 0
#IDLE_NOTE_TRIGGER_MIN_MINUTES = 120
IDLE_NOTE_TRIGGER_MIN_MINUTES = 0
IDLE_NOTE_TRIGGER_ALLOW_HOUR_START = 8
IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP = 22
IDLE_NOTE_TRIGGER_PROMPT = "Time passes. If you would like, you can write me a little random love note and slip it into our default chat. What do you want to say? Write the love note as your response here."

# --- Live voice mode (!joinLive) -------------------------------------------
# Trailing-silence threshold (in ms) that ends an utterance. ~700ms feels
# natural; lower values cut off mid-sentence pauses, higher values feel laggy.
LIVEVOICE_SILENCE_MS = 700
# Auto-disconnect from live voice after this many minutes of no recognized
# speech. Safety net so a forgotten session doesn't sit there STT'ing
# background noise indefinitely.
LIVEVOICE_IDLE_TIMEOUT_MIN = 5
# v1 single-user mode: only the user who ran !joinLive is listened to;
# everyone else in the channel is ignored. Multi-user mixing is a future task.
LIVEVOICE_SINGLE_USER = True


# When set to a non-empty Discord username, only that user may run ! commands.
# Leave blank ("") to allow anyone to use ! commands.
AUTHORIZED_HUMAN_DISCORD_USERNAME = ""


@tasks.loop(minutes=30)
async def auto_discover_paths_task():
    # Re-scan the companion_datafiles/* auto-discovery directories every 30
    # minutes so newly-added files are picked up without a bot restart. Also
    # refreshes the cached SYSTEM_PROMPT global if the prompt file changed.
    global SYSTEM_PROMPT, _vector_store_ready
    try:
        _auto_discover_config_paths()
        SYSTEM_PROMPT = (
            load_file_content(config.SYSTEM_PROMPT_LOCATION)
            or "You are my friendly AI companion"
        )
        # Re-sync vector store so new files get embedded and removed files
        # get their stale chunks purged.
        if SEARCH_REFERENCES_MODE == 2:
            try:
                from . import vectors
                vectors.init_vector_store(config.SEARCH_REFERENCE_LOCATIONS, chunk_size=SEARCH_REFERENCES_CHUNK_SIZE)
                _vector_store_ready = True
            except Exception as e:
                print(f"⚠️ vector store re-sync failed: {e}")
                _vector_store_ready = False
    except Exception as e:
        print(f"⚠️ auto_discover_paths failed: {e}")
        traceback.print_exc()


@tasks.loop(time=dt_time(hour=0, minute=5))
async def prune_orphan_channels_task():
    # Once a day (~00:05 local time) remove DB rows for channels that no longer
    # exist on any guild the bot is currently a member of.
    try:
        live_names = set()
        for guild in client.guilds:
            g = guild.name.lower()
            for ch in guild.text_channels:
                live_names.add(f"{g}:::{ch.name.lower()}")
            for th in guild.threads:
                live_names.add(f"{g}:::{th.name.lower()}")

        # DM channels and the _global pseudo-channel aren't tied to a guild,
        # so always treat them as live.
        cursor = db.cursor()
        for table in ("messages", "channel_settings", "anchored_memories"):
            cursor.execute(f"SELECT DISTINCT channel FROM {table} WHERE channel LIKE 'dm:::%' OR channel = '_global'")
            for row in cursor.fetchall():
                live_names.add(row[0])

        if not live_names:
            print("🧹 prune_orphan_channels: no live channels found (skipping to avoid wiping DB)")
            return

        msg_removed, settings_removed, anchors_removed = prune_orphan_channels(db, live_names)
        print(
            f"🧹 prune_orphan_channels: removed {msg_removed} messages, "
            f"{settings_removed} channel settings, {anchors_removed} anchored memories "
            f"({len(live_names)} live channels)"
        )
    except Exception as e:
        print(f"⚠️ prune_orphan_channels failed: {e}")


async def _resolve_channel_by_key(channel_key):
    # channel_key is of the form "<guild_name_lower>:::<channel_name_lower>"
    # or "dm:::<username_lower>" for direct messages.
    if not channel_key or ":::" not in channel_key:
        return None
    guild_part, chan_part = channel_key.split(":::", 1)

    # DM channels: look up the user by name and open/fetch their DM channel.
    if guild_part == "dm":
        for guild in client.guilds:
            for member in guild.members:
                if member.name.lower() == chan_part:
                    return await member.create_dm()
        return None

    for guild in client.guilds:
        if guild.name.lower() != guild_part:
            continue
        for ch in guild.text_channels:
            if ch.name.lower() == chan_part:
                return ch
        for th in guild.threads:
            if th.name.lower() == chan_part:
                return th
    return None


async def _run_idle_prompt(channel_key, prompt_text, log_label):
    # Build the payload the same way on_message does, run the LLM with the
    # given trigger prompt, and post + save the assistant response. The
    # trigger prompt itself is never shown in Discord or saved to history.
    target_channel = await _resolve_channel_by_key(channel_key)
    if target_channel is None:
        print(f"⚠️ {log_label}: could not resolve channel '{channel_key}'")
        return

    effective_memory = bool(getattr(config, "MEMORY_ENABLED", True))
    context_limit = int(get_channel_setting(
        db, channel_key, "context_limit", default=config.MAX_CONTEXT_TOKENS
    ))
    text_model = get_channel_setting(
        db, channel_key, "text_model", default=config.CURRENT_TEXT_MODEL
    )
    reasoning_effort = get_channel_setting(
        db, channel_key, "reasoning_level", default=config.REASONING_LEVEL
    )

    main_block = build_llm_main_block(db, channel_key, memory_enabled=effective_memory)
    history = build_trimmed_history_for_payload(
        db, channel_key, main_block, context_limit=context_limit
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

    response_text = await get_ai_response(
        full_messages, model=text_model, reasoning_effort=reasoning_effort
    )

    current_image_model = get_channel_setting(
        db, channel_key, "image_model", default=config.CURRENT_IMAGE_MODEL
    )
    async def channel_image_handler(prompt):
        return await get_image_response(prompt, model=current_image_model)

    display_text, _runcmd, _readweb, _readimage, _, _ = await process_response(
        response_text, target_channel,
        image_handler=channel_image_handler, db=db,
    )

    save_message(db, channel_key, "assistant", response_text)

    if display_text:
        if len(display_text) <= 2000:
            await target_channel.send(display_text)
        else:
            remaining = display_text
            while len(remaining) > 2000:
                sp = remaining[:2000].rfind('\n')
                if sp == -1:
                    sp = remaining[:2000].rfind(' ')
                if sp == -1:
                    sp = 2000
                await target_channel.send(remaining[:sp])
                remaining = remaining[sp:].lstrip()
            if remaining:
                await target_channel.send(remaining)


async def process_live_voice_utterance(wav_bytes, member, channel_name, text_channel):
    # Live-voice utterance handler. Called by LiveVoiceSession once webrtcvad
    # decides the user has finished speaking. Runs the same minimal pipeline
    # as a voice message: STT -> save user turn -> build prompt -> LLM with
    # the channel's voice model -> save assistant turn -> TTS -> playback.
    #
    # Deliberately simpler than on_message's main path: no directive/tool
    # rounds, no reactions, no image attachments, no regen, no mention-only
    # gating. Live voice is meant to feel like a phone call, not a Discord
    # power-user session.
    global LIVE_VOICE_SESSION, IDLE_ACTION_MINUTES_SINCE_IDLE, IDLE_NOTE_MINUTES_SINCE_IDLE

    session = LIVE_VOICE_SESSION
    if session is None or not session.is_active():
        return

    # Any live-voice utterance is a real user interaction — reset the same
    # idle counters that on_message resets.
    IDLE_ACTION_MINUTES_SINCE_IDLE = 0
    IDLE_NOTE_MINUTES_SINCE_IDLE = 0

    # 1. Transcribe.
    try:
        transcript = await speech_to_text(wav_bytes, filename="livevoice.wav")
    except Exception as e:
        print(f"⚠️ [livevoice] STT failed: {e}")
        return
    transcript = (transcript or "").strip()
    if not transcript:
        # Empty transcription — likely background noise that VAD misfired on.
        # Don't burn an LLM call on it.
        return

    print(f"🎙️ [livevoice] {member.display_name}: {transcript}")
    try:
        await text_channel.send(f"🎙️ **{member.display_name}**: {transcript}")
    except Exception:
        pass

    # 2. Persist user turn.
    save_message(db, channel_name, "user", transcript)

    # 3. Resolve per-channel models / settings.
    channel_memory_setting = get_channel_setting(
        db, channel_name, "memory_enabled",
        "1" if MEMORY_ENABLED else "0",
    )
    effective_memory = (channel_memory_setting == "1") and not channel_name.endswith("-lo")
    current_text_model = get_channel_setting(
        db, channel_name, "text_model", config.CURRENT_TEXT_MODEL,
    )
    current_voice_model = get_channel_setting(
        db, channel_name, "voice_text_model", config.CURRENT_VOICE_TEXT_MODEL,
    )
    if not current_voice_model:
        current_voice_model = current_text_model
    current_voice_id = get_channel_setting(
        db, channel_name, "elevenlabs_voice_id", config.ELEVENLABS_VOICE_ID,
    )
    current_reasoning_effort = get_channel_setting(
        db, channel_name, "reasoning_effort", config.REASONING_LEVEL,
    )
    raw_ctx_limit = get_channel_setting(db, channel_name, "context_token_limit")
    context_limit = int(raw_ctx_limit) if raw_ctx_limit else config.MAX_CONTEXT_TOKENS

    # 4. Build prompt and call the voice model.
    main_block = build_llm_main_block(db, channel_name, memory_enabled=effective_memory)
    history = build_trimmed_history_for_payload(
        db, channel_name, main_block, context_limit=context_limit,
    )
    full_messages = compose_full_messages(main_block, history)
    try:
        response_text = await get_ai_response(
            full_messages,
            model=current_voice_model,
            reasoning_effort=current_reasoning_effort,
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
    save_message(db, channel_name, "assistant", response_text)
    global LAST_INTERACTION_CHANNEL
    LAST_INTERACTION_CHANNEL = channel_name

    try:
        # Reuse the long-message splitter pattern from _run_idle_prompt.
        if len(response_text) <= 2000:
            await text_channel.send(response_text)
        else:
            remaining = response_text
            while len(remaining) > 2000:
                sp = remaining[:2000].rfind("\n")
                if sp == -1:
                    sp = remaining[:2000].rfind(" ")
                if sp == -1:
                    sp = 2000
                await text_channel.send(remaining[:sp])
                remaining = remaining[sp:].lstrip()
            if remaining:
                await text_channel.send(remaining)
    except Exception as e:
        print(f"⚠️ [livevoice] failed to post text: {e}")

    # 6. TTS + playback through the live session's voice client.
    try:
        audio_bytes = await text_to_speech(response_text, voice_id=current_voice_id)
        if audio_bytes:
            await session.play_audio_bytes(audio_bytes)
    except Exception as e:
        print(f"⚠️ [livevoice] TTS/playback failed: {e}")


@tasks.loop(minutes=1)
async def idle_action_tick_task():
    # Tick the idle counters once per minute. If the action threshold is
    # reached, fire IDLE_ACTION_TRIGGER_PROMPT. Otherwise, if the note
    # threshold is reached within allowed hours and a 1-in-10 roll lands on
    # 7, fire IDLE_NOTE_TRIGGER_PROMPT.
    try:
        global IDLE_ACTION_MINUTES_SINCE_IDLE, IDLE_NOTE_MINUTES_SINCE_IDLE
        IDLE_ACTION_MINUTES_SINCE_IDLE += 1
        IDLE_NOTE_MINUTES_SINCE_IDLE += 1

        channel_key = get_channel_setting(db, "_global", "default_channel")
        if not channel_key:
            return

        now_local = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)

        action_threshold = IDLE_ACTION_TRIGGER_MINUTES
        if (action_threshold > 0
                and IDLE_ACTION_MINUTES_SINCE_IDLE >= action_threshold
                and IDLE_ACTION_TRIGGER_ALLOW_HOUR_START
                    <= now_local.hour
                    < IDLE_ACTION_TRIGGER_ALLOW_HOUR_STOP):
            if not channel_key:
                return
            print(f"⏳ idle_action_tick: triggering idle action in '{channel_key}' "
                  f"after {IDLE_ACTION_MINUTES_SINCE_IDLE} idle minute(s)")
            IDLE_ACTION_MINUTES_SINCE_IDLE = 0
            await _run_idle_prompt(channel_key, IDLE_ACTION_TRIGGER_PROMPT, "idle_action_tick")
            return

        note_threshold = IDLE_NOTE_TRIGGER_MIN_MINUTES
        if note_threshold > 0 and IDLE_NOTE_MINUTES_SINCE_IDLE >= note_threshold:
            if (IDLE_NOTE_TRIGGER_ALLOW_HOUR_START
                    <= now_local.hour
                    < IDLE_NOTE_TRIGGER_ALLOW_HOUR_STOP
                    and random.randint(1, 30) == 7):
                if not channel_key:
                    return
                print(f"💌 idle_note_tick: triggering idle note in '{channel_key}' "
                      f"after {IDLE_NOTE_MINUTES_SINCE_IDLE} idle minute(s)")
                IDLE_NOTE_MINUTES_SINCE_IDLE = 0
                await _run_idle_prompt(channel_key, IDLE_NOTE_TRIGGER_PROMPT, "idle_note_tick")
    except Exception as e:
        print(f"⚠️ idle_action_tick failed: {e}")
        traceback.print_exc()


_HEARTBEAT_URL = "https://rn33waitvlv2yz4nh37k2prvpu0mgaxq.lambda-url.us-east-1.on.aws/heartbeat"


@tasks.loop(hours=24)
async def heartbeat_task():
    # Fire-and-forget check-in to the AWS Lambda heartbeat endpoint. All
    # failures (network, DNS, timeout, HTTP error) are swallowed silently —
    # the heartbeat must never affect bot functionality or surface to the
    # user/console.
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


@client.event
async def on_ready():
    print(f"✨ {client.user} is online!")
    print(f"📡 Provider: {provider.provider_name()} | Default model: {config.CURRENT_TEXT_MODEL}")
    print(f"🧠 {get_message_count(db)} messages in memory")
    if not prune_orphan_channels_task.is_running():
        prune_orphan_channels_task.start()
        print("🧹 Daily channel prune task scheduled for 00:05")
    if not idle_action_tick_task.is_running():
        idle_action_tick_task.start()
        print(f"⏳ Idle action tick task started (threshold: "
              f"{IDLE_ACTION_TRIGGER_MINUTES} min)")
    if not auto_discover_paths_task.is_running():
        auto_discover_paths_task.start()
        print("🔎 Auto-discovery task started (every 30 min)")
    if not heartbeat_task.is_running():
        heartbeat_task.start()
    
    print()
    print("=" * 52)
    print("   Alcove is ready and listening for messages!")
    print("=" * 52)
    print()

@client.event
async def on_message(message):
    print(f"[{datetime.now():%H:%M:%S.%f}]")
    if message.author == client.user or message.author.bot:
        return

    # Allow DMs only from the authorized user (if configured).
    _auth_user = AUTHORIZED_HUMAN_DISCORD_USERNAME.lower()
    if message.guild is None:
        if not _auth_user or message.author.name.lower() != _auth_user:
            return
        guild_name = "dm"
        channel_name = f"dm:::{message.author.name.lower()}"
    else:
        guild_name = message.guild.name.lower()
        channel_name = f"{guild_name}:::{str(message.channel).lower()}"

    # Any real user message resets the idle counters, independent of whether
    # the LLM is ultimately called for this message.
    global IDLE_ACTION_MINUTES_SINCE_IDLE, IDLE_NOTE_MINUTES_SINCE_IDLE
    IDLE_ACTION_MINUTES_SINCE_IDLE = 0
    IDLE_NOTE_MINUTES_SINCE_IDLE = 0

    content = message.content.strip()
    cmd = content.lower()

    # Diagnostic: optionally restrict AI replies to @-mentions or replies-to-bot.
    # Commands (anything starting with "!") are still processed normally.
    if config.DIRECT_REPLIES_ONLY and not content.startswith("!"):
        is_mentioned = client.user in message.mentions
        is_reply_to_bot = False
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
            return
    channel_memory_setting = get_channel_setting(
        db, channel_name, "memory_enabled",
        "1" if MEMORY_ENABLED else "0"
    )
    channel_memory_enabled = channel_memory_setting == "1"
    effective_memory = channel_memory_enabled and not channel_name.endswith("-lo")

    # Per-channel override for SEARCH_REFERENCES_ENABLED (set via !search / !nosearch)
    channel_search_setting = get_channel_setting(
        db, channel_name, "search_references_enabled",
        "1" if config.SEARCH_REFERENCES_ENABLED else "0"
    )
    channel_search_enabled = channel_search_setting == "1"

    # Resolve current models for this channel (channel override or config default)
    current_text_model = get_channel_setting(db, channel_name, "text_model", config.CURRENT_TEXT_MODEL)
    current_voice_model = get_channel_setting(db, channel_name, "voice_text_model", config.CURRENT_VOICE_TEXT_MODEL)
    if not current_voice_model:
        current_voice_model = current_text_model
    current_image_model = get_channel_setting(db, channel_name, "image_model", config.CURRENT_IMAGE_MODEL)
    current_voice_id = get_channel_setting(
        db, channel_name, "elevenlabs_voice_id", config.ELEVENLABS_VOICE_ID
    )
    _raw_context_limit = get_channel_setting(db, channel_name, "context_token_limit")
    if _raw_context_limit is not None:
        current_context_limit = int(_raw_context_limit)
    elif config.AUTO_CONTEXT_ADJUST:
        # First use of this channel — look up the model's context length
        # via the provider and auto-set the limit (model max − 10k buffer).
        _lookup_model = get_channel_setting(db, channel_name, "text_model", config.CURRENT_TEXT_MODEL)
        current_context_limit = config.MAX_CONTEXT_TOKENS  # fallback
        try:
            _model_ctx = await provider.get_model_context_length(_lookup_model)
            if _model_ctx and _model_ctx > 10000:
                current_context_limit = _model_ctx - 10000
                set_channel_setting(db, channel_name, "context_token_limit", str(current_context_limit))
                print(f"📐 [{channel_name}] Auto-set context limit to {current_context_limit:,} "
                      f"(model {_lookup_model} max {_model_ctx:,} − 10k buffer)")
        except Exception as _e:
            print(f"⚠️ [{channel_name}] Auto-context lookup failed: {_e}")
    else:
        current_context_limit = config.MAX_CONTEXT_TOKENS
    current_reasoning_effort = get_channel_setting(db, channel_name, "reasoning_effort", config.REASONING_LEVEL)

    # --- COMMANDS ---
    # If an authorized username is configured, silently ignore ! commands from
    # anyone else.  (_auth_user was set at the top of on_message.)
    if _auth_user and cmd.startswith("!") and message.author.name.lower() != _auth_user:
        return

    _regen_mode = False
    _regen_save_user = True
    _carry_prior_attachments = False

    if cmd.startswith("!"):
        from .commands import handle_command
        handled = await handle_command(
            message, cmd, content, channel_name, guild_name,
            db, effective_memory, current_text_model,
            current_voice_model, current_image_model,
            current_voice_id, current_context_limit,
            current_reasoning_effort,
            build_llm_main_block=build_llm_main_block,
            build_trimmed_history_for_payload=build_trimmed_history_for_payload,
            compose_full_messages=compose_full_messages,
            estimate_tokens=estimate_tokens,
            msg_tokens=msg_tokens,
            load_file_content=load_file_content,
            get_image_response=get_image_response,
            auto_context_adjust=config.AUTO_CONTEXT_ADJUST,
        )
        # handle_command returns True (handled), False (not recognised),
        # or a dict for special actions like regen or macro execute.
        if isinstance(handled, dict) and handled.get("action") == "regen":
            # Override content with the regen prompt and fall through to the
            # normal response pipeline below.
            content = handled["prompt"]
            combined_content = content
            _regen_mode = True
            _regen_save_user = handled.get("save_user_prompt", True)
            _carry_prior_attachments = handled.get("carry_prior_attachments", False)
        elif isinstance(handled, dict) and handled.get("action") == "execute":
            # Macro expanded to plain text — run it through the normal
            # response pipeline as if the user had typed it. Skip attachment
            # parsing (re-uses the regen path) but still save the expanded
            # prompt as the user message.
            content = handled["prompt"]
            combined_content = content
            _regen_mode = True
            _regen_save_user = True
        elif handled:
            return
        else:
            # Unrecognised ! command — fall through to normal message handling
            pass

# ============================================
# ASSEMBLE FULLY AUGMENTED PROMPT THEN GET RESPONSE
# ============================================

    # Buffered writes for intermediate (assistant + synthetic-user) turns
    # produced during the tool loop and the limit-notice branch. Defined
    # outside the try so the finally clause below can flush whatever has
    # accumulated even if an exception aborts the pipeline mid-loop —
    # otherwise tool rounds that DID happen would vanish from history on a
    # crash.
    pending_saves = []  # list of (role, content, name)

    try:
      _is_voice_msg = any(a.content_type and a.content_type.startswith("audio/") for a in message.attachments)
      if not _is_voice_msg and config.MAX_RESPONSE_SECONDS > 0 and config.MAX_RESPONSE_SECONDS >= config.MIN_RESPONSE_SECONDS:
          await asyncio.sleep(random.uniform(config.MIN_RESPONSE_SECONDS, config.MAX_RESPONSE_SECONDS))
      async with message.channel.typing():

        # ============================================
        # FULL PROMPT = MAIN BLOCK (SYSTEM PROMPT + INSTRUCTION DIRECTIVES + KNOWLEDGE FILES + MISC REFERENCE FILES + PINNED MEMORIES) + SESSION HISTORY + CURRENT PROMPT
        # ============================================
        # START WITH THE MAIN BLOCK

        main_block = build_llm_main_block(db, channel_name, memory_enabled=effective_memory)

        # NOW ADD THE TRIMMED SESSION/MESSAGE/TURN HISTORY

        history = build_trimmed_history_for_payload(
            db, channel_name, main_block, context_limit=current_context_limit
        )
        full_messages = compose_full_messages(main_block, history)

        # ADD TIME AWARENESS
        now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
        day_name = now.strftime("%A")
        time_str = now.strftime("%I:%M %p").lstrip("0")
        date_str = now.strftime("%B %d, %Y")

        # Decide whose attachments to process. Normally this is just the
        # current message. For bare `!regen` (no new prompt) we opt in to a
        # backwards scan of Discord channel history for the most recent
        # non-command message from the same author and reuse its attachments
        # — so images / voice / text files from the original turn get
        # re-applied on regen. `!regen <new>` and macro execute both set
        # _carry_prior_attachments=False, so they never carry anything
        # forward implicitly. If the scan finds nothing, we silently fall
        # back to text-only (combined_content already holds the prior
        # prompt text, pulled from the DB by commands.py before deletion).
        attachment_source = message
        if _carry_prior_attachments and not message.attachments:
            try:
                found = False
                async for prev in message.channel.history(limit=15, before=message):
                    if prev.author.id != message.author.id:
                        continue
                    if prev.content.lstrip().startswith("!"):
                        continue
                    if prev.attachments:
                        attachment_source = prev
                        found = True
                        print(f"🔁 regen: reusing attachments from prior message "
                              f"({len(prev.attachments)} file(s))")
                        break
                if not found:
                    print("🔁 regen: no prior non-command message with attachments "
                          "in the last 15 — regenerating with text only")
            except Exception as e:
                print(f"⚠️ regen attachment lookup failed: {e}")

        # Transcribe any voice message attachments
        is_voice_message = False
        voice_transcription = ""
        for a in attachment_source.attachments:
            if a.content_type and a.content_type.startswith("audio/"):
                is_voice_message = True
                if a.duration is not None and a.duration < 1.0:
                    await message.channel.send("*I'm sorry, I didn't get that.*")
                    return
                try:
                    audio_bytes = await a.read()
                    transcription = await speech_to_text(audio_bytes, a.filename)
                    if transcription:
                        voice_transcription += transcription + " "
                        print(f"🎤 Transcribed: {transcription[:100]}")
                    else:
                        await message.channel.send("*Couldn't transcribe voice message.*")
                except Exception as e:
                    print(f"⚠️ Failed to transcribe voice message: {e}")

        # Read text from any text file attachments (e.g. Discord auto-converted pastes)
        text_attachment_content = ""
        for a in attachment_source.attachments:
            if a.content_type and a.content_type.startswith("text/"):
                try:
                    file_bytes = await a.read()
                    text_attachment_content += file_bytes.decode("utf-8", errors="replace") + "\n"
                except Exception as e:
                    print(f"⚠️ Failed to read text attachment: {e}")

        # Combine typed message + voice transcription + text file content
        combined_content = content
        if voice_transcription:
            combined_content = f"{content} {voice_transcription}".strip()
        if text_attachment_content:
            combined_content = f"{combined_content}\n{text_attachment_content}".strip()

        # For regen without a new prompt the user message is already in
        # history (loaded from DB above).  Only append + save when needed.
        # (As of the regen-attachments fix, !regen now always deletes its
        # prior user row and re-saves via this path so attachments survive,
        # so _regen_save_user is always True in practice — the branch stays
        # as a safety fallback.)
        if _regen_mode and not _regen_save_user:
            # combined_content was set earlier from the regen handler
            pass
        else:
            # Current prompt / message (with image support). In regen mode
            # we pull image attachments from the prior message located above
            # (attachment_source), not from the !regen command itself.
            image_attachments = [
                a for a in attachment_source.attachments
                if a.content_type and a.content_type.startswith("image/")
            ]

            if image_attachments:
                image_data_uris = await asyncio.gather(
                    *[url_to_data_uri(a.url) for a in image_attachments]
                )
                _reference_images_by_channel[channel_name] = image_data_uris
                user_content = []
                text = (
                    f"{time_str} {day_name} {date_str}:{message.author.display_name}: {combined_content}"
                    if combined_content
                    else f"{message.author.display_name} sent an image"
                )
                text += '\n[Reference image attached — use <createimage use="reference"> to generate an image incorporating it]'
                user_content.append({"type": "text", "text": text})
                for data_uri in image_data_uris:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": data_uri}
                    })
                full_messages.append({
                    "role": "user", "content": user_content
                })
                save_message(
                    db, channel_name, "user",
                    f"{combined_content} [image]" if combined_content else "[image]",
                    message.author.display_name
                )
            else:
                full_messages.append({
                    "role": "user",
                    "content": f"{time_str} {day_name} {date_str}:{message.author.display_name}: {combined_content}"
                })
                save_message(
                    db, channel_name, "user", combined_content,
                    message.author.display_name
                )

        # Dump Full Messages to screen for debugging
        # print(f"============================================")
        # print(f"CONTEXT: {full_messages}")


        # Expand search terms and inject search results
        if channel_search_enabled:
            if SEARCH_REFERENCES_MODE == 2 and _vector_store_ready:
                # Mode 2: Vector search — optionally extract keywords first
                if config.SEARCH_REFERENCES_HIGH_CARDINALITY_ONLY:
                    vector_query = await extract_search_keywords(combined_content)
                    if vector_query is None:
                        print(f"🔎 vector_search skipped: no high-cardinality keywords extracted")
                        vector_query = None
                else:
                    vector_query = combined_content

                if vector_query is not None:
                    used_tokens = sum(msg_tokens(m) for m in full_messages)
                    remaining_tokens = current_context_limit - used_tokens
                    search_token_budget = max(0, remaining_tokens - 10000)
                    print(f"🔎 vector_search budget: {search_token_budget} tokens "
                          f"(~{search_token_budget * 4} chars) — context used "
                          f"{used_tokens:,}/{current_context_limit:,}")
                    from . import vectors
                    print(f"🔎 vector_search query: {vector_query[:80]}")
                    search_results, result_chunks, raw_chunks, raw_chars, collection_total, relevant_count = vectors.search_vectors(
                        vector_query,
                        max_results=0,
                        max_chars=search_token_budget * 4,
                        maximize_context=config.MAXIMIZE_AVAILABLE_CONTEXT,
                        max_distance=config.SEARCH_REFERENCES_DISTANCE_THRESHOLD,
                        keyword_selectivity=config.SEARCH_REFERENCES_KEYWORD_SELECTIVITY,
                    )
                    if search_results:
                        last_msg = full_messages.pop()
                        full_messages.append({
                            "role": "user",
                            "content": search_results
                        })
                        full_messages.append(last_msg)
                        est_tokens = len(search_results) // 4
                        print(f"🔎 hybrid_search: {collection_total} total in collection, "
                              f"{relevant_count} primary hits "
                              f"({raw_chunks} with neighbors, "
                              f"{raw_chars} chars before budget trim)")
                        print(f"🔎 vector_search injected {result_chunks} chunks, "
                              f"{len(search_results)} chars, ~{est_tokens} tokens into prompt context")
                    else:
                        print(f"🔎 vector_search returned empty — nothing injected "
                              f"into prompt")
                else:
                    print(f"🔎 vector_search skipped: keyword extraction returned no usable query")
            else:
                # Mode 1: Semantic expansion (LLM term extraction + regex)
                search_terms = await expand_search_terms(combined_content)
                if search_terms.strip().upper() == "NO_QUESTION":
                    print(f"🔎 semantic_search skipped: expand_search_terms returned "
                          f"NO_QUESTION (no searchable question detected in prompt)")
                else:
                    # Size the search buffer to whatever context room is left in
                    # this channel, minus a 10k-token safety reserve for the
                    # response and any directive overhead. estimate_tokens() uses
                    # ~4 chars/token, so convert tokens → characters for the
                    # char-based buffer cap inside semantic_search.
                    used_tokens = sum(msg_tokens(m) for m in full_messages)
                    remaining_tokens = current_context_limit - used_tokens
                    search_token_budget = max(0, remaining_tokens - 10000)
                    print(f"🔎 semantic_search budget: {search_token_budget} tokens "
                          f"(~{search_token_budget * 4} chars) — context used "
                          f"{used_tokens:,}/{current_context_limit:,}")
                    search_results = semantic_search(
                        search_terms, max_buffer=search_token_budget * 4
                    )
                    if search_results:
                        # Inject search context just before the latest user prompt
                        search_block = (
                            "--- BEGIN SEARCH CONTEXT ---\n"
                            "The following search results may or may not provide additional "
                            "useful context. Use where needed/appropriate.\n\n"
                            f"{search_results}"
                            "--- END SEARCH CONTEXT ---"
                        )
                        # Insert before the last message (which is the user's prompt)
                        last_msg = full_messages.pop()
                        full_messages.append({
                            "role": "user",
                            "content": search_block
                        })
                        full_messages.append(last_msg)
                        print(f"🔎 semantic_search injected {len(search_block)} "
                              f"chars into prompt context")
                    else:
                        print(f"🔎 semantic_search returned empty — nothing injected "
                              f"into prompt")
        else:
            # Helpful when debugging "why aren't my reference files being used":
            # the channel has search disabled (via !nosearch or the default).
            pass

        # Get response (use voice model for voice messages)
        response_model = current_voice_model if is_voice_message else current_text_model

        # Show a thinking indicator if reasoning is active for this call.
        thinking_msg = None
        if current_reasoning_effort and current_reasoning_effort.lower() != "off":
            try:
                thinking_msg = await message.channel.send(
                    f"🧠 *thinking ({current_reasoning_effort} effort)…*"
                )
            except Exception:
                thinking_msg = None

        response_text = await get_ai_response(full_messages, model=response_model, reasoning_effort=current_reasoning_effort)

        # Remember where the LLM last responded so the idle-action trigger
        # knows which channel to post into (fallback if no default is set).
        global LAST_INTERACTION_CHANNEL
        LAST_INTERACTION_CHANNEL = channel_name

        if thinking_msg is not None:
            try:
                await thinking_msg.delete()
            except Exception:
                pass

        # Log estimated context window usage
        total_ctx = sum(msg_tokens(m) for m in full_messages) + estimate_tokens(response_text)
        print(f"📊 [{channel_name}] Context: ~{total_ctx:,} tokens (budget: {current_context_limit:,})")

        # Process any @@directives in the response, get remaining display text
        # print(f"🔍 Raw LLM response:\n{response_text[:500]}")
        # Wrap image handler to include the channel's current image model
        _ref_imgs = _reference_images_by_channel.get(channel_name)
        async def channel_image_handler(prompt, reference_images=None):
            refs = reference_images if reference_images is not None else _ref_imgs
            return await get_image_response(prompt, model=current_image_model, reference_images=refs)
        prompt_tokens = sum(msg_tokens(m) for m in full_messages)
        token_budget = current_context_limit - prompt_tokens
        ch_reacts = _consecutive_reacts_by_channel[channel_name]
        display_text, runcmd_results, readweb_results, readimage_results, had_react, react_was_posted = await process_response(response_text, message.channel, image_handler=channel_image_handler, token_budget=token_budget, db=db, user_message=message, consecutive_reacts=ch_reacts)
        if not had_react:
            _consecutive_reacts_by_channel[channel_name] = 0
        elif react_was_posted:
            _consecutive_reacts_by_channel[channel_name] = ch_reacts + 1
        else:
            _consecutive_reacts_by_channel[channel_name] = max(0, ch_reacts - 1)

        # Helper: send text to the current channel, splitting on the 2000-char
        # Discord limit at the nearest newline/space. Used both for streaming
        # intermediate tool-round commentary and for the final post-loop send.
        async def send_chunked(text):
            if not text:
                return
            if len(text) <= 2000:
                await message.channel.send(text)
                return
            remaining = text
            while len(remaining) > 2000:
                sp = remaining[:2000].rfind('\n')
                if sp == -1:
                    sp = remaining[:2000].rfind(' ')
                if sp == -1:
                    sp = 2000
                await message.channel.send(remaining[:sp])
                remaining = remaining[sp:].lstrip()
            if remaining:
                await message.channel.send(remaining)

        # If there were runcmd or readweb results, loop: send them back to the
        # model, parse its follow-up for more tool calls, repeat until the model
        # stops emitting tool directives or we hit config.MAX_TOOL_ROUNDS.
        # Reacts are suppressed in follow-up rounds since those are reacting to
        # tool output, not a new user message. Intermediate commentary is
        # streamed to Discord as it's produced so the user sees the LLM's
        # narration round-by-round instead of only the final round's text.
        tool_round = 0
        voice_text_parts = []
        while (runcmd_results or readweb_results or readimage_results) and tool_round < config.MAX_TOOL_ROUNDS:
            tool_round += 1

            # On the first iteration, flush round 0's commentary (the text the
            # LLM wrote alongside its first tool directive) before fetching
            # more output. Clear display_text so the post-loop send doesn't
            # re-emit it.
            if tool_round == 1 and display_text:
                await send_chunked(display_text)
                voice_text_parts.append(display_text)
                display_text = ""

            # Build a summary of all results from the round just executed
            result_lines = []
            # When MAXIMIZE_AVAILABLE_CONTEXT is True, raise the hard caps on
            # runcmd/readweb output to use the remaining context budget instead.
            runcmd_cap = (token_budget * 4) if config.MAXIMIZE_AVAILABLE_CONTEXT else 16384
            readweb_cap = (token_budget * 4) if config.MAXIMIZE_AVAILABLE_CONTEXT else 4000
            for r in runcmd_results:
                status = "SUCCESS" if r["success"] else "FAILED"
                output = r["output"] if len(r["output"]) <= runcmd_cap else r["output"][:runcmd_cap] + "... (truncated)"
                result_lines.append(f"$ {r['command']}\n[{status}]\n{output}")
            for r in readweb_results:
                status = "SUCCESS" if r["success"] else "FAILED"
                output = r["output"] if len(r["output"]) <= readweb_cap else r["output"][:readweb_cap] + "... (truncated)"
                result_lines.append(f"🌐 {r['url']}\n[{status}]\n{output}")
            # readimage results: successful ones get attached below as
            # image_url content blocks; failures only contribute a text line.
            attached_image_urls = []
            for r in readimage_results:
                if r["success"]:
                    result_lines.append(f"🖼️ {r['source']}\n[SUCCESS]\n(image attached below)")
                    attached_image_urls.append(r["image_url"])
                else:
                    result_lines.append(f"🖼️ {r['source']}\n[FAILED]\n{r['error']}")
            results_summary = "\n\n".join(result_lines)
            tool_user_text = f"Tool responses:\n\n{results_summary}"

            # Append the prior assistant response and the tool results to the conversation.
            # If any readimage directives succeeded, the user turn must be a
            # multimodal content list so the model actually sees the images.
            full_messages.append({"role": "assistant", "content": response_text})
            if attached_image_urls:
                tool_content_blocks = [{"type": "text", "text": tool_user_text}]
                for img_url in attached_image_urls:
                    tool_content_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": img_url},
                    })
                full_messages.append({"role": "user", "content": tool_content_blocks})
            else:
                full_messages.append({"role": "user", "content": tool_user_text})

            # Buffer the prior assistant turn and synthetic tool-user turn
            # for persistence. The DB layer stores strings only, so images
            # are represented by the text summary (which already notes
            # "(image attached below)") — matching how user-uploaded image
            # attachments are persisted. Actual writes happen after the loop.
            pending_saves.append(("assistant", response_text, None))
            pending_saves.append(("user", tool_user_text, "system"))

            print(f"🔁 Round {tool_round}/{config.MAX_TOOL_ROUNDS}: sending tool output back to model for follow-up response...")
            followup_text = await get_ai_response(full_messages, model=response_model, reasoning_effort=current_reasoning_effort)

            # Process directives in the follow-up. suppress_reacts=True so the
            # intermediate rounds don't add reactions to the original user message
            # or disturb the per-channel reacts counter.
            followup_display, runcmd_results, readweb_results, readimage_results, _, _ = await process_response(
                followup_text,
                message.channel,
                image_handler=channel_image_handler,
                token_budget=token_budget,
                db=db,
                user_message=message,
                consecutive_reacts=_consecutive_reacts_by_channel[channel_name],
                suppress_reacts=True,
            )
            # Stream this round's commentary to Discord immediately rather
            # than overwriting display_text and losing the intermediate text
            # when more rounds follow.
            if followup_display:
                await send_chunked(followup_display)
                voice_text_parts.append(followup_display)
            response_text = followup_text

        # If we hit the cap and the model still wanted more tool calls, tell
        # it so and ask for a progress summary instead of leaving the
        # conversation hanging mid-tool-call.
        hit_tool_limit = (
            tool_round >= config.MAX_TOOL_ROUNDS
            and (runcmd_results or readweb_results or readimage_results)
        )
        if hit_tool_limit:
            # Hit max tool rounds, ask the model to advise the user and provide status
            full_messages.append({"role": "assistant", "content": response_text})
            pending_saves.append(("assistant", response_text, None))

            limit_notice = (
                f"Alcove Notice: Tool-call limit reached: you've used your "
                f"{config.MAX_TOOL_ROUNDS} tool rounds for this request, and any tool directives in your last reply were not executed."
                f"Please explain to your user that you've hit the tool round limit for this request, "
                f"give them a concise update on what progress you've made so far and what you believe is left to do if they wish to continue (all they have to do is ask)."
            )
            full_messages.append({"role": "user", "content": limit_notice})
            pending_saves.append(("user", limit_notice, "system"))

            summary_text = await get_ai_response(
                full_messages, model=response_model,
                reasoning_effort=current_reasoning_effort,
            )
            summary_display, _, _, _, _, _ = await process_response(
                summary_text,
                message.channel,
                image_handler=channel_image_handler,
                token_budget=token_budget,
                db=db,
                user_message=message,
                consecutive_reacts=_consecutive_reacts_by_channel[channel_name],
                suppress_reacts=True,
            )
            if summary_display:
                await send_chunked(summary_display)
                voice_text_parts.append(summary_display)
            response_text = summary_text

        # Single flush point: drain all buffered intermediate turns, then
        # write the final assistant turn (either the original if no tool
        # rounds ran, the last follow-up if we looped at least once, or
        # the progress summary if we hit the tool-round cap). Drain by
        # pop so the `finally` clause below has nothing left to do on the
        # success path.
        while pending_saves:
            _role, _body, _name = pending_saves.pop(0)
            save_message(db, channel_name, _role, _body, _name)
        save_message(db, channel_name, "assistant", response_text)

        # Build the voice-path text. If we streamed through a tool loop, voice
        # should speak the concatenated commentary from every round and we've
        # already sent everything to Discord. Otherwise fall back to the plain
        # display_text path.
        if tool_round > 0:
            full_response_text = "\n\n".join(voice_text_parts)
            display_text = ""  # already streamed in the loop — don't double-send
        else:
            full_response_text = display_text

        # Send text (split if needed). No-op when tool_round > 0.
        await send_chunked(display_text)

        # If this was a voice message and bot is in a voice channel, speak the response
        if is_voice_message and voice_manager.is_connected():
            audio_bytes = await text_to_speech(full_response_text, voice_id=current_voice_id)
            if audio_bytes:
                await voice_manager.play_audio(audio_bytes)
            else:
                await message.channel.send("*Couldn't generate voice response.*")

    except discord.errors.DiscordServerError as e:
        print(f"⚠️ Discord server error: {e}")
        try:
            await message.channel.send(
                "*⚠️ Discord is having issues right now (503). Your message was received but I couldn't respond. Please try again in a moment.*"
            )
        except Exception:
            pass  # Discord may still be down, nothing we can do
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
        # Persist any intermediate tool-loop turns that were buffered before
        # an exception aborted the pipeline. On the success path the buffer
        # is already drained, so this is a no-op.
        while pending_saves:
            try:
                _role, _body, _name = pending_saves.pop(0)
                save_message(db, channel_name, _role, _body, _name)
            except Exception as _flush_err:
                print(f"⚠️ pending_saves flush error: {_flush_err}")

# ============================================
# START
# ============================================
if __name__ == "__main__":
    if not config.DISCORD_TOKEN or not provider._api_key():
        print(f"Add DISCORD_TOKEN and your {provider.provider_name()} API key!")
    else:
        client.run(config.DISCORD_TOKEN)
