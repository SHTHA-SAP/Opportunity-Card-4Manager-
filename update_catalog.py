"""
SAP AI Catalog Auto-Updater (v6 — direct API call via Playwright)
==================================================================
Calls the SAP Discovery Center REST API directly (no page rendering).
Uses Playwright only as an HTTP client to fetch JSON from the API.

API: /servicecatalog/api/v1/ai/ai-catalog
Fields (from actual API response):
  isBillable true/false  ->  premium/base
  type ai_agent etc      ->  icon agents/joule/aiFeatures
  products[0].productName -> product

MERGE: adds new, updates changed, removes deleted.
EXIT: 0=success/no-data, 1=real error
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from playwright.async_api import async_playwright

# ── Configuration ────────────────────────────────────────────────────────

API_BASE = "https://discovery-center.cloud.sap/servicecatalog/api/v1/ai/ai-catalog"

# Only AI Features and Agents (no assistants)
API_URLS = [
    API_BASE,
]

HTML_FILE = "Opportunity-Card-4Manager-v4.html"


# ── Field mapping (from actual API data) ─────────────────────────────────

def map_type(item):
    """isBillable true means premium, false means base."""
    val = item.get("isBillable")
    # Handle bool, string, or int — API may return any of these
    if val is True or val == 1:
        return "premium"
    if isinstance(val, str) and val.lower() == "true":
        return "premium"
    return "base"


def map_icon(item):
    """Map API type field plus ribbons and name to icon category."""
    api_type = (item.get("type") or "").lower()
    name = (item.get("name") or "").lower()
    ribbons = (item.get("ribbons") or "").lower()

    if api_type in ("ai_agent", "ai_agent_package"):
        return "agents"
    if "joule" in name:
        return "joule"
    if "joule" in ribbons:
        return "joule"
    return "aiFeatures"


def get_product_name(item):
    """Extract product name from the products array."""
    products = item.get("products", [])
    if products and isinstance(products, list) and len(products) > 0:
        first = products[0]
        if isinstance(first, dict):
            pname = (first.get("productName") or "").strip()
            if pname:
                return pname
    return (item.get("name") or "").strip()


# ── Fetch via Playwright API request (no page rendering) ─────────────────

async def fetch_catalog():
    """Call SAP API directly using Playwright as HTTP client."""
    all_items = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36"
        )

        for url in API_URLS:
            print(f"\n  Fetching: {url}")
            try:
                # Use Playwright API request context for direct HTTP calls
                api = context.request
                response = await api.get(url, headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://discovery-center.cloud.sap/ai-catalog/",
                })

                status = response.status
                if status != 200:
                    print(f"  HTTP {status}: {response.status_text}")
                    continue

                data = await response.json()

                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = []
                    for key in ("results", "value", "data", "features", "items"):
                        if key in data and isinstance(data[key], list):
                            items = data[key]
                            break
                else:
                    items = []

                print(f"  [{status}] Got {len(items)} items")
                all_items.extend(items)

            except Exception as e:
                print(f"  Error: {e}")

        await context.close()
        await browser.close()

    # Deduplicate by ID
    seen = set()
    unique = []
    for item in all_items:
        item_id = item.get("id") or item.get("name", "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            unique.append(item)

    if all_items:
        print(f"\n  Total: {len(all_items)} raw -> {len(unique)} unique")

    return unique


# ── Build catalog from API data ──────────────────────────────────────────

def build_catalog(raw):
    """Convert flat API items into catalog entries."""
    catalog = []
    seen = set()

    if raw:
        first = raw[0]
        print(f"\n{'~'*60}")
        print("DEBUG: ACTUAL API FIELDS")
        print(f"{'~'*60}")
        print(f"  Fields: {list(first.keys())}")
        print(f"  name: {first.get('name')}")
        print(f"  type: {first.get('type')}")
        print(f"  isBillable: {first.get('isBillable')} (type: {type(first.get('isBillable')).__name__})")
        print(f"  ribbons: {first.get('ribbons')}")
        print(f"  products: {first.get('products')}")
        print(f"{'~'*60}")

        type_counts = {}
        billable_counts = {"premium": 0, "base": 0}
        for item in raw:
            t = item.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
            if item.get("isBillable"):
                billable_counts["premium"] += 1
            else:
                billable_counts["base"] += 1
        print(f"  By API type: {type_counts}")
        print(f"  By billable: {billable_counts}")
        print(f"{'~'*60}")

    # Whitelist: only these API types make it into the catalog
    ALLOWED_TYPES = {"ai_feature", "ai_feature_package", "ai_agent", "ai_agent_package"}
    skipped_types = {}

    for item in raw:
        if not isinstance(item, dict):
            continue

        # Only keep allowed types — skip assistants and anything else
        api_type = (item.get("type") or "").lower().strip()
        if api_type not in ALLOWED_TYPES:
            skipped_types[api_type or "(empty)"] = skipped_types.get(api_type or "(empty)", 0) + 1
            continue

        name = (item.get("name") or "").strip()
        if not name:
            continue

        product = get_product_name(item)
        desc = (item.get("shortDescription") or item.get("description") or "").strip()

        key = (name.lower(), product.lower())
        if key in seen:
            continue
        seen.add(key)

        catalog.append({
            "name": name,
            "product": product,
            "desc": desc,
            "type": map_type(item),
            "icon": map_icon(item),
        })

    if skipped_types:
        print(f"  Skipped types (not in whitelist): {skipped_types}")

    return catalog


# ── Read existing catalog from HTML ──────────────────────────────────────

def read_existing_catalog(content):
    START = "const UC_CATALOG = "
    start_idx = content.find(START)
    if start_idx == -1:
        return []

    bracket_start = start_idx + len(START)
    depth = 0
    in_string = False
    escape_next = False
    end_bracket = -1

    for i in range(bracket_start, len(content)):
        c = content[i]
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if c == "\\":
                escape_next = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end_bracket = i
                break

    if end_bracket == -1:
        return []

    json_str = content[bracket_start: end_bracket + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"WARNING: Could not parse existing catalog JSON: {e}")
        return []


# ── Merge logic ──────────────────────────────────────────────────────────

def merge_catalogs(existing, new_entries):
    existing_map = {}
    for item in existing:
        key = (item.get("name", "").lower(), item.get("product", "").lower())
        existing_map[key] = item

    new_map = {}
    for item in new_entries:
        key = (item.get("name", "").lower(), item.get("product", "").lower())
        new_map[key] = item

    merged = []
    added = 0
    updated = 0
    removed = 0
    unchanged = 0

    for key, new_item in new_map.items():
        if key in existing_map:
            old_item = existing_map[key]
            if (old_item.get("desc") != new_item.get("desc") or
                old_item.get("type") != new_item.get("type") or
                old_item.get("icon") != new_item.get("icon") or
                old_item.get("product") != new_item.get("product")):
                merged.append(new_item)
                updated += 1
            else:
                merged.append(old_item)
                unchanged += 1
        else:
            merged.append(new_item)
            added += 1

    for key in existing_map:
        if key not in new_map:
            removed += 1

    return {
        "catalog": merged,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
    }


# ── Write catalog back to HTML ───────────────────────────────────────────

def write_catalog_to_html(catalog):
    path = Path(HTML_FILE)
    if not path.exists():
        print(f"ERROR: {HTML_FILE} not found!")
        return False

    content = path.read_text(encoding="utf-8-sig")

    START = "const UC_CATALOG = ["
    start_idx = content.find(START)
    if start_idx == -1:
        print("ERROR: Could not find 'const UC_CATALOG = [' in HTML file")
        return False

    bracket_start = start_idx + len("const UC_CATALOG = ")
    depth = 0
    in_string = False
    escape_next = False
    end_bracket = -1

    for i in range(bracket_start, len(content)):
        c = content[i]
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if c == "\\":
                escape_next = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end_bracket = i
                break

    if end_bracket == -1:
        print("ERROR: Could not find closing ] of UC_CATALOG")
        return False

    tail = content[end_bracket: end_bracket + 10]
    semi_off = tail.find(";")
    if semi_off == -1:
        print("ERROR: No semicolon after UC_CATALOG ]")
        return False

    end_semi = end_bracket + semi_off
    new_json = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    updated_content = (
        content[:start_idx]
        + "const UC_CATALOG = " + new_json + ";"
        + content[end_semi + 1:]
    )

    path.write_text(updated_content, encoding="utf-8")
    return True


# ── Main ─────────────────────────────────────────────────────────────────

async def main():
    print("SAP AI Catalog Auto-Updater (v6 direct API)")
    print("=" * 60)

    path = Path(HTML_FILE)
    if not path.exists():
        print(f"ERROR: {HTML_FILE} not found.")
        sys.exit(1)

    html_content = path.read_text(encoding="utf-8-sig")
    existing = read_existing_catalog(html_content)
    print(f"Existing catalog: {len(existing)} entries")

    raw = await fetch_catalog()

    if not raw:
        print(f"\n{'='*60}")
        print("INFO: No data from SAP Discovery Center API.")
        print("  Existing catalog is UNCHANGED.")
        print(f"  Keeping all {len(existing)} existing entries.")
        print(f"{'='*60}")
        sys.exit(0)

    print(f"\nFetched {len(raw)} items from API")

    new_entries = build_catalog(raw)
    print(f"Processed into {len(new_entries)} unique catalog entries")

    if not new_entries:
        print("INFO: Could not process entries from API data.")
        print(f"  Keeping all {len(existing)} existing entries.")
        sys.exit(0)

    result = merge_catalogs(existing, new_entries)
    merged = result["catalog"]

    print(f"\n{'='*60}")
    print("MERGE REPORT")
    print(f"{'='*60}")
    print(f"  Existing entries:  {len(existing)}")
    print(f"  SAP entries:       {len(new_entries)}")
    print(f"  ---")
    print(f"  Added (new):       +{result['added']}")
    print(f"  Updated:           ~{result['updated']}")
    print(f"  Removed (deleted): -{result['removed']}")
    print(f"  Unchanged:          {result['unchanged']}")
    print(f"  ---")
    print(f"  Final catalog:     {len(merged)} entries")
    print(f"{'='*60}")

    if len(merged) == 0 and len(existing) > 0:
        print("ERROR: Merge produced 0 entries. Refusing to wipe catalog.")
        sys.exit(1)

    if result["added"] == 0 and result["updated"] == 0 and result["removed"] == 0:
        print("\nNo changes. Catalog is already up to date.")
        sys.exit(0)

    if not write_catalog_to_html(merged):
        print("ERROR: Failed to write catalog to HTML.")
        sys.exit(1)

    print(f"\nHTML updated: {len(merged)} entries written to {HTML_FILE}")
    print("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"SCRIPT ERROR: {e}")
        print(f"{'='*60}")
        traceback.print_exc()
        sys.exit(1)
