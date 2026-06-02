# ============================================
# Alcove v1.3.0 — directives.py
# Directive parsing and execution
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import base64
import config
from .database import add_anchored_memory, remove_anchored_memory
import discord
import io
import os
import random
import subprocess
import re
import trafilatura


# Parse XML-style directive blocks from LLM response text.
# Returns a list of segments in order, each being either:
#   {"type": "text", "content": "..."}                    — normal text to send to Discord
#   {"type": "output", "path": "...", "content": "..."}   — write content to file
#   {"type": "runcmd", "command": "..."}                  — run a shell command
#   {"type": "createimage", "prompt": "..."}              — generate an image
#   {"type": "readweb", "url": "..."}                    — fetch readable content from a URL
#   {"type": "readimage", "source": "..."}                — load an image (URL or local path) for the model to see
#
# Format:
#   <output path="/path/to/file.txt">
#   file content here
#   </output>
#
#   <runcmd>
#   echo "hello"
#   </runcmd>
#
#   <createimage>
#   a cat wearing a top hat in watercolor style
#   </createimage>
#
#   <readweb>
#   https://example.com/article
#   </readweb>
#
#   <saveglobalanchor>
#   Pippin's birthday is October 16th
#   </saveglobalanchor>
#
#   <react>
#   🥰😅
#   </react>

_KNOWN_DIRECTIVES = {"output", "runcmd", "createimage", "readweb", "readimage", "saveglobalanchor", "deleteglobalanchor", "react"}
_DIRECTIVE_OPEN = re.compile(r'^<(output|runcmd|createimage|readweb|readimage|saveglobalanchor|deleteglobalanchor|react)(?:\s+path="([^"]*)")?(?:\s+use="([^"]*)")?\s*>', re.IGNORECASE)
_DIRECTIVE_CLOSE = re.compile(r'^</(output|runcmd|createimage|readweb|readimage|saveglobalanchor|deleteglobalanchor|react)\s*>', re.IGNORECASE)


def _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=None):
    # Build and append a parsed directive segment. Shared between the
    # "close tag on its own line" and "close tag at end of a content line"
    # paths so both produce identical output.
    body = "\n".join(body_lines)
    if directive_type == "output":
        segments.append({
            "type": "output",
            "path": directive_arg,
            "content": body,
        })
    elif directive_type == "runcmd":
        command = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "runcmd", "command": command.strip()})
    elif directive_type == "createimage":
        prompt = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "createimage", "prompt": prompt.strip(), "use": directive_use})
    elif directive_type == "readweb":
        url = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "readweb", "url": url.strip()})
    elif directive_type == "readimage":
        source = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "readimage", "source": source.strip()})
    elif directive_type == "saveglobalanchor":
        memory = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "saveglobalanchor", "content": memory.strip()})
    elif directive_type == "deleteglobalanchor":
        raw_id = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "deleteglobalanchor", "memory_id": raw_id.strip()})
    elif directive_type == "react":
        emojis_raw = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "react", "emojis": emojis_raw.strip()})


