# ============================================
# Alcove v1.3.0 — commands.py
# Discord ! command handlers
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import asyncio
import base64
import datetime
import discord
import io
import json
import os
import re
import shutil
import subprocess
import uuid

import config
from . import provider
from .database import (
    get_channel_setting, set_channel_setting,
    clear_channel_setting,
    get_recent_messages, get_anchored_memories,
    add_anchored_memory, remove_anchored_memory,
    list_anchored_memories, rename_channel,
    reset_all_channel_settings,
    list_channel_settings_by_prefix,
    get_global_var, set_global_var,
    delete_global_var, list_global_vars,
)

# Matches !M<n> or !m<n> where n is 1..20 (full 1..20 enforced in handler).
# Group 1: the macro number; group 2: anything after the macro token.
_MACRO_RE = re.compile(r"^!m(\d+)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
MACRO_MIN = 1
MACRO_MAX = 20
from .voice import voice_manager

DISCORD_MAX = 2000


async def send_chunked(channel, text, limit=DISCORD_MAX):
    """Send text to a Discord channel, splitting on newlines if it exceeds the limit."""
    if len(text) <= limit:
        await channel.send(text)
        return
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        # If a single line alone exceeds the limit, send what we have then force-send the long line
        if len(line) + 1 > limit:
            if chunk:
                await channel.send(chunk)
                chunk = ""
            # Truncate the oversized line
            await channel.send(line[:limit - 3] + "...")
            continue
        if len(chunk) + len(line) + 1 > limit:
            await channel.send(chunk)
            chunk = ""
        chunk = chunk + "\n" + line if chunk else line
    if chunk:
        await channel.send(chunk)


async def handle_command(message, cmd, content, channel_name, guild_name,
                         db, effective_memory, current_text_model,
                         current_voice_model, current_image_model,
                         current_voice_id, current_context_limit,
                         current_reasoning_effort,
                         # Helpers from main — passed in to avoid circular imports
                         build_llm_main_block, build_trimmed_history_for_payload,
                         compose_full_messages, estimate_tokens, msg_tokens,
                         load_file_content, get_image_response,
                         auto_context_adjust=False):
    """
    Try to handle a ! command. Returns True if the command was handled,
    False if it was not a recognised command (so on_message should continue).
    """

    if cmd.startswith("!model"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            new_model = parts[1].strip()
            set_channel_setting(db, channel_name, "text_model", new_model)
            await message.channel.send(
                f"*Switched text model for `{channel_name}` to **{new_model}***"
            )

            # Auto-adjust context limit based on the model's advertised context_length
            if auto_context_adjust:
                try:
                    model_ctx = await provider.get_model_context_length(new_model)
                    if model_ctx:
                        buffer = 10000
                        new_limit = model_ctx - buffer
                        if new_limit < buffer:
                            await message.channel.send(
                                f"*⚠️ Model context length ({model_ctx:,}) is too small to auto-adjust.*"
                            )
                        else:
                            set_channel_setting(db, channel_name, "context_token_limit", str(new_limit))

                            # Calculate current usage to report headroom
                            main_block = build_llm_main_block(db, channel_name, memory_enabled=effective_memory)
                            history = build_trimmed_history_for_payload(
                                db, channel_name, main_block, context_limit=new_limit
                            )
                            full_messages = compose_full_messages(main_block, history)
                            used_tokens = sum(msg_tokens(m) for m in full_messages)
                            remaining = new_limit - used_tokens

                            ctx_msg = (
                                f"*📐 Auto-adjusted context limit to **{new_limit:,}** "
                                f"(model max {model_ctx:,} − {buffer:,} buffer).\n"
                                f"Current usage: ~{used_tokens:,} tokens — "
                                f"**~{remaining:,}** tokens remaining.*"
                            )
                            if remaining < 0:
                                ctx_msg += (
                                    f"\n*⚠️ Current session history exceeds the new limit by "
                                    f"~{abs(remaining):,} tokens. Older messages will be "
                                    f"trimmed from context on the next prompt.*"
                                )
                            await message.channel.send(ctx_msg)
                    else:
                        await message.channel.send(
                            f"*ℹ️ No context length found for `{new_model}` on {provider.provider_name()} — context limit unchanged.*"
                        )
                except Exception as e:
                    await message.channel.send(
                        f"*ℹ️ Could not auto-adjust context: {e}*"
                    )
        else:
            await message.channel.send(
                f"*Text: **{current_text_model}** | Image: **{current_image_model}** (`{channel_name}`)*"
            )
        return True

    if cmd.startswith("!imagemodel"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            new_model = parts[1].strip()
            set_channel_setting(db, channel_name, "image_model", new_model)
            await message.channel.send(
                f"*Switched image model for `{channel_name}` to **{new_model}***"
            )
        else:
            await message.channel.send(
                f"*Currently using **{current_image_model}** (`{channel_name}`)*"
            )
        return True

    if cmd.startswith("!voicetextmodel"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            new_model = parts[1].strip()
            set_channel_setting(db, channel_name, "voice_text_model", new_model)
            await message.channel.send(
                f"*Switched voice text model for `{channel_name}` to **{new_model}***"
            )
        else:
            await message.channel.send(
                f"*Voice text model for `{channel_name}`: **{current_voice_model}** "
                f"(default **{config.CURRENT_VOICE_TEXT_MODEL}**)*"
            )
        return True

    if cmd.startswith("!voicemodelid"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            new_voice_id = parts[1].strip()
            set_channel_setting(db, channel_name, "elevenlabs_voice_id", new_voice_id)
            await message.channel.send(
                f"*Switched ElevenLabs voice ID for `{channel_name}` to **{new_voice_id}***"
            )
        else:
            await message.channel.send(
                f"*ElevenLabs voice ID for `{channel_name}`: **{current_voice_id}** "
                f"(default **{config.ELEVENLABS_VOICE_ID}**)*"
            )
        return True

    if cmd == "!resetmodel":
        count = reset_all_channel_settings(db, "text_model")
        count += reset_all_channel_settings(db, "voice_text_model")
        await message.channel.send(
            f"*Reset all channels to default text model ({count} overrides removed).*\n"
            f"*Text: **{config.CURRENT_TEXT_MODEL}***"
        )
        return True

    if cmd == "!resetimagemodel":
        count = reset_all_channel_settings(db, "image_model")
        await message.channel.send(
            f"*Reset all channels to default image model ({count} overrides removed).*\n"
            f"*Image: **{config.CURRENT_IMAGE_MODEL}***"
        )
        return True

    if cmd == "!resetvoicetextmodel":
        count = reset_all_channel_settings(db, "voice_text_model")
        await message.channel.send(
            f"*Reset all channels to default voice text model ({count} overrides removed).*\n"
            f"*Voice text: **{config.CURRENT_VOICE_TEXT_MODEL}***"
        )
        return True

    if cmd == "!resetvoicemodelid":
        count = reset_all_channel_settings(db, "elevenlabs_voice_id")
        await message.channel.send(
            f"*Reset all channels to default ElevenLabs voice ID ({count} overrides removed).*\n"
            f"*Voice ID: **{config.ELEVENLABS_VOICE_ID}***"
        )
        return True

    if cmd == "!resetchannelsettings":
        cursor = db.cursor()
        cursor.execute(
            "DELETE FROM channel_settings WHERE channel = ?",
            (channel_name,)
        )
        removed = cursor.rowcount
        db.commit()
        from . import main as _main
        _main.DYNAMIC_CONTEXT_FILE_LOCATIONS.pop(channel_name, None)
        await message.channel.send(
            f"*Cleared all channel settings for `{channel_name}` ({removed} entries removed). "
            f"All defaults will now apply.*"
        )
        return True

    if cmd.startswith("!contextlimit"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            raw = parts[1].strip().replace(",", "").replace("_", "")
            try:
                new_limit = int(raw)
            except ValueError:
                await message.channel.send(
                    f"*Could not parse **{parts[1].strip()}** as an integer.*"
                )
                return True
            if new_limit <= 0:
                await message.channel.send("*Context limit must be positive.*")
                return True
            # Measure current system prompt for this channel
            main_block = build_llm_main_block(db, channel_name, memory_enabled=effective_memory)
            system_tokens = sum(estimate_tokens(b["text"]) for b in main_block)
            if new_limit <= system_tokens:
                await message.channel.send(
                    f"*Refusing to set limit to **{new_limit:,}**: the system prompt alone "
                    f"is **~{system_tokens:,}** tokens right now "
                    f"(memory {'on' if effective_memory else 'off'}). "
                    f"Pick a value above that.*"
                )
                return True
            set_channel_setting(db, channel_name, "context_token_limit", str(new_limit))
            headroom = new_limit - system_tokens
            warning = ""
            if headroom < 2000:
                warning = (
                    f"\n*⚠ Only **~{headroom:,}** tokens left for history after the system "
                    f"prompt (~{system_tokens:,}). History will be trimmed aggressively.*"
                )
            await message.channel.send(
                f"*Set context token limit for `{channel_name}` to **{new_limit:,}**.*"
                f"{warning}"
            )
        else:
            await message.channel.send(
                f"*Context limit for `{channel_name}`: **{current_context_limit:,}** "
                f"(default **{config.MAX_CONTEXT_TOKENS:,}**). "
                f"Usage: `!contextLimit <N>`*"
            )
        return True

    if cmd.startswith("!diag69105"):
        # Test Base-64 decoding is working properly
        diag_string="VW05aUlIZGhjeUJvWlhKbA=="
        unpack=base64.b64decode(diag_string)
        unpack=base64.b64decode(unpack)
        await message.channel.send(
                    f"** {unpack} **"
                )
        return True

    if cmd.startswith("!reasoning"):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            new_val = parts[1].strip().lower()
            if new_val not in ("off", "low", "medium", "high"):
                await message.channel.send(
                    "*Invalid value. Use `!reasoning off`, `!reasoning low`, `!reasoning medium`, or `!reasoning high`.*"
                )
                return True
            set_channel_setting(db, channel_name, "reasoning_effort", new_val)
            await message.channel.send(
                f"*Reasoning effort for `{channel_name}` set to **{new_val}**.*"
            )
        else:
            display = current_reasoning_effort if current_reasoning_effort else "unset"
            await message.channel.send(
                f"*Reasoning effort for `{channel_name}`: **{display}**.*"
            )
        return True

    if cmd == "!resetreasoning":
        count = reset_all_channel_settings(db, "reasoning_effort")
        await message.channel.send(
            f"*Reset all channels to default reasoning effort ({count} overrides removed).*\n"
            f"*Default: **{config.REASONING_LEVEL}***"
        )
        return True

    if cmd == "!resetcontextlimit":
        count = reset_all_channel_settings(db, "context_token_limit")
        await message.channel.send(
            f"*Reset all channels to default context limit ({count} overrides removed).*\n"
            f"*Default: **{config.MAX_CONTEXT_TOKENS:,}***"
        )
        return True

    if cmd.startswith("!image"):
        if not current_image_model or not str(current_image_model).strip():
            await message.channel.send(
                "*No image model configured. Set one with `!imagemodel <openrouter-id>` "
                "or define `CURRENT_IMAGE_MODEL` in config.py.*"
            )
            return True
        prompt = content[len("!image"):].strip()
        # Collect image attachments as reference images for image-to-image
        image_attachments = [a for a in message.attachments
                            if a.content_type and a.content_type.startswith("image/")]
        reference_images = None
        if image_attachments:
            from . import main as _main
            reference_images = await asyncio.gather(
                *[_main.url_to_data_uri(a.url) for a in image_attachments]
            )
        if not prompt and not reference_images:
            await message.channel.send("*Usage: `!image <prompt>` — attach images for image-to-image. e.g. `!image make it look like a painting`*")
            return True
        async with message.channel.typing():
            result = await get_image_response(prompt, model=current_image_model, reference_images=reference_images)
            if result is None:
                await message.channel.send("*No response from image model. Check console for details.*")
                return True
            if result["type"] == "base64":
                image_bytes = base64.b64decode(result["data"])
                file = discord.File(io.BytesIO(image_bytes), filename="image.png")
                await message.channel.send(file=file)
            elif result["type"] == "url":
                await message.channel.send(result["url"])
            elif result["type"] == "error":
                await message.channel.send(result["text"])
            else:
                # Model returned text instead of an image
                await message.channel.send(result.get("text", "*No image generated.*"))
        return True

    if cmd.startswith("!remember"):
        memory = content[len("!remember"):].strip()
        if memory:
            if memory.startswith("global "):
                memory_text = memory[len("global "):].strip()
                add_anchored_memory(db, "global", memory_text)
                await message.channel.send(f"*Remembered (global): {memory_text}*")
            else:
                add_anchored_memory(db, channel_name, memory)
                await message.channel.send(f"*Remembered ({channel_name}): {memory}*")
        return True

    if cmd == "!memories":
        memories = list_anchored_memories(db, channel_name)
        if memories:
            text = "**Anchored Memories:**\n"
            for mid, ch, mc in memories:
                tag = "global" if ch == "global" else ch
                text += f"`{mid}` [{tag}]: {mc}\n"
            await send_chunked(message.channel, text)
        else:
            await message.channel.send("*No anchored memories yet.*")
        return True

    if cmd == "!forget" or cmd.startswith("!forget "):
        parts = content.split(maxsplit=1)
        if len(parts) > 1:
            try:
                mid = int(parts[1].strip())
                if remove_anchored_memory(db, mid):
                    await message.channel.send(f"*Forgot memory #{mid}*")
                else:
                    await message.channel.send(f"*No memory with id #{mid}*")
            except ValueError:
                await message.channel.send("*Use: !forget 3*")
        return True

    if cmd == "!channel":
        await message.channel.send(f"*Current channel: **{channel_name}***")
        return True

    if cmd == "!defaultchannel":
        set_channel_setting(db, "_global", "default_channel", channel_name)
        await message.channel.send(
            f"*Default channel set to **{channel_name}** — idle action responses will be sent here.*"
        )
        return True

    if cmd.startswith("!channelrename"):
        parts = content.split(maxsplit=2)
        if len(parts) == 3:
            old_name = f"{guild_name}:::{parts[1].lower()}"
            new_name = f"{guild_name}:::{parts[2].lower()}"
            count = rename_channel(db, old_name, new_name)
            if count > 0:
                await message.channel.send(
                    f"*Renamed channel **{old_name}** → **{new_name}** ({count} messages updated).*"
                )
            else:
                await message.channel.send(
                    f"*No messages found for channel **{old_name}**.*"
                )
        else:
            await message.channel.send("*Usage: `!channelRename old_name new_name`*")
        return True

    if cmd == "!channelcontext":
        history = get_recent_messages(db, channel_name)
        # Apply same token budget trimming as the payload
        system_tokens = 0
        for path in ([config.SYSTEM_PROMPT_LOCATION] if config.SYSTEM_PROMPT_LOCATION else []) + config.INSTRUCTION_LOCATIONS:
            fc = load_file_content(path)
            if fc:
                system_tokens += estimate_tokens(fc)
        for path in config.CONTEXT_HISTORY_LOCATIONS + config.CONTEXT_REFERENCE_LOCATIONS + config.LOADED_TOOL_LOCATIONS:
            fc = load_file_content(path)
            if fc:
                system_tokens += estimate_tokens(fc)
        anchored = get_anchored_memories(db, channel_name)
        if anchored:
            system_tokens += estimate_tokens(
                "\n".join(f"{mid}. {m}" for mid, m in anchored)
            )
        history_budget = current_context_limit - system_tokens
        history_tokens = sum(estimate_tokens(msg["content"]) for msg in history)
        while history and history_tokens > history_budget:
            removed = history.pop(0)
            history_tokens -= estimate_tokens(removed["content"])

        text = f"**Context for `{channel_name}`** ({len(history)} turns, ~{history_tokens} tokens)\n"
        for msg in history:
            role = msg["role"]
            c = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
            text += f"`{role}`: {c}\n"
        # Split if needed
        while len(text) > 2000:
            sp = text[:2000].rfind('\n')
            if sp == -1:
                sp = 2000
            await message.channel.send(text[:sp])
            text = text[sp:].lstrip()
        if text:
            await message.channel.send(text)
        return True

    if cmd == "!channelcontextall":
        cursor = db.cursor()
        cursor.execute(
            "SELECT role, name, content FROM messages "
            "WHERE channel = ? ORDER BY id",
            (channel_name,)
        )
        rows = cursor.fetchall()
        total_tokens = 0
        lines = []
        for role, name, c in rows:
            display = f"{name}: {c}" if role == "user" else c
            total_tokens += estimate_tokens(display)
            lines.append(f"`{role}`: {display}")

        text = f"**All history for `{channel_name}`** ({len(rows)} turns, ~{total_tokens} tokens)\n"
        text += "\n".join(lines)
        # Split if needed
        while len(text) > 2000:
            sp = text[:2000].rfind('\n')
            if sp == -1:
                sp = 2000
            await message.channel.send(text[:sp])
            text = text[sp:].lstrip()
        if text:
            await message.channel.send(text)
        return True

    if cmd == "!join":
        if message.author.voice and message.author.voice.channel:
            success = await voice_manager.join(message.author.voice.channel)
            if success:
                await message.channel.send(f"*Joined **{message.author.voice.channel.name}***")
            else:
                await message.channel.send("*Failed to join voice channel.*")
        else:
            await message.channel.send("*You need to be in a voice channel first.*")
        return True

    if cmd == "!joinlive":
        # Hands-free live voice mode. Requires the optional discord-ext-voice-recv
        # and webrtcvad deps; livevoice.py reports which (if any) are missing.
        from . import livevoice
        from . import main as _main
        if not livevoice.dependencies_available():
            missing = ", ".join(livevoice.missing_dependencies())
            await message.channel.send(
                f"*Live voice unavailable — missing optional deps: {missing}. "
                f"Install with `python3 install_deps.py`.*"
            )
            return True
        if not (message.author.voice and message.author.voice.channel):
            await message.channel.send("*You need to be in a voice channel first.*")
            return True
        # Don't try to coexist with a regular !join voice connection.
        if voice_manager.is_connected():
            await message.channel.send(
                "*Already in a voice channel — use `!leave` first, then `!joinLive`.*"
            )
            return True
        if _main.LIVE_VOICE_SESSION is not None and _main.LIVE_VOICE_SESSION.is_active():
            await message.channel.send("*Live voice session is already running.*")
            return True

        # Build the per-utterance callback that closes over the text channel
        # we're chatting in. channel_name is captured here so the live
        # session always logs into the same channel it was started from,
        # even if the user later types in a different channel.
        live_text_channel = message.channel
        live_channel_name = channel_name

        async def _on_utterance(wav_bytes, member):
            await _main.process_live_voice_utterance(
                wav_bytes, member, live_channel_name, live_text_channel,
            )

        session = livevoice.LiveVoiceSession(
            silence_ms=getattr(_main, "LIVEVOICE_SILENCE_MS", 700),
            idle_timeout_min=getattr(_main, "LIVEVOICE_IDLE_TIMEOUT_MIN", 5),
            single_user=getattr(_main, "LIVEVOICE_SINGLE_USER", True),
            on_utterance=_on_utterance,
        )
        try:
            await session.start(
                message.author.voice.channel,
                text_channel=live_text_channel,
                author=message.author,
                loop=asyncio.get_running_loop(),
            )
        except Exception as e:
            await message.channel.send(f"*Failed to start live voice: {e}*")
            try:
                await session.stop()
            except Exception:
                pass
            return True

        _main.LIVE_VOICE_SESSION = session
        await message.channel.send(
            f"*🎙️ Live voice mode — joined **{message.author.voice.channel.name}**. "
            f"Speak normally; I'll respond after a {session.silence_ms}ms pause. "
            f"Auto-disconnects after {session.idle_timeout_min} min of silence. "
            f"Use `!leave` to stop.*"
        )
        return True

    if cmd == "!leave":
        # Tear down a live-voice session first if one is running, then fall
        # through to the regular voice_manager disconnect.
        from . import main as _main
        live_was_active = False
        if _main.LIVE_VOICE_SESSION is not None and _main.LIVE_VOICE_SESSION.is_active():
            try:
                await _main.LIVE_VOICE_SESSION.stop()
            except Exception as e:
                print(f"⚠️ live voice stop error: {e}")
            _main.LIVE_VOICE_SESSION = None
            live_was_active = True
            await message.channel.send("*Left live voice channel.*")
            return True
        if voice_manager.is_connected():
            await voice_manager.leave()
            await message.channel.send("*Left voice channel.*")
        else:
            await message.channel.send("*Not in a voice channel.*")
        return True

    if cmd in ("!knowledge", "!memory"):
        set_channel_setting(db, channel_name, "memory_enabled", "1")
        await message.channel.send(
            f"*Knowledge enabled for `{channel_name}` — all knowledge files will be loaded into context.*"
        )
        return True

    if cmd in ("!noknowledge", "!nomemory"):
        set_channel_setting(db, channel_name, "memory_enabled", "0")
        await message.channel.send(
            f"*Knowledge disabled for `{channel_name}` — only the system prompt and session history will be loaded into context.*"
        )
        return True

    if cmd == "!search":
        set_channel_setting(db, channel_name, "search_references_enabled", "1")
        await message.channel.send(
            f"*Search references enabled for `{channel_name}`.*"
        )
        return True

    if cmd == "!nosearch":
        set_channel_setting(db, channel_name, "search_references_enabled", "0")
        await message.channel.send(
            f"*Search references disabled for `{channel_name}`.*"
        )
        return True

    if cmd.startswith("!exportchat"):
        parts = content.split(maxsplit=1)
        filepath = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else None
        cursor = db.cursor()
        cursor.execute(
            "SELECT timestamp, role, content FROM messages "
            "WHERE channel = ? ORDER BY id",
            (channel_name,)
        )
        rows = cursor.fetchall()
        if not rows:
            await message.channel.send("*No messages found for this channel.*")
            return True
        messages = []
        for ts, role, content in rows:
            entry = {"role": role, "content": content}
            if ts:
                try:
                    dt = datetime.datetime.fromisoformat(ts)
                    entry["timestamp"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    entry["timestamp"] = ts
            messages.append(entry)
        payload = {"messages": messages}
        json_text = json.dumps(payload, indent=2, ensure_ascii=False)
        if filepath is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            attachment_name = f"{channel_name}_chat_{stamp}.json"
            file = discord.File(
                io.BytesIO(json_text.encode("utf-8")),
                filename=attachment_name,
            )
            await message.channel.send(
                content=f"*Exported {len(messages)} messages from `{channel_name}`.*",
                file=file,
            )
            return True
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(json_text)
            await message.channel.send(
                f"*Exported {len(messages)} messages from `{channel_name}` to `{filepath}`.*"
            )
        except OSError as e:
            await message.channel.send(f"*Failed to write to `{filepath}`: {e}*")
        return True

    if cmd == "!load" or cmd.startswith("!load "):
        parts = content.split(maxsplit=1)
        from . import main as _main
        if len(parts) < 2 or not parts[1].strip():
            rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
            if not rows:
                await message.channel.send(
                    f"*No dynamically loaded files for `{channel_name}`.*"
                )
            else:
                lines = [f"**Dynamic files for `{channel_name}`** ({len(rows)}):"]
                for _param, path in rows:
                    lines.append(f"• `{path}`")
                await send_chunked(message.channel, "\n".join(lines))
            return True
        path = parts[1].strip()
        if not os.path.isfile(path):
            await message.channel.send(
                f"*⚠️ Could not find file `{path}` — nothing loaded. Check the path and try again.*"
            )
            return True
        # Context-budget guard: make sure the system block (with this file
        # included) + a small reply reserve still fits in the channel's limit.
        new_content = load_file_content(path)
        if new_content is None:
            await message.channel.send(
                f"*⚠️ Could not read `{path}` — nothing loaded.*"
            )
            return True
        main_block = build_llm_main_block(db, channel_name, memory_enabled=effective_memory)
        current_system_tokens = sum(estimate_tokens(b["text"]) for b in main_block)
        new_file_tokens = estimate_tokens(new_content)
        REPLY_RESERVE = 2000  # tokens kept free for the model's reply
        projected = current_system_tokens + new_file_tokens + REPLY_RESERVE
        if projected > current_context_limit:
            over = projected - current_context_limit
            await message.channel.send(
                f"*⚠️ Refusing to load `{path}` — it would exceed this channel's context budget.*\n"
                f"*File: ~{new_file_tokens:,} tokens · current system block: ~{current_system_tokens:,} · "
                f"reserve: {REPLY_RESERVE:,} · limit: {current_context_limit:,} "
                f"(**over by ~{over:,} tokens**).*\n"
                f"*Unload something with `!unload`, raise the limit with `!contextLimit`, "
                f"or switch to a larger-context model.*"
            )
            return True
        param = f"dynamic_file_{uuid.uuid4()}"
        set_channel_setting(db, channel_name, param, path)
        cache = _main.DYNAMIC_CONTEXT_FILE_LOCATIONS.setdefault(channel_name, [])
        cache.append(path)
        headroom = current_context_limit - (current_system_tokens + new_file_tokens)
        await message.channel.send(
            f"*Loaded `{path}` into context for `{channel_name}` "
            f"(~{new_file_tokens:,} tokens; ~{headroom:,} tokens headroom for history + reply).*"
        )
        return True

    if cmd.startswith("!unload"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("*Usage: `!unload <pathname>`*")
            return True
        target = parts[1].strip()
        rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
        match = next((p for p, v in rows if v == target), None)
        if match is None:
            await message.channel.send(
                f"*No dynamic file `{target}` loaded for `{channel_name}`. Try `!load` to see the list.*"
            )
            return True
        clear_channel_setting(db, channel_name, match)
        from . import main as _main
        cache = _main.DYNAMIC_CONTEXT_FILE_LOCATIONS.get(channel_name)
        if cache and target in cache:
            cache.remove(target)
        await message.channel.send(
            f"*Unloaded `{target}` from `{channel_name}`.*"
        )
        return True

    if cmd == "!clear":
        cursor = db.cursor()
        cursor.execute(
            "DELETE FROM messages WHERE channel = ?",
            (channel_name,)
        )
        db.commit()
        from . import main as _main
        _main._reference_images_by_channel.pop(channel_name, None)
        # Visual session divider — renders the timestamp in the reader's
        # local timezone via Discord's dynamic <t:...:f> markup, so both the
        # bot operator and any channel participants see a clear break
        # between the old conversation and the new one.
        ts = int(message.created_at.timestamp())
        dynamic_rows = list_channel_settings_by_prefix(db, channel_name, "dynamic_file_")
        reload_note = ""
        if dynamic_rows:
            reload_note = "\nReloading Dynamic files:\n" + "\n".join(
                f"• `{path}`" for _param, path in dynamic_rows
            )
        await message.channel.send(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**· · · new session · · ·**\n"
            f"<t:{ts}:f>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Session history up to this point has been cleared.*"
            f"{reload_note}"
        )
        return True

    if cmd.startswith("!regen"):
        new_prompt = content[len("!regen"):].strip()
        cursor = db.cursor()

        if new_prompt:
            # Delete the last assistant response AND the last user prompt,
            # then resubmit with the rewritten prompt.
            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'assistant' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'user' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            db.commit()
            # Signal main.py to process this prompt through the full pipeline
            # and save both the user message and the assistant response.
            # carry_prior_attachments=False — `!regen <new>` starts fresh;
            # images / voice / text files from the prior turn are NOT carried
            # forward. Users who want them back should re-attach on the
            # !regen message itself.
            return {
                "action": "regen",
                "prompt": new_prompt,
                "save_user_prompt": True,
                "carry_prior_attachments": False,
            }
        else:
            # Recover the last user prompt text, then delete BOTH the last
            # assistant response and the last user entry. We delete + re-save
            # rather than leaving the user turn in place because the DB only
            # stores text — any image / voice / file attachments from the
            # original message would be lost. Re-running through the normal
            # pipeline (with attachments pulled from Discord channel history
            # in on_message) preserves them on the regenerated turn.
            cursor.execute(
                "SELECT content FROM messages WHERE channel = ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1",
                (channel_name,)
            )
            row = cursor.fetchone()
            if not row:
                await message.channel.send("*No user message found to regenerate from.*")
                return True
            last_user_prompt = row[0]
            # Strip any trailing "[image]" marker added when the original
            # message was saved — the attachment itself will be re-attached
            # by on_message from Discord channel history, so we don't want
            # the stringified marker echoed in the prompt text.
            if last_user_prompt.endswith(" [image]"):
                last_user_prompt = last_user_prompt[: -len(" [image]")]
            elif last_user_prompt == "[image]":
                last_user_prompt = ""

            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'assistant' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            cursor.execute(
                "DELETE FROM messages WHERE id = ("
                "  SELECT id FROM messages WHERE channel = ? AND role = 'user' "
                "  ORDER BY id DESC LIMIT 1"
                ")", (channel_name,)
            )
            db.commit()
            # save_user_prompt=True so on_message rebuilds the user turn via
            # the normal path. carry_prior_attachments=True tells main.py to
            # scan Discord channel history for the prior user message so any
            # images / voice / text files from it get re-attached. If the
            # scan comes up empty, main.py silently falls back to text-only
            # (combined_content already holds the DB text).
            return {
                "action": "regen",
                "prompt": last_user_prompt,
                "save_user_prompt": True,
                "carry_prior_attachments": True,
            }

    if cmd.startswith("!copycontextfrom"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send("Usage: `!copyContextFrom <channel_name>`")
            return True
        source_channel = f"{guild_name}:::{parts[1].strip().lower()}"
        if source_channel == channel_name:
            await message.channel.send("You're already in that channel, silly. 🙃 Pick a *different* channel to copy from!")
            return True
        cursor = db.cursor()
        # Check that the source channel has messages
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE channel = ?",
            (source_channel,)
        )
        if cursor.fetchone()[0] == 0:
            await message.channel.send(f"*No messages found in channel '{source_channel}'.*")
            return True
        # Clear current channel
        cursor.execute(
            "DELETE FROM messages WHERE channel = ?",
            (channel_name,)
        )
        # Copy messages from source channel with new IDs
        cursor.execute(
            "INSERT INTO messages (timestamp, channel, role, name, content) "
            "SELECT timestamp, ?, role, name, content FROM messages "
            "WHERE channel = ? ORDER BY id",
            (channel_name, source_channel)
        )
        db.commit()
        # Find the last assistant response in the newly copied messages
        cursor.execute(
            "SELECT content FROM messages WHERE channel = ? AND role = 'assistant' "
            "ORDER BY id DESC LIMIT 1",
            (channel_name,)
        )
        row = cursor.fetchone()
        last_response = row[0] if row else "(no prior response found)"
        header = f"*Picking up where we left off from channel {source_channel}. My last response was:*\n\n"
        await send_chunked(message.channel, header + last_response)
        return True

    if cmd == "!context":

        # Build the LLM augmented prompt
        main_block = build_llm_main_block(db, channel_name, memory_enabled=effective_memory)

        # Now Add Session History (as much will fit in context)
        history = build_trimmed_history_for_payload(
            db, channel_name, main_block, context_limit=current_context_limit
        )

        full_messages = compose_full_messages(main_block, history)

        prompt_tokens = sum(msg_tokens(m) for m in full_messages)

        system_prompt_tokens = msg_tokens(full_messages[0])
        history_prompt_tokens = sum(msg_tokens(m) for m in full_messages[1:])
        remaining = current_context_limit - prompt_tokens
        await message.channel.send(
            f"**Context** (`{channel_name}`)\n"
            f"Estimated **~{prompt_tokens:,}** tokens in the next payload "
            f"(system **~{system_prompt_tokens:,}**, history **~{history_prompt_tokens:,}**, "
            f"{len(history)} turns).\n"
            f"Budget **{current_context_limit:,}** — **~{remaining:,}** tokens headroom "
            f"(before your message and the model reply).\n"
            "_Same ~1 token per 4 characters as console logging._"
        )
        return True

    if cmd == "!credits":
        lines = ["**API credits**"]
        # LLM provider
        credits = await provider.get_credits()
        if credits["error"]:
            lines.append(f"• **{credits['label']}**: {credits['error']}")
        elif credits["used"] > 0:
            lines.append(
                f"• **{credits['label']}**: ${credits['remaining']:.2f} remaining "
                f"(${credits['used']:.2f} used of ${credits['total']:.2f})"
            )
        else:
            lines.append(
                f"• **{credits['label']}**: ${credits['remaining']:.2f} remaining"
            )

        # ElevenLabs
        if config.ELEVENLABS_API_KEY and str(config.ELEVENLABS_API_KEY).strip():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.elevenlabs.io/v1/user/subscription",
                        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            used = int(data.get("character_count", 0))
                            limit = int(data.get("character_limit", 0))
                            remaining = max(limit - used, 0)
                            tier = data.get("tier", "?")
                            lines.append(
                                f"• **ElevenLabs** ({tier}): {remaining:,} chars remaining "
                                f"({used:,} / {limit:,} used)"
                            )
                        else:
                            body = (await resp.text())[:120]
                            lines.append(f"• **ElevenLabs**: error {resp.status} — {body}")
            except Exception as e:
                lines.append(f"• **ElevenLabs**: request failed — {e}")
        else:
            lines.append("• **ElevenLabs**: no API key configured")

        await send_chunked(message.channel, "\n".join(lines))
        return True

    if cmd == "!diag":
        sections = []  # list of strings, each sent as a separate message

        # ── 1. LLM provider connectivity + credits + model validation ──
        prov_label = provider.provider_name()
        or_lines = [f"**🔍 Diagnostics**\n────────────────────\n**{prov_label}**"]

        # Check credits
        credits = await provider.get_credits()
        if credits["error"]:
            or_lines.append(f"❌ {credits['error']}")
        elif credits["remaining"] > 0:
            or_lines.append(f"✅ Connected — credits active (${credits['remaining']:.2f} remaining)")
        else:
            or_lines.append(f"⚠️ Connected — **no credits remaining** (${credits['used']:.2f} / ${credits['total']:.2f} used)")

        # Fetch available models for validation
        available_models = await provider.get_models()
        available_ids = {m["id"] for m in available_models} if available_models else None
        if available_models is None or (not available_models and not credits["error"]):
            or_lines.append(f"⚠️ Could not fetch model list")

        # Validate configured models
        from . import main as _main
        models_to_check = {
            "(default) Text model": config.CURRENT_TEXT_MODEL,
            "(default) Image model": config.CURRENT_IMAGE_MODEL,
            "(default) Voice text model": config.CURRENT_VOICE_TEXT_MODEL,
            "(default) Internal logic model": getattr(_main, "INTERNAL_LOGIC_MODEL", None),
        }
        # Include per-channel overrides for this channel
        ch_text = get_channel_setting(db, channel_name, "text_model")
        ch_image = get_channel_setting(db, channel_name, "image_model")
        ch_voice = get_channel_setting(db, channel_name, "voice_text_model")
        if ch_text:
            models_to_check[f"(current) Text model (`{channel_name}`)"] = ch_text
        if ch_image:
            models_to_check[f"(current) Image model (`{channel_name}`)"] = ch_image
        if ch_voice:
            models_to_check[f"(current) Voice text model (`{channel_name}`)"] = ch_voice

        or_lines.append("")
        or_lines.append("**Models**")
        for label, model_id in models_to_check.items():
            if not model_id or not str(model_id).strip():
                or_lines.append(f"⬜ {label}: *(not configured)*")
                continue
            normalized = provider.normalize_model_id(model_id)
            if available_ids is not None:
                if normalized in available_ids:
                    or_lines.append(f"✅ {label}: `{model_id}`")
                else:
                    or_lines.append(f"❌ {label}: `{model_id}` — **not found on {prov_label}**")
            else:
                or_lines.append(f"❓ {label}: `{model_id}` *(could not verify)*")

        sections.append("\n".join(or_lines))

        # ── 2. ElevenLabs connectivity ──
        el_lines = ["**ElevenLabs**"]
        if config.ELEVENLABS_API_KEY and str(config.ELEVENLABS_API_KEY).strip():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.elevenlabs.io/v1/user/subscription",
                        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            used = int(data.get("character_count", 0))
                            limit = int(data.get("character_limit", 0))
                            remaining = max(limit - used, 0)
                            tier = data.get("tier", "?")
                            if remaining > 0:
                                el_lines.append(f"✅ Connected ({tier}) — {remaining:,} chars remaining")
                            else:
                                el_lines.append(f"⚠️ Connected ({tier}) — **no characters remaining** ({used:,} / {limit:,})")
                        else:
                            body = (await resp.text())[:120]
                            el_lines.append(f"❌ API error {resp.status} — {body}")
            except Exception as e:
                el_lines.append(f"❌ Connection failed — {e}")

            el_lines.append(f"Voice model: `{getattr(config, 'ELEVENLABS_VOICE_MODEL', 'N/A')}`")
            el_lines.append(f"Voice ID: `{getattr(config, 'ELEVENLABS_VOICE_ID', 'N/A')}`")
            ch_voice_id = get_channel_setting(db, channel_name, "elevenlabs_voice_id")
            if ch_voice_id:
                el_lines.append(f"Voice ID (`{channel_name}`): `{ch_voice_id}`")
        else:
            el_lines.append("⬜ No API key configured")

        sections.append("\n".join(el_lines))

        # ── 3. System tools & dependencies ──
        sys_lines = ["**System**"]

        # ffmpeg
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-version"],
                    capture_output=True, text=True, timeout=5,
                )
                version_line = result.stdout.split("\n")[0] if result.stdout else "unknown version"
                sys_lines.append(f"✅ ffmpeg: `{version_line}`")
            except Exception:
                sys_lines.append(f"✅ ffmpeg: found at `{ffmpeg_path}` (version unknown)")
        else:
            sys_lines.append("❌ ffmpeg: **not found** — voice playback will not work")

        # opus
        if discord.opus.is_loaded():
            sys_lines.append("✅ libopus: loaded")
        else:
            sys_lines.append("❌ libopus: **not loaded** — voice will not work")

        # System prompt
        sp_path = getattr(config, "SYSTEM_PROMPT_LOCATION", None)
        if sp_path and str(sp_path).strip():
            if os.path.isfile(sp_path):
                size = os.path.getsize(sp_path)
                sys_lines.append(f"✅ System prompt: `{os.path.basename(sp_path)}` ({size:,} bytes)")
            else:
                sys_lines.append(f"❌ System prompt: `{sp_path}` — **file not found**")
        else:
            sys_lines.append("⬜ System prompt: *(not configured — using built-in default)*")

        # Instruction files
        for i, path in enumerate(config.INSTRUCTION_LOCATIONS, 1):
            if os.path.isfile(path):
                sys_lines.append(f"✅ Instruction {i}: `{os.path.basename(path)}`")
            else:
                sys_lines.append(f"❌ Instruction {i}: `{path}` — **not found**")

        # Tool files
        for i, path in enumerate(config.LOADED_TOOL_LOCATIONS, 1):
            if os.path.isfile(path):
                sys_lines.append(f"✅ Tool {i}: `{os.path.basename(path)}`")
            else:
                sys_lines.append(f"❌ Tool {i}: `{path}` — **not found**")

        sections.append("\n".join(sys_lines))

        # Send each section as a separate message (stay under 2000 chars)
        for section in sections:
            if len(section) <= 2000:
                await message.channel.send(section)
            else:
                remaining_text = section
                while len(remaining_text) > 2000:
                    sp = remaining_text[:2000].rfind('\n')
                    if sp == -1:
                        sp = 2000
                    await message.channel.send(remaining_text[:sp])
                    remaining_text = remaining_text[sp:].lstrip()
                if remaining_text:
                    await message.channel.send(remaining_text)
        return True

    # ── Macros ──
    if cmd == "!macros":
        rows = list_global_vars(db, param_prefix="macro_")
        stored = {param: value for param, value in rows}
        lines = ["**Macros**"]
        for n in range(MACRO_MIN, MACRO_MAX + 1):
            value = stored.get(f"macro_{n}")
            if value:
                display = value if len(value) <= 40 else value[:40] + "..."
                lines.append(f"`!M{n}` {display}")
            else:
                lines.append(f"`!M{n}`")
        await send_chunked(message.channel, "\n".join(lines))
        return True

    if cmd.startswith("!forgetmacro"):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send(
                f"*Usage: `!forgetMacro M<n>` where n is {MACRO_MIN}-{MACRO_MAX}*"
            )
            return True
        target = parts[1].strip()
        m = re.match(r"^[Mm](\d+)$", target)
        if not m:
            await message.channel.send(
                f"*Invalid macro reference **{target}**. Use `M1`..`M{MACRO_MAX}`.*"
            )
            return True
        n = int(m.group(1))
        if not (MACRO_MIN <= n <= MACRO_MAX):
            await message.channel.send(
                f"*Macro number must be between {MACRO_MIN} and {MACRO_MAX}.*"
            )
            return True
        if delete_global_var(db, f"macro_{n}"):
            await message.channel.send(f"*Macro M{n} forgotten.*")
        else:
            await message.channel.send(f"*Macro M{n} was not set.*")
        return True

    _macro_match = _MACRO_RE.match(content.strip())
    if _macro_match:
        n = int(_macro_match.group(1))
        if not (MACRO_MIN <= n <= MACRO_MAX):
            await message.channel.send(
                f"*Macro number must be between {MACRO_MIN} and {MACRO_MAX}.*"
            )
            return True
        param = f"macro_{n}"
        arg = _macro_match.group(2)
        if arg is None or not arg.strip():
            # ── Execute macro ──
            stored = get_global_var(db, param)
            if stored is None:
                await message.channel.send(f"*Macro M{n} is undefined.*")
                return True
            stored_stripped = stored.strip()
            # Guard against macros invoking other macros (prevents loops).
            if _MACRO_RE.match(stored_stripped):
                await message.channel.send(
                    "*Macros cannot invoke other macros.*"
                )
                return True
            if stored_stripped.startswith("!"):
                # Recursively dispatch the stored ! command as if typed.
                inner = await handle_command(
                    message, stored_stripped.lower(), stored_stripped,
                    channel_name, guild_name, db, effective_memory,
                    current_text_model, current_voice_model, current_image_model,
                    current_voice_id, current_context_limit,
                    current_reasoning_effort,
                    build_llm_main_block, build_trimmed_history_for_payload,
                    compose_full_messages, estimate_tokens, msg_tokens,
                    load_file_content, get_image_response,
                    auto_context_adjust,
                )
                if inner is False:
                    # Unrecognised ! command inside the macro — treat the
                    # stored text as a regular prompt.
                    return {"action": "execute", "prompt": stored_stripped}
                return inner
            # Plain text prompt — signal main.py to run it through the
            # normal response pipeline.
            return {"action": "execute", "prompt": stored_stripped}
        # ── Set macro ──
        value = arg.strip()
        set_global_var(db, param, value)
        await message.channel.send(f"*Macro M{n} set.*")
        return True

    if cmd == "!help":
        # Discord caps messages at 2000 characters, so send each section
        # as its own message. Keep each section well under 2000 on its own.
        help_sections = [
            # Group 1: Models + Resets
            "**Companion commands**\n"
            "────────────────────\n"
            "**Models**\n"
            "`!model` — show text and image models, or set text model for this channel: `!model <openrouter-id>`\n"
            "`!imageModel` — show or set image model for this channel: `!imagemodel <openrouter-id>`\n"
            "`!voiceTextModel` — show or set the text model used for voice replies: `!voicetextmodel <openrouter-id>`\n"
            "`!voiceModelID` — show or set the ElevenLabs voice ID for this channel: `!voicemodelid <voice-id>`\n"
            "`!reasoning <off|low|medium|high>` — set extended thinking level for this channel\n"
            "────────────────────\n"
            "**Resets**\n"
            "`!resetChannelSettings` — clear all settings (models, context limit, memory/search overrides, etc.) for THIS channel\n"
            "`!resetModel` — reset text model to default across ALL channels\n"
            "`!resetImageModel` — reset image model to default across ALL channels\n"
            "`!resetVoiceTextModel` — reset voice text model to default across ALL channels\n"
            "`!resetVoiceModelID` — reset ElevenLabs voice ID to default across ALL channels\n"
            "`!resetContextLimit` — reset context limit to default across all channels",

            # Group 2: Context + Images + Memory & Knowledge
            "────────────────────\n"
            "**Context**\n"
            "`!context` — show the estimated payload tokens and headroom left (same math as console)\n"
            "`!clear` — delete session context for this channel\n"
            "`!regen` — regenerate the last response, or `!regen <new prompt>` to replace and resubmit\n"
            "`!copyContextFrom <channel>` — erase this channel's history and copy all messages from the named channel\n"
            "`!exportChat [pathname]` — export all messages for this channel to a JSON file (omit pathname to attach it directly to the chat)\n"
            "────────────────────\n"
            "**Images**\n"
            "`!image <prompt>` — generate an image from a prompt\n"
            "────────────────────\n"
            "**Memory & Knowledge**\n"
            "`!remember [global] <text>` — anchor a memory to a specific channel or across all channels\n"
            "`!memories` — list anchored memories (with ids)\n"
            "`!forget <id>` — remove an anchored memory by id\n"
            "\n`!knowledge` — enable knowledge files in context for this channel (default)\n"
            "`!noknowledge` — disable knowledge files from context for this channel\n"
	            "`!search` — enable fusion search of reference files for this channel (default)\n"
	            "`!nosearch` — disable fusion search of reference files for this channel\n"
            "\n`!load <pathname>` — dynamically load a speciality file into context for THIS channel\n"
            "`!load` — list dynamically loaded files for this channel\n"
            "`!unload <pathname>` — remove a dynamically loaded file from this channel",

            # Group 3: Voice + Macros + Other
            "────────────────────\n"
            "**Voice**\n"
            "`!join` — join your current voice channel\n"
            "`!leave` — disconnect from voice\n"
            "────────────────────\n"
            "**Macros**\n"
            f"`!M<n> <prompt or !command>` — set macro n (n = {MACRO_MIN}-{MACRO_MAX})\n"
            f"`!M<n>` — execute the stored macro n\n"
            "`!macros` — list all macros\n"
            "`!forgetMacro M<n>` — remove a specific macro\n"
            "────────────────────\n"
            "**Other**\n"
            "`!channel` — show this channel's id string\n"
            "`!channelRename <old> <new>` — rename a channel in the database (migrate history after a Discord rename)\n"
            "`!credits` — show remaining credits on your LLM provider and ElevenLabs\n"
            "`!diag` — run diagnostics (API connectivity, model validation, system tools)\n"
            "`!help` — this list",
        ]
        for section in help_sections:
            await message.channel.send(section)
        return True

    # Not a recognised command. If it looks like a typo'd command
    # (short message whose second character is alphanumeric — i.e. `!foo`
    # rather than `! hey`, `!!`, `!?`, etc.), report it back so we don't
    # waste LLM tokens on it.
    if len(content) < 60 and len(content) >= 2 and content[1].isalnum():
        command_word = content.split(maxsplit=1)[0]
        await message.channel.send(
            f"*Sorry, `{command_word}` is not a recognized command.*"
        )
        return True

    return False
