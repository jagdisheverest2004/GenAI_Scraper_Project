"""Prompt helpers for router, navigation, and extraction prompts."""

from __future__ import annotations

import json
import re
from textwrap import dedent


def _extract_query_terms(user_query: str, goal_fields: dict[str, object]) -> set[str]:
    terms = {token for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-_]{2,}", str(user_query or "").lower())}
    if isinstance(goal_fields, dict):
        for key in ("search_terms", "entities", "filters", "output_fields", "primary_goal"):
            value = goal_fields.get(key)
            if isinstance(value, list):
                for item in value:
                    terms.update(re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-_]{2,}", str(item).lower()))
            elif isinstance(value, str):
                terms.update(re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-_]{2,}", value.lower()))
    return terms


def _collect_sitemap_links(sitemap_tree: dict[str, object]) -> list[dict[str, object]]:
    links: list[dict[str, object]] = []

    def _walk(node: dict[str, object], breadcrumbs: list[str]) -> None:
        url = str(node.get("url", "")).strip()
        segment = str(node.get("segment", "")).strip()
        is_page = bool(node.get("is_page", False))
        if is_page and url:
            path_parts = [part for part in breadcrumbs if part and part != "/"]
            links.append(
                {
                    "url": url,
                    "segment": segment,
                    "path": "/".join(path_parts) if path_parts else "/",
                }
            )

        children = node.get("children") if isinstance(node.get("children"), dict) else {}
        for child in children.values(): # type: ignore
            if isinstance(child, dict):
                child_segment = str(child.get("segment", "")).strip()
                _walk(child, [*breadcrumbs, child_segment])

    if isinstance(sitemap_tree, dict):
        root_segment = str(sitemap_tree.get("segment", "")).strip()
        _walk(sitemap_tree, [root_segment])
    return links


def _select_relevant_sitemap_links(
    user_query: str,
    goal_fields: dict[str, object],
    sitemap_tree: dict[str, object],
    max_links: int = 5,
) -> list[dict[str, object]]:
    candidates = _collect_sitemap_links(sitemap_tree)
    if not candidates:
        return []

    query_terms = _extract_query_terms(user_query, goal_fields)
    scored: list[tuple[int, dict[str, object]]] = []
    for candidate in candidates:
        haystack = " ".join(
            [
                str(candidate.get("url", "")),
                str(candidate.get("segment", "")),
                str(candidate.get("path", "")),
            ]
        ).lower()
        score = sum(1 for term in query_terms if term in haystack)
        scored.append((score, candidate))

    scored.sort(key=lambda row: row[0], reverse=True)
    if query_terms and any(score > 0 for score, _ in scored):
        ranked = [candidate for score, candidate in scored if score > 0]
    else:
        ranked = [candidate for _, candidate in scored]

    trimmed = ranked[:max_links]
    return [
        {
            "url": str(item.get("url", "")).strip(),
            "path": str(item.get("path", "")).strip(),
        }
        for item in trimmed
        if str(item.get("url", "")).strip()
    ]


def build_goal_fields_prompt(user_query: str) -> str:
    """Build a prompt that extracts structured goal fields from the user query."""

    print(f"[prompt_builder] Building goal fields prompt: user_query_len={len(user_query.strip())}")
    prompt = dedent(
        f"""
        You are extracting structured goal fields for a sequential web navigation agent.

        User Query:
        {user_query.strip()}

        Instructions:
        - Infer the user's navigation intent, target entities, filters, and desired output fields.
        - Keep the response concise, actionable, and machine-readable.
        - Return strict JSON only.
        - Do not include markdown, commentary, or extra keys outside the JSON object.

        Required JSON shape:
        {{
            "goal_fields": {{
                "primary_goal": "short summary of what the user wants",
                "entities": ["key entities such as products, brands, pages, or topics"],
                "filters": ["important constraints like price, location, date, category"],
                "output_fields": ["fields the user wants returned, if any"],
                "search_terms": ["high-signal words to use during navigation"],
                "dead_end_signals": ["what would make a page a dead end for this task"]
            }}
        }}

        Return ONLY valid JSON matching the required shape.
        """
    ).strip()
    print(f"[prompt_builder] Goal fields prompt built: prompt_len={len(prompt)}")
    return prompt


