from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .deduplication import DeduplicationAgent


class PriceTierExpansionAgent:
    def __init__(self) -> None:
        self.dedup_agent = DeduplicationAgent()

    def extract_price_tiers(self, item: dict[str, Any]) -> list[dict[str, str]]:
        tiers: list[dict[str, str]] = []
        base_price = self.extract_numeric_price(item)
        if base_price:
            tiers.append({"qty": "1", "price": base_price})

        structured_tiers = item.get("tier_price")
        if isinstance(structured_tiers, dict) and structured_tiers:
            for tier in structured_tiers.values():
                qty = self.extract_first_number(str(tier.get("price_qty", "")))
                price = self.extract_first_price(str(tier.get("price", "")))
                if qty and price:
                    tiers.append({"qty": qty, "price": price})
            return self.dedup_agent.dedupe_tiers(tiers)

        raw_tier_html = item.get("price", {}).get("tier_price")
        if not raw_tier_html or raw_tier_html is False:
            return tiers

        fragment = BeautifulSoup(str(raw_tier_html), "html.parser")

        for row in fragment.select("tr"):
            cells = [self.clean_text(td.get_text(" ", strip=True)) for td in row.select("td,th")]
            if len(cells) >= 2:
                qty = self.extract_first_number(cells[0])
                price = self.extract_first_price(cells[1])
                if qty and price:
                    tiers.append({"qty": qty, "price": price})

        if tiers and len(tiers) > 1:
            return self.dedup_agent.dedupe_tiers(tiers)

        text_lines = [
            self.clean_text(line)
            for line in fragment.get_text("\n", strip=True).splitlines()
            if self.clean_text(line)
        ]
        pending_qty = ""
        for line in text_lines:
            qty = self.extract_first_number(line)
            price = self.extract_first_price(line)
            if qty and price:
                tiers.append({"qty": qty, "price": price})
                pending_qty = ""
            elif qty:
                pending_qty = qty
            elif price and pending_qty:
                tiers.append({"qty": pending_qty, "price": price})
                pending_qty = ""

        return self.dedup_agent.dedupe_tiers(tiers)

    @staticmethod
    def extract_numeric_price(item: dict[str, Any]) -> str:
        price_html = item.get("price", {}).get("price", "")
        if price_html:
            amount_match = re.search(r'data-price-amount="([^"]+)"', str(price_html))
            if amount_match:
                return amount_match.group(1)
            text = BeautifulSoup(str(price_html), "html.parser").get_text(" ", strip=True)
            price = PriceTierExpansionAgent.extract_first_price(text)
            if price:
                return price
        raw = item.get("product_price")
        return str(raw) if raw not in (None, "") else ""

    @staticmethod
    def extract_first_number(text: str) -> str:
        match = re.search(r"\b\d+\b", text)
        return match.group(0) if match else ""

    @staticmethod
    def extract_first_price(text: str) -> str:
        match = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{2})?)", text)
        return match.group(1) if match else ""

    @staticmethod
    def clean_text(value: Any) -> str:
        if value in (None, False):
            return ""
        text = html.unescape(str(value))
        if "<" in text and ">" in text:
            text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()


