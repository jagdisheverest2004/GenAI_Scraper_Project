"""Streamlit entry point for the local AI scraping app."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from ai.gemini_client import extract_structured_json
from scraper.engine import scrape_url

load_dotenv()
print(f"[main] .env loaded; GEMINI_API_KEY present={bool(os.getenv('GEMINI_API_KEY'))}")

st.set_page_config(page_title="AI Scraper", page_icon="🕵️", layout="wide")

st.title("AI Web Scraper")
st.caption("Scrape dynamic sites, clean the page, and structure results with Gemini.")

with st.sidebar:
    st.header("Scraping Settings")
    strategy = st.selectbox("Navigation strategy", ["single", "pagination", "infinite_scroll"], index=0)
    max_pages = st.slider("Max pages", min_value=1, max_value=10, value=3)
    max_scrolls = st.slider("Max scrolls", min_value=1, max_value=10, value=3)

url = st.text_input("Target URL", placeholder="https://example.com")
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


if run_button:
    print("[main] Run button clicked")
    print(f"[main] Inputs received: strategy={strategy}, max_pages={max_pages}, max_scrolls={max_scrolls}, url_present={bool(url)}, extraction_goal_present={bool(extraction_goal)}")
    if not url or not extraction_goal:
        print("[main] Validation failed: URL or extraction goal missing")
        st.error("Provide both a URL and an extraction requirement.")
    else:
        try:
            print("[main] Starting scrape phase")
            with st.spinner("Scraping site and preparing content..."):
                scrape_result = scrape_url(
                    url=url,
                    strategy=strategy,
                    max_pages=max_pages,
                    max_scrolls=max_scrolls,
                )
            print(
                f"[main] Scrape phase complete: pages_visited={scrape_result.pages_visited}, "
                f"scrolls_performed={scrape_result.scrolls_performed}, markdown_len={len(scrape_result.markdown)}"
            )

            st.success(
                f"Scraping complete. Pages visited: {scrape_result.pages_visited}, "
                f"scrolls performed: {scrape_result.scrolls_performed}."
            )

            print("[main] Starting Gemini extraction phase")
            with st.spinner("Sending cleaned content to Gemini..."):
                extracted = extract_structured_json(
                    page_markdown=scrape_result.markdown,
                    extraction_goal=extraction_goal,
                )
            print(f"[main] Gemini extraction complete: top_level_keys={list(extracted.keys())}")

            _render_json_payload(extracted)
            print("[main] Payload rendered in UI")

            with st.expander("Scraped Markdown"):
                st.text_area("Cleaned page content", scrape_result.markdown, height=300)
            print("[main] Scraped markdown displayed")

        except Exception as exc:
            print(f"[main] Exception raised: {type(exc).__name__}: {exc}")
            st.error(f"Extraction failed: {exc}")
            st.stop()
