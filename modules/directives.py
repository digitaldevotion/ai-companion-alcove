# ============================================
# Alcove — directives.py
# Directive parsing and execution
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import aiohttp
import asyncio
import base64
from curl_cffi.requests import AsyncSession
from curl_cffi import CurlError
import config
from .database import add_anchored_memory, remove_anchored_memory, update_anchored_memory, is_social_mode
from .utils import safe_send_chunked, send_image_result, build_channel_key, build_dm_key, tokens_to_bytes
import os
import random
import subprocess
import re
import trafilatura
from pathlib import Path
from .companions import _COMPANION_DATAFILES_DIR


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
#   <manageAnchor action="add">
#   Pippin's birthday is October 16th
#   </manageAnchor>
#   <manageAnchor action="update" id="50">
#   Pippin's birthday is October 16th. This year she's getting a ball.
#   </manageAnchor>
#   <manageAnchor action="remove" id="50">
#   </manageAnchor>
#
#   <react>
#   🥰😅
#   </react>

_KNOWN_DIRECTIVES = {"output", "runcmd", "createimage", "readweb", "readimage", "saveglobalanchor", "deleteglobalanchor", "manageanchor", "react", "autojournal", "permjournal", "groupchat"}
_DIRECTIVE_OPEN = re.compile(
    r'^<(output|runcmd|createimage|readweb|readimage|saveglobalanchor|deleteglobalanchor|manageanchor|react|autojournal|permjournal|groupchat)'
    r'((?:\s+[A-Za-z_][\w-]*\s*=\s*"[^"]*")*)\s*>',
    re.IGNORECASE,
)
_DIRECTIVE_CLOSE = re.compile(r'^</(output|runcmd|createimage|readweb|readimage|saveglobalanchor|deleteglobalanchor|manageanchor|react|autojournal|permjournal|groupchat)\s*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')


def _parse_directive_attrs(attr_str):
    # Parse a directive's attribute string (e.g. 'path="/foo" use="reference"'
    # or 'action="add" id="50"') into a lowercased-key dict. Attribute order
    # does not matter, so manageAnchor's action/id may appear in any order.
    return {k.lower(): v for k, v in _ATTR_RE.findall(attr_str or "")}


def _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=None, directive_attrs=None):
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
    elif directive_type == "manageanchor":
        attrs = directive_attrs or {}
        action = (attrs.get("action") or "").strip().lower()
        raw_id = (attrs.get("id") or "").strip()
        segments.append({
            "type": "manageanchor",
            "action": action,
            "id": raw_id,
            "content": body.strip(),
        })
    elif directive_type == "react":
        emojis_raw = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "react", "emojis": emojis_raw.strip()})
    elif directive_type == "autojournal":
        journal_text = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "autojournal", "content": journal_text.strip()})
    elif directive_type == "permjournal":
        journal_text = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "permjournal", "content": journal_text.strip()})
    elif directive_type == "groupchat":
        segments.append({"type": "groupchat", "content": body.strip()})


def parse_directives(response_text):
    segments = []
    text_buffer = []
    body_lines = []
    current_directive = None  # (type, arg, use, attrs)

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
                attrs = _parse_directive_attrs(match.group(2))
                directive_arg = attrs.get("path", "")
                directive_use = attrs.get("use") or None
                current_directive = (directive_type, directive_arg, directive_use, attrs)
                body_lines = []
            else:
                text_buffer.append(line)
        else:
            # Inside a directive — look for closing tag
            clean = re.sub(r'[`*]', '', stripped)
            close_match = _DIRECTIVE_CLOSE.match(clean)
            if close_match and close_match.group(1).lower() == current_directive[0]:
                # Clean "close-tag on its own line" case.
                directive_type, directive_arg, directive_use, directive_attrs = current_directive
                _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=directive_use, directive_attrs=directive_attrs)
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
                    directive_type, directive_arg, directive_use, directive_attrs = current_directive
                    _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=directive_use, directive_attrs=directive_attrs)
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
        directive_type, directive_arg, _directive_use, _directive_attrs = current_directive
        raw = f"<{directive_type}>\n" + "\n".join(body_lines)
        text_buffer.append(raw)

    # Flush remaining text
    if text_buffer:
        joined = "\n".join(text_buffer).strip()
        if joined:
            segments.append({"type": "text", "content": joined})

    return segments


