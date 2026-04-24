"""Groq client helpers for structured JSON extraction via Groq Cloud."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from groq import Groq  # type: ignore[import-not-found]

from ai.prompt_builder import build_extraction_prompt, build_navigation_prompt, build_router_prompt


def _get_client() -> Groq:
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set.")
    return Groq(api_key=api_key)


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _parse_json_content(response: Any) -> Any:
    message = response.choices[0].message if getattr(response, "choices", None) else None
    response_text = getattr(message, "content", None) or "{}"
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


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    error_code = getattr(exc, "code", None)
    if error_code == 429:
        return True
    message = f"{type(exc).__name__}: {exc}".lower()
    return "429" in message or "rate limit" in message


def _is_model_decommissioned(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return "model_decommissioned" in message or "decommissioned" in message


def _call_groq_json(prompt: str, model: str) -> Any:
    client = _get_client()

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return _parse_json_content(response)
        except Exception as exc:
            last_error = exc
            if not _is_rate_limit_error(exc) or attempt == 3:
                raise
            sleep_seconds = 0.75 * attempt
            print(
                f"[groq_client] Rate limited on model={model}; retrying in {sleep_seconds:.2f}s "
                f"(attempt {attempt}/3)"
            )
            time.sleep(sleep_seconds)

    raise RuntimeError(f"Groq request failed: {last_error}")


def _coerce_url_list(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(item).strip() for item in payload if str(item).strip()]

    if isinstance(payload, dict):
        for key in ("selected_urls", "winning_paths", "urls", "paths"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [str(item).strip() for item in candidate if str(item).strip()]

    return []


def _coerce_id_list(payload: Any) -> list[int]:
    candidates: Any = payload
    if isinstance(payload, dict):
        candidates = payload.get("selected_ids") or payload.get("ids") or []

    if not isinstance(candidates, list):
        return []

    ids: list[int] = []
    for item in candidates:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_priority_queue(payload: Any) -> list[dict[str, Any]]:
    candidates: Any = payload
    if isinstance(payload, dict):
        candidates = payload.get("priority_queue") or payload.get("selected_actions") or []

    if not isinstance(candidates, list):
        return []

    queue: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            normalized: dict[str, Any] = {}
            for key in ("id", "action", "priority", "reason"):
                if key in item:
                    normalized[key] = item[key]
            if "id" in normalized:
                try:
                    normalized["id"] = int(normalized["id"])
                except (TypeError, ValueError):
                    continue
            if "priority" in normalized:
                try:
                    normalized["priority"] = int(normalized["priority"])
                except (TypeError, ValueError):
                    normalized["priority"] = 0
            queue.append(normalized)
        else:
            try:
                queue.append({"id": int(item), "action": "click", "priority": 0, "reason": ""})
            except (TypeError, ValueError):
                continue

    queue.sort(key=lambda row: int(row.get("priority", 0)))
    return queue


def _normalize_url_value(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.endswith("/"):
        return value[:-1]
    return value


def _build_router_candidates(
    sitemap_metadata: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str], set[str]]:
    candidates: list[dict[str, Any]] = []
    id_to_url: dict[int, str] = {}
    discovered_url_set: set[str] = set()

    for index, row in enumerate(sitemap_metadata, start=1):
        row_url = _normalize_url_value(str(row.get("url", "")))
        if not row_url:
            continue

        discovered_url_set.add(row_url)
        id_to_url[index] = row_url
        candidate = {
            "id": index,
            "url": row_url,
            "anchor_text": str(row.get("anchor_text", "")).strip()[:200],
            "parent_context": str(row.get("parent_context", "")).strip()[:400],
        }
        candidates.append(candidate)

    return candidates, id_to_url, discovered_url_set


def _extract_intent_groups(user_query: str) -> list[list[str]]:
    text = str(user_query or "").lower()
    quoted_groups = re.findall(r"'([^']+)'|\"([^\"]+)\"", text)
    phrases = [first or second for first, second in quoted_groups if (first or second)]

    groups: list[list[str]] = []
    for phrase in phrases:
        cleaned = phrase.strip()
        if not cleaned:
            continue
        tokens = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", cleaned)
        group_terms = [cleaned, *tokens]
        groups.append(group_terms)

    if groups:
        return groups

    fallback_tokens = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", text)
    if fallback_tokens:
        return [fallback_tokens]
    return []


def _score_candidate_for_terms(candidate: dict[str, Any], terms: list[str]) -> int:
    haystack = " ".join(
        [
            str(candidate.get("url", "")),
            str(candidate.get("anchor_text", "")),
            str(candidate.get("parent_context", "")),
        ]
    ).lower()

    score = 0
    for term in terms:
        normalized = str(term).strip().lower()
        if not normalized:
            continue
        if normalized in haystack:
            score += 3 if " " in normalized else 1
    return score


def _rank_relevant_urls(
    router_candidates: list[dict[str, Any]],
    selected_urls: list[str],
    user_query: str,
    target_count: int,
) -> list[str]:
    candidate_by_url = {
        _normalize_url_value(str(candidate.get("url", ""))): candidate for candidate in router_candidates
    }
    candidate_by_url = {url: candidate for url, candidate in candidate_by_url.items() if url}

    intent_groups = _extract_intent_groups(user_query)
    if not intent_groups:
        return selected_urls[:target_count]

    selected_set = {_normalize_url_value(url) for url in selected_urls if _normalize_url_value(url)}

    # Keep only relevant URLs from model-selected set.
    relevant_selected: list[str] = []
    for url in selected_urls:
        normalized = _normalize_url_value(url)
        candidate = candidate_by_url.get(normalized)
        if not candidate:
            continue
        max_group_score = max(_score_candidate_for_terms(candidate, group) for group in intent_groups)
        if max_group_score > 0:
            relevant_selected.append(normalized)

    # Ensure at least one URL per intent group when possible.
    covered = set(relevant_selected)
    for group in intent_groups:
        ranked_group = sorted(
            candidate_by_url.items(),
            key=lambda pair: _score_candidate_for_terms(pair[1], group),
            reverse=True,
        )
        for url, candidate in ranked_group:
            if url in covered:
                break
            if _score_candidate_for_terms(candidate, group) <= 0:
                break
            relevant_selected.append(url)
            covered.add(url)
            break

    # Fill remaining slots with globally best relevant candidates.
    if len(relevant_selected) < target_count:
        ranked_global = sorted(
            candidate_by_url.items(),
            key=lambda pair: max(_score_candidate_for_terms(pair[1], group) for group in intent_groups),
            reverse=True,
        )
        for url, candidate in ranked_global:
            if len(relevant_selected) >= target_count:
                break
            if url in covered:
                continue
            if max(_score_candidate_for_terms(candidate, group) for group in intent_groups) <= 0:
                continue
            relevant_selected.append(url)
            covered.add(url)

    return _unique_urls(relevant_selected)[:target_count]


def select_winning_paths(
    sitemap_metadata: list[dict[str, Any]], user_query: str, target_count: int = 5
) -> list[str]:
    """Use Groq Llama 3.1 70B to rank sitemap paths and return the most relevant URLs."""

    print("[groq_client] select_winning_paths called")
    if not sitemap_metadata:
        print("[groq_client] Empty sitemap metadata received; returning no URLs")
        return []

    target_count = max(1, min(int(target_count or 5), 10))
    router_model = str(os.getenv("GROQ_ROUTER_MODEL", "llama-3.3-70b-versatile")).strip() or "llama-3.3-70b-versatile"
    router_fallback_model = "openai/gpt-oss-120b"
    router_candidates, id_to_url, discovered_url_set = _build_router_candidates(sitemap_metadata)
    if not router_candidates:
        print("[groq_client] No valid sitemap URLs found after candidate build")
        return []

    prompt = build_router_prompt(user_query, router_candidates, target_count=target_count)

    try:
        try:
            parsed = _call_groq_json(prompt, model=router_model)
        except Exception as router_exc:
            if _is_model_decommissioned(router_exc) and router_model != router_fallback_model:
                print(
                    f"[groq_client] Router model {router_model} is decommissioned; "
                    f"falling back to {router_fallback_model}"
                )
                parsed = _call_groq_json(prompt, model=router_fallback_model)
            else:
                raise

        selected_ids = _coerce_id_list(parsed)
        selected_urls = [id_to_url[candidate_id] for candidate_id in selected_ids if candidate_id in id_to_url]

        if not selected_urls:
            # Fallback path for older prompts/models that still return URLs directly.
            selected_urls = [
                _normalize_url_value(url)
                for url in _coerce_url_list(parsed)
                if _normalize_url_value(url) in discovered_url_set
            ]

        selected_urls = _unique_urls([url for url in selected_urls if url])
        selected_urls = _rank_relevant_urls(
            router_candidates=router_candidates,
            selected_urls=selected_urls,
            user_query=user_query,
            target_count=target_count,
        )

        print(f"[groq_client] Router selected URLs count={len(selected_urls)}")
        return selected_urls[:target_count]
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"[groq_client] URL selection failed: {error_msg}")
        raise RuntimeError(
            f"Groq URL routing failed. Is GROQ_API_KEY configured? Error: {error_msg}"
        )


def select_relevant_urls(sitemap: list[dict[str, Any]], user_query: str) -> list[str]:
    """Backward-compatible wrapper for older callers."""

    return select_winning_paths(sitemap, user_query)


def evaluate_traversal_path(
    user_query: str,
    page_snippet: str,
    discovered_elements: list[dict[str, Any]],
    logic_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use Groq Llama 3.1 70B to classify a page and rank the next navigation actions."""

    print("[groq_client] evaluate_traversal_path called")
    navigator_model = str(os.getenv("GROQ_NAVIGATOR_MODEL", "llama-3.3-70b-versatile")).strip() or "llama-3.3-70b-versatile"
    prompt = build_navigation_prompt(
        user_query=user_query,
        page_snippet=page_snippet,
        discovered_elements=discovered_elements,
        result_limit=max(1, min(int((logic_metadata or {}).get("result_limit", 10) or 10), 20)),
    )

    try:
        parsed = _call_groq_json(prompt, model=navigator_model)
        if not isinstance(parsed, dict):
            raise ValueError("Traversal response JSON is not an object")

        decision = str(parsed.get("decision", "continue")).strip().lower()
        if decision not in {"extract", "continue", "backtrack"}:
            decision = "continue"

        terminal_page = bool(parsed.get("terminal_page", decision == "extract"))
        priority_queue = _coerce_priority_queue(parsed)

        return {
            "decision": decision,
            "terminal_page": terminal_page,
            "reason": str(parsed.get("reason", "")).strip(),
            "priority_queue": priority_queue,
            "raw": parsed,
        }
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"[groq_client] Traversal evaluation failed: {error_msg}")
        raise RuntimeError(
            f"Groq traversal evaluation failed. Is GROQ_API_KEY configured? Error: {error_msg}"
        )


