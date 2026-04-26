import os
import sys
import asyncio
import json
import urllib.parse
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


def run_unstructure_agent(url: str, unstructure_def: List[FieldDef]):
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

            nav_prompt = f"""
            We are still missing facts: {missing_fields}.
            Look at the following HTML snippet and find a navigation link that might contain this info (e.g. 'About Us', 'Contact').
            Return JSON: {{"target_href": "/link"}} or {{"target_href": null}}
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
