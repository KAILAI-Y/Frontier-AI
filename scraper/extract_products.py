#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "output"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scraper.agents.deduplication import DeduplicationAgent  # noqa: E402
from scraper.agents.item_extraction import ItemExtractionAgent  # noqa: E402
from scraper.agents.models import CategoryConfig, CrawlReportRow, VisibleLinkTarget  # noqa: E402
from scraper.agents.page_classification import PageClassificationAgent  # noqa: E402
from scraper.agents.recovery import FallbackRecoveryDecisionAgent  # noqa: E402
CATEGORY_URLS = [
    "https://www.safcodental.com/catalog/gloves",
    "https://www.safcodental.com/catalog/sutures-surgical-products",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    )
}
CSV_FIELDS = [
    "Product Name",
    "Brand",
    "Item Number",
    "Mfr #",
    "Category Hierarchy",
    "Product URL",
    "Qty",
    "Price",
    "Unit",
    "Availability",
    "Description",
    "Attributes",
    "Image URLs",
    "Alternative Product URL",
    "Alternative Item Number",
]
REPORT_FIELDS = [
    "Category URL",
    "Product URL",
    "Page Number",
    "Page Type",
    "Classification Reason",
    "Status",
    "Error Stage",
    "Error Type",
    "Error Message",
    "HTTP Status",
    "Fallback Used",
    "Records Written",
]
CHECKPOINT_FIELDS = [
    "Category URL",
    "Product URL",
    "Page Number",
    "Status",
    "Error Type",
    "Fallback Used",
    "Records Written",
    "Updated At",
]


