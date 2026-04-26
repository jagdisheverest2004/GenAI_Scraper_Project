import os
import sys
import asyncio
import json
import urllib.parse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page
from app.processor import StructureDef


def safe_goto(page: Page, url: str):
    print(f"[List Agent] ➜  Navigating to: {url}")
    for strategy in ["domcontentloaded", "commit"]:
        try:
            page.goto(url, wait_until=strategy, timeout=30000)
            page.wait_for_timeout(2500)
            print(f"[List Agent] ✓  Page loaded ({strategy}): {page.url}")
            return True
        except Exception as e:
            print(f"[List Agent] ✗  Strategy '{strategy}' failed: {e}")
    print(f"[List Agent] ✗  All navigation strategies failed for {url}")
    return False


def clean_html_for_nav(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript", "meta", "head"]):
        tag.decompose()
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in ['class', 'href', 'src']}
    return str(soup.body)[:10000] if soup.body else ""


def find_card_snippet(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript", "meta", "head"]):
        tag.decompose()
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in ['class', 'href', 'src']}

    card_keywords = ['post', 'card', 'item', 'article', 'entry', 'product', 'result', 'news', 'blog']
    candidates = []
    for tag in soup.find_all(True, class_=True):
        classes = ' '.join(tag.get('class', [])).lower()
        if any(kw in classes for kw in card_keywords):
            text = tag.get_text(strip=True)
            if 20 < len(text) < 2000:
                candidates.append(tag)

    if candidates:
        seen = set()
        unique = []
        for c in candidates:
            key = c.get('class', [''])[0] if c.get('class') else ''
            if key not in seen:
                seen.add(key)
                unique.append(str(c))
        snippet = '\n'.join(unique[:6])
        print(f"[List Agent] ✓  Card detector found {len(candidates)} candidates, {len(unique)} unique classes")
        return snippet[:8000]

    print("[List Agent] ⚠  Card detector found nothing, falling back to flat 10k slice")
    return str(soup.body)[:10000] if soup.body else ""


