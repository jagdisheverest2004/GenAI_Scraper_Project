"""Gemini client helpers for structured JSON extraction."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from google import genai
from google.genai import types

from ai.prompt_builder import build_extraction_prompt

DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


def _resolve_model_candidates() -> list[str]:
    """Return model candidates from env override or default fallback list."""

    raw = os.getenv("GEMINI_MODELS", "").strip()
    if raw:
        models = [item.strip() for item in raw.split(",") if item.strip()]
        if models:
            return models
    return DEFAULT_GEMINI_MODELS


def _is_transient_model_error(error_text: str) -> bool:
    """Detect transient provider errors that are safe to retry/fallback."""

    transient_markers = [
        "503",
        "unavailable",
        "high demand",
        "overloaded",
        "temporarily",
        "timeout",
        "429",
        "rate limit",
    ]
    lowered = error_text.lower()
    return any(marker in lowered for marker in transient_markers)


def extract_structured_json(page_markdown: str, extraction_goal: str) -> dict[str, Any]:
    """Send content to Gemini and enforce JSON-only output."""

    print("[gemini_client] extract_structured_json called")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[gemini_client] GEMINI_API_KEY missing")
        raise ValueError("GEMINI_API_KEY is not set.")
    print("[gemini_client] GEMINI_API_KEY found")

    client = genai.Client(api_key=api_key)
    model_candidates = _resolve_model_candidates()
    print(f"[gemini_client] Gemini client created with model candidates={model_candidates}")
    prompt = build_extraction_prompt(extraction_goal, page_markdown)
    print(f"[gemini_client] Prompt ready: prompt_len={len(prompt)}")

    attempted_models: list[str] = []
    last_error: Exception | None = None

    for model in model_candidates:
        attempted_models.append(model)
        print(f"[gemini_client] Trying model={model}")

        for attempt in range(2):
            try:
                # Gemini is forced to emit JSON by setting the response MIME type explicitly.
                print(f"[gemini_client] Sending request to Gemini (model={model}, attempt={attempt + 1}/2)")
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                print(f"[gemini_client] Response received from Gemini (model={model})")

                response_text = response.text or "{}"
                print(f"[gemini_client] Response text length={len(response_text)}")
                parsed = json.loads(response_text)
                if not isinstance(parsed, dict):
                    print(f"[gemini_client] Invalid JSON type: {type(parsed).__name__}")
                    raise ValueError("Gemini returned JSON that is not an object.")

                print(f"[gemini_client] JSON parsed successfully with model={model}: keys={list(parsed.keys())}")
                return parsed

            except Exception as exc:
                last_error = exc
                error_text = str(exc)
                print(f"[gemini_client] Model={model} failed on attempt={attempt + 1}: {type(exc).__name__}: {error_text}")

                is_last_attempt = attempt == 1
                if _is_transient_model_error(error_text) and not is_last_attempt:
                    print("[gemini_client] Transient error detected; retrying same model after backoff")
                    time.sleep(1.2)
                    continue

                if _is_transient_model_error(error_text):
                    print(f"[gemini_client] Transient issue persists for model={model}; moving to fallback model")
                    break

                print(f"[gemini_client] Non-transient error for model={model}; moving to next fallback model")
                break

    raise RuntimeError(
        "All Gemini models failed. "
        f"Attempted models: {attempted_models}. "
        f"Last error: {last_error}"
    )
