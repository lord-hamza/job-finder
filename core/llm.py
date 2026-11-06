"""Minimal client for the Gemini REST API."""
import base64
import json
import logging
import os
import time

import requests

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-lite-latest"  # fast, cheap and rarely overloaded
FALLBACK_MODEL = "gemini-3.5-flash"
TIMEOUT = (10, 45)  # overloaded models can hang rather than say so
RETRY_PAUSES = (10, 30)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Message is shown to the user."""


class _Transient(LLMError):
    """Worth another try."""


def api_key():
    return os.environ.get("GEMINI_API_KEY", "").strip()


def model():
    return os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def verify_key(key):
    """Raises LLMError if Gemini won't accept this key."""
    try:
        response = requests.get(
            f"{API_ROOT}/models",
            headers={"x-goog-api-key": key},
            params={"pageSize": 1},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LLMError("Couldn't reach Gemini. Check your internet connection.") from exc
    if response.status_code != 200:
        raise _http_error(response)


def generate_json(prompt, schema, system="", pdf=None, patient=True):
    """Returns the dict Gemini produced for the schema. Raises LLMError with a readable message.

    If a model is overloaded it moves straight on to the other one.
    patient: then also try again after a pause. Not for when someone is waiting on the page.
    """
    key = api_key()
    if not key:
        raise LLMError("The Gemini API key isn't set. Add it on the dashboard.")
    parts = [{"text": prompt}]
    if pdf is not None:
        encoded = base64.b64encode(pdf).decode()
        parts.insert(0, {"inlineData": {"mimeType": "application/pdf", "data": encoded}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    problem = None
    for pause in (0, *RETRY_PAUSES) if patient else (0,):
        if pause:
            time.sleep(pause)
        for name in dict.fromkeys((model(), FALLBACK_MODEL)):
            try:
                return _call(f"{API_ROOT}/models/{name}:generateContent", key, payload)
            except _Transient as exc:
                log.warning("Gemini %s didn't answer: %s", name, exc)
                problem = exc
    raise LLMError(str(problem)) from problem


def _call(url, key, payload):
    try:
        response = requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.Timeout as exc:
        raise _Transient("Gemini is overloaded and didn't answer in time. Try again in a minute.") from exc
    except requests.RequestException as exc:
        raise _Transient("Couldn't reach Gemini. Check your internet connection.") from exc
    except UnicodeError as exc:
        raise LLMError("The Gemini API key contains invalid characters.") from exc
    if response.status_code != 200:
        raise _http_error(response)
    try:
        data = response.json()
    except ValueError as exc:
        raise _Transient("Gemini returned an unreadable response.") from exc
    return _parse(data)


def _parse(data):
    if not isinstance(data, dict):
        raise _Transient("Gemini returned an unreadable response.")
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise LLMError(f"Gemini refused this text ({block}).")
    candidates = data.get("candidates") or []
    if not candidates:
        raise _Transient("Gemini returned no answer.")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    finish = candidate.get("finishReason", "STOP")
    if not text.strip():
        if finish in ("STOP", "MAX_TOKENS", "OTHER"):
            raise _Transient(f"Gemini returned an empty answer ({finish}).")
        raise LLMError(f"Gemini refused this text ({finish}).")
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        parsed = json.loads(cleaned)
    except ValueError as exc:
        raise _Transient(f"Gemini's answer wasn't valid JSON ({finish}).") from exc
    if not isinstance(parsed, dict):
        raise _Transient("Gemini's answer wasn't in the expected format.")
    return parsed


def _http_error(response):
    status = response.status_code
    try:
        body = response.json()
        body = body[0] if isinstance(body, list) and body else body
        message = str(body["error"]["message"])[:200]
    except (ValueError, KeyError, TypeError, IndexError):
        message = f"HTTP {status}"
    if status in RETRYABLE_STATUS:
        kind = "quota or rate limit reached" if status == 429 else "temporarily unavailable"
        return _Transient(f"Gemini {kind}: {message}")
    if status == 404:
        return LLMError(f"Gemini can't find that model. Set GEMINI_MODEL in .env. ({message})")
    if status in (401, 403) or "api key" in message.lower():
        return LLMError(f"Gemini rejected the API key: {message}")
    return LLMError(f"Gemini error: {message}")
