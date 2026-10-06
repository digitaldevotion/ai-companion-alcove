# ============================================
# Alcove — provider.py
# LLM provider abstraction layer
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
#
# Centralizes all outbound LLM API calls behind a provider interface so that
# swapping or adding providers (OpenRouter, nanoGPT, etc.) only requires
# changes in this file and config.py.

import asyncio
import base64
import json
import uuid

import aiohttp
import config
from . import debug as debug_mod
from .database import get_db, get_channel_companion, get_channel_setting
from .utils import msg_tokens, msg_content_bytes, update_token_calibration, HTTP_PIN

# ContentEncodingError is raised by aiohttp when a response body's
# Content-Encoding (e.g. "br") cannot be decoded — normally prevented by
# the Accept-Encoding pin (HTTP_PIN), but a misbehaving server or
# middlebox can still force a compressed body through. Import defensively
# so older aiohttp layouts with different module paths cannot break startup.
try:
    from aiohttp.http_exceptions import ContentEncodingError as _ContentEncodingError
except ImportError:  # pragma: no cover
    # Sentinel exception type that nothing ever raises, keeping the
    # except-clauses below valid on any aiohttp version.
    _ContentEncodingError = type("_NoContentEncodingError", (Exception,), {})


# Generous ceiling — reasoning models (adaptive thinking, long tool rounds)
# can legitimately take a while. We'd rather wait than kill a live request.
# Bumped past pycord's internal typing refresh so a stuck upstream still
# surfaces as an error instead of hanging the message handler forever.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=300)


def _extract_error_detail(data):
    """
    Pull a human-readable error message out of a response dict, handling both
    envelope shapes we see in the wild:

      (a) Our own non-200 envelope: {"error": True, "status": int, "detail": str}
      (b) Upstream 200-with-error envelope (OpenRouter/nanoGPT/Qwen style):
          {"error": {"message": "...", "code": "..."}}    or
          {"error": "some string"}

    Returns (status_str, detail_str). status_str may be "" if unknown.
    """
    err = data.get("error")
    # Case (a): our own envelope — err is literal True.
    if err is True:
        return str(data.get("status", "")), str(data.get("detail", "unknown error"))[:500]
    # Case (b): upstream shape — err is a dict or string.
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or json.dumps(err)[:500]
        code = err.get("code") or err.get("type") or ""
        return str(code), str(msg)[:500]
    if err:
        return "", str(err)[:500]
    return "", ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_provider():
    return getattr(config, "PROVIDER", "openrouter").lower()


def _api_key():
    """Return the API key for the active provider."""
    provider = get_provider()
    if provider == "nanogpt":
        return getattr(config, "NANOGPT_KEY", "")
    return config.OPENROUTER_KEY


def _base_url():
    """Return the base API URL for the active provider."""
    provider = get_provider()
    if provider == "nanogpt":
        return "https://nano-gpt.com/api/v1"
    return "https://openrouter.ai/api/v1"


def _headers():
    """Return the common HTTP headers for the active provider."""
    provider = get_provider()
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        **HTTP_PIN,
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://discord.com"
        headers["X-Title"] = "Companion Bot"
    return headers


def _auth_headers():
    """Minimal auth-only headers (for GET endpoints like /credits, /models)."""
    return {"Authorization": f"Bearer {_api_key()}", **HTTP_PIN}


def provider_name():
    """Return a human-friendly label for the active provider."""
    provider = get_provider()
    return {"openrouter": "OpenRouter", "nanogpt": "nanoGPT"}.get(provider, provider)


def normalize_model_id(model_id):
    """
    Ensure a model ID is in the form the active provider expects.

    nanoGPT's /v1/models endpoint returns OpenRouter-style IDs
    (e.g. 'anthropic/claude-sonnet-4.6') *without* a 'nano-gpt/'
    prefix, and its chat completions endpoint accepts them in that
    same bare form. Some users may have seen a 'nano-gpt/...' form
    in other tooling (e.g. LiteLLM provider paths) and configured
    their model IDs with it — so if we see that prefix we strip it,
    case-insensitively.

    Other providers pass through unchanged.
    """
    if not model_id:
        return model_id
    prov = get_provider()
    if prov != "nanogpt":
        return model_id
    if model_id.lower().startswith("nano-gpt/"):
        return model_id[len("nano-gpt/"):]
    return model_id


def _has_multimodal_content(messages):
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") in ("image_url", "input_audio"):
                    return True
    return False


def _is_explicit_caching_model(model):
    """
    Return True if the model uses explicit cache_control breakpoints
    (Anthropic, Alibaba/Qwen, Google/Gemini). All other models on
    OpenRouter use implicit prefix caching, which is disrupted by
    cache_control markers in the payload.
    """
    if not model:
        return False
    m = model.lower()
    if m.startswith("claude-") or "anthropic/" in m:
        return True
    if m.startswith("qwen/") or "alibaba/" in m:
        return True
    if m.startswith("google/") or "gemini/" in m:
        return True
    return False