def build_router_prompt(user_query: str, sitemap_data: list[dict[str, object]], target_count: int = 5) -> str:
    """Build a navigation prompt that ranks the most semantically relevant URLs."""

    print(
        f"[prompt_builder] Building router prompt: user_query_len={len(user_query.strip())}, "
        f"sitemap_count={len(sitemap_data)}, target_count={target_count}"
    )
    prompt = dedent(
        f"""
        You are a Web Navigator for a deep-discovery scraper.

        User Query:
        {user_query.strip()}

        Candidate Link Metadata:
        {json.dumps(sitemap_data, ensure_ascii=False)}

        Instructions:
        - Rank links only by semantic relevance to the user query.
        - Prefer links whose anchor_text and parent_context best match the task.
        - You MUST choose only from the provided candidate ids.
        - Do NOT invent, rewrite, or normalize URLs.
        - Return strict JSON only.
        - Return a JSON object with a single key named selected_ids.
        - selected_ids must be an array of up to {target_count} integer ids in order of relevance.
        - If fewer than {target_count} high-confidence matches exist, return fewer ids.
        - Never include low-confidence ids just to satisfy a count.
        - Do not include explanations, markdown, or extra keys.

        Return format:
        {{"selected_ids": [12, 45, 3]}}
        """
    ).strip()
    print(f"[prompt_builder] Router prompt built: prompt_len={len(prompt)}")
    return prompt


def build_navigation_prompt(
    user_query: str,
    page_snippet: str,
    discovered_elements: list[dict[str, object]],
    attempted_actions: list[str] | None = None,
    stuck_turns: int = 0,
    result_limit: int = 10,
) -> str:
    """Build a pathfinder prompt that ranks live page actions by relevance."""

    print(
        f"[prompt_builder] Building navigation prompt: user_query_len={len(user_query.strip())}, "
        f"snippet_len={len(page_snippet.strip())}, element_count={len(discovered_elements)}, result_limit={result_limit}"
    )
    prompt = dedent(
        f"""
        You are Pathfinder, an autonomous web navigation agent.

        User Query:
        {user_query.strip()}

        Current Page Snippet:
        {page_snippet.strip()}

        Discovered Elements:
        {json.dumps(discovered_elements, ensure_ascii=False)}

        Previously Attempted Actions On This URL (failed or no movement):
        {json.dumps(attempted_actions or [], ensure_ascii=False)}

        Stuck Turn Count On This URL:
        {int(stuck_turns)}

        Instructions:
        - Decide whether this page is a Terminal Page (the data is here), an Intermediate Page (a useful path), or a Dead End.
        - Distinguish between pages that contain the target data and pages that only contain paths to the target data.
        - Rank only the provided discovered elements.
        - Prefer elements that are semantically relevant to the query, including links, buttons, role=button elements, and search inputs.
        - Ignore accessibility-only links like "Skip to content", "Jump to main", and "Skip navigation" because they usually do not move to a new page.
        - DO NOT repeat any action listed in Previously Attempted Actions On This URL.
        - Return a priority queue of the next actions to attempt, ordered from highest to lowest priority.
        - Use the discovered element ids exactly as provided.
        - Do not invent elements, URLs, or selectors.
        - Return strict JSON only.

        Required JSON shape:
        {{
            "decision": "extract|continue|backtrack",
            "terminal_page": true,
            "reason": "short explanation",
            "priority_queue": [
                {{
                    "id": 3,
                    "action": "click|type|submit|inspect",
                    "priority": 1,
                    "reason": "why this element is promising"
                }}
            ]
        }}

        Return no more than {result_limit} queue items.
        Return ONLY valid JSON.
        """
    ).strip()
    print(f"[prompt_builder] Navigation prompt built: prompt_len={len(prompt)}")
    return prompt


