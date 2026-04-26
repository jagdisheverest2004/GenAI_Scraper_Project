import os
import re
import json
import time
from groq import Groq


def _strip_fences(text: str) -> str:
    text = re.sub(r'^```[a-zA-Z]*\n?', '', text.strip())
    text = re.sub(r'\n?```$', '', text.strip())
    return text.strip()


def format_html_output(raw_data: str, goal: str) -> str:
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    if not api_key:
        return "<p>GROQ_API_KEY is not set.</p>"
    client = Groq(api_key=api_key)

    filter_instruction = ""
    try:
        parsed = json.loads(raw_data)
        filter_hint = parsed.get("filter_hint", "")
        output_limit = parsed.get("output_limit", None)
        if filter_hint:
            filter_instruction += f"IMPORTANT FILTER: {filter_hint}. "
        if output_limit:
            filter_instruction += f"Show ONLY the top {output_limit} items."
    except Exception:
        pass

    prompt = f"""
    The user's goal was: "{goal}"
    {filter_instruction}

    Here is the raw scraped data (JSON):
    {raw_data[:5000]}

    Generate a BEAUTIFUL, self-contained HTML component (no <html>/<head>/<body> tags) with embedded <style> to display this data.
    Design rules:
    - Dark theme: card background #1e2535, page background transparent, text #e2e8f0
    - Accent color: #38bdf8 (sky blue)
    - Use CSS Grid or Flexbox for card layout
    - Each item in list_data should be a stylish card
    - facts_data should show as key-value pairs with unicode emoji icons
    - Smooth hover effects on cards (transform: translateY(-4px))
    - Font: inherit from page (no Google Fonts import)
    - Apply any filter/sort conditions and only show qualifying items
    - If no relevant data found, show a friendly "No results" message

    CRITICAL: Your response must start with <style> or <div> directly.
    Do NOT wrap in markdown code fences. Return ONLY raw HTML.
    """

    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return _strip_fences(str(resp.choices[0].message.content).strip())


def format_final_output(raw_data: str, goal: str) -> str:
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    if not api_key:
        return "GROQ_API_KEY is not set."

    client = Groq(api_key=api_key)
    CHUNK_SIZE = 8000

    if len(raw_data) <= CHUNK_SIZE:
        return _ask_llm(client, raw_data, goal)

    chunks = []
    current_chunk = []
    current_length = 0
    for line in raw_data.split('\n'):
        if current_length + len(line) > CHUNK_SIZE and current_chunk:
            chunks.append('\n'.join(current_chunk))
            current_chunk = []
            current_length = 0
        current_chunk.append(line)
        current_length += len(line) + 1
    if current_chunk:
        chunks.append('\n'.join(current_chunk))

    intermediate_results = []
    for idx, chunk in enumerate(chunks):
        prompt = f"""
        Goal: "{goal}"
        Extract and summarize ONLY the information necessary to answer the Goal.
        Raw Data Chunk:
        {chunk}
        """
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            intermediate_results.append(response.choices[0].message.content)
            if idx < len(chunks) - 1:
                time.sleep(2)
        except Exception as e:
            if "rate_limit_exceeded" in str(e):
                time.sleep(20)
                try:
                    response = client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                    )
                    intermediate_results.append(response.choices[0].message.content)
                except Exception:
                    pass

    combined_data = "\n\n---\n\n".join(intermediate_results)
    if len(combined_data) > CHUNK_SIZE * 2:
        combined_data = combined_data[:CHUNK_SIZE * 2]

    return _ask_llm(client, combined_data, goal)


def _ask_llm(client, data: str, goal: str) -> str:
    filter_instruction = ""
    try:
        parsed = json.loads(data)
        filter_hint = parsed.get("filter_hint", "")
        output_limit = parsed.get("output_limit", None)
        if filter_hint:
            filter_instruction += f"\nIMPORTANT FILTER: {filter_hint}."
        if output_limit:
            filter_instruction += f"\nReturn ONLY the top {output_limit} items that satisfy the filter."
    except Exception:
        pass

    prompt = f"""
    The user wanted to achieve the following goal: "{goal}"
    {filter_instruction}

    Here is the extracted data:
    {data}

    Write a detailed natural language summary directly addressing the user's goal.
    Apply any filter/sort conditions. Use a professional structure.
    """

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return str(response.choices[0].message.content).strip()
