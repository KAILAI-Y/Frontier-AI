from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class IrregularExtractionResult:
    page_type: str
    found_more_info: bool
    item_count: int
    item_numbers: list[str]
    notes: str
    raw_items_json: str


class GeminiIrregularExtractionAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        timeout: int = 60,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()

    def extract_from_html(
        self,
        *,
        product_url: str,
        product_name: str,
        category_hierarchy: str,
        html_snippet: str,
        text_snippet: str,
    ) -> IrregularExtractionResult:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")

        prompt = self._build_prompt(
            product_url=product_url,
            product_name=product_name,
            category_hierarchy=category_hierarchy,
            html_snippet=html_snippet,
            text_snippet=text_snippet,
        )
        response_json = self._call_gemini(prompt)
        items = response_json.get("items") or []
        if not isinstance(items, list):
            items = []
        item_numbers: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_number = str(item.get("item_number", "")).strip()
            if item_number:
                item_numbers.append(item_number)
        return IrregularExtractionResult(
            page_type=str(response_json.get("page_type", "")).strip(),
            found_more_info=bool(response_json.get("found_more_info", False)),
            item_count=len(items),
            item_numbers=item_numbers[:10],
            notes=str(response_json.get("notes", "")).strip(),
            raw_items_json=json.dumps(items, ensure_ascii=False),
        )

    def _call_gemini(self, prompt: str) -> dict[str, Any]:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        response = self.session.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logging.warning("Gemini returned non-JSON text: %s", text)
            raise RuntimeError("Gemini returned invalid JSON.") from exc

    @staticmethod
    def sanitize_error_message(message: str) -> str:
        return re.sub(r"([?&]key=)[^&\\s]+", r"\\1REDACTED", message)

    @staticmethod
    def _build_prompt(
        *,
        product_url: str,
        product_name: str,
        category_hierarchy: str,
        html_snippet: str,
        text_snippet: str,
    ) -> str:
        return "\n\n".join(
            [
                "You are inspecting a product page that the deterministic extractor marked as unsupported_layout.",
                "Decide whether the page contains additional item-level information beyond the page-level fallback record.",
                "Only extract information that is explicitly visible in the provided HTML/text.",
                "Do not invent missing fields.",
                (
                    'Return strict JSON with this shape: '
                    '{"page_type":"...", "found_more_info": true/false, "notes":"...", '
                    '"items":[{"item_number":"","product_name":"","mfr_number":"",'
                    '"attributes":"","availability":"","price":"","qty_tiers":""}]}'
                ),
                "\n".join(
                    [
                        f"Product URL: {product_url}",
                        f"Product Name: {product_name}",
                        f"Category: {category_hierarchy}",
                    ]
                ),
                f"Visible text snippet:\n{text_snippet}",
                f"HTML snippet:\n{html_snippet}",
            ]
        )
