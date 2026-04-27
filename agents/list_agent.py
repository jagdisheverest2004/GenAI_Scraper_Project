import os
import sys
import asyncio
import json
import urllib.parse
import re
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

    card_keywords = ['post', 'card', 'item', 'article', 'entry', 'product', 'result', 'news', 'blog', 'tech', 'tool', 'partner', 'logo', 'stack']
    nav_blacklist = ['menu', 'nav', 'navbar', 'header', 'footer', 'breadcrumb', 'submenu']
    candidates = []
    for tag in soup.find_all(True, class_=True):
        classes = ' '.join(tag.get('class', [])).lower()
        if any(kw in classes for kw in nav_blacklist):
            continue
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


def _is_textual_field(field_name: str) -> bool:
    n = field_name.lower().strip()
    keywords = [
        "technology", "tech", "tool", "platform", "title", "name", "headline", "label",
        "summary", "description", "category", "company", "vendor"
    ]
    return any(k in n for k in keywords)


def _clean_candidate_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', str(text or '').strip())
    text = re.sub(r'\.(png|jpg|jpeg|svg|webp)$', '', text, flags=re.IGNORECASE)
    return text.strip(' -|_')


def _is_valid_candidate(text: str) -> bool:
    t = _clean_candidate_text(text)
    if not t:
        return False
    if len(t) < 2 or len(t) > 80:
        return False
    blocked = {
        "home", "about", "about us", "contact", "contact us", "read more", "learn more", "menu",
        "search", "submit", "next", "previous", "view", "click here", "logo", "image"
    }
    if t.lower() in blocked:
        return False
    if re.fullmatch(r'https?://[^\s]+', t.lower()):
        return False
    return True


def _extract_category_hint(filter_hint: str) -> str:
    fh = (filter_hint or "").strip().lower()
    if not fh:
        return ""

    patterns = [
        r'category\s+is\s+([a-z0-9 &\-]+)',
        r'category\s*:\s*([a-z0-9 &\-]+)',
        r'in\s+the\s+([a-z0-9 &\-]+)\s+category',
        r'under\s+([a-z0-9 &\-]+)\s+category',
    ]
    for pattern in patterns:
        match = re.search(pattern, fh)
        if match:
            return _clean_candidate_text(match.group(1))

    return ""


def _score_category_link(anchor_text: str, href: str, category_hint: str) -> int:
    if not category_hint:
        return 0
    hint = category_hint.lower().strip()
    text = (anchor_text or "").lower()
    link = (href or "").lower()

    score = 0
    if hint in text:
        score += 10
    if hint in link:
        score += 10
    if any(token in text for token in hint.split()):
        score += 3
    if any(token in link for token in hint.split()):
        score += 3
    return score


def _find_best_category_link(page_url: str, soup: BeautifulSoup, category_hint: str):
    if not category_hint:
        return None

    best = None
    best_score = 0
    category_slug = re.sub(r'[^a-z0-9]+', '-', category_hint.lower()).strip('-')

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        absolute = urllib.parse.urljoin(page_url, href)
        text = _clean_candidate_text(a.get_text(separator=" ", strip=True))
        score = _score_category_link(text, absolute, category_hint)

        if category_slug and category_slug in absolute.lower():
            score += 8

        if score > best_score:
            best_score = score
            best = absolute

    return best if best_score >= 6 else None


def _extract_specific_item_hint(filter_hint: str) -> str:
    fh = (filter_hint or "").strip()
    if not fh:
        return ""

    quoted = re.search(r'"([^"]{3,200})"', fh)
    if quoted:
        return _clean_candidate_text(quoted.group(1))

    single_quoted = re.search(r"'([^']{3,200})'", fh)
    if single_quoted:
        return _clean_candidate_text(single_quoted.group(1))

    patterns = [
        r'book\s+name\s+is\s+([a-z0-9:,&()\-\s]{3,200})',
        r'product\s+name\s+is\s+([a-z0-9:,&()\-\s]{3,200})',
        r'book\s+title\s+is\s+([a-z0-9:,&()\-\s]{3,200})',
        r'title\s+is\s+([a-z0-9:,&()\-\s]{3,200})',
        r'name\s+is\s+([a-z0-9:,&()\-\s]{3,200})',
    ]
    for pattern in patterns:
        match = re.search(pattern, fh, flags=re.IGNORECASE)
        if match:
            return _clean_candidate_text(match.group(1))

    return ""


