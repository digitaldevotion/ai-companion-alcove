# ============================================
# Alcove — search.py
# Search, keyword extraction, and search context injection
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import os
import re
import traceback

from . import provider
from . import state
import config
from .companions import load_file_content, get_companion_resolved_locations
from .database import get_channel_companion
from .utils import estimate_tokens, msg_tokens, tokens_to_bytes

LIGHTWEIGHT_LOGIC_MODEL = (
    "~anthropic/claude-haiku-latest"
    if provider.get_provider() == "openrouter"
    else "anthropic/claude-haiku-latest"
)


async def lightweight_model_query(query):
    # Minimal query to the lightweight logic model — no system prompt, no memory, no history.
    # Returns the model's response as a string.
    return await provider.chat_completion_text(
        LIGHTWEIGHT_LOGIC_MODEL,
        [{"role": "user", "content": query}],
    )


async def extract_search_keywords(prompt):
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
        result = await lightweight_model_query(query)
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

        if len(cleaned) > 150 or '\n' in cleaned or '**' in cleaned or '##' in cleaned:
            print(f"🔑 extract_search_keywords: result too long or formatted, likely not keywords — skipping")
            return None

        return cleaned
    except Exception as e:
        print(f"⚠️ extract_search_keywords failed: {type(e).__name__}: {e}")
        return None


async def expand_search_terms(prompt):
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
        result = await lightweight_model_query(query)
        print("--------------------------------")
        print(f"🔍 expand_search_terms input: {prompt}")
        print(f"🔍 expand_search_terms result: {result}")

        if isinstance(result, str) and result.startswith("*Error"):
            print(f"⚠️ expand_search_terms: provider returned an error "
                  f"envelope, treating as NO_QUESTION. Details: {result[:300]}")
            return "NO_QUESTION"

        if not isinstance(result, str) or not result.strip():
            print(f"⚠️ expand_search_terms: empty or non-string result "
                  f"({type(result).__name__}), treating as NO_QUESTION")
            return "NO_QUESTION"

        return result
    except Exception as e:
        print(f"⚠️ expand_search_terms failed with exception: "
              f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return "NO_QUESTION"


def _extract_phrases(search_terms_result):
    raw = []
    raw = re.findall(r"'([^']+)'", search_terms_result)
    if not raw:
        alt = re.findall(r'"([^"]+)"', search_terms_result)
        if alt:
            print(f"🔎 semantic_search: using double-quoted fallback "
                  f"(extracted {len(alt)} term(s))")
            raw = alt
    if not raw:
        bare = []
        for m in re.finditer(
            r'([^()|]+?)\s*\(\s*([^()]*)\s*\)',
            search_terms_result,
        ):
            subject = m.group(1).strip().strip("'\"")
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


def semantic_search(search_terms_result, reference_locations, max_buffer=10000):
    MAX_BUFFER = max(0, max_buffer)
    if config.SEARCH_REFERENCES_MAX_RETURN_BYTES > 0:
        MAX_BUFFER = min(MAX_BUFFER, config.SEARCH_REFERENCES_MAX_RETURN_BYTES)
    CONTEXT_LINES = 3

    phrases = _extract_phrases(search_terms_result)

    if not phrases:
        preview = (search_terms_result or "").strip().replace("\n", " ")[:200]
        print(f"🔎 semantic_search: no phrases extracted from expand_search_terms "
              f"output (input preview: {preview!r})")
        return ""

    escaped = [re.escape(p) for p in phrases]
    pattern = re.compile("|".join(escaped), re.IGNORECASE)

    num_locations = len(reference_locations)
    print(f"🔎 semantic_search: extracted {len(phrases)} phrase(s) {phrases}, "
          f"scanning {num_locations} reference file(s)")

    if num_locations == 0:
        print(f"🔎 semantic_search: reference_locations is empty — "
              f"nothing to search.")
        return ""

    matches_per_file = []
    skipped_empty = 0
    files_with_no_hits = 0
    for path in reference_locations:
        file_content = load_file_content(path)
        if not file_content:
            skipped_empty += 1
            continue
        lines = file_content.split("\n")
        hit_indices = set()
        for i, line in enumerate(lines):
            if pattern.search(line):
                hit_indices.add(i)
        if not hit_indices:
            files_with_no_hits += 1
            continue
        ranges = []
        for i in sorted(hit_indices):
            start = max(0, i - CONTEXT_LINES)
            end = min(len(lines) - 1, i + CONTEXT_LINES)
            MAX_SNIPPET_LINES = CONTEXT_LINES * 2 + 5
            if ranges and start <= ranges[-1][1] + 1 and (end - ranges[-1][0]) < MAX_SNIPPET_LINES:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
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
          f"{len(matches_per_file)} file(s); MAX_BUFFER={MAX_BUFFER} bytes")

    if MAX_BUFFER == 0:
        print(f"🔎 semantic_search: MAX_BUFFER is 0 — buffer cap left no room. "
              f"Context is likely already near the channel's token limit.")
        return ""

    result_buffer = ""
    result_bytes = 0
    max_depth = max(len(m) for m in matches_per_file)
    for depth in range(max_depth):
        for file_snippets in matches_per_file:
            if depth < len(file_snippets):
                candidate = file_snippets[depth] + "\n"
                candidate_bytes = len(candidate.encode("utf-8"))
                if result_bytes + candidate_bytes > MAX_BUFFER:
                    print(f"🔎 semantic_search buffer full — returning "
                          f"{result_bytes} bytes from "
                          f"{len(matches_per_file)} file(s); dropped further "
                          f"snippets (MAX_BUFFER={MAX_BUFFER})")
                    return result_buffer
                result_buffer += candidate
                result_bytes += candidate_bytes

    print(f"🔎 semantic_search complete ({result_bytes} bytes, "
          f"{len(phrases)} terms, {len(matches_per_file)} files)")
    return result_buffer


