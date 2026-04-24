"""Playwright scraping engine and HTML-to-Markdown cleanup."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import sys
from typing import Any
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse

import html2text
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from scraper.strategies import paginate_pages, scroll_infinite_content

ScrapeStrategy = Literal["single", "pagination", "infinite_scroll"]


def _ensure_windows_proactor_policy() -> None:
    """Force a subprocess-capable asyncio policy for Playwright on Windows."""

    if sys.platform != "win32":
        print("[engine] Non-Windows platform detected; skipping event-loop policy setup")
        return

    policy_factory = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_factory is None:
        print("[engine] WindowsProactorEventLoopPolicy not available")
        return

    asyncio.set_event_loop_policy(policy_factory())
    print("[engine] WindowsProactorEventLoopPolicy configured")


@dataclass
class ScrapeResult:
    url: str
    markdown: str
    pages_visited: int = 1
    scrolls_performed: int = 0


class DeepDiscoveryCrawler:
    """Crawl a site breadth-first, collect link metadata, and extract selector text."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.base_parsed = urlparse(base_url)

    def _normalize_url(self, source_url: str, href: str) -> str | None:
        href = str(href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None

        resolved_url = urljoin(source_url, href)
        parsed_url = urlparse(resolved_url)
        if parsed_url.scheme not in {"http", "https"}:
            return None

        if not _is_internal_url(self.base_url, resolved_url):
            return None

        normalized = parsed_url._replace(fragment="")
        return urlunparse(normalized)

    def _collect_anchor_metadata(self, page, page_url: str) -> list[dict[str, Any]]:
        print(f"[engine] Collecting anchors from: {page_url}")
        anchor_rows = page.locator("a").evaluate_all(
            """
            (elements) => elements.map((el) => ({
              href: el.getAttribute('href') || '',
              anchor_text: (el.innerText || el.textContent || '').trim(),
              context: (el.parentElement ? (el.parentElement.innerText || el.parentElement.textContent || '') : '').trim()
            }))
            """
        )
        print(f"[engine] Anchor scan complete: anchor_count={len(anchor_rows)}")

        metadata: list[dict[str, Any]] = []
        for row in anchor_rows:
            href = str(row.get("href", "")).strip()
            anchor_text = str(row.get("anchor_text", "")).strip()
            parent_context = str(row.get("parent_context") or row.get("context") or "").strip()
            resolved_url = self._normalize_url(page_url, href)
            if not resolved_url:
                continue

            metadata.append(
                {
                    "url": resolved_url,
                    "anchor_text": anchor_text,
                    "parent_context": parent_context,
                }
            )

        return metadata

    def crawl_site(self, base_url: str | None = None, max_depth: int = 3, max_links: int = 100) -> list[dict[str, Any]]:
        """Crawl a site breadth-first and return de-duplicated link metadata."""

        start_url = base_url or self.base_url
        print(
            f"[engine] crawl_site called with base_url={start_url}, max_depth={max_depth}, max_links={max_links}"
        )
        _ensure_windows_proactor_policy()
        stealth = Stealth()

        queue = deque([(start_url, 0)])
        seen_urls: set[str] = {urlunparse(urlparse(start_url)._replace(fragment=""))}
        collected_metadata: list[dict[str, Any]] = []

        with sync_playwright() as playwright:
            print("[engine] Launching Chromium browser for deep discovery (headless=True)")
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            stealth.apply_stealth_sync(page)

            while queue and len(collected_metadata) < max_links:
                current_url, depth = queue.popleft()
                print(f"[engine] Discovery visit: url={current_url}, depth={depth}")
                try:
                    page.goto(current_url, wait_until="domcontentloaded")
                    page.wait_for_load_state("networkidle")
                except Exception as visit_exc:
                    print(f"[engine] Discovery visit failed: {type(visit_exc).__name__}: {visit_exc}")
                    continue

                page_metadata = self._collect_anchor_metadata(page, current_url)
                for row in page_metadata:
                    collected_metadata.append(row)
                    if len(collected_metadata) >= max_links:
                        break

                    resolved_url = row["url"]
                    if resolved_url in seen_urls:
                        continue

                    seen_urls.add(resolved_url)
                    if depth + 1 <= max_depth and len(seen_urls) <= max_links:
                        queue.append((resolved_url, depth + 1))

                print(
                    f"[engine] Discovery progress: collected={len(collected_metadata)}, queued={len(queue)}, seen={len(seen_urls)}"
                )

            context.close()
            browser.close()

        print(f"[engine] crawl_site completed: metadata_count={len(collected_metadata)}")
        return collected_metadata

    def execute_selector_extraction(self, url: str, selectors: list[str]) -> dict[str, str]:
        """Visit a URL and return only the text content from the requested CSS selectors."""

        print(f"[engine] execute_selector_extraction called with url={url}, selector_count={len(selectors)}")
        _ensure_windows_proactor_policy()
        stealth = Stealth()
        extracted: dict[str, str] = {}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            stealth.apply_stealth_sync(page)

            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")

            for selector in selectors:
                cleaned_selector = str(selector or "").strip()
                if not cleaned_selector:
                    continue

                try:
                    locator = page.locator(cleaned_selector)
                    if locator.count() == 0:
                        extracted[cleaned_selector] = ""
                        continue

                    texts = locator.evaluate_all(
                        """
                        (elements) => elements
                          .map((el) => (el.innerText || el.textContent || '').trim())
                          .filter(Boolean)
                        """
                    )
                    extracted[cleaned_selector] = "\n".join(
                        str(text).strip() for text in texts if str(text).strip()
                    )
                except Exception as selector_exc:
                    print(
                        f"[engine] Selector extraction failed for {cleaned_selector}: {type(selector_exc).__name__}: {selector_exc}"
                    )
                    extracted[cleaned_selector] = ""

            context.close()
            browser.close()

        return extracted


def _is_internal_url(base_url: str, candidate_url: str) -> bool:
    """Return True when the candidate URL belongs to the same site as the base URL."""

    base_parsed = urlparse(base_url)
    candidate_parsed = urlparse(candidate_url)

    if candidate_parsed.scheme not in {"http", "https"}:
        return False

    base_host = (base_parsed.hostname or "").lower().removeprefix("www.")
    candidate_host = (candidate_parsed.hostname or "").lower().removeprefix("www.")
    return bool(base_host) and base_host == candidate_host


def _clean_body_html(page) -> str:
    """Remove noisy tags and strip attributes before converting to Markdown."""

    print("[engine] Cleaning page HTML: removing junk tags and attributes")

    page.evaluate(
        """
        () => {
          // 1. Expand the list of useless tags
          const removableSelectors = [
            "script", "style", "nav", "footer", "header", "aside", 
            "svg", "canvas", "iframe", "noscript", "form", "meta"
          ];
          removableSelectors.forEach((selector) => {
            document.querySelectorAll(selector).forEach((node) => node.remove());
          });

          // 2. The Token Saver: Strip ALL attributes from remaining elements 
          // (except hrefs on links so we don't lose URLs)
          document.querySelectorAll('*').forEach(el => {
              if (el.tagName !== 'A') {
                  // Create a list of attribute names to remove
                  const attrsToRemove = Array.from(el.attributes).map(attr => attr.name);
                  attrsToRemove.forEach(attrName => el.removeAttribute(attrName));
              }
          });
        }
        """
    )
    body_html = page.locator("body").inner_html()
    print(f"[engine] HTML cleanup complete: body_html_len={len(body_html)}")
    return body_html

def _html_to_markdown(body_html: str) -> str:
    print(f"[engine] Converting HTML to markdown: html_len={len(body_html)}")
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0
    converter.single_line_break = True
    markdown = converter.handle(body_html).strip()
    print(f"[engine] Markdown conversion complete: markdown_len={len(markdown)}")
    return markdown


def map_site(url: str) -> list[dict[str, Any]]:
    """Discover the internal link graph for a site using Playwright."""

    return DeepDiscoveryCrawler(url).crawl_site(url)


def crawl_site(base_url: str, max_depth: int = 3, max_links: int = 100) -> list[dict[str, Any]]:
    """Compatibility wrapper for the deep discovery crawler."""

    return DeepDiscoveryCrawler(base_url).crawl_site(base_url=base_url, max_depth=max_depth, max_links=max_links)


def execute_selector_extraction(url: str, selectors: list[str]) -> dict[str, str]:
    """Compatibility wrapper for selector-only extraction."""

    return DeepDiscoveryCrawler(url).execute_selector_extraction(url, selectors)


def scrape_url(
    url: str,
    strategy: ScrapeStrategy = "single",
    max_pages: int = 3,
    max_scrolls: int = 3,
    target_selector: str = None,
) -> ScrapeResult:
    """Open a URL, apply the selected navigation strategy, and return Markdown."""

    print(
        f"[engine] scrape_url called with url={url}, strategy={strategy}, "
        f"max_pages={max_pages}, max_scrolls={max_scrolls}"
    )

    _ensure_windows_proactor_policy()
    stealth = Stealth()
    print("[engine] Playwright and stealth initialized")

    with sync_playwright() as playwright:
        print("[engine] Launching Chromium browser (headless=True)")
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        stealth.apply_stealth_sync(page)
        print("[engine] Browser context/page ready and stealth applied")

        print(f"[engine] Navigating to URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        print("[engine] Initial page load complete (networkidle)")

        pages_visited = 1
        scrolls_performed = 0

        if strategy == "pagination":
            print("[engine] Executing pagination strategy")
            pages_visited = paginate_pages(page, max_pages=max_pages)
        elif strategy == "infinite_scroll":
            print("[engine] Executing infinite_scroll strategy")
            scrolls_performed = scroll_infinite_content(page, max_scrolls=max_scrolls)
        else:
            print("[engine] Executing single-page strategy (no extra navigation)")

        if target_selector:
            print(f"[engine] Target selector provided; waiting for: {target_selector}")
            page.wait_for_selector(target_selector)
            body_html = page.locator(target_selector).first.inner_html()
            print(f"[engine] Target selector extraction complete: html_len={len(body_html)}")
        else:
            body_html = _clean_body_html(page)
        markdown = _html_to_markdown(body_html)

        print("[engine] Closing browser context")
        context.close()
        browser.close()
        print("[engine] Browser closed")

    print(
        f"[engine] scrape_url completed: pages_visited={pages_visited}, "
        f"scrolls_performed={scrolls_performed}, markdown_len={len(markdown)}"
    )
    return ScrapeResult(
        url=url,
        markdown=markdown,
        pages_visited=pages_visited,
        scrolls_performed=scrolls_performed,
    )