def _find_best_item_link(page_url: str, soup: BeautifulSoup, item_hint: str):
    if not item_hint:
        return None

    hint = item_hint.lower()
    hint_tokens = [token for token in re.findall(r'[a-z0-9]+', hint) if len(token) > 2]
    best = None
    best_score = 0

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        absolute = urllib.parse.urljoin(page_url, href)
        text = _clean_candidate_text(a.get_text(separator=" ", strip=True))
        title_attr = _clean_candidate_text(a.get("title", ""))
        haystack = f"{text} {title_attr} {absolute}".lower()

        score = 0
        if hint in haystack:
            score += 20
        for token in hint_tokens:
            if token in haystack:
                score += 2
        if any(k in absolute.lower() for k in ["catalogue", "book", "product"]) :
            score += 1

        if score > best_score:
            best_score = score
            best = absolute

    return best if best_score >= 6 else None


def _extract_detail_page_values(page_html: str, requested_fields: list):
    soup = BeautifulSoup(page_html, "html.parser")
    data = {}

    title_node = soup.select_one("div.product_main h1, h1")
    price_node = soup.select_one("p.price_color")
    stock_node = soup.select_one("p.instock.availability")
    desc_node = None

    desc_heading = soup.select_one("#product_description")
    if desc_heading:
        desc_node = desc_heading.find_next("p")
    if not desc_node:
        desc_node = soup.select_one("article.product_page p")

    page_title = _clean_candidate_text(title_node.get_text(separator=" ", strip=True) if title_node else "")
    page_price = _clean_candidate_text(price_node.get_text(separator=" ", strip=True) if price_node else "")
    page_desc = _clean_candidate_text(desc_node.get_text(separator=" ", strip=True) if desc_node else "")
    page_stock = _clean_candidate_text(stock_node.get_text(separator=" ", strip=True) if stock_node else "")

    for field_name in requested_fields:
        n = field_name.lower().strip()
        if n in ["title", "name", "book name"]:
            data[field_name] = page_title
        elif "price" in n:
            data[field_name] = page_price
        elif "description" in n or "summary" in n:
            data[field_name] = page_desc
        elif "stock" in n or "availability" in n:
            data[field_name] = page_stock

    return {k: v for k, v in data.items() if v}


