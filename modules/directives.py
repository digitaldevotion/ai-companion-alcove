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
import discord
import io
from .database import add_anchored_memory, remove_anchored_memory, update_anchored_memory, get_anchored_memory, is_social_mode, add_crontab_entry, update_crontab_entry, delete_crontab_entry, get_crontab_entry, list_crontab_entries
from .utils import safe_send_chunked, send_image_result, build_channel_key, build_dm_key, tokens_to_bytes, estimate_tokens, HTTP_PIN
from .video_delivery import deliver_video
import json
import os
import random
import subprocess
import re
import trafilatura
from html import unescape as html_unescape
from urllib.parse import unquote, parse_qs, urlsplit, quote_plus
from pathlib import Path
from .companions import _COMPANION_DATAFILES_DIR


# Parse XML-style directive blocks from LLM response text.
# Returns a list of segments in order, each being either:
#   {"type": "text", "content": "..."}                    — normal text to send to Discord
#   {"type": "output", "path": "...", "content": "..."}   — write content to file
#   {"type": "runcmd", "command": "..."}                  — run a shell command
#   {"type": "createimage", "prompt": "..."}              — generate an image
#   {"type": "createvideo", "prompt": "..."}              — generate a video
#   {"type": "readweb", "url": "..."}                    — fetch readable content from a URL
#   {"type": "readimage", "source": "..."}                — load an image (URL or local path) for the model to see
#   {"type": "websearch", "query": "..."}                 — search the web for real-time information
#   {"type": "attachfiletochannel", "source": "..."}            — read a file/URL, post it to Discord, and attach it to the model's context
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
#   <runcmd timeout="300">
#   echo "long-running"
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
#   <websearch query="latest AI news">
#   </websearch>
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
#   <task action="create" event_type="recurring" when="*/5 * * * *" status="active">
#   Prompt text to send when the task fires.
#   </task>
#   <task action="edit" id="7" when="0 9 * * 1">
#   Updated prompt (or empty body to keep existing prompt).
#   </task>
#   <task action="remove" id="7">
#   </task>
#   <task action="list">
#   </task>
#   <task action="list" status="disabled">
#   </task>
#
#   <react>
#   🥰😅
#   </react>

_KNOWN_DIRECTIVES = {"output", "runcmd", "createimage", "createvideo", "readweb", "readimage", "readskill", "saveglobalanchor", "deleteglobalanchor", "manageanchor", "react", "autojournal", "permjournal", "groupchat", "task", "websearch", "attachfiletochannel"}
_DIRECTIVE_OPEN = re.compile(
    r'<(output|runcmd|createimage|createvideo|readweb|readimage|readskill|saveglobalanchor|deleteglobalanchor|manageanchor|react|autojournal|permjournal|groupchat|task|websearch|attachfiletochannel)'
    r'((?:\s+[A-Za-z][\w-]*\s*=\s*"[^"]*")*)\s*>',
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')


def _parse_directive_attrs(attr_str):
    # Parse a directive's attribute string (e.g. 'path="/foo" use="reference"'
    # or 'action="add" id="50"') into a lowercased-key dict. Attribute order
    # does not matter, so manageAnchor's action/id may appear in any order.
    # Values are HTML-unescaped so embedded quotation marks may be expressed
    # as &quot; (or &#34; / &#39; etc.) without breaking the attribute regex,
    # which cannot tell a value-internal " from the closing delimiter.
    return {
        k.lower(): html_unescape(v)
        for k, v in _ATTR_RE.findall(attr_str or "")
    }


def _find_closer(text, directive_type):
    pat = re.compile(
        rf'</\s*{re.escape(directive_type)}\s*>',
        re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return None
    prefix = text[:m.start()]
    suffix = text[m.end():]
    prefix = re.sub(r'[`*]+$', '', prefix)
    suffix = re.sub(r'^[`*]+', '', suffix).rstrip()
    return prefix, suffix


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
        timeout_raw = (directive_attrs or {}).get("timeout")
        segments.append({"type": "runcmd", "command": command.strip(), "timeout": timeout_raw})
    elif directive_type == "createimage":
        prompt = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "createimage", "prompt": prompt.strip(), "use": directive_use})
    elif directive_type == "createvideo":
        prompt = directive_arg + "\n" + body if directive_arg else body
        attrs = directive_attrs or {}
        seconds = attrs.get("seconds")
        segments.append({"type": "createvideo", "prompt": prompt.strip(), "use": directive_use, "seconds": seconds})
    elif directive_type == "readweb":
        url = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "readweb", "url": url.strip()})
    elif directive_type == "websearch":
        attrs = directive_attrs or {}
        query = (attrs.get("query") or directive_arg or body).strip()
        segments.append({"type": "websearch", "query": query})
    elif directive_type == "readimage":
        source = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "readimage", "source": source.strip()})
    elif directive_type == "attachfiletochannel":
        source = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "attachfiletochannel", "source": source.strip()})
    elif directive_type == "readskill":
        path = directive_arg + "\n" + body if directive_arg else body
        segments.append({"type": "readskill", "path": path.strip()})
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
    elif directive_type == "task":
        attrs = directive_attrs or {}
        segments.append({
            "type": "task",
            "action": (attrs.get("action") or "").strip().lower(),
            "event_type": (attrs.get("event_type") or "").strip().lower(),
            "when": (attrs.get("when") or "").strip(),
            "status": (attrs.get("status") or "").strip().lower(),
            "id": (attrs.get("id") or "").strip(),
            "content": body.strip(),
        })


def parse_directives(response_text):
    segments = []
    text_buffer = []
    body_lines = []
    current_directive = None  # (type, arg, use, attrs)

    for line in response_text.split("\n"):
        if current_directive is None:
            # Not inside a directive — look for an opening tag anywhere
            # in the line. The directive tag names (runcmd, output, etc.)
            # are custom enough that they won't false-positive in prose.
            match = _DIRECTIVE_OPEN.search(line)
            if match:
                # Text before the opener on this line → text segment
                prefix = line[:match.start()]
                prefix_trimmed = re.sub(r'[`*]+$', '', prefix)
                if prefix_trimmed.strip():
                    text_buffer.append(prefix_trimmed)
                # Flush accumulated text
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
                # Handle text after the opener on the same line (supports
                # single-line directives like <output>x</output>).
                suffix = line[match.end():]
                suffix_trimmed = re.sub(r'^[`*]+', '', suffix)
                if suffix_trimmed.strip():
                    closer = _find_closer(suffix_trimmed, directive_type)
                    if closer:
                        body_prefix, text_suffix = closer
                        if body_prefix.strip():
                            body_lines.append(body_prefix)
                        _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=directive_use, directive_attrs=attrs)
                        current_directive = None
                        body_lines = []
                        if text_suffix.strip():
                            text_buffer.append(text_suffix)
                    else:
                        body_lines.append(suffix_trimmed)
            else:
                text_buffer.append(line)
        else:
            # Inside a directive — look for closing tag anywhere in the line.
            closer = _find_closer(line, current_directive[0])
            if closer:
                body_prefix, text_suffix = closer
                if body_prefix.strip():
                    body_lines.append(body_prefix)
                directive_type, directive_arg, directive_use, directive_attrs = current_directive
                _emit_directive_segment(segments, directive_type, directive_arg, body_lines, directive_use=directive_use, directive_attrs=directive_attrs)
                current_directive = None
                body_lines = []
                if text_suffix.strip():
                    text_buffer.append(text_suffix)
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