def _sanitize_messages(messages, model, provider):
    """
    Remove "cache_control" from message content structures unless the
    model supports explicit caching breakpoints.

    Explicit-caching models (Anthropic, Alibaba/Qwen, Google/Gemini)
    on OpenRouter need cache_control markers. All other OpenRouter
    models use implicit prefix caching — cache_control markers can
    interfere with implicit caching on these models.

    On nanoGPT, cache_control is always stripped because nanoGPT uses
    body-level promptCaching/caching helpers instead of inline markers,
    and having both could exceed the 4-breakpoint limit.
    """
    if not messages:
        return messages

    is_nanogpt = provider == "nanogpt"

    if not is_nanogpt and _is_explicit_caching_model(model):
        return messages

    sanitized = []
    for msg in messages:
        if not isinstance(msg, dict):
            sanitized.append(msg)
            continue

        msg_copy = dict(msg)
        content = msg_copy.get("content")

        if isinstance(content, list):
            block_list = []
            for block in content:
                if isinstance(block, dict):
                    block_copy = dict(block)
                    block_copy.pop("cache_control", None)
                    block_list.append(block_copy)
                else:
                    block_list.append(block)
            msg_copy["content"] = block_list
        elif isinstance(content, dict):
            content_copy = dict(content)
            content_copy.pop("cache_control", None)
            msg_copy["content"] = content_copy

        sanitized.append(msg_copy)

    return sanitized


# ── Chat Completions ─────────────────────────────────────────────────────────

