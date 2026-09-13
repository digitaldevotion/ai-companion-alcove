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
import json

import aiohttp
import config
from .utils import update_token_calibration


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
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://discord.com"
        headers["X-Title"] = "Companion Bot"
    return headers


def _auth_headers():
    """Minimal auth-only headers (for GET endpoints like /credits, /models)."""
    return {"Authorization": f"Bearer {_api_key()}"}


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
                          channel_key=None, provider_lock=None):
    """
    Send a chat completion request and return the raw JSON response dict.

    Callers are responsible for parsing the response (extracting text,
    thinking content, etc.) since the shape is mostly provider-agnostic
    (OpenAI compatible).
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
        if prov == "openrouter":
            payload["include_reasoning"] = True

    is_anthropic = "anthropic/" in model.lower() or model.lower().startswith("claude-")

    if prov == "nanogpt":
        if is_anthropic:
            payload["promptCaching"] = {"enabled": True, "ttl": "5m", "explicitCacheControl": True}
        else:
            payload["caching"] = True

    if prov == "openrouter" and channel_key:
        payload["session_id"] = channel_key

    if provider_lock:
        payload["provider"] = {"order": [provider_lock]}

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
                    return {"error": True, "status": response.status,
                            "detail": body_text[:500] or response.reason or "no body"}
                try:
                    res_data = json.loads(body_text)
                    usage = res_data.get("usage")
                    if usage and isinstance(usage, dict) and channel_key:
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        if prompt_tokens and prompt_tokens > 0:
                            if not _has_multimodal_content(messages):
                                msg_bytes = sum(
                                    len(json.dumps(m).encode("utf-8"))
                                    for m in _sanitize_messages(messages, model, prov)
                                )
                                update_token_calibration(channel_key, msg_bytes, prompt_tokens)
                    return res_data
                except (json.JSONDecodeError, ValueError) as e:
                    return {"error": True, "status": response.status,
                            "detail": f"invalid JSON from upstream ({e}); body: {body_text[:300]}"}
    except asyncio.TimeoutError:
        return {"error": True, "status": 0,
                "detail": f"request timed out after {REQUEST_TIMEOUT.total:.0f}s"}
    except aiohttp.ClientError as e:
        return {"error": True, "status": 0, "detail": f"network error: {e}"}
    except Exception as e:
        # Last-ditch catch-all so a provider bug never crashes on_message.
        return {"error": True, "status": 0, "detail": f"unexpected error: {e}"}


# ── Convenience wrappers ─────────────────────────────────────────────────────

async def chat_completion_text(model, messages, temperature=None,
                                max_tokens=0, reasoning=None, top_k=None,
                                channel_key=None, provider_lock=None):
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
    )

    # --- Error envelopes (either ours or upstream's) --------------------
    if data.get("error"):
        status, detail = _extract_error_detail(data)
        prefix = f"Error {status}" if status else "Error"
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
            return f"*Model refused: {str(refusal)[:300]}*"
        hint = f" (finish_reason={finish_reason})" if finish_reason else ""
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
                                              channel_key=None, provider_lock=None):
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
                    headers={"x-api-key": _api_key()},
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
    catalog (/models) with the dedicated image catalog (/images/models),
    since dedicated generation models (gpt-image family, seedream, flux,
    grok-imagine, ...) are absent from the general catalog.

    This lets !diag validate configured image models without special casing.

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

    # OpenRouter: merge image models from the dedicated Image API. Dedicated
    # image-generation models are absent from the general /models catalog,
    # so without this merge !diag would report configured image models as
    # "not found".
    if provider == "openrouter":
        try:
            image_raw = await _fetch_model_endpoint(f"{_base_url()}/images/models")
            models.extend(_normalize_model_entry(m) for m in image_raw)
        except Exception:
            pass  # image models endpoint may not be available on all setups

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
