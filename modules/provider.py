# ============================================
# Alcove v1.3.0 — provider.py
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
import re

import aiohttp
import config


# Generous ceiling — reasoning models (adaptive thinking, long tool rounds)
# can legitimately take a while. We'd rather wait than kill a live request.
# Bumped past discord.py's internal typing refresh so a stuck upstream still
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

def _api_key():
    """Return the API key for the active provider."""
    provider = getattr(config, "PROVIDER", "openrouter").lower()
    if provider == "nanogpt":
        return getattr(config, "NANOGPT_KEY", "")
    return config.OPENROUTER_KEY


def _base_url():
    """Return the base API URL for the active provider."""
    provider = getattr(config, "PROVIDER", "openrouter").lower()
    if provider == "nanogpt":
        return "https://nano-gpt.com/api/v1"
    return "https://openrouter.ai/api/v1"


def _headers():
    """Return the common HTTP headers for the active provider."""
    provider = getattr(config, "PROVIDER", "openrouter").lower()
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
    provider = getattr(config, "PROVIDER", "openrouter").lower()
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
    prov = getattr(config, "PROVIDER", "openrouter").lower()
    if prov != "nanogpt":
        return model_id
    if model_id.lower().startswith("nano-gpt/"):
        return model_id[len("nano-gpt/"):]
    return model_id


# ── Chat Completions ─────────────────────────────────────────────────────────

async def chat_completion(model, messages, temperature=None, max_tokens=0,
                          reasoning=None, modalities=None):
    """
    Send a chat completion request and return the raw JSON response dict.

    Callers are responsible for parsing the response (extracting text,
    images, etc.) since the shape is mostly provider-agnostic (OpenAI
    compatible) but image handling varies by model.
    """
    if temperature is None:
        temperature = config.TEMPERATURE

    payload = {
        "model": normalize_model_id(model),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens
    if reasoning and reasoning.lower() != "off":
        payload["reasoning"] = {"effort": reasoning.lower()}
    if modalities:
        payload["modalities"] = modalities

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
                    return json.loads(body_text)
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
                               max_tokens=0, reasoning=None):
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
        max_tokens=max_tokens, reasoning=reasoning,
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


# ── Image Generation ─────────────────────────────────────────────────────────

# Default size sent to nanoGPT's /images/generations endpoint. OpenRouter's
# chat-completions-based image generation doesn't use this parameter.
DEFAULT_IMAGE_SIZE = "1024x1024"


async def generate_image(prompt, model, reference_images=None):
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
    prov = getattr(config, "PROVIDER", "openrouter").lower()
    if prov == "nanogpt":
        return await _generate_image_nanogpt(prompt, model, reference_images=reference_images)
    return await _generate_image_openrouter(prompt, model, reference_images=reference_images)


async def _generate_image_nanogpt(prompt, model, reference_images=None):
    """Call nanoGPT's OpenAI-style /v1/images/generations endpoint."""
    payload = {
        "model": normalize_model_id(model),
        "prompt": prompt,
        "n": 1,
        "size": DEFAULT_IMAGE_SIZE,
        "response_format": "b64_json",
    }
    if reference_images:
        if len(reference_images) == 1:
            payload["imageDataUrl"] = reference_images[0]
        else:
            payload["imageDataUrls"] = reference_images
    try:
        async with aiohttp.ClientSession() as session:
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


def _data_uri_to_base64(url):
    """
    Strip the scheme/mime prefix off a data: URI and return the base64
    payload, or None if the URI is malformed (e.g. missing comma).
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    if "," not in url:
        return None
    return url.split(",", 1)[1]


async def _generate_image_openrouter(prompt, model, reference_images=None):
    """
    OpenRouter tunnels image generation through chat completions with
    modalities=["image"]. Response shape varies by model, so we
    try several formats.
    """
    if reference_images:
        user_content = [{"type": "text", "text": prompt}]
        for data_uri in reference_images:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": data_uri}
            })
        messages = [{"role": "user", "content": user_content}]
    else:
        messages = [{"role": "user", "content": prompt}]

    data = await chat_completion(
        model,
        messages,
        modalities=["image"],
    )

    # --- Error envelopes (ours or upstream) ----------------------------
    if data.get("error"):
        status, detail = _extract_error_detail(data)
        prefix = f"Error {status}" if status else "Error"
        return {"type": "error", "text": f"*{prefix}: {detail}*"}

    # --- Guard the choices/message chain -------------------------------
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return {"type": "error",
                "text": "*Image model returned no choices (possible moderation block or empty response).*"}
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}

    # Check for images array (Gemini via OpenRouter)
    images = msg.get("images") or []
    if images:
        img = images[0] if isinstance(images[0], dict) else {}
        url = (img.get("image_url") or {}).get("url", "") if isinstance(img.get("image_url"), dict) else ""
        b64 = _data_uri_to_base64(url)
        if b64 is not None:
            return {"type": "base64", "data": b64}
        if url:
            return {"type": "url", "url": url}

    content = msg.get("content")

    # content is null — no image found
    if content is None:
        refusal = msg.get("refusal")
        if refusal:
            return {"type": "error", "text": f"*Model refused: {str(refusal)[:300]}*"}
        return {"type": "error",
                "text": "*Image model returned no image data. Check console for full response.*"}

    # Content block format (image_url or inline_data blocks)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url", "") if isinstance(block.get("image_url"), dict) else ""
                b64 = _data_uri_to_base64(url)
                if b64 is not None:
                    return {"type": "base64", "data": b64}
                if url:
                    return {"type": "url", "url": url}
            elif block.get("type") == "inline_data":
                payload = block.get("data")
                if payload:
                    return {"type": "base64", "data": payload}
        return {"type": "error",
                "text": "*Image model returned content blocks but no image was found.*"}
    elif isinstance(content, str):
        # Markdown image syntax ![...](url)
        match = re.search(r'!\[.*?\]\((.*?)\)', content)
        if match:
            url = match.group(1)
            b64 = _data_uri_to_base64(url)
            if b64 is not None:
                return {"type": "base64", "data": b64}
            if url:
                return {"type": "url", "url": url}
        # Bare base64 data URI
        b64 = _data_uri_to_base64(content)
        if b64 is not None:
            return {"type": "base64", "data": b64}
        return {"type": "error", "text": f"*Image model returned text instead of an image: {content[:200]}*"}
    return {"type": "error", "text": f"*Unexpected content type: {type(content).__name__}*"}


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
    provider = getattr(config, "PROVIDER", "openrouter").lower()
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
    separate endpoint there but OpenRouter exposes everything in one list.
    This lets !diag validate configured image models without special casing.

    Returns an empty list on error (caller should check).
    """
    provider = getattr(config, "PROVIDER", "openrouter").lower()

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

    return models


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
