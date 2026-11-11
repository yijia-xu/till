"""Small HTTP client for Gemini's native generateContent API."""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx


class GeminiAPIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def generate_json(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_instruction: str,
    payload: dict,
    timeout: float,
) -> dict:
    if not api_key:
        raise GeminiAPIError("Gemini API key is not configured.")
    endpoint = f"{base_url.rstrip('/')}/{quote(model, safe='')}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    response = None
    try:
        for attempt in range(3):
            response = httpx.post(
                endpoint,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                response.raise_for_status()
                break
            # Gemini can return transient capacity errors. Retry briefly before
            # surfacing the provider status to the user.
            time.sleep(1 + attempt)
    except httpx.HTTPStatusError as exc:
        raise GeminiAPIError(
            f"Gemini API returned HTTP {exc.response.status_code}.",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.TimeoutException as exc:
        raise GeminiAPIError("Gemini API request timed out.") from exc
    except httpx.RequestError as exc:
        raise GeminiAPIError("Till could not reach Gemini API.") from exc

    if response is None:
        raise GeminiAPIError("Gemini API returned no response.")

    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GeminiAPIError("Gemini API returned an unreadable JSON response.") from exc
    if not isinstance(result, dict):
        raise GeminiAPIError("Gemini API response must be a JSON object.")
    return result
