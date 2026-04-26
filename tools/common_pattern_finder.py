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

def find_common_patterns(start_url: str, goal: str, max_pages: int = 50) -> str:
    """
    Intelligent Agentic Scraper:
    1. Feeds the first page's HTML to the LLM to generate a CSS Selector extraction recipe.
    2. Uses that recipe to rapidly scrape all subsequent pages manually using BeautifulSoup and Playwright.
    This avoids slow LLM calls on every single page and prevents browser timeouts.
    """
    extracted_items = []
    
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    from groq import Groq
    groq_client = Groq(api_key=api_key)
    
    _ensure_windows_proactor_policy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        try:
            print(f"[MCP TOOL: Common Finder] Navigating to initial page: {start_url}")
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000) # Wait for dynamic rendering
            
            content = page.content()
            soup = BeautifulSoup(content, "html.parser")
            
            # Clean HTML to fit in prompt token limits
            for tag in soup(["script", "style", "svg", "noscript", "meta", "head"]):
                tag.decompose()
            
            # Convert body to string for LLM analysis
            html_snippet = soup.prettify()[:15000] # Provide a large chunk of the DOM
            
            prompt = f"""
            Goal: {goal}
            Analyze the following HTML structure to find the repeating items (like books, products, or news articles) that match the Goal.

            Instead of extracting the data yourself, you must write a JSON configuration that a Python BeautifulSoup scraper can use to extract the data rapidly from this page and all subsequent paginated pages.

            Return a JSON with the following structure:
            {{
                "container_selector": "The CSS selector for the main repeating element (e.g., 'li.product', 'article.product_pod', 'div.card')",
                "fields": {{
                    "field_name_1": {{"selector": "relative css selector inside container", "attr": "text" or "href" or "title" or "src"}},
                    "field_name_2": {{"selector": "...", "attr": "..."}}
                }},
                "next_page_selector": "The CSS selector for the 'Next' pagination link (e.g., 'li.next a', 'a.next-page'). If there is no pagination, return null."
            }}

            For example, to extract a book's title and price from <article class="product_pod">, the fields might be:
            "fields": {{
                "title": {{"selector": "h3 a", "attr": "title"}},
                "price": {{"selector": "p.price_color", "attr": "text"}}
            }}

            HTML Snippet:
            {html_snippet}
            """
            
            print("[MCP TOOL: Common Finder] Asking LLM to generate extraction recipe...")
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            recipe = json.loads(response.choices[0].message.content)
            print(f"[MCP TOOL: Common Finder] LLM Recipe: {json.dumps(recipe, indent=2)}")
            
            container_sel = recipe.get("container_selector")
            fields_map = recipe.get("fields", {})
            next_sel = recipe.get("next_page_selector")
            
            if not container_sel:
                print("[MCP TOOL: Common Finder] LLM failed to provide a container selector.")
                return "Could not determine a pattern to extract data."

            pages_visited = 0
            
            # Fast Local Scraping Loop
            while pages_visited < max_pages:
                pages_visited += 1
                current_url = page.url
                print(f"[MCP TOOL: Common Finder] Scraping page {pages_visited}: {current_url}")
                
                # Re-parse current page
                current_content = page.content()
                current_soup = BeautifulSoup(current_content, "html.parser")
                
                containers = current_soup.select(container_sel)
                if not containers:
                    print("[MCP TOOL: Common Finder] No more containers found on page.")
                    break
                    
                for container in containers:
                    item_data = {}
                    for field, config in fields_map.items():
                        sel = config.get("selector")
                        attr = config.get("attr", "text")
                        
                        el = container.select_one(sel) if sel else container
                        if el:
                            if attr == "text":
                                item_data[field] = el.get_text(separator=" ", strip=True)
                            else:
                                item_data[field] = el.get(attr, "")
                                
                    if item_data:
                        extracted_items.append(json.dumps(item_data))
                
                print(f"[MCP TOOL: Common Finder] Extracted {len(containers)} items from page {pages_visited}.")
                
                # Handle Pagination
                if next_sel:
                    next_el = page.locator(next_sel).first
                    if next_el.count() > 0:
                        try:
                            # Scroll and click
                            next_el.scroll_into_view_if_needed(timeout=5000)
                            next_el.click(force=True, timeout=5000)
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                            page.wait_for_timeout(1000) # Short wait for DOM to settle
                            continue # Loop to next page
                        except Exception as e:
                            print(f"[MCP TOOL: Common Finder] Reached end or failed to click next: {e}")
                            break
                    else:
                        print("[MCP TOOL: Common Finder] Next button not found on page.")
                        break
                else:
                    print("[MCP TOOL: Common Finder] No next_page_selector provided by LLM.")
                    break
                    
        except Exception as e:
            print(f"[MCP TOOL: Common Finder] Fatal error: {e}")
            
        finally:
            browser.close()
            
    if extracted_items:
        return "\n---\n".join(extracted_items)
    return "Could not find the requested common data."
