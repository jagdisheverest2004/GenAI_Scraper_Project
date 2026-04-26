"""Playwright scraping engine and HTML-to-Markdown cleanup."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
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
from PIL import Image
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from ai.groq_client import evaluate_traversal_path, extract_goal_fields
from scraper.strategies import paginate_pages, scroll_infinite_content

ScrapeStrategy = Literal["single", "pagination", "infinite_scroll"]

SCREENSHOT_MAX_WIDTH = 512
SCREENSHOT_JPEG_QUALITY = 40
NAVIGATION_ELEMENT_LIMIT = 8
STUCK_TURN_THRESHOLD = 3


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
        self.history: list[str] = []
        self.attempted_actions: dict[str, set[str]] = {}
        self.page_turn_counts: dict[str, int] = {}

    def _normalize_root_url(self, raw_url: str) -> str:
        normalized = str(raw_url or "").strip()
        if not normalized:
            return normalized
        if not urlparse(normalized).scheme:
            normalized = f"https://{normalized}"
        if normalized.startswith("http:///") or normalized.startswith("https:///"):
            return ""
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urlunparse(parsed._replace(fragment=""))

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

    def _fetch_sitemap_urls(self, start_url: str) -> dict[str, Any]:
        """Recursively parse sitemaps and return a hierarchical tree of discovered HTML URLs."""
        discovered_urls: list[str] = []
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

        sitemap_tree = self._build_sitemap_tree(start_url, discovered_urls)
        print(f"[engine] Sitemap crawl complete. Found {len(discovered_urls)} HTML URLs.")
        return sitemap_tree

    def _build_sitemap_tree(self, root_url: str, urls: list[str]) -> dict[str, Any]:
        root = {
            "url": self._normalize_root_url(root_url),
            "segment": "/",
            "children": {},
            "is_page": True,
        }
        for page_url in urls:
            normalized = self._normalize_root_url(page_url)
            parsed = urlparse(normalized)
            parts = [part for part in parsed.path.split("/") if part]
            if not parts:
                continue

            cursor = root
            current_path = ""
            for part in parts:
                current_path = f"{current_path}/{part}" if current_path else f"/{part}"
                node = cursor["children"].setdefault(
                    part,
                    {
                        "url": urlunparse(parsed._replace(path=current_path, query="", fragment="")),
                        "segment": part,
                        "children": {},
                        "is_page": False,
                    },
                )
                cursor = node
            cursor["is_page"] = True
        return root

    def _flatten_sitemap_tree(self, sitemap_tree: dict[str, Any]) -> list[str]:
        urls: list[str] = []

        def _walk(node: dict[str, Any]) -> None:
            node_url = str(node.get("url", "")).strip()
            if node_url and node.get("is_page"):
                urls.append(node_url)
            children = node.get("children") or {}
            for child in children.values():
                if isinstance(child, dict):
                    _walk(child)

        _walk(sitemap_tree)
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped

    def _find_sitemap_branch(self, sitemap_tree: dict[str, Any], current_url: str) -> dict[str, Any]:
        normalized = self._normalize_root_url(current_url)
        parsed = urlparse(normalized)
        parts = [part for part in parsed.path.split("/") if part]

        cursor = sitemap_tree
        for part in parts:
            children = cursor.get("children") or {}
            next_node = children.get(part)
            if not isinstance(next_node, dict):
                break
            cursor = next_node
        return cursor

    def _build_sitemap_branch_candidates(
        self,
        sitemap_branch: dict[str, Any],
        extraction_goal: str,
        starting_id: int,
    ) -> list[dict[str, Any]]:
        children = sitemap_branch.get("children") or {}
        focus_terms = {token.lower() for token in str(extraction_goal or "").split() if len(token) > 2}
        candidates: list[dict[str, Any]] = []

        next_id = starting_id
        for segment, node in children.items():
            if not isinstance(node, dict):
                continue
            node_url = str(node.get("url", "")).strip()
            if not node_url:
                continue

            haystack = f"{segment} {node_url}".lower()
            semantic_score = sum(1 for term in focus_terms if term in haystack)
            candidates.append(
                {
                    "id": next_id,
                    "kind": "sitemap-node",
                    "text": str(segment).strip()[:200],
                    "label": str(segment).replace("-", " ").replace("_", " ").strip()[:200],
                    "href": node_url,
                    "placeholder": "",
                    "title": "",
                    "type": "",
                    "name": "",
                    "role": "",
                    "parent_class": "",
                    "parent_text": "",
                    "semantic_score": semantic_score,
                    "signature": self._hash_signature("sitemap", node_url),
                }
            )
            next_id += 1
        return candidates

    def _capture_screenshot_b64(self, page) -> str:
        try:
            screenshot_bytes = page.screenshot(full_page=False)
            if not screenshot_bytes:
                return ""

            image = Image.open(BytesIO(screenshot_bytes)).convert("L")
            if image.width > SCREENSHOT_MAX_WIDTH:
                ratio = SCREENSHOT_MAX_WIDTH / float(image.width)
                resized_height = max(1, int(image.height * ratio))
                image = image.resize((SCREENSHOT_MAX_WIDTH, resized_height), Image.Resampling.LANCZOS)

            compressed_buffer = BytesIO()
            image.save(compressed_buffer, format="JPEG", quality=SCREENSHOT_JPEG_QUALITY, optimize=True)
            compressed_bytes = compressed_buffer.getvalue()
            b64 = base64.b64encode(compressed_bytes).decode("utf-8")
            print(f"[DEBUG] Image compressed to {len(b64)} chars")
            return b64
        except Exception as e:
            print(f"[DEBUG] Screenshot capture failed: {e}")
            return ""

    def _go_back_with_history(self, page, fallback_url: str) -> str | None:
        if self.history:
            target_url = self.history.pop()
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                self._wait_for_page_stable(page)
                return self._normalize_root_url(page.url)
            except Exception as e:
                print(f"[engine] History BACK failed: {e}")
                return None
        return self._normalize_root_url(fallback_url)

    def _normalize_url(self, source_url: str, href: str) -> str | None:
        href = str(href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None
        resolved_url = urljoin(source_url, href)
        if resolved_url.startswith("http:///") or resolved_url.startswith("https:///"):
            return None
        parsed_url = urlparse(resolved_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return None
        normalized_url = urlunparse(parsed_url._replace(fragment=""))
        if not _is_internal_url(self.base_url, normalized_url):
            return None
        return normalized_url

    def _wait_for_page_stable(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
            print("[DEBUG] Page stabilized")
        except Exception as e:
            print(f"[DEBUG] Page stabilization warning: {e}")

    def _goal_fields_to_search_text(self, goal_fields: dict[str, Any], fallback_query: str) -> str:
        parts: list[str] = []
        if isinstance(goal_fields, dict):
            for key in ("search_terms", "entities", "output_fields", "filters", "primary_goal"):
                value = goal_fields.get(key)
                if isinstance(value, list):
                    parts.extend(str(item).strip() for item in value if str(item).strip())
                elif isinstance(value, str) and value.strip():
                    parts.append(value.strip())

        if not parts:
            parts = [token for token in str(fallback_query or "").split() if len(token.strip()) > 1]

        deduped: list[str] = []
        seen: set[str] = set()
        for part in parts:
            normalized = str(part).strip()
            if not normalized or normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            deduped.append(normalized)
        return " ".join(deduped).strip()

    def _discover_navigation_elements(self, page, page_url: str, extraction_goal: str) -> list[dict[str, Any]]:
        print(f"[DEBUG] Discovering navigation elements from: {page_url}")
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
            candidate = {
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
            }
            if self._is_skip_navigation_link(candidate):
                continue
            discovered.append(candidate)
        return discovered

    def _evaluate_current_page(
        self,
        extraction_goal: str,
        goal_fields: dict[str, Any],
        page_snippet: str,
        discovered_elements: list[dict[str, Any]],
        result_limit: int,
        screenshot_b64: str,
        sitemap_tree: dict[str, Any],
        current_url: str,
        url_stack: list[str],
        attempted_actions: list[str],
        stuck_turns_current_url: int,
    ) -> dict[str, Any]:
        return evaluate_traversal_path(
            user_query=extraction_goal,
            goal_fields=goal_fields,
            page_snippet=page_snippet,
            discovered_elements=discovered_elements,
            screenshot_b64=screenshot_b64,
            sitemap_tree=sitemap_tree,
            current_url=current_url,
            url_stack=url_stack,
            attempted_actions=attempted_actions,
            stuck_turns_current_url=stuck_turns_current_url,
            logic_metadata={"result_limit": result_limit},
        )

    def _is_skip_navigation_link(self, element: dict[str, Any]) -> bool:
        text = " ".join(
            [
                str(element.get("text", "")),
                str(element.get("label", "")),
                str(element.get("title", "")),
                str(element.get("href", "")),
            ]
        ).lower()
        return any(
            pattern in text
            for pattern in ("skip to content", "jump to main", "skip navigation", "#content", "#main")
        )

    def _action_key(self, action: str, element: dict[str, Any] | None = None) -> str:
        if not isinstance(element, dict):
            return str(action or "").strip().lower()
        token = str(
            element.get("signature")
            or element.get("href")
            or element.get("label")
            or element.get("text")
            or ""
        ).strip()
        return f"{str(action or '').strip().lower()}::{token}".lower()

    def _force_sitemap_jump(
        self,
        sitemap_tree: dict[str, Any],
        current_url: str,
        extraction_goal: str,
        visited_urls: set[str],
        attempted_on_page: set[str],
    ) -> str | None:
        branch = self._find_sitemap_branch(sitemap_tree, current_url)
        branch_candidates = self._build_sitemap_branch_candidates(branch, extraction_goal, starting_id=1)
        ranked_branch = sorted(branch_candidates, key=lambda item: int(item.get("semantic_score", 0)), reverse=True)

        for candidate in ranked_branch:
            candidate_url = self._normalize_root_url(str(candidate.get("href", "")))
            if not candidate_url or candidate_url == current_url or candidate_url in visited_urls:
                continue
            if f"navigate::{candidate_url}" in attempted_on_page:
                continue
            return candidate_url

        focus_terms = {token.lower() for token in str(extraction_goal or "").split() if len(token) > 2}
        flattened = self._flatten_sitemap_tree(sitemap_tree)
        scored: list[tuple[int, str]] = []
        for url in flattened:
            normalized = self._normalize_root_url(url)
            if not normalized or normalized == current_url or normalized in visited_urls:
                continue
            if f"navigate::{normalized}" in attempted_on_page:
                continue
            haystack = normalized.lower()
            score = sum(1 for token in focus_terms if token in haystack)
            scored.append((score, normalized))

        if not scored:
            return None
        scored.sort(key=lambda row: row[0], reverse=True)
        return scored[0][1]

    def _try_execute_navigation_action(
        self,
        page,
        page_url: str,
        element: dict[str, Any],
        extraction_goal: str,
        goal_fields: dict[str, Any],
        action: str = "click",
    ) -> str | None:
        kind = str(element.get("kind", "")).lower()
        label = str(element.get("label") or element.get("text") or "").strip()
        href = str(element.get("href", "")).strip()
        text = str(element.get("text", "")).strip()
        requested_action = str(action or "click").lower()
        goal_text = self._goal_fields_to_search_text(goal_fields, extraction_goal)

        try:
            if kind in {"search-input", "input"} or requested_action in {"type", "submit"}:
                locator = page.locator("input[type='search'], input[role='searchbox'], input[placeholder*='search' i]").first
                if locator.count() == 0 and label:
                    locator = page.get_by_role("textbox", name=label).first
                locator.click()
                locator.fill(goal_text)
                locator.press("Enter")
                self._wait_for_page_stable(page)
                print(f"[DEBUG] Action Failed/Success: success type/submit -> {page.url}")
                return page.url
            if requested_action == "navigate" and href:
                next_url = self._normalize_url(page_url, href)
                print(f"[DEBUG] Action Failed/Success: success navigate -> {next_url or 'None'}")
                return next_url
            if href:
                locator = page.get_by_role("link", name=label or text).first
                if locator.count() == 0:
                    locator = page.locator(f"a[href='{href}']").first
                locator.scroll_into_view_if_needed(timeout=5000)
                try:
                    locator.click(timeout=5000)
                except Exception:
                    locator.click(force=True, timeout=5000)
                self._wait_for_page_stable(page)
                print(f"[DEBUG] Action Failed/Success: success click/link -> {page.url}")
                return page.url
            if kind in {"button", "role-button"}:
                locator = page.get_by_role("button", name=label or text).first
                locator.scroll_into_view_if_needed(timeout=5000)
                try:
                    locator.click(timeout=5000)
                except Exception:
                    locator.click(force=True, timeout=5000)
                self._wait_for_page_stable(page)
                print(f"[DEBUG] Action Failed/Success: success click/button -> {page.url}")
                return page.url
        except Exception as e:
            print(f"[DEBUG] Action Failed/Success: failed {requested_action} [{label or text or href}] -> {e}")
        return None

    def crawl_site(self, base_url: str | None = None, extraction_goal: str = "", result_limit: int = 50, global_page_limit: int = 50) -> dict[str, Any]:
        start_url = self._normalize_root_url(base_url or self.base_url)
        _ensure_windows_proactor_policy()
        stealth = Stealth()
        self.history = []
        self.attempted_actions = {}
        self.page_turn_counts = {}
        goal_fields = extract_goal_fields(extraction_goal) if str(extraction_goal or "").strip() else {}
        print(f"[DEBUG] Extracted Goal Fields: {json.dumps(goal_fields, ensure_ascii=False)}")

        sitemap_tree = self._fetch_sitemap_urls(start_url)
        visited_pages: list[dict[str, Any]] = []
        terminal_pages: list[str] = []
        dead_end_pages: list[str] = []
        url_stack: list[str] = [start_url]
        current_url = start_url
        loaded_url = ""

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            stealth.apply_stealth_sync(page)

            while current_url and len(visited_pages) < global_page_limit:
                try:
                    if loaded_url != current_url:
                        print(f"[DEBUG] Visiting: {current_url} | History Depth: {len(self.history)}")
                        page.goto(current_url, wait_until="domcontentloaded", timeout=20000)
                        self._wait_for_page_stable(page)
                        loaded_url = current_url
                    else:
                        print(f"[DEBUG] Visiting: {current_url} | History Depth: {len(self.history)}")
                except Exception as e:
                    print(f"[engine] Load failed for {current_url}: {e}")
                    dead_end_pages.append(current_url)
                    if len(url_stack) > 1:
                        url_stack.pop()
                        current_url = url_stack[-1]
                        loaded_url = ""
                        print(f"[engine] Executing action: BACK -> {current_url}")
                        continue
                    continue

                page_snippet = self._extract_context_snippet(page)
                discovered_elements = self._discover_navigation_elements(page, current_url, extraction_goal)
                filtered_discovered = list(discovered_elements)

                self.page_turn_counts[current_url] = self.page_turn_counts.get(current_url, 0) + 1
                current_turns = self.page_turn_counts[current_url]
                current_attempted = self.attempted_actions.setdefault(current_url, set())
                visited_urls = {str(entry.get("url", "")).strip() for entry in visited_pages if isinstance(entry, dict)}

                if current_turns > STUCK_TURN_THRESHOLD:
                    forced_url = self._force_sitemap_jump(
                        sitemap_tree=sitemap_tree,
                        current_url=current_url,
                        extraction_goal=extraction_goal,
                        visited_urls=visited_urls,
                        attempted_on_page=current_attempted,
                    )
                    if forced_url:
                        print(f"[engine] Stuck on {current_url} for {current_turns} turns; forcing sitemap jump -> {forced_url}")
                        current_attempted.add(f"navigate::{forced_url}")
                        self.history.append(current_url)
                        url_stack.append(forced_url)
                        current_url = forced_url
                        loaded_url = ""
                        continue

                print("[engine] Sending vision prompt to LLM")
                screenshot_b64 = self._capture_screenshot_b64(page)
                filtered_elements = sorted(
                    filtered_discovered,
                    key=lambda x: x.get("semantic_score", 0),
                    reverse=True,
                )[:NAVIGATION_ELEMENT_LIMIT]

                evaluation = self._evaluate_current_page(
                    extraction_goal,
                    goal_fields,
                    page_snippet,
                    filtered_elements,
                    result_limit,
                    screenshot_b64,
                    sitemap_tree,
                    current_url,
                    list(url_stack),
                    attempted_actions=sorted(current_attempted),
                    stuck_turns_current_url=current_turns,
                )

                priority_queue = evaluation.get("priority_queue", []) if isinstance(evaluation, dict) else []
                first_action = str(priority_queue[0].get("action", "")) if priority_queue else ""
                llm_decision = str(evaluation.get("decision", "continue") if isinstance(evaluation, dict) else "continue")
                llm_action = first_action or llm_decision
                print(f"[DEBUG] LLM Decision: {llm_decision} | Action: {llm_action}")

                page_record = {
                    "url": current_url,
                    "decision": evaluation.get("decision", "continue"),
                    "terminal_page": bool(evaluation.get("terminal_page", False)),
                    "priority_queue": evaluation.get("priority_queue", []),
                    "page_index": len(visited_pages) + 1,
                    "goal_fields": goal_fields,
                    "url_stack": list(url_stack),
                }
                visited_pages.append(page_record)

                if page_record["terminal_page"]:
                    terminal_pages.append(current_url)
                    break

                executed = False
                backtracked = False
                for candidate in (page_record["priority_queue"] or []):
                    action = str(candidate.get("action", "click")).strip().lower()
                    if action == "back":
                        back_key = self._action_key("back")
                        if back_key in current_attempted:
                            continue
                        print("[engine] Executing action: BACK")
                        previous_url = self._go_back_with_history(page, url_stack[-2] if len(url_stack) > 1 else current_url)
                        if previous_url and previous_url != current_url:
                            current_url = previous_url
                            if len(url_stack) > 1:
                                url_stack.pop()
                            loaded_url = current_url
                            executed = True
                            backtracked = True
                        else:
                            current_attempted.add(back_key)
                            dead_end_pages.append(current_url)
                        break

                    matching_element = next((item for item in filtered_elements if item.get("id") == int(candidate.get("id", -1))), None)
                    if not isinstance(matching_element, dict):
                        continue

                    action_key = self._action_key(action, matching_element)
                    if action_key in current_attempted:
                        continue
                    current_attempted.add(action_key)

                    print(
                        f"[engine] Executing action: {action.upper()} "
                        f"[{str(matching_element.get('label') or matching_element.get('text') or matching_element.get('href') or '').strip()}]"
                    )
                    next_url = self._try_execute_navigation_action(
                        page,
                        current_url,
                        matching_element,
                        extraction_goal,
                        goal_fields,
                        action=action,
                    )
                    executed = True
                    if next_url:
                        normalized_next = self._normalize_root_url(next_url)
                        if normalized_next and normalized_next != current_url:
                            self.history.append(current_url)
                            url_stack.append(normalized_next)
                            current_url = normalized_next
                            loaded_url = ""
                        else:
                            print(f"[DEBUG] Action Failed/Success: no movement after {action_key}")
                    else:
                        print(f"[DEBUG] Action Failed/Success: failed execution {action_key}")
                    break

                if backtracked:
                    continue

                if not executed:
                    dead_end_pages.append(current_url)
                    break

            browser.close()

        return {
            "start_url": start_url,
            "sitemap_tree": sitemap_tree,
            "goal_fields": goal_fields,
            "visited_pages": visited_pages,
            "terminal_pages": terminal_pages,
            "dead_end_pages": dead_end_pages,
            "url_stack": url_stack,
            "seed_urls": [start_url],
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

def scrape_url(url: str, strategy: ScrapeStrategy = "single", max_pages: int = 3, max_scrolls: int = 3, target_selector: str | None = None) -> ScrapeResult:
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