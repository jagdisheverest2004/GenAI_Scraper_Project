"""Llama 3 client helpers for structured JSON extraction via local Ollama."""

from __future__ import annotations

import json
from typing import Any

import ollama

from ai.prompt_builder import build_extraction_prompt


def _chunk_items(items: list[Any], chunk_size: int) -> list[list[Any]]:
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def _parse_json_content(response: dict[str, Any]) -> Any:
    response_text = response.get("message", {}).get("content") or "{}"
    return json.loads(response_text)


def _unique_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        normalized = str(url).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _select_urls_from_chunk(sitemap_chunk: list[dict[str, Any]], user_query: str) -> list[str]:
    prompt = (
        f"Based on the user query '{user_query}', identify the top 3 most relevant URLs from this list "
        "that likely contain the requested data. Return ONLY a valid JSON list of URLs.\n\n"
        f"Sitemap chunk:\n{json.dumps(sitemap_chunk, ensure_ascii=False)}"
    )

    response = ollama.chat(
        model="llama3.1:8b",
        messages=[{"role": "user", "content": prompt}],
        format="json",
        options={"temperature": 0.1},
    )

    parsed = _parse_json_content(response)
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    if isinstance(parsed, dict):
        candidate = parsed.get("urls") or parsed.get("selected_urls") or []
        if isinstance(candidate, list):
            return [str(item) for item in candidate if str(item).strip()]
    return []


def select_relevant_urls(sitemap: list[dict[str, Any]], user_query: str) -> list[str]:
    """Use local Llama 3 to rank sitemap paths and return the most relevant URLs."""

    print("[llama_client] select_relevant_urls called")
    if not sitemap:
        print("[llama_client] Empty sitemap received; returning no URLs")
        return []

    chunk_size = 50
    sitemap_chunks = _chunk_items(sitemap, chunk_size)
    print(f"[llama_client] Prepared sitemap chunks: chunk_count={len(sitemap_chunks)}, chunk_size={chunk_size}")

    ranked_candidates: list[str] = []

    try:
        for index, chunk in enumerate(sitemap_chunks, start=1):
            print(f"[llama_client] Routing chunk {index}/{len(sitemap_chunks)}: chunk_len={len(chunk)}")
            try:
                chunk_urls = _select_urls_from_chunk(chunk, user_query)
                ranked_candidates.extend(chunk_urls)
                print(
                    f"[llama_client] Chunk {index} routed: urls_added={len(chunk_urls)}, "
                    f"urls_total={len(ranked_candidates)}"
                )
            except Exception as chunk_exc:
                print(f"[llama_client] Routing chunk {index} failed: {type(chunk_exc).__name__}: {chunk_exc}")
                continue

        ranked_candidates = _unique_urls(ranked_candidates)
        if not ranked_candidates:
            print("[llama_client] No candidate URLs found after chunk routing")
            return []

        if len(ranked_candidates) > 3:
            print("[llama_client] Running final ranking pass on merged candidate list")
            final_chunk = [{"url": url, "text": "", "context": ""} for url in ranked_candidates]
            ranked_candidates = _select_urls_from_chunk(final_chunk, user_query)
            ranked_candidates = _unique_urls(ranked_candidates)

        print(f"[llama_client] Final selected URLs count={len(ranked_candidates)}")
        return ranked_candidates[:3]

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"[llama_client] URL selection failed: {error_msg}")
        raise RuntimeError(
            f"Local Llama 3 URL routing failed. Is Ollama running? Error: {error_msg}"
        )

def extract_structured_json(page_markdown: str, extraction_goal: str) -> dict[str, Any]:
    """Process markdown in chunks and merge extracted records from local Llama 3."""

    print("[llama_client] extract_structured_json called")
    chunk_size = 3000
    chunks = [page_markdown[i : i + chunk_size] for i in range(0, len(page_markdown), chunk_size)]
    print(f"[llama_client] Prepared chunks: chunk_count={len(chunks)}, chunk_size={chunk_size}")

    all_records: list[Any] = []
    resolved_logic_metadata: dict[str, Any] | None = None

    try:
        for index, chunk in enumerate(chunks, start=1):
            print(f"[llama_client] Processing chunk {index}/{len(chunks)}: chunk_len={len(chunk)}")
            prompt = build_extraction_prompt(extraction_goal, chunk)

            try:
                response = ollama.chat(
                    model="llama3.1:8b",
                    messages=[
                        {"role": "user", "content": prompt}
                    ],
                    format="json",
                    options={
                        "temperature": 0.1
                    },
                )

                parsed = _parse_json_content(response)
                if not isinstance(parsed, dict):
                    raise ValueError("Chunk response JSON is not an object")

                chunk_records = parsed.get("records", [])
                if isinstance(chunk_records, list):
                    all_records.extend(chunk_records)
                    print(
                        f"[llama_client] Chunk {index} merged: records_added={len(chunk_records)}, "
                        f"records_total={len(all_records)}"
                    )
                else:
                    print(f"[llama_client] Chunk {index} skipped: 'records' is not a list")

                chunk_logic_metadata = parsed.get("logic_metadata")
                if resolved_logic_metadata is None and isinstance(chunk_logic_metadata, dict):
                    resolved_logic_metadata = chunk_logic_metadata
                    print(f"[llama_client] Chunk {index} logic metadata captured")

            except Exception as chunk_exc:
                print(f"[llama_client] Chunk {index} failed: {type(chunk_exc).__name__}: {chunk_exc}")
                continue

        print(f"[llama_client] Chunk processing complete: records_total={len(all_records)}")
        return {"success": True, "records": all_records, "logic_metadata": resolved_logic_metadata or {}}

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"[llama_client] Extraction failed: {error_msg}")
        raise RuntimeError(
            f"Local Llama 3 processing failed. Is Ollama running? Error: {error_msg}"
        )