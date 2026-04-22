"""Playwright scraping engine and HTML-to-Markdown cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
from typing import Any
from typing import Literal
from urllib.parse import urljoin, urlparse

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

    print(f"[engine] map_site called with url={url}")
    _ensure_windows_proactor_policy()
    stealth = Stealth()

    sitemap: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    with sync_playwright() as playwright:
        print("[engine] Launching Chromium browser for discovery (headless=True)")
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        print(f"[engine] Navigating to discovery URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        print("[engine] Discovery page load complete (networkidle)")

        anchor_rows = page.locator("a").evaluate_all(
            """
            (elements) => elements.map((el) => ({
              href: el.getAttribute('href') || '',
              text: (el.innerText || el.textContent || '').trim(),
              context: (el.parentElement ? (el.parentElement.innerText || el.parentElement.textContent || '') : '').trim()
            }))
            """
        )
        print(f"[engine] Anchor scan complete: anchor_count={len(anchor_rows)}")

        for row in anchor_rows:
            href = str(row.get("href", "")).strip()
            text = str(row.get("text", "")).strip()
            context = str(row.get("context", "")).strip()

            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue

            absolute_url = urljoin(url, href)
            if not _is_internal_url(url, absolute_url):
                continue

            if absolute_url in seen_urls:
                continue

            seen_urls.add(absolute_url)
            sitemap.append(
                {
                    "url": absolute_url,
                    "text": text,
                    "context": context,
                }
            )

        print(f"[engine] Internal sitemap built: internal_link_count={len(sitemap)}")
        context.close()
        browser.close()

    return sitemap


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