def parse_directives(response_text):
    segments = []
    text_buffer = []
    body_lines = []
    current_directive = None  # (type, arg, use)

    for line in response_text.split("\n"):
        stripped = line.strip()
        if current_directive is None:
            # Not inside a directive — look for an opening tag.
            # Strip stray characters the LLM may add (markdown, backticks, etc.)
            clean = re.sub(r'[`*]', '', stripped)
            match = _DIRECTIVE_OPEN.match(clean)
            if match:
                # Flush any accumulated text
                if text_buffer:
                    joined = "\n".join(text_buffer).strip()
                    if joined:
                        segments.append({"type": "text", "content": joined})
                    text_buffer = []
                directive_type = match.group(1).lower()
                directive_arg = (match.group(2) or "").strip()
                directive_use = (match.group(3) or "").strip() or None
                current_directive = (directive_type, directive_arg, directive_use)
                body_lines = []
            else:
                text_buffer.append(line)
        else:
            # Inside a directive — look for closing tag
            clean = re.sub(r'[`*]', '', stripped)
            close_match = _DIRECTIVE_CLOSE.match(clean)
            if close_match and close_match.group(1).lower() == current_directive[0]:
                # Clean "close-tag on its own line" case.
                directive_type, directive_arg, directive_use = current_directive
                _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=directive_use)
                current_directive = None
                body_lines = []
            else:
                # Models sometimes drop the closer at the end of the last
                # content line instead of on its own line, e.g.:
                #   <runcmd>
                #   echo hello</runcmd>
                # Detect an embedded closer that matches the currently-open
                # directive, treat any text before it as the last line of
                # body, and any text after it as a new text segment.
                embedded_pat = re.compile(
                    rf'</\s*{re.escape(current_directive[0])}\s*>',
                    re.IGNORECASE,
                )
                emb = embedded_pat.search(line)
                if emb:
                    prefix = line[: emb.start()]
                    suffix = line[emb.end():]
                    # Trim a single trailing markdown char (backtick/asterisk)
                    # from the prefix in case the model wrote e.g. `</foo>`.
                    prefix_trimmed = re.sub(r'[`*]+$', '', prefix)
                    if prefix_trimmed.strip():
                        body_lines.append(prefix_trimmed)
                    directive_type, directive_arg, directive_use = current_directive
                    _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=directive_use)
                    current_directive = None
                    body_lines = []
                    # Any text after the closer on the same line becomes
                    # regular text for the following segment.
                    suffix_trimmed = re.sub(r'^[`*]+', '', suffix).rstrip()
                    if suffix_trimmed.strip():
                        text_buffer.append(suffix_trimmed)
                else:
                    body_lines.append(line)

    # If we ended mid-directive, treat the whole thing as text (malformed)
    if current_directive is not None:
        directive_type, directive_arg, _directive_use = current_directive
        raw = f"<{directive_type}>\n" + "\n".join(body_lines)
        text_buffer.append(raw)

    # Flush remaining text
    if text_buffer:
        joined = "\n".join(text_buffer).strip()
        if joined:
            segments.append({"type": "text", "content": joined})

    return segments


def execute_output(path, content):
    # Write content to a file, creating parent directories if needed.
    # If the file already exists, add a datestamp suffix to avoid overwriting.
    # Returns (success: bool, message: str)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base, ext = os.path.splitext(path)
            path = f"{base}_{stamp}{ext}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True, f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return False, f"Failed to write {path}: {e}"


def execute_runcmd(command):
    # Run a shell command and return its output.
    # Returns (success: bool, message: str)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip()
            return False, f"Command exited {result.returncode}: {error or output}"
        return True, output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Command timed out (30s limit)"
    except Exception as e:
        return False, f"Failed to run command: {e}"


async def execute_readweb(url, token_budget=None):
    # Fetch a web page and extract readable content using trafilatura.
    # token_budget: available tokens remaining before hitting the context limit.
    #   If provided, content is truncated so its estimated tokens (len/4) stay
    #   within that budget (minus a 10,000-token reserve for the model's reply).
    # Returns (success: bool, message: str)
    RESERVE_TOKENS = 20000
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status} fetching {url}"
                html = await response.text()
        content = trafilatura.extract(html)
        if not content or not content.strip():
            return False, f"No readable content extracted from {url}"
        # Truncate based on available context budget
        if token_budget is not None:
            max_chars = (token_budget - RESERVE_TOKENS) * 4
            if max_chars < 400:
                return False, "Not enough context budget remaining to read this page."
            if len(content) > max_chars:
                content = content[:max_chars] + (
                    "\n\n... (Content truncated — total size was approaching the context limit. "
                    "Please let the user know that the full article could not be loaded.)"
                )
        return True, content
    except aiohttp.ClientError as e:
        return False, f"Failed to fetch {url}: {e}"
    except Exception as e:
        return False, f"Error reading {url}: {e}"


_READIMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_READIMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB cap on local file reads


