"""Streamlit entry point for the local AI scraping app."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from ai.llama_client import extract_structured_json, select_relevant_urls
from scraper.engine import map_site, scrape_url

load_dotenv()
# CHANGE 2: Removed the Gemini API Key check since we are running locally
print("[main] .env loaded; using local Llama 3 backend.")

st.set_page_config(page_title="AI Scraper", page_icon="🕵️", layout="wide")

st.title("AI Web Scraper")
# CHANGE 3: Update caption
st.caption("Scrape dynamic sites, clean the page, and structure results with local Llama 3.")

with st.sidebar:
    st.header("Scraping Settings")
    strategy = st.selectbox("Navigation strategy", ["single", "pagination", "infinite_scroll"], index=0)
    max_pages = st.slider("Max pages", min_value=1, max_value=10, value=3)
    max_scrolls = st.slider("Max scrolls", min_value=1, max_value=10, value=3)

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
    result_limit = logic_metadata.get("result_limit")

    if not filter_field or not operator:
        return records

    dataframe = pd.DataFrame(records)
    if filter_field not in dataframe.columns:
        return records

    series = dataframe[filter_field]

    if operator in {"<", "<=", ">", ">="}:
        left = pd.to_numeric(series, errors="coerce")
        right_value = pd.to_numeric(pd.Series([target_value]), errors="coerce").iloc[0]
        if pd.isna(right_value):
            return records

        operator_map = {
            "<": left < right_value,
            "<=": left <= right_value,
            ">": left > right_value,
            ">=": left >= right_value,
        }
        filtered = dataframe[operator_map[operator]]
    elif operator in {"=", "==", "!="}:
        operator_map = {
            "=": series == target_value,
            "==": series == target_value,
            "!=": series != target_value,
        }
        filtered = dataframe[operator_map[operator]]
    else:
        return records

    try:
        limit = int(result_limit)
        if limit > 0:
            filtered = filtered.head(limit)
    except (TypeError, ValueError):
        pass

    return filtered.to_dict(orient="records")


if run_button:
    print("[main] Run button clicked")
    print(f"[main] Inputs received: strategy={strategy}, max_pages={max_pages}, max_scrolls={max_scrolls}, url_present={bool(url)}, extraction_goal_present={bool(extraction_goal)}")
    if not url or not extraction_goal:
        print("[main] Validation failed: URL or extraction goal missing")
        st.error("Provide both a URL and an extraction requirement.")
    else:
        try:
            print("[main] Starting discovery phase")
            with st.spinner("Step 1 of 4: Discovering site links..."):
                sitemap = map_site(url)
            print(f"[main] Discovery complete: sitemap_count={len(sitemap)}")
            st.success(f"Discovery complete. Internal links found: {len(sitemap)}")

            print("[main] Starting routing phase")
            with st.spinner("Step 2 of 4: Routing to the most relevant paths..."):
                selected_urls = select_relevant_urls(sitemap, extraction_goal)
            selected_urls = selected_urls or [url]
            print(f"[main] Routing complete: selected_url_count={len(selected_urls)}")
            st.success(f"Routing complete. Selected paths: {len(selected_urls)}")

            print("[main] Starting targeted extraction phase")
            combined_markdown_parts: list[str] = []
            total_pages_visited = 0
            total_scrolls_performed = 0
            successful_sources: list[str] = []

            with st.spinner("Step 3 of 4: Scraping selected pages..."):
                for index, selected_url in enumerate(selected_urls, start=1):
                    print(f"[main] Scraping selected URL {index}/{len(selected_urls)}: {selected_url}")
                    try:
                        scrape_result = scrape_url(
                            url=selected_url,
                            strategy=strategy,
                            max_pages=max_pages,
                            max_scrolls=max_scrolls,
                            target_selector=target_selector or None,
                        )
                        total_pages_visited += scrape_result.pages_visited
                        total_scrolls_performed += scrape_result.scrolls_performed
                        successful_sources.append(scrape_result.url)
                        combined_markdown_parts.append(
                            f"## Source URL: {scrape_result.url}\n\n{scrape_result.markdown}"
                        )
                        print(
                            f"[main] Scrape success: pages_visited={scrape_result.pages_visited}, "
                            f"scrolls_performed={scrape_result.scrolls_performed}, markdown_len={len(scrape_result.markdown)}"
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

            print("[main] Starting Llama 3 extraction phase")
            with st.spinner("Step 4 of 4: Extracting structured data with local Llama 3..."):
                extracted = extract_structured_json(
                    page_markdown=combined_markdown,
                    extraction_goal=extraction_goal,
                )
            print(f"[main] Llama 3 extraction complete: top_level_keys={list(extracted.keys())}")

            filtered_records = _apply_logic_metadata(
                records=extracted.get("records", []),
                logic_metadata=extracted.get("logic_metadata", {}),
            )
            final_payload = {
                **extracted,
                "records": filtered_records,
                "selected_urls": successful_sources,
            }

            st.success(
                f"Scraping complete. Pages visited: {total_pages_visited}, scrolls performed: {total_scrolls_performed}."
            )
            _render_json_payload(final_payload)
            print("[main] Payload rendered in UI")

            with st.expander("Scraped Markdown"):
                st.text_area("Combined cleaned page content", combined_markdown, height=300)
            print("[main] Scraped markdown displayed")

        except Exception as exc:
            print(f"[main] Exception raised: {type(exc).__name__}: {exc}")
            st.error(f"Extraction failed: {exc}")
            st.stop()