async def chat_completion(model, messages, temperature=None, max_tokens=0,
                          reasoning=None, top_k=None,
                          channel_key=None, provider_lock=None,
                          debug_enabled=False):
    """
    Send a chat completion request and return the raw JSON response dict.

    Callers are responsible for parsing the response (extracting text,
    thinking content, etc.) since the shape is mostly provider-agnostic
    (OpenAI compatible).

    When debug_enabled is True, writes the fully-augmented prompt payload
    and the raw response JSON to diag/<uuid>_prompt.txt and
    diag/<uuid>_response.txt.
    """
    if temperature is None:
        temperature = config.TEMPERATURE

    if top_k is None:
        top_k = getattr(config, "TOP_K", 0)

    prov = get_provider()
    payload = {
        "model": normalize_model_id(model),
        "messages": _sanitize_messages(messages, model, prov),
        "temperature": temperature,
    }
    if top_k and top_k > 0:
        payload["top_k"] = top_k
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens
    if reasoning and reasoning.lower() != "off":
        payload["reasoning"] = {"effort": reasoning.lower()}

    is_anthropic = "anthropic/" in model.lower() or model.lower().startswith("claude-")

    if prov == "nanogpt":
        # nanoGPT omits the usage object (prompt_tokens, etc.) unless it is
        # explicitly requested — only prompt-caching helper fields force it
        # implicitly. Without usage, per-channel token-ratio calibration
        # never updates on nanoGPT, leaving token estimates pinned to the
        # 4-bytes-per-token default and vulnerable to context overflow on
        # token-dense conversations. Harmless when caching helpers already
        # force it.
        payload["include_usage"] = True
        if is_anthropic:
            payload["promptCaching"] = {"enabled": True, "ttl": "5m", "explicitCacheControl": True}
        else:
            payload["caching"] = True

    if prov == "openrouter" and channel_key:
        payload["session_id"] = channel_key

    if provider_lock:
        payload["provider"] = {"order": [provider_lock]}

    # ── Debug capture (prompt side) ──────────────────────────────────────
    # Excludes calls without a channel_key (none currently, but future-proof).
    debug_id = None
    if debug_enabled and channel_key:
        debug_id = str(uuid.uuid4())
        try:
            # Resolve the active companion's DB for this channel so the
            # image/voice/context_limit lookups match what the channel
            # is actually using. Falls back to the default DB.
            try:
                _comp_name = get_channel_companion(channel_key)
            except Exception:
                _comp_name = "default"
            _debug_db = get_db(_comp_name or "default")
            context_limit_raw = get_channel_setting(
                _debug_db, channel_key, "context_token_limit",
                str(config.MAX_CONTEXT_TOKENS),
            )
            try:
                context_limit_val = int(context_limit_raw)
            except (TypeError, ValueError):
                context_limit_val = config.MAX_CONTEXT_TOKENS
            # When auto-adjust is off, the resolver ignores the persisted row
            # and uses MAX_CONTEXT_TOKENS — mirror that here so the debug
            # record matches what is actually enforced.
            if not config.AUTO_CONTEXT_ADJUST:
                context_limit_val = config.MAX_CONTEXT_TOKENS
            prompt_tokens_est = sum(msg_tokens(m, channel_key=channel_key) for m in messages)
            info = {
                "uuid": debug_id,
                "timestamp": debug_mod.now_iso_utc(),
                "channel_key": channel_key,
                "companion": _comp_name or "default",
                "provider": provider_name(),
                "text_model": model,
                "image_model": get_channel_setting(
                    _debug_db, channel_key, "image_model", config.CURRENT_IMAGE_MODEL
                ) or config.CURRENT_IMAGE_MODEL,
                "video_model": get_channel_setting(
                    _debug_db, channel_key, "video_model", config.CURRENT_VIDEO_MODEL
                ) or config.CURRENT_VIDEO_MODEL,
                "voice_text_model": get_channel_setting(
                    _debug_db, channel_key, "voice_text_model", config.CURRENT_VOICE_TEXT_MODEL
                ) or config.CURRENT_VOICE_TEXT_MODEL,
                "context_limit": context_limit_val,
                "prompt_tokens": prompt_tokens_est,
                "headroom_tokens": context_limit_val - prompt_tokens_est,
                "reasoning_effort": reasoning or "off",
                "temperature": temperature if temperature is not None else config.TEMPERATURE,
                "top_k": top_k if top_k else None,
                "text_provider_lock": provider_lock,
                "message_count": len(messages),
                "multimodal": _has_multimodal_content(messages),
            }
            header = debug_mod.build_header_block(info)
            await debug_mod.write_prompt_file(debug_id, header, messages)
        except Exception as _e:
            print(f"⚠️ [debug] failed to write prompt file for {debug_id}: {_e}")
            debug_id = None  # don't attempt the response-side write

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{_base_url()}/chat/completions",
                headers=_headers(),
                json=payload,
            ) as response:
                # Read the body as text first so we can still salvage a
                # detail message even if JSON parsing fails on a 200.
                body_text = await response.text()
                if response.status != 200:
                    data = {"error": True, "status": response.status,
                            "detail": body_text[:500] or response.reason or "no body"}
                else:
                    try:
                        data = json.loads(body_text)
                        usage = data.get("usage")
                        if usage and isinstance(usage, dict) and channel_key:
                            prompt_tokens = usage.get("prompt_tokens", 0)
                            if prompt_tokens and prompt_tokens > 0:
                                if not _has_multimodal_content(messages):
                                    # Calibrate on CONTENT bytes (the same basis
                                    # estimate_tokens measures), NOT json.dumps
                                    # bytes. JSON serialization inflates the byte
                                    # count with role labels, quotes, and escaping
                                    # the estimator never sees — structurally
                                    # biasing the calibrated bytes-per-token ratio
                                    # high and every token estimate low. On long
                                    # many-message conversations that bias compounds
                                    # until a "fits the budget" payload actually
                                    # exceeds the endpoint's context window.
                                    msg_bytes = sum(msg_content_bytes(m) for m in messages)
                                    update_token_calibration(channel_key, msg_bytes, prompt_tokens)
                    except (json.JSONDecodeError, ValueError) as e:
                        data = {"error": True, "status": response.status,
                                "detail": f"invalid JSON from upstream ({e}); body: {body_text[:300]}"}
    except asyncio.TimeoutError:
        data = {"error": True, "status": 0,
                "detail": f"request timed out after {REQUEST_TIMEOUT.total:.0f}s"}
    except aiohttp.ClientError as e:
        data = {"error": True, "status": 0, "detail": f"network error: {e}"}
    except _ContentEncodingError as e:
        # Server sent a compressed body this interpreter can't decode.
        # Normally prevented by the Accept-Encoding pin, but a misbehaving
        # server or middlebox can force one through — surface the likely
        # root cause (broken optional decompressor) instead of the raw
        # cryptic "400, message: Can not decode content-encoding: br".
        data = {"error": True, "status": 0,
                "detail": f"couldn't decode the server's compressed response "
                          f"({e}). This machine likely has a broken brotli/zstd "
                          f"module — reinstall Brotli or remove the "
                          f"brotli/brotlicffi packages."}
    except Exception as e:
        # Last-ditch catch-all so a provider bug never crashes on_message.
        data = {"error": True, "status": 0, "detail": f"unexpected error: {e}"}

    # ── Debug capture (response side) ────────────────────────────────────
    if debug_id is not None:
        try:
            await debug_mod.write_response_file(debug_id, data)
        except Exception as _e:
            print(f"⚠️ [debug] failed to write response file for {debug_id}: {_e}")

    return data


# ── Convenience wrappers ─────────────────────────────────────────────────────

