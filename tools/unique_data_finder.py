import os
import sys
import asyncio
import json
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def _ensure_windows_proactor_policy():
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

def find_unique_data(start_url: str, goal: str, max_pages: int = 10) -> str:
    """
    Implements DFS with a memory stack. Visits pages, checks if data is present, 
    and navigates links.
    Rule: Never visit the same URL more than 3 times; backtrack if a page is a dead end.
    """
    url_stack = [start_url]
    visited_counts = {}
    extracted_data = None
    
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    from groq import Groq
    groq_client = Groq(api_key=api_key)
    
    _ensure_windows_proactor_policy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        while url_stack and len(visited_counts) < max_pages:
            current_url = url_stack[-1]
            
            # Rule: Never visit the same URL more than 3 times
            if visited_counts.get(current_url, 0) >= 3:
                url_stack.pop() # Backtrack
                continue
                
            visited_counts[current_url] = visited_counts.get(current_url, 0) + 1
            print(f"[MCP TOOL: Unique Finder] Navigated to: {current_url}")
            
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=20000)
                # Scroll a bit to trigger lazy loading if needed
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                
                content = page.content()
                soup = BeautifulSoup(content, "html.parser")
                for tag in soup(["script", "style", "img", "svg", "noscript"]):
                    tag.decompose()
                text_content = " ".join(soup.stripped_strings)[:4000] # Take first 4000 chars for LLM
                
                # Check if data is present
                prompt = f"""
                Goal: {goal}
                Does the following page text contain the specific unique data requested in the goal?
                If YES, extract the data and return it in 'extracted_data'.
                If NO, suggest the best link text to click to find the data from the page, or return 'BACKTRACK' to go back.
                
                Return a JSON with the following structure:
                {{
                    "found": true or false,
                    "extracted_data": "The extracted data if found, else null",
                    "next_action": "click", "backtrack", or "none",
                    "link_text": "Text of the link to click if next_action is click"
                }}
                
                Page Text: {text_content}
                """
                
                response = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    response_format={"type": "json_object"}
                )
                
                result = json.loads(response.choices[0].message.content)
                
                if result.get("found") and result.get("extracted_data"):
                    extracted_data = result.get("extracted_data")
                    print(f"[MCP TOOL: Unique Finder] Found data: {extracted_data}")
                    break
                    
                if result.get("next_action") == "click" and result.get("link_text"):
                    link_text = result.get("link_text")
                    try:
                        # Use force: true and scroll_into_view_if_needed()
                        locator = page.get_by_role("link", name=link_text).first
                        if locator.count() == 0:
                            locator = page.locator(f"text={link_text}").first
                        
                        locator.scroll_into_view_if_needed(timeout=5000)
                        locator.click(force=True, timeout=5000)
                        page.wait_for_load_state("domcontentloaded")
                        
                        new_url = page.url
                        if new_url != current_url:
                            url_stack.append(new_url)
                        else:
                            url_stack.pop() # Dead end, backtrack
                    except Exception as e:
                        print(f"[MCP TOOL: Unique Finder] Failed to click link: {e}")
                        url_stack.pop() # Backtrack on failure
                else:
                    url_stack.pop() # Backtrack
                    
            except Exception as e:
                print(f"[MCP TOOL: Unique Finder] Error processing {current_url}: {e}")
                url_stack.pop()
                
        browser.close()
        
    return extracted_data or "Could not find the requested unique data."