def extract_structured_json(page_markdown: str, extraction_goal: str) -> dict[str, Any]:
    """Process markdown in chunks and merge extracted records from Groq Llama 3.1 8B."""

    print("[groq_client] extract_structured_json called")
    chunk_size = 4000
    extractor_model = str(os.getenv("GROQ_EXTRACTOR_MODEL", "llama-3.1-8b-instant")).strip() or "llama-3.1-8b-instant"
    chunks = _chunk_text(page_markdown, chunk_size)
    print(f"[groq_client] Prepared chunks: chunk_count={len(chunks)}, chunk_size={chunk_size}")

    all_records: list[Any] = []
    resolved_logic_metadata: dict[str, Any] | None = None

    try:
        for index, chunk in enumerate(chunks, start=1):
            print(f"[groq_client] Processing chunk {index}/{len(chunks)}: chunk_len={len(chunk)}")
            prompt = build_extraction_prompt(extraction_goal, chunk)

            try:
                parsed = _call_groq_json(prompt, model=extractor_model)
                if not isinstance(parsed, dict):
                    raise ValueError("Chunk response JSON is not an object")

                action = str(parsed.get("action", "extract")).strip().lower()

                chunk_records = parsed.get("records", [])
                if isinstance(chunk_records, list):
                    all_records.extend(chunk_records)
                    print(
                        f"[groq_client] Chunk {index} merged: records_added={len(chunk_records)}, "
                        f"records_total={len(all_records)}"
                    )
                else:
                    print(f"[groq_client] Chunk {index} skipped: 'records' is not a list")

                chunk_logic_metadata = parsed.get("logic_metadata")
                if resolved_logic_metadata is None and isinstance(chunk_logic_metadata, dict):
                    resolved_logic_metadata = chunk_logic_metadata
                    print(f"[groq_client] Chunk {index} logic metadata captured")

                if action == "final":
                    print(f"[groq_client] Chunk {index} signaled final action; stopping loop")
                    break

            except Exception as chunk_exc:
                print(f"[groq_client] Chunk {index} failed: {type(chunk_exc).__name__}: {chunk_exc}")
                continue

        print(f"[groq_client] Chunk processing complete: records_total={len(all_records)}")
        return {"success": True, "records": all_records, "logic_metadata": resolved_logic_metadata or {}}

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"[groq_client] Extraction failed: {error_msg}")
        raise RuntimeError(
            f"Groq processing failed. Is GROQ_API_KEY configured? Error: {error_msg}"
        )