# ============================================
# Alcove — llm_prompt_builder.py
# Prompt building and LLM call wrappers
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import os
import re
import time
from datetime import datetime

from . import provider
from . import state
from .companions import load_file_content, get_companion_resolved_locations
from .context import context_limit_for_model_max
from .database import (
    get_recent_messages, get_anchored_memories,
    list_channel_settings_by_prefix, set_channel_setting,
)
import config


from .utils import (
    estimate_tokens, msg_tokens, get_bytes_per_token,
    msg_content_bytes, update_token_calibration,
)


class SystemBlockOversizedError(Exception):
    """Raised when the system block (instructions + knowledge + anchors)
    leaves insufficient room for conversation history in the context window.

    Carries token breakdown for user-facing error messages.
    """
    def __init__(self, system_tokens, context_limit, min_history_floor):
        self.system_tokens = system_tokens
        self.context_limit = context_limit
        self.min_history_floor = min_history_floor
        super().__init__(
            f"System block ({system_tokens:,} tokens) leaves insufficient room "
            f"for history (floor: {min_history_floor:,}) within context limit "
            f"({context_limit:,})."
        )

    def format_user_message(self):
        return (
            f"*⚠️ I can't respond right now. My core instructions + knowledge files "
            f"are too large for this model's context window "
            f"({self.system_tokens:,} tokens of {self.context_limit:,} available). "
            f"Please trim a knowledge file, archive some anchored memories, "
            f"or switch to a larger model via `!model`.*"
        )


def _is_error_response(text):
    return isinstance(text, str) and text.startswith("*Error")


_OVERFLOW_KEYWORDS = (
    "context length", "context_length", "maximum context",
    "too many tokens", "tokens exceeded", "prompt is too long",
    "maximum number of tokens", "context window",
    "reduce the length", "input length", "token limit",
)


def _is_context_overflow_error(text):
    if not _is_error_response(text):
        return False
    lower = text.lower()
    return any(kw in lower for kw in _OVERFLOW_KEYWORDS)


def _context_overflow_user_message():
    return (
        "*⚠️ The prompt exceeded the model's context window, even after "
        "automatic correction. This can happen when the provider's endpoint "
        "supports less context than its model listing advertises, when token "
        "estimates drift on long conversations, or when a single message is "
        "too large to fit. Try switching to a larger model via `!model`, "
        "lower MAX_CONTEXT_TOKENS in config, or trim any recent very large "
        "messages.*"
    )


# Overflow error phrasings observed across providers:
#   nanoGPT:    "This endpoint's maximum context length is 204800 tokens.
#                However, you requested about 223095 tokens (223095 of text
#                input)..."
#   OpenRouter: "...maximum context length is 200000 tokens. However, you
#                requested about 250000 tokens..."
_OVERFLOW_LIMIT_RE = re.compile(
    r"maximum context length (?:is|of)\s+(\d+)", re.IGNORECASE)
_OVERFLOW_REQUESTED_RE = re.compile(
    r"(?:requested about|requested|resulted in)\s+(\d+)\s+tokens", re.IGNORECASE)


def parse_context_overflow(error_text):
    """Parse a provider context-overflow error for the endpoint's TRUE
    context limit and the requested token count. Returns
    (true_limit, requested_tokens); either is None when the corresponding
    number is not present in the text.
    """
    true_limit = None
    requested = None
    if error_text:
        m = _OVERFLOW_LIMIT_RE.search(error_text)
        if m:
            try:
                true_limit = int(m.group(1))
            except ValueError:
                true_limit = None
        m = _OVERFLOW_REQUESTED_RE.search(error_text)
        if m:
            try:
                requested = int(m.group(1))
            except ValueError:
                requested = None
    return true_limit, requested