async def chat_completion_text(model, messages, temperature=None,
                                max_tokens=0, reasoning=None, top_k=None,
                                channel_key=None, provider_lock=None,
                                debug_enabled=False):
    """
    Convenience wrapper: returns the assistant's text content directly,
    or an error string starting with '*Error'.

    Defensive against several real-world failure shapes observed from
    OpenRouter / nanoGPT / Qwen upstreams:
      - HTTP non-200 (our own envelope)
      - HTTP 200 with a nested error envelope (upstream moderation, rate
        limits, provider-side failures returned as {"error": {...}})
      - Missing or empty "choices" array (content filter refusals)
      - null content / null message (moderation refusal with no text)
      - message.refusal populated instead of content (newer OpenAI spec)
      - content returned as a list of blocks instead of a bare string
    """
    data = await chat_completion(
        model, messages, temperature=temperature,
        max_tokens=max_tokens, reasoning=reasoning, top_k=top_k,
        channel_key=channel_key, provider_lock=provider_lock,
        debug_enabled=debug_enabled,
    )

    # --- Error envelopes (either ours or upstream's) --------------------
    if data.get("error"):
        status, detail = _extract_error_detail(data)
        prefix = f"Error {status}" if status else "Error"
        print(f"⚠️ [provider] {prefix}: {detail[:200]}")
        return f"*{prefix}: {detail}*"

    # --- Guard the choices/message/content chain ------------------------
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        # Some Qwen/moderation paths return 200 with no choices at all,
        # or with an empty list. Surface whatever hint we can find.
        finish = None
        if isinstance(choices, list) and choices:
            finish = choices[0].get("finish_reason")
        hint = f" (finish_reason={finish})" if finish else ""
        print(f"⚠️ [provider] upstream returned no choices{hint}")
        return (f"*Error: upstream returned no choices{hint} — likely a "
                f"moderation block, rate limit, or empty reply. "
                f"Try rephrasing or switching models.*")

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    refusal = message.get("refusal")
    finish_reason = first.get("finish_reason")

    # content=null: moderation refusal, stop-token-only, etc.
    if content is None:
        if refusal:
            print(f"⚠️ [provider] model refused: {str(refusal)[:200]}")
            return f"*Model refused: {str(refusal)[:300]}*"
        hint = f" (finish_reason={finish_reason})" if finish_reason else ""
        print(f"⚠️ [provider] model returned no content{hint}")
        return (f"*Error: model returned no content{hint} — likely a "
                f"content filter or empty response.*")

    # content is a list of blocks (some models do this even on text calls).
    if isinstance(content, list):
        text_parts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "\n".join(p for p in text_parts if p).strip()
        if joined:
            return joined
        return ("*Error: model returned content blocks with no text. "
                "The model may have emitted only non-text output.*")

    # Normal happy path.
    if isinstance(content, str):
        return content

    # Anything else (int, dict, etc.) — preserve the debug info.
    return f"*Error: unexpected content type {type(content).__name__}*"