def run_structure_agent(url: str, structure_def: StructureDef, max_pages: int = 50, item_limit: int = 10):
    if not structure_def:
        return []

    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    from groq import Groq
    client = Groq(api_key=api_key)

    extracted_items = []

    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    print(f"\n[List Agent] ══ Starting extraction: entity='{structure_def.entity}', scan_limit={item_limit} ══")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        if not safe_goto(page, url):
            print("[List Agent] ✗  Could not load initial URL. Aborting.")
            browser.close()
            return []

        navigation_steps = 0
        recipe = None

        while navigation_steps < 3:
            previous_url = page.url
            html = page.content()
            nav_snippet = clean_html_for_nav(html)
            card_snippet = find_card_snippet(html)

            print(f"\n[List Agent] ── Step {navigation_steps + 1}: Nav/Extract decision (URL: {page.url}) ──")

            nav_prompt = f"""
            Goal: Extract repeating items of type '{structure_def.entity}'.
            Current Page URL: {page.url}

            Do NOT navigate to the current page again.
            Choose the BEST action and return a JSON response:

            1. If card/post/list-like items matching the goal are ALREADY visible on this page -> 'extract'
               Return JSON: {{"action": "extract"}}

            2. If a specific navigation link leads to a page listing the items -> 'navigate'
               Return JSON: {{"action": "navigate", "target_href": "/path"}}

            3. If this is a homepage/landing page with a SEARCH BAR and searching would give better results -> 'search'
               Return JSON: {{"action": "search", "search_query": "what to search for", "search_selector": "CSS selector for the search input"}}
               Example search_selectors: "#twotabsearchtextbox", "input[name='q']", "input[type='search']"

            HTML Snippet:
            {nav_snippet}
            """

            nav_resp = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": nav_prompt}],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            nav_decision = json.loads(nav_resp.choices[0].message.content)
            print(f"[List Agent] LLM Nav Decision: {nav_decision}")

            if nav_decision.get("action") == "navigate":
                target_href = nav_decision.get("target_href")
                if target_href:
                    absolute_url = urllib.parse.urljoin(page.url, target_href)
                    if not safe_goto(page, absolute_url):
                        print("[List Agent] ✗  Navigation failed. Forcing extract from current page.")
                        break
                    if page.url.rstrip('/') == previous_url.rstrip('/'):
                        print("[List Agent] ⚠  URL unchanged after navigation. Forcing extract.")
                        break
                    navigation_steps += 1
                    continue
                else:
                    print("[List Agent] ⚠  Navigate chosen but no href returned. Forcing extract.")
                    break

            elif nav_decision.get("action") == "search":
                search_query = nav_decision.get("search_query", structure_def.entity)
                search_selector = nav_decision.get("search_selector", "input[type='search'], input[name='q'], input[type='text']")
                print(f"[List Agent] 🔍  Searching for '{search_query}' using selector '{search_selector}'")
                try:
                    page.wait_for_selector(search_selector.split(',')[0].strip(), timeout=8000)
                    page.fill(search_selector.split(',')[0].strip(), search_query)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    print(f"[List Agent] ✓  Search complete. Now on: {page.url}")
                    navigation_steps += 1
                    continue
                except Exception as e:
                    print(f"[List Agent] ✗  Search action failed: {e}. Forcing extract.")
                    break

            else:
                print(f"\n[List Agent] ── Generating CSS recipe from card snippet ──")
                recipe_prompt = f"""
                Goal: Extract '{structure_def.entity}' items with fields: {[f.name for f in structure_def.fields]}.

                Generate CSS selectors from these card/post HTML elements.

                IMPORTANT ATTR RULES:
                - Fields named 'title', 'name', 'book name', 'headline', 'label' → ALWAYS use attr: "text"
                - Fields named 'link', 'url', 'href' → use attr: "href"
                - Fields named 'image' → use attr: "src"
                - All other fields (price, date, rating, summary, etc.) → use attr: "text"
                - NEVER use attr: "textContent" or attr: "class" — these are not valid

                Return JSON:
                {{
                    "action": "extract",
                    "container_selector": "CSS selector for each repeating container element",
                    "fields": {{
                        "field_name": {{"selector": "relative CSS selector inside container", "attr": "text or href or src"}}
                    }},
                    "next_page_selector": null
                }}

                Card Elements:
                {card_snippet}
                """

                recipe_resp = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": recipe_prompt}],
                    temperature=0.1,
                    response_format={"type": "json_object"}
                )
                recipe = json.loads(recipe_resp.choices[0].message.content)
                print(f"[List Agent] LLM Recipe:\n{json.dumps(recipe, indent=2)}")
                break

        if not recipe:
            print("[List Agent] ✗  No recipe generated. Aborting.")
            browser.close()
            return []

        container_sel = recipe.get("container_selector")
        fields_map = recipe.get("fields", {})
        next_sel = recipe.get("next_page_selector")

        if not container_sel:
            print("[List Agent] ✗  No container_selector in recipe. Aborting.")
            browser.close()
            return []

        print(f"\n[List Agent] ══ Extracting data: selector='{container_sel}' ══")

        pages_visited = 0
        while pages_visited < max_pages:
            pages_visited += 1
            current_soup = BeautifulSoup(page.content(), "html.parser")
            containers = current_soup.select(container_sel)
            print(f"[List Agent] Page {pages_visited}: found {len(containers)} containers")

            if not containers:
                print(f"[List Agent] ✗  No containers matched '{container_sel}'. Check selector.")
                break

            for container in containers:
                item_data = {}
                for field_name, config in fields_map.items():
                    sel = config.get("selector")
                    attr = config.get("attr", "text")
                    el = container.select_one(sel) if sel else container
                    if el:
                        val = el.get_text(separator=" ", strip=True) if attr == "text" else el.get(attr, "")
                        if isinstance(val, list):
                            val = val[-1] if val else ""
                        item_data[field_name] = val

                if item_data:
                    extracted_items.append(item_data)
                    print(f"[List Agent]   ✓  Item {len(extracted_items)}: {item_data}")
                    if len(extracted_items) >= item_limit:
                        print(f"[List Agent] ✓  Reached item limit ({item_limit}). Stopping early.")
                        break

            if len(extracted_items) >= item_limit:
                break

            if next_sel:
                next_el = page.locator(next_sel).first
                if next_el.count() > 0:
                    try:
                        next_el.scroll_into_view_if_needed(timeout=5000)
                        next_el.click(force=True, timeout=5000)
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                        page.wait_for_timeout(1000)
                        print(f"[List Agent] ➜  Paginated to next page")
                        continue
                    except Exception as e:
                        print(f"[List Agent] ✗  Pagination failed: {e}")
                        break
                else:
                    print("[List Agent] ⚠  Next page selector not found. Stopping pagination.")
                    break
            else:
                break

        browser.close()

    print(f"\n[List Agent] ══ Done. Extracted {len(extracted_items)} items total. ══\n")
    return extracted_items
