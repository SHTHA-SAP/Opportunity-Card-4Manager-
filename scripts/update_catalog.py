"""
SAP AI Catalog Auto-Updater
Uses Playwright to intercept the live API call from the Discovery Center page.
No login or stored credentials needed.
"""

import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

PAGE_URL  = "https://discovery-center.cloud.sap/ai-catalog/"
API_PATH  = "/api/v1/ai/features"
HTML_FILE = "Opportunity-Card-4Manager-v4.html"


# ── Icon + type mapping ───────────────────────────────────────────────────

def map_icon(name: str, ribbons: str) -> str:
    if "agent" in name.lower():
        return "agents"
    if "joule" in (ribbons or "").lower():
        return "joule"
    return "aiFeatures"

def map_type(cap: dict) -> str:
    """Try every possible field that might indicate Base vs Premium."""
    candidates = [
        cap.get("commercialType"),
        cap.get("CommercialType"),
        cap.get("package"),
        cap.get("Package"),
        cap.get("licenseModel"),
        cap.get("LicenseModel"),
        cap.get("packageName"),
        cap.get("ribbons"),   # sometimes "Joule Premium" appears here
        cap.get("tier"),
    ]
    for val in candidates:
        if val and "premium" in str(val).lower():
            return "premium"
    return "base"


# ── Fetch ─────────────────────────────────────────────────────────────────

async def fetch_catalog() -> list:
    result = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def on_response(response):
            if API_PATH in response.url:
                try:
                    data = await response.json()
                    if isinstance(data, list):
                        result.extend(data)
                    elif isinstance(data, dict):
                        for key in ("results", "value", "data", "features", "items"):
                            if key in data and isinstance(data[key], list):
                                result.extend(data[key])
                                break
                    print(f"  Captured {len(result)} raw items from {response.url}")
                except Exception as e:
                    print(f"  Could not parse response: {e}")

        page.on("response", on_response)

        print(f"Opening {PAGE_URL} ...")
        try:
            await page.goto(PAGE_URL, wait_until="networkidle", timeout=90_000)
        except Exception:
            pass

        await page.wait_for_timeout(5_000)
        await browser.close()

    # Deduplicate product groups by id
    seen_ids = set()
    unique   = []
    for item in result:
        pid = item.get("id") or item.get("name", "")
        if pid not in seen_ids:
            seen_ids.add(pid)
            unique.append(item)

    return unique


# ── Build catalog from nested capabilities ────────────────────────────────

def build_catalog(raw: list) -> list:
    catalog = []
    seen    = set()

    # ── Debug: show full structure of first capability ──
    if raw:
        first_group = raw[0]
        print(f"\nDEBUG top-level fields : {list(first_group.keys())}")
        caps = first_group.get("capabilities", [])
        if caps:
            print(f"DEBUG capability fields: {list(caps[0].keys())}")
            print(f"DEBUG first capability :\n{json.dumps(caps[0], indent=2, ensure_ascii=False)}\n")

    for product_group in raw:
        if not isinstance(product_group, dict):
            continue

        # Top-level name = the SAP product name
        product_name = (product_group.get("name") or "").strip()
        capabilities = product_group.get("capabilities", [])

        if not isinstance(capabilities, list):
            continue

        for cap in capabilities:
            if not isinstance(cap, dict):
                continue

            name = (cap.get("name") or "").strip()
            if not name:
                continue

            # Description — use shortDescription (confirmed from debug)
            desc = (
                cap.get("shortDescription") or
                cap.get("description")      or
                cap.get("Description")      or
                cap.get("summary")          or
                cap.get("abstract")         or ""
            ).strip()

            ribbons = (cap.get("ribbons") or "").strip()

            key = (name.lower(), product_name.lower())
            if key in seen:
                continue
            seen.add(key)

            catalog.append({
                "name":    name,
                "product": product_name,
                "desc":    desc,
                "type":    map_type(cap),
                "icon":    map_icon(name, ribbons),
            })

    return catalog


# ── Update the HTML file ───────────────────────────────────────────────────

def update_html(catalog: list) -> bool:
    path = Path(HTML_FILE)
    if not path.exists():
        print(f"ERROR: {HTML_FILE} not found")
        return False

    content = path.read_text(encoding="utf-8-sig")  # utf-8-sig strips BOM if present

    # ── Robust string-based search (no regex) ──
    START = "const UC_CATALOG = ["
    start_idx = content.find(START)

    if start_idx == -1:
        # Try to help diagnose
        print("WARNING: Could not find 'const UC_CATALOG = [' in file")
        print(f"  File size : {len(content):,} characters")
        uc = content.find("UC_CATALOG")
        if uc != -1:
            print(f"  Found 'UC_CATALOG' at position {uc}")
            print(f"  Surrounding text: ...{repr(content[max(0,uc-30):uc+60])}...")
        else:
            print("  'UC_CATALOG' not found at all in file")
        return False

    # Walk forward from [ tracking bracket depth to find the matching ]
    bracket_start = start_idx + len("const UC_CATALOG = ")  # points to [
    depth         = 0
    in_string     = False
    escape_next   = False
    end_bracket   = -1

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
        print("WARNING: Could not find closing ] of UC_CATALOG")
        return False

    # Find the ; immediately after ]
    tail      = content[end_bracket: end_bracket + 10]
    semi_off  = tail.find(";")
    if semi_off == -1:
        print("WARNING: No semicolon found after UC_CATALOG closing ]")
        return False

    end_semi = end_bracket + semi_off  # index of ;

    # Replace everything from start to end (inclusive of ;)
    new_json = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    updated  = content[:start_idx] + f"const UC_CATALOG = {new_json};" + content[end_semi + 1:]

    path.write_text(updated, encoding="utf-8")
    print(f"HTML updated — {len(catalog)} entries written")
    return True


# ── Main ──────────────────────────────────────────────────────────────────

async def main():
    print("SAP AI Catalog Auto-Updater")
    print("-" * 40)

    raw = await fetch_catalog()

    if not raw:
        print("ERROR: No data captured from the page.")
        sys.exit(1)

    print(f"Fetched {len(raw)} product groups")
    catalog = build_catalog(raw)
    print(f"Processed {len(catalog)} unique feature entries")

    if not catalog:
        print("ERROR: No features could be extracted.")
        sys.exit(1)

    if not update_html(catalog):
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
