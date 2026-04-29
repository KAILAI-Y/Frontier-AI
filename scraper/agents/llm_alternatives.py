from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class ProductFamilySummary:
    product_url: str
    product_name: str
    brand: str
    category_hierarchy: str
    description: str
    attributes: str
    unit: str


class GeminiAlternativeRankingAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        timeout: int = 45,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()

    def rank_alternative(
        self,
        current: ProductFamilySummary,
        candidates: list[ProductFamilySummary],
    ) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        if not candidates:
            return ""

        prompt = self._build_prompt(current=current, candidates=candidates)
        response_json = self._call_gemini(prompt)
        candidate_url = str(response_json.get("alternative_product_url", "")).strip()
        valid_urls = {candidate.product_url for candidate in candidates}
        if candidate_url in valid_urls:
            return candidate_url
        return ""

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
        current: ProductFamilySummary,
        candidates: list[ProductFamilySummary],
    ) -> str:
        candidate_lines = []
        for index, candidate in enumerate(candidates, start=1):
            candidate_lines.append(
                "\n".join(
                    [
                        f"Candidate {index}",
                        f"URL: {candidate.product_url}",
                        f"Name: {candidate.product_name}",
                        f"Brand: {candidate.brand}",
                        f"Category: {candidate.category_hierarchy}",
                        f"Description: {candidate.description}",
                        f"Attributes: {candidate.attributes}",
                        f"Unit: {candidate.unit}",
                    ]
                )
            )

        return "\n\n".join(
            [
                "Choose at most one true substitute product for the current product.",
                "A true substitute should match the same product use case closely.",
                "Do not choose a trending, recommended, or complementary product.",
                "If none of the candidates is a real substitute, return an empty string.",
                'Return strict JSON only: {"alternative_product_url": "...", "reason": "..."}',
                "\n".join(
                    [
                        "Current product",
                        f"URL: {current.product_url}",
                        f"Name: {current.product_name}",
                        f"Brand: {current.brand}",
                        f"Category: {current.category_hierarchy}",
                        f"Description: {current.description}",
                        f"Attributes: {current.attributes}",
                        f"Unit: {current.unit}",
                    ]
                ),
                "\n\n".join(candidate_lines),
            ]
        )
