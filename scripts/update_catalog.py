"""
SAP AI Catalog Auto-Updater  (v5 — merge mode + timeout fix)
==============================================================
Fetches ALL three categories from SAP Discovery Center:
  - AI Features
  - Joule (assistant)
  - AI Agents

MERGE logic: adds new entries, updates changed ones, removes deleted ones.
Never replaces the full catalog — only applies diffs.

EXIT CODES:
  0  = success (data merged) or no data captured (site down — catalog untouched)
  1  = real error (file missing, HTML broken, script crash)
"""

import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# ── Configuration ────────────────────────────────────────────────────────

URLS = [
    "https://discovery-center.cloud.sap/ai-catalog/",
    "https://discovery-center-dev.cfapps.eu10-004.hana.ondemand.com/ai-catalog",
]

# The SAP backend domain that serves the catalog data
SAP_BACKEND_DOMAIN = "platformxproxy"

HTML_FILE = "Opportunity-Card-4Manager-v4.html"

# Timeout for page navigation (ms)
NAV_TIMEOUT   = 60_000
# How long to wait for API data after page load (ms)
DATA_WAIT     = 30_000
# How long to wait after scrolling for lazy-loaded content (ms)
SCROLL_WAIT   = 5_000


# ── Icon + type mapping ─────────────────────────────────────────────────

def map_icon(name: str, ribbons: str) -> str:
    n = name.lower()
    r = (ribbons or "").lower()
    if "agent" in n:
        return "agents"
    if "joule" in n or "joule" in r:
        return "joule"
    return "aiFeatures"

def map_type(cap: dict) -> str:
    """Check multiple fields for Premium vs Base."""
    candidates = [
        cap.get("commercialType"),
        cap.get("CommercialType"),
        cap.get("package"),
        cap.get("Package"),
        cap.get("licenseModel"),
        cap.get("LicenseModel"),
        cap.get("packageName"),
        cap.get("ribbons"),
        cap.get("tier"),
    ]
    for val in candidates:
        if val and "premium" in str(val).lower():
            return "premium"
    return "base"


# ── Fetch from SAP Discovery Center ─────────────────────────────────────