def _heuristic_extract_items(page_html: str, structure_def: StructureDef, item_limit: int):
    """Fallback extractor used when LLM selectors fail to produce usable items."""
    soup = BeautifulSoup(page_html, "html.parser")
    requested_fields = [f.name for f in structure_def.fields]
    if not requested_fields:
        return []

    primary = requested_fields[0]
    if not _is_textual_field(primary):
        return []

    candidates = []
    seen = set()

    # 1) Logo-heavy stacks usually expose meaningful names in image alt/title.
    for img in soup.select("img[alt], img[title]"):
        value = img.get("alt", "") or img.get("title", "")
        value = _clean_candidate_text(value)
        key = value.lower()
        if _is_valid_candidate(value) and key not in seen:
            seen.add(key)
            candidates.append({primary: value})
            if len(candidates) >= item_limit:
                return candidates

    # 2) Card-style capability blocks: capture title + short description when present.
    card_selectors = [
        ".elementor-cta",
        ".elementor-cta__content",
        "[class*='card']",
        "[class*='service']",
        "[class*='feature']",
        "[class*='technology']",
    ]
    for sel in card_selectors:
        for card in soup.select(sel):
            title_node = card.select_one(".elementor-cta__title, h1, h2, h3, h4, strong")
            desc_node = card.select_one(".elementor-cta__description, p")
            title_val = _clean_candidate_text(title_node.get_text(separator=" ", strip=True) if title_node else "")
            desc_val = _clean_candidate_text(desc_node.get_text(separator=" ", strip=True) if desc_node else "")
            key = title_val.lower()
            if _is_valid_candidate(title_val) and key not in seen:
                seen.add(key)
                row = {primary: title_val}
                if desc_val and len(desc_val) > 20:
                    row["description"] = desc_val
                candidates.append(row)
                if len(candidates) >= item_limit:
                    return candidates

    # 3) Fallback to visible heading-like text blocks inside likely technology/toolkit sections.
    selectors = [
        ".elementor-tab-title",
        ".elementor-cta__title",
        ".elementor-heading-title",
        "[class*='tech'] [class*='title']",
        "[class*='tool'] [class*='title']",
    ]
    for sel in selectors:
        for node in soup.select(sel):
            value = _clean_candidate_text(node.get_text(separator=" ", strip=True))
            key = value.lower()
            if _is_valid_candidate(value) and key not in seen:
                seen.add(key)
                candidates.append({primary: value})
                if len(candidates) >= item_limit:
                    return candidates

    return candidates


