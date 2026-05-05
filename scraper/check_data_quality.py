#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scraper.agents.item_extraction import ItemExtractionAgent  # noqa: E402


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def summarize_empty_values(rows: list[dict[str, str]]) -> list[tuple[str, int, float]]:
    if not rows:
        return []
    total = len(rows)
    results: list[tuple[str, int, float]] = []
    for field in rows[0].keys():
        empty_count = sum(1 for row in rows if not (row.get(field) or "").strip())
        results.append((field, empty_count, empty_count / total))
    return results


def find_unit_gaps(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    agent = ItemExtractionAgent()
    gaps: list[dict[str, str]] = []
    for row in rows:
        if (row.get("Unit") or "").strip():
            continue
        inference_sources = [
            row.get("Attributes", ""),
            row.get("Product Name", ""),
            row.get("Description", ""),
        ]
        suggested = ""
        source_used = ""
        for source in inference_sources:
            suggested = agent.extract_unit_from_text(source or "")
            if suggested:
                source_used = source
                break
        gaps.append(
            {
                "Product URL": row.get("Product URL", ""),
                "Item Number": row.get("Item Number", ""),
                "Product Name": row.get("Product Name", ""),
                "Attributes": row.get("Attributes", ""),
                "Suggested Unit": suggested,
                "Suggested Source": source_used,
            }
        )
    return gaps


def write_unit_gap_report(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "Product URL",
        "Item Number",
        "Product Name",
        "Attributes",
        "Suggested Unit",
        "Suggested Source",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def apply_unit_fixes(rows: list[dict[str, str]]) -> int:
    agent = ItemExtractionAgent()
    updates = 0
    for row in rows:
        if (row.get("Unit") or "").strip():
            continue
        for source_field in ["Attributes", "Product Name", "Description"]:
            suggested = agent.extract_unit_from_text(row.get(source_field, "") or "")
            if suggested:
                row["Unit"] = suggested
                updates += 1
                break
    return updates


def write_rows(rows: list[dict[str, str]], output_path: Path) -> None:
    if not rows:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="output/products.csv",
        help="Path to products.csv",
    )
    parser.add_argument(
        "--unit-gap-output",
        default="output/unit_gap_report.csv",
        help="CSV report for rows with empty Unit values.",
    )
    parser.add_argument(
        "--apply-unit-fixes",
        action="store_true",
        help="Fill inferable Unit values directly back into the input CSV.",
    )
    args = parser.parse_args()

    rows = load_rows(Path(args.input))
    if args.apply_unit_fixes:
        updates = apply_unit_fixes(rows)
        write_rows(rows, Path(args.input))
        print(f"Applied Unit fixes: {updates}")

    print(f"Rows: {len(rows)}")
    print("Field completeness:")
    for field, empty_count, empty_ratio in summarize_empty_values(rows):
        print(f"- {field}: empty={empty_count} ({empty_ratio:.1%})")

    unit_gaps = find_unit_gaps(rows)
    write_unit_gap_report(unit_gaps, Path(args.unit_gap_output))
    print(f"Unit gap rows: {len(unit_gaps)}")
    print(f"Unit gap report: {args.unit_gap_output}")
    inferable = sum(1 for row in unit_gaps if (row.get('Suggested Unit') or '').strip())
    print(f"Inferable empty Unit rows: {inferable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
