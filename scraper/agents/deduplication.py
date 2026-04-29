from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


class DeduplicationAgent:
    def dedupe_product_urls(self, urls: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for url in urls:
            normalized = str(url).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(normalized)
        return output

    def dedupe_images(self, images: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for image in images:
            normalized = self.normalize_image_url(image)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(image)
        return output

    def dedupe_tiers(self, tiers: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        output: list[dict[str, str]] = []
        for tier in tiers:
            key = (str(tier.get("qty", "")), str(tier.get("price", "")))
            if key in seen:
                continue
            seen.add(key)
            output.append({"qty": key[0], "price": key[1]})
        return output

    def dedupe_product_rows(
        self, rows: list[dict[str, str]]
    ) -> tuple[list[dict[str, str]], int]:
        seen: set[tuple[str, str, str, str]] = set()
        output: list[dict[str, str]] = []
        duplicates_removed = 0

        for row in rows:
            key = (
                str(row.get("Product URL", "")).strip(),
                str(row.get("Item Number", "")).strip(),
                str(row.get("Qty", "")).strip(),
                str(row.get("Price", "")).strip(),
            )
            if key in seen:
                duplicates_removed += 1
                continue
            seen.add(key)
            output.append(row)

        return output, duplicates_removed

    @staticmethod
    def normalize_image_url(url: str) -> str:
        try:
            parts = urlsplit(str(url))
        except Exception:
            return str(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