_TURN_HEADER_RE = re.compile(r'\*\*[^*]+\*\*\s*:')


def _parse_groupchat_turns(body):
    turns = [t.strip() for t in re.split(r'\n{2,}', body) if t.strip()]
    if not turns or not any(_TURN_HEADER_RE.search(t) for t in turns):
        return None
    return turns


def execute_output(path, content):
    # Write content to a file, creating parent directories if needed.
    # If the file already exists, add a datestamp suffix to avoid overwriting.
    # Returns (success: bool, message: str)
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
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
    # Uses curl_cffi with browser TLS impersonation to bypass bot detection.
    # token_budget: available tokens remaining before hitting the context limit.
    #   If provided, content is truncated so its estimated tokens (bytes/4) stay
    #   within that budget (minus a reserve for the model's reply).
    # Returns (success: bool, message: str)
    RESERVE_TOKENS = 20000
    try:
        async with AsyncSession(impersonate="chrome") as session:
            response = await session.get(url, timeout=15)
            if response.status_code != 200:
                return False, f"HTTP {response.status_code} fetching {url}"
            html = response.text
        content = trafilatura.extract(html)
        if not content or not content.strip():
            content = html
        # Truncate based on available context budget (byte-aware)
        if token_budget is not None:
            max_bytes = tokens_to_bytes(token_budget - RESERVE_TOKENS)
            if max_bytes < 400:
                return False, "Not enough context budget remaining to read this page."
            if len(content.encode("utf-8")) > max_bytes:
                # Binary-search for the longest prefix that fits within max_bytes
                # while keeping the string valid UTF-8 (slice on char boundary).
                lo, hi = 0, len(content)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if len(content[:mid].encode("utf-8")) <= max_bytes:
                        lo = mid
                    else:
                        hi = mid - 1
                content = content[:lo] + (
                    "\n\n... (Content truncated — total size was approaching the context limit. "
                    "Please let the user know that the full article could not be loaded.)"
                )
        return True, content
    except CurlError as e:
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

    # Detect LLM mistakes: source is not a URL and doesn't resemble a file
    # path (no path separators, no file extension).
    if source.lower() in ("reference image", "reference"):
        return False, f"readimage: '{source}' is not a file path or URL. If you meant to use an attached reference image, use <createimage use=\"reference\"> instead."
    if "/" not in source and "\\" not in source and "." not in source:
        return False, f"readimage: '{source}' is not a valid file path or URL."

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