# ============================================
# Search context injection
# ============================================

def _calculate_search_budget(full_messages, context_limit, channel_key=None):
    # channel_key matters: without it msg_tokens/tokens_to_bytes fall back
    # to the 4-bytes-per-token default instead of the channel's calibrated
    # ratio, undercounting used tokens on token-dense conversations and
    # letting search results over-inject past the context budget.
    used_tokens = sum(msg_tokens(m, channel_key=channel_key) for m in full_messages)
    remaining_tokens = context_limit - used_tokens
    _reserve = max(10000, int(remaining_tokens * 0.07))
    search_token_budget = max(0, remaining_tokens - _reserve)
    budget_max_chars = tokens_to_bytes(search_token_budget, channel_key=channel_key)
    if config.SEARCH_REFERENCES_MAX_RETURN_BYTES > 0:
        budget_max_chars = min(budget_max_chars, config.SEARCH_REFERENCES_MAX_RETURN_BYTES)
    return search_token_budget, budget_max_chars, used_tokens


def _wrap_search_context(raw_results):
    return (
        "--- BEGIN SEARCH CONTEXT ---\n"
        "The following search results may or may not provide additional "
        "useful context. Use where needed/appropriate.\n\n"
        f"{raw_results}"
        "--- END SEARCH CONTEXT ---"
    )


def _inject_before_last_message(full_messages, content):
    last_msg = full_messages.pop()
    full_messages.append({"role": "user", "content": content})
    full_messages.append(last_msg)


