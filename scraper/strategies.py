"""Navigation strategies for pagination and infinite scroll."""

from __future__ import annotations

import re

from playwright.sync_api import Page


def paginate_pages(page: Page, max_pages: int = 3) -> int:
    """Click a resilient Next control until it disappears or the limit is hit."""

    print(f"[strategies] paginate_pages started: max_pages={max_pages}")
    pages_visited = 1

    for attempt in range(max_pages - 1):
        print(f"[strategies] Pagination attempt {attempt + 1}/{max_pages - 1}")
        next_candidates = [
            page.get_by_role("button", name=re.compile(r"next", re.IGNORECASE)),
            page.get_by_role("link", name=re.compile(r"next", re.IGNORECASE)),
            page.get_by_text(re.compile(r"next", re.IGNORECASE)),
        ]

        clicked = False
        for idx, candidate in enumerate(next_candidates, start=1):
            candidate_count = candidate.count()
            print(f"[strategies] Candidate {idx} count={candidate_count}")
            if candidate.count() == 0:
                continue
            if candidate.first.is_disabled():
                print(f"[strategies] Candidate {idx} is disabled")
                continue
            print(f"[strategies] Clicking candidate {idx}")
            candidate.first.click()
            # Wait for the page to settle after navigation instead of sleeping blindly.
            page.wait_for_load_state("networkidle")
            pages_visited += 1
            print(f"[strategies] Navigation successful; pages_visited={pages_visited}")
            clicked = True
            break

        if not clicked:
            print("[strategies] No clickable next control found; stopping pagination")
            break

    print(f"[strategies] paginate_pages completed: pages_visited={pages_visited}")
    return pages_visited


def scroll_infinite_content(page: Page, max_scrolls: int = 3) -> int:
    """Scroll until content stops growing or the configured limit is reached."""

    print(f"[strategies] scroll_infinite_content started: max_scrolls={max_scrolls}")
    scrolls = 0
    previous_height = page.evaluate("() => document.body.scrollHeight")
    print(f"[strategies] Initial page height={previous_height}")

    for attempt in range(max_scrolls):
        print(f"[strategies] Scroll attempt {attempt + 1}/{max_scrolls}")
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        # Network idle gives dynamic pages time to load newly revealed content.
        page.wait_for_load_state("networkidle")
        current_height = page.evaluate("() => document.body.scrollHeight")
        scrolls += 1
        print(f"[strategies] Scroll complete: current_height={current_height}, scrolls={scrolls}")
        if current_height == previous_height:
            print("[strategies] Page height unchanged; stopping scroll")
            break
        previous_height = current_height

    print(f"[strategies] scroll_infinite_content completed: scrolls={scrolls}")
    return scrolls
