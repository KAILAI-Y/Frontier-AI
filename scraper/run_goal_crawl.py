#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scraper.agents.category_navigation import CategoryNavigationAgent  # noqa: E402
from scraper.agents.goal_planning import GoalPlanningAgent  # noqa: E402
from scraper.agents.intent_classification import IntentClassificationAgent  # noqa: E402
from scraper.agents.llm_intent_matching import GeminiIntentMatchingAgent  # noqa: E402
from scraper.agents.models import ProductLinkRecord, VisibleLinkTarget  # noqa: E402
from scraper.extract_products import (  # noqa: E402
    SafcoItemLevelScraper,
    load_checkpoint,
    write_crawl_report,
    write_csv,
)

LINK_FIELDS = ["category_url", "page_number", "product_url", "anchor_text"]


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


def write_link_csv(records: list[ProductLinkRecord], output_path: Path) -> None:
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


def records_to_targets(records: list[ProductLinkRecord]) -> list[VisibleLinkTarget]:
    return [
        VisibleLinkTarget(
            category_url=record.category_url,
            page_number=str(record.page_number),
            product_url=record.product_url,
            anchor_text=record.anchor_text,
        )
        for record in records
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", default="", help="High-level crawl goal.")
    parser.add_argument(
        "--homepage",
        default="https://www.safcodental.com/",
        help="Homepage to start planning from.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum visible category pages to crawl. Use 0 for no limit.",
    )
    parser.add_argument(
        "--max-products-per-category",
        type=int,
        default=0,
        help="Maximum products per resolved category. Use 0 for no limit.",
    )
    parser.add_argument(
        "--links-output",
        default=str(OUTPUT_DIR / "product_links.csv"),
        help="Visible product links CSV output path.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_DIR / "products.csv"),
        help="Final products CSV output path.",
    )
    parser.add_argument(
        "--report",
        default=str(OUTPUT_DIR / "crawl_report.csv"),
        help="Crawl report CSV output path.",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(OUTPUT_DIR / "checkpoint.csv"),
        help="Checkpoint CSV output path.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume extraction by skipping URLs already marked success in checkpoint.csv.",
    )
    parser.add_argument(
        "--disable-llm-match",
        action="store_true",
        help="Disable Gemini-based category matching and use rule-based ranking only.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(PROJECT_DIR / ".env")

    goal = (args.goal or "").strip()
    if not goal:
        goal = input("Enter your crawl request: ").strip()
    if not goal:
        raise SystemExit("No crawl request was provided.")

    intent_agent = IntentClassificationAgent()
    intent = intent_agent.classify(goal)
    logging.info(
        "Intent classification: type=%s match=%s confidence=%.2f reason=%s",
        intent.intent_type,
        intent.is_category_product_request,
        intent.confidence,
        intent.reason,
    )
    if not intent.is_category_product_request:
        raise SystemExit(
            "The provided goal was not classified as a category/product-information request. "
            "Current goal-driven crawl only supports product-category style goals."
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        llm_matcher = None
        if not args.disable_llm_match and os.getenv("GEMINI_API_KEY"):
            llm_matcher = GeminiIntentMatchingAgent()
        planner = GoalPlanningAgent(browser=browser, llm_matcher=llm_matcher)
        plan = planner.plan(goal=goal, homepage_url=args.homepage)
        if not plan.selected_category_url:
            browser.close()
            raise SystemExit("Could not resolve a category URL from the provided goal.")

        logging.info(
            "Goal resolved to category %s (%s)",
            plan.selected_category_url,
            plan.selected_anchor_text or plan.candidates[0].reason if plan.candidates else "",
        )
        navigator = CategoryNavigationAgent(browser=browser)
        link_records = navigator.discover(
            [plan.selected_category_url],
            max_pages=(args.max_pages or None),
        )
        browser.close()

    links_output = Path(args.links_output)
    write_link_csv(link_records, links_output)

    scraper = SafcoItemLevelScraper()
    checkpoint_path = Path(args.checkpoint)
    checkpoint_state = load_checkpoint(checkpoint_path) if args.resume else {}
    rows = scraper.scrape_visible_targets(
        records_to_targets(link_records),
        max_products_per_category=(args.max_products_per_category or None),
        checkpoint_path=checkpoint_path,
        resume=args.resume,
        checkpoint_state=checkpoint_state,
    )

    products_output = Path(args.output)
    report_output = Path(args.report)
    write_csv(rows, products_output)
    write_crawl_report(scraper.crawl_reports, report_output)

    logging.info("Goal: %s", goal)
    logging.info("Selected category: %s", plan.selected_category_url)
    logging.info("Discovered %s product links", len(link_records))
    logging.info("Wrote %s product rows to %s", len(rows), products_output)
    logging.info("Wrote crawl report to %s", report_output)
    print(products_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
