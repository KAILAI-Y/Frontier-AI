from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .llm_intent_matching import GeminiIntentMatchingAgent
from .models import CategoryCandidate, GoalPlan

SAFECO_HOME_URL = "https://www.safcodental.com/"
CATALOG_BASE_URL = "https://www.safcodental.com/catalog/"
STOP_WORDS = {
    "a",
    "all",
    "an",
    "and",
    "collect",
    "company",
    "content",
    "find",
    "for",
    "from",
    "help",
    "information",
    "on",
    "page",
    "pages",
    "product",
    "products",
    "related",
    "site",
    "the",
    "this",
    "website",
    "with",
}


class GoalPlanningAgent:
    def __init__(
        self,
        browser: Browser,
        page_timeout_ms: int = 45000,
        llm_matcher: GeminiIntentMatchingAgent | None = None,
    ) -> None:
        self.browser = browser
        self.page_timeout_ms = page_timeout_ms
        self.llm_matcher = llm_matcher

    def plan(self, goal: str, homepage_url: str = SAFECO_HOME_URL) -> GoalPlan:
        keywords = self._extract_keywords(goal)
        candidates = self.inventory_categories(goal=goal, homepage_url=homepage_url, keywords=keywords)

        deduped: dict[str, CategoryCandidate] = {}
        for candidate in candidates:
            current = deduped.get(candidate.category_url)
            if current is None or candidate.score > current.score:
                deduped[candidate.category_url] = candidate

        ranked = sorted(
            deduped.values(),
            key=lambda item: (-item.score, len(item.category_url), item.category_url),
        )

        llm_selected_url = ""
        if ranked and self.llm_matcher is not None:
            try:
                llm_selected_url, llm_reason, llm_confidence = self.llm_matcher.select_category(
                    goal=goal,
                    candidates=ranked,
                )
                if llm_selected_url:
                    for candidate in ranked:
                        if candidate.category_url == llm_selected_url:
                            candidate.score += 100.0
                            candidate.reason = (
                                f"{candidate.reason}; llm selected: {llm_reason}; "
                                f"llm confidence={llm_confidence:.2f}"
                            ).strip("; ")
                            break
                    ranked = sorted(
                        ranked,
                        key=lambda item: (-item.score, len(item.category_url), item.category_url),
                    )
            except Exception as exc:
                sanitized = self.llm_matcher.sanitize_error_message(str(exc))
                logging.warning("LLM category matching failed for goal %r: %s", goal, sanitized)

        if not ranked:
            ranked = self._fallback_candidates_from_keywords(keywords)

        selected = ranked[0] if ranked else CategoryCandidate("", "", 0.0, "No candidate found")
        logging.info(
            "Goal planner selected category %s for goal %r using keywords %s",
            selected.category_url,
            goal,
            ", ".join(keywords) or "(none)",
        )
        return GoalPlan(
            goal=goal,
            keywords=keywords,
            selected_category_url=selected.category_url,
            selected_anchor_text=selected.anchor_text,
            candidates=ranked,
        )

    def inventory_categories(
        self,
        goal: str,
        homepage_url: str = SAFECO_HOME_URL,
        keywords: list[str] | None = None,
    ) -> list[CategoryCandidate]:
        keywords = keywords or self._extract_keywords(goal)
        candidates: list[CategoryCandidate] = []
        context = self.browser.new_context()
        page = context.new_page()
        try:
            candidates.extend(self._collect_candidates_from_url(page, homepage_url, keywords))
            catalog_root = urljoin(homepage_url, "/catalog/")
            if catalog_root.rstrip("/") != homepage_url.rstrip("/"):
                candidates.extend(self._collect_candidates_from_url(page, catalog_root, keywords))
        finally:
            context.close()
        return candidates

    def _collect_candidates_from_url(
        self, page: Page, url: str, keywords: list[str]
    ) -> list[CategoryCandidate]:
        try:
            logging.info("Goal planner opening %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
            page.wait_for_timeout(1200)
        except PlaywrightTimeoutError:
            logging.warning("Goal planner timed out opening %s", url)
            return []
        except Exception as exc:
            logging.warning("Goal planner failed to open %s: %s", url, exc)
            return []

        raw_links = page.evaluate(self._candidate_links_script())
        candidates: list[CategoryCandidate] = []
        for item in raw_links:
            href = str(item.get("href", "")).strip()
            text = str(item.get("text", "")).strip()
            if not href:
                continue
            normalized = self._normalize_catalog_url(href)
            if not normalized:
                continue
            score, reason = self._score_candidate(normalized, text, keywords)
            if score <= 0:
                continue
            candidates.append(
                CategoryCandidate(
                    category_url=normalized,
                    anchor_text=text,
                    score=score,
                    reason=reason,
                )
            )
        return candidates

    def _score_candidate(
        self, category_url: str, anchor_text: str, keywords: list[str]
    ) -> tuple[float, str]:
        url_lower = category_url.lower()
        text_lower = anchor_text.lower()
        slug = url_lower.rstrip("/").split("/catalog/")[-1].strip("/")
        slug_tokens = self._tokenize(slug.replace("-", " "))
        text_tokens = self._tokenize(text_lower)

        score = 0.0
        reasons: list[str] = []
        if "/catalog/" in url_lower:
            score += 4
            reasons.append("catalog path")

        for keyword in keywords:
            if keyword in slug:
                score += 6
                reasons.append(f"slug matched {keyword}")
            elif keyword in text_lower:
                score += 5
                reasons.append(f"text matched {keyword}")
            elif keyword in slug_tokens:
                score += 4
                reasons.append(f"slug token matched {keyword}")
            elif keyword in text_tokens:
                score += 3
                reasons.append(f"text token matched {keyword}")

        if keywords and all(keyword in slug or keyword in text_lower for keyword in keywords):
            score += 3
            reasons.append("all keywords covered")

        if anchor_text:
            score += 0.5
            reasons.append("visible anchor")

        return score, "; ".join(reasons)

    def _fallback_candidates_from_keywords(
        self, keywords: list[str]
    ) -> list[CategoryCandidate]:
        guesses: list[CategoryCandidate] = []
        phrases: list[str] = []
        if keywords:
            phrases.append("-".join(keywords))
            if len(keywords) > 1:
                phrases.append(keywords[-1])
                phrases.append(keywords[0])

        seen: set[str] = set()
        for phrase in phrases:
            slug = phrase.strip("-")
            if not slug:
                continue
            category_url = urljoin(CATALOG_BASE_URL, slug)
            if category_url in seen:
                continue
            seen.add(category_url)
            guesses.append(
                CategoryCandidate(
                    category_url=category_url,
                    anchor_text="",
                    score=1.0,
                    reason="constructed from goal keywords",
                )
            )
        return guesses

    @staticmethod
    def _extract_keywords(goal: str) -> list[str]:
        words = re.findall(r"[a-z0-9]+", goal.lower())
        keywords: list[str] = []
        for word in words:
            if word in STOP_WORDS:
                continue
            if len(word) <= 2:
                continue
            keywords.append(word)
        return list(dict.fromkeys(keywords))

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    @staticmethod
    def _normalize_catalog_url(href: str) -> str:
        try:
            parsed = urlparse(href)
        except Exception:
            return ""
        if parsed.netloc and "safcodental.com" not in parsed.netloc:
            return ""
        path = parsed.path or ""
        if "/catalog/" not in path:
            return ""
        if "/product/" in path:
            return ""
        slug = path.split("/catalog/")[-1].strip("/")
        if not slug:
            return ""
        return urljoin(CATALOG_BASE_URL, slug)

    @staticmethod
    def _candidate_links_script() -> str:
        return """
(() => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return (
      rect.width > 0 &&
      rect.height > 0 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none'
    );
  };

  const anchors = [...document.querySelectorAll('a[href]')];
  return anchors
    .filter((a) => isVisible(a))
    .map((a) => ({
      href: a.href,
      text: (a.textContent || '').replace(/\\s+/g, ' ').trim(),
    }))
    .filter((item) => item.href && item.href.includes('/catalog/'));
})();
"""