class ItemExtractionAgent:
    def __init__(self) -> None:
        self.price_agent = PriceTierExpansionAgent()
        self.dedup_agent = DeduplicationAgent()

    def build_records_from_detail(
        self, hit: dict[str, Any], detail: dict[str, Any]
    ) -> list[dict[str, str]]:
        master_data = detail.get("master_data") or {}
        if not master_data:
            return [self.build_listing_fallback_record(hit, detail)]

        records: list[dict[str, str]] = []
        for _, item in sorted(master_data.items(), key=lambda pair: self.sort_key(pair[1])):
            base_fields = self.build_item_base_fields(hit, detail, item)
            price_tiers = self.price_agent.extract_price_tiers(item)
            if not price_tiers:
                price_tiers = [{"qty": "", "price": self.price_agent.extract_numeric_price(item)}]
            for tier in price_tiers:
                record = dict(base_fields)
                record["Qty"] = str(tier.get("qty", ""))
                record["Price"] = str(tier.get("price", ""))
                records.append(record)
        return records

    def build_item_base_fields(
        self, hit: dict[str, Any], detail: dict[str, Any], item: dict[str, Any]
    ) -> dict[str, str]:
        category_hierarchy = detail.get("category_hierarchy") or self.coerce_categories(
            hit.get("categories")
        )
        description = detail.get("description", "")
        item_details = self.clean_text(item.get("short_description"))
        image_urls = [
            image
            for image in [item.get("main_image"), item.get("image"), *detail.get("image_urls", [])]
            if image and not self.is_placeholder_image(image)
        ]
        image_urls = self.dedup_agent.dedupe_images(image_urls)

        return {
            "Product Name": self.clean_text(item.get("name"))
            or detail.get("product_name")
            or hit.get("name", ""),
            "Brand": item.get("manufacturer_name")
            or detail.get("brand")
            or hit.get("manufacturer_name", ""),
            "Item Number": item.get("sku") or item.get("sku_requisition") or "",
            "Mfr #": item.get("manufacturer_part_number", ""),
            "Category Hierarchy": " > ".join(category_hierarchy),
            "Product URL": hit.get("url", ""),
            "Qty": "",
            "Price": "",
            "Unit": self.extract_unit(item),
            "Availability": item.get("stock_availability_label")
            or self.normalize_availability(item.get("stock_availability"))
            or "",
            "Description": description,
            "Attributes": self.clean_text(item.get("description")) or item_details,
            "Image URLs": "|".join(image_urls),
            "Alternative Product URL": detail.get("alternative_product_url", ""),
            "Alternative Item Number": detail.get("alternative_item_number", ""),
        }

    def build_listing_fallback_record(
        self, hit: dict[str, Any], detail: dict[str, Any]
    ) -> dict[str, str]:
        item_numbers = hit.get("sku") or []
        if isinstance(item_numbers, str):
            item_numbers = [item_numbers]
        category_hierarchy = detail.get("category_hierarchy") or self.coerce_categories(
            hit.get("categories")
        )
        availability = detail.get("availability") or self.normalize_availability(
            hit.get("stock_availability")
        ) or str(hit.get("stock_availability", ""))
        if detail.get("page_type") == "no_item_options_page":
            availability = "Unavailable"
        return {
            "Product Name": detail.get("product_name") or hit.get("name", ""),
            "Brand": detail.get("brand") or hit.get("manufacturer_name", ""),
            "Item Number": "|".join(item_numbers),
            "Mfr #": "",
            "Category Hierarchy": " > ".join(category_hierarchy),
            "Product URL": hit.get("url", ""),
            "Qty": "",
            "Price": str(detail.get("page_level_price") or self.extract_listing_price(hit) or ""),
            "Unit": "",
            "Availability": availability,
            "Description": detail.get("description", ""),
            "Attributes": "",
            "Image URLs": "|".join(
                image for image in detail.get("image_urls", []) if image
            ),
            "Alternative Product URL": detail.get("alternative_product_url", ""),
            "Alternative Item Number": detail.get("alternative_item_number", ""),
        }

    @staticmethod
    def clean_text(value: Any) -> str:
        return PriceTierExpansionAgent.clean_text(value)

    @staticmethod
    def sort_key(item: dict[str, Any]) -> tuple[str, int]:
        group = str(item.get("itemgroup") or "")
        position = int(item.get("position") or 0)
        return group, position

    @staticmethod
    def extract_listing_price(hit: dict[str, Any]) -> Any:
        return hit.get("price", {}).get("USD", {}).get("default")

    @staticmethod
    def normalize_availability(value: Any) -> str:
        mapping = {
            "in_stock": "In stock",
            "backorder": "Backorder",
            "special_order": "Special order",
            "direct_from_manufacturer": "Direct from manufacturer",
            "discontinued": "Discontinued",
        }
        return mapping.get(str(value or "").lower(), "")

    def extract_unit(self, item: dict[str, Any]) -> str:
        for candidate in [item.get("description"), item.get("name")]:
            unit = self.extract_unit_from_text(self.clean_text(candidate))
            if unit:
                return unit
        return ""

    @staticmethod
    def extract_unit_from_text(text: str) -> str:
        patterns = [
            r"\b\d+(?:\.\d+)?\s*oz\s+bottle\b",
            r"\b\d+\s*-\s*pack\b",
            r"\b\d+\s*pack\b",
            r"\b\d+\s*/\s*box\b",
            r"\b\d+\s+gloves\s+per\s+box\b",
            r"\b\d+\s*/\s*case\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(0)
        return ""

    @staticmethod
    def is_placeholder_image(url: str) -> bool:
        lowered = str(url).lower()
        return "placeholder" in lowered or "white-placeholder" in lowered

    @staticmethod
    def coerce_categories(categories: Any) -> list[str]:
        if not categories:
            return []
        if isinstance(categories, list):
            first = categories[0]
            if isinstance(first, str):
                return [part.strip() for part in first.split("///") if part.strip()]
        if isinstance(categories, str):
            return [part.strip() for part in categories.split("///") if part.strip()]
        return []