async def chat_completion_text_with_thinking(model, messages, temperature=None,
                                              max_tokens=0, reasoning=None, top_k=None,
                                              channel_key=None, provider_lock=None,
                                              debug_enabled=False):
    """Like chat_completion_text but returns (text, thinking_text_or_None).

    Extracts reasoning/thinking content from the response where available.
    OpenRouter returns it as message.reasoning_content (Anthropic-style) or
    as 'thinking' type blocks inside message.content. Returns (text, None)
    if no thinking content is found.
    """
    data = await chat_completion(
        model, messages, temperature=temperature,
        max_tokens=max_tokens, reasoning=reasoning, top_k=top_k,
        channel_key=channel_key, provider_lock=provider_lock,
        debug_enabled=debug_enabled,
    )

    if data.get("error"):
        status, detail = _extract_error_detail(data)
        prefix = f"Error {status}" if status else "Error"
        return f"*{prefix}: {detail}*", None

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        finish = None
        if isinstance(choices, list) and choices:
            finish = choices[0].get("finish_reason")
        hint = f" (finish_reason={finish})" if finish else ""
        return (f"*Error: upstream returned no choices{hint} — likely a "
                f"moderation block, rate limit, or empty reply. "
                f"Try rephrasing or switching models.*"), None

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    refusal = message.get("refusal")
    finish_reason = first.get("finish_reason")

    # Debug: log thinking-related fields in the response
    thinking_keys = [k for k in message if "reason" in k.lower() or "thinking" in k.lower()]
    if thinking_keys:
        print(f"🧠 thinking fields present: {thinking_keys}")
    if isinstance(content, list):
        block_types = [b.get("type", "?") for b in content if isinstance(b, dict)]
        if any(t in ("thinking", "reasoning") for t in block_types):
            print(f"🧠 thinking content block types: {block_types}")

    # Extract thinking content — check common locations in the response
    thinking_text = None
    # OpenRouter standard: message.reasoning (string)
    r = message.get("reasoning")
    if isinstance(r, str) and r.strip():
        thinking_text = r
    # Anthropic direct: message.reasoning_content (string)
    if thinking_text is None:
        rc = message.get("reasoning_content")
        if isinstance(rc, str) and rc.strip():
            thinking_text = rc
    # Block-style: 'thinking' type blocks inside content list
    if thinking_text is None and isinstance(content, list):
        thinking_parts = [
            b.get("thinking", "") for b in content
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        joined_thinking = "\n".join(p for p in thinking_parts if p).strip()
        if joined_thinking:
            thinking_text = joined_thinking
    # reasoning_details array (structured reasoning from some models)
    if thinking_text is None:
        rd = message.get("reasoning_details")
        if isinstance(rd, list):
            text_parts = []
            for item in rd:
                if isinstance(item, dict):
                    t = item.get("text", "")
                    if t:
                        text_parts.append(t)
            joined_rd = "\n".join(text_parts).strip()
            if joined_rd:
                thinking_text = joined_rd

    if content is None:
        if refusal:
            return f"*Model refused: {str(refusal)[:300]}*", None
        hint = f" (finish_reason={finish_reason})" if finish_reason else ""
        return (f"*Error: model returned no content{hint} — likely a "
                f"content filter or empty response.*"), None

    if isinstance(content, list):
        text_parts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "\n".join(p for p in text_parts if p).strip()
        if joined:
            return joined, thinking_text
        return ("*Error: model returned content blocks with no text. "
                "The model may have emitted only non-text output.*"), None

    if isinstance(content, str):
        return content, thinking_text

    return f"*Error: unexpected content type {type(content).__name__}*", None


# ── Image Generation ─────────────────────────────────────────────────────────

# Default size sent to nanoGPT's /images/generations endpoint. OpenRouter's
# /images endpoint uses provider defaults and doesn't take this parameter.
DEFAULT_IMAGE_SIZE = "1024x1024"


async def generate_image(prompt, model, reference_images=None, provider_lock=None):
    """
    Generate an image via the active provider and return a normalized dict:

        {"type": "base64", "data": "<b64 PNG>"}
        {"type": "url",    "url":  "<hosted URL>"}
        {"type": "error",  "text": "<error message>"}

    Callers (like !image and the @@createimage directive) only need to
    handle these three shapes; provider-specific response parsing stays here.

    reference_images: optional list of base64 data URI strings to send as
    input images (image-to-image / reference-image workflows).
    """
    prov = get_provider()
    if prov == "nanogpt":
        return await _generate_image_nanogpt(prompt, model, reference_images=reference_images, provider_lock=provider_lock)
    return await _generate_image_openrouter(prompt, model, reference_images=reference_images, provider_lock=provider_lock)


async def _generate_image_nanogpt(prompt, model, reference_images=None, provider_lock=None):
    """Call nanoGPT's OpenAI-style /v1/images/generations endpoint."""
    payload = {
        "model": normalize_model_id(model),
        "prompt": prompt,
        "n": 1,
        "size": DEFAULT_IMAGE_SIZE,
        "response_format": "b64_json",
    }
    if provider_lock:
        payload["provider"] = {"order": [provider_lock]}
    if reference_images:
        if len(reference_images) == 1:
            payload["imageDataUrl"] = reference_images[0]
        else:
            payload["imageDataUrls"] = reference_images
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{_base_url()}/images/generations",
                headers=_headers(),
                json=payload,
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    return {"type": "error", "text": f"*Error {response.status}: {body}*"}
                data = await response.json()
    except Exception as e:
        return {"type": "error", "text": f"*Image request failed: {e}*"}

    items = data.get("data") or []
    if not items:
        return {"type": "error", "text": "*Image model returned no image data.*"}
    first = items[0]
    if first.get("b64_json"):
        return {"type": "base64", "data": first["b64_json"]}
    if first.get("url"):
        return {"type": "url", "url": first["url"]}
    return {"type": "error", "text": "*Image response had no b64_json or url field.*"}


async def _generate_image_openrouter(prompt, model, reference_images=None, provider_lock=None):
    """
    OpenRouter's dedicated Image API: POST /api/v1/images with a model and
    prompt. Reference images (image-to-image) go in input_references as
    image_url blocks — HTTP(S) or base64 data URLs. Generated images come
    back as base64 in data[].b64_json.

    This endpoint serves ALL OpenRouter image models, including dedicated
    generation models (gpt-image family, seedream, flux, grok-imagine, ...)
    that reject the chat/completions endpoint outright with a 404.
    """
    payload = {
        "model": normalize_model_id(model),
        "prompt": prompt,
    }
    if provider_lock:
        payload["provider"] = {"order": [provider_lock]}
    if reference_images:
        payload["input_references"] = [
            {"type": "image_url", "image_url": {"url": data_uri}}
            for data_uri in reference_images
        ]

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{_base_url()}/images",
                headers=_headers(),
                json=payload,
            ) as response:
                if response.status != 200:
                    body_text = await response.text()
                    detail = body_text[:500]
                    try:
                        _, msg = _extract_error_detail(json.loads(body_text))
                        if msg:
                            detail = msg
                    except (json.JSONDecodeError, ValueError):
                        pass
                    return {"type": "error", "text": f"*Error {response.status}: {detail}*"}
                data = await response.json()
    except asyncio.TimeoutError:
        return {"type": "error",
                "text": f"*Image request timed out after {REQUEST_TIMEOUT.total:.0f}s*"}
    except Exception as e:
        return {"type": "error", "text": f"*Image request failed: {e}*"}

    items = data.get("data") or []
    if not items:
        # 200 with no data — check for an upstream error envelope.
        if data.get("error"):
            status, detail = _extract_error_detail(data)
            prefix = f"Error {status}" if status else "Error"
            return {"type": "error", "text": f"*{prefix}: {detail}*"}
        return {"type": "error", "text": "*Image model returned no image data.*"}
    first = items[0] if isinstance(items[0], dict) else {}
    if first.get("b64_json"):
        return {"type": "base64", "data": first["b64_json"]}
    if first.get("url"):
        return {"type": "url", "url": first["url"]}
    return {"type": "error", "text": "*Image response had no b64_json or url field.*"}