def execute_runcmd(command, timeout=180):
    # Run a shell command and return its output.
    # Returns (success: bool, message: str)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip()
            return False, f"Command exited {result.returncode}: {error or output}"
        return True, output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return False, f"Command timed out ({timeout}s limit)"
    except Exception as e:
        return False, f"Failed to run command: {e}"


_SKILLS_ROOT = Path(__file__).parent.parent / "skills"


def execute_readskill(path, token_budget=None, channel_key=None):
    # Load a skill file's full contents from disk into the conversation context.
    # Security: path must resolve inside the project-root skills/ directory and
    # must not be a symlink (defense-in-depth against following symlinks outside).
    # No content cap — but if token_budget is provided and the file's estimated
    # token count exceeds it, abort with an error so the model is informed the
    # skill did not load rather than silently truncating.
    # Returns (success: bool, message: str)
    try:
        raw_path = Path(path).expanduser()
    except Exception as e:
        return False, f"Invalid path: {e}"
    # Reject symlinks on the raw path BEFORE resolving — Path.resolve() follows
    # symlinks, so checking the resolved target's is_symlink() would always be
    # False. This catches both symlinks pointing outside skills/ (redundant with
    # the containment check below) and symlinks pointing inside skills/ (which
    # the containment check would NOT catch).
    try:
        if raw_path.is_symlink():
            return False, f"Symlinks are not allowed for skill files: {path}"
    except OSError as e:
        return False, f"Could not inspect path: {e}"
    try:
        target = raw_path.resolve()
        skills_root_resolved = _SKILLS_ROOT.resolve()
    except Exception as e:
        return False, f"Could not resolve path: {e}"
    if target == skills_root_resolved or not str(target).startswith(str(skills_root_resolved) + os.sep):
        return False, f"Path must be inside the skills/ directory: {path}"
    if not target.is_file():
        return False, f"Skill file not found: {path}"
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return False, f"Could not read skill file: {e}"
    if token_budget is not None:
        skill_tokens = estimate_tokens(content, channel_key=channel_key)
        if skill_tokens > token_budget:
            return False, (
                f"Skill file too large for available context "
                f"({skill_tokens:,} tokens, only {token_budget:,} available). "
                f"Aborted — skill not loaded."
            )
    return True, content


_READWEB_PROFILES = {
    "chrome": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site",
        "sec-fetch-user": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
    },
    "safari": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site",
        "sec-fetch-user": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
    },
    "firefox": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site",
        "sec-fetch-user": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
    },
}

_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "__cf_chl_jschl_tk__",
    "cf-mitigated",
    "just a moment",
    "datadome",
    "px-captcha",
    "perimeterx",
    "_incapsula_resource",
    "pardon our interruption",
    "window._cf_chl_opt",
    "cf-please-wait",
    "dd-guard",
    "pxdiffcrawler",
    "chl-api-widget",
    # DuckDuckGo bot-challenge markers (served by lite.duckduckgo.com when
    # it suspects automation — an "anomaly-modal" image CAPTCHA page).
    "anomaly-modal",
    "unfortunately, bots use duckduckgo",
    "anomaly.js",
)


def _looks_like_challenge(html):
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


async def execute_readweb(url, token_budget=None):
    # Fetch a web page and extract readable content using trafilatura.
    # Uses curl_cffi with browser TLS impersonation to bypass bot detection.
    # Tries three fingerprints (chrome, safari, firefox) in sequence with
    # matched header sets, retrying on 403/429 or detected JS challenge pages.
    # token_budget: available tokens remaining before hitting the context limit.
    #   If provided, content is truncated so its estimated tokens (bytes/4) stay
    #   within that budget (minus a reserve for the model's reply).
    # Returns (success: bool, message: str)
    RESERVE_TOKENS = 20000
    try:
        html = None
        last_status = None
        last_error = None
        saw_challenge = False

        for profile in ("chrome", "safari", "firefox"):
            try:
                async with AsyncSession(impersonate=profile) as session:
                    response = await session.get(
                        url, headers=_READWEB_PROFILES[profile], timeout=15
                    )
                    body = response.text if response.status_code == 200 else None
                    last_status = response.status_code
                if last_status == 200:
                    if _looks_like_challenge(body):
                        saw_challenge = True
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                        continue
                    html = body
                    break
                elif last_status in (403, 429):
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    continue
                else:
                    return False, f"HTTP {last_status} fetching {url}"
            except CurlError as e:
                last_error = str(e)
                last_status = None
                await asyncio.sleep(random.uniform(0.5, 1.2))
                continue

        if html is None:
            if saw_challenge:
                return False, (
                    f"Site is protected by a JS bot-detection challenge "
                    f"(Cloudflare/Datadome/etc.) — cannot fetch with HTTP-only: {url}"
                )
            if last_status is not None:
                return False, (
                    f"HTTP {last_status} fetching {url} "
                    f"(tried chrome/safari/firefox fingerprints)"
                )
            return False, f"Failed to fetch {url}: {last_error or 'unknown error'}"

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
    except Exception as e:
        return False, f"Error reading {url}: {e}"


_WEBSEARCH_RESERVE_TOKENS = 20000
_WEBSEARCH_TRUNC_TAIL = (
    "\n\n... (Search results truncated — total size was approaching "
    "the context limit.)"
)
_WEBSEARCH_TOO_SMALL = "Not enough context budget remaining to run web search."

# Placeholder value the config template ships with for OPENROUTER_KEY.
# Treating it as "no key configured" lets the websearch fallback kick in
# for nanoGPT-only installs that never replaced the template default.
_OPENROUTER_KEY_PLACEHOLDER = "REPLACE_WITH_OPENROUTER_KEY"

# DuckDuckGo Lite redirect URLs look like
#   //duckduckgo.com/l/?uddg=<urlencoded real url>&rut=...
# We pull the uddg param out and url-decode it to recover the real target.
_DDGO_UDDG_RE = re.compile(r'[?&]uddg=([^&]+)', re.IGNORECASE)