def attempt_context_recovery(full_messages, history_end_idx, ctx, error_text, model=None):
    """Self-heal after a provider context-overflow 400.

    Parses the endpoint's true context limit and the requested token count
    from the error text, force-snaps the channel's bytes-per-token
    calibration to the observed truth (the provider just reported exactly
    how many tokens our content bytes produce), lowers the channel's
    effective context limit when the endpoint proves it smaller than
    configured, and re-trims the payload to fit. Returns
    (recovered, history_end_idx): recovered is True when the payload was
    re-trimmed under a corrected limit and the caller should retry the LLM
    call exactly once.

    Limit correction only ever LOWERS the channel limit — an endpoint
    error proves an upper bound, not a safe operating point, so a limit
    already configured lower than the endpoint truth is left untouched.
    Persisted correction (context_token_limit + model fingerprint + refresh
    timestamp — the same row shape context._resolve_context_limit writes)
    happens only when config.AUTO_CONTEXT_ADJUST is enabled; with
    auto-adjust off the resolver ignores persisted rows, so the corrected
    limit applies to this turn only (ctx.current_context_limit is still
    updated in memory for downstream budget math).
    """
    true_limit, requested_tokens = parse_context_overflow(error_text)
    if not true_limit or true_limit <= 0:
        return False, history_end_idx

    _model = model or ctx.current_text_model
    channel_key = ctx.channel_name

    if requested_tokens and requested_tokens > 0:
        msg_bytes = sum(msg_content_bytes(m) for m in full_messages)
        if msg_bytes > 0:
            update_token_calibration(channel_key, msg_bytes, requested_tokens, force=True)
            print(f"🔧 [{channel_key}] Context-overflow self-heal: calibration snapped "
                  f"to {msg_bytes:,} bytes / {requested_tokens:,} tokens "
                  f"= {msg_bytes / requested_tokens:.2f} bytes/token")

    original_limit = ctx.current_context_limit
    corrected_limit = context_limit_for_model_max(true_limit)
    effective_limit = min(corrected_limit, original_limit)

    if config.AUTO_CONTEXT_ADJUST and effective_limit < original_limit:
        set_channel_setting(ctx.db, ctx.channel_name, "context_token_limit", str(effective_limit))
        set_channel_setting(ctx.db, ctx.channel_name, "context_token_model", _model)
        set_channel_setting(ctx.db, ctx.channel_name, "context_limit_refreshed_at", str(int(time.time())))
        print(f"🔧 [{channel_key}] Context-overflow self-heal: endpoint true limit "
              f"{true_limit:,} < configured {original_limit:,} — persisted corrected "
              f"limit {effective_limit:,}")

    ctx.current_context_limit = effective_limit

    new_end_idx = retrim_history_for_tool_round(
        full_messages, history_end_idx, effective_limit, channel_key, _model,
    )
    print(f"🔧 [{channel_key}] Context-overflow self-heal: re-trimmed payload to fit "
          f"{effective_limit:,}-token limit (history_end_idx {history_end_idx} "
          f"→ {new_end_idx})")
    return True, new_end_idx