# ── Video Generation ─────────────────────────────────────────────────────────

# Polling config for nanoGPT's async video API (POST /api/generate-video
# returns a runId; poll GET /api/video/status until COMPLETED).
VIDEO_POLL_INTERVAL = 5          # seconds between polls
VIDEO_POLL_MAX_ATTEMPTS = 240   # ~20 minutes max


async def generate_video(prompt, model, reference_images=None, reference_videos=None,
                          seconds=None, provider_lock=None):
    """
    Generate a video via the active provider and return a normalized dict:

        {"type": "base64", "data": "<b64>"}
        {"type": "url",    "url":  "<hosted URL>"}
        {"type": "error",  "text": "<error message>"}

    reference_images: optional list of base64 data URI strings (image-to-video).
    reference_videos: optional list of HTTPS URLs to source videos (video-to-video).
    seconds: optional duration string (e.g. "5", "10") — passed through uncapped.
    """
    prov = get_provider()
    if prov == "nanogpt":
        return await _generate_video_nanogpt(
            prompt, model, reference_images=reference_images,
            reference_videos=reference_videos, seconds=seconds,
            provider_lock=provider_lock,
        )
    return await _generate_video_openrouter(
        prompt, model, reference_images=reference_images,
        reference_videos=reference_videos, seconds=seconds,
        provider_lock=provider_lock,
    )


async def _generate_video_openrouter(prompt, model, reference_images=None,
                                      reference_videos=None, seconds=None,
                                      provider_lock=None):
    """
    OpenRouter exposes video generation through a dedicated async API:
      POST /api/v1/videos          → submit (returns job id, 202)
      GET  /api/v1/videos/{id}      → poll status until completed/failed
      GET  /api/v1/videos/{id}/content → download the video bytes
    """
    payload = {
        "model": normalize_model_id(model),
        "prompt": prompt,
    }
    if seconds:
        payload["duration"] = int(seconds)
    if provider_lock:
        payload["provider"] = {"order": [provider_lock]}
    if reference_images:
        payload["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": data_uri},
                "frame_type": "first_frame",
            }
            for data_uri in reference_images
        ]

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{_base_url()}/videos",
                headers=_headers(),
                json=payload,
            ) as response:
                if response.status not in (200, 202):
                    body = (await response.text())[:300]
                    return {"type": "error",
                            "text": f"*Video request failed ({response.status}): {body}*"}
                data = await response.json()
    except Exception as e:
        return {"type": "error", "text": f"*Video request failed: {e}*"}

    job_id = data.get("id") or data.get("generation_id")
    if not job_id:
        return {"type": "error",
                "text": f"*Video API returned no job id: {str(data)[:200]}*"}

    print(f"🎬 video generation started: job={job_id} model={model}")
    return await _poll_openrouter_video(job_id)


async def _poll_openrouter_video(job_id):
    """Poll OpenRouter's video status endpoint until completed/failed/timeout."""
    for attempt in range(1, VIDEO_POLL_MAX_ATTEMPTS + 1):
        await asyncio.sleep(VIDEO_POLL_INTERVAL)
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"{_base_url()}/videos/{job_id}",
                    headers=_headers(),
                ) as response:
                    if response.status != 200:
                        body = (await response.text())[:200]
                        if attempt % 12 == 0:
                            print(f"🎬 video poll {attempt}: status {response.status} — {body}")
                        continue
                    data = await response.json()
        except Exception as e:
            print(f"⚠️ video poll {attempt} error: {e}")
            continue

        status = (data.get("status") or "").lower()

        if status == "completed":
            # Download the video content
            content_url = data.get("unsigned_urls")
            if content_url and isinstance(content_url, list) and content_url[0]:
                print(f"🎬 video ready: {content_url[0]}")
                return {"type": "url", "url": content_url[0]}
            # Fall back to the content endpoint
            try:
                async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                    async with session.get(
                        f"{_base_url()}/videos/{job_id}/content",
                        headers=_headers(),
                    ) as resp:
                        if resp.status == 200:
                            video_bytes = await resp.read()
                            b64 = base64.b64encode(video_bytes).decode("ascii")
                            print(f"🎬 video downloaded: {len(video_bytes)} bytes")
                            return {"type": "base64", "data": b64}
                        body = (await resp.text())[:200]
                        return {"type": "error",
                                "text": f"*Video completed but content download failed ({resp.status}): {body}*"}
            except Exception as e:
                return {"type": "error",
                        "text": f"*Video completed but content download failed: {e}*"}
            return {"type": "error",
                    "text": f"*Video completed but no download URL: {str(data)[:200]}*"}

        if status in ("failed", "cancelled", "expired"):
            err = data.get("error") or f"Video generation {status}"
            return {"type": "error", "text": f"*{err}*"}

        if attempt % 12 == 0:
            print(f"🎬 video poll {attempt}: {status}")

    return {"type": "error",
            "text": f"*Video generation timed out after {VIDEO_POLL_MAX_ATTEMPTS * VIDEO_POLL_INTERVAL}s.*"}