# DDG Lite SERP rows. Links are <a ... class='result-link' ...>TITLE</a>;
# the href and class attributes may appear in either order, so we match
# the anchor tag first and extract the href separately. Snippets live in
# a separate <td class='result-snippet'>SNIPPET</td> row that follows
# the link row in the same result table.
_DDGO_LINK_RE = re.compile(
    r"<a([^>]*\bclass=['\"]result-link['\"][^>]*)>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_DDGO_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_DDGO_SNIPPET_RE = re.compile(
    r"<td[^>]*class='result-snippet'[^>]*>(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)


def _decode_ddg_url(href):
    # Decode a DuckDuckGo Lite redirect href into the real target URL.
    # Returns the decoded URL string, or None if it can't be parsed.
    href = html_unescape(href or "")
    m = _DDGO_UDDG_RE.search(href)
    if not m:
        return None
    return unquote(m.group(1))


def _strip_html(s):
    # Strip HTML tags and decode the common entities DDG emits in SERP text.
    s = re.sub(r'<[^>]+>', '', s or "")
    s = html_unescape(s)
    return s.strip()


def _truncate_to_budget(text, token_budget, reserve_tokens,
                         too_small_msg, trunc_tail):
    # Byte-aware truncation shared by the OpenRouter and DDG fallback
    # websearch paths. Keeps the string valid UTF-8 by slicing on a
    # character boundary found via binary search.
    # Returns (ok, result) where:
    #   ok=True, result=text            — fits (possibly truncated)
    #   ok=False, result=too_small_msg  — not enough budget
    if token_budget is None:
        return True, text
    max_bytes = tokens_to_bytes(token_budget - reserve_tokens)
    if max_bytes < 400:
        return False, too_small_msg
    if len(text.encode("utf-8")) <= max_bytes:
        return True, text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return True, text[:lo] + trunc_tail


async def _websearch_fallback_ddg(query, max_results, token_budget):
    # Keyless web-search fallback used when OpenRouter is unavailable
    # (no API key, call failure, or empty result). Scrapes DuckDuckGo
    # Lite with curl_cffi browser impersonation, parses titles/URLs/
    # snippets, and formats them identically to the OpenRouter path so
    # downstream code (llm_loop.py, idle.py) sees no shape difference.
    # Returns (success: bool, message: str).
    if not query or not query.strip():
        return False, "No search query provided."
    # Light jitter to space out requests (mirrors execute_readweb's pattern).
    await asyncio.sleep(random.uniform(0.5, 1.2))
    serp_url = f"https://lite.duckduckgo.com/lite?q={quote_plus(query)}"
    try:
        async with AsyncSession(impersonate="chrome") as session:
            response = await session.get(
                serp_url,
                headers=_READWEB_PROFILES["chrome"],
                timeout=15,
            )
            if response.status_code != 200:
                return False, (
                    f"Web search fallback failed (HTTP {response.status_code})."
                )
            body = response.text
    except CurlError as e:
        return False, f"Web search fallback network error: {e}"
    except Exception as e:
        return False, f"Web search fallback error: {e}"

    if not body or _looks_like_challenge(body):
        return False, (
            "Web search fallback blocked by DuckDuckGo (bot challenge). "
            "Try again later."
        )

    # Parse SERP. DDG Lite returns results in <a ... class='result-link' ...>
    # anchors; the href and class attributes may appear in either order, so
    # we match the anchor first and extract the href from its attributes.
    # Snippets live in following <td class='result-snippet'>...</td> rows.
    raw_links = []
    for m in _DDGO_LINK_RE.finditer(body):
        attrs = m.group(1)
        content = m.group(2)
        href_m = _DDGO_HREF_RE.search(attrs)
        if href_m:
            raw_links.append((href_m.group(1), content))
    snippets = _DDGO_SNIPPET_RE.findall(body)

    if not raw_links:
        return False, "Web search fallback returned no results."

    results = []
    for i, (raw_href, raw_title) in enumerate(raw_links):
        if len(results) >= max_results:
            break
        url = _decode_ddg_url(raw_href)
        if not url or not url.startswith(("http://", "https://")):
            continue
        title = _strip_html(raw_title)
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        results.append({"url": url, "title": title, "content": snippet})

    if not results:
        return False, "Web search fallback returned no parseable results."

    blocks = []
    for r in results:
        header = r["title"] if r["title"] else r["url"]
        block = f"### {header}\n{r['url']}"
        if r["content"]:
            block += f"\n\n{r['content']}"
        blocks.append(block)
    content_text = "\n\n".join(blocks)

    ok, content_text = _truncate_to_budget(
        content_text, token_budget, _WEBSEARCH_RESERVE_TOKENS,
        _WEBSEARCH_TOO_SMALL, _WEBSEARCH_TRUNC_TAIL,
    )
    if not ok:
        return False, _WEBSEARCH_TOO_SMALL
    return True, content_text


def _openrouter_key_available():
    # True if config.OPENROUTER_KEY looks usable (non-empty and not the
    # template placeholder). Drives the "skip OpenRouter entirely" path
    # so a missing/placeholder key doesn't waste a network round-trip.
    key = getattr(config, "OPENROUTER_KEY", "") or ""
    key = key.strip()
    if not key:
        return False
    return key != _OPENROUTER_KEY_PLACEHOLDER


async def execute_websearch(query, token_budget=None):
    # Run a web search via OpenRouter's openrouter:web_search server tool.
    # Uses a FIXED model (config.WEBSEARCH_MODEL) independent of the channel's
    # current text model, so search works regardless of which model the channel
    # is set to. Always hits OpenRouter directly (not nanogpt), since the web
    # search server tool is an OpenRouter-specific feature.
    #
    # FALLBACK: if OpenRouter is unavailable (no API key, HTTP failure, or
    # empty result), falls back to a keyless DuckDuckGo Lite scrape that
    # returns snippet-only results in the same format. Controlled by
    # config.WEBSEARCH_FALLBACK ("on_failure" default, or "off" to disable).
    #
    # token_budget: available tokens remaining before hitting the context limit.
    #   If provided, results are truncated so estimated tokens (bytes/4) stay
    #   within that budget (minus a reserve for the model's reply).
    # Returns (success: bool, message: str) where message is the raw search
    #   results (titles, URLs, excerpt snippets) formatted as structured text
    #   for the main conversation model to synthesize from.
    model = getattr(config, "WEBSEARCH_MODEL", "openai/gpt-4.1")
    engine = getattr(config, "WEBSEARCH_ENGINE", "auto")
    try:
        max_results = int(getattr(config, "WEBSEARCH_MAX_RESULTS", 5))
    except (TypeError, ValueError):
        max_results = 5
    fallback_mode = getattr(config, "WEBSEARCH_FALLBACK", "on_failure").lower()
    if fallback_mode not in ("on_failure", "off"):
        fallback_mode = "on_failure"

    if not query or not query.strip():
        return False, "No search query provided."

    # If there's no usable OpenRouter key, skip straight to the fallback
    # rather than burning a network round-trip on a guaranteed 401.
    if not _openrouter_key_available():
        if fallback_mode == "off":
            return False, (
                "Web search unavailable: no OpenRouter API key configured "
                "(config.OPENROUTER_KEY) and WEBSEARCH_FALLBACK is off."
            )
        return await _websearch_fallback_ddg(query, max_results, token_budget)

    try:
        headers = {
            "Authorization": f"Bearer {config.OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://discord.com",
            "X-Title": "Alcove websearch",
            **HTTP_PIN,
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a web research assistant. Use the web search tool to "
                        "find current, accurate information for the user's query. Return "
                        "the search results you find — titles, URLs, and the relevant "
                        "excerpts from each source."
                    ),
                },
                {"role": "user", "content": query},
            ],
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "parameters": {
                        "engine": engine,
                        "max_results": max_results,
                    },
                }
            ],
        }
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload,
            ) as response:
                body_text = await response.text()
                if response.status != 200:
                    # Don't return the error — fall through to the fallback
                    # so the user still gets results when OpenRouter is up
                    # but rejecting this request (rate limit, bad key, etc.).
                    if fallback_mode == "off":
                        return False, (
                            f"Web search request failed (HTTP {response.status}): "
                            f"{body_text[:500]}"
                        )
                    print(f"🔍 websearch: OpenRouter HTTP {response.status}, "
                          f"falling back to DuckDuckGo.")
                    return await _websearch_fallback_ddg(
                        query, max_results, token_budget
                    )
                data = json.loads(body_text)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        # Extract url_citation annotations — the raw search results. OpenRouter
        # surfaces these as {"type": "url_citation", "url_citation": {...}} in
        # the message's annotations array. Handle both nested and flat shapes
        # defensively since the exact schema varies across engines/models.
        citations = []
        annotations = message.get("annotations") or []
        for ann in annotations:
            if not isinstance(ann, dict):
                continue
            if ann.get("type") == "url_citation":
                uc = ann.get("url_citation") or ann
                citations.append({
                    "url": uc.get("url") or "",
                    "title": uc.get("title") or "",
                    "content": uc.get("content") or "",
                })

        if citations:
            blocks = []
            for c in citations:
                header = c["title"] if c["title"] else c["url"]
                block = f"### {header}\n{c['url']}"
                if c["content"]:
                    block += f"\n\n{c['content']}"
                blocks.append(block)
            content_text = "\n\n".join(blocks)
        else:
            # Fall back to the model's text answer if no url_citation annotations
            # were returned (some engines embed sources inline instead).
            content_text = message.get("content") or ""

        # If OpenRouter gave us nothing usable (no citations AND no text),
        # fall through to the keyless fallback rather than returning
        # "(no results)" — the fallback may still find something.
        if not content_text.strip() and fallback_mode != "off":
            print("🔍 websearch: OpenRouter returned no content, "
                  "falling back to DuckDuckGo.")
            return await _websearch_fallback_ddg(
                query, max_results, token_budget
            )
        if not content_text.strip():
            content_text = "(no results)"

        ok, content_text = _truncate_to_budget(
            content_text, token_budget, _WEBSEARCH_RESERVE_TOKENS,
            _WEBSEARCH_TOO_SMALL, _WEBSEARCH_TRUNC_TAIL,
        )
        if not ok:
            return False, _WEBSEARCH_TOO_SMALL
        return True, content_text
    except Exception as e:
        # Last-ditch: try the fallback before surfacing the error, so
        # a transient OpenRouter outage doesn't kill the search entirely.
        if fallback_mode != "off":
            print(f"🔍 websearch: OpenRouter error ({e}), "
                  f"falling back to DuckDuckGo.")
            return await _websearch_fallback_ddg(
                query, max_results, token_budget
            )
        return False, f"Error running web search: {e}"


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


