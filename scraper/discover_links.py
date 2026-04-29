#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
CATALOG_BASE_URL = "https://www.safcodental.com/catalog/"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scraper.agents.category_navigation import CategoryNavigationAgent  # noqa: E402
from scraper.agents.models import ProductLinkRecord  # noqa: E402

DEFAULT_CATEGORIES = [
    "gloves",
    "sutures-surgical-products",
]
LINK_FIELDS = ["category_url", "page_number", "product_url", "anchor_text"]


def write_csv(records: list[ProductLinkRecord], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LINK_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "category_url": record.category_url,
                    "page_number": record.page_number,
                    "product_url": record.product_url,
                    "anchor_text": record.anchor_text,
                }
            )


def normalize_category_input(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"{CATALOG_BASE_URL}{raw.lstrip('/')}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--category",
        action="append",
        help="Category slug or full URL to crawl. Example: gloves",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum visible pages to crawl per category. Use 0 for no limit.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_DIR / "product_links.csv"),
        help="CSV output path.",
    )
    args = parser.parse_args()

    raw_categories = args.category or DEFAULT_CATEGORIES
    categories = []
    for category in raw_categories:
        normalized = normalize_category_input(category)
        if normalized:
            categories.append(normalized)
    max_pages = args.max_pages or None

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        agent = CategoryNavigationAgent(browser=browser)
        records = agent.discover(categories, max_pages=max_pages)
        browser.close()

    output_path = Path(args.output)
    write_csv(records, output_path)
    logging.info("Wrote %s visible product links to %s", len(records), output_path)
    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
