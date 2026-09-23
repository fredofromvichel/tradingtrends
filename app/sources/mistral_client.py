"""Zusammenfassung ueber die Mistral-API (Chat Completions, JSON-Modus).

Der Key kommt aus der .env (MISTRAL_API_KEY), das Modell aus config.yaml oder
MISTRAL_MODEL.
"""

from __future__ import annotations

import json

import httpx

API_URL = "https://api.mistral.ai/v1/chat/completions"
TIMEOUT_SECONDS = 90.0


class MistralError(RuntimeError):
    pass


def complete_json(
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
) -> dict:
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if client is not None:
            response = client.post(API_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
        else:
            response = httpx.post(API_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise MistralError(f"Mistral nicht erreichbar: {exc}") from exc

    if response.status_code in (401, 403):
        raise MistralError("Mistral lehnt den Key ab – MISTRAL_API_KEY in der .env prüfen.")
    if response.status_code == 429:
        raise MistralError("Mistral: Kontingent oder Anfragegrenze erreicht, später erneut versuchen.")
    if response.status_code >= 400:
        raise MistralError(f"Mistral antwortet mit {response.status_code}.")
    try:
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise MistralError("Mistral lieferte kein gültiges JSON.") from exc