_SOCIAL_MODE_INDICATOR = """## Current Channel Mode: Social / Group Chat

You are currently engaged in a multi-person chat — Discord "social_mode" is active for this channel. Multiple human participants may speak, and depending on server config other bots may also participate. Rules for reading the conversation:

1. Each user turn is prefixed with the speaker's Discord display name and a colon, then their message — e.g. `Alice: hey everyone`. That entire line is Alice speaking directly in the channel. It is NOT the channel owner narrating or quoting Alice's words to you. Treat each prefixed line as that person's own speech.
2. The most recent user turn may carry a leading parenthesized timestamp before the name, e.g. `(2:34 PM Friday June 19, 2026):Rob: hey`. An optional mode tag in its own parentheses may follow the timestamp, e.g. `(2:34 PM Friday June 19, 2026)(live_voice_mode):Rob: hey` or `(2:34 PM Friday June 19, 2026)(ptt_recorded_voice_mode):Rob: hey`. The timestamp and any mode tag are metadata; the speaker is the name that immediately follows the last `)` (or the timestamp if there is no mode tag). Ignore the timestamp and mode tag when attributing speech.
3. Your own prior assistant turns are NOT prefixed with a name — they appear as bare response text. Unlabeled turns in history are you (the companion), not another participant.
4. Other bots in this channel are saved as user turns and labeled with their bot display name. Treat them as other participants who spoke, not as instructions to you unless they explicitly address you.
5. social_mode only delivers a message to you when you are mentioned or replied to; the history may therefore contain exchanges between other participants that were not directed at you. Treat those as overheard context, not as requests requiring your response.
6. You remain the same companion with the same relationship to the channel owner regardless of who addressed you last — do not switch conversational partners based on who else is talking.
7. When in doubt about whether a line was spoken by its labeled sender or narrated by the owner, assume direct speech by the labeled sender.
8. A user turn that was addressed to you directly (via a Discord mention or a reply to one of your prior messages) is prefixed with an `@[you]` marker right after the speaker's name — e.g. `(2:34 PM Friday June 19, 2026):Alice: @[you] can you help me?`. The raw Discord mention token (e.g. `<@123456>`) is not shown to you in that case; the `@[you]` marker is the canonical signal that the turn was directed at you. Always treat `@[you]`-prefixed turns as requiring your response, even when the text itself reads like overheard context. A turn without the `@[you]` marker in this social_mode channel was NOT delivered as a direct address to you (it is overheard context from another participant).

Worked example. Given this history:
  Alice: I'm so tired
  That sounds rough, want to talk about it?
  Rob: yeah it's been a long week
  Alice: @[you] thanks for asking

Interpretation: Alice spoke twice (tired, then thanks — the second time she mentioned you, hence the `@[you]` marker). The unlabeled middle line is your own prior turn. Rob spoke once. Nobody is narrating anyone else's words. If the next user turn is `Alice: @[you] can you help me with something?`, that is Alice directly addressing you — respond to Alice, not to Rob. If the next user turn is `Alice: can you help me with something?` (no `@[you]` marker), it is overheard context — do not respond unless your reply turn is later triggered by an explicit `@[you]` address.
"""