async def execute_readimage(source):
    # Resolve a <readimage> source (URL or local file path) to an image_url
    # value suitable for a multimodal user-content block.
    # Returns (success, result) where on success result is a dict
    #   {"image_url": <url or data-uri>, "display": <short human label>}
    # and on failure result is an error message string.
    source = (source or "").strip()
    if not source:
        return False, "readimage: empty source"

    # URLs: pass through directly (same way Discord attachment URLs are handled
    # elsewhere in the bot). Models fetch the URL themselves.
    lowered = source.lower()
    if lowered.startswith(("http://", "https://")):
        return True, {"image_url": source, "display": source}

    # Otherwise treat as a local filesystem path. Strip surrounding quotes the
    # LLM may add around Windows paths with spaces.
    path = source.strip('"').strip("'")
    if not os.path.isfile(path):
        return False, f"readimage: file not found: {path}"

    ext = os.path.splitext(path)[1].lower()
    mime = _READIMAGE_MIME_BY_EXT.get(ext)
    if not mime:
        supported = ", ".join(sorted(_READIMAGE_MIME_BY_EXT.keys()))
        return False, f"readimage: unsupported image type '{ext or '(no extension)'}' (supported: {supported})"

    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"readimage: cannot stat {path}: {e}"
    if size > _READIMAGE_MAX_BYTES:
        mb = size / (1024 * 1024)
        return False, f"readimage: file too large ({mb:.1f} MB, max 10 MB)"

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return False, f"readimage: failed to read {path}: {e}"

    b64 = base64.b64encode(raw).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"
    return True, {"image_url": data_uri, "display": os.path.basename(path)}


def _extract_utf8_emojis(text):
    """Extract individual Unicode emoji characters from a string, ignoring non-emoji."""
    # Match emoji sequences: flags, ZWJ sequences, keycaps, and single emoji codepoints
    emoji_pattern = re.compile(
        "["
        "\U0001F1E0-\U0001F1FF"   # flags (regional indicators)
        "\U0001F300-\U0001F5FF"   # symbols & pictographs
        "\U0001F600-\U0001F64F"   # emoticons
        "\U0001F680-\U0001F6FF"   # transport & map
        "\U0001F700-\U0001F77F"   # alchemical symbols
        "\U0001F780-\U0001F7FF"   # geometric shapes extended
        "\U0001F800-\U0001F8FF"   # supplemental arrows-C
        "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
        "\U0001FA00-\U0001FA6F"   # chess symbols
        "\U0001FA70-\U0001FAFF"   # symbols & pictographs extended-A
        "\U00002702-\U000027B0"   # dingbats
        "\U0000FE00-\U0000FE0F"   # variation selectors
        "\U0000200D"              # ZWJ
        "\U000024C2-\U0001F251"
        "\U00002600-\U000026FF"   # misc symbols
        "\U00002700-\U000027BF"   # dingbats
        "\U0000231A-\U0000231B"
        "\U000023E9-\U000023F3"
        "\U000023F8-\U000023FA"
        "\U000025AA-\U000025AB"
        "\U000025B6\U000025C0"
        "\U000025FB-\U000025FE"
        "\U00002934-\U00002935"
        "\U00002B05-\U00002B07"
        "\U00002B1B-\U00002B1C"
        "\U00002B50\U00002B55"
        "\U00003030\U0000303D"
        "\U00003297\U00003299"
        "\U0000200D"              # ZWJ (repeated to be safe)
        "]+",
        flags=re.UNICODE,
    )
    # Find all emoji clusters, then split ZWJ sequences apart only if they
    # aren't real ZWJ emoji (keep connected sequences as single reactions).
    matches = emoji_pattern.findall(text)
    # Discord needs each reaction added individually; split clusters that
    # are just concatenated single emoji (no ZWJ between them).
    emojis = []
    for m in matches:
        # If the cluster contains ZWJ it's a combined emoji — keep whole
        if "\u200d" in m:
            emojis.append(m)
        else:
            # Split into individual grapheme clusters.  A simple approach:
            # iterate codepoints and re-merge variation selectors / skin tones.
            buf = ""
            for ch in m:
                if "\uFE00" <= ch <= "\uFE0F" or "\U0001F3FB" <= ch <= "\U0001F3FF":
                    buf += ch  # attach modifier to previous
                else:
                    if buf:
                        emojis.append(buf)
                    buf = ch
            if buf:
                emojis.append(buf)
    return emojis[:5]  # cap at 5 per the tool spec