def build_vision_navigation_prompt(
    user_query: str,
    goal_fields: dict[str, object],
    page_snippet: str,
    discovered_elements: list[dict[str, object]],
    sitemap_tree: dict[str, object],
    screenshot_b64: str,
    current_url: str,
    url_stack: list[str],
    attempted_actions: list[str] | None = None,
    stuck_turns: int = 0,
    result_limit: int = 10,
) -> str:
    """Build a vision-first pathfinder prompt with structured intent and sitemap context."""

    relevant_sitemap_links = _select_relevant_sitemap_links(
        user_query=user_query,
        goal_fields=goal_fields,
        sitemap_tree=sitemap_tree,
        max_links=5,
    )
    print(
        f"[prompt_builder] Building vision navigation prompt: user_query_len={len(user_query.strip())}, "
        f"goal_fields_keys={len(goal_fields or {})}, snippet_len={len(page_snippet.strip())}, "
        f"element_count={len(discovered_elements)}, sitemap_links={len(relevant_sitemap_links)}, "
        f"stack_depth={len(url_stack)}, attempted_count={len(attempted_actions or [])}, "
        f"stuck_turns={int(stuck_turns)}, image_len={len(screenshot_b64.strip())}, result_limit={result_limit}"
    )
    prompt = dedent(
        f"""
        You are Pathfinder-Vision, a sequential web navigation agent.

        User Query:
        {user_query.strip()}

        Goal Fields:
        {json.dumps(goal_fields or {}, ensure_ascii=False)}

        Current URL:
        {current_url.strip()}

        URL Stack:
        {json.dumps(url_stack or [], ensure_ascii=False)}

        Previously Attempted Actions On This URL (failed or no movement):
        {json.dumps(attempted_actions or [], ensure_ascii=False)}

        Stuck Turn Count On This URL:
        {int(stuck_turns)}

        Sitemap Reference Links (top 5 by query relevance):
        {json.dumps(relevant_sitemap_links, ensure_ascii=False)}

        Current Page Snippet:
        {page_snippet.strip()}

        Discovered Elements:
        {json.dumps(discovered_elements, ensure_ascii=False)}

        Screenshot Context:
        - The full-page screenshot is provided below as base64 metadata for vision reasoning.
        - screenshot_base64:
        {screenshot_b64.strip()}

        Instructions:
        - Use the Goal Fields first to understand what data or page state matters.
        - Use the screenshot to decide which visible control is most promising.
        - Treat the sitemap reference links as a map only; do not auto-traverse them.
        - If the current page is a dead end, return decision backtrack and include a BACK action.
        - If a search input is visible, prefer typing the goal fields or search terms and submitting Enter.
        - Ignore accessibility-only links like "Skip to content", "Jump to main", and "Skip navigation" because they usually do not move to a new page.
        - DO NOT repeat any action listed in Previously Attempted Actions On This URL.
        - If stuck turn count is greater than 3, prefer a direct action=navigate using a relevant Sitemap Reference Link instead of repeating same-page clicks.
        - Rank only the provided discovered element ids.
        - Use action navigate only when you intentionally want to visit a specific href.
        - Return strict JSON only.

        Required JSON shape:
        {{
          "decision": "extract|continue|backtrack",
          "terminal_page": true,
          "reason": "short explanation",
          "priority_queue": [
            {{
              "id": 3,
              "action": "click|type|submit|inspect|navigate|back",
              "priority": 1,
              "reason": "why this element is promising"
            }}
          ]
        }}

        Return no more than {result_limit} queue items.
        Return ONLY valid JSON.
        """
    ).strip()
    print(f"[prompt_builder] Vision navigation prompt built: prompt_len={len(prompt)}")
    return prompt


def build_extraction_prompt(extraction_goal: str, page_markdown: str) -> str:
    """Build a strict extraction prompt for Groq-driven chunk loops."""

    print(
        f"[prompt_builder] Building extraction prompt: extraction_goal_len={len(extraction_goal.strip())}, "
        f"page_markdown_len={len(page_markdown.strip())}"
    )
    prompt = dedent(
        f"""
        You are running in MCP Code Mode for surgical data extraction.

        Extraction Goal:
        {extraction_goal.strip()}

        Instructions:
        - Use only the provided page content.
        - Never invent headlines, URLs, or facts that are not present in the content.
        - Think through the chunk internally, then return strict JSON only.
        - Follow a Thought-Action loop: decide whether the chunk should be extracted, skipped, or marked final.
        - Do not output chain-of-thought text outside the JSON object.
        - Include logic_metadata so Python can apply the final mathematical filter exactly.
        - Keep logic_metadata normalized with operator values limited to gt, lt, or eq.
        - Keep records compact, normalized, and machine-readable.

        Required JSON shape:
        {{
          "thought": "short internal summary",
          "action": "extract|skip|final",
          "records": [{{}}],
          "logic_metadata": {{
            "filter_field": "price",
            "operator": "lt",
            "target_value": 2000.0,
                        "result_limit": 10
          }},
          "errors": []
        }}

        Page Content:
        {page_markdown.strip()}

        Return ONLY valid JSON matching the required shape.
        """
    ).strip()
    print(f"[prompt_builder] Extraction prompt built: prompt_len={len(prompt)}")
    return prompt