# ---------------------------------------------------------------------------
# <attachFileToChannel> — read a file (local path or URL), post it to Discord, and
# attach its contents to the model's context. Supports images (embedded as
# visual content), text files (fed as text), and arbitrary binary files
# (posted to Discord only). Cross-platform path handling (Win/Mac/Linux).
# ---------------------------------------------------------------------------

_ATTACHFILE_MAX_BYTES = 25 * 1024 * 1024  # Discord free-tier upload ceiling
_ATTACHFILE_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml",
    ".yml", ".html", ".htm", ".log", ".py", ".js", ".mjs", ".cjs", ".ts",
    ".tsx", ".jsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".java",
    ".kt", ".rs", ".go", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".bat", ".cmd", ".ps1", ".psm1", ".ini", ".cfg", ".conf", ".toml",
    ".env", ".gitignore", ".gitattributes", ".dockerfile", ".makefile",
    ".r", ".m", ".sql", ".graphql", ".gql", ".vue", ".svelte", ".lua",
    ".pl", ".pm", ".scala", ".swift", ".dart", ".gradle", ".properties",
    ".css", ".scss", ".sass", ".less", ".svg", ".rtf",
}
_ATTACHFILE_TEXT_MIME_PREFIXES = (
    "text/", "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/yaml", "application/x-sh",
)
_ATTACHFILE_TEXT_PREVIEW_MAX_CHARS = 20000
# content-type -> extension fallback for URL downloads with no path filename
_ATTACHFILE_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/x-icon": ".ico",
    "image/svg+xml": ".svg", "image/tiff": ".tif",
    "text/plain": ".txt", "text/html": ".html", "text/css": ".css",
    "text/csv": ".csv", "application/json": ".json", "application/xml": ".xml",
    "application/javascript": ".js", "application/pdf": ".pdf",
    "application/zip": ".zip", "application/x-tar": ".tar",
    "application/gzip": ".gz", "application/x-7z-compressed": ".7z",
    "application/octet-stream": ".bin", "video/mp4": ".mp4",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
    "audio/webm": ".weba", "video/webm": ".webm", "video/quicktime": ".mov",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
}


def _attachfile_classify(ext, mime):
    # Decide whether a file is an image, text, or binary based on its
    # extension and (for URL downloads) Content-Type. Returns one of
    # "image", "text", "binary".
    ext = (ext or "").lower()
    mime = (mime or "").lower().split(";")[0].strip()
    if ext in _READIMAGE_MIME_BY_EXT or mime.startswith("image/"):
        return "image"
    if ext in _ATTACHFILE_TEXT_EXTS or any(mime.startswith(p) for p in _ATTACHFILE_TEXT_MIME_PREFIXES):
        return "text"
    return "binary"


def _attachfile_mime_for_image(ext, mime):
    # Resolve the MIME type for an image (used to build the data: URI the
    # model consumes). Prefer the extension's known MIME; fall back to the
    # URL's Content-Type; final fallback image/png.
    if ext and ext in _READIMAGE_MIME_BY_EXT:
        return _READIMAGE_MIME_BY_EXT[ext]
    if mime and mime.startswith("image/"):
        return mime
    return "image/png"


