# 1. Overview of the use case or project

The GenAI Scraper Project is an AI-assisted web extraction system that converts natural-language scraping requests into a coordinated, two-agent scraping pipeline. It is designed to collect both:

- Structured, repeating entities (for example: product cards, news article lists, table-like rows), and
- Unstructured, singular facts (for example: CEO name, contact detail, a specific company-level fact).

The project uses FastAPI as the runtime API layer, a browser-based frontend for user interaction, Playwright for robust dynamic page rendering, BeautifulSoup for local DOM parsing, and Groq-hosted Llama models for intent decomposition, extraction guidance, and final presentation formatting.

Core objectives implemented in the codebase:

- Intelligent query decomposition into actionable extraction plans (`ScrapeManifest`).
- Reduced LLM token consumption by letting the LLM propose selector/navigation strategies while local Python executes the bulk parsing and extraction.
- Better resilience against rate-limit pressure (429-style scenarios) via chunking and retry/backoff logic in final summarization paths.
- Guardrails against inefficient navigation behavior by capping navigation attempts and forcing extraction fallback when loops or no-progress states are detected.
- Unified output synthesis that merges structured arrays and singular facts into one response payload.

Primary use case:

- A user provides a website URL plus a natural-language requirement (for example, "Find top 5 recent posts and the founder name").
- The system routes the request to specialized agents and returns merged, human-readable results.

Target audience:

- Data analysts and business users who need quick website intelligence without writing custom scrapers.
- Developers and solution architects building AI-assisted extraction workflows.
- Teams requiring rapid prototyping for web intelligence, lead research, or content monitoring.

# 2. Projects concepts & technologies

## Technology stack and roles

| Technology | Role in this project | Where it appears |
|---|---|---|
| Python | Core backend language for orchestration, agents, extraction logic, and formatting pipelines. | Entire codebase |
| FastAPI | API service layer exposing `/scrape`, `/health`, and frontend root endpoint. Handles request/response flow and concurrency via thread executor. | `api/main.py` |
| Streamlit | Included as dependency but not currently used in runtime code paths. Potentially retained for alternate UI/prototyping workflows. | `requirements.txt` |
| Playwright | Headless browser automation for dynamic rendering, real navigation, search submission, and pagination interactions. | `agents/list_agent.py`, `agents/facts_agent.py` |
| BeautifulSoup | Fast local HTML cleanup and DOM parsing. Used to sanitize content and execute selector-based extraction with minimal token usage. | `agents/list_agent.py`, `agents/facts_agent.py` |
| httpx | Included as dependency but not currently imported in active source files. Likely reserved for future optimized HTTP-only fetch routines. | `requirements.txt` |
| Groq API + Llama models | LLM backbone for manifest generation, navigation decisions, selector recipe generation, fact extraction, and final narrative/HTML formatting. | `app/processor.py`, `agents/*.py`, `tools/final_formatter.py` |
| Pydantic | Strong typing/validation for request models and extraction manifests (`FieldDef`, `StructureDef`, `ScrapeManifest`). | `app/processor.py`, `api/main.py` |
| MCP concepts (orchestrated multi-component pipeline) | Project models an MCP-style orchestrator pattern: one coordinator delegates tasks to specialized components, then aggregates outputs into a single result. | `mcp_server/orchestrator.py` |

## LLM model usage in code

- `llama-3.3-70b-versatile`
  - Used for high-level query-to-manifest decomposition and final higher-quality formatting/synthesis tasks.
- `llama-3.1-8b-instant`
  - Used for fast operational decisions (navigation vs extract), selector recipe generation, and fact extraction loops.

## Key project concepts

- Manifest-driven orchestration:
  - User intent is transformed into a typed `ScrapeManifest` containing:
    - `unstructure`: singular fact fields
    - `structure`: repeating entity schema
    - `limit`: final output size
    - `scan_limit`: over-scan amount to support filtering/sorting
    - `filter_hint`: human-readable filter condition
- Token-efficient extraction:
  - Instead of asking the LLM to parse full pages repeatedly, the system sends compact snippets and executes CSS extraction locally.