async def fetch_catalog() -> list:
    """
    Try each URL.  Key fix: use 'domcontentloaded' instead of 'networkidle'
    (the SAP page never reaches networkidle due to persistent background
    requests — analytics, heartbeats, etc. — causing 90s timeouts).
    
    Intercepts ALL JSON responses from the SAP backend domain, not just
    a specific API path, to catch data regardless of endpoint changes.
    """
    all_captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for url in URLS:
            print(f"\n{'='*60}")
            print(f"Trying: {url}")
            print(f"{'='*60}")

            captured = []

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # ── Strategy 1: Intercept ALL JSON responses from SAP backend ──
            async def on_response(response):
                try:
                    resp_url = response.url
                    # Catch any JSON from the SAP platform proxy backend
                    if SAP_BACKEND_DOMAIN in resp_url:
                        content_type = response.headers.get("content-type", "")
                        if "json" in content_type or "/api/" in resp_url:
                            try:
                                data = await response.json()
                                items = []
                                if isinstance(data, list):
                                    items = data
                                elif isinstance(data, dict):
                                    for key in ("results", "value", "data", "features", "items", "d"):
                                        if key in data and isinstance(data[key], list):
                                            items = data[key]
                                            break
                                    # Also check nested d.results (OData pattern)
                                    if not items and "d" in data and isinstance(data["d"], dict):
                                        for key in ("results", "value"):
                                            if key in data["d"] and isinstance(data["d"][key], list):
                                                items = data["d"][key]
                                                break
                                if items and len(items) > 5:  # Only count meaningful payloads
                                    captured.extend(items)
                                    print(f"  [API] Captured {len(items)} items from: {resp_url[:100]}...")
                            except Exception:
                                pass  # Not JSON or parse error — skip silently
                except Exception:
                    pass

            page.on("response", on_response)

            # ── Navigate with domcontentloaded (NOT networkidle!) ──
            print("  Loading page (domcontentloaded)...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                print("  Page DOM loaded successfully")
            except Exception as e:
                print(f"  Page load issue: {e}")
                await context.close()
                continue

            # ── Click consent/cookie buttons if present ──
            for selector in [
                "button:has-text('Accept')",
                "button:has-text('Accept All')",
                "button:has-text('Agree')",
                "button:has-text('OK')",
                "[id*='consent'] button",
                "[class*='consent'] button",
                "[class*='cookie'] button",
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        print(f"  Clicked consent button: {selector}")
                        await page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # ── Wait for API data to arrive ──
            print(f"  Waiting up to {DATA_WAIT // 1000}s for API data...")
            elapsed = 0
            check_interval = 2000
            while elapsed < DATA_WAIT and not captured:
                await page.wait_for_timeout(check_interval)
                elapsed += check_interval

            if captured:
                print(f"  [API] Success: {len(captured)} total items captured")
            else:
                print("  [API] No data from API interception after wait")

            # ── Strategy 2: DOM scraping fallback ──
            if not captured:
                print("  [DOM] Attempting DOM scraping...")
                try:
                    # Wait a bit more for rendering
                    await page.wait_for_timeout(5000)
                    
                    # Try to scroll down to trigger lazy loading
                    for i in range(5):
                        await page.evaluate("window.scrollBy(0, 1000)")
                        await page.wait_for_timeout(1000)
                    
                    # Check if API data arrived during scrolling
                    if captured:
                        print(f"  [DOM+Scroll] Got {len(captured)} items after scrolling")
                    else:
                        # Try extracting from __NEXT_DATA__ or similar JS state
                        js_data = await page.evaluate("""() => {
                            // Check Next.js data
                            if (window.__NEXT_DATA__) return JSON.stringify(window.__NEXT_DATA__);
                            // Check Angular/React state
                            const scripts = document.querySelectorAll('script[type="application/json"]');
                            for (const s of scripts) {
                                if (s.textContent.length > 1000) return s.textContent;
                            }
                            return null;
                        }""")
                        if js_data:
                            try:
                                parsed = json.loads(js_data)
                                # Recursively search for arrays of items with 'name' and 'capabilities'
                                found = _find_capability_arrays(parsed)
                                if found:
                                    captured.extend(found)
                                    print(f"  [JS State] Extracted {len(found)} items from page state")
                            except Exception:
                                pass

                        if not captured:
                            print("  [DOM] No data from DOM scraping")
                except Exception as e:
                    print(f"  [DOM] Error: {e}")

            await context.close()

            if captured:
                all_captured = captured
                break  # Got data — no need to try other URLs

        await browser.close()

    # Deduplicate by ID
    seen_ids = set()
    unique = []
    for item in all_captured:
        pid = item.get("id") or item.get("name", "")
        if pid not in seen_ids:
            seen_ids.add(pid)
            unique.append(item)

    return unique


def _find_capability_arrays(obj, depth=0):
    """Recursively search a JSON object for arrays of capability-like items."""
    if depth > 10:
        return []
    results = []
    if isinstance(obj, list):
        # Check if this looks like a capabilities array
        if len(obj) > 5 and all(isinstance(x, dict) for x in obj[:5]):
            if any("capabilities" in x or "name" in x for x in obj[:5]):
                results.extend(obj)
        for item in obj:
            results.extend(_find_capability_arrays(item, depth + 1))
    elif isinstance(obj, dict):
        for val in obj.values():
            results.extend(_find_capability_arrays(val, depth + 1))
    return results


# ── Build catalog from nested capabilities ───────────────────────────────

def build_catalog(raw: list) -> list:
    """
    Extract individual features from product groups.
    Each product group has a 'capabilities' array with the actual features.
    """
    catalog = []
    seen = set()

    # Debug: show structure of first item
    if raw:
        first = raw[0]
        print(f"\nDEBUG top-level fields: {list(first.keys())}")
        caps = first.get("capabilities", [])
        if caps:
            print(f"DEBUG capability fields: {list(caps[0].keys())}")

    for product_group in raw:
        if not isinstance(product_group, dict):
            continue

        product_name = (product_group.get("name") or "").strip()
        capabilities = product_group.get("capabilities", [])

        if not isinstance(capabilities, list):
            # Maybe this item IS a capability (flat structure)
            if "name" in product_group and ("shortDescription" in product_group or "description" in product_group):
                cap = product_group
                name = (cap.get("name") or "").strip()
                if not name:
                    continue
                desc = (
                    cap.get("shortDescription") or
                    cap.get("description") or
                    cap.get("Description") or ""
                ).strip()
                ribbons = (cap.get("ribbons") or "").strip()
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    catalog.append({
                        "name": name,
                        "product": product_name or name,
                        "desc": desc,
                        "type": map_type(cap),
                        "icon": map_icon(name, ribbons),
                    })
            continue

        for cap in capabilities:
            if not isinstance(cap, dict):
                continue

            name = (cap.get("name") or "").strip()
            if not name:
                continue

            desc = (
                cap.get("shortDescription") or
                cap.get("description") or
                cap.get("Description") or
                cap.get("summary") or
                cap.get("abstract") or ""
            ).strip()

            ribbons = (cap.get("ribbons") or "").strip()

            key = (name.lower(), product_name.lower())
            if key in seen:
                continue
            seen.add(key)

            catalog.append({
                "name": name,
                "product": product_name,
                "desc": desc,
                "type": map_type(cap),
                "icon": map_icon(name, ribbons),
            })

    return catalog


# ── Read existing catalog from the HTML ──────────────────────────────────

def read_existing_catalog() -> list:
    """Parse the existing UC_CATALOG from the HTML file."""
    path = Path(HTML_FILE)
    if not path.exists():
        print(f"ERROR: {HTML_FILE} not found in working directory")
        sys.exit(1)

    content = path.read_text(encoding="utf-8-sig")
    START = "const UC_CATALOG = ["
    start_idx = content.find(START)

    if start_idx == -1:
        print("ERROR: Could not find 'const UC_CATALOG = [' in HTML file")
        sys.exit(1)

    # Find the matching ]
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
        sys.exit(1)

    json_str = content[bracket_start:end_bracket + 1]
    try:
        existing = json.loads(json_str)
        return existing
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse existing UC_CATALOG JSON: {e}")
        sys.exit(1)


# ── Merge logic ──────────────────────────────────────────────────────────

def merge_catalogs(existing: list, new_entries: list) -> tuple:
    """
    Merge new data INTO existing catalog:
      - Add entries that are new (not in existing)
      - Update entries that changed (same name+product, different desc/type/icon)
      - Remove entries that are no longer on SAP (deleted)
      - Keep entries that are unchanged

    Returns: (merged_catalog, stats_dict)
    """
    # Build lookup maps: key = (name.lower(), product.lower())
    existing_map = {}
    for entry in existing:
        key = (entry["name"].lower(), entry["product"].lower())
        existing_map[key] = entry

    new_map = {}
    for entry in new_entries:
        key = (entry["name"].lower(), entry["product"].lower())
        new_map[key] = entry

    added = []
    updated = []
    removed = []
    unchanged = []

    # Check what's new or changed
    for key, new_entry in new_map.items():
        if key not in existing_map:
            added.append(new_entry)
        else:
            old_entry = existing_map[key]
            # Check if anything changed
            if (old_entry.get("desc") != new_entry.get("desc") or
                old_entry.get("type") != new_entry.get("type") or
                old_entry.get("icon") != new_entry.get("icon")):
                updated.append(new_entry)
            else:
                unchanged.append(old_entry)

    # Check what was removed from SAP
    for key, old_entry in existing_map.items():
        if key not in new_map:
            removed.append(old_entry)

    # Build merged catalog: keep unchanged + updated + added (skip removed)
    merged = unchanged + updated + added

    stats = {
        "existing_count": len(existing),
        "sap_count": len(new_entries),
        "added": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "unchanged": len(unchanged),
        "final_count": len(merged),
        "added_names": [e["name"] for e in added],
        "removed_names": [e["name"] for e in removed],
        "updated_names": [e["name"] for e in updated],
    }

    return merged, stats


# ── Write merged catalog back to HTML ────────────────────────────────────

def write_catalog_to_html(catalog: list) -> bool:
    path = Path(HTML_FILE)
    content = path.read_text(encoding="utf-8-sig")

    START = "const UC_CATALOG = ["
    start_idx = content.find(START)
    if start_idx == -1:
        print("ERROR: Could not find 'const UC_CATALOG = [' for writing")
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
        print("ERROR: Could not find closing ] of UC_CATALOG for writing")
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


# ── Print merge report ───────────────────────────────────────────────────

def print_report(stats: dict):
    print(f"\n{'='*60}")
    print("MERGE REPORT")
    print(f"{'='*60}")
    print(f"  Existing entries:  {stats['existing_count']}")
    print(f"  SAP entries:       {stats['sap_count']}")
    print(f"  {'─'*40}")
    print(f"  Added (new):       +{stats['added']}")
    print(f"  Updated:           ~{stats['updated']}")
    print(f"  Removed (deleted): -{stats['removed']}")
    print(f"  Unchanged:          {stats['unchanged']}")
    print(f"  {'─'*40}")
    print(f"  Final catalog:     {stats['final_count']} entries")
    print(f"{'='*60}")

    if stats["added_names"]:
        print(f"\n  NEW entries added:")
        for name in stats["added_names"][:20]:
            print(f"    + {name}")
        if len(stats["added_names"]) > 20:
            print(f"    ... and {len(stats['added_names']) - 20} more")

    if stats["removed_names"]:
        print(f"\n  REMOVED entries (deleted from SAP):")
        for name in stats["removed_names"][:20]:
            print(f"    - {name}")
        if len(stats["removed_names"]) > 20:
            print(f"    ... and {len(stats['removed_names']) - 20} more")

    if stats["updated_names"]:
        print(f"\n  UPDATED entries (changed on SAP):")
        for name in stats["updated_names"][:20]:
            print(f"    ~ {name}")
        if len(stats["updated_names"]) > 20:
            print(f"    ... and {len(stats['updated_names']) - 20} more")


# ── Main ─────────────────────────────────────────────────────────────────

async def main():
    print("SAP AI Catalog Auto-Updater (v5 — merge mode + timeout fix)")
    print("=" * 60)

    # Step 1: Read existing catalog
    existing = read_existing_catalog()
    print(f"Existing catalog: {len(existing)} entries")

    # Step 2: Fetch from SAP Discovery Center
    try:
        raw = await fetch_catalog()
    except Exception as e:
        print(f"\nERROR: Unexpected error during fetch: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 3: Handle no-data scenario (site down)
    if not raw:
        print(f"\n{'='*60}")
        print("INFO: No data captured from SAP Discovery Center.")
        print("  This usually means SAP's backend is temporarily down.")
        print("  Existing catalog is UNCHANGED.")
        print(f"  Keeping all {len(existing)} existing entries.")
        print("  Exiting with SUCCESS (no harm done).")
        print(f"{'='*60}")
        sys.exit(0)

    print(f"\nFetched {len(raw)} product groups from SAP")

    # Step 4: Build catalog from raw data
    new_catalog = build_catalog(raw)
    print(f"Processed into {len(new_catalog)} unique feature entries")

    if not new_catalog:
        print(f"\n{'='*60}")
        print("WARNING: Could extract product groups but no features from them.")
        print("  The API response structure may have changed.")
        print("  Existing catalog is UNCHANGED.")
        print(f"  Keeping all {len(existing)} existing entries.")
        print(f"{'='*60}")
        sys.exit(0)

    # Step 5: Merge
    merged, stats = merge_catalogs(existing, new_catalog)
    print_report(stats)

    # Step 6: Safety check — if merge result is 0 entries, something is very wrong
    if len(merged) == 0:
        print("\nERROR: Merge produced 0 entries — this should never happen.")
        print("  Existing catalog is UNCHANGED.")
        sys.exit(1)

    # Step 7: Check if anything actually changed
    if stats["added"] == 0 and stats["updated"] == 0 and stats["removed"] == 0:
        print("\nNo changes detected — catalog is already up to date.")
        sys.exit(0)

    # Step 8: Write to HTML
    if write_catalog_to_html(merged):
        print(f"\nHTML updated — {stats['final_count']} entries written to {HTML_FILE}")
        print("Done — catalog merge complete.")
        sys.exit(0)
    else:
        print("\nERROR: Failed to write merged catalog to HTML")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