async def execute_attachfiletochannel(source):
    # Resolve a <attachFileToChannel> source (URL or local file path) to a dict
    # describing the file to post to Discord and feed to the model.
    # Returns (success, result) where on success result is:
    #   {"kind": "image"|"text"|"binary", "bytes": <bytes>, "filename": str,
    #    "mime": str, "image_url": <data uri> (image only),
    #    "text_content": <str> (text only)}
    # and on failure result is an error message string.
    source = (source or "").strip()
    if not source:
        return False, "attachFileToChannel: empty source"

    lowered = source.lower()
    if lowered.startswith(("http://", "https://")):
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source, headers=HTTP_PIN) as resp:
                    if resp.status != 200:
                        return False, f"attachFileToChannel: HTTP {resp.status} fetching {source}"
                    # Stream against the size cap instead of buffering the
                    # whole body first — resp.read() would happily pull
                    # gigabytes into RAM before the limit was ever checked.
                    _buf = bytearray()
                    async for _chunk in resp.content.iter_chunked(64 * 1024):
                        _buf.extend(_chunk)
                        if len(_buf) > _ATTACHFILE_MAX_BYTES:
                            mb = _ATTACHFILE_MAX_BYTES / (1024 * 1024)
                            return False, (f"attachFileToChannel: download exceeded "
                                           f"{mb:.0f} MB cap — aborted")
                    raw = bytes(_buf)
                    content_type = resp.headers.get("Content-Type", "") or ""
        except Exception as e:
            return False, f"attachFileToChannel: failed to fetch {source}: {e}"

        if len(raw) > _ATTACHFILE_MAX_BYTES:
            mb = len(raw) / (1024 * 1024)
            return False, f"attachFileToChannel: file too large ({mb:.1f} MB, max 25 MB)"
        if not raw:
            return False, f"attachFileToChannel: empty response from {source}"

        # Derive filename from URL path; fall back to content-type mapping.
        path_part = unquote(urlsplit(source).path)
        filename = os.path.basename(path_part) or ""
        ext = os.path.splitext(filename)[1].lower()
        mime = content_type.lower().split(";")[0].strip()
        if not filename or filename == "/" or not ext:
            # Try to recover an extension from the content-type.
            guessed_ext = _ATTACHFILE_EXT_BY_MIME.get(mime, "")
            if not filename or filename == "/":
                filename = "download" + guessed_ext
            elif not ext:
                filename = filename + guessed_ext
                ext = guessed_ext
        kind = _attachfile_classify(ext, mime)
        result = {"kind": kind, "bytes": raw, "filename": filename, "mime": mime}
        if kind == "image":
            img_mime = _attachfile_mime_for_image(ext, mime)
            b64 = base64.b64encode(raw).decode("ascii")
            result["image_url"] = f"data:{img_mime};base64,{b64}"
        elif kind == "text":
            text = raw.decode("utf-8", errors="replace")
            if len(text) > _ATTACHFILE_TEXT_PREVIEW_MAX_CHARS:
                text = text[:_ATTACHFILE_TEXT_PREVIEW_MAX_CHARS] + (
                    "\n\n... (text content truncated for context preview; "
                    "the full file was still posted to Discord)"
                )
            result["text_content"] = text
        return True, result

    # Local filesystem path. Strip surrounding quotes the LLM may add around
    # Windows paths with spaces.
    path = source.strip('"').strip("'")
    if not os.path.isfile(path):
        return False, f"attachFileToChannel: file not found: {path}"
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"attachFileToChannel: cannot stat {path}: {e}"
    if size > _ATTACHFILE_MAX_BYTES:
        mb = size / (1024 * 1024)
        return False, f"attachFileToChannel: file too large ({mb:.1f} MB, max 25 MB)"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return False, f"attachFileToChannel: failed to read {path}: {e}"
    if not raw:
        return False, f"attachFileToChannel: file is empty: {path}"

    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    kind = _attachfile_classify(ext, "")
    mime = _READIMAGE_MIME_BY_EXT.get(ext, "application/octet-stream")
    if kind == "image":
        mime = _READIMAGE_MIME_BY_EXT[ext]
    result = {"kind": kind, "bytes": raw, "filename": filename, "mime": mime}
    if kind == "image":
        b64 = base64.b64encode(raw).decode("ascii")
        result["image_url"] = f"data:{mime};base64,{b64}"
    elif kind == "text":
        text = raw.decode("utf-8", errors="replace")
        if len(text) > _ATTACHFILE_TEXT_PREVIEW_MAX_CHARS:
            text = text[:_ATTACHFILE_TEXT_PREVIEW_MAX_CHARS] + (
                "\n\n... (text content truncated for context preview; "
                "the full file was still posted to Discord)"
            )
        result["text_content"] = text
    return True, result


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


async def execute_autojournal(channel, user_message, journal_content, active_companion=None, journal_mode="short", db=None, channel_name=None):
    import os
    import re
    import tempfile
    import shutil
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    import config
    from .database import get_channel_companion, get_db, get_channel_setting, set_channel_setting

    try:
        # 1. Resolve channel key and active companion
        if channel_name is None:
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
        if active_companion is None:
            active_companion = get_channel_companion(channel_name) if channel_name else "default"

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
        # DESIGN NOTE — intentional. The permanent journal is meant for
        # moments the user/companion wants to remember forever. It is
        # written infrequently and must be manually pruned by the user
        # when it grows too large for the configured context window; if
        # it outgrows the model's window the system-block abort in
        # llm_prompt_builder.py will surface that as an explicit error (by design —
        # see llm_prompt_builder.py:build_trimmed_history_for_payload). Alcove will
        # not silently discard permanent memories on the user's behalf.
        long_term_path = None
        if journal_mode == "short":
            search_reference_dir = comp_dir / "5_search_reference"
            search_reference_dir.mkdir(parents=True, exist_ok=True)
            long_term_path = search_reference_dir / "kb_auto_long_term_journal.md"

        # 3. Format current date & time (respecting configured offset)
        now = datetime.now(timezone.utc) + timedelta(hours=getattr(config, "TIMEZONE_OFFSET", 0))
        now_str = now.strftime("%Y-%m-%d-%H-%M-%S")

        # 3a. Resolve per-companion DB for channel_settings reads/writes.
        # Soft-fail: journal writes must never break on a DB problem.
        if db is None and channel_name:
            try:
                db = get_db(active_companion)
            except Exception:
                db = None

        # 3b. Date-range headers: when LIVE_JOURNAL_ENTRY_DATE_RANGE is on
        # and the channel has a valid last_journal_datetime in channel_settings,
        # the entry header spans that timestamp to now. Missing/null/corrupt
        # values or any DB error degrade to the original single-date format.
        last_journal_str = None
        if getattr(config, "LIVE_JOURNAL_ENTRY_DATE_RANGE", False) and db is not None and channel_name:
            try:
                stored = get_channel_setting(db, channel_name, "last_journal_datetime")
                if stored:
                    datetime.strptime(stored, "%Y-%m-%d-%H-%M-%S")
                    last_journal_str = stored
            except Exception:
                last_journal_str = None

        # 4. Check if LLM already added a '###' timestamp line, and strip it to avoid duplication
        journal_text = journal_content.strip()
        if journal_text.startswith("###"):
            lines = journal_text.splitlines()
            if lines and lines[0].strip().startswith("###"):
                journal_text = "\n".join(lines[1:]).strip()

        # Format new entry
        if last_journal_str:
            new_entry = f"### {last_journal_str} - {now_str}\n{journal_text}\n\n"
        else:
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
            #
            # DESIGN NOTE — the backup -> tmp-write -> os.replace -> cleanup
            # dance is intentional redundancy for fallback safety during
            # errors, not waste. These are small, infrequent journal
            # writes (roughly once per session/day), so the extra I/O is
            # negligible and the atomicity guarantees are worth it. Do not
            # "optimize" this down to a single in-place write.
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

            # 10. Stamp last_journal_datetime so the next entry's range starts
            # from this write. Runs regardless of LIVE_JOURNAL_ENTRY_DATE_RANGE;
            # soft-fail so a settings error can't fail an already-written journal.
            if db is not None and channel_name:
                try:
                    set_channel_setting(db, channel_name, "last_journal_datetime", now_str)
                except Exception as stamp_err:
                    print(f"⚠️ updateJournal: failed to stamp last_journal_datetime for {channel_name} ({stamp_err})")

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


