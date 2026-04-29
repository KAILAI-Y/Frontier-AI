from __future__ import annotations

import logging
import time
from typing import Any

from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .deduplication import DeduplicationAgent
from .models import ProductLinkRecord


class CategoryNavigationAgent:
    def __init__(self, browser: Browser, page_timeout_ms: int = 60000) -> None:
        self.browser = browser
        self.page_timeout_ms = page_timeout_ms
        self.dedup_agent = DeduplicationAgent()

    def discover(
        self, category_urls: list[str], max_pages: int | None = None
    ) -> list[ProductLinkRecord]:
        all_records: list[ProductLinkRecord] = []
        for category_url in category_urls:
            all_records.extend(
                self._discover_category(category_url=category_url, max_pages=max_pages)
            )
        return all_records

    def _discover_category(
        self, category_url: str, max_pages: int | None = None
    ) -> list[ProductLinkRecord]:
        context = self.browser.new_context()
        page = context.new_page()
        try:
            logging.info("Open category %s", category_url)
            page.goto(
                category_url,
                wait_until="domcontentloaded",
                timeout=self.page_timeout_ms,
            )
            self._wait_for_listing_results(page, minimum_count=15, timeout_ms=30000)

            records: list[ProductLinkRecord] = []
            page_number = 1

            while True:
                self._wait_for_listing_results(page, minimum_count=1, timeout_ms=15000)
                links = self._extract_visible_links(page)
                existing_urls = [record.product_url for record in records]
                page_urls = [str(link.get("href", "")).strip() for link in links]
                deduped_page_urls = set(
                    self.dedup_agent.dedupe_product_urls(existing_urls + page_urls)
                )
                added_this_page = 0

                for link in links:
                    href = str(link.get("href", "")).strip()
                    if not href or href in existing_urls:
                        continue
                    if href not in deduped_page_urls:
                        continue
                    records.append(
                        ProductLinkRecord(
                            category_url=category_url,
                            page_number=page_number,
                            product_url=href,
                            anchor_text=str(link.get("text", "")).strip(),
                        )
                    )
                    added_this_page += 1
                    existing_urls.append(href)

                logging.info(
                    "Category %s page %s discovered %s visible product links (%s new)",
                    category_url,
                    page_number,
                    len(links),
                    added_this_page,
                )

                if max_pages is not None and page_number >= max_pages:
                    break

                if not self._has_next_page(page):
                    break

                before = {record.product_url for record in records}
                old_url = page.url
                if not self._go_to_next_page(page):
                    break

                self._wait_for_listing_results(page, minimum_count=1, timeout_ms=15000)
                page_number += 1
                new_links = self._extract_visible_links(page)
                after = {str(item.get("href", "")).strip() for item in new_links}
                if page.url == old_url and after.issubset(before):
                    logging.info("No new links after next-page click; stopping pagination")
                    break

            return records
        finally:
            context.close()

    def _extract_visible_links(self, page: Page) -> list[dict[str, Any]]:
        return page.evaluate(self._visible_product_links_script())

    def _wait_for_listing_results(
        self, page: Page, minimum_count: int, timeout_ms: int
    ) -> None:
        deadline = time.time() + timeout_ms / 1000
        last_count = 0
        while time.time() < deadline:
            try:
                count = page.locator("a.result[href]").count()
            except Exception:
                count = 0
            last_count = count
            if count >= minimum_count:
                page.wait_for_timeout(800)
                return
            page.wait_for_timeout(500)
        raise PlaywrightTimeoutError(
            f"Timed out waiting for listing results; saw {last_count} result anchors"
        )

    def _has_next_page(self, page: Page) -> bool:
        for selector in self._next_page_candidates():
            locator = page.locator(selector).first
            try:
                if not locator.count() or not locator.is_visible():
                    continue
                disabled = locator.get_attribute("disabled")
                aria_disabled = locator.get_attribute("aria-disabled")
                classes = (locator.get_attribute("class") or "").lower()
                if disabled is not None or aria_disabled == "true" or "disabled" in classes:
                    return False
                return True
            except Exception:
                continue
        return False

    def _go_to_next_page(self, page: Page) -> bool:
        for selector in self._next_page_candidates():
            locator = page.locator(selector).first
            try:
                if not locator.count() or not locator.is_visible():
                    continue
                locator.click(timeout=15000)
                return True
            except PlaywrightTimeoutError:
                logging.warning("Timed out clicking next-page control")
                return False
            except Exception:
                continue
        return False

    @staticmethod
    def _visible_product_links_script() -> str:
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

  const anchors = [
    ...document.querySelectorAll('a.result[href]'),
    ...document.querySelectorAll('.ais-Hits-item a.result[href]'),
    ...document.querySelectorAll('a[href*="/product/"]'),
  ];
  return anchors
    .filter((a) => isVisible(a))
    .map((a) => ({
      href: a.href,
      text: (a.textContent || '').replace(/\\s+/g, ' ').trim(),
    }))
    .filter((item) => item.href && item.href.includes('/product/'));
})();
"""

    @staticmethod
    def _next_page_candidates() -> list[str]:
        return [
            'a[aria-label*="Next" i]',
            'button[aria-label*="Next" i]',
            'a[title*="Next" i]',
            'button[title*="Next" i]',
            'a:has-text("Next")',
            'button:has-text("Next")',
            'a:has-text("Next page")',
            'button:has-text("Next page")',
            '.ais-Pagination-link[aria-label*="Next" i]',
            '.ais-Pagination-item--next a',
            '.pages-item-next a',
        ]
