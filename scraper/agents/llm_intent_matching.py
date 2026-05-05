from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

from .models import CategoryCandidate


class GeminiIntentMatchingAgent:
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

    def select_category(
        self,
        goal: str,
        candidates: list[CategoryCandidate],
    ) -> tuple[str, str, float]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        if not candidates:
            return "", "No category candidates available.", 0.0

        prompt = self._build_prompt(goal=goal, candidates=candidates)
        payload = self._call_gemini(prompt)
        category_url = str(payload.get("selected_category_url", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        confidence = self._coerce_confidence(payload.get("confidence"))
        valid_urls = {candidate.category_url for candidate in candidates}
        if category_url in valid_urls:
            return category_url, reason, confidence
        return "", reason or "Gemini did not return a valid category URL.", confidence

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
            raise RuntimeError(f"Gemini returned invalid JSON: {text}") from exc

    @staticmethod
    def sanitize_error_message(message: str) -> str:
        return re.sub(r"([?&]key=)[^&\\s]+", r"\\1REDACTED", message)

    @staticmethod
    def _coerce_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(confidence, 1.0))

    @staticmethod
    def _build_prompt(goal: str, candidates: list[CategoryCandidate]) -> str:
        candidate_lines = []
        for index, candidate in enumerate(candidates, start=1):
            candidate_lines.append(
                "\n".join(
                    [
                        f"Candidate {index}",
                        f"URL: {candidate.category_url}",
                        f"Anchor text: {candidate.anchor_text}",
                        f"Rule score: {candidate.score}",
                        f"Rule reason: {candidate.reason}",
                    ]
                )
            )

        return "\n\n".join(
            [
                "You are ranking website category candidates for a product-catalog crawl.",
                "The user goal is a product/category information request.",
                "Choose the single best category URL for the user's goal from the candidates below.",
                "Only choose from the provided candidate URLs.",
                "Return strict JSON only with this schema:",
                '{"selected_category_url": "...", "reason": "...", "confidence": 0.0}',
                f"User goal: {goal}",
                "\n\n".join(candidate_lines),
            ]
        )
