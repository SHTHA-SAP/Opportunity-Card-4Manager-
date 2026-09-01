"""
SAP AI Catalog Auto-Updater (v4 — merge mode)

Behavior:
  - MERGES new data into the existing catalog (never replaces it)
  - New entries found on SAP → ADDED
  - Existing entries still on SAP → UPDATED (desc, type, icon refreshed)
  - Existing entries DELETED from SAP → REMOVED
  - No data captured from SAP → exit 0 (success), catalog untouched
  - Actual script errors (HTML corrupt, file missing) → exit 1 (fail) so you know

Exit codes:
  0 = success (catalog updated, or no new data — either way, safe)
  1 = real error (script bug, file not found, HTML parsing broken)
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from playwright.async_api import async_playwright

# Visit multiple filter URLs to ensure ALL categories load.
# The API may paginate or serve different items per filter.
# We visit all of them and combine + deduplicate.
PAGE_URL_GROUPS = [
    # Group 1: Production — unfiltered then each category
    [
        "https://discovery-center.cloud.sap/ai-catalog/",                                    # all items
        "https://discovery-center.cloud.sap/ai-catalog/?aiType=ai_agent_package,ai_agent",    # agents
        "https://discovery-center.cloud.sap/ai-catalog/?aiType=joule",                        # joule
    ],
    # Group 2: Dev fallback
    [
        "https://discovery-center-dev.cfapps.eu10-004.hana.ondemand.com/ai-catalog",
    ],
]
HTML_FILE = "Opportunity-Card-4Manager-v4.html"

FEATURE_KEYWORDS = {"capabilities", "features", "name", "shortDescription", "commercialType"}


# ── Icon + type mapping ───────────────────────────────────────────────────

def map_icon(name: str, ribbons: str) -> str:
    if "agent" in name.lower():
        return "agents"
    if "joule" in (ribbons or "").lower():
        return "joule"
    return "aiFeatures"

def map_type(cap: dict, product_group: dict = None) -> str:
    """
    Extract commercial type (base/premium) from the API data.
    Checks BOTH the capability AND its parent product group,
    because SAP may store commercialType at either level.
    """
    # Check capability first, then fall back to parent product group
    sources = [cap]
    if product_group is not None and product_group is not cap:
        sources.append(product_group)

    # All possible field names for commercial type
    type_fields = [
        "commercialType", "CommercialType", "commercial_type",
        "commercialModel", "CommercialModel",
        "package", "Package", "packageName", "PackageName",
        "licenseModel", "LicenseModel", "license_model",
        "pricingType", "PricingType",
        "tier", "Tier",
        "ribbons", "Ribbons",
        "tags",
    ]

    for source in sources:
        if not isinstance(source, dict):
            continue
        for field in type_fields:
            val = source.get(field)
            if val is None:
                continue
            val_str = str(val).lower()
            if "premium" in val_str:
                return "premium"

    return "base"


# ── Strategy 1: Intercept API responses ───────────────────────────────────

async def try_api_intercept(page) -> list:
    """Intercept all JSON responses and look for AI feature data."""
    result = []
    api_log = []

    async def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" not in ct and "odata" not in ct:
            return
        if "discovery-center" not in url and "hana.ondemand" not in url:
            return
        try:
            data = await response.json()
            api_log.append(url[:120])
            items = _extract_items_from_json(data)
            if items:
                print(f"  [API] Captured {len(items)} items from: {url[:120]}")
                result.extend(items)
        except Exception:
            pass

    page.on("response", on_response)
    await page.wait_for_timeout(8_000)

    # Try clicking cookie consent buttons first
    for selector in ['button:has-text("Accept")', 'button:has-text("Accept All")',
                     'button:has-text("Agree")', 'button:has-text("OK")']:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500):
                print(f"  [API] Clicking consent: {selector}")
                await btn.click()
                await page.wait_for_timeout(1_000)
                break
        except Exception:
            pass

    # Scroll aggressively to trigger lazy loading / infinite scroll
    prev_count = len(result)
    for scroll_round in range(10):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2_000)
        except Exception:
            pass
        # If new data arrived, keep scrolling
        if len(result) > prev_count:
            prev_count = len(result)
            print(f"  [API] Scroll {scroll_round+1}: now {len(result)} items")
        elif scroll_round >= 3:
            break  # No new data after 3+ scrolls — stop

    # Try clicking load-more / show-all buttons
    for selector in ['button:has-text("Load More")', 'button:has-text("Show All")',
                     'button:has-text("Show more")', '[class*="showMore"]',
                     'button:has-text("More")', '[class*="loadMore"]']:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500):
                print(f"  [API] Clicking: {selector}")
                await btn.click()
                await page.wait_for_timeout(5_000)
        except Exception:
            pass

    await page.wait_for_timeout(3_000)

    if api_log:
        print(f"  [API] {len(api_log)} JSON responses intercepted:")
        for url in api_log:
            print(f"    {url}")

    return result


def _extract_items_from_json(data) -> list:
    """Try to extract AI feature items from any JSON shape."""
    items = []

    if isinstance(data, list) and len(data) > 0:
        items = data
    elif isinstance(data, dict):
        for key in ("results", "value", "data", "features", "items",
                    "d", "aiFeatures", "aiAgents", "capabilities"):
            val = data.get(key)
            if isinstance(val, list) and len(val) > 0:
                items = val
                break
            if isinstance(val, dict):
                for subkey in ("results", "value"):
                    subval = val.get(subkey)
                    if isinstance(subval, list) and len(subval) > 0:
                        items = subval
                        break
                if items:
                    break

    if not items or not isinstance(items[0], dict):
        return []

    item_keys = set(items[0].keys())
    if "name" in item_keys and len(item_keys & FEATURE_KEYWORDS) >= 2:
        return items
    if any("capabilities" in (it or {}) for it in items if isinstance(it, dict)):
        return items

    return []


# ── Strategy 2: DOM scraping ─────────────────────────────────────────────

async def try_dom_scraping(page) -> list:
    """Extract data directly from rendered DOM elements."""
    print("  [DOM] Attempting DOM scraping...")

    try:
        await page.wait_for_selector(
            '[class*="card"], [class*="Card"], [class*="tile"], [class*="Tile"], '
            '[class*="feature"], [class*="Feature"]',
            timeout=10_000
        )
    except Exception:
        print("  [DOM] No card elements found on page")

    # Try extracting from page's JavaScript state (UI5 models)
    try:
        data = await page.evaluate("""() => {
            if (typeof sap !== 'undefined' && sap.ui) {
                try {
                    const core = sap.ui.getCore();
                    const models = core.getModel && core.getModel();
                    if (models) return JSON.stringify(models.getData());
                } catch(e) {}
            }
            for (const key of Object.keys(window)) {
                const val = window[key];
                if (Array.isArray(val) && val.length > 10 && val[0] && val[0].name) {
                    return JSON.stringify(val);
                }
            }
            return null;
        }""")
        if data:
            parsed = json.loads(data)
            items = _extract_items_from_json(parsed) if isinstance(parsed, dict) else parsed
            if items and len(items) > 5:
                print(f"  [DOM] Extracted {len(items)} items from page JS state")
                return items
    except Exception as e:
        print(f"  [DOM] JS state extraction failed: {e}")

    return []


# ── Fetch orchestrator ────────────────────────────────────────────────────

async def fetch_catalog() -> list:
    """
    Visit ALL filter URLs in a group to capture every category.
    Don't stop after the first URL — combine data from all visits.
    Only move to the next group (e.g. dev fallback) if the current group got nothing.
    """
    all_items = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for group_idx, url_group in enumerate(PAGE_URL_GROUPS):
            group_items = []

            for url in url_group:
                print(f"\n{'='*60}")
                print(f"Trying: {url}")
                print(f"{'='*60}")

                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/130.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080}
                )
                page = await context.new_page()

                try:
                    print(f"  Loading page...")
                    await page.goto(url, wait_until="networkidle", timeout=90_000)
                except Exception as e:
                    print(f"  Page load issue: {e}")

                # Strategy 1: API interception
                api_data = await try_api_intercept(page)
                if api_data:
                    group_items.extend(api_data)
                    print(f"  [API] Success: {len(api_data)} items")
                else:
                    print(f"  [API] No data from API interception")

                    # Strategy 2: DOM scraping (only if API failed for this URL)
                    dom_data = await try_dom_scraping(page)
                    if dom_data:
                        group_items.extend(dom_data)
                        print(f"  [DOM] Success: {len(dom_data)} items")
                    else:
                        print(f"  [DOM] No data from DOM scraping")

                await context.close()

            # If this group got data, use it and skip remaining groups
            if group_items:
                all_items = group_items
                print(f"\n  Group {group_idx+1}: captured {len(group_items)} total items (before dedup)")
                break
            else:
                print(f"\n  Group {group_idx+1}: no data — trying next group")

        await browser.close()

    # Deduplicate by ID (items from multiple filter URLs may overlap)
    seen_ids = set()
    unique = []
    for item in all_items:
        pid = item.get("id") or item.get("name", "")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            unique.append(item)

    if all_items:
        print(f"  After deduplication: {len(unique)} unique items (from {len(all_items)} raw)")

    return unique


# ── Build catalog entries from raw API data ───────────────────────────────

def build_catalog(raw: list) -> list:
    catalog = []
    seen = set()

    # ── DEBUG: Dump the FULL structure of the first item ──
    if raw:
        first = raw[0]
        print(f"\n{'─'*60}")
        print("DEBUG — API DATA STRUCTURE (first product group)")
        print(f"{'─'*60}")
        print(f"  Product group fields: {sorted(first.keys())}")
        # Print ALL field values (truncated)
        for k, v in first.items():
            if k == "capabilities":
                print(f"  {k}: [{len(v)} capabilities]" if isinstance(v, list) else f"  {k}: {v}")
            else:
                val_str = str(v)[:100]
                print(f"  {k}: {val_str}")

        caps = first.get("capabilities", [])
        if caps and isinstance(caps, list) and isinstance(caps[0], dict):
            print(f"\n  First capability fields: {sorted(caps[0].keys())}")
            for k, v in caps[0].items():
                val_str = str(v)[:100]
                print(f"    {k}: {val_str}")
        print(f"{'─'*60}\n")

    for item in raw:
        if not isinstance(item, dict):
            continue

        capabilities = item.get("capabilities", [])

        if isinstance(capabilities, list) and capabilities:
            product_name = (item.get("name") or "").strip()
            for cap in capabilities:
                if not isinstance(cap, dict):
                    continue
                # Pass the parent product group so map_type can check both levels
                entry = _extract_entry(cap, product_name, product_group=item)
                if entry and entry["key"] not in seen:
                    seen.add(entry["key"])
                    catalog.append(entry["data"])
        else:
            entry = _extract_entry(item, item.get("productName", ""))
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                catalog.append(entry["data"])

    return catalog


def _extract_entry(cap: dict, product_name: str, product_group: dict = None):
    name = (cap.get("name") or cap.get("title") or cap.get("featureName") or "").strip()
    if not name:
        return None

    product = (cap.get("productName") or cap.get("product") or product_name or "").strip()
    desc = (
        cap.get("shortDescription") or cap.get("description") or
        cap.get("Description") or cap.get("summary") or
        cap.get("abstract") or ""
    ).strip()
    ribbons = (cap.get("ribbons") or "").strip()

    return {
        "key": (name.lower(), product.lower()),
        "data": {
            "name": name,
            "product": product,
            "desc": desc,
            "type": map_type(cap, product_group),   # ← NOW checks both levels
            "icon": map_icon(name, ribbons),
        }
    }


# ── Read existing catalog from HTML ───────────────────────────────────────

def read_existing_catalog(content: str) -> list:
    """Parse the existing UC_CATALOG array from the HTML file."""
    START = "const UC_CATALOG = "
    start_idx = content.find(START)
    if start_idx == -1:
        return []

    bracket_start = start_idx + len(START)

    # Find the matching closing bracket
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


# ── Merge logic ───────────────────────────────────────────────────────────

def merge_catalogs(existing: list, new_entries: list) -> dict:
    """
    Merge new data INTO the existing catalog.

    Returns a dict with:
      - catalog:  the merged list
      - added:    count of new entries added
      - updated:  count of existing entries updated
      - removed:  count of entries removed (no longer on SAP)
      - unchanged: count of entries that stayed the same
    """
    # Build lookup from existing catalog by (name_lower, product_lower)
    existing_map = {}
    for item in existing:
        key = (item.get("name", "").lower(), item.get("product", "").lower())
        existing_map[key] = item

    # Build lookup from new catalog
    new_map = {}
    for item in new_entries:
        key = (item.get("name", "").lower(), item.get("product", "").lower())
        new_map[key] = item

    merged = []
    added = 0
    updated = 0
    removed = 0
    unchanged = 0

    # 1) Walk through new entries — add or update
    for key, new_item in new_map.items():
        if key in existing_map:
            old_item = existing_map[key]
            # Check if anything actually changed
            if (old_item.get("desc") != new_item.get("desc") or
                old_item.get("type") != new_item.get("type") or
                old_item.get("icon") != new_item.get("icon") or
                old_item.get("product") != new_item.get("product")):
                merged.append(new_item)
                updated += 1
            else:
                merged.append(old_item)  # keep existing (no change)
                unchanged += 1
        else:
            merged.append(new_item)
            added += 1

    # 2) Identify removed entries (in existing but NOT in new)
    for key, old_item in existing_map.items():
        if key not in new_map:
            removed += 1
            # Do NOT add to merged — it's been deleted from SAP

    return {
        "catalog": merged,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
    }


# ── Write catalog back to HTML ────────────────────────────────────────────

def write_catalog_to_html(catalog: list) -> bool:
    """Replace the UC_CATALOG array in the HTML file. Returns True on success."""
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

    # Find matching ]
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
        print("ERROR: No semicolon found after UC_CATALOG closing ]")
        return False

    end_semi = end_bracket + semi_off
    new_json = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    updated = content[:start_idx] + f"const UC_CATALOG = {new_json};" + content[end_semi + 1:]

    path.write_text(updated, encoding="utf-8")
    return True


# ── Main ──────────────────────────────────────────────────────────────────

async def main():
    print("SAP AI Catalog Auto-Updater (v4 — merge mode)")
    print("=" * 60)

    # ── Step 1: Read existing catalog ─────────────────────────────────
    path = Path(HTML_FILE)
    if not path.exists():
        print(f"ERROR: {HTML_FILE} not found — cannot continue.")
        sys.exit(1)  # ← REAL ERROR — tell the user

    html_content = path.read_text(encoding="utf-8-sig")
    existing = read_existing_catalog(html_content)
    print(f"Existing catalog: {len(existing)} entries")

    # ── Step 2: Fetch new data from SAP ───────────────────────────────
    raw = await fetch_catalog()

    if not raw:
        print(f"\n{'='*60}")
        print("INFO: No data captured from SAP Discovery Center.")
        print("  This usually means SAP's backend is temporarily down.")
        print("  Existing catalog is UNCHANGED.")
        print(f"  Keeping all {len(existing)} existing entries.")
        print("  Exiting with SUCCESS (no harm done).")
        print(f"{'='*60}")
        sys.exit(0)  # ← No data = success, catalog untouched

    print(f"\nFetched {len(raw)} raw items from SAP")

    # ── Step 3: Process new data ──────────────────────────────────────
    new_entries = build_catalog(raw)
    print(f"Processed into {len(new_entries)} unique feature entries")

    if not new_entries:
        print("INFO: Could not process features from raw data.")
        print(f"  Keeping all {len(existing)} existing entries.")
        sys.exit(0)  # ← No processable data = success, catalog untouched

    # ── Step 4: Merge ─────────────────────────────────────────────────
    result = merge_catalogs(existing, new_entries)
    merged = result["catalog"]

    print(f"\n{'='*60}")
    print("MERGE REPORT")
    print(f"{'='*60}")
    print(f"  Existing entries:  {len(existing)}")
    print(f"  SAP entries:       {len(new_entries)}")
    print(f"  ─────────────────────────────")
    print(f"  Added (new):       +{result['added']}")
    print(f"  Updated:           ~{result['updated']}")
    print(f"  Removed (deleted): -{result['removed']}")
    print(f"  Unchanged:          {result['unchanged']}")
    print(f"  ─────────────────────────────")
    print(f"  Final catalog:     {len(merged)} entries")
    print(f"{'='*60}")

    # ── Step 5: Safety check ──────────────────────────────────────────
    if len(merged) == 0 and len(existing) > 0:
        print("ERROR: Merge produced 0 entries but existing catalog had data.")
        print("  This should never happen. Refusing to wipe catalog.")
        sys.exit(1)  # ← REAL ERROR — something is very wrong

    # ── Step 6: Write if changed ──────────────────────────────────────
    if result["added"] == 0 and result["updated"] == 0 and result["removed"] == 0:
        print("\nNo changes detected. Catalog is already up to date.")
        sys.exit(0)

    if not write_catalog_to_html(merged):
        print("ERROR: Failed to write updated catalog to HTML file.")
        sys.exit(1)  # ← REAL ERROR — file write failed

    print(f"\nHTML updated — {len(merged)} entries written to {HTML_FILE}")
    print("Done — catalog merge complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"SCRIPT ERROR: {e}")
        print(f"{'='*60}")
        traceback.print_exc()
        sys.exit(1)  # ← REAL ERROR — unhandled exception, tell the user
