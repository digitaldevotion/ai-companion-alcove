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
from .llm_prompt_builder import (
    _is_error_response,
    _is_context_overflow_error,
    _context_overflow_user_message,
    attempt_context_recovery,
    get_ai_response, get_image_response, get_video_response,
    retrim_history_for_tool_round,
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
    had_journal_write: bool = False


async def run_llm_loop(full_messages, ctx, message, is_voice_message,
                        reference_images_by_channel,
                        consecutive_reacts_by_channel,
                        client, history_end_idx=0,
                        reference_videos_by_channel=None):
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
        debug_enabled=ctx.debug_enabled,
    )

    if _is_context_overflow_error(response_text):
        print(f"🚫 [{ctx.channel_name}] Context window exceeded: {response_text[:200]}")
        # Self-heal: parse the endpoint's true limit + requested token count
        # from the error, snap channel calibration to the observed truth,
        # correct the channel limit downward if the endpoint proves it
        # smaller than configured, and re-trim the payload — then retry
        # exactly once.
        recovered, history_end_idx = attempt_context_recovery(
            full_messages, history_end_idx, ctx, response_text,
            model=response_model,
        )
        if recovered:
            print(f"🔁 [{ctx.channel_name}] Retrying once after context-overflow "
                  f"self-heal…")
            response_text, thinking_text = await get_ai_response(
                full_messages, model=response_model,
                reasoning_effort=ctx.current_reasoning_effort,
                temperature=ctx.current_temperature, top_k=ctx.current_top_k,
                channel_key=ctx.channel_name,
                provider_lock=response_provider_lock,
                debug_enabled=ctx.debug_enabled,
            )
        if _is_context_overflow_error(response_text):
            print(f"🚫 [{ctx.channel_name}] Context window still exceeded after "
                  f"self-heal: {response_text[:200]}")
            try:
                await safe_send_chunked(message.channel, _context_overflow_user_message())
            except Exception:
                pass
            result.response_text = response_text
            return result

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

    _ref_vids = (reference_videos_by_channel or {}).get(ctx.channel_name)
    async def channel_video_handler(prompt, reference_images=None, reference_videos=None, seconds=None):
        refs_img = reference_images if reference_images is not None else _ref_imgs
        refs_vid = reference_videos if reference_videos is not None else _ref_vids
        return await get_video_response(prompt, model=ctx.current_video_model,
                                          reference_images=refs_img, reference_videos=refs_vid,
                                          seconds=seconds, provider_lock=ctx.current_video_provider_lock)

    prompt_tokens = sum(msg_tokens(m, channel_key=ctx.channel_name) for m in full_messages)
    token_budget = ctx.current_context_limit - prompt_tokens
    ch_reacts = consecutive_reacts_by_channel[ctx.channel_name]

    display_text, runcmd_results, readweb_results, readimage_results, websearch_results, readskill_results, had_react, react_was_posted, had_journal_write = await process_response(
        response_text, message.channel,
        image_handler=channel_image_handler, video_handler=channel_video_handler, token_budget=token_budget,
        db=ctx.db, user_message=message, consecutive_reacts=ch_reacts,
        active_companion=ctx.active_companion, send_func=safe_send,
        channel_name=ctx.channel_name,
        anchor_review_ctx=ctx,
    )
    result.had_journal_write = had_journal_write

    if not had_react:
        consecutive_reacts_by_channel[ctx.channel_name] = 0
    elif react_was_posted:
        consecutive_reacts_by_channel[ctx.channel_name] = ch_reacts + 1
    else:
        consecutive_reacts_by_channel[ctx.channel_name] = max(0, ch_reacts - 1)

    tool_round = 0
    voice_text_parts = []
    while (runcmd_results or readweb_results or readimage_results or websearch_results or readskill_results) and tool_round < config.MAX_TOOL_ROUNDS:
        tool_round += 1

        # Recompute token budget each round — the prompt grows as tool
        # output is appended, so the budget must shrink accordingly.
        prompt_tokens = sum(msg_tokens(m, channel_key=ctx.channel_name) for m in full_messages)
        token_budget = ctx.current_context_limit - prompt_tokens

        if tool_round == 1 and display_text:
            await safe_send_chunked(message.channel, display_text)
            voice_text_parts.append(display_text)
            display_text = ""

        result_lines = []
        runcmd_cap = tokens_to_bytes(token_budget, channel_key=ctx.channel_name) if config.MAXIMIZE_AVAILABLE_CONTEXT else 16384
        readweb_cap = tokens_to_bytes(token_budget, channel_key=ctx.channel_name) if config.MAXIMIZE_AVAILABLE_CONTEXT else 4000
        websearch_cap = tokens_to_bytes(token_budget, channel_key=ctx.channel_name) if config.MAXIMIZE_AVAILABLE_CONTEXT else 4000
        for r in runcmd_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= runcmd_cap else r["output"][:runcmd_cap] + "... (truncated)"
            result_lines.append(f"$ {r['command']}\n[{status}]\n{output}")
        for r in readweb_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= readweb_cap else r["output"][:readweb_cap] + "... (truncated)"
            result_lines.append(f"🌐 {r['url']}\n[{status}]\n{output}")
        for r in websearch_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            output = r["output"] if len(r["output"]) <= websearch_cap else r["output"][:websearch_cap] + "... (truncated)"
            result_lines.append(f"🔍 {r['query']}\n[{status}]\n{output}")
        attached_image_urls = []
        for r in readimage_results:
            if r["success"]:
                result_lines.append(f"🖼️ {r['source']}\n[SUCCESS]\n(image attached below)")
                attached_image_urls.append(r["image_url"])
            else:
                result_lines.append(f"🖼️ {r['source']}\n[FAILED]\n{r['error']}")
        for r in readskill_results:
            status = "SUCCESS" if r["success"] else "FAILED"
            result_lines.append(f"📖 {r['path']}\n[{status}]\n{r['output']}")
        results_summary = "\n\n".join(result_lines)
        tool_user_text = f"Tool responses:\n\n{results_summary}"

        full_messages.append({"role": "assistant", "content": response_text})
        if attached_image_urls:
            async def _to_data_uri(u):
                # execute_readimage already returns data: URIs for local files;
                # only HTTP(S) URLs need to be fetched and re-encoded.
                return u if u.startswith("data:") else await url_to_data_uri(u)
            # One expired/dead image URL must not abort an otherwise
            # successful multi-round turn — fetch all, keep the good ones.
            _fetch_results = await asyncio.gather(
                *[_to_data_uri(u) for u in attached_image_urls],
                return_exceptions=True,
            )
            image_data_uris = []
            for _url, _res in zip(attached_image_urls, _fetch_results):
                if isinstance(_res, BaseException):
                    print(f"⚠️ [llm_loop] image fetch failed for {_url[:100]}: "
                          f"{type(_res).__name__}: {_res}")
                elif _res:
                    image_data_uris.append(_res)
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

        history_end_idx = retrim_history_for_tool_round(
            full_messages, history_end_idx,
            ctx.current_context_limit, ctx.channel_name,
            ctx.current_text_model,
        )

        print(f"🔁 Round {tool_round}/{config.MAX_TOOL_ROUNDS}: sending tool output back to model for follow-up response...")
        followup_text, _ = await get_ai_response(
            full_messages, model=response_model,
            reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=ctx.channel_name,
            provider_lock=response_provider_lock,
            debug_enabled=ctx.debug_enabled,
        )

        followup_display, runcmd_results, readweb_results, readimage_results, websearch_results, readskill_results, _, _, _ = await process_response(
            followup_text, message.channel,
            image_handler=channel_image_handler, video_handler=channel_video_handler, token_budget=token_budget,
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
        and (runcmd_results or readweb_results or readimage_results or websearch_results or readskill_results)
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

        history_end_idx = retrim_history_for_tool_round(
            full_messages, history_end_idx,
            ctx.current_context_limit, ctx.channel_name,
            ctx.current_text_model,
        )

        # Recompute token budget for the summary call — the prompt has
        # grown since the last round's computation.
        prompt_tokens = sum(msg_tokens(m, channel_key=ctx.channel_name) for m in full_messages)
        token_budget = ctx.current_context_limit - prompt_tokens

        summary_text, _ = await get_ai_response(
            full_messages, model=response_model,
            reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=ctx.channel_name,
            provider_lock=response_provider_lock,
            debug_enabled=ctx.debug_enabled,
        )
        summary_display, _, _, _, _, _, _, _, _ = await process_response(
            summary_text, message.channel,
            image_handler=channel_image_handler, video_handler=channel_video_handler, token_budget=token_budget,
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

    # Record whether this reply was spoken via TTS, so the idle-loop
    # random-thought follow-up can decide whether to also speak itself.
    # Only user-driven replies set this; spontaneous idle/dream/note/random
    # responses intentionally do NOT (per the random-thought feature spec).
    state.LAST_REPLY_WAS_VOICE_BY_CHANNEL[ctx.channel_name] = bool(result.tts_text)

    # Random Thoughts: re-arm the idle timer now that the bot has finished
    # responding to a user-driven message. (Spontaneous idle/dream/note
    # responses intentionally do NOT re-arm, per the feature spec.)
    if getattr(state, "RANDOM_THOUGHTS_ENABLED", False):
        try:
            idle._reset_random_thought_counter(ctx.db)
        except Exception as _e:
            print(f"⚠️ random-thought reset failed: {_e}")

    return result
