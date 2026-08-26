"""
SAP AI Catalog Auto-Updater
Uses Playwright to intercept the live API call from the Discovery Center page.
No login or stored credentials needed.
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright

PAGE_URL  = "https://discovery-center.cloud.sap/ai-catalog/"
API_PATH  = "/api/v1/ai/features"
HTML_FILE = "Opportunity-Card-4Manager-v4.html"


def map_icon(name: str, quick_filters: str, package: str) -> str:
    if "agent" in name.lower():
        return "agents"
    if "joule" in (quick_filters + package).lower():
        return "joule"
    return "aiFeatures"

def map_type(commercial_type: str) -> str:
    return "premium" if "premium" in (commercial_type or "").lower() else "base"


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
                    print(f"  Captured {len(result)} entries from {response.url}")
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

    return result


def build_catalog(raw: list) -> list:
    catalog = []
    seen    = set()

    # Debug: print all field names from the first entry
    # so we can see exactly what the API returns
    if raw and isinstance(raw[0], dict):
        print("\nDEBUG — API field names:", list(raw[0].keys()))
        print("DEBUG — First entry sample:")
        print(json.dumps(raw[0], indent=2, ensure_ascii=False)[:600])
        print()

    for f in raw:
        if not isinstance(f, dict):
            continue

        name = (
            f.get("name") or f.get("Name") or
            f.get("title") or f.get("Title") or ""
        ).strip()

        product = (
            f.get("product") or f.get("productName") or
            f.get("Product") or f.get("ProductName") or
            f.get("solution") or f.get("Solution") or ""
        ).strip()

        # Try every common field name SAP might use for description
        desc = (
            f.get("description") or
            f.get("Description") or
            f.get("shortDescription") or
            f.get("ShortDescription") or
            f.get("short_description") or
            f.get("summary") or
            f.get("Summary") or
            f.get("longDescription") or
            f.get("LongDescription") or
            f.get("abstract") or
            f.get("Abstract") or
            f.get("text") or
            f.get("Text") or ""
        ).strip()

        ctype = (
            f.get("commercialType") or f.get("CommercialType") or
            f.get("licenseModel") or f.get("LicenseModel") or
            f.get("package") or f.get("Package") or
            f.get("tier") or f.get("Tier") or ""
        )

        qf  = (f.get("quickFilters")  or f.get("QuickFilters")  or
               f.get("tags")          or f.get("Tags")          or "")
        pkg = (f.get("package")       or f.get("Package")       or
               f.get("packageName")   or f.get("PackageName")   or "")

        if not name:
            continue

        key = (name.lower(), product.lower())
        if key in seen:
            continue
        seen.add(key)

        catalog.append({
            "name":    name,
            "product": product,
            "desc":    desc,
            "type":    map_type(ctype),
            "icon":    map_icon(name, qf, pkg),
        })

    return catalog


def update_html(catalog: list) -> bool:
    path = Path(HTML_FILE)
    if not path.exists():
        print(f"ERROR: {HTML_FILE} not found"); return False

    content  = path.read_text(encoding="utf-8")
    new_json = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))

    updated = re.sub(
        r"const UC_CATALOG = \[.*?\];",
        f"const UC_CATALOG = {new_json};",
        content,
        flags=re.DOTALL,
    )

    if updated == content:
        print("WARNING: UC_CATALOG block not found")
        return False

    path.write_text(updated, encoding="utf-8")
    print(f"HTML updated — {len(catalog)} entries written")
    return True


async def main():
    print("SAP AI Catalog Auto-Updater")
    print("-" * 40)

    raw = await fetch_catalog()

    if not raw:
        print("ERROR: No data captured.")
        sys.exit(1)

    catalog = build_catalog(raw)
    print(f"Processed {len(catalog)} unique entries")

    if not update_html(catalog):
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