async def process_response(response_text, channel, image_handler=None, token_budget=None, db=None, user_message=None, consecutive_reacts=0, suppress_reacts=False):
    # Parse directives from the response, execute them, and return:
    #   (display_text, runcmd_results, readweb_results, readimage_results, had_react, react_was_posted)
    # display_text: remaining text to send to Discord
    # runcmd_results: list of {"command": ..., "success": bool, "output": ...} dicts
    # readweb_results: list of {"url": ..., "success": bool, "output": ...} dicts
    # readimage_results: list of {"source": ..., "success": bool, "image_url": ..., "error": ...} dicts
    #   — image_url is a real URL or a data: URI and is None on failure; error is None on success.
    # had_react: True if the response contained a <react> directive (even if skipped)
    # Directive results are also reported back to the channel as status messages.
    # image_handler is an async function(prompt, reference_images=None) that returns an image result dict.
    # suppress_reacts: if True, <react> directives are parsed but silently skipped
    #   (used for tool-chain follow-up rounds so reacts only fire on the first response).
    segments = parse_directives(response_text)
    text_parts = []
    runcmd_results = []
    readweb_results = []
    readimage_results = []
    had_react = False
    react_was_posted = False

    for seg in segments:
        if seg["type"] == "text":
            text_parts.append(seg["content"])

        elif seg["type"] == "output":
            success, msg = execute_output(seg["path"], seg["content"])
            icon = "📄" if success else "⚠️"
            print(f"{icon} output directive: {msg}")
            await channel.send(f"*{icon} {msg}*")

        elif seg["type"] == "runcmd":
            print(f"🔧 runcmd directive: {seg['command'][:100]}")
            # Always echo the command(s) to Discord before execution so the
            # user can see what's about to run, regardless of DISPLAY_CMD_OUTPUT
            # (which still controls whether the OUTPUT is posted afterwards).
            # Multi-line runcmd blocks are rendered one command per line.
            raw_cmd = seg["command"] or ""
            lines = [ln.rstrip() for ln in raw_cmd.split("\n")]
            lines = [ln for ln in lines if ln.strip()]  # drop empty lines
            MAX_LINE_CHARS = 400  # keep any single command readable
            display_lines = [
                (ln if len(ln) <= MAX_LINE_CHARS
                 else ln[:MAX_LINE_CHARS] + "... (truncated)")
                for ln in lines
            ]
            cmd_block = "\n".join(display_lines) or "(empty)"
            # Neutralize any stray triple-backticks so they don't break out
            # of the Discord code block (rare in shell, but worth guarding).
            cmd_block = cmd_block.replace("```", "'' '")
            # Cap total message size; Discord limit is 2000 chars.
            if len(cmd_block) > 1800:
                cmd_block = cmd_block[:1800] + "\n... (truncated)"
            await channel.send(f"*🔧 Running:*\n```\n{cmd_block}\n```")

            success, msg = execute_runcmd(seg["command"])
            icon = "✅" if success else "⚠️"
            print(f"{icon} runcmd result: {msg[:200]}")
            # Output is still gated by DISPLAY_CMD_OUTPUT — only the command
            # echo above is unconditional.
            if config.DISPLAY_CMD_OUTPUT:
                display = msg if len(msg) <= 1500 else msg[:1500] + "... (truncated)"
                await channel.send(f"*{icon} `{seg['command'][:100]}`*\n```\n{display}\n```")
            runcmd_results.append({
                "command": seg["command"],
                "success": success,
                "output": msg,
            })

        elif seg["type"] == "createimage":
            if image_handler is None:
                print("⚠️ createimage directive but no image_handler provided")
                await channel.send("*⚠️ Image generation not available*")
                continue
            prompt = seg["prompt"]
            use_refs = seg.get("use") == "reference"
            ref_label = " with references" if use_refs else ""
            print(f"🎨 createimage directive{ref_label}: {prompt[:100]}")
            await channel.send(f"*🎨 Generating image{ref_label}...*")
            result = await image_handler(prompt, reference_images=None if use_refs else [])
            if result is None:
                await channel.send("*⚠️ No response from image model.*")
            elif result["type"] == "base64":
                image_bytes = base64.b64decode(result["data"])
                file = discord.File(io.BytesIO(image_bytes), filename="image.png")
                await channel.send(file=file)
            elif result["type"] == "url":
                await channel.send(result["url"])
            elif result["type"] == "error":
                await channel.send(result["text"])
            else:
                await channel.send(result.get("text", "*⚠️ No image generated.*"))

        elif seg["type"] == "readweb":
            url = seg["url"]
            print(f"🌐 readweb directive: {url}")
            await channel.send(f"*🌐 Reading {url}...*")
            success, content = await execute_readweb(url, token_budget=token_budget)
            icon = "✅" if success else "⚠️"
            print(f"{icon} readweb result: {content[:200]}")
            readweb_results.append({
                "url": url,
                "success": success,
                "output": content,
            })

        elif seg["type"] == "readimage":
            source = seg["source"]
            print(f"🖼️ readimage directive: {source}")
            # await channel.send(f"*Reading image: {source}*")
            success, result = await execute_readimage(source)
            if success:
                print(f"✅ readimage result: loaded {result['display']}")
                readimage_results.append({
                    "source": source,
                    "success": True,
                    "image_url": result["image_url"],
                    "error": None,
                })
            else:
                print(f"⚠️ readimage result: {result}")
                await channel.send(f"*⚠️ {result}*")
                readimage_results.append({
                    "source": source,
                    "success": False,
                    "image_url": None,
                    "error": result,
                })

        elif seg["type"] == "saveglobalanchor":
            memory_text = seg["content"]
            if db is None:
                print("⚠️ saveglobalanchor directive but no db provided")
                await channel.send("*⚠️ Could not save memory — database unavailable.*")
                continue
            add_anchored_memory(db, "global", memory_text)
            print(f"📌 saveglobalanchor: {memory_text[:100]}")
            await channel.send("*📌 Anchored global memory*\n\n")

        elif seg["type"] == "deleteglobalanchor":
            raw_id = (seg.get("memory_id") or "").strip()
            if db is None:
                print("⚠️ deleteglobalanchor directive but no db provided")
                await channel.send("*⚠️ Could not remove memory — database unavailable.*")
                continue
            # Accept plain integers or a leading "#"; be tolerant of stray text.
            m = re.search(r"\d+", raw_id)
            if not m:
                print(f"⚠️ deleteglobalanchor: could not parse id from {raw_id!r}")
                await channel.send(f"*⚠️ Could not parse memory id from `{raw_id}`.*")
                continue
            mid = int(m.group(0))
            if remove_anchored_memory(db, mid):
                print(f"🗑️ deleteglobalanchor: removed anchor #{mid}")
                await channel.send(f"*🗑️ Removed anchored memory #{mid}.*")
            else:
                print(f"⚠️ deleteglobalanchor: no anchor with id {mid}")
                await channel.send(f"*⚠️ No anchored memory with id #{mid}.*")

        elif seg["type"] == "react":
            if suppress_reacts:
                # Intermediate tool-chain rounds: parse but don't execute or count.
                continue
            had_react = True
            if user_message is None:
                print("⚠️ react directive but no user_message provided")
                continue
            # Throttle consecutive reacts: first is always allowed,
            # subsequent ones have a 1-in-(N*2) chance.
            if consecutive_reacts > 1:
                roll = random.randint(1, consecutive_reacts * 2)
                if roll != 1:
                    continue
            react_was_posted = True
            emojis = _extract_utf8_emojis(seg["emojis"])
            if emojis:
                count = random.randint(1, len(emojis))
                emojis = emojis[:count]
            for emoji in emojis:
                try:
                    await user_message.add_reaction(emoji)
                except Exception as e:
                    print(f"⚠️ react directive: failed to add {emoji!r} — {e}")

    return "\n\n".join(text_parts), runcmd_results, readweb_results, readimage_results, had_react, react_was_posted