def run_structure_agent(url: str, structure_def: StructureDef, max_pages: int = 50, item_limit: int = 10, filter_hint: str = ""):
    if not structure_def:
        return []

    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    from groq import Groq
    client = Groq(api_key=api_key)

    extracted_items = []
    category_hint = _extract_category_hint(filter_hint)

    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    print(f"\n[List Agent] ══ Starting extraction: entity='{structure_def.entity}', scan_limit={item_limit} ══")
    specific_item_hint = _extract_specific_item_hint(filter_hint)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        if not safe_goto(page, url):
            print("[List Agent] ✗  Could not load initial URL. Aborting.")
            browser.close()
            return []

        if category_hint:
            print(f"[List Agent] ↪  Category hint detected: '{category_hint}'")
        if specific_item_hint:
            print(f"[List Agent] ↪  Specific item hint detected: '{specific_item_hint}'")

        # If the query names a specific book/product, try to jump to that exact detail page first.
        if specific_item_hint:
            homepage_soup = BeautifulSoup(page.content(), "html.parser")
            item_url = _find_best_item_link(page.url, homepage_soup, specific_item_hint)
            if item_url and page.url.rstrip('/') != item_url.rstrip('/'):
                print(f"[List Agent] ↪  Navigating to specific item page: {item_url}")
                if safe_goto(page, item_url):
                    detail_values = _extract_detail_page_values(page.content(), [f.name for f in structure_def.fields])
                    if detail_values:
                        extracted_items.append(detail_values)
                        print(f"[List Agent] ✓  Extracted detail-page data directly: {detail_values}")
                        browser.close()
                        print(f"\n[List Agent] ══ Done. Extracted {len(extracted_items)} items total. ══\n")
                        return extracted_items

        navigation_steps = 0
        recipe = None

        while navigation_steps < 3:
            previous_url = page.url
            html = page.content()
            nav_snippet = clean_html_for_nav(html)
            card_snippet = find_card_snippet(html)

            if category_hint and navigation_steps == 0:
                soup = BeautifulSoup(html, "html.parser")
                category_url = _find_best_category_link(page.url, soup, category_hint)
                if category_url and page.url.rstrip('/') != category_url.rstrip('/'):
                    print(f"[List Agent] ↪  Navigating to category page for '{category_hint}': {category_url}")
                    if safe_goto(page, category_url):
                        navigation_steps += 1
                        continue

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
                if category_hint:
                    soup = BeautifulSoup(html, "html.parser")
                    category_url = _find_best_category_link(page.url, soup, category_hint)
                    if category_url and page.url.rstrip('/') != category_url.rstrip('/'):
                        print(f"[List Agent] ↪  Category override selected: {category_url}")
                        if safe_goto(page, category_url):
                            navigation_steps += 1
                            continue

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
                - Return ONLY these fields exactly: {[f.name for f in structure_def.fields]}
                - Do not add extra fields
                - Avoid header/menu/navigation selectors (e.g. .menu, .nav, .header, .footer)
                - Prefer selectors that target repeating content cards/logos/listings that contain actual entity data

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
        
        if isinstance(recipe, list):
            print("[List Agent] ⚠ LLM returned a list. Grabbing the first recipe.")
            recipe = recipe[0]
        
        
        container_sel = recipe.get("container_selector")
        llm_fields_map = recipe.get("fields", {})
        next_sel = recipe.get("next_page_selector")

        requested_fields = [f.name for f in structure_def.fields]
        can_enrich_description = len(requested_fields) == 1 and _is_textual_field(requested_fields[0])
        fields_map = {}
        for field_name in requested_fields:
            if field_name in llm_fields_map:
                fields_map[field_name] = llm_fields_map[field_name]

        # If the LLM returned aliases instead of requested field names, map a best effort alias.
        if len(requested_fields) == 1 and not fields_map:
            only_field = requested_fields[0]
            for alias in ["technology", "title", "name", "label", "tool", "platform"]:
                if alias in llm_fields_map:
                    fields_map[only_field] = llm_fields_map[alias]
                    break

        if not container_sel:
            print("[List Agent] ✗  No container_selector in recipe. Aborting.")
            browser.close()
            return []

        if not fields_map:
            print("[List Agent] ✗  Recipe has no usable requested fields. Aborting.")
            browser.close()
            return []

        print(f"\n[List Agent] ══ Extracting data: selector='{container_sel}' ══")

        pages_visited = 0
        seen_rows = set()
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
                        if attr == "text":
                            val = el.get_text(separator=" ", strip=True)
                        else:
                            val = el.get(attr, "")
                            # If LLM picks src/href for a textual field, recover via alt/title/text.
                            if (not str(val).strip()) or (_is_textual_field(field_name) and attr in ["src", "href"]):
                                val = el.get("alt", "") or el.get("title", "") or el.get_text(separator=" ", strip=True)
                        if isinstance(val, list):
                            val = val[-1] if val else ""
                        item_data[field_name] = _clean_candidate_text(str(val).strip())
                    elif attr == "text" or _is_textual_field(field_name):
                        # Text fallback helps when selectors are slightly off but container is correct.
                        item_data[field_name] = _clean_candidate_text(container.get_text(separator=" ", strip=True))

                if can_enrich_description and item_data.get(requested_fields[0], ""):
                    desc_node = container.select_one(".elementor-cta__description, p")
                    if desc_node:
                        desc_val = _clean_candidate_text(desc_node.get_text(separator=" ", strip=True))
                        if len(desc_val) > 20:
                            item_data["description"] = desc_val

                has_requested_value = any(str(item_data.get(name, "")).strip() for name in requested_fields)
                row_key = tuple(str(item_data.get(name, "")).strip().lower() for name in requested_fields)

                if has_requested_value and row_key not in seen_rows:
                    seen_rows.add(row_key)
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

        if not extracted_items:
            print("[List Agent] ⚠  LLM extraction returned 0 items. Running heuristic fallback extractor...")
            fallback_items = _heuristic_extract_items(page.content(), structure_def, item_limit)
            if fallback_items:
                extracted_items.extend(fallback_items)
                print(f"[List Agent] ✓  Heuristic fallback recovered {len(fallback_items)} items")
            else:
                print("[List Agent] ✗  Heuristic fallback also found no valid items")

        browser.close()

    print(f"\n[List Agent] ══ Done. Extracted {len(extracted_items)} items total. ══\n")
    return extracted_items
