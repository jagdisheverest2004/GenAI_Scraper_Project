import os
import json
from pydantic import BaseModel, Field
from typing import List, Optional
from groq import Groq


class FieldDef(BaseModel):
    name: str = Field(description="The name of the field to extract")
    description: str = Field(description="A brief description of what this field represents")


class StructureDef(BaseModel):
    entity: str = Field(description="The name of the repeating item or entity")
    fields: List[FieldDef] = Field(description="The list of fields to extract for each entity")


class ScrapeManifest(BaseModel):
    unstructure: List[FieldDef] = Field(description="Singular facts to scrape from the website")
    structure: Optional[StructureDef] = Field(description="Repeating list items to extract, if any")
    limit: int = Field(default=10, description="Final number of items the user wants in output")
    scan_limit: int = Field(default=20, description="How many items to scrape before stopping")
    filter_hint: str = Field(default="", description="Filter/sort condition for the formatter")


def process_query(query: str) -> ScrapeManifest:
    api_key = str(os.getenv("GROQ_API_KEY", "")).strip().strip('"').strip("'")
    client = Groq(api_key=api_key)

    prompt = f"""
    Analyze the following user scraping query and break it down into categories.
    Query: "{query}"

    CRITICAL RULES:
    - "unstructure" = ONLY real singular facts that physically exist on a webpage and must be SCRAPED.
      Examples: CEO name, company address, phone number, founding year.
      DO NOT include query filters like "price limit", "quantity", "number of results", "timeframe", "recent".
    - "structure" = repeating list items to extract (books, articles, products, etc.)
    - "limit" = the NUMBER of final output items the user wants (e.g. "3 books" -> 3)
    - "scan_limit" = how many items to scrape before stopping.
      If there is a filter or sort condition, set scan_limit = limit * 15 (capped at 150).
      If there is NO filter or sort, set scan_limit = limit.
    - "filter_hint" = describe any filter/sort condition for the formatter, e.g. "price must be less than 15 GBP".
      If none, return "".

    Examples:
    - "Find 3 books under 15 GBP" -> limit=3, scan_limit=45, filter_hint="price must be less than 15 GBP"
    - "Find top 3 highest price books under 15 GBP" -> limit=3, scan_limit=45, filter_hint="price under 15 GBP, sort descending"
    - "Find 3 recent news articles" -> limit=3, scan_limit=3, filter_hint=""
    - "Find the CEO and recent news" -> unstructure=[CEO], structure=news, limit=5, scan_limit=5, filter_hint=""

    Return JSON:
    {{
        "unstructure": [{{"name": "field_name", "description": "what it is"}}],
        "structure": {{
            "entity": "name of the repeating item",
            "fields": [{{"name": "field_name", "description": "what it is"}}]
        }},
        "limit": <integer>,
        "scan_limit": <integer>,
        "filter_hint": "<string>"
    }}
    If there are no repeating items, set "structure" to null.
    If there are no real singular facts to SCRAPE, set "unstructure" to [].
    """

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"}
    )

    data = json.loads(response.choices[0].message.content)
    return ScrapeManifest(**data)