async def _generate_video_nanogpt(prompt, model, reference_images=None,
                                   reference_videos=None, seconds=None,
                                   provider_lock=None):
    """Call nanoGPT's async video API: POST /api/generate-video, then poll."""
    payload = {
        "model": normalize_model_id(model),
        "prompt": prompt,
    }
    if seconds:
        payload["duration"] = str(seconds)
    if provider_lock:
        payload["provider"] = {"order": [provider_lock]}
    if reference_images:
        if len(reference_images) == 1:
            payload["imageDataUrl"] = reference_images[0]
        else:
            payload["referenceImages"] = reference_images
    if reference_videos:
        # Pass the Discord CDN URLs directly (avoids the 4 MB base64 cap)
        if len(reference_videos) == 1:
            payload["videoUrl"] = reference_videos[0]
        else:
            payload["referenceVideos"] = reference_videos

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                "https://nano-gpt.com/api/generate-video",
                headers=_headers(),
                json=payload,
            ) as response:
                if response.status not in (200, 202):
                    body = (await response.text())[:300]
                    return {"type": "error",
                            "text": f"*Video request failed ({response.status}): {body}*"}
                data = await response.json()
    except Exception as e:
        return {"type": "error", "text": f"*Video request failed: {e}*"}

    run_id = data.get("runId") or data.get("id")
    if not run_id:
        return {"type": "error",
                "text": f"*Video API returned no runId: {str(data)[:200]}*"}

    print(f"🎬 video generation started: runId={run_id} model={model}")
    return await _poll_video_status(run_id)


async def _poll_video_status(run_id):
    """Poll nanoGPT's video status endpoint until COMPLETED/FAILED/timeout."""
    for attempt in range(1, VIDEO_POLL_MAX_ATTEMPTS + 1):
        await asyncio.sleep(VIDEO_POLL_INTERVAL)
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"https://nano-gpt.com/api/video/status?requestId={run_id}",
                    headers=_headers(),
                ) as response:
                    if response.status != 200:
                        body = (await response.text())[:200]
                        if attempt % 12 == 0:  # log every ~minute
                            print(f"🎬 video poll {attempt}: status {response.status} — {body}")
                        continue
                    data = await response.json()
        except Exception as e:
            print(f"⚠️ video poll {attempt} error: {e}")
            continue

        inner = data.get("data") or {}
        status = inner.get("status", "").upper()

        if status == "COMPLETED":
            output = inner.get("output") or {}
            video_url = (output.get("video") or {}).get("url")
            if not video_url:
                return {"type": "error",
                        "text": f"*Video completed but no URL returned: {str(data)[:200]}*"}
            print(f"🎬 video ready: {video_url}")
            return {"type": "url", "url": video_url}

        if status in ("FAILED", "CANCELED"):
            err = inner.get("userFriendlyError") or inner.get("error") or "Video generation failed"
            return {"type": "error", "text": f"*{err}*"}

        if attempt % 12 == 0:
            print(f"🎬 video poll {attempt}: {status}")

    return {"type": "error",
            "text": f"*Video generation timed out after {VIDEO_POLL_MAX_ATTEMPTS * VIDEO_POLL_INTERVAL}s.*"}


# ── Credits / Balance ────────────────────────────────────────────────────────