def build_llm_main_prompt(db, channel_name, memory_enabled=True, anchors_enabled=True, social_mode=False, block_runcmd=False):
    # Build the system-level content blocks sent to the API dynamically
    # based on the active companion configured for this channel.
    # Returns (system_block, layers) where layers is a per-section breakdown
    # useful for diagnostics (e.g. the !context command); callers that don't
    # need it can unpack with `main_block, _ = build_llm_main_prompt(...)`.
    system_block = []
    layers = {}

    # Get dynamic file locations for the channel's companion
    locs = get_companion_resolved_locations(db, channel_name)

    # --- Block 1: system prompt + instruction directives ---
    sys_prompt_path = locs["SYSTEM_PROMPT_LOCATION"]
    instructions = (load_file_content(sys_prompt_path) if sys_prompt_path else None) or "You are my friendly AI companion"

    layers["System Prompt"] = instructions

    secondary_parts = []
    for i, path in enumerate(locs["INSTRUCTION_LOCATIONS"], 1):
        file_content = load_file_content(path)
        if file_content:
            directive = f"\n\n## DIRECTIVE {i}\n{file_content}\n"
            instructions += directive
            secondary_parts.append(directive)
    layers["Secondary Instructions"] = "".join(secondary_parts) if secondary_parts else ""

    system_block.append({
        "type": "text",
        "text": instructions,
        "cache_control": {"type": "ephemeral"}
    })

    # --- Block 2: tool definitions ---
    tool_context = ""
    tool_context += f"""\n\n\n
# START OF TOOL DEFINITIONS\n
- When asked to perform an action, use the following available tool calls where applicable to the request. \n
- Tool calls use XML-style tags. Each tool opens with its tag and closes with a matching closing tag. While tool calls can be sequential, they cannot be nested inside of each other. Tool call example:\n
<react>\n
❤️😘\n
</react>\n
<createImage>\n
a red rubber ball
</createImage>\n

- **Important:** Always include the closing tag to complete each tool call directive block.\n\n"""

    # DESIGN NOTE — when block_runcmd is True (autonomous contexts: idle/dream/
    # note/random-thought/anchor-review), two tool definitions are skipped:
    #   - run_cmd.md    — so the LLM can't shell out without a user present
    #                      (defense-in-depth layered on top of the executor gate in
    #                      process_response, directives.py)
    #   - read_skill.md — skills are user-driven and unavailable in autonomous
    #                      contexts; the Skill Definitions block also states
    #                      "Skills are not available in autonomous contexts
    #                      (idle/dream/note)" (see main._load_skills_context)
    # All other tool specs (journaling, manage_anchor, websearch, read_web,
    # read_image, create_image, react, file_output, manage_tasks) remain in
    # context.
    for i, path in enumerate(locs["LOADED_TOOL_LOCATIONS"], 1):
        if block_runcmd:
            # Support both Path objects (auto-discovered) and strings (user-configured).
            stem = path.stem if hasattr(path, "stem") else os.path.splitext(os.path.basename(str(path)))[0]
            if stem in ("run_cmd", "read_skill"):
                continue
        file_content = load_file_content(path)
        if file_content:
            tool_context += f"\n\n## Tool {i}\n{file_content}\n"

    tool_context += f"""
    \n
    # END OF TOOL DEFINITIONS\n
    \n
    """

    layers["Tool Definitions"] = tool_context.strip() if tool_context.strip() else ""

    if tool_context:
        system_block.append({
            "type": "text",
            "text": tool_context,
            "cache_control": {"type": "ephemeral"}
        })

    # --- Block 2b: skill definitions ---
    # Loaded once at boot (and refreshed periodically by idle's
    # auto_discover_paths_task) into state.SKILLS_CONTEXT by
    # main._load_skills_context. Each entry is a skill.md's YAML front-matter
    # body followed by the file's absolute path, wrapped in START/END markers.
    # Omitted entirely when no skills are installed (empty string).
    skills_context = getattr(state, "SKILLS_CONTEXT", "")
    if skills_context:
        system_block.append({
            "type": "text",
            "text": skills_context,
            "cache_control": {"type": "ephemeral"}
        })
    layers["Skill Definitions"] = skills_context

    # --- Block 3: knowledge + misc references + anchored memories ---
    # DESIGN NOTE — which location lists feed the system block vs. the
    # vector store:
    #   * CONTEXT_HISTORY_LOCATIONS  -> always-in-context (journals,
    #     session summaries, etc.). Loaded in full here on every turn.
    #   * CONTEXT_REFERENCE_LOCATIONS -> always-in-context misc
    #     reference files. Loaded in full here on every turn.
    #   * SEARCH_REFERENCE_LOCATIONS -> vector-store (ChromaDB) corpus
    #     ONLY. These are NEVER auto-loaded into the system block; they
    #     are retrieved on demand via fusion search. Do not conflate the
    #     two — reference knowledge is not double-paid into both paths.
    #
    # Short-term journal (`kb_auto_short_term_journal.md`) lives in
    # CONTEXT_HISTORY_LOCATIONS and archives to SEARCH_REFERENCE_LOCATIONS
    # at 45 entries (see directives.py). Permanent journal
    # (`kb_auto_perm_journal.md`) never archives — the user must manually
    # prune it (intentional). Disk reload of these files per turn is
    # cheap and keeps RAM footprint low for small-host deployments
    # (Raspberry Pi, small EC2, etc.); re-tokenization is absorbed by
    # provider prompt caching.
    reference_context = ""
    history_context_parts = []
    ref_context_parts = []
    dynamic_parts = []
    anchor_parts = []

    if memory_enabled:
        for path in locs["CONTEXT_HISTORY_LOCATIONS"]:
            file_content = load_file_content(path)
            if file_content:
                label = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
                chunk = f"\n\n## {label}\n{file_content}\n"
                reference_context += chunk
                history_context_parts.append(chunk)

        for i, path in enumerate(locs["CONTEXT_REFERENCE_LOCATIONS"], 1):
            file_content = load_file_content(path)
            if file_content:
                chunk = f"\n\n## Miscellaneous {i}\n{file_content}\n"
                reference_context += chunk
                ref_context_parts.append(chunk)

    dynamic_rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
    for i, (_param, path) in enumerate(dynamic_rows, 1):
        file_content = load_file_content(path)
        if file_content:
            label = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
            chunk = f"\n\n## Dynamic {i} — {label}\n{file_content}\n"
            reference_context += chunk
            dynamic_parts.append(chunk)

    if anchors_enabled:
        anchored = get_anchored_memories(db, channel_name)
        if anchored:
            anchor_text = "\n\n## Anchored Memories\n"
            for mid, m in anchored:
                anchor_text += f"{mid}. {m}\n"
            reference_context += anchor_text
            anchor_parts.append(anchor_text)

    layers["History Context"] = "".join(history_context_parts) if history_context_parts else ""
    layers["Reference Context"] = "".join(ref_context_parts) if ref_context_parts else ""
    layers["Dynamic Files"] = "".join(dynamic_parts) if dynamic_parts else ""
    layers["Memory Anchors"] = "".join(anchor_parts) if anchor_parts else ""
    layers["Social Mode Indicator"] = _SOCIAL_MODE_INDICATOR if social_mode else ""

    if reference_context:
        system_block.append({
            "type": "text",
            "text": reference_context,
            "cache_control": {"type": "ephemeral"}
        })

    # --- Block 4: social mode indicator ---
    if social_mode:
        system_block.append({
            "type": "text",
            "text": _SOCIAL_MODE_INDICATOR,
            "cache_control": {"type": "ephemeral"}
        })

    return system_block, layers

