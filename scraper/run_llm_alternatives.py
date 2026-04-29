#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scraper.agents.llm_alternatives import (  # noqa: E402
    GeminiAlternativeRankingAgent,
    ProductFamilySummary,
)


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


def load_rows(input_path: Path) -> list[dict[str, str]]:
    with input_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if "Alternative Product URL" not in row:
            row["Alternative Product URL"] = ""
        if "Alternative Item Number" not in row:
            row["Alternative Item Number"] = ""
        if "Alternative Products" in row:
            row.pop("Alternative Products", None)
    return rows


def summarize_families(rows: list[dict[str, str]]) -> list[ProductFamilySummary]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        product_url = (row.get("Product URL") or "").strip()
        if product_url:
            grouped[product_url].append(row)

    summaries: list[ProductFamilySummary] = []
    for product_url, family_rows in grouped.items():
        first = family_rows[0]
        summaries.append(
            ProductFamilySummary(
                product_url=product_url,
                product_name=first.get("Product Name", ""),
                brand=first.get("Brand", ""),
                category_hierarchy=first.get("Category Hierarchy", ""),
                description=first.get("Description", ""),
                attributes=_top_attributes(family_rows),
                unit=first.get("Unit", ""),
            )
        )
    return summaries


def _top_attributes(rows: list[dict[str, str]], limit: int = 3) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = (row.get("Attributes") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
        if len(values) >= limit:
            break
    return " | ".join(values)


def build_candidate_map(
    summaries: list[ProductFamilySummary], max_candidates: int
) -> dict[str, list[ProductFamilySummary]]:
    by_category: dict[str, list[ProductFamilySummary]] = defaultdict(list)
    for summary in summaries:
        by_category[_category_key(summary.category_hierarchy)].append(summary)

    candidate_map: dict[str, list[ProductFamilySummary]] = {}
    for summary in summaries:
        pool = [
            candidate
            for candidate in by_category[_category_key(summary.category_hierarchy)]
            if candidate.product_url != summary.product_url
        ]
        scored = sorted(
            pool,
            key=lambda candidate: _candidate_score(summary, candidate),
            reverse=True,
        )
        candidate_map[summary.product_url] = scored[:max_candidates]
    return candidate_map


def _category_key(category_hierarchy: str) -> str:
    parts = [part.strip() for part in category_hierarchy.split(">") if part.strip()]
    if len(parts) >= 2:
        return " > ".join(parts[:-1])
    return category_hierarchy.strip()


def _candidate_score(current: ProductFamilySummary, candidate: ProductFamilySummary) -> float:
    score = 0.0
    if current.brand and candidate.brand and current.brand == candidate.brand:
        score += 1.0
    if current.unit and candidate.unit and current.unit == candidate.unit:
        score += 1.0
    current_tokens = _tokens(f"{current.product_name} {current.description} {current.attributes}")
    candidate_tokens = _tokens(
        f"{candidate.product_name} {candidate.description} {candidate.attributes}"
    )
    if current_tokens or candidate_tokens:
        overlap = len(current_tokens & candidate_tokens)
        denominator = max(len(current_tokens | candidate_tokens), 1)
        score += overlap / denominator
    return score


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in text.replace("/", " ").replace("-", " ").split() if len(token) > 2}


def enrich_rows(
    rows: list[dict[str, str]],
    agent: GeminiAlternativeRankingAgent,
    max_candidates: int,
    limit_families: int | None = None,
) -> list[dict[str, str]]:
    summaries = summarize_families(rows)
    candidate_map = build_candidate_map(summaries, max_candidates=max_candidates)
    rows_by_url: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        product_url = (row.get("Product URL") or "").strip()
        if product_url:
            rows_by_url[product_url].append(row)

    selection_by_url: dict[str, tuple[str, str]] = {}

    families = summaries if limit_families is None else summaries[:limit_families]
    total = len(families)
    for index, summary in enumerate(families, start=1):
        candidates = candidate_map.get(summary.product_url, [])
        logging.info(
            "Gemini alternative ranking %s/%s for %s with %s candidates",
            index,
            total,
            summary.product_url,
            len(candidates),
        )
        if not candidates:
            selection_by_url[summary.product_url] = ("", "")
            continue
        try:
            alt_url = agent.rank_alternative(summary, candidates)
            alt_item_number = _choose_alternative_item_number(
                current=summary,
                alternative_url=alt_url,
                rows_by_url=rows_by_url,
            )
            selection_by_url[summary.product_url] = (alt_url, alt_item_number)
        except Exception as exc:
            logging.warning(
                "Gemini alternative ranking failed for %s: %s",
                summary.product_url,
                agent.sanitize_error_message(str(exc)),
            )
            selection_by_url[summary.product_url] = ("", "")

    for row in rows:
        product_url = (row.get("Product URL") or "").strip()
        if product_url in selection_by_url:
            alt_url, alt_item_number = selection_by_url[product_url]
            row["Alternative Product URL"] = alt_url
            row["Alternative Item Number"] = alt_item_number
    return rows


def _choose_alternative_item_number(
    current: ProductFamilySummary,
    alternative_url: str,
    rows_by_url: dict[str, list[dict[str, str]]],
) -> str:
    if not alternative_url:
        return ""
    candidate_rows = rows_by_url.get(alternative_url, [])
    if not candidate_rows:
        return ""

    current_unit = (current.unit or "").strip().lower()
    current_attrs = (current.attributes or "").strip().lower()

    for row in candidate_rows:
        item_number = (row.get("Item Number") or "").strip()
        unit = (row.get("Unit") or "").strip().lower()
        attrs = (row.get("Attributes") or "").strip().lower()
        if item_number and current_unit and unit == current_unit:
            return item_number
        if item_number and current_attrs and attrs and attrs == current_attrs:
            return item_number

    for row in candidate_rows:
        item_number = (row.get("Item Number") or "").strip()
        if item_number:
            return item_number
    return ""


def write_rows(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default=str(OUTPUT_DIR / "products.csv"),
        help="Input products CSV.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_DIR / "products.csv"),
        help="Output products CSV with alternative columns filled.",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model name.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=8,
        help="Maximum candidate products to send to Gemini per product family.",
    )
    parser.add_argument(
        "--limit-families",
        type=int,
        default=0,
        help="Only enrich the first N product families. Use 0 for all.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(PROJECT_DIR / ".env")
    rows = load_rows(Path(args.input))
    agent = GeminiAlternativeRankingAgent(model=args.model)
    limit_families = args.limit_families or None
    rows = enrich_rows(
        rows=rows,
        agent=agent,
        max_candidates=args.max_candidates,
        limit_families=limit_families,
    )
    write_rows(rows, Path(args.output))
    logging.info("Wrote alternative-enriched products to %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
