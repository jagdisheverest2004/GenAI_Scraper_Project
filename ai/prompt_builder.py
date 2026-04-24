"""Prompt helpers for router, navigation, and extraction prompts."""

from __future__ import annotations

import json
from textwrap import dedent


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

                Instructions:
                - Decide whether this page is a Terminal Page (the data is here), an Intermediate Page (a useful path), or a Dead End.
                - Distinguish between pages that contain the target data and pages that only contain paths to the target data.
                - Rank only the provided discovered elements.
                - Prefer elements that are semantically relevant to the query, including links, buttons, role=button elements, and search inputs.
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