"""Playwright scraping engine and HTML-to-Markdown cleanup."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
import sys
import time
from typing import Any
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import html2text
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from ai.groq_client import evaluate_traversal_path
from scraper.strategies import paginate_pages, scroll_infinite_content

ScrapeStrategy = Literal["single", "pagination", "infinite_scroll"]


def _ensure_windows_proactor_policy() -> None:
    """Force a subprocess-capable asyncio policy for Playwright on Windows."""
    if sys.platform != "win32":
        return
    policy_factory = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_factory:
        asyncio.set_event_loop_policy(policy_factory())


@dataclass
class ScrapeResult:
    url: str
    markdown: str
    pages_visited: int = 1
    scrolls_performed: int = 0


class DeepDiscoveryCrawler:
    """Crawl a site with DFS, collect live navigation elements, and evaluate relevance in real time."""

    def __init__(self, base_url: str):
        self.base_url = self._normalize_root_url(base_url)

    def _normalize_root_url(self, raw_url: str) -> str:
        normalized = str(raw_url or "").strip()
        if not normalized:
            return normalized
        if not urlparse(normalized).scheme:
            normalized = f"https://{normalized}"
        return urlunparse(urlparse(normalized)._replace(fragment=""))

    def _hash_signature(self, prefix: str, value: str) -> str:
        payload = f"{prefix}:{value.strip()}".encode("utf-8", errors="ignore")
        return hashlib.sha256(payload).hexdigest()

    def _extract_context_snippet(self, page, limit: int = 1500) -> str:
        snippet = page.evaluate(
            r"""
            () => {
              const text = document.body ? (document.body.innerText || document.body.textContent || '') : '';
              return String(text || '').replace(/\s+/g, ' ').trim();
            }
            """
        )
        return str(snippet or "")[:limit]

    def _fetch_sitemap_urls(self, start_url: str) -> list[str]:
        """Recursively parse sitemaps to find HTML URLs, avoiding XML navigation in browser."""
        discovered_urls = []
        to_process = [urljoin(start_url, "/sitemap.xml")]
        processed_xmls = set()

        while to_process and len(discovered_urls) < 100:
            current_xml = to_process.pop()
            if current_xml in processed_xmls:
                continue
            processed_xmls.add(current_xml)

            try:
                request = Request(current_xml, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(request, timeout=10) as response:
                    raw_xml = response.read()
                root = ElementTree.fromstring(raw_xml)
                
                namespace = ""
                if root.tag.startswith("{"):
                    namespace = root.tag.split("}", maxsplit=1)[0][1:]
                
                def _tag(name: str) -> str:
                    return f"{{{namespace}}}{name}" if namespace else name

                # Handle sitemap indexes (links to other XML files)
                if root.tag.endswith("sitemapindex"):
                    for loc in root.findall(f".//{_tag('loc')}"):
                        if loc.text and loc.text.strip().endswith(".xml"):
                            to_process.append(loc.text.strip())
                # Handle standard sitemaps (links to HTML pages)
                else:
                    for loc in root.findall(f".//{_tag('loc')}"):
                        if loc.text:
                            url = self._normalize_url(start_url, loc.text.strip())
                            if url and not url.endswith(".xml") and url not in discovered_urls:
                                discovered_urls.append(url)
            except Exception as e:
                print(f"[engine] Sitemap processing skipped for {current_xml}: {e}")

        print(f"[engine] Sitemap crawl complete. Found {len(discovered_urls)} HTML URLs.")
        return discovered_urls

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
        return urlunparse(parsed_url._replace(fragment=""))

    def _discover_navigation_elements(self, page, page_url: str, extraction_goal: str) -> list[dict[str, Any]]:
        print(f"[engine] Discovering navigation elements from: {page_url}")
        element_rows = page.locator("a[href], button, [role='button'], input[type='search']").evaluate_all(
            """
            (nodes) => nodes.map((el) => ({
              tag: (el.tagName || '').toLowerCase(),
              role: (el.getAttribute('role') || '').toLowerCase(),
              text: (el.innerText || el.textContent || '').trim(),
              label: (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.textContent || el.getAttribute('placeholder') || '').trim(),
              href: el.getAttribute('href') || '',
              placeholder: el.getAttribute('placeholder') || '',
              title: el.getAttribute('title') || '',
              type: el.getAttribute('type') || '',
              name: el.getAttribute('name') || '',
              id: el.getAttribute('id') || '',
              class_name: el.className || '',
              parent_class: el.parentElement ? (el.parentElement.className || '') : '',
              parent_text: el.parentElement ? ((el.parentElement.innerText || el.parentElement.textContent || '')).trim() : '',
              aria_label: el.getAttribute('aria-label') || ''
            }))
            """
        )

        focus_terms = {token.lower() for token in str(extraction_goal or "").split() if len(token) > 2}
        discovered: list[dict[str, Any]] = []
        for index, row in enumerate(element_rows, start=1):
            text_bits = " ".join([str(row.get(k, "")) for k in ("text", "label", "parent_text", "aria_label", "placeholder", "title")]).lower()
            semantic_score = sum(1 for term in focus_terms if term in text_bits)
            signature_seed = "|".join([str(row.get(k, "")) for k in ("tag", "role", "text", "href", "parent_class", "placeholder", "title")])
            discovered.append({
                "id": index,
                "kind": "search-input" if row.get("type") == "search" else str(row.get("tag", "unknown")),
                "text": str(row.get("text", "")).strip()[:200],
                "label": str(row.get("label", "")).strip()[:200],
                "href": str(row.get("href", "")).strip(),
                "placeholder": str(row.get("placeholder", "")).strip()[:120],
                "title": str(row.get("title", "")).strip()[:120],
                "type": str(row.get("type", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "role": str(row.get("role", "")).strip(),
                "parent_class": str(row.get("parent_class", "")).strip()[:200],
                "parent_text": str(row.get("parent_text", "")).strip()[:200],
                "semantic_score": semantic_score,
                "signature": self._hash_signature("element", signature_seed),
            })
        return discovered

    def _evaluate_current_page(self, extraction_goal: str, page_snippet: str, discovered_elements: list[dict[str, Any]], result_limit: int) -> dict[str, Any]:
        return evaluate_traversal_path(user_query=extraction_goal, page_snippet=page_snippet, discovered_elements=discovered_elements, logic_metadata={"result_limit": result_limit})

    def _try_execute_navigation_action(self, page, page_url: str, element: dict[str, Any], extraction_goal: str) -> str | None:
        kind = str(element.get("kind", "")).lower()
        label = str(element.get("label") or element.get("text") or "").strip()
        href = str(element.get("href", "")).strip()
        text = str(element.get("text", "")).strip()

        try:
            if kind == "search-input":
                search_text = " ".join([t for t in str(extraction_goal or "").split() if len(t) > 2][:5])
                locator = page.locator("input[type='search']").first
                locator.fill(search_text)
                locator.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                return page.url
            if href:
                locator = page.get_by_role("link", name=label or text).first
                if locator.count() == 0:
                    locator = page.locator(f"a[href='{href}']").first
                locator.click()
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                return page.url
            if kind in {"button", "role-button"}:
                page.get_by_role("button", name=label or text).first.click()
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                return page.url
        except Exception as e:
            print(f"[engine] Action failed: {e}")
        return None

    def crawl_site(self, base_url: str | None = None, extraction_goal: str = "", result_limit: int = 50, global_page_limit: int = 50) -> dict[str, Any]:
        start_url = self._normalize_root_url(base_url or self.base_url)
        _ensure_windows_proactor_policy()
        stealth = Stealth()

        # Seed with homepage first, then sitemaps to ensure interactive discovery
        sitemap_urls = self._fetch_sitemap_urls(start_url)
        frontier = [start_url] + [url for url in reversed(sitemap_urls) if url != start_url]
        
        visited_elements: set[str] = set()
        visited_pages: list[dict[str, Any]] = []
        terminal_pages: list[str] = []
        dead_end_pages: list[str] = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            stealth.apply_stealth_sync(page)

            while frontier and len(visited_pages) < global_page_limit:
                current_url = self._normalize_root_url(frontier.pop())
                page_signature = self._hash_signature("url", current_url)
                if page_signature in visited_elements:
                    continue

                visited_elements.add(page_signature)
                print(f"[engine] Visiting: {current_url} (Frontier: {len(frontier)})")
                try:
                    # Resilient wait states for discovery
                    page.goto(current_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[engine] Load failed for {current_url}: {e}")
                    continue

                # OPTIMIZED CODE
                page_snippet = self._extract_context_snippet(page)
                discovered_elements = self._discover_navigation_elements(page, current_url, extraction_goal)

                # --- NEW: Semantic Pre-Filtering ---
                # Sort elements by semantic_score (highest first) and take the top 20.
                # This keeps the most relevant links and stays well within Groq's TPM limits.
                filtered_elements = sorted(
                    discovered_elements, 
                    key=lambda x: x.get("semantic_score", 0), 
                    reverse=True
                )[:20] 

                # Pass the smaller, high-quality list to the LLM
                evaluation = self._evaluate_current_page(extraction_goal, page_snippet, filtered_elements, result_limit)
                # ------------------------------------

                page_record = {
                    "url": current_url,
                    "decision": evaluation.get("decision", "continue"),
                    "terminal_page": bool(evaluation.get("terminal_page", False)),
                    "priority_queue": evaluation.get("priority_queue", []),
                    "page_index": len(visited_pages) + 1,
                }
                visited_pages.append(page_record)

                if page_record["terminal_page"]:
                    terminal_pages.append(current_url)
                    continue

                # Execute prioritized navigation actions
                moved = False
                for candidate in (page_record["priority_queue"] or []):
                    matching_element = next((item for item in discovered_elements if item.get("id") == int(candidate.get("id", -1))), None)
                    if matching_element and matching_element.get("signature") not in visited_elements:
                        visited_elements.add(matching_element["signature"])
                        next_url = self._try_execute_navigation_action(page, current_url, matching_element, extraction_goal)
                        if next_url:
                            norm_next = self._normalize_root_url(next_url)
                            if self._hash_signature("url", norm_next) not in visited_elements:
                                frontier.append(norm_next)
                                moved = True
                                break
                if not moved:
                    dead_end_pages.append(current_url)

            browser.close()

        return {
            "start_url": start_url,
            "visited_pages": visited_pages,
            "terminal_pages": terminal_pages,
            "pages_visited": len(visited_pages),
        }

    def execute_selector_extraction(self, url: str, selectors: list[str]) -> dict[str, str]:
        print(f"[engine] Extracting selectors from: {url}")
        _ensure_windows_proactor_policy()
        stealth = Stealth()
        extracted: dict[str, str] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            stealth.apply_stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=30000)
            for selector in selectors:
                if selector.strip():
                    try:
                        texts = page.locator(selector).evaluate_all("(elements) => elements.map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean)")
                        extracted[selector] = "\n".join(texts)
                    except:
                        extracted[selector] = ""
            browser.close()
        return extracted


def _is_internal_url(base_url: str, candidate_url: str) -> bool:
    base_host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    cand_host = (urlparse(candidate_url).hostname or "").lower().removeprefix("www.")
    return bool(base_host) and base_host == cand_host

def map_site(url: str) -> dict[str, Any]:
    return DeepDiscoveryCrawler(url).crawl_site(url)

def crawl_site(base_url: str, max_depth: int = 3, max_links: int = 100, extraction_goal: str = "", result_limit: int | None = None, global_page_limit: int | None = None) -> dict[str, Any]:
    limit = max_links if global_page_limit is None else global_page_limit
    return DeepDiscoveryCrawler(base_url).crawl_site(base_url=base_url, extraction_goal=extraction_goal, result_limit=result_limit or 10, global_page_limit=limit)

def execute_selector_extraction(url: str, selectors: list[str]) -> dict[str, str]:
    return DeepDiscoveryCrawler(url).execute_selector_extraction(url, selectors)

def scrape_url(url: str, strategy: ScrapeStrategy = "single", max_pages: int = 3, max_scrolls: int = 3, target_selector: str = None) -> ScrapeResult:
    _ensure_windows_proactor_policy()
    stealth = Stealth()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        stealth.apply_stealth_sync(page)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        
        pages_visited = 1
        if strategy == "pagination":
            pages_visited = paginate_pages(page, max_pages=max_pages)
        elif strategy == "infinite_scroll":
            scroll_infinite_content(page, max_scrolls=max_scrolls)

        converter = html2text.HTML2Text()
        converter.ignore_links, converter.ignore_images, converter.body_width = False, True, 0
        markdown = converter.handle(page.locator("body").inner_html()).strip()
        browser.close()
    return ScrapeResult(url=url, markdown=markdown, pages_visited=pages_visited)