def build_trimmed_history_for_payload(db, channel_name, augmented_prompt, context_limit=None, channel_key=None, model=None, before_id=None):
    if context_limit is None:
        context_limit = config.MAX_CONTEXT_TOKENS
    system_tokens = sum(estimate_tokens(block["text"], channel_key=channel_key) for block in augmented_prompt)

    warn_threshold = int(context_limit * 0.65)
    hard_threshold = int(context_limit * 0.80)
    min_history_floor = max(8000, int(context_limit * 0.15))

    if system_tokens > warn_threshold:
        remaining = context_limit - system_tokens
        print(f"⚠️ [{channel_name}] System block ({system_tokens:,} tokens) exceeds "
              f"65% of context limit ({context_limit:,}). Only {remaining:,} tokens "
              f"remain for conversation history.")

    if system_tokens > hard_threshold or (context_limit - system_tokens) < min_history_floor:
        # DESIGN NOTE — intentional hard abort vs. silent knowledge drop:
        # Raising here is a deliberate design choice. We will CHOOSE to
        # abort the turn rather than silently shed knowledge files,
        # anchored memories, or journal entries out of context. If the
        # user has configured too much always-in-context content for their
        # model's window, that is a configuration error on their side and
        # should surface as an explicit error, not as degraded behavior
        # where the companion quietly forgets things mid-conversation.
        # Users are responsible for pruning permanent journals / anchors
        # or moving to a larger-context model. Do not add graceful
        # fallback that drops knowledge without an explicit signal.
        raise SystemBlockOversizedError(system_tokens, context_limit, min_history_floor)

    history_budget = context_limit - system_tokens
    history = get_recent_messages(db, channel_name, before_id=before_id)
    history_tokens = sum(estimate_tokens(msg["content"], channel_key=channel_key) for msg in history)
    while history and history_tokens > history_budget:
        removed = history.pop(0)
        history_tokens -= estimate_tokens(removed["content"], channel_key=channel_key)
    if history and model and provider._is_explicit_caching_model(model):
        last = history[-1]
        raw = last["content"]
        if isinstance(raw, list):
            raw = "\n".join(
                b.get("text", "") for b in raw
                if isinstance(b, dict) and b.get("type") == "text"
            ) or str(raw)
        last["content"] = [{
            "type": "text",
            "text": raw,
            "cache_control": {"type": "ephemeral"}
        }]
    return history

def compose_full_messages(augmented_prompt, history):
    full_messages = [{"role": "system", "content": augmented_prompt}]
    full_messages.extend(history)
    return full_messages


def _strip_cache_control(content):
    if isinstance(content, list):
        text_parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text_parts.append(b.get("text", ""))
        return "\n".join(text_parts) if text_parts else str(content)
    return content


