from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CategoryConfig:
    category_url: str
    category_name: str
    app_id: str
    api_key: str
    index_name: str
    facet_path: str
    hits_per_page: int


@dataclass
class ProductLinkRecord:
    category_url: str
    page_number: int
    product_url: str
    anchor_text: str


@dataclass
class VisibleLinkTarget:
    category_url: str
    page_number: str
    product_url: str
    anchor_text: str


@dataclass
class CrawlReportRow:
    category_url: str
    product_url: str
    page_number: str
    page_type: str
    classification_reason: str
    status: str
    error_stage: str
    error_type: str
    error_message: str
    http_status: str
    fallback_used: str
    records_written: int


@dataclass
class RecoveryDecision:
    status: str
    error_stage: str
    error_type: str
    error_message: str
    fallback_used: str
