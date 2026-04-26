import re
import json
from app.processor import process_query
from agents.list_agent import run_structure_agent as run_list_agent
from agents.facts_agent import run_unstructure_agent as run_facts_agent


def _apply_filter(items: list, filter_hint: str, output_limit: int) -> list:
    if not filter_hint or not items:
        return items[:output_limit]

    fh = filter_hint.lower()

    lt_match = re.search(r'less than\s*[£$€]?\s*(\d+\.?\d*)', fh)
    gt_match = re.search(r'greater than\s*[£$€]?\s*(\d+\.?\d*)', fh)
    between_match = re.search(r'between\s*[£$€]?\s*(\d+\.?\d*)\s*and\s*[£$€]?\s*(\d+\.?\d*)', fh)
    sort_desc = any(w in fh for w in ['highest', 'descending', 'most expensive', 'largest'])
    sort_asc = any(w in fh for w in ['lowest', 'ascending', 'cheapest', 'smallest'])

    def extract_numeric(item: dict) -> float:
        for v in item.values():
            if isinstance(v, str):
                m = re.search(r'[\d]+\.?\d*', v.replace(',', ''))
                if m:
                    return float(m.group())
        return float('inf')

    filtered = []
    for item in items:
        val = extract_numeric(item)
        if between_match:
            lo, hi = float(between_match.group(1)), float(between_match.group(2))
            if lo <= val <= hi:
                filtered.append(item)
        elif lt_match:
            if val < float(lt_match.group(1)):
                filtered.append(item)
        elif gt_match:
            if val > float(gt_match.group(1)):
                filtered.append(item)
        else:
            filtered.append(item)

    if sort_desc:
        filtered.sort(key=extract_numeric, reverse=True)
    elif sort_asc:
        filtered.sort(key=extract_numeric)

    result = filtered[:output_limit]
    print(f"[Orchestrator] Filter applied: '{filter_hint}' → {len(items)} items → {len(result)} kept")
    return result


def run_orchestrator(start_url: str, query: str) -> str:
    print(f"[Orchestrator] Processing query: {query}")
    manifest = process_query(query)
    print(f"[Orchestrator] Manifest generated: {manifest.model_dump_json(indent=2)}")

    result = {
        "facts_data": {},
        "list_data": []
    }

    if manifest.unstructure:
        print("[Orchestrator] Running Facts Agent...")
        result["facts_data"] = run_facts_agent(start_url, manifest.unstructure)

    if manifest.structure:
        print(f"[Orchestrator] Running List Agent... (scan: {manifest.scan_limit} items, output: {manifest.limit} items)")
        raw_items = run_list_agent(start_url, manifest.structure, item_limit=manifest.scan_limit)

        if manifest.filter_hint:
            result["list_data"] = _apply_filter(raw_items, manifest.filter_hint, manifest.limit)
        else:
            result["list_data"] = raw_items[:manifest.limit]

        result["filter_hint"] = manifest.filter_hint
        result["output_limit"] = manifest.limit

    return json.dumps(result, indent=2)
