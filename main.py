"""Streamlit entry point for the local AI scraping app."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from ai.groq_client import extract_structured_json, select_winning_paths
from scraper.engine import crawl_site, execute_selector_extraction

load_dotenv()
# CHANGE 2: Removed the Gemini API Key check since we are running locally
print("[main] .env loaded; using Groq Cloud backend.")

st.set_page_config(page_title="AI Scraper", page_icon="🕵️", layout="wide")

st.title("AI Web Scraper")
# CHANGE 3: Update caption
st.caption("Scrape dynamic sites, clean the page, and structure results powered by Groq LPU.")

with st.sidebar:
    st.header("Scraping Settings")
    max_depth = st.slider("Crawl depth", min_value=1, max_value=5, value=3)
    max_links = st.slider("Max discovered links", min_value=10, max_value=200, value=100, step=10)

url = st.text_input("Target URL", placeholder="https://example.com")
target_selector = st.text_input(
    "Target CSS Selector (Optional)",
    placeholder="e.g., .product-grid, #main-content",
)
extraction_goal = st.text_area(
    "Extraction requirement",
    placeholder="Find keyboards under 2000 and return name, price, and rating.",
    height=120,
)

run_button = st.button("Scrape and Extract", type="primary")


def _render_json_payload(payload: dict[str, Any]) -> None:
    st.subheader("Structured JSON")
    st.json(payload)

    records = payload.get("records")
    if isinstance(records, list) and records:
        st.subheader("Tabular View")
        st.dataframe(pd.DataFrame(records), use_container_width=True)


def _apply_logic_metadata(records: list[dict[str, Any]], logic_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    if not records or not isinstance(logic_metadata, dict):
        return records

    filter_field = logic_metadata.get("filter_field")
    operator = str(logic_metadata.get("operator", "")).strip()
    target_value = logic_metadata.get("target_value")
    result_limit = logic_metadata.get("result_limit", logic_metadata.get("limit"))

    if not filter_field or not operator:
        return records

    dataframe = pd.DataFrame(records)
    if filter_field not in dataframe.columns:
        return records

    series = dataframe[filter_field]

    numeric_target = pd.to_numeric(pd.Series([target_value]), errors="coerce").iloc[0]
    normalized_operator = {"<": "lt", "<=": "lt", ">": "gt", ">=": "gt", "=": "eq", "==": "eq"}.get(
        operator, operator
    )

    if normalized_operator in {"lt", "gt"}:
        left = pd.to_numeric(series, errors="coerce")
        if pd.isna(numeric_target):
            return records

        operator_map = {
            "lt": left < numeric_target,
            "gt": left > numeric_target,
        }
        filtered = dataframe[operator_map[normalized_operator]]
    elif normalized_operator == "eq":
        numeric_series = pd.to_numeric(series, errors="coerce")
        if pd.notna(numeric_target) and numeric_series.notna().any():
            filtered = dataframe[numeric_series == numeric_target]
        else:
            filtered = dataframe[series.astype(str) == str(target_value)]
    else:
        return records

    try:
        limit = int(result_limit)
        if limit > 0:
            filtered = filtered.head(limit)
    except (TypeError, ValueError):
        pass

    return filtered.to_dict(orient="records")


def _normalize_url_value(url: Any) -> str:
    normalized = str(url or "").strip()
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _ground_and_dedupe_records(records: list[dict[str, Any]], allowed_urls: set[str]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []

    grounded: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue

        record_url = _normalize_url_value(record.get("url"))
        if record_url:
            if allowed_urls and record_url not in allowed_urls:
                continue
            if record_url in seen_urls:
                continue
            seen_urls.add(record_url)

        grounded.append(record)

    return grounded


def _extract_requested_count(extraction_goal: str, default_count: int = 3) -> int:
    text = str(extraction_goal or "")
    match = re.search(r"\b(\d{1,2})\b", text)
    if not match:
        return default_count

    try:
        value = int(match.group(1))
    except ValueError:
        return default_count

    return max(1, min(value, 10))


def _build_sitemap_lookup(sitemap: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in sitemap:
        if not isinstance(row, dict):
            continue
        row_url = _normalize_url_value(row.get("url"))
        if not row_url or row_url in lookup:
            continue
        lookup[row_url] = row
    return lookup


def _backfill_missing_records(
    records: list[dict[str, Any]],
    selected_urls: list[str],
    sitemap_lookup: dict[str, dict[str, Any]],
    target_count: int,
) -> list[dict[str, Any]]:
    if target_count <= 0:
        return records

    result = list(records)
    existing_urls = {
        _normalize_url_value(record.get("url"))
        for record in result
        if isinstance(record, dict) and _normalize_url_value(record.get("url"))
    }

    for raw_url in selected_urls:
        if len(result) >= target_count:
            break

        selected_url = _normalize_url_value(raw_url)
        if not selected_url or selected_url in existing_urls:
            continue

        metadata = sitemap_lookup.get(selected_url, {})
        anchor_text = str(metadata.get("anchor_text", "")).strip()
        parent_context = str(metadata.get("parent_context", "")).strip()

        headline = anchor_text or selected_url.rsplit("/", maxsplit=1)[-1].replace("-", " ").strip() or "Untitled"
        summary = parent_context or "No grounded summary could be extracted from the selected page content."

        result.append(
            {
                "headline": headline,
                "url": selected_url,
                "summary": summary,
                "source": "metadata_fallback",
            }
        )
        existing_urls.add(selected_url)

    return result


def _extract_focus_terms(extraction_goal: str) -> list[str]:
    text = str(extraction_goal or "")
    quoted_groups = re.findall(r"'([^']+)'|\"([^\"]+)\"", text)
    quoted_phrases = [first or second for first, second in quoted_groups if (first or second)]

    stop_words = {
        "find",
        "recent",
        "article",
        "articles",
        "specifically",
        "about",
        "return",
        "headline",
        "headlines",
        "url",
        "brief",
        "sentence",
        "summary",
        "context",
        "found",
        "link",
        "metadata",
        "only",
        "with",
        "from",
        "that",
        "this",
        "there",
        "news",
    }

    focus_terms: set[str] = set()
    for phrase in quoted_phrases:
        normalized_phrase = phrase.strip().lower()
        if normalized_phrase:
            focus_terms.add(normalized_phrase)
        for token in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", normalized_phrase):
            if token not in stop_words:
                focus_terms.add(token)

    if not focus_terms:
        for token in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", text.lower()):
            if token not in stop_words:
                focus_terms.add(token)

    return sorted(term for term in focus_terms if term)


def _is_record_relevant(record: dict[str, Any], focus_terms: list[str]) -> bool:
    if not focus_terms:
        return True
    haystack = " ".join(
        [
            str(record.get("headline", "")),
            str(record.get("summary", "")),
            str(record.get("url", "")),
        ]
    ).lower()
    return any(term in haystack for term in focus_terms)


def _filter_relevant_records(records: list[dict[str, Any]], extraction_goal: str) -> list[dict[str, Any]]:
    focus_terms = _extract_focus_terms(extraction_goal)
    return [record for record in records if isinstance(record, dict) and _is_record_relevant(record, focus_terms)]


def _build_seed_urls(base_url: str) -> list[str]:
    normalized_base = str(base_url or "").strip()
    if not normalized_base:
        return []

    parsed = urlparse(normalized_base)
    if not parsed.scheme:
        normalized_base = f"https://{normalized_base}"

    seeds = [
        normalized_base,
        urljoin(normalized_base, "/news"),
        urljoin(normalized_base, "/news/technology"),
        urljoin(normalized_base, "/news/science-environment"),
        urljoin(normalized_base, "/news/climate"),
    ]

    unique: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        value = _normalize_url_value(seed)
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _fallback_select_urls(
    sitemap: list[dict[str, Any]], extraction_goal: str, target_count: int
) -> list[str]:
    focus_terms = _extract_focus_terms(extraction_goal)
    scored: list[tuple[int, str]] = []

    for row in sitemap:
        if not isinstance(row, dict):
            continue
        row_url = _normalize_url_value(row.get("url"))
        if not row_url:
            continue

        haystack = " ".join(
            [
                row_url,
                str(row.get("anchor_text", "")),
                str(row.get("parent_context", "")),
            ]
        ).lower()

        if focus_terms:
            score = sum(1 for term in focus_terms if term in haystack)
        else:
            score = 1
        scored.append((score, row_url))

    scored.sort(key=lambda item: item[0], reverse=True)
    fallback_urls: list[str] = []
    seen_urls: set[str] = set()
    for score, row_url in scored:
        if row_url in seen_urls:
            continue
        # Keep zero-score URLs only when no positive match exists at all.
        if focus_terms and score <= 0 and any(candidate_score > 0 for candidate_score, _ in scored):
            continue
        fallback_urls.append(row_url)
        seen_urls.add(row_url)
        if len(fallback_urls) >= target_count:
            break

    return fallback_urls


if run_button:
    print("[main] Run button clicked")
    print(
        f"[main] Inputs received: max_depth={max_depth}, max_links={max_links}, url_present={bool(url)}, "
        f"extraction_goal_present={bool(extraction_goal)}"
    )
    if not url or not extraction_goal:
        print("[main] Validation failed: URL or extraction goal missing")
        st.error("Provide both a URL and an extraction requirement.")
    else:
        try:
            requested_count = _extract_requested_count(extraction_goal, default_count=3)
            print(f"[main] Parsed requested_count={requested_count}")

            print("[main] Starting discovery phase")
            with st.spinner("Step 1 of 4: Discovering site frontier..."):
                sitemap = crawl_site(url, max_depth=max_depth, max_links=max_links)

            if not sitemap:
                seed_urls = _build_seed_urls(url)
                sitemap = [
                    {
                        "url": seed_url,
                        "anchor_text": "seed fallback",
                        "parent_context": "generated fallback path when crawler returned no links",
                    }
                    for seed_url in seed_urls
                ]
                st.warning("Discovery returned no links; using fallback site sections for routing.")

            print(f"[main] Discovery complete: sitemap_count={len(sitemap)}")
            st.success(f"Discovery complete. Frontier links found: {len(sitemap)}")

            print("[main] Starting routing phase")
            with st.spinner("Step 2 of 4: Ranking the most relevant paths..."):
                selected_urls = select_winning_paths(sitemap, extraction_goal, target_count=requested_count)

            if not selected_urls:
                selected_urls = _fallback_select_urls(sitemap, extraction_goal, requested_count)
                if selected_urls:
                    st.warning("Router returned no links; using deterministic relevance fallback.")

            print(f"[main] Routing complete: selected_url_count={len(selected_urls)}")
            st.success(f"Routing complete. Selected paths: {len(selected_urls)}")

            if not selected_urls:
                diagnostics = {
                    "requested_count": requested_count,
                    "selected_url_count": 0,
                    "successful_source_count": 0,
                    "raw_extracted_count": 0,
                    "after_logic_count": 0,
                    "after_grounding_count": 0,
                    "after_relevance_count": 0,
                    "final_count": 0,
                    "dropped_count": 0,
                    "backfilled_count": 0,
                }
                final_payload = {
                    "success": True,
                    "records": [],
                    "logic_metadata": {},
                    "selected_urls": [],
                    "diagnostics": diagnostics,
                }
                st.warning("No relevant records found for the requested requirement.")
                _render_json_payload(final_payload)
                with st.expander("Run Diagnostics"):
                    st.json(diagnostics)
                print("[main] No selected URLs; returned empty payload")
                st.stop()

            print("[main] Starting targeted extraction phase")
            combined_markdown_parts: list[str] = []
            successful_sources: list[str] = []
            selector_list = [target_selector] if target_selector.strip() else ["main", "article", "[role='main']", "body"]

            with st.spinner("Step 3 of 4: Scraping selected pages..."):
                for index, selected_url in enumerate(selected_urls, start=1):
                    print(f"[main] Scraping selected URL {index}/{len(selected_urls)}: {selected_url}")
                    try:
                        selector_payload = execute_selector_extraction(selected_url, selector_list)
                        selector_text_parts = [f"## Source URL: {selected_url}"]
                        for selector_name, selector_text in selector_payload.items():
                            if selector_text.strip():
                                selector_text_parts.append(f"### Selector: {selector_name}\n\n{selector_text.strip()}")

                        combined_text = "\n\n".join(selector_text_parts).strip()
                        if combined_text:
                            successful_sources.append(selected_url)
                            combined_markdown_parts.append(combined_text)
                            print(
                                f"[main] Selector extraction success: selector_count={len(selector_payload)}, "
                                f"combined_len={len(combined_text)}"
                            )
                    except Exception as scrape_exc:
                        print(f"[main] Scrape failed for {selected_url}: {type(scrape_exc).__name__}: {scrape_exc}")
                        st.warning(f"Skipped {selected_url}: {scrape_exc}")

            if not combined_markdown_parts:
                raise RuntimeError("No selected pages could be scraped successfully.")

            combined_markdown = "\n\n---\n\n".join(combined_markdown_parts)
            print(
                f"[main] Targeted extraction input ready: source_count={len(successful_sources)}, "
                f"combined_markdown_len={len(combined_markdown)}"
            )

            print("[main] Starting Groq extraction phase")
            with st.spinner("Step 4 of 4: Extracting structured data with Groq LPU..."):
                extracted = extract_structured_json(
                    page_markdown=combined_markdown,
                    extraction_goal=extraction_goal,
                )
            print(f"[main] Groq extraction complete: top_level_keys={list(extracted.keys())}")

            raw_records = extracted.get("records", [])
            if not isinstance(raw_records, list):
                raw_records = []

            records_after_logic = _apply_logic_metadata(
                records=raw_records,
                logic_metadata=extracted.get("logic_metadata", {}),
            )
            allowed_urls = {
                _normalize_url_value(item.get("url"))
                for item in sitemap
                if isinstance(item, dict) and _normalize_url_value(item.get("url"))
            }
            allowed_urls.update(_normalize_url_value(item) for item in selected_urls if _normalize_url_value(item))
            allowed_urls.add(_normalize_url_value(url))
            records_after_grounding = _ground_and_dedupe_records(records_after_logic, allowed_urls)
            records_after_relevance = _filter_relevant_records(records_after_grounding, extraction_goal)
            filtered_records = records_after_relevance[:requested_count]

            diagnostics = {
                "requested_count": requested_count,
                "selected_url_count": len(selected_urls),
                "successful_source_count": len(successful_sources),
                "raw_extracted_count": len(raw_records),
                "after_logic_count": len(records_after_logic),
                "after_grounding_count": len(records_after_grounding),
                "after_relevance_count": len(records_after_relevance),
                "final_count": len(filtered_records),
                "dropped_count": max(0, len(records_after_logic) - len(records_after_grounding)),
                "backfilled_count": 0,
            }

            final_payload = {
                **extracted,
                "records": filtered_records,
                "selected_urls": successful_sources,
                "diagnostics": diagnostics,
            }

            st.success(
                f"Scraping complete. Sources processed: {len(successful_sources)}."
            )

            if not filtered_records:
                st.warning("No relevant records found for the requested requirement.")

            _render_json_payload(final_payload)
            print("[main] Payload rendered in UI")

            with st.expander("Run Diagnostics"):
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Requested", diagnostics["requested_count"])
                col2.metric("Selected URLs", diagnostics["selected_url_count"])
                col3.metric("Extracted", diagnostics["raw_extracted_count"])
                col4.metric("Final", diagnostics["final_count"])

                col5, col6, col7, col8 = st.columns(4)
                col5.metric("After Logic", diagnostics["after_logic_count"])
                col6.metric("After Grounding", diagnostics["after_grounding_count"])
                col7.metric("Dropped", diagnostics["dropped_count"])
                col8.metric("Backfilled", diagnostics["backfilled_count"])

                st.metric("After Relevance", diagnostics["after_relevance_count"])

                st.json(diagnostics)

            with st.expander("Scraped Markdown"):
                st.text_area("Combined cleaned page content", combined_markdown, height=300)
            print("[main] Scraped markdown displayed")

        except Exception as exc:
            print(f"[main] Exception raised: {type(exc).__name__}: {exc}")
            st.error(f"Extraction failed: {exc}")
            st.stop()