class SafcoItemLevelScraper:
    def __init__(self, delay_seconds: float = 0.35, timeout: int = 30) -> None:
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.crawl_reports: list[CrawlReportRow] = []
        self.classification_agent = PageClassificationAgent()
        self.dedup_agent = DeduplicationAgent()
        self.recovery_agent = FallbackRecoveryDecisionAgent()
        self.item_extraction_agent = ItemExtractionAgent()

    def fetch_html(self, url: str) -> str:
        logging.info("GET %s", url)
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        time.sleep(self.delay_seconds)
        return response.text

    def load_category_config(self, category_url: str) -> CategoryConfig:
        html_text = self.fetch_html(category_url)
        match = re.search(r"window\.algoliaConfig = (\{.*?\});", html_text, re.DOTALL)
        if not match:
            raise RuntimeError(f"Could not find Algolia config on {category_url}")

        config = json.loads(match.group(1))
        request_cfg = config["request"]

        return CategoryConfig(
            category_url=category_url,
            category_name=request_cfg["path"].split("///")[-1].strip(),
            app_id=config["applicationId"],
            api_key=config["apiKey"],
            index_name=f"{config['indexName']}_products",
            facet_path=request_cfg["path"],
            hits_per_page=int(config.get("hitsPerPage", 15)),
        )

    def fetch_category_hits(
        self, config: CategoryConfig, max_products: int | None = None
    ) -> list[dict[str, Any]]:
        endpoint = (
            f"https://{config.app_id}-dsn.algolia.net/1/indexes/{config.index_name}/query"
        )
        headers = {
            "X-Algolia-Application-Id": config.app_id,
            "X-Algolia-API-Key": config.api_key,
            "Content-Type": "application/json",
        }

        all_hits: list[dict[str, Any]] = []
        page = 0

        while True:
            facet_filters = json.dumps([[f"categories.level1:{config.facet_path}"]])
            params = urlencode(
                {
                    "hitsPerPage": config.hits_per_page,
                    "page": page,
                    "facetFilters": facet_filters,
                }
            )
            logging.info("POST %s page=%s", endpoint, page)
            response = self.session.post(
                endpoint,
                headers=headers,
                json={"params": params},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            hits = payload.get("hits", [])
            all_hits.extend(hits)
            logging.info(
                "Category %s page %s: %s hits", config.category_name, page, len(hits)
            )
            time.sleep(self.delay_seconds)

            if max_products is not None and len(all_hits) >= max_products:
                return all_hits[:max_products]
            if page + 1 >= payload.get("nbPages", 0):
                break
            page += 1

        return all_hits

    def parse_detail_page(self, product_url: str) -> dict[str, Any]:
        html_text = self.fetch_html(product_url)
        soup = BeautifulSoup(html_text, "html.parser")
        product_json, breadcrumb_json = self._extract_jsonld(soup)
        master_data = self._extract_master_data(html_text)
        jsonld_description, jsonld_specifications = self._split_description_and_specs(
            product_json.get("description") or ""
        )
        main_description = self._extract_main_description(soup) or jsonld_description
        inferred_availability = self._infer_page_availability(soup, main_description)
        page_text = self._clean_text(soup.get_text(" ", strip=True))
        page_type, classification_reason = self.classification_agent.classify(
            master_data=master_data, page_text=page_text
        )
        return {
            "product_name": product_json.get("name", ""),
            "brand": self._extract_brand(product_json),
            "category_hierarchy": self._extract_breadcrumbs(breadcrumb_json),
            "description": main_description,
            "specifications": jsonld_specifications,
            "availability": inferred_availability,
            "page_level_price": self._extract_page_level_price(page_text),
            "image_urls": self._extract_images(product_json, soup),
            "alternative_products": "",
            "page_type": page_type,
            "classification_reason": classification_reason,
            "master_data": master_data,
        }

    def build_records_from_detail(
        self, category: CategoryConfig, hit: dict[str, Any], detail: dict[str, Any]
    ) -> list[dict[str, str]]:
        return self.item_extraction_agent.build_records_from_detail(hit, detail)

    def scrape(
        self,
        category_urls: list[str],
        max_products_per_category: int | None = None,
        checkpoint_path: Path | None = None,
        resume: bool = False,
        checkpoint_state: dict[str, dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        checkpoint_state = checkpoint_state or {}

        for category_url in category_urls:
            category = self.load_category_config(category_url)
            hits = self.fetch_category_hits(
                category, max_products=max_products_per_category
            )
            for hit in hits:
                product_url = hit.get("url")
                if not product_url or product_url in seen_urls:
                    continue
                if resume and checkpoint_state.get(product_url, {}).get("Status") == "success":
                    logging.info("Resume mode: skipping already successful URL %s", product_url)
                    continue
                seen_urls.add(product_url)
                record_rows, report = self._process_product_url(
                    category=category,
                    hit=hit,
                    product_url=product_url,
                    page_number="",
                )
                rows.extend(record_rows)
                self.crawl_reports.append(report)
                if checkpoint_path is not None:
                    checkpoint_state[product_url] = checkpoint_entry_from_report(report)
                    write_checkpoint(checkpoint_state, checkpoint_path)

        rows, duplicates_removed = self.dedup_agent.dedupe_product_rows(rows)
        if duplicates_removed:
            logging.info(
                "Deduplication Agent removed %s duplicate product rows from extracted output",
                duplicates_removed,
            )
        return rows

    def scrape_visible_targets(
        self,
        targets: list[VisibleLinkTarget],
        max_products_per_category: int | None = None,
        checkpoint_path: Path | None = None,
        resume: bool = False,
        checkpoint_state: dict[str, dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        counts_by_category: dict[str, int] = {}
        checkpoint_state = checkpoint_state or {}

        for target in targets:
            if target.product_url in seen_urls:
                continue
            if resume and checkpoint_state.get(target.product_url, {}).get("Status") == "success":
                logging.info(
                    "Resume mode: skipping already successful URL %s",
                    target.product_url,
                )
                continue

            category_count = counts_by_category.get(target.category_url, 0)
            if (
                max_products_per_category is not None
                and category_count >= max_products_per_category
            ):
                continue

            seen_urls.add(target.product_url)
            counts_by_category[target.category_url] = category_count + 1
            category = self._category_config_from_visible_link(target.category_url)
            hit = self._build_visible_link_hit(target)
            record_rows, report = self._process_product_url(
                category=category,
                hit=hit,
                product_url=target.product_url,
                page_number=target.page_number,
            )
            rows.extend(record_rows)
            self.crawl_reports.append(report)
            if checkpoint_path is not None:
                checkpoint_state[target.product_url] = checkpoint_entry_from_report(report)
                write_checkpoint(checkpoint_state, checkpoint_path)

        rows, duplicates_removed = self.dedup_agent.dedupe_product_rows(rows)
        if duplicates_removed:
            logging.info(
                "Deduplication Agent removed %s duplicate product rows from extracted output",
                duplicates_removed,
            )
        return rows

    def _process_product_url(
        self,
        category: CategoryConfig,
        hit: dict[str, Any],
        product_url: str,
        page_number: str,
    ) -> tuple[list[dict[str, str]], CrawlReportRow]:
        detail: dict[str, Any]
        status = "success"
        page_type = ""
        classification_reason = ""
        error_stage = ""
        error_type = ""
        error_message = ""
        http_status = ""
        fallback_used = "no"

        try:
            detail = self.parse_detail_page(product_url)
        except requests.HTTPError as exc:
            http_status = str(exc.response.status_code) if exc.response is not None else ""
            status = "partial_fallback"
            error_stage = "detail_fetch"
            error_type = "http_error"
            error_message = str(exc)
            fallback_used = "yes"
            logging.warning(
                "Detail page %s returned %s; using listing fallback",
                product_url,
                http_status or "?",
            )
            detail = self._build_listing_fallback_detail(hit)
        except Exception as exc:  # pragma: no cover - operational fallback
            status = "partial_fallback"
            error_stage = "detail_parse"
            error_type = exc.__class__.__name__
            error_message = str(exc)
            fallback_used = "yes"
            logging.warning(
                "Failed to parse %s (%s); using listing fallback",
                product_url,
                exc,
            )
            detail = self._build_listing_fallback_detail(hit)

        page_type = detail.get("page_type", "") or page_type
        classification_reason = (
            detail.get("classification_reason", "") or classification_reason
        )

        if not page_type:
            page_type = "item_table_page" if detail.get("master_data") else "unknown_page"
        if not classification_reason:
            classification_reason = "masterData found" if detail.get("master_data") else ""

        decision = self.recovery_agent.decide(
            detail=detail,
            current_status=status,
            error_stage=error_stage,
            error_type=error_type,
            error_message=error_message,
            fallback_used=fallback_used,
        )
        status = decision.status
        error_stage = decision.error_stage
        error_type = decision.error_type
        error_message = decision.error_message
        fallback_used = decision.fallback_used

        rows = self.build_records_from_detail(category, hit, detail)
        if not rows:
            status = "failed"
            error_stage = error_stage or "record_build"
            error_type = error_type or "no_records"
            error_message = error_message or "No output rows were produced."

        report = CrawlReportRow(
            category_url=category.category_url,
            product_url=product_url,
            page_number=page_number,
            page_type=page_type,
            classification_reason=classification_reason,
            status=status,
            error_stage=error_stage,
            error_type=error_type,
            error_message=error_message,
            http_status=http_status,
            fallback_used=fallback_used,
            records_written=len(rows),
        )
        return rows, report

    def _build_item_base_fields(
        self,
        category: CategoryConfig,
        hit: dict[str, Any],
        detail: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, str]:
        category_hierarchy = detail.get("category_hierarchy") or self._coerce_categories(
            hit.get("categories")
        )
        description = detail.get("description", "")
        item_details = self._clean_text(item.get("short_description"))
        image_urls = [
            image
            for image in [item.get("main_image"), item.get("image"), *detail.get("image_urls", [])]
            if image and not self._is_placeholder_image(image)
        ]
        image_urls = self._dedupe_images(image_urls)

        return {
            "Product Name": self._clean_text(item.get("name"))
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
            "Unit": self._extract_unit(item),
            "Availability": item.get("stock_availability_label")
            or self._normalize_availability(item.get("stock_availability"))
            or "",
            "Description": description,
            "Attributes": self._clean_text(item.get("description")) or item_details,
            "Image URLs": "|".join(image_urls),
            "Alternative Products": detail.get("alternative_products", ""),
        }

    def _build_listing_fallback_detail(self, hit: dict[str, Any]) -> dict[str, Any]:
        return {
            "product_name": hit.get("name", ""),
            "brand": hit.get("manufacturer_name", ""),
            "category_hierarchy": self._coerce_categories(hit.get("categories")),
            "description": "",
            "specifications": "",
            "availability": self._normalize_availability(hit.get("stock_availability")),
            "image_urls": [hit.get("image_url") or hit.get("thumbnail_url") or ""],
            "alternative_products": "",
            "page_type": "listing_fallback_page",
            "classification_reason": "Detail page could not be parsed; listing-level fallback used.",
            "master_data": {},
        }

    @staticmethod
    def _category_config_from_visible_link(category_url: str) -> CategoryConfig:
        slug = category_url.rstrip("/").split("/")[-1]
        category_name = slug.replace("-", " ").strip().title()
        return CategoryConfig(
            category_url=category_url,
            category_name=category_name,
            app_id="",
            api_key="",
            index_name="",
            facet_path="",
            hits_per_page=0,
        )

    def _build_visible_link_hit(self, target: VisibleLinkTarget) -> dict[str, Any]:
        return {
            "url": target.product_url,
            "name": self._clean_anchor_text(target.anchor_text),
            "manufacturer_name": "",
            "categories": self._category_segments_from_url(target.category_url),
            "sku": [],
            "stock_availability": "",
            "image_url": "",
            "thumbnail_url": "",
            "price": {"USD": {"default": ""}},
        }

    def _build_listing_fallback_record(
        self, category: CategoryConfig, hit: dict[str, Any], detail: dict[str, Any]
    ) -> dict[str, str]:
        item_numbers = hit.get("sku") or []
        if isinstance(item_numbers, str):
            item_numbers = [item_numbers]
        category_hierarchy = detail.get("category_hierarchy") or self._coerce_categories(
            hit.get("categories")
        )
        availability = detail.get("availability") or self._normalize_availability(
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
            "Price": str(detail.get("page_level_price") or self._extract_listing_price(hit) or ""),
            "Unit": "",
            "Availability": availability,
            "Description": detail.get("description", ""),
            "Attributes": "",
            "Image URLs": "|".join(
                image for image in detail.get("image_urls", []) if image
            ),
            "Alternative Products": detail.get("alternative_products", ""),
        }

    @staticmethod
    def _extract_jsonld(soup: BeautifulSoup) -> tuple[dict[str, Any], dict[str, Any]]:
        product_json: dict[str, Any] = {}
        breadcrumb_json: dict[str, Any] = {}
        for script_tag in soup.select('script[type="application/ld+json"]'):
            raw = script_tag.get_text(strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("@type") == "Product":
                product_json = parsed
            if isinstance(parsed, dict) and parsed.get("@type") == "BreadcrumbList":
                breadcrumb_json = parsed
        return product_json, breadcrumb_json

    @staticmethod
    def _extract_master_data(html_text: str) -> dict[str, Any]:
        match = re.search(r'window\.masterData = "(.*?)";', html_text)
        if not match:
            return {}
        raw = match.group(1)
        decoded = json.loads(f'"{raw}"')
        return json.loads(decoded)

    @staticmethod
    def _extract_brand(product_json: dict[str, Any]) -> str:
        brand_obj = product_json.get("brand")
        if isinstance(brand_obj, dict):
            return brand_obj.get("name", "")
        if isinstance(brand_obj, str):
            return brand_obj
        return ""

    @staticmethod
    def _extract_breadcrumbs(breadcrumb_json: dict[str, Any]) -> list[str]:
        breadcrumbs: list[str] = []
        for item in breadcrumb_json.get("itemListElement", []):
            name = item.get("name")
            if name:
                breadcrumbs.append(name)
        return breadcrumbs

    @staticmethod
    def _extract_images(product_json: dict[str, Any], soup: BeautifulSoup) -> list[str]:
        images = product_json.get("image") or []
        if isinstance(images, str):
            images = [images]
        og_image = soup.select_one('meta[property="og:image"]')
        if og_image and og_image.get("content"):
            images.append(og_image["content"])
        return SafcoItemLevelScraper._dedupe_images(
            [
                image
                for image in images
                if image and not SafcoItemLevelScraper._is_placeholder_image(image)
            ]
        )

    @staticmethod
    def _extract_main_description(soup: BeautifulSoup) -> str:
        node = soup.select_one(".product-description")
        if not node:
            return ""
        return SafcoItemLevelScraper._clean_text(node.get_text(" ", strip=True))

    @staticmethod
    def _classify_page(master_data: dict[str, Any], page_text: str) -> tuple[str, str]:
        normalized_text = (page_text or "").lower()
        if master_data:
            has_groups = any(str(item.get("itemgroup") or "").strip() for item in master_data.values())
            if has_groups:
                return "multi_group_item_page", "masterData found with grouped item rows"
            return "item_table_page", "masterData found"
        if "no options of this product are available" in normalized_text:
            return "no_item_options_page", "Page states that no product options are available"
        if "404" in normalized_text and "page not found" in normalized_text:
            return "broken_page", "Page contains 404 / page not found text"
        return "unsupported_layout", "No item-level masterData found"

    @staticmethod
    def _infer_page_availability(soup: BeautifulSoup, description: str) -> str:
        page_text = SafcoItemLevelScraper._clean_text(soup.get_text(" ", strip=True))
        combined = f"{description} {page_text}".lower()
        checks = [
            ("backorder", "Backorder"),
            ("indefinite delivery", "Backorder"),
            ("special order", "Special order"),
            ("direct from manufacturer", "Direct from manufacturer"),
            ("discontinued", "Discontinued"),
            ("in stock", "In stock"),
        ]
        for needle, label in checks:
            if needle in combined:
                return label
        return ""

    @staticmethod
    def _split_description_and_specs(raw_description: str) -> tuple[str, str]:
        text = html.unescape(raw_description or "")
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        if not lines:
            return "", ""
        return lines[0], " | ".join(lines[1:]) if len(lines) > 1 else ""

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value in (None, False):
            return ""
        text = html.unescape(str(value))
        if "<" in text and ">" in text:
            text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_anchor_text(text: str) -> str:
        cleaned = SafcoItemLevelScraper._clean_text(text)
        if not cleaned:
            return ""
        cleaned = re.sub(r"^>\s*", "", cleaned)
        cleaned = re.sub(
            r"\s+(?:Promo|Price Drop|New!|shopping-cart|Shop Now|scale)\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+\d+(?:\.\d+)?\s+As low as \$.*$", "", cleaned)
        cleaned = re.sub(r"\s+As low as \$.*$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _category_segments_from_url(category_url: str) -> list[str]:
        slug = category_url.rstrip("/").split("/")[-1]
        return [part.strip().title() for part in slug.split("-") if part.strip()]

    @staticmethod
    def _coerce_categories(categories: Any) -> list[str]:
        if not categories:
            return []
        if isinstance(categories, list):
            first = categories[0]
            if isinstance(first, str):
                return [part.strip() for part in first.split("///") if part.strip()]
        if isinstance(categories, str):
            return [part.strip() for part in categories.split("///") if part.strip()]
        return []

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> tuple[str, int]:
        group = str(item.get("itemgroup") or "")
        position = int(item.get("position") or 0)
        return group, position

    @staticmethod
    def _extract_listing_price(hit: dict[str, Any]) -> Any:
        return hit.get("price", {}).get("USD", {}).get("default")

    @staticmethod
    def _normalize_availability(value: Any) -> str:
        mapping = {
            "in_stock": "In stock",
            "backorder": "Backorder",
            "special_order": "Special order",
            "direct_from_manufacturer": "Direct from manufacturer",
            "discontinued": "Discontinued",
        }
        return mapping.get(str(value or "").lower(), "")

    @staticmethod
    def _extract_page_level_price(page_text: str) -> str:
        text = page_text or ""
        patterns = [
            r"\bfrom\s*\$\s*([0-9][0-9,]*\.?[0-9]*)",
            r"\bas low as\s*\$\s*([0-9][0-9,]*\.?[0-9]*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).replace(",", "")
        return ""

    def _extract_unit(self, item: dict[str, Any]) -> str:
        for candidate in [item.get("description"), item.get("name")]:
            unit = self._extract_unit_from_text(self._clean_text(candidate))
            if unit:
                return unit
        return ""

    @staticmethod
    def _extract_unit_from_text(text: str) -> str:
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
    def _is_placeholder_image(url: str) -> bool:
        lowered = str(url).lower()
        return "placeholder" in lowered or "white-placeholder" in lowered

    @staticmethod
    def _dedupe_images(images: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for image in images:
            normalized = SafcoItemLevelScraper._normalize_image_url(image)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(image)
        return output

    @staticmethod
    def _normalize_image_url(url: str) -> str:
        try:
            parts = urlsplit(str(url))
        except Exception:
            return str(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def _extract_price_tiers(self, item: dict[str, Any]) -> list[dict[str, str]]:
        tiers: list[dict[str, str]] = []
        base_price = self._extract_numeric_price(item)
        if base_price:
            tiers.append({"qty": "1", "price": base_price})

        structured_tiers = item.get("tier_price")
        if isinstance(structured_tiers, dict) and structured_tiers:
            for tier in structured_tiers.values():
                qty = self._extract_first_number(str(tier.get("price_qty", "")))
                price = self._extract_first_price(str(tier.get("price", "")))
                if qty and price:
                    tiers.append({"qty": qty, "price": price})
            return self._dedupe_tiers(tiers)

        raw_tier_html = item.get("price", {}).get("tier_price")
        if not raw_tier_html or raw_tier_html is False:
            return tiers

        fragment = BeautifulSoup(str(raw_tier_html), "html.parser")

        for row in fragment.select("tr"):
            cells = [self._clean_text(td.get_text(" ", strip=True)) for td in row.select("td,th")]
            if len(cells) >= 2:
                qty = self._extract_first_number(cells[0])
                price = self._extract_first_price(cells[1])
                if qty and price:
                    tiers.append({"qty": qty, "price": price})

        if tiers and len(tiers) > 1:
            return self._dedupe_tiers(tiers)

        text_lines = [
            self._clean_text(line)
            for line in fragment.get_text("\n", strip=True).splitlines()
            if self._clean_text(line)
        ]
        pending_qty = ""
        for line in text_lines:
            qty = self._extract_first_number(line)
            price = self._extract_first_price(line)
            if qty and price:
                tiers.append({"qty": qty, "price": price})
                pending_qty = ""
            elif qty:
                pending_qty = qty
            elif price and pending_qty:
                tiers.append({"qty": pending_qty, "price": price})
                pending_qty = ""

        return self._dedupe_tiers(tiers)

    @staticmethod
    def _dedupe_tiers(tiers: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        output: list[dict[str, str]] = []
        for tier in tiers:
            key = (str(tier.get("qty", "")), str(tier.get("price", "")))
            if key in seen:
                continue
            seen.add(key)
            output.append({"qty": key[0], "price": key[1]})
        return output

    @staticmethod
    def _extract_numeric_price(item: dict[str, Any]) -> str:
        price_html = item.get("price", {}).get("price", "")
        if price_html:
            amount_match = re.search(r'data-price-amount="([^"]+)"', str(price_html))
            if amount_match:
                return amount_match.group(1)
            text = BeautifulSoup(str(price_html), "html.parser").get_text(" ", strip=True)
            price = SafcoItemLevelScraper._extract_first_price(text)
            if price:
                return price
        raw = item.get("product_price")
        return str(raw) if raw not in (None, "") else ""

    @staticmethod
    def _extract_first_number(text: str) -> str:
        match = re.search(r"\b\d+\b", text)
        return match.group(0) if match else ""

    @staticmethod
    def _extract_first_price(text: str) -> str:
        match = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{2})?)", text)
        return match.group(1) if match else ""


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_crawl_report(rows: list[CrawlReportRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "Category URL": row.category_url,
                    "Product URL": row.product_url,
                    "Page Number": row.page_number,
                    "Page Type": row.page_type,
                    "Classification Reason": row.classification_reason,
                    "Status": row.status,
                    "Error Stage": row.error_stage,
                    "Error Type": row.error_type,
                    "Error Message": row.error_message,
                    "HTTP Status": row.http_status,
                    "Fallback Used": row.fallback_used,
                    "Records Written": row.records_written,
                }
            )


def load_visible_link_targets(input_path: Path) -> list[VisibleLinkTarget]:
    targets: list[VisibleLinkTarget] = []
    with input_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            product_url = (row.get("product_url") or "").strip()
            category_url = (row.get("category_url") or "").strip()
            if not product_url or not category_url:
                continue
            targets.append(
                VisibleLinkTarget(
                    category_url=category_url,
                    page_number=(row.get("page_number") or "").strip(),
                    product_url=product_url,
                    anchor_text=(row.get("anchor_text") or "").strip(),
                )
            )
    return targets


def load_checkpoint(checkpoint_path: Path) -> dict[str, dict[str, str]]:
    if not checkpoint_path.exists():
        return {}
    state: dict[str, dict[str, str]] = {}
    with checkpoint_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            product_url = (row.get("Product URL") or "").strip()
            if not product_url:
                continue
            state[product_url] = dict(row)
    return state


def checkpoint_entry_from_report(report: CrawlReportRow) -> dict[str, str]:
    return {
        "Category URL": report.category_url,
        "Product URL": report.product_url,
        "Page Number": report.page_number,
        "Status": report.status,
        "Error Type": report.error_type,
        "Fallback Used": report.fallback_used,
        "Records Written": str(report.records_written),
        "Updated At": str(int(time.time())),
    }


def write_checkpoint(state: dict[str, dict[str, str]], checkpoint_path: Path) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHECKPOINT_FIELDS)
        writer.writeheader()
        for product_url in sorted(state.keys()):
            writer.writerow(state[product_url])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-products-per-category",
        type=int,
        default=5,
        help="Limit products fetched per category. Use 0 for no limit.",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_DIR / "products.csv"),
        help="CSV output path.",
    )
    parser.add_argument(
        "--input-links-csv",
        default="",
        help="Optional visible-links CSV from scraper/discover_links.py.",
    )
    parser.add_argument(
        "--report",
        default=str(OUTPUT_DIR / "crawl_report.csv"),
        help="Crawl status report output path.",
    )
    parser.add_argument(
        "--checkpoint",
        default=str(OUTPUT_DIR / "checkpoint.csv"),
        help="Checkpoint CSV path.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint and skip URLs already marked success.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scraper = SafcoItemLevelScraper()
    max_products = args.max_products_per_category or None
    checkpoint_path = Path(args.checkpoint)
    checkpoint_state = load_checkpoint(checkpoint_path) if args.resume else {}
    if args.input_links_csv:
        targets = load_visible_link_targets(Path(args.input_links_csv))
        logging.info("Loaded %s visible product links from %s", len(targets), args.input_links_csv)
        rows = scraper.scrape_visible_targets(
            targets,
            max_products_per_category=max_products,
            checkpoint_path=checkpoint_path,
            resume=args.resume,
            checkpoint_state=checkpoint_state,
        )
    else:
        rows = scraper.scrape(
            CATEGORY_URLS,
            max_products_per_category=max_products,
            checkpoint_path=checkpoint_path,
            resume=args.resume,
            checkpoint_state=checkpoint_state,
        )
    output_path = Path(args.output)
    write_csv(rows, output_path)
    report_path = Path(args.report)
    write_crawl_report(scraper.crawl_reports, report_path)
    logging.info("Wrote %s rows to %s", len(rows), output_path)
    logging.info("Wrote %s crawl report rows to %s", len(scraper.crawl_reports), report_path)
    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
