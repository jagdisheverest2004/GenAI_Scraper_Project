import os
import sys
import asyncio
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def _ensure_windows_proactor_policy():
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

def clean_html(url: str) -> str:
    """
    Fetches a URL via Playwright; strips <script>, <style>, <head>, and <img>; 
    returns a cleaned structure of <div>, <p>, <a>, and <button>.
    """
    # Fetch content with Playwright
    _ensure_windows_proactor_policy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            html_content = page.content()
        except Exception as e:
            print(f"[MCP TOOL: Cleaner] Error fetching URL: {e}")
            html_content = ""
        finally:
            browser.close()

    if not html_content:
        return ""

    # Clean with BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Strip unwanted tags
    for tag in soup(["script", "style", "head", "img", "svg", "noscript", "iframe"]):
        tag.decompose()
        
    # We can also keep only div, p, a, button, and structural tags
    allowed_tags = {"html", "body", "div", "p", "a", "button", "ul", "li", "span", "h1", "h2", "h3", "h4", "h5", "h6", "main", "article", "section"}
    for tag in soup.find_all(True):
        if tag.name not in allowed_tags:
            tag.unwrap() # Removes the tag but keeps its contents

    cleaned_text = soup.prettify()
    body_len = len(cleaned_text)
    
    # Save to resources (MCP Resources)
    os.makedirs("resources", exist_ok=True)
    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()
    resource_path = f"resources/{url_hash}.html"
    with open(resource_path, "w", encoding="utf-8") as f:
        f.write(cleaned_text)

    print(f"[MCP TOOL: Cleaner] URL: {url} | Body text length: {body_len}")
    
    return cleaned_text
