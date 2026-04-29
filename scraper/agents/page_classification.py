from __future__ import annotations

from typing import Any


class PageClassificationAgent:
    def classify(self, master_data: dict[str, Any], page_text: str) -> tuple[str, str]:
        normalized_text = (page_text or "").lower()
        if master_data:
            has_groups = any(
                str(item.get("itemgroup") or "").strip() for item in master_data.values()
            )
            if has_groups:
                return "multi_group_item_page", "masterData found with grouped item rows"
            return "item_table_page", "masterData found"
        if "no options of this product are available" in normalized_text:
            return "no_item_options_page", "Page states that no product options are available"
        if "404" in normalized_text and "page not found" in normalized_text:
            return "broken_page", "Page contains 404 / page not found text"
        return "unsupported_layout", "No item-level masterData found"