async def get_credits():
    """
    Fetch account balance from the active provider.

    Returns a dict:
        {
            "remaining": float,
            "used": float,
            "total": float,
            "label": str,          # e.g. "OpenRouter" or "nanoGPT"
            "error": str or None,
        }
    """
    provider = get_provider()
    result = {"remaining": 0, "used": 0, "total": 0, "label": provider_name(), "error": None}

    def _safe_float(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    try:
        if provider == "nanogpt":
            # nanoGPT's check-balance endpoint: POST, x-api-key header,
            # no /v1 prefix, returns usd_balance as a string.
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.post(
                    "https://nano-gpt.com/api/check-balance",
                    headers={"x-api-key": _api_key(), **HTTP_PIN},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        balance = _safe_float(data.get("usd_balance"))
                        result["remaining"] = balance
                        result["total"] = balance  # pay-as-you-go, no "spent" tracking
                    else:
                        body = (await resp.text())[:120]
                        result["error"] = f"error {resp.status} — {body}"
        else:
            # OpenRouter
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(
                    f"{_base_url()}/credits",
                    headers=_auth_headers(),
                ) as resp:
                    if resp.status == 200:
                        data = (await resp.json()).get("data", {}) or {}
                        total = _safe_float(data.get("total_credits"))
                        used = _safe_float(data.get("total_usage"))
                        result["total"] = total
                        result["used"] = used
                        result["remaining"] = total - used
                    else:
                        body = (await resp.text())[:120]
                        result["error"] = f"error {resp.status} — {body}"
    except asyncio.TimeoutError:
        result["error"] = "request timed out"
    except _ContentEncodingError as e:
        result["error"] = (
            f"couldn't decode the server's compressed response ({e}) — "
            f"this machine likely has a broken brotli/zstd module "
            f"(reinstall Brotli or remove brotli/brotlicffi)"
        )
    except Exception as e:
        result["error"] = f"request failed — {e}"

    return result


# ── Model listing ────────────────────────────────────────────────────────────

def _normalize_model_entry(m):
    """Ensure a model dict has a 'context_length' key (int or None)."""
    entry = dict(m)  # shallow copy
    if "context_length" not in entry or entry["context_length"] is None:
        entry["context_length"] = (
            entry.get("context_window")
            or entry.get("max_context")
            or entry.get("max_tokens")
        )
    return entry


async def _fetch_model_endpoint(url):
    """GET a model-listing endpoint and return the raw 'data' list, or []."""
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(url, headers=_auth_headers()) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as e:
                        print(f"[provider] Non-JSON response from {url}: {e}")
                        return []
                    raw = data.get("data", []) if isinstance(data, dict) else []
                    return raw if isinstance(raw, list) else []
                print(f"[provider] Could not fetch {url} (HTTP {resp.status})")
                return []
    except asyncio.TimeoutError:
        print(f"[provider] Model list request to {url} timed out")
        return []
    except _ContentEncodingError as e:
        print(f"[provider] Model list request to {url} hit an undecodable "
              f"compressed response — likely a broken brotli/zstd module on "
              f"this machine: {e}")
        return []
    except Exception as e:
        print(f"[provider] Model list request to {url} failed: {e}")
        return []


async def get_models():
    """
    Fetch the list of available models from the active provider.

    Returns a list of dicts, each guaranteed to have at least:
        {"id": str, "context_length": int or None}

    For nanoGPT, this merges BOTH the chat-model list (/v1/models) and the
    image-model list (/v1/image-models), since image models live on a
    separate endpoint there. For OpenRouter, this merges the general
    catalog (/models) with the dedicated image (/images/models) and video
    (/videos/models) catalogs, since dedicated generation models are
    absent from the general catalog.

    This lets !diag validate configured image/video models without
    special casing.

    Returns an empty list on error (caller should check).
    """
    provider = get_provider()

    # Primary model list (chat/text models).
    url = f"{_base_url()}/models"
    if provider == "nanogpt":
        url += "?detailed=true"
    raw_models = await _fetch_model_endpoint(url)
    models = [_normalize_model_entry(m) for m in raw_models]

    # nanoGPT: also fetch and merge image models.
    if provider == "nanogpt":
        image_raw = await _fetch_model_endpoint(f"{_base_url()}/image-models")
        models.extend(_normalize_model_entry(m) for m in image_raw)
        # Also merge video models so !diag can validate video model ids.
        video_raw = await _fetch_model_endpoint(f"{_base_url()}/video-models")
        models.extend(_normalize_model_entry(m) for m in video_raw)

    # OpenRouter: merge image models from the dedicated Image API and video
    # models from the dedicated video API. Dedicated image-generation models
    # (gpt-image family, seedream, flux, grok-imagine, ...) are absent from
    # the general /models catalog, so without this merge !diag would report
    # configured image models as "not found".
    if provider == "openrouter":
        try:
            image_raw = await _fetch_model_endpoint(f"{_base_url()}/images/models")
            models.extend(_normalize_model_entry(m) for m in image_raw)
        except Exception:
            pass  # image models endpoint may not be available on all setups
        try:
            video_raw = await _fetch_model_endpoint(f"{_base_url()}/videos/models")
            models.extend(_normalize_model_entry(m) for m in video_raw)
        except Exception:
            pass  # video models endpoint may not be available on all setups

    # Dedupe by id — image models like the gemini-* family appear in both
    # the general catalog and /images/models.
    seen = set()
    deduped = []
    for m in models:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        deduped.append(m)

    return deduped


async def get_model_context_length(model_id):
    """
    Look up the context window size for a specific model.

    Returns an int, or None if the model wasn't found or had no context info.
    """
    model_id = normalize_model_id(model_id)
    models = await get_models()
    for m in models:
        if m["id"] == model_id:
            ctx = m.get("context_length")
            return int(ctx) if ctx else None
    return None
