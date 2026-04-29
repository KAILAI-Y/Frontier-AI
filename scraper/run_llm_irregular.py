#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scraper.agents.llm_irregular_extraction import (  # noqa: E402
    GeminiIrregularExtractionAgent,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    )
}


def load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_problem_urls(report_path: Path, limit: int | None) -> list[dict[str, str]]:
    rows = list(csv.DictReader(report_path.open()))
    filtered = [
        row
        for row in rows
        if row.get("Page Type") == "unsupported_layout"
        and row.get("Error Type") == "no_master_data"
    ]
    return filtered[:limit] if limit is not None else filtered


def fetch_page(url: str, timeout: int) -> tuple[str, str]:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    main = (
        soup.select_one("main")
        or soup.select_one(".product-info-main")
        or soup.select_one(".columns")
        or soup.body
    )
    html_snippet = str(main)[:20000] if main else html[:20000]
    text_snippet = (
        main.get_text(" ", strip=True)[:6000] if main else soup.get_text(" ", strip=True)[:6000]
    )
    return html_snippet, text_snippet


def write_report(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "Product URL",
        "Reported Page Type",
        "LLM Page Type",
        "Found More Info",
        "LLM Item Count",
        "LLM Item Numbers",
        "Notes",
        "Raw Items JSON",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default=str(OUTPUT_DIR / "crawl_report.csv"))
    parser.add_argument("--output", default=str(OUTPUT_DIR / "llm_irregular_report.csv"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(PROJECT_DIR / ".env")
    limit = args.limit or None
    problem_rows = load_problem_urls(Path(args.report), limit)
    agent = GeminiIrregularExtractionAgent(model=args.model)
    output_rows: list[dict[str, str]] = []

    for index, row in enumerate(problem_rows, start=1):
        product_url = (row.get("Product URL") or "").strip()
        logging.info("LLM irregular extraction %s/%s for %s", index, len(problem_rows), product_url)
        try:
            html_snippet, text_snippet = fetch_page(product_url, timeout=args.timeout)
            result = agent.extract_from_html(
                product_url=product_url,
                product_name="",
                category_hierarchy="",
                html_snippet=html_snippet,
                text_snippet=text_snippet,
            )
            output_rows.append(
                {
                    "Product URL": product_url,
                    "Reported Page Type": row.get("Page Type", ""),
                    "LLM Page Type": result.page_type,
                    "Found More Info": "yes" if result.found_more_info else "no",
                    "LLM Item Count": str(result.item_count),
                    "LLM Item Numbers": "|".join(result.item_numbers),
                    "Notes": result.notes,
                    "Raw Items JSON": result.raw_items_json,
                }
            )
        except Exception as exc:
            output_rows.append(
                {
                    "Product URL": product_url,
                    "Reported Page Type": row.get("Page Type", ""),
                    "LLM Page Type": "",
                    "Found More Info": "error",
                    "LLM Item Count": "",
                    "LLM Item Numbers": "",
                    "Notes": agent.sanitize_error_message(str(exc)),
                    "Raw Items JSON": "",
                }
            )

    write_report(output_rows, Path(args.output))
    logging.info("Wrote LLM irregular extraction report to %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
