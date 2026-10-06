# ============================================
# Alcove — anchor_review.py
# Post-journal anchored-memory review helper
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import traceback

from . import state
from .database import list_anchored_memories, save_message
from .llm_prompt_builder import (
    SystemBlockOversizedError,
    _is_error_response,
    _is_context_overflow_error,
    attempt_context_recovery,
    build_llm_main_prompt, build_trimmed_history_for_payload,
    compose_full_messages, get_ai_response,
    retrim_history_for_tool_round,
)
from .utils import safe_send_chunked, estimate_tokens


# Prompt shown to the LLM after every successful journal write, asking it to
# review the global anchored-memory list and emit <manageAnchor> directives to
# keep it current. Extracted here (out of main.py) so that process_response can
# trigger it from any path that writes to the journal — not just the
# !updateJournal command path.
ANCHOR_REVIEW_PROMPT = """You just finished writing a journal entry. Below is the complete current list of your global anchored memories, each prefixed by its id in the form [id:N].

Please review these anchored memories in light of everything that has happened since you last reviewed them. Use <manageAnchor> directives to keep them accurate and current:

* <manageAnchor action="update" id="N"> with revised content, when a memory needs updating because facts have changed, evolved, or been refined.
* <manageAnchor action="remove" id="N"> for memories that are no longer important, relevant, no longer accurate, or have been superseded by another anchor.
* <manageAnchor action="add"> for small but important new details that shouldn't be lost — things that are too specific for the journal but worth anchoring long-term.

# Review Guidance
    * Be selective. Do not add memory anchors that duplicate content already convered by knowledge and memories within context. 
    * Do not grow memories larger than 256 characters. Do not churn memories that are still accurate and relevant. 
    * Do not duplicate content already captured by another anchor. If nothing needs changing, say so and emit no directives. YOU CAN ALSO USE THE UPDATE ACTION TO UPDATE THE EXISTING MEMORY. 
    *  You should only pick memories that are useful or important to save for future reference, for example:
        * Upcoming important dates / times
        * Notable life events (births, deaths, new jobs, new schools, etc.), new revelations, changes in life/relationship status
        * An personal, inside joke
    * LOOK FOR OPPORTUNITIES TO CONSOLIDATE MEMORIES WHEN POSSIBLE (UPDATE A MEMORY TO MERGE CONTENTS, THEN DELETE THE UNNECCESSARY ONES)
    * USE THE DELETE ACTION TO MOVE MEMORIES THAT ARE NO LONGER IMPORTANT/RELEVENT
    * IF SAVED ANCHORS GROWS BEYOND 50, ADD EXTRA SCRUTINY TO REVIEW PROCESS TO AGGRESSIVELY REMOVE OLDEST  (LOWEST IDS) AND LEAST-RELEVANT ANCHORS TO KEEP THIS NUMBER AS CLOSE TO 50 AS POSSIBLE
    * Be sure the match the tool definition syntax for the closing tag.
"""