# ============================================
# TASK DIRECTIVE HANDLER
# <task action="create|edit|remove|list" event_type="recurring|once"
#       when="..." status="active|disabled" id="N">prompt body</task>
#
# Channel is always the current channel_name (guild:::channel). All enum
# fields are case-insensitive. On any malformed field, log to console AND
# post a descriptive error to Discord so the user can see what the LLM
# sent wrong and rephrase.
#
# action=list returns the formatted task list back to the model as a tool
# response (via the readskill_results channel) so it can look up ids or
# check whether a task exists before create/edit/remove.
# ============================================

_VALID_ACTIONS = {"create", "edit", "remove", "list"}
_VALID_EVENT_TYPES = {"recurring", "once"}
_VALID_STATUSES = {"active", "disabled"}


def _validate_cron_expr(expr):
    # Returns (ok, error_msg). Constructs a croniter against a fixed base
    # time purely to validate syntax — the resulting object is discarded.
    try:
        from croniter import croniter
        from datetime import datetime
        croniter(expr, datetime(2026, 1, 1, 0, 0))
        return True, None
    except ImportError:
        return False, "croniter library is not installed"
    except Exception as e:
        return False, str(e)


def _validate_iso_once(ts):
    # Returns (ok, error_msg). Expects local-naive YYYY-MM-DDTHH:MM:SS.
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            return False, "datetime includes a timezone offset — use a naive local datetime (no 'Z' or '+HH:MM')"
        return True, None
    except (TypeError, ValueError) as e:
        return False, str(e)


async def _handle_task_directive(seg, channel, db, channel_name, active_companion, _send):
    action = seg.get("action") or ""
    event_type = seg.get("event_type") or ""
    when = seg.get("when") or ""
    status = seg.get("status") or ""
    raw_id = seg.get("id") or ""
    prompt = seg.get("content") or ""

    def _err(msg):
        print(f"⚠️ [task] {msg}")
        try:
            import asyncio as _aio
            asyncio.ensure_future(_send(f"*⚠️ {msg}*"))
        except Exception:
            pass

    if action not in _VALID_ACTIONS:
        _err(f"task directive: invalid action `{action!r}`. "
             f"Expected one of: {', '.join(sorted(_VALID_ACTIONS))}.")
        return

    if action == "create":
        if not event_type:
            _err("task directive: action=create requires `event_type` "
                 "(expected 'recurring' or 'once').")
            return
        if event_type not in _VALID_EVENT_TYPES:
            _err(f"task directive: invalid event_type `{event_type!r}`. "
                 f"Expected one of: {', '.join(sorted(_VALID_EVENT_TYPES))}.")
            return
        if not when:
            _err(f"task directive: action=create requires `when` "
                 f"(expected {'a 5-field cron expression' if event_type == 'recurring' else 'an ISO datetime YYYY-MM-DDTHH:MM:SS'}).")
            return
        if event_type == "recurring":
            ok, err = _validate_cron_expr(when)
            if not ok:
                _err(f"task directive: invalid `when` for recurring job. "
                     f"Expected a 5-field cron expression (e.g. \"*/5 * * * *\", "
                     f"\"0 9 * * 1\"). Received: {when!r}. Error: {err}")
                return
        else:  # once
            ok, err = _validate_iso_once(when)
            if not ok:
                _err(f"task directive: invalid `when` for once job. "
                     f"Expected local-naive ISO datetime YYYY-MM-DDTHH:MM:SS "
                     f"(e.g. \"2026-07-20T14:30:00\"). Received: {when!r}. Error: {err}")
                return
        if not prompt:
            _err("task directive: action=create requires a prompt body.")
            return
        if status and status not in _VALID_STATUSES:
            _err(f"task directive: invalid status `{status!r}`. "
                 f"Expected one of: {', '.join(sorted(_VALID_STATUSES))}.")
            return
        final_status = status or "active"
        try:
            new_id = add_crontab_entry(
                db, event_type, when, prompt, channel_name, status=final_status,
            )
            print(f"📌 [task] created #{new_id} for '{active_companion}' in "
                  f"'{channel_name}' ({event_type}, when={when!r}, status={final_status})")
            await _send(f"*📌 Task #{new_id} created ({event_type}, "
                        f"when=`{when}`, status={final_status}) in `{channel_name}`.*")
        except Exception as e:
            _err(f"task directive: failed to create task: {e}")
        return

    if action == "edit":
        m = re.search(r"\d+", raw_id)
        if not m:
            _err(f"task directive: action=edit requires `id` (numeric). Received: {raw_id!r}.")
            return
        tid = int(m.group(0))
        existing = get_crontab_entry(db, tid)
        if existing is None:
            _err(f"task directive: no task with id #{tid} to edit.")
            return
        fields = {}
        if event_type:
            if event_type not in _VALID_EVENT_TYPES:
                _err(f"task directive: invalid event_type `{event_type!r}`. "
                     f"Expected one of: {', '.join(sorted(_VALID_EVENT_TYPES))}.")
                return
            fields["event_type"] = event_type
        if when:
            effective_et = event_type or existing["event_type"]
            if effective_et == "recurring":
                ok, err = _validate_cron_expr(when)
                if not ok:
                    _err(f"task directive: invalid `when` for recurring job (id #{tid}). "
                         f"Expected 5-field cron. Received: {when!r}. Error: {err}")
                    return
            else:
                ok, err = _validate_iso_once(when)
                if not ok:
                    _err(f"task directive: invalid `when` for once job (id #{tid}). "
                         f"Expected ISO datetime. Received: {when!r}. Error: {err}")
                    return
            fields["when"] = when
        if status:
            if status not in _VALID_STATUSES:
                _err(f"task directive: invalid status `{status!r}`. "
                     f"Expected one of: {', '.join(sorted(_VALID_STATUSES))}.")
                return
            fields["status"] = status
        if prompt:
            fields["prompt"] = prompt
        if not fields:
            _err(f"task directive: action=edit on #{tid} but no fields to update "
                 f"(provide at least one of event_type, when, status, or a prompt body).")
            return
        try:
            updated = update_crontab_entry(db, tid, **fields)
            if updated:
                print(f"📝 [task] edited #{tid} for '{active_companion}' in "
                      f"'{channel_name}' (fields: {', '.join(fields.keys())})")
                await _send(f"*📝 Task #{tid} updated "
                            f"(fields: {', '.join(fields.keys())}).*")
            else:
                _err(f"task directive: edit on #{tid} reported no rows changed.")
        except Exception as e:
            _err(f"task directive: failed to edit task #{tid}: {e}")
        return

    if action == "remove":
        m = re.search(r"\d+", raw_id)
        if not m:
            _err(f"task directive: action=remove requires `id` (numeric). Received: {raw_id!r}.")
            return
        tid = int(m.group(0))
        try:
            if delete_crontab_entry(db, tid):
                print(f"🗑️ [task] removed #{tid} for '{active_companion}' in '{channel_name}'")
                await _send(f"*🗑️ Task #{tid} removed.*")
            else:
                _err(f"task directive: no task with id #{tid} to remove.")
        except Exception as e:
            _err(f"task directive: failed to remove task #{tid}: {e}")
        return

    if action == "list":
        # Returns a readskill_results-style payload so the caller can feed
        # the formatted task list back to the model as a tool response.
        # Other actions fall through and return None.
        if db is None:
            _err("task directive: action=list but no database available.")
            return None
        if status and status not in _VALID_STATUSES:
            _err(f"task directive: invalid status `{status!r}`. "
                 f"Expected one of: {', '.join(sorted(_VALID_STATUSES))}.")
            return None
        print(f"📋 [task] list requested by '{active_companion}' in "
              f"'{channel_name}'{f' (status={status})' if status else ''}")
        await _send("*📋 Reading tasks...*")
        try:
            rows = list_crontab_entries(db, status=status or None)
        except Exception as e:
            _err(f"task directive: failed to list tasks: {e}")
            return None
        if not rows:
            suffix = f" with status '{status}'" if status else ""
            return {
                "path": "task list",
                "success": True,
                "output": f"No scheduled tasks{suffix} for this companion.",
            }
        # Lazy call-time import of the !tasks formatting helpers — avoids
        # any circular-import hazard at module load.
        try:
            from .commands import _compute_next_fire, _describe_cron
        except Exception:
            _compute_next_fire = _describe_cron = None
        from datetime import datetime, timezone, timedelta
        now_local = (datetime.now(tz=timezone.utc)
                     + timedelta(hours=getattr(config, "TIMEZONE_OFFSET", 0))
                     ).replace(tzinfo=None)
        _PROMPT_DISPLAY_CAP = 300
        lines = ["Scheduled Tasks:\n"]
        for row in rows:
            et = row["event_type"]
            next_fire = (
                _compute_next_fire(et, row["when"], now_local, row.get("last_fired"))
                if _compute_next_fire else "(unknown)"
            )
            prompt_display = row["prompt"] or "(empty)"
            if len(prompt_display) > _PROMPT_DISPLAY_CAP:
                prompt_display = prompt_display[:_PROMPT_DISPLAY_CAP] + "... (truncated)"
            when_display = row["when"]
            if et == "recurring" and _describe_cron:
                when_display = _describe_cron(row["when"]) or row["when"]
            fail_info = f" | ⚠️ fails: {row['fail_count']}" if row.get("fail_count", 0) > 0 else ""
            lines.append(
                f"`#{row['id']}` [{et}] {row['status']} | next: {next_fire} "
                f"| 🗓️ {when_display} | channel: `{row['channel']}`{fail_info}\n{prompt_display}\n"
            )
        return {
            "path": "task list",
            "success": True,
            "output": "\n".join(lines),
        }


