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
    manifest = ScrapeManifest(**data)

    # Guardrail: person-profile questions often contain multiple singular asks that the LLM may collapse.
    q = query.lower()
    is_person_query = any(k in q for k in ["who is", "who's", "profile", "about"])
    asks_role = "role" in q or "title" in q or "position" in q
    asks_domain = "domain" in q or "team" in q or "department" in q or "practice" in q
    asks_current = "what he does" in q or "what she does" in q or "responsibil" in q or "currently" in q
    asks_past = "what he did" in q or "what she did" in q or "previous" in q or "past" in q or "experience" in q

    if is_person_query and any([asks_role, asks_domain, asks_current, asks_past]):
        existing_names = {f.name.strip().lower() for f in manifest.unstructure}

        def add_field(name: str, description: str):
            if name.lower() not in existing_names:
                manifest.unstructure.append(FieldDef(name=name, description=description))
                existing_names.add(name.lower())

        if asks_role and all(k not in existing_names for k in ["role", "title", "position"]):
            add_field("role", "Current role or title of the person")
        if asks_domain and all(k not in existing_names for k in ["domain", "team", "practice", "department"]):
            add_field("domain", "Domain, team, or practice area where the person works")
        if asks_current and all(k not in existing_names for k in ["current_responsibilities", "responsibilities", "current_focus"]):
            add_field("current_responsibilities", "What the person currently does")
        if asks_past and all(k not in existing_names for k in ["past_experience", "previous_experience", "background"]):
            add_field("past_experience", "What the person previously did or prior experience")

    return manifest