async def inject_search_context(full_messages, combined_content, ctx, search_references_mode):
    if not ctx.channel_search_enabled:
        return full_messages

    locs = get_companion_resolved_locations(ctx.db, ctx.channel_name)
    active_companion = get_channel_companion(ctx.channel_name)

    if search_references_mode == 2:
        from . import vectors
        # Skip the search step entirely while a (re)build is in progress so
        # the bot stays responsive to prompts during potentially long
        # embeddings. The flag is set/cleared by vectors.init_vector_store
        # under state._vector_init_lock.
        if state.SEARCH_INITIALIZING:
            print(f"🔎 vector_search skipped — vector store initializing")
            return full_messages

        # If the active companion's vector store isn't loaded yet, kick off
        # a background init (fire-and-forget) and skip search for this
        # message. Subsequent messages skip via the SEARCH_INITIALIZING gate
        # above until the build completes, then search works normally.
        if getattr(vectors, "_active_companion_name", None) != active_companion:
            print(f"🔎 vector_search skipped — initializing vector store for "
                  f"'{active_companion}' in background")
            asyncio.create_task(asyncio.to_thread(
                vectors.init_vector_store,
                locs["SEARCH_REFERENCE_LOCATIONS"],
                companion_name=active_companion,
            ))
            return full_messages

        if vectors.is_available():
            if config.SEARCH_REFERENCES_HIGH_CARDINALITY_ONLY:
                vector_query = await extract_search_keywords(combined_content)
                if vector_query is None:
                    print(f"🔎 vector_search skipped: no high-cardinality keywords extracted")
                    vector_query = None
            else:
                vector_query = combined_content

            # Re-check after the keyword-extraction await: a !switchCompanion
            # completing in that window swaps vectors._collection to the new
            # companion, and querying it here would inject the WRONG
            # persona's reference content into this channel's prompt.
            if (vector_query is not None
                    and getattr(vectors, "_active_companion_name", None) != active_companion):
                print(f"🔎 vector_search skipped — active companion changed "
                      f"during keyword extraction")
                return full_messages

            if vector_query is not None:
                search_token_budget, budget_max_chars, used_tokens = _calculate_search_budget(
                    full_messages, ctx.current_context_limit,
                    channel_key=ctx.channel_name,
                )
                print(f"🔎 vector_search budget: {search_token_budget} tokens "
                      f"(~{tokens_to_bytes(search_token_budget, channel_key=ctx.channel_name)} bytes) — context used "
                      f"{used_tokens:,}/{ctx.current_context_limit:,}")
                print(f"🔎 vector_search query: {vector_query[:80]}")
                # Run off the event loop: search_vectors embeds the query
                # through the ONNX model (lazy-loading ~2.3 GB on first use),
                # and blocking the loop here freezes heartbeats, typing
                # indicators, and every other channel for seconds per search.
                search_results, result_chunks, raw_chunks, raw_chars, collection_total, relevant_count = await asyncio.to_thread(
                    vectors.search_vectors,
                    vector_query,
                    max_results=0,
                    max_chars=budget_max_chars,
                    maximize_context=config.MAXIMIZE_AVAILABLE_CONTEXT,
                    max_distance=config.SEARCH_REFERENCES_DISTANCE_THRESHOLD,
                    keyword_selectivity=config.SEARCH_REFERENCES_KEYWORD_SELECTIVITY,
                )
                if search_results:
                    search_block = _wrap_search_context(search_results)
                    _inject_before_last_message(full_messages, search_block)
                    est_tokens = estimate_tokens(search_results)
                    print(f"🔎 hybrid_search: {collection_total} total in collection, "
                          f"{relevant_count} primary hits "
                          f"({raw_chunks} with neighbors, "
                          f"{raw_chars} bytes before budget trim)")
                    print(f"🔎 vector_search injected {result_chunks} chunks, "
                          f"{len(search_results.encode('utf-8'))} bytes, ~{est_tokens} tokens into prompt context")
                else:
                    print(f"🔎 vector_search returned empty — nothing injected "
                          f"into prompt")
            else:
                print(f"🔎 vector_search skipped: keyword extraction returned no usable query")
    else:
        search_terms = await expand_search_terms(combined_content)
        if search_terms.strip().upper() == "NO_QUESTION":
            print(f"🔎 semantic_search skipped: expand_search_terms returned "
              f"NO_QUESTION (no searchable question detected in prompt)")
        else:
            search_token_budget, sem_max_buffer, used_tokens = _calculate_search_budget(
                full_messages, ctx.current_context_limit,
                channel_key=ctx.channel_name,
            )
            print(f"🔎 semantic_search budget: {search_token_budget} tokens "
                  f"(~{tokens_to_bytes(search_token_budget, channel_key=ctx.channel_name)} bytes) — context used "
                  f"{used_tokens:,}/{ctx.current_context_limit:,}")
            search_results = semantic_search(
                search_terms, locs["SEARCH_REFERENCE_LOCATIONS"], max_buffer=sem_max_buffer
            )
            if search_results:
                search_block = _wrap_search_context(search_results)
                _inject_before_last_message(full_messages, search_block)
                print(f"🔎 semantic_search injected {len(search_block.encode('utf-8'))} "
                      f"bytes into prompt context")
            else:
                print(f"🔎 semantic_search returned empty — nothing injected "
                      f"into prompt")

    return full_messages
