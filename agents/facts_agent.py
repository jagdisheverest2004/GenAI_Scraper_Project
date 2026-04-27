import os
import sys
import asyncio
import json
import urllib.parse
import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page
from typing import List
from app.processor import FieldDef


def safe_goto(page: Page, url: str) -> bool:
    print(f"[Facts Agent] ➜  Navigating to: {url}")
    for strategy in ["domcontentloaded", "commit"]:
        try:
            page.goto(url, wait_until=strategy, timeout=30000)
            page.wait_for_timeout(2500)
            print(f"[Facts Agent] ✓  Page loaded ({strategy}): {page.url}")
            return True
        except Exception as e:
            print(f"[Facts Agent] ✗  Strategy '{strategy}' failed: {e}")
    print(f"[Facts Agent] ✗  All navigation strategies failed for {url}")
    return False


def _extract_tokens(user_query: str, missing_fields: List[str]) -> set:
    stop = {
        "who", "is", "what", "where", "the", "a", "an", "and", "or", "for", "with", "from",
        "give", "tell", "me", "his", "her", "their", "role", "domain", "works", "work", "does",
        "did", "details", "information", "about", "profile"
    }
    corpus = f"{user_query} {' '.join(missing_fields)}".lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_-]{2,}", corpus))
    return {t for t in tokens if t not in stop}


def _build_candidate_links(page_url: str, soup: BeautifulSoup, user_query: str, missing_fields: List[str]) -> list:
    tokens = _extract_tokens(user_query, missing_fields)
    candidates = []
    seen = set()

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)

        text = a.get_text(separator=" ", strip=True)
        text_l = text.lower()
        abs_l = absolute.lower()

        score = 0
        for t in tokens:
            if t in abs_l:
                score += 4
            if t in text_l:
                score += 3

        if any(k in abs_l for k in ["team", "member", "leadership", "about", "people", "profile", "bio"]):
            score += 2
        if any(k in text_l for k in ["team", "leadership", "about", "profile", "bio"]):
            score += 2

        if score > 0:
            candidates.append({"href": absolute, "text": text[:120], "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:10]


def run_unstructure_agent(url: str, unstructure_def: List[FieldDef], user_query: str = ""):
    if not unstructure_def:
        return {}

    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    from groq import Groq
    client = Groq(api_key=api_key)

    extracted_data = {}
    missing_fields = [f.name for f in unstructure_def]

    print(f"\n[Facts Agent] ══ Starting extraction: fields={missing_fields} ══")

    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        if not safe_goto(page, url):
            print("[Facts Agent] ✗  Could not load initial URL. Aborting.")
            browser.close()
            return {}

        navigation_steps = 0
        while navigation_steps < 3 and missing_fields:
            print(f"\n[Facts Agent] ── Attempt {navigation_steps + 1}: Extracting from {page.url} ──")
            content = page.content()
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup(["script", "style", "svg", "noscript", "meta", "head"]):
                tag.decompose()
            text_content = soup.get_text(separator=" ", strip=True)[:10000]
            print(f"[Facts Agent]    Text content length: {len(text_content)} chars")

            prompt = f"""
            Extract the following missing singular facts from the text:
            Missing fields: {missing_fields}

            If a fact is found, extract it. If NOT found, set its value to null.
            Return JSON:
            {{
                "extracted": {{"field_name": "value", ...}},
                "all_found": true/false
            }}

            Text:
            {text_content}
            """

            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            result = json.loads(response.choices[0].message.content)
            print(f"[Facts Agent] LLM Extraction Result: {result}")

            for k, v in result.get("extracted", {}).items():
                if v and k in missing_fields:
                    extracted_data[k] = v
                    missing_fields.remove(k)
                    print(f"[Facts Agent] ✓  Found '{k}': {v}")

            if not missing_fields:
                print("[Facts Agent] ✓  All fields found.")
                break

            print(f"[Facts Agent] ⚠  Still missing: {missing_fields}. Looking for a navigation link...")

            candidate_links = _build_candidate_links(page.url, soup, user_query, missing_fields)
            candidate_text = "\n".join(
                [f"- text='{c['text']}' | href='{c['href']}' | score={c['score']}" for c in candidate_links]
            )

            nav_prompt = f"""
            We are still missing facts: {missing_fields}.
            User query context: "{user_query}"
            Choose ONE best candidate link that is most likely to contain the missing facts.
            Return JSON: {{"target_href": "/link-or-absolute-url"}} or {{"target_href": null}}

            Candidate Links:
            {candidate_text}

            HTML Snippet (fallback context):
            HTML Snippet: {str(soup.body)[:8000]}
            """

            nav_res = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": nav_prompt}],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            nav_action = json.loads(nav_res.choices[0].message.content)
            target_href = nav_action.get("target_href")
            print(f"[Facts Agent] LLM Nav Decision: {nav_action}")

            if (not target_href) and candidate_links:
                target_href = candidate_links[0]["href"]
                print(f"[Facts Agent] ⚠  LLM returned null. Using top heuristic candidate: {target_href}")

            if target_href:
                absolute_url = urllib.parse.urljoin(page.url, target_href)
                if safe_goto(page, absolute_url):
                    navigation_steps += 1
                else:
                    break
            else:
                print("[Facts Agent] No nav link found. Stopping.")
                break

        browser.close()

    print(f"\n[Facts Agent] ══ Done. Extracted: {extracted_data} ══\n")
    return extracted_data