async def process_response(response_text, channel, image_handler=None, video_handler=None, token_budget=None, db=None, user_message=None, consecutive_reacts=0, suppress_reacts=False, active_companion=None, send_func=None, channel_name=None, anchor_review_ctx=None, block_runcmd=False):
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
    # websearch_results: list of {"query": ..., "success": bool, "output": ...} dicts
    # readskill_results: list of {"path": ..., "success": bool, "output": ...} dicts
    #   — output is the full skill file contents on success, or an error message on failure.
    # had_react: True if the response contained a <react> directive (even if skipped)
    # Directive results are also reported back to the channel as status messages.
    # image_handler is an async function(prompt) that returns an image result dict.
    # video_handler is an async function(prompt, reference_images, reference_videos, seconds) that returns a video result dict.
    # suppress_reacts: if True, <react> directives are parsed but silently skipped
    #   (used for tool-chain follow-up rounds so reacts only fire on the first response).
    # anchor_review_ctx: optional ChannelContext. When provided, a successful
    #   <autojournal>/<permjournal> directive is recorded in the returned
    #   `had_journal_write` flag so the caller can fire run_anchor_review() AFTER
    #   saving the main response to the DB — preserving correct chronological
    #   ordering (main assistant turn, then the review turns). Pass None on tool
    #   follow-up rounds and the review's own inner process_response call to
    #   suppress the flag and prevent recursion.
    # Returns a 9-tuple: (display_text, runcmd_results, readweb_results,
    #   readimage_results, websearch_results, readskill_results, had_react,
    #   react_was_posted, had_journal_write).
    had_journal_write = False
    segments = parse_directives(response_text)
    text_parts = []
    runcmd_results = []
    readweb_results = []
    readimage_results = []
    websearch_results = []
    readskill_results = []
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
            if block_runcmd:
                # DESIGN NOTE — autonomous contexts (idle/dream/note/random-thought/
                # anchor review) must not run arbitrary shell commands on the host
                # without a user present to approve. See the `block_runcmd` flag
                # threaded from _run_idle_prompt (via log_label), _run_random_thought_prompt,
                # and run_anchor_review. User turns, tasks (task#/task_once#), and live
                # voice keep `runcmd` enabled (block_runcmd=False, the default).
                print(f"🔒 runcmd directive blocked: autonomous context (block_runcmd=True)")
                await _send(
                    "*🔒 The `runcmd` directive is not available in autonomous "
                    "contexts (idle/dream/anchor review).*"
                )
                continue
            print(f"🔧 runcmd directive: {seg['command'][:100]}")
            # Resolve optional timeout="..." attribute (default 180s, max 900s).
            _RUNCMD_DEFAULT_TIMEOUT = 180
            _RUNCMD_MAX_TIMEOUT = 900
            raw_timeout = seg.get("timeout")
            try:
                timeout = int(raw_timeout) if raw_timeout else _RUNCMD_DEFAULT_TIMEOUT
            except (TypeError, ValueError):
                timeout = _RUNCMD_DEFAULT_TIMEOUT
            timeout = max(1, min(timeout, _RUNCMD_MAX_TIMEOUT))
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
            await _send(f"*🔧 Running (timeout: {timeout}s):*\n```\n{cmd_block}\n```")

            success, msg = execute_runcmd(seg["command"], timeout=timeout)
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

        elif seg["type"] == "createvideo":
            if video_handler is None:
                print("⚠️ createvideo directive but no video_handler provided")
                await _send("*⚠️ Video generation not available*")
                continue
            prompt = seg["prompt"]
            use_refs = seg.get("use") == "reference"
            seconds = seg.get("seconds")
            ref_label = " with references" if use_refs else ""
            dur_label = f", {seconds}s" if seconds else ""
            print(f"🎬 createvideo directive{ref_label}{dur_label}: {prompt[:100]}")
            await _send(f"*🎬 Generating video{ref_label}... this may take a few minutes.*")
            result = await video_handler(
                prompt,
                reference_images=None if use_refs else [],
                reference_videos=None if use_refs else [],
                seconds=seconds,
            )
            await deliver_video(channel, result, prompt_slug=prompt, send_func=_send)

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

        elif seg["type"] == "websearch":
            query = seg["query"]
            print(f"🔍 websearch directive: {query[:100]}")
            await _send(f"*🔍 Searching the web: {query[:150]}...*")
            success, content = await execute_websearch(query, token_budget=token_budget)
            icon = "✅" if success else "⚠️"
            print(f"{icon} websearch result: {content[:200]}")
            websearch_results.append({
                "query": query,
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

        elif seg["type"] == "attachfiletochannel":
            source = seg["source"]
            print(f"📎 attachFileToChannel directive: {source}")
            success, result = await execute_attachfiletochannel(source)
            if not success:
                print(f"⚠️ attachFileToChannel result: {result}")
                await _send(f"*⚠️ {result}*")
                # Surface the error to the model in the next tool round via
                # the readskill_results channel (reuses existing plumbing —
                # no tuple-shape change needed at any process_response callsite).
                readskill_results.append({
                    "path": source,
                    "success": False,
                    "output": result,
                })
                continue

            kind = result["kind"]
            filename = result["filename"]
            file_bytes = result["bytes"]
            print(f"✅ attachFileToChannel result: {kind} '{filename}' "
                  f"({len(file_bytes)} bytes)")
            # Post the file to Discord so the user sees it (images render
            # inline; other files appear as downloadable attachments).
            posted_to_discord = False
            discord_error = None
            try:
                file = discord.File(io.BytesIO(file_bytes), filename=filename)
                await _send(file=file)
                posted_to_discord = True
            except discord.HTTPException as e:
                discord_error = f"Discord rejected upload: {e}"
                print(f"⚠️ attachFileToChannel Discord upload failed: {e}")
            except Exception as e:
                discord_error = f"Failed to post file to Discord: {e}"
                print(f"⚠️ attachFileToChannel post failed: {e}")

            if not posted_to_discord:
                await _send(f"*⚠️ attachFileToChannel: {discord_error}*")
                readskill_results.append({
                    "path": source,
                    "success": False,
                    "output": f"attachFileToChannel: {discord_error}",
                })
                continue

            # Feed the file's contents to the model via existing result
            # channels so the tool-round loop in llm_loop.py / idle.py picks
            # them up with no further changes:
            #   - image -> readimage_results (model gets an image_url block)
            #   - text  -> readskill_results (model gets the text inline)
            #   - binary-> readskill_results (model gets a note that the
            #              file was posted but no content was extracted)
            if kind == "image":
                readimage_results.append({
                    "source": source,
                    "success": True,
                    "image_url": result["image_url"],
                    "error": None,
                })
            elif kind == "text":
                preview = result["text_content"]
                readskill_results.append({
                    "path": source,
                    "success": True,
                    "output": (
                        f"attachFileToChannel: posted text file '{filename}' "
                        f"({len(file_bytes)} bytes) to Discord and attached "
                        f"its contents below for analysis.\n\n--- BEGIN FILE CONTENTS ---\n"
                        f"{preview}\n--- END FILE CONTENTS ---"
                    ),
                })
            else:
                readskill_results.append({
                    "path": source,
                    "success": True,
                    "output": (
                        f"attachFileToChannel: posted binary file '{filename}' "
                        f"({len(file_bytes)} bytes, MIME {result.get('mime', 'unknown')}) "
                        f"to Discord. No content was extracted into the model's "
                        f"context (binary type). If you need to analyze its "
                        f"contents, use a more specific tool (e.g. <readimage> "
                        f"for images, <runcmd> for shell-based extraction)."
                    ),
                })

        elif seg["type"] == "readskill":
            skill_path = seg["path"]
            print(f"📖 readskill directive: {skill_path}")
            skill_name = os.path.basename(os.path.dirname(skill_path)) or os.path.basename(skill_path)
            await _send(f"*📖 Loading skill: {skill_name}...*")
            success, msg = execute_readskill(skill_path, token_budget=token_budget, channel_key=channel_name)
            icon = "✅" if success else "⚠️"
            print(f"{icon} readskill result: {msg[:200]}")
            if not success:
                await _send(f"*⚠️ Skill load failed: {msg}*")
            readskill_results.append({
                "path": skill_path,
                "success": success,
                "output": msg,
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
                await _send(f"*📌 Anchored global memory:*\n> {content}")
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
                old_text = get_anchored_memory(db, mid)
                if update_anchored_memory(db, mid, content):
                    print(f"📝 manageanchor update: updated anchor #{mid}")
                    await _send(f"*📝 Updated anchored memory #{mid}:*\n> {content}")
                else:
                    # DESIGN NOTE — intentional silent insert on missing id
                    # (no content-based dedup). If the LLM hallucinates an
                    # id (common for anchors that were pruned), we insert
                    # the content as a new global memory rather than
                    # dropping it. Dedup/cleanup is handled by the anchor
                    # review process (anchor_review.py, which runs after
                    # every journal write) and by the user's own periodic
                    # review/purge — not by a content-equality gate here.
                    add_anchored_memory(db, "global", content)
                    print(f"📌 manageanchor update: no anchor #{mid}, inserted as new global memory")
                    await _send(f"*📝 No anchor #{mid} found — inserted as new anchored memory:*\n> {content}")
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
                old_text = get_anchored_memory(db, mid)
                if remove_anchored_memory(db, mid):
                    print(f"🗑️ manageanchor remove: removed anchor #{mid}")
                    removed_display = old_text if old_text else "(no content)"
                    await _send(f"*🗑️ Removed anchored memory #{mid}:*\n> {removed_display}")
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
                db=db, channel_name=channel_name,
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

                # Record that a journal write happened so the caller can fire
                # the anchored-memory review AFTER saving the main response to
                # the DB (correct chronological order). This closes the
                # consistency gap: any path that writes to the journal (the
                # !updateJournal command, organic <autojournal>/<permjournal>
                # in chat, or idle/dream self-talk) now also reviews the
                # anchors, instead of only the !updateJournal command doing so.
                if anchor_review_ctx is not None:
                    had_journal_write = True

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

        elif seg["type"] == "task":
            task_result = await _handle_task_directive(
                seg, channel, db, channel_name, active_companion, _send,
            )
            if task_result:
                # action=list — feed the task list back to the model via the
                # readskill_results channel (existing tool-response plumbing,
                # same pattern as attachfiletochannel).
                readskill_results.append(task_result)

    return "\n\n".join(text_parts), runcmd_results, readweb_results, readimage_results, websearch_results, readskill_results, had_react, react_was_posted, had_journal_write
