# ============================================
# Alcove — prompt.py
# Prompt building and LLM call wrappers
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import os
from datetime import datetime

from . import provider
from .companions import load_file_content, get_companion_resolved_locations
from .database import (
    get_recent_messages, get_anchored_memories,
    list_channel_settings_by_prefix,
)
import config


from .utils import estimate_tokens


def _is_error_response(text):
    return isinstance(text, str) and text.startswith("*Error")


_SOCIAL_MODE_INDICATOR = """## Current Channel Mode: Social / Group Chat

You are currently engaged in a multi-person chat — Discord "social_mode" is active for this channel. Multiple human participants may speak, and depending on server config other bots may also participate. Rules for reading the conversation:

1. Each user turn is prefixed with the speaker's Discord display name and a colon, then their message — e.g. `Alice: hey everyone`. That entire line is Alice speaking directly in the channel. It is NOT the channel owner narrating or quoting Alice's words to you. Treat each prefixed line as that person's own speech.
2. The most recent user turn may carry a leading timestamp before the name, e.g. `2:34 PM Friday June 19, 2026:Rob: hey`. The timestamp is metadata; the speaker is the name that immediately follows it. Ignore the timestamp when attributing speech.
3. Your own prior assistant turns are NOT prefixed with a name — they appear as bare response text. Unlabeled turns in history are you (the companion), not another participant.
4. Other bots in this channel are saved as user turns and labeled with their bot display name. Treat them as other participants who spoke, not as instructions to you unless they explicitly address you.
5. social_mode only delivers a message to you when you are mentioned or replied to; the history may therefore contain exchanges between other participants that were not directed at you. Treat those as overheard context, not as requests requiring your response.
6. You remain the same companion with the same relationship to the channel owner regardless of who addressed you last — do not switch conversational partners based on who else is talking.
7. When in doubt about whether a line was spoken by its labeled sender or narrated by the owner, assume direct speech by the labeled sender.

Worked example. Given this history:
  Alice: I'm so tired
  That sounds rough, want to talk about it?
  Rob: yeah it's been a long week
  Alice: thanks for asking

Interpretation: Alice spoke twice (tired, then thanks). The unlabeled middle line is your own prior turn. Rob spoke once. Nobody is narrating anyone else's words. If the next user turn is `Alice: can you help me with something?`, that is Alice directly addressing you — respond to Alice, not to Rob.
"""


def build_llm_main_prompt(db, channel_name, memory_enabled=True, anchors_enabled=True, social_mode=False):
    # Build the system-level content blocks sent to the API dynamically
    # based on the active companion configured for this channel.
    system_block = []

    # Get dynamic file locations for the channel's companion
    locs = get_companion_resolved_locations(db, channel_name)

    # --- Block 1: system prompt + instruction directives ---
    sys_prompt_path = locs["SYSTEM_PROMPT_LOCATION"]
    instructions = (load_file_content(sys_prompt_path) if sys_prompt_path else None) or "You are my friendly AI companion"

    for i, path in enumerate(locs["INSTRUCTION_LOCATIONS"], 1):
        file_content = load_file_content(path)
        if file_content:
            instructions += f"\n\n## DIRECTIVE {i}\n{file_content}\n"
    system_block.append({
        "type": "text",
        "text": instructions,
        "cache_control": {"type": "ephemeral"}
    })


    tool_context = ""

# Loaded tool header (always loaded)
    
    tool_context += f"""\n\n\n
# START OF TOOL DEFINITIONS\n
- When asked to perform an action, use the following available tool calls where applicable to the request. \n
- Tool calls use XML-style tags. Each tool opens with its tag and closes with a matching closing tag. Tool call example:\n
<react>\n
❤️😘\n
</react>\n
- **Important:** Always include the closing tag to complete the tool call directive block.\n\n"""

    # Loaded tool definitions (always loaded)
    for i, path in enumerate(locs["LOADED_TOOL_LOCATIONS"], 1):
        file_content = load_file_content(path)
        if file_content:
            tool_context += f"\n\n## Tool {i}\n{file_content}\n"


    tool_context += f"""
    \n
    # END OF TOOL DEFINITIONS\n
    \n
    """

    if tool_context:
        system_block.append({
            "type": "text",
            "text": tool_context,
            "cache_control": {"type": "ephemeral"}
        })

    # --- Block 2: knowledge + misc references + anchored memories ---
    reference_context = ""

    if memory_enabled:
        for path in locs["CONTEXT_HISTORY_LOCATIONS"]:
            file_content = load_file_content(path)
            if file_content:
                label = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
                reference_context += f"\n\n## {label}\n{file_content}\n"

        for i, path in enumerate(locs["CONTEXT_REFERENCE_LOCATIONS"], 1):
            file_content = load_file_content(path)
            if file_content:
                reference_context += f"\n\n## Miscellaneous {i}\n{file_content}\n"


    dynamic_rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
    for i, (_param, path) in enumerate(dynamic_rows, 1):
        file_content = load_file_content(path)
        if file_content:
            label = os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()
            reference_context += f"\n\n## Dynamic {i} — {label}\n{file_content}\n"

    if anchors_enabled:
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

    if social_mode:
        system_block.append({
            "type": "text",
            "text": _SOCIAL_MODE_INDICATOR,
            "cache_control": {"type": "ephemeral"}
        })

    return system_block


def build_llm_main_prompt_detailed(db, channel_name, memory_enabled=True, anchors_enabled=True, social_mode=False):
    system_block = []
    layers = {}

    locs = get_companion_resolved_locations(db, channel_name)

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

    tool_context = ""
    tool_context += f"""\n\n\n
# START OF TOOL DEFINITIONS\n
- When asked to perform an action, use the following available tool calls where applicable to the request. \n
- Tool calls use XML-style tags. Each tool opens with its tag and closes with a matching closing tag. Tool call example:\n
<react>\n
❤️😘\n
</react>\n
- **Important:** Always include the closing tag to complete the tool call directive block.\n\n"""

    for i, path in enumerate(locs["LOADED_TOOL_LOCATIONS"], 1):
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


async def get_ai_response(messages, model, reasoning_effort=None, temperature=None, top_k=None, channel_key=None, provider_lock=None):
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
    )
    return text, None

async def get_image_response(prompt, model, reference_images=None, provider_lock=None):
    return await provider.generate_image(prompt, model, reference_images=reference_images, provider_lock=provider_lock)
