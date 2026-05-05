from __future__ import annotations

import re

from .models import IntentDecision

PRODUCT_WORDS = {
    "collect",
    "crawl",
    "extract",
    "find",
    "get",
    "gather",
    "list",
    "scrape",
}
ITEM_WORDS = {
    "category",
    "categories",
    "exam",
    "gloves",
    "item",
    "items",
    "product",
    "products",
    "sku",
    "sutures",
    "surgical",
}
NON_CATEGORY_HINTS = {
    "about",
    "article",
    "articles",
    "blog",
    "career",
    "careers",
    "contact",
    "customer service",
    "docs",
    "documentation",
    "faq",
    "manual",
    "manuals",
    "news",
    "pdf",
    "policy",
    "policies",
    "returns",
    "sds",
    "shipping",
    "support",
    "terms",
}
STOP_WORDS = {
    "a",
    "all",
    "an",
    "and",
    "company",
    "from",
    "information",
    "of",
    "on",
    "related",
    "the",
    "this",
    "website",
}


class IntentClassificationAgent:
    def classify(self, goal: str) -> IntentDecision:
        text = (goal or "").strip().lower()
        tokens = self._tokenize(text)
        keywords = [token for token in tokens if token not in STOP_WORDS]

        if not text:
            return IntentDecision(
                intent_type="unknown",
                is_category_product_request=False,
                confidence=0.0,
                reason="Empty goal.",
                extracted_keywords=[],
            )

        non_category_hits = [hint for hint in NON_CATEGORY_HINTS if hint in text]
        product_hits = [token for token in keywords if token in PRODUCT_WORDS]
        item_hits = [token for token in keywords if token in ITEM_WORDS]

        if non_category_hits and not item_hits:
            return IntentDecision(
                intent_type="non_catalog_site_content",
                is_category_product_request=False,
                confidence=0.9,
                reason=f"Goal matched non-catalog hints: {', '.join(non_category_hits)}.",
                extracted_keywords=keywords,
            )

        score = 0.0
        reasons: list[str] = []
        if product_hits:
            score += 0.35
            reasons.append(f"product-action words: {', '.join(product_hits)}")
        if item_hits:
            score += 0.45
            reasons.append(f"catalog/product words: {', '.join(item_hits)}")
        if "category" in keywords or "products" in keywords or "product" in keywords:
            score += 0.1
            reasons.append("explicit product/category phrasing")
        if non_category_hits:
            score -= 0.25
            reasons.append(f"non-catalog hints also present: {', '.join(non_category_hits)}")

        is_match = score >= 0.45 and bool(item_hits or product_hits)
        intent_type = "category_product_request" if is_match else "unknown_or_non_catalog"
        if not reasons:
            reasons.append("No strong catalog or product signals found.")

        return IntentDecision(
            intent_type=intent_type,
            is_category_product_request=is_match,
            confidence=max(0.0, min(score, 1.0)),
            reason="; ".join(reasons),
            extracted_keywords=keywords,
        )

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())