async def execute_autojournal(channel, user_message, journal_content, active_companion=None, journal_mode="short"):
    import os
    import re
    import tempfile
    import shutil
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    import config
    from .database import get_channel_companion

    try:
        # 1. Resolve active companion
        if active_companion is None:
            if channel is not None:
                if channel.guild is None:
                    if user_message is not None:
                        channel_name = build_dm_key(user_message.author.name)
                    else:
                        recipient = getattr(channel, "recipient", None)
                        if recipient:
                            channel_name = build_dm_key(recipient.name)
                        else:
                            channel_name = "dm"
                else:
                    channel_name = build_channel_key(channel.guild.name, str(channel))
                active_companion = get_channel_companion(channel_name)
            else:
                active_companion = "default"

        # 2. Resolve directory paths
        comp_dir = _COMPANION_DATAFILES_DIR / active_companion
        if not comp_dir.is_dir():
            comp_dir = _COMPANION_DATAFILES_DIR / "default"
        comp_dir.mkdir(parents=True, exist_ok=True)

        context_history_dir = comp_dir / "3_context_history"
        context_history_dir.mkdir(parents=True, exist_ok=True)
        if journal_mode == "perm":
            short_term_path = context_history_dir / "kb_auto_perm_journal.md"
        else:
            short_term_path = context_history_dir / "kb_auto_short_term_journal.md"

        # Permanent journal entries are never archived — no long-term path.
        long_term_path = None
        if journal_mode == "short":
            search_reference_dir = comp_dir / "5_search_reference"
            search_reference_dir.mkdir(parents=True, exist_ok=True)
            long_term_path = search_reference_dir / "kb_auto_long_term_journal.md"

        # 3. Format current date & time (respecting configured offset)
        now = datetime.now(timezone.utc) + timedelta(hours=getattr(config, "TIMEZONE_OFFSET", 0))
        now_str = now.strftime("%Y-%m-%d-%H-%M-%S")

        # 4. Check if LLM already added a '###' timestamp line, and strip it to avoid duplication
        journal_text = journal_content.strip()
        if journal_text.startswith("###"):
            lines = journal_text.splitlines()
            if lines and lines[0].strip().startswith("###"):
                journal_text = "\n".join(lines[1:]).strip()

        # Format new entry
        new_entry = f"### {now_str}\n{journal_text}\n\n"

        # Header template for a new journal file
        if journal_mode == "perm":
            header_template = (
                f"# File: kb_auto_perm_journal.md\n"
                f"  - This contains permanent journal entries that must always remain in context forever. "
                f"These are never archived. Use this file for historic reference of moments you want to remember permanently.\n\n"
                f"  - Most Recent Memory Pointer: {now_str}\n\n"
            )
        else:
            header_template = (
                f"# File: kb_auto_short_term_journal.md\n"
                f"  - This contains journals and summaries of session history between yourself and your special human. "
                f"Please use this summary to determine writing style, tone, and mannerisms. Also use this file for historic reference.\n\n"
                f"  - Most Recent Memory Pointer: {now_str}\n\n"
            )

        short_term_backup = None
        long_term_backup = None
        short_term_existed = os.path.exists(short_term_path)
        long_term_existed = os.path.exists(long_term_path) if long_term_path else False
        short_term_tmp = str(short_term_path) + ".tmp"
        long_term_tmp = str(long_term_path) + ".tmp" if long_term_path else None
        temp_dir = tempfile.gettempdir()

        def _safe_remove(path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        try:
            # SAFETY BACKUPS: Save temporary copies in local OS temp directory
            # Backup failures are soft — we warn but continue, since original files are intact
            if short_term_existed:
                short_term_backup = os.path.join(temp_dir, f"kb_auto_short_term_journal_{now_str}.md.bak")
                try:
                    shutil.copy2(short_term_path, short_term_backup)
                except Exception as bk_err:
                    print(f"⚠️ updateJournal: failed to back up short-term journal ({bk_err}); proceeding without backup")
                    short_term_backup = None
            if long_term_existed:
                long_term_backup = os.path.join(temp_dir, f"kb_auto_long_term_journal_{now_str}.md.bak")
                try:
                    shutil.copy2(long_term_path, long_term_backup)
                except Exception as bk_err:
                    print(f"⚠️ updateJournal: failed to back up long-term journal ({bk_err}); proceeding without backup")
                    long_term_backup = None

            # 5. Load/initialize short term journal
            if not short_term_existed:
                file_content = header_template + new_entry
            else:
                with open(short_term_path, "r", encoding="utf-8") as f:
                    file_content = f.read()

                # Update header's memory pointer with current date/time
                file_content = re.sub(
                    r'(\s*-\s*Most Recent Memory Pointer:\s*)\S*',
                    rf'\g<1>{now_str}',
                    file_content
                )

                # Append new entry at the bottom
                if not file_content.endswith("\n"):
                    file_content += "\n"
                file_content += new_entry

            # 6. Retrieve MAX limit from config
            LIVE_JOURNAL_MAX_CONTEXT_ENTRIES = getattr(config, "LIVE_JOURNAL_MAX_CONTEXT_ENTRIES", 45)

            # 7. Split file content into Header + Entries
            lines = file_content.splitlines()
            header_lines = []
            entries_raw = []
            current_entry = None

            for line in lines:
                if line.strip().startswith("###"):
                    if current_entry is not None:
                        entries_raw.append(current_entry)
                    current_entry = [line]
                else:
                    if current_entry is None:
                        header_lines.append(line)
                    else:
                        current_entry.append(line)

            if current_entry is not None:
                entries_raw.append(current_entry)

            # Clean and structure parsed entries
            entries = []
            for ent_lines in entries_raw:
                hdr = ent_lines[0].strip()
                body = "\n".join(ent_lines[1:]).strip()
                entries.append((hdr, body))

            # 8. Build all content in memory before touching any files on disk
            archived_count = 0
            long_term_new_content = None
            if journal_mode == "short" and len(entries) > LIVE_JOURNAL_MAX_CONTEXT_ENTRIES:
                num_to_archive = len(entries) - LIVE_JOURNAL_MAX_CONTEXT_ENTRIES
                entries_to_archive = entries[:num_to_archive]
                entries_to_keep = entries[num_to_archive:]

                # Generate long-term archived content
                archived_text = ""
                for hdr, body in entries_to_archive:
                    archived_text += f"{hdr}\n{body}\n\n"

                # Generate new short-term file content with kept entries
                kept_header = "\n".join(header_lines).strip()
                kept_entries_text = ""
                for hdr, body in entries_to_keep:
                    kept_entries_text += f"{hdr}\n{body}\n\n"

                file_content = kept_header + "\n\n" + kept_entries_text
                archived_count = num_to_archive

                # Prepare full long-term content (existing + new archive entries)
                if long_term_existed:
                    with open(long_term_path, "r", encoding="utf-8") as f:
                        existing_long = f.read()
                    long_term_new_content = existing_long + archived_text
                else:
                    long_term_new_content = archived_text

            # 9. Write files atomically: write to .tmp then os.replace() into place
            #    Long-term first (so if short-term fails, we can rollback both;
            #    doing short-term first risks data loss if long-term then fails)

            # 9a. Write long-term journal (atomic)
            if long_term_new_content is not None:
                with open(long_term_tmp, "w", encoding="utf-8") as f:
                    f.write(long_term_new_content)
                os.replace(long_term_tmp, long_term_path)
                long_term_tmp = None

            # 9b. Write short-term journal (atomic)
            with open(short_term_tmp, "w", encoding="utf-8") as f:
                f.write(file_content)
            os.replace(short_term_tmp, short_term_path)
            short_term_tmp = None

            # Clean up backups on success
            _safe_remove(short_term_backup)
            _safe_remove(long_term_backup)

            if journal_mode == "perm":
                result_msg = "Permanent journal entry added — it will never be archived."
            elif archived_count > 0:
                result_msg = f"Journal entry added. Archived {archived_count} oldest entries to long-term journal."
            else:
                result_msg = "Journal entry added successfully."

            if active_companion == "default":
                user_ctx = getattr(config, "CONTEXT_HISTORY_LOCATIONS", None)
                user_search = getattr(config, "SEARCH_REFERENCE_LOCATIONS", None)
                if user_ctx or user_search:
                    await safe_send_chunked(channel,
                        "⚠️ **Journailing notice:** Your journal was saved to disk, but it will **not** be available in context "
                        "because you are using manually-specified file paths in `config.py`. "
                        "To include auto-generated journals, switch to auto-detection by clearing "
                        "`CONTEXT_HISTORY_LOCATIONS` and `SEARCH_REFERENCE_LOCATIONS` in `config.py`. "
                        "See the **\"Connecting Your Files\"** section in the **Alcove Engine Setup Guide** for details."
                    )

            return True, result_msg

        except Exception as io_err:
            # ROLLBACK: Restore original files as best we can
            restore_issues = []

            # Clean up any .tmp files left over from partial atomic writes
            _safe_remove(short_term_tmp)
            _safe_remove(long_term_tmp)

            # Restore short-term
            if short_term_backup:
                try:
                    shutil.copy2(short_term_backup, short_term_path)
                except Exception as rst_err:
                    restore_issues.append(f"short-term restore failed ({rst_err})")
            elif not short_term_existed:
                try:
                    if os.path.exists(short_term_path):
                        os.remove(short_term_path)
                except Exception as rst_err:
                    restore_issues.append(f"short-term cleanup failed ({rst_err})")

            # Restore long-term
            if long_term_backup:
                try:
                    shutil.copy2(long_term_backup, long_term_path)
                except Exception as rst_err:
                    restore_issues.append(f"long-term restore failed ({rst_err})")
            elif not long_term_existed and long_term_path:
                try:
                    if os.path.exists(long_term_path):
                        os.remove(long_term_path)
                except Exception as rst_err:
                    restore_issues.append(f"long-term cleanup failed ({rst_err})")

            # Clean up backup files from temp dir
            _safe_remove(short_term_backup)
            _safe_remove(long_term_backup)

            # Build descriptive error for user
            if restore_issues:
                detail = f"Journal write failed AND restoration had issues: {'; '.join(restore_issues)}. Original error: {io_err}"
            else:
                detail = f"Journal write failed — original files restored from backup. Error: {io_err}"

            raise type(io_err)(detail).with_traceback(io_err.__traceback__)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, f"Failed to save journal entry: {e}"


async def process_response(response_text, channel, image_handler=None, token_budget=None, db=None, user_message=None, consecutive_reacts=0, suppress_reacts=False, active_companion=None, send_func=None, channel_name=None):
    # Parse directives from the response, execute them, and return:
    #   (display_text, runcmd_results, readweb_results, readimage_results, had_react, react_was_posted)
    # display_text: remaining text to send to Discord
    # runcmd_results: list of {"command": ..., "success": bool, "output": ...} dicts
    # send_func: optional async callable(channel, content, **kwargs) to use instead of
    #   channel.send() — e.g. safe_send with a timeout guard when the gateway
    #   may be disconnected.
    # readweb_results: list of {"url": ..., "success": bool, "output": ...} dicts
    # readimage_results: list of {"source": ..., "success": bool, "image_url": ..., "error": ...} dicts
    #   — image_url is a real URL or a data: URI and is None on failure; error is None on success.
    # had_react: True if the response contained a <react> directive (even if skipped)
    # Directive results are also reported back to the channel as status messages.
    # image_handler is an async function(prompt) that returns an image result dict.
    # suppress_reacts: if True, <react> directives are parsed but silently skipped
    #   (used for tool-chain follow-up rounds so reacts only fire on the first response).
    segments = parse_directives(response_text)
    text_parts = []
    runcmd_results = []
    readweb_results = []
    readimage_results = []
    had_react = False
    react_was_posted = False

    async def _send(content=None, **kwargs):
        if send_func is not None:
            return await send_func(channel, content, **kwargs)
        return await channel.send(content, **kwargs)

    for seg in segments:
        if seg["type"] == "text":
            text_parts.append(seg["content"])

        elif seg["type"] == "output":
            success, msg = execute_output(seg["path"], seg["content"])
            icon = "📄" if success else "⚠️"
            print(f"{icon} output directive: {msg}")
            await _send(f"*{icon} {msg}*")

        elif seg["type"] == "runcmd":
            if channel_name and is_social_mode(channel_name):
                print(f"🔒 runcmd directive blocked: !socialMode is active in `{channel_name}`")
                await _send(
                    "*🔒 Sorry — the `runcmd` directive is currently sandboxed while "
                    "`!socialMode` is active in this channel.*"
                )
                continue
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
            await _send(f"*🔧 Running:*\n```\n{cmd_block}\n```")

            success, msg = execute_runcmd(seg["command"])
            icon = "✅" if success else "⚠️"
            print(f"{icon} runcmd result: {msg[:200]}")
            # Output is still gated by DISPLAY_CMD_OUTPUT — only the command
            # echo above is unconditional.
            if config.DISPLAY_CMD_OUTPUT:
                display = msg if len(msg) <= 1500 else msg[:1500] + "... (truncated)"
                await _send(f"*{icon} `{seg['command'][:100]}`*\n```\n{display}\n```")
            runcmd_results.append({
                "command": seg["command"],
                "success": success,
                "output": msg,
            })

        elif seg["type"] == "createimage":
            if image_handler is None:
                print("⚠️ createimage directive but no image_handler provided")
                await _send("*⚠️ Image generation not available*")
                continue
            prompt = seg["prompt"]
            use_refs = seg.get("use") == "reference"
            ref_label = " with references" if use_refs else ""
            print(f"🎨 createimage directive{ref_label}: {prompt[:100]}")
            await _send(f"*🎨 Generating image{ref_label}...*")
            result = await image_handler(prompt, reference_images=None if use_refs else [])
            await send_image_result(channel, result, send_func=_send)

        elif seg["type"] == "readweb":
            url = seg["url"]
            print(f"🌐 readweb directive: {url}")
            await _send(f"*🌐 Reading {url}...*")
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
                if 'use <createimage use="reference">' not in result:
                    await _send(f"*⚠️ {result}*")
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
                await _send("*⚠️ Could not save memory — database unavailable.*")
                continue
            add_anchored_memory(db, "global", memory_text)
            print(f"📌 saveglobalanchor: {memory_text[:100]}")
            await _send("*📌 Anchored global memory*\n\n")

        elif seg["type"] == "deleteglobalanchor":
            raw_id = (seg.get("memory_id") or "").strip()
            if db is None:
                print("⚠️ deleteglobalanchor directive but no db provided")
                await _send("*⚠️ Could not remove memory — database unavailable.*")
                continue
            # Accept plain integers or a leading "#"; be tolerant of stray text.
            m = re.search(r"\d+", raw_id)
            if not m:
                print(f"⚠️ deleteglobalanchor: could not parse id from {raw_id!r}")
                await _send(f"*⚠️ Could not parse memory id from `{raw_id}`.*")
                continue
            mid = int(m.group(0))
            if remove_anchored_memory(db, mid):
                print(f"🗑️ deleteglobalanchor: removed anchor #{mid}")
                await _send(f"*🗑️ Removed anchored memory #{mid}.*")
            else:
                print(f"⚠️ deleteglobalanchor: no anchor with id {mid}")
                await _send(f"*⚠️ No anchored memory with id #{mid}.*")

        elif seg["type"] == "manageanchor":
            action = (seg.get("action") or "").strip().lower()
            raw_id = (seg.get("id") or "").strip()
            content = (seg.get("content") or "").strip()
            if db is None:
                print("⚠️ manageanchor directive but no db provided")
                await _send("*⚠️ Could not modify memory — database unavailable.*")
                continue
            if action not in ("add", "update", "remove"):
                print(f"⚠️ manageanchor: unknown action {action!r}")
                await _send(f"*⚠️ Unknown manageAnchor action `{action}` (expected add, update, or remove).*")
                continue
            if action == "add":
                if not content:
                    print("⚠️ manageanchor add: empty content")
                    await _send("*⚠️ manageAnchor add requires memory content.*")
                    continue
                add_anchored_memory(db, "global", content)
                print(f"📌 manageanchor add: {content[:100]}")
                await _send("*📌 Anchored global memory*\n\n")
            elif action == "update":
                m = re.search(r"\d+", raw_id)
                if not m:
                    print(f"⚠️ manageanchor update: could not parse id from {raw_id!r}")
                    await _send(f"*⚠️ Could not parse memory id from `{raw_id}`.*")
                    continue
                mid = int(m.group(0))
                if not content:
                    print("⚠️ manageanchor update: empty content")
                    await _send("*⚠️ manageAnchor update requires new memory content.*")
                    continue
                if update_anchored_memory(db, mid, content):
                    print(f"📝 manageanchor update: updated anchor #{mid}")
                    await _send(f"*📝 Updated anchored memory #{mid}.*")
                else:
                    add_anchored_memory(db, "global", content)
                    print(f"📌 manageanchor update: no anchor #{mid}, inserted as new global memory")
                    await _send(f"*📝 No anchor #{mid} found — inserted as new anchored memory.*\n\n")
            elif action == "remove":
                if not getattr(config, "ANCHORS_ALLOW_AUTO_REMOVE", True):
                    print(f"🔒 manageanchor remove blocked by ANCHORS_ALLOW_AUTO_REMOVE=False (id={raw_id})")
                    await _send("*🔒 manageAnchor `remove` action is currently disabled on this instance.*")
                    continue
                m = re.search(r"\d+", raw_id)
                if not m:
                    print(f"⚠️ manageanchor remove: could not parse id from {raw_id!r}")
                    await _send(f"*⚠️ Could not parse memory id from `{raw_id}`.*")
                    continue
                mid = int(m.group(0))
                if remove_anchored_memory(db, mid):
                    print(f"🗑️ manageanchor remove: removed anchor #{mid}")
                    await _send(f"*🗑️ Removed anchored memory #{mid}.*")
                else:
                    print(f"⚠️ manageanchor remove: no anchor with id {mid}")
                    await _send(f"*⚠️ No anchored memory with id #{mid}.*")

        elif seg["type"] in ("autojournal", "permjournal"):
            journal_content = seg["content"]
            is_perm = seg["type"] == "permjournal"
            journal_mode = "perm" if is_perm else "short"
            label = "permJournal" if is_perm else "autoJournal"
            log_icon = "📓"
            print(f"{log_icon} directive received")
            success, msg = await execute_autojournal(
                channel, user_message, journal_content,
                active_companion=active_companion, journal_mode=journal_mode,
            )
            icon = log_icon if success else "⚠️"
            print(f"{icon} result: {msg}")
            await _send(f"*{icon} {msg}*")
            if success:
                # Strip the ### header if the LLM wrote one inside the tags
                clean_journal = journal_content.strip()
                if clean_journal.startswith("###"):
                    lines = clean_journal.splitlines()
                    if lines and lines[0].strip().startswith("###"):
                        clean_journal = "\n".join(lines[1:]).strip()
                # Post the journal content back to the current channel with a
                # clear label so the user can see which journal type was written.
                perm_tag = " (permanent — never archived)" if is_perm else ""
                await safe_send_chunked(channel, f"**{label} entry{perm_tag}:**\n{clean_journal}")

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

        elif seg["type"] == "groupchat":
            body = seg["content"]
            turns = _parse_groupchat_turns(body)
            if turns is None:
                print("⚠️ groupchat: could not parse turns, sending as plain text")
                text_parts.append(body)
            else:
                try:
                    from . import main as _main_mod
                    _delay_min = _main_mod.GROUPCHAT_TURN_DELAY_MIN
                    _delay_max = _main_mod.GROUPCHAT_TURN_DELAY_MAX
                except (ImportError, AttributeError):
                    _delay_min, _delay_max = 1, 4
                print(f"💬 groupchat: sending {len(turns)} turns")
                for i, turn in enumerate(turns):
                    if i > 0:
                        await safe_send_chunked(channel, "\u200B")
                    await asyncio.sleep(random.uniform(_delay_min, _delay_max))
                    async with channel.typing():
                        await safe_send_chunked(channel, turn)

    return "\n\n".join(text_parts), runcmd_results, readweb_results, readimage_results, had_react, react_was_posted