- Bifurcated data strategy:
  - Structured arrays and singular facts are extracted independently, then merged.
- Post-extraction filtering:
  - Numeric filter/sort hints (less than, greater than, between, ascending/descending) are applied in orchestrator logic before final output.

# 3. Setup steps

Use the commands below for a clean local setup.

## 3.1 Clone repository

```bash
git clone <YOUR_REPO_URL>
cd ai_scraper_project
```

## 3.2 Create and activate virtual environment

### Windows (PowerShell)

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### macOS/Linux (bash/zsh)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3.3 Install dependencies

```bash
pip install -r requirements.txt
```

## 3.4 Install Playwright browsers

```bash
playwright install
```

## 3.5 Configure environment variables

Create `.env` in project root:

```env
GROQ_API_KEY=your_groq_api_key_here
```

## 3.6 Run API server

```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Open:

- `http://localhost:8000` for the frontend UI.
- `http://localhost:8000/health` for service health check.

# 4. Project architectures

## 4.1 High-level architecture

The runtime is a dual-agent orchestration system:

1. Classifier / Orchestrator layer
2. Structure Agent (list/repeating extraction)
3. Unstructure Agent (singular fact extraction)
4. Final formatter (HTML and/or narrative synthesis)

Flow ownership is centralized in `run_orchestrator`, which:

- builds a `ScrapeManifest` from user query,
- dispatches tasks to the relevant specialized agents,
- post-filters list results using interpreted numeric hints,
- merges outputs into one normalized JSON response.

## 4.2 Separation of concerns

### The Classifier / Orchestrator: Splitting natural language into a `ScrapeManifest`

- Implemented by `process_query(query)` in `app/processor.py`.
- Uses Pydantic models to enforce output contract:
  - `FieldDef`
  - `StructureDef`
  - `ScrapeManifest`
- LLM prompt rules explicitly separate:
  - actual singular facts that exist on pages (go to `unstructure`), versus
  - repeating entities (go to `structure`).
- It also determines extraction scale controls:
  - `limit` (final answer size)
  - `scan_limit` (oversampling budget)
  - `filter_hint` (post-processing instruction)

### Structure Agent: Handling 'Common' data (lists/tables) by generating CSS selector recipes and executing them locally via BeautifulSoup to save tokens

- Implemented by `run_structure_agent` in `agents/list_agent.py`.
- Uses Playwright to:
  - load dynamic pages,
  - optionally navigate to listing pages,
  - optionally execute search-bar interactions,
  - optionally paginate.
- Uses compact HTML snippets for LLM decisions:
  - `clean_html_for_nav` supports nav-decision prompt.
  - `find_card_snippet` isolates likely card/list blocks for selector generation.
- LLM returns extraction recipe (container selector + field selectors + attrs).
- BeautifulSoup applies selectors locally to extract items at scale.
- Safety/loop controls:
  - limited navigation/search rounds,
  - abort/fallback when URL does not change after navigation,
  - stop when item limit is reached.

### Unstructure Agent: Handling 'Unique' data (singular facts) using a targeted search approach

- Implemented by `run_unstructure_agent` in `agents/facts_agent.py`.
- Uses Playwright + BeautifulSoup text extraction to obtain compact page context.
- LLM extracts only missing singular fields in iterative passes.
- For unresolved fields, LLM proposes likely navigation links (for example: About, Contact).
- Agent follows targeted links with bounded retries, then stops gracefully if no progress is possible.

# 5. Project workflow

A single request follows this lifecycle:

1. User Input
- Frontend sends POST `/scrape` with:
  - `url`
  - `query`

2. Goal Categorization
- API calls `run_orchestrator(url, query)`.
- Orchestrator calls `process_query(query)`.
- LLM returns `ScrapeManifest` separating structured and unstructured goals.

3. Agent Routing
- If `manifest.unstructure` is not empty, orchestrator runs Unstructure Agent first.
- If `manifest.structure` exists, orchestrator runs Structure Agent with `item_limit=scan_limit`.

4. Fast HTML Fetching/Cleaning
- Both agents use Playwright for render/navigation reliability.
- HTML is cleaned with BeautifulSoup:
  - removes script/style/head/meta/noise,
  - trims content snippets before sending to LLM,
  - preserves token budget.