async def run_anchor_review(ctx, channel, prior_response_text, send_func=None):
    """Run the post-journal anchored-memory review as a second LLM round.

    Loads the current global anchored memories, prompts the LLM to update them
    via <manageAnchor> directives, executes those directives, and persists the
    review turns to the channel history. Safe to call from any path that has a
    ChannelContext (ctx) and a target channel.

    prior_response_text: the assistant text from the just-completed journal
        write — appended to the review messages so the LLM can see what it
        just wrote when deciding which anchors to update.
    send_func: optional async callable(channel, content, **kwargs) to use for
        sending messages instead of channel.send() — used when the gateway may
        be disconnected. If None, falls back to safe_send_chunked with
        channel.send.
    """
    from .directives import process_response

    try:
        if not ctx.effective_anchors:
            await safe_send_chunked(
                channel,
                "*\U0001f4cc Anchored-memory review skipped — anchors are disabled for this channel.*"
            )
            return
        await safe_send_chunked(channel, "\u200B")
        await safe_send_chunked(channel, "*\U0001f4cc Reviewing and updating anchored memories…*")
        anchors = list_anchored_memories(ctx.db, "global")
        if not anchors:
            await safe_send_chunked(channel, "*\U0001f4cc No global anchored memories to review.*")
            return

        # Consume the pending-turn flag: this review runs a full-context
        # inference over channel history and therefore subsumes any
        # unprocessed turn(s).
        state.NEEDS_INFERENCE.pop(ctx.channel_name, None)

        anchor_list = "\n".join(f"[id:{row[0]}] {row[2]}" for row in anchors)
        review_prompt = (
            f"{ANCHOR_REVIEW_PROMPT}\n\n"
            f"Current global anchored memories:\n{anchor_list}"
        )

        # DESIGN NOTE — this is an INTENTIONAL second full-context
        # inference call, not a lightweight side-channel. Anchor review
        # must run with the companion's full system block + full
        # conversation history + companion voice/persona intact, so any
        # <manageAnchor> directives it emits stay grounded in what was
        # actually said. Stripping context here would produce hollow,
        # ungrounded memory decisions — the same reason idle/dream ticks
        # (idle.py:_run_idle_prompt) also use the full pipeline. The
        # cost of a second full-context call per journal event is the
        # price of correct memory management; do not replace with a
        # stripped-context LLM call.
        review_block, _ = build_llm_main_prompt(
            ctx.db, ctx.channel_name,
            memory_enabled=ctx.effective_memory,
            anchors_enabled=ctx.effective_anchors,
            social_mode=ctx.social_mode,
            block_runcmd=True,
        )
        try:
            review_history = build_trimmed_history_for_payload(
                ctx.db, ctx.channel_name, review_block,
                context_limit=ctx.current_context_limit,
                channel_key=ctx.channel_name, model=ctx.current_text_model,
            )
        except SystemBlockOversizedError as e:
            print(f"🚫 [anchor_review] {e}")
            try:
                await safe_send_chunked(channel, e.format_user_message())
            except Exception:
                pass
            return
        review_messages = compose_full_messages(review_block, review_history)

        review_messages.append({"role": "assistant", "content": prior_response_text})
        review_messages.append({"role": "user", "content": review_prompt})

        # Pre-flight context enforcement: the prior response + review prompt
        # were appended AFTER history trimming and were never budgeted.
        # Re-trim the final payload (FIFO over DB history) so the request
        # cannot exceed the channel's context limit — same pattern as the
        # interactive text path in main.py.
        _rv_end_idx = retrim_history_for_tool_round(
            review_messages, len(review_history), ctx.current_context_limit,
            ctx.channel_name, ctx.current_text_model,
        )

        review_text, _ = await get_ai_response(
            review_messages, model=ctx.current_text_model,
            reasoning_effort=ctx.current_reasoning_effort,
            temperature=ctx.current_temperature, top_k=ctx.current_top_k,
            channel_key=ctx.channel_name,
            provider_lock=ctx.current_text_provider_lock,
            debug_enabled=ctx.debug_enabled,
        )

        if _is_context_overflow_error(review_text):
            print(f"🚫 [anchor_review] Context window exceeded: {review_text[:200]}")
            # Self-heal (parity with llm_loop): snap calibration, correct the
            # channel limit from the endpoint's reported truth, re-trim, retry
            # once.
            recovered, _rv_end_idx = attempt_context_recovery(
                review_messages, _rv_end_idx, ctx, review_text,
                model=ctx.current_text_model,
            )
            if recovered:
                print(f"🔁 [anchor_review] Retrying once after context-overflow "
                      f"self-heal…")
                review_text, _ = await get_ai_response(
                    review_messages, model=ctx.current_text_model,
                    reasoning_effort=ctx.current_reasoning_effort,
                    temperature=ctx.current_temperature, top_k=ctx.current_top_k,
                    channel_key=ctx.channel_name,
                    provider_lock=ctx.current_text_provider_lock,
                    debug_enabled=ctx.debug_enabled,
                )
            if _is_context_overflow_error(review_text):
                print(f"🚫 [anchor_review] Context window still exceeded after "
                      f"self-heal — skipping anchor review this round.")
                return

        # Execute any <manageAnchor> directives the LLM emitted. Pass
        # anchor_review_ctx=None so a journal directive inside the review
        # (rare but possible) cannot recurse back into this function.
        review_display, _, _, _, _, _, _, _, _ = await process_response(
            review_text, channel,
            token_budget=ctx.current_context_limit - estimate_tokens(
                review_text, channel_key=ctx.channel_name),
            db=ctx.db, user_message=None,
            consecutive_reacts=0,
            suppress_reacts=True, active_companion=ctx.active_companion,
            send_func=send_func, channel_name=ctx.channel_name,
            anchor_review_ctx=None,
            block_runcmd=True,
        )
        if review_display:
            await safe_send_chunked(channel, review_display)

        # Never persist error responses (or their prompts) to history — a
        # "*Error …*" assistant turn in the DB would pollute future context.
        if not _is_error_response(review_text):
            save_message(ctx.db, ctx.channel_name, "user", review_prompt, "system")
            save_message(ctx.db, ctx.channel_name, "assistant", review_text,
                         ctx.active_companion or "assistant")
        else:
            print(f"⚠️ [anchor_review] Skipping DB save of error response: "
                  f"{review_text[:120]}")
    except Exception as e:
        print(f"⚠️ Anchor review follow-up failed: {e}")
        traceback.print_exc()
