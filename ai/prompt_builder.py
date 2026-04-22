"""Prompt helpers for Gemini/Llama extraction."""

from __future__ import annotations

from textwrap import dedent


def build_extraction_prompt(extraction_goal: str, page_markdown: str) -> str:
  """Build a strict extraction prompt for Gemini/Llama."""

  print(f"[prompt_builder] Building prompt: extraction_goal_len={len(extraction_goal.strip())}, "f"page_markdown_len={len(page_markdown.strip())}")
  prompt = dedent(
    f"""
    You are a precision data extraction engine.

    Extraction Goal:
    {extraction_goal.strip()}

    Instructions:
    - Use only the provided page content.
    - Preserve the user's intent exactly and infer the best matching fields.
    - Return strict JSON only. Do not wrap the output in markdown.
    - If useful data is missing, include an empty value and explain it in an "errors" array.
    - Include a logic_metadata object so Python can apply the final mathematical filter exactly.
    - Do not apply numeric comparisons in free text; instead describe them in logic_metadata.
    - Keep the response compact, normalized, and machine-readable.

    Suggested JSON shape:
    {{
      "success": true,
      "extraction_goal": "...",
      "source_summary": "...",
      "logic_metadata": {{
        "filter_field": "price",
        "operator": "<",
        "target_value": 2000,
        "result_limit": 10
      }},
      "records": [{{}}],
      "errors": []
    }}

    Page Content:
    {page_markdown.strip()}

    # --- OPTIONAL ADDITION BELOW ---
    Remember: Return ONLY valid JSON matching the exact shape requested above.
    """
  ).strip()
  print(f"[prompt_builder] Prompt built: prompt_len={len(prompt)}")
  return prompt