5. Extraction
- Unstructure Agent:
  - extracts singular facts from page text,
  - iterates only for missing fields,
  - follows targeted links when needed.
- Structure Agent:
  - asks LLM for navigate/search/extract decision,
  - if extract path selected, asks LLM for selector recipe,
  - applies selectors locally across containers/pages.

6. Post-processing and constraints
- Orchestrator applies numeric filter/sort rules from `filter_hint`:
  - less than / greater than / between
  - ascending / descending heuristics
- Truncates final list to `limit`.

7. Final Synthesis/Formatting
- Raw merged JSON is passed to `format_html_output`.
- LLM returns styled HTML component (no full-page wrapper).
- API returns:
  - `status`
  - `raw_data`
  - `html_output`

8. Frontend Rendering
- UI injects `html_output` into result panel.
- Raw JSON remains available via toggle for debugging/transparency.

# 6. Project examples with full workflow how it is been working

## Example scenario

User request:

- "List news articles with title and summary, and also tell me the company CEO."

Target URL:

- Example: company website homepage with both news feed and company information links.

## Step-by-step technical trace

1. Request submission
- Frontend sends:

```json
{
  "url": "https://example-company.com",
  "query": "List news articles with title and summary, and also tell me the company CEO."
}
```

2. Orchestrator starts and generates manifest
- `process_query` classifies intent into two buckets.
- Expected manifest shape (illustrative):

```json
{
  "unstructure": [
    { "name": "ceo", "description": "Name of the company CEO" }
  ],
  "structure": {
    "entity": "news articles",
    "fields": [
      { "name": "title", "description": "Headline of the article" },
      { "name": "summary", "description": "Short description of the article" }
    ]
  },
  "limit": 5,
  "scan_limit": 5,
  "filter_hint": ""
}
```

3. Unstructure Agent execution (CEO)
- Opens homepage via Playwright.
- Cleans page text and asks LLM to extract missing field `ceo`.
- If not found, asks LLM for a likely navigation link (for example `/about`, `/leadership`).
- Navigates and retries extraction within bounded attempts.
- Produces facts output, for example:

```json
{
  "ceo": "Jane Doe"
}
```

4. Structure Agent execution (news list)
- Opens homepage via Playwright.
- LLM decides whether to navigate/search/extract directly.
- If list is visible, it proceeds to selector recipe generation.
- Recipe example:

```json
{
  "action": "extract",
  "container_selector": ".news-card",
  "fields": {
    "title": { "selector": "h2", "attr": "text" },
    "summary": { "selector": "p.summary", "attr": "text" }
  },
  "next_page_selector": null
}
```

- BeautifulSoup applies selectors locally for each matching card.
- Produces list output, for example:

```json
[
  {
    "title": "Quarterly Results Announced",
    "summary": "The company reported strong year-over-year growth..."
  },
  {
    "title": "New Sustainability Initiative",
    "summary": "A multi-year plan to reduce carbon impact..."
  }
]
```

5. Orchestrator merge and filter stage
- Builds combined result payload:

```json
{
  "facts_data": {
    "ceo": "Jane Doe"
  },
  "list_data": [
    {
      "title": "Quarterly Results Announced",
      "summary": "The company reported strong year-over-year growth..."
    },
    {
      "title": "New Sustainability Initiative",
      "summary": "A multi-year plan to reduce carbon impact..."
    }
  ],
  "filter_hint": "",
  "output_limit": 5
}
```

6. Final merged output rendering
- `format_html_output` takes merged JSON + original goal.
- LLM returns styled HTML with:
  - facts section (CEO as key-value), and
  - article cards for structured list.
- API response includes both raw JSON and ready-to-render HTML.

## Why this example demonstrates the architecture

- One query is intentionally mixed (repeating + singular) and triggers both agents.
- The orchestrator enforces clean separation and deterministic merge behavior.
- Most heavy extraction work remains local (selector execution), preserving token budget.
- The system still benefits from LLM flexibility for decomposition, strategy selection, and final presentation.
