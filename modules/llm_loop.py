# ============================================
# Alcove — llm_loop.py
# LLM response loop with tool-round handling
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any, List, Optional

import config
from . import state
from . import idle
from .context import ChannelContext
from .directives import process_response
from .prompt import (
    _is_error_response,
    get_ai_response, get_image_response,
)
from .attachments import url_to_data_uri
from .database import get_channel_setting
from .utils import send_spoilered_code_blocks, safe_send, safe_send_chunked, estimate_tokens, msg_tokens, tokens_to_bytes


@dataclass
class LLMLoopResult:
    display_text: str = ""
    response_text: str = ""
    voice_text_parts: list = field(default_factory=list)
    pending_saves: list = field(default_factory=list)
    is_voice_message: bool = False


async def run_llm_loop(full_messages, ctx, message, is_voice_message,
                        reference_images_by_channel,
                        consecutive_reacts_by_channel,
                        client):
    result = LLMLoopResult(is_voice_message=is_voice_message)

    response_model = ctx.current_voice_model if is_voice_message else ctx.current_text_model
    response_provider_lock = ctx.current_voice_text_provider_lock if is_voice_message else ctx.current_text_provider_lock

    thinking_msg = None
    if ctx.current_reasoning_effort and ctx.current_reasoning_effort.lower() != "off":
        try:
            thinking_msg = await safe_send(message.channel,
                f"🧠 *thinking ({ctx.current_reasoning_effort} effort)…*"
            )
        except Exception:
            thinking_msg = None

    response_text, thinking_text = await get_ai_response(
        full_messages, model=response_model,
        reasoning_effort=ctx.current_reasoning_effort,
        temperature=ctx.current_temperature, top_k=ctx.current_top_k,
        channel_key=ctx.channel_name,
        provider_lock=response_provider_lock,
    )

    state.LAST_INTERACTION_CHANNEL = ctx.channel_name

    if message.author.bot:
        pass

    if thinking_msg is not None:
        try:
            await thinking_msg.delete()
        except Exception:
            pass

    reasoning_no_display = get_channel_setting(ctx.db, ctx.channel_name, "reasoning_no_display")
    if thinking_text and not reasoning_no_display:
        try:
            effort_label = ctx.current_reasoning_effort or "unknown"
            await send_spoilered_code_blocks(
                message.channel, thinking_text,
                header=f"🧠 thinking ({effort_label} effort):"
            )
        except Exception as e:
            print(f"⚠️ Failed to send thinking content: {e}")

    total_ctx = sum(msg_tokens(m, channel_key=ctx.channel_name) for m in full_messages) + estimate_tokens(response_text, channel_key=ctx.channel_name)
    print(f"📊 [{ctx.channel_name}] Context: ~{total_ctx:,} tokens (budget: {ctx.current_context_limit:,})")

    _ref_imgs = reference_images_by_channel.get(ctx.channel_name)
    async def channel_image_handler(prompt, reference_images=None):
        refs = reference_images if reference_images is not None else _ref_imgs
        return await get_image_response(prompt, model=ctx.current_image_model, reference_images=refs, provider_lock=ctx.current_image_provider_lock)

    prompt_tokens = sum(msg_tokens(m, channel_key=ctx.channel_name) for m in full_messages)
    token_budget = ctx.current_context_limit - prompt_tokens
    ch_reacts = consecutive_reacts_by_channel[ctx.channel_name]

    display_text, runcmd_results, readweb_results, readimage_results, had_react, react_was_posted = await process_response(
        response_text, message.channel,
        image_handler=channel_image_handler, token_budget=token_budget,
        db=ctx.db, user_message=message, consecutive_reacts=ch_reacts,
        active_companion=ctx.active_companion, send_func=safe_send,
        channel_name=ctx.channel_name,
    )

    if not had_react:
        consecutive_reacts_by_channel[ctx.channel_name] = 0
    elif react_was_posted:
        consecutive_reacts_by_channel[ctx.channel_name] = ch_reacts + 1
    else:
        consecutive_reacts_by_channel[ctx.channel_name] = max(0, ch_reacts - 1)

    tool_round = 0
    voice_text_parts = []
    while (runcmd_results or readweb_results or readimage_results) and tool_round < config.MAX_TOOL_ROUNDS:
        tool_round += 1

        if tool_round == 1 and display_text:
            await safe_send_chunked(message.channel, display_text)
            voice_text_parts.append(display_text)
            display_text = ""

        result_lines = []
        runcmd_cap = tokens_to_bytes(token_budget, channel_key=ctx.channel_name) if config.MAXIMIZE_AVAILABLE_CONTEXT else 16384
        readweb_cap = tokens_to_bytes(token_budget, channel_key=ctx.channel_name) if config.MAXIMIZE_AVAILABLE_CONTEXT else 4000
        for r in runcmd_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= runcmd_cap else r["output"][:runcmd_cap] + "... (truncated)"
            result_lines.append(f"$ {r['command']}\n[{status}]\n{output}")
        for r in readweb_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= readweb_cap else r["output"][:readweb_cap] + "... (truncated)"
            result_lines.append(f"🌐 {r['url']}\n[{status}]\n{output}")
        attached_image_urls = []
        for r in readimage_results:
            if r["success"]:
                result_lines.append(f"🖼️ {r['source']}\n[SUCCESS]\n(image attached below)")
                attached_image_urls.append(r["image_url"])
            else:
                result_lines.append(f"🖼️ {r['source']}\n[FAILED]\n{r['error']}")
        results_summary = "\n\n".join(result_lines)
        tool_user_text = f"Tool responses:\n\n{results_summary}"

        full_messages.append({"role": "assistant", "content": response_text})
        if attached_image_urls:
            async def _to_data_uri(u):
                # execute_readimage already returns data: URIs for local files;
                # only HTTP(S) URLs need to be fetched and re-encoded.
                return u if u.startswith("data:") else await url_to_data_uri(u)
            image_data_uris = await asyncio.gather(
                *[_to_data_uri(u) for u in attached_image_urls]
            )
            image_data_uris = [d for d in image_data_uris if d]
            tool_content_blocks = [{"type": "text", "text": tool_user_text}]
            for data_uri in image_data_uris:
                tool_content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                })
            full_messages.append({"role": "user", "content": tool_content_blocks})
        else:
            full_messages.append({"role": "user", "content": tool_user_text})

        result.pending_saves.append(("assistant", response_text, None))
        result.pending_saves.append(("user", tool_user_text, "system"))

        print(f"🔁 Round {tool_round}/{config.MAX_TOOL_ROUNDS}: sending tool output back to model for follow-up response...")
        followup_text, _ = await get_ai_response(
            full_messages, model=response_model,
            reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=ctx.channel_name,
            provider_lock=response_provider_lock,
        )

        followup_display, runcmd_results, readweb_results, readimage_results, _, _ = await process_response(
            followup_text, message.channel,
            image_handler=channel_image_handler, token_budget=token_budget,
            db=ctx.db, user_message=message,
            consecutive_reacts=consecutive_reacts_by_channel[ctx.channel_name],
            suppress_reacts=True, active_companion=ctx.active_companion,
            send_func=safe_send,
            channel_name=ctx.channel_name,
        )
        if followup_display:
            await safe_send_chunked(message.channel, followup_display)
            voice_text_parts.append(followup_display)
        response_text = followup_text

    hit_tool_limit = (
        tool_round >= config.MAX_TOOL_ROUNDS
        and (runcmd_results or readweb_results or readimage_results)
    )
    if hit_tool_limit:
        full_messages.append({"role": "assistant", "content": response_text})
        result.pending_saves.append(("assistant", response_text, None))

        limit_notice = (
            f"Alcove Notice: Tool-call limit reached: you've used your "
            f"{config.MAX_TOOL_ROUNDS} tool rounds for this request, and any tool directives in your last reply were not executed."
            f"Please explain to your user that you've hit the tool round limit for this request, "
            f"give them a concise update on what progress you've made so far and what you believe is left to do if they wish to continue (all they have to do is ask)."
        )
        full_messages.append({"role": "user", "content": limit_notice})
        result.pending_saves.append(("user", limit_notice, "system"))

        summary_text, _ = await get_ai_response(
            full_messages, model=response_model,
            reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=ctx.channel_name,
            provider_lock=response_provider_lock,
        )
        summary_display, _, _, _, _, _ = await process_response(
            summary_text, message.channel,
            image_handler=channel_image_handler, token_budget=token_budget,
            db=ctx.db, user_message=message,
            consecutive_reacts=consecutive_reacts_by_channel[ctx.channel_name],
            suppress_reacts=True, active_companion=ctx.active_companion,
            send_func=safe_send,
            channel_name=ctx.channel_name,
        )
        if summary_display:
            await safe_send_chunked(message.channel, summary_display)
            voice_text_parts.append(summary_display)
        response_text = summary_text

    if tool_round > 0:
        full_response_text = "\n\n".join(voice_text_parts)
        display_text = ""
    else:
        full_response_text = display_text

    result.display_text = display_text
    result.response_text = response_text
    result.voice_text_parts = voice_text_parts

    # Compute TTS text: only for voice messages when voice manager is connected
    from .voice import voice_manager
    result.tts_text = full_response_text if (is_voice_message and voice_manager.is_connected(guild=message.guild)) else None
    result.tts_voice_id = ctx.current_voice_id

    # Random Thoughts: re-arm the idle timer now that the bot has finished
    # responding to a user-driven message. (Spontaneous idle/dream/note
    # responses intentionally do NOT re-arm, per the feature spec.)
    if getattr(state, "RANDOM_THOUGHTS_ENABLED", False):
        try:
            idle._reset_random_thought_counter(ctx.db)
        except Exception as _e:
            print(f"⚠️ random-thought reset failed: {_e}")

    return result