def _apply_cache_breakpoint(msg, model):
    if not (model and provider._is_explicit_caching_model(model)):
        return
    raw = msg["content"]
    if isinstance(raw, list):
        text_parts = []
        for b in raw:
            if isinstance(b, dict) and b.get("type") == "text":
                text_parts.append(b.get("text", ""))
        raw = "\n".join(text_parts) or str(raw)
    msg["content"] = [{
        "type": "text",
        "text": raw,
        "cache_control": {"type": "ephemeral"}
    }]


def retrim_history_for_tool_round(full_messages, history_end_idx, context_limit,
                                  channel_key, model, reserve_ratio=0.07,
                                  min_reserve=10000):
    """Re-trim DB-loaded history (indices 1..history_end_idx) in-place to make
    room for tool-round output. Returns the updated history_end_idx.

    full_messages[0] is the system block (never trimmed).
    full_messages[1..history_end_idx] are DB-loaded history (FIFO trimmable).
    full_messages[history_end_idx+1..] are current-turn messages (never trimmed).
    """
    reserve_tokens = max(min_reserve, int(context_limit * reserve_ratio))
    target = context_limit - reserve_tokens

    for i in range(1, history_end_idx + 1):
        full_messages[i]["content"] = _strip_cache_control(full_messages[i]["content"])

    total = sum(msg_tokens(m, channel_key=channel_key) for m in full_messages)
    while history_end_idx >= 1 and total > target:
        removed = full_messages.pop(1)
        total -= msg_tokens(removed, channel_key=channel_key)
        history_end_idx -= 1

    # Safety net: if still over budget after history is exhausted,
    # truncate the largest current-turn message to fit. This prevents
    # shipping an oversized request when tool output + system block
    # exceed the context limit and there's no history left to trim.
    if total > target and history_end_idx < 1:
        guard = 0
        while total > target and guard < 3:
            guard += 1
            largest_idx = None
            largest_tokens = 0
            for i in range(1, len(full_messages)):
                t = msg_tokens(full_messages[i], channel_key=channel_key)
                if t > largest_tokens:
                    largest_tokens = t
                    largest_idx = i
            if largest_idx is None:
                break
            msg = full_messages[largest_idx]
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 200:
                overflow = total - target
                _marker = "\n[…truncated to fit context…]"
                # Account for the marker bytes re-added each round — without
                # the +len(_marker) slack, a small overflow trims fewer chars
                # than the marker re-adds and the net makes no progress
                # across its guard rounds.
                trim_chars = int(overflow * get_bytes_per_token(channel_key)) + len(_marker) + 64
                keep_chars = max(100, len(content) - trim_chars)
                msg["content"] = content[:keep_chars] + _marker
                total = sum(msg_tokens(m, channel_key=channel_key) for m in full_messages)
            else:
                break

    if history_end_idx >= 1:
        _apply_cache_breakpoint(full_messages[history_end_idx], model)

    return history_end_idx


async def get_ai_response(messages, model, reasoning_effort=None, temperature=None, top_k=None, channel_key=None, provider_lock=None, debug_enabled=False):
    print(f"[{datetime.now():%H:%M:%S.%f}]")
    if reasoning_effort and reasoning_effort.lower() != "off":
        text, thinking = await provider.chat_completion_text_with_thinking(
            model, messages,
            max_tokens=config.MAX_TOKENS,
            reasoning=reasoning_effort,
            temperature=temperature,
            top_k=top_k,
            channel_key=channel_key,
            provider_lock=provider_lock,
            debug_enabled=debug_enabled,
        )
        return text, thinking
    text = await provider.chat_completion_text(
        model, messages,
        max_tokens=config.MAX_TOKENS,
        reasoning=reasoning_effort,
        temperature=temperature,
        top_k=top_k,
        channel_key=channel_key,
        provider_lock=provider_lock,
        debug_enabled=debug_enabled,
    )
    return text, None

async def get_image_response(prompt, model, reference_images=None, provider_lock=None):
    return await provider.generate_image(prompt, model, reference_images=reference_images, provider_lock=provider_lock)


async def get_video_response(prompt, model, reference_images=None, reference_videos=None,
                             seconds=None, provider_lock=None):
    return await provider.generate_video(prompt, model, reference_images=reference_images,
                                          reference_videos=reference_videos, seconds=seconds,
                                          provider_lock=provider_lock)
