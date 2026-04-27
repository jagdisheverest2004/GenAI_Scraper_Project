import os
import re
import json
import time
from groq import Groq


def _strip_fences(text: str) -> str:
    text = re.sub(r'^```[a-zA-Z]*\n?', '', text.strip())
    text = re.sub(r'\n?```$', '', text.strip())
    return text.strip()


def _escape_html(value: object) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_local_html(parsed: dict, goal: str) -> str:
    facts_data = parsed.get("facts_data") or {}
    list_data = parsed.get("list_data") or []
    filter_hint = parsed.get("filter_hint", "")

    parts = [
        "<style>",
        ".fallback-wrap{font-family:inherit;color:#e2e8f0;background:#0f172a;border:1px solid #1e3a5f;border-radius:14px;padding:20px}",
        ".fallback-title{font-size:18px;font-weight:700;margin:0 0 12px}",
        ".fallback-sub{color:#94a3b8;font-size:13px;margin:0 0 16px}",
        ".fallback-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}",
        ".fallback-card{background:#1e2535;border:1px solid #284064;border-radius:12px;padding:14px}",
        ".fallback-card h3{margin:0 0 8px;font-size:15px;color:#fff}",
        ".fallback-card p{margin:0;color:#cbd5e1;font-size:13px;line-height:1.5}",
        ".fallback-kv{display:grid;gap:10px}",
        ".fallback-item{display:flex;gap:10px;align-items:flex-start}",
        ".fallback-key{min-width:140px;color:#7dd3fc;font-weight:600}",
        ".fallback-val{color:#e2e8f0;word-break:break-word}",
        "</style>",
        f"<div class='fallback-wrap'>",
        f"<div class='fallback-title'>Extraction Result</div>",
        f"<div class='fallback-sub'>Goal: {_escape_html(goal)}" + (f" | Filter: {_escape_html(filter_hint)}" if filter_hint else "") + "</div>",
    ]

    if facts_data:
        parts.append("<div class='fallback-kv'>")
        for key, value in facts_data.items():
            parts.append(
                f"<div class='fallback-item'><div class='fallback-key'>{_escape_html(key)}</div><div class='fallback-val'>{_escape_html(value)}</div></div>"
            )
        parts.append("</div>")

    if list_data:
        parts.append("<div class='fallback-grid' style='margin-top:16px'>")
        for item in list_data:
            title = item.get("title") or item.get("name") or item.get("technology") or item.get("book") or "Item"
            parts.append("<div class='fallback-card'>")
            parts.append(f"<h3>{_escape_html(title)}</h3>")
            for key, value in item.items():
                if key == "title" or key == "name":
                    continue
                if value in [None, ""]:
                    continue
                parts.append(f"<p><strong>{_escape_html(key)}:</strong> {_escape_html(value)}</p>")
            parts.append("</div>")
        parts.append("</div>")

    if not facts_data and not list_data:
        parts.append("<div style='padding:20px;text-align:center;color:#cbd5e1'>No results</div>")

    parts.append("</div>")
    return "\n".join(parts)


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

    # Deterministic fallback: if the scraper already produced usable data, render it directly.
    try:
        parsed = json.loads(raw_data)
        if parsed.get("facts_data") or parsed.get("list_data"):
            return _build_local_html(parsed, goal)
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
    rendered = str(response.choices[0].message.content).strip()
    if "No results" in rendered:
        try:
            parsed = json.loads(data)
            if parsed.get("facts_data") or parsed.get("list_data"):
                return _build_local_html(parsed, goal)
        except Exception:
            pass
    return rendered
