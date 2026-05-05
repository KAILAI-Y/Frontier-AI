# Safco Dental Product Scraper POC

## Overview

This repository contains a working proof-of-concept for extracting structured product catalog data from selected Safco Dental Supply categories.

The main design choice was to treat many Safco product pages as product-family pages rather than single-SKU pages. Because of that, the output grain is item-level with quantity-tier expansion, not one row per product URL.

Current scope:
- `https://www.safcodental.com/catalog/gloves`
- `https://www.safcodental.com/catalog/sutures-surgical-products`

Current crawl strategy:
1. Discover visible product URLs from the rendered category pages.
2. Visit each product detail page.
3. Expand item-table rows into item-level records.
4. Expand quantity-based tier pricing into separate rows.
5. Deduplicate final rows by `Product URL + Item Number + Qty + Price`.
6. Enrich alternative product fields with a Gemini-based ranking step.
7. Export normalized records to CSV.

Status:
- The current workflow is implemented for the two target categories above
- The latest outputs are written to `output/product_links.csv`, `output/products.csv`, and `output/crawl_report.csv`

---

## Architecture Overview

### High-Level Flow

```text
Category Page
  -> Category Navigation Agent
  -> Product Fetch Agent
  -> Page Classification Agent
  -> Item Extraction Agent
  -> Price Tier Expansion Agent
  -> Deduplication Agent
  -> LLM Alternative Ranking Agent
  -> Validation / Recovery Agent
  -> CSV Output + Crawl Report
```

### Current Components

- `scraper/discover_links.py`
  - CLI entrypoint for category discovery using Playwright-rendered visible links.
- `scraper/extract_products.py`
  - CLI entrypoint for detail extraction and output writing.
- `scraper/run_llm_alternatives.py`
  - CLI entrypoint for Gemini-based alternative product enrichment.
- `scraper/run_goal_crawl.py`
  - CLI entrypoint for goal-driven category resolution plus downstream extraction.
- `scraper/run_llm_irregular.py`
  - CLI entrypoint for probing unsupported-layout pages with an LLM-based irregular extraction fallback.
- `scraper/agents/`
  - Contains the workflow agents used by the entrypoints.
- `output/`
  - Stores visible link exports, normalized product data, and crawl diagnostics.

---

## Why I Chose This Approach

### What I tried and why I ended up here

I did not end up with the first approach I tried.

My initial approach was closer to a traditional scraper:
- fetch the category page
- use embedded config / index data to discover product URLs
- request product pages and parse them

That worked to a point, but it exposed an important issue: category discovery through the index could return product URLs that were no longer the same links a user would actually reach from the rendered category page. In practice, that meant I could discover URLs that looked valid from the upstream listing source but did not line up cleanly with the visible category experience.

Because of that, I changed the first stage of the workflow. I now use browser automation to collect the product URLs that are actually visible on the rendered category pages, page by page. That choice trades some speed for better alignment with the user-visible site and avoids a class of stale or mismatched listing URLs.

For detail extraction, I stayed deterministic rather than LLM-first. The detail pages expose enough stable signals to make that worthwhile:
- visible item tables
- embedded `masterData`
- JSON-LD
- breadcrumbs
- page-level text for fallback classification

### Main trade-offs

- I chose browser-based visible-link discovery for category pages because it better matches what a real user can click and ensures we capture the currently active product information on the website, even though it is slower than pure index/API discovery.
- I chose deterministic parsing for detail pages because the site already exposes structured signals, which makes extraction more reproducible and easier to debug than sending the primary path through an LLM.
- I chose item-level plus qty-tier-level output grain because family-level exports would hide real catalog distinctions such as different item numbers and different price breaks.
- I kept LLM usage out of the primary extraction path because the core site structure was already deterministic enough to parse reliably. I used Gemini where it adds more practical value: constrained alternative-product ranking, and an experimental irregular-layout probe for pages the deterministic extractor marked as unsupported.

### How I thought about LLM usage

I did not want to add LLMs just because the assignment mentions agents.

For the main crawl path, the site already exposes enough structure through rendered listings, `masterData`, JSON-LD, breadcrumbs, and page-level metadata. In those parts of the workflow, an LLM would add cost and complexity without clearly improving correctness.

Because of that, I used a deterministic-first approach for:
- category discovery
- page fetching
- page classification
- item extraction
- qty-tier expansion

The place where LLMs add more practical value in this prototype is alternative matching. `Alternative Product URL` and `Alternative Item Number` are not directly exposed as clean structured fields, and they require semantic judgment rather than simple parsing. That makes constrained candidate ranking a better fit for Gemini than the core extraction path.

I also added an experimental irregular-page probe for unsupported layouts, but I kept that outside the primary workflow because it is still a diagnostic tool rather than a trusted extraction path.

---

## Agent Responsibilities

This prototype is organized as an agent-oriented workflow, but still kept lightweight enough to run and inspect locally.

### 1. Category Navigation Agent

Responsibilities:
- Open the target category page
- Wait for rendered listing results
- Collect visible product URLs
- Handle pagination

Implementation:
- `scraper/discover_links.py`

### 2. Product Page Agent

Responsibilities:
- Open each product detail page
- Resolve page-level metadata
- Extract embedded structured data sources

Implementation:
- `scraper/extract_products.py`

### 3. Page Classification Agent

Responsibilities:
- Determine what kind of product page was fetched
- Route the page into the correct extraction path

Current page types:
- `item_table_page`
- `multi_group_item_page`
- `no_item_options_page`
- `unsupported_layout`
- `broken_page`
- `listing_fallback_page`

Implementation:
- `scraper/extract_products.py`

### 4. Item Extraction Agent

Responsibilities:
- Parse item-table rows from product detail pages
- Extract item-level fields such as item number, Mfr #, availability, unit, and attributes

Implementation:
- `scraper/extract_products.py`

### 5. Price Tier Expansion Agent

Responsibilities:
- Expand quantity-based pricing into separate records
- Preserve product URL + item number + qty tier as record grain

Implementation:
- `scraper/extract_products.py`

### 6. Validation / Normalization Agent

Responsibilities:
- Normalize field values
- Filter placeholder images
- Clean text fields
- Deduplicate image URLs

Implementation:
- `scraper/extract_products.py`

### 7. Deduplication Agent

Responsibilities:
- Deduplicate visible product URLs
- Deduplicate price tiers
- Deduplicate final output rows by `Product URL + Item Number + Qty + Price`

Implementation:
- `scraper/agents/deduplication.py`

### 8. LLM Alternative Ranking Agent

Responsibilities:
- Generate a constrained candidate set for each product family
- Use Gemini to rank which candidate is the strongest substitute
- Write `Alternative Product URL` and `Alternative Item Number`

Implementation:
- `scraper/agents/llm_alternatives.py`
- `scraper/run_llm_alternatives.py`

### 9. LLM Irregular Extraction Agent

Responsibilities:
- Inspect pages classified as `unsupported_layout`
- Look at constrained HTML and text snippets rather than the full page
- Test whether additional item-level information is present beyond the deterministic fallback

Implementation:
- `scraper/agents/llm_irregular_extraction.py`
- `scraper/run_llm_irregular.py`

Current status:
- implemented as a probe / fallback experiment
- not yet wired into the main extraction path automatically

### 10. Recovery / Failure Handling Agent

Responsibilities:
- Handle detail-page failures
- Fallback when extraction is partial
- Surface failure classes for later retry or review

Implementation:
- `scraper/extract_products.py`
- `output/crawl_report.csv`

Current output:
- page type
- classification reason
- status
- error stage
- error type
- fallback usage
- records written

### 11. Persistence Agent

Responsibilities:
- Write normalized CSV outputs
- Preserve crawl results for inspection and export

Implementation:
- `output/product_links.csv`
- `output/products.csv`
- `output/crawl_report.csv`

---

## Setup & Execution Instructions

### Run Order

Recommended order:
1. discover visible product links from category pages
2. extract and normalize product rows
3. enrich alternative fields with Gemini
4. optionally probe unsupported pages with the irregular-layout LLM step

If you want to start from a higher-level instruction instead of a known category slug, you can use the goal-driven entrypoint:

```bash
python3 scraper/run_goal_crawl.py \
  --goal "Collect all gloves product information from this company website." \
  --output output/products.csv \
  --report output/crawl_report.csv \
  --checkpoint output/checkpoint.csv
```

That workflow:
- first classifies whether the goal is a product-category request
- starts from the Safco homepage
- collects visible category candidates
- uses Gemini to match the goal against that category inventory when an API key is available
- falls back to rule-based matching if Gemini is disabled or unavailable
- runs the existing visible-link discovery and detail extraction pipeline

### Prerequisites

- Python 3.11+ recommended
- Playwright with Chromium installed locally

### Install Dependencies

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

### Run Category Discovery

Run both categories:

```bash
python3 scraper/discover_links.py
```

Output:
- `output/product_links.csv`

Run a single category:

```bash
python3 scraper/discover_links.py \
  --category gloves \
  --output output/product_links_gloves.csv
```

Run multiple categories by slug:

```bash
python3 scraper/discover_links.py \
  --category gloves \
  --category sutures-surgical-products \
  --output output/product_links.csv
```

### Run Goal-Driven Crawl

Resolve a category from a high-level goal and then run the normal extraction pipeline:

```bash
python3 scraper/run_goal_crawl.py \
  --goal "Collect all gloves product information from this company website." \
  --output output/products.csv \
  --report output/crawl_report.csv \
  --checkpoint output/checkpoint.csv
```

You can also run it interactively and type the request when prompted:

```bash
python3 scraper/run_goal_crawl.py \
  --output output/products.csv \
  --report output/crawl_report.csv \
  --checkpoint output/checkpoint.csv
```

Current design notes:
- the goal must describe a product/category information request, not arbitrary site content
- the planner starts from the Safco homepage
- it collects visible `/catalog/` candidates from the homepage and catalog root
- it uses Gemini-based semantic matching over those category candidates when available
- it falls back to keyword relevance scoring if no Gemini key is configured
- once a category is selected, the downstream workflow is the same as the standard pipeline

### Run Item-Level Detail Extraction

Run full extraction using the visible product links:

```bash
python3 scraper/extract_products.py \
  --input-links-csv output/product_links.csv \
  --max-products-per-category 0 \
  --output output/products.csv \
  --report output/crawl_report.csv \
  --checkpoint output/checkpoint.csv
```

### Run Alternative Product Enrichment

Run Gemini-based alternative enrichment after the main extraction step:

```bash
python3 scraper/run_llm_alternatives.py \
  --input output/products.csv \
  --output output/products.csv \
  --max-candidates 8 \
  --limit-families 0
```

### Run Irregular Page LLM Probe

Test whether `unsupported_layout` pages contain additional item-level information:

```bash
python3 scraper/run_llm_irregular.py \
  --report output/crawl_report.csv \
  --output output/llm_irregular_report.csv \
  --limit 5
```

### Sample Outputs

- `output/product_links.csv`
- `output/products.csv`
- `output/crawl_report.csv`

### Notes

- `--max-products-per-category 0` means no per-category limit
- Use `--max-products-per-category 1` or `5` for quick validation runs
- If `output/` is empty, run category discovery before extraction because `extract_products.py` depends on `output/product_links.csv`
- `--checkpoint output/checkpoint.csv` writes per-URL crawl state as the extraction runs
- `--resume` skips URLs already marked `success` in the checkpoint file

### Recommended Full Run

Use this flow to regenerate the current dataset:

```bash
python3 scraper/discover_links.py
python3 scraper/extract_products.py \
  --input-links-csv output/product_links.csv \
  --max-products-per-category 0 \
  --output output/products.csv \
  --report output/crawl_report.csv \
  --checkpoint output/checkpoint.csv
python3 scraper/run_llm_alternatives.py \
  --input output/products.csv \
  --output output/products.csv \
  --max-candidates 8 \
  --limit-families 0
```

---

## Sample Output Schema

### Output File

- `output/products.csv`

### Current Columns

- `Product Name`
- `Brand`
- `Item Number`
- `Mfr #`
- `Category Hierarchy`
- `Product URL`
- `Qty`
- `Price`
- `Unit`
- `Availability`
- `Description`
- `Attributes`
- `Image URLs`
- `Alternative Product URL`
- `Alternative Item Number`

### Field Definitions

- `Description`
  - product-page-level description or main page message
- `Attributes`
  - item-table-level row description
- `Qty`
  - price-break quantity tier
- `Unit`
  - item-level package or size descriptor when detectable

### Record Grain

Current record grain:
- one row per `Product URL + Item Number + Qty tier`

---

## Limitations

- Category discovery is intentionally based on visible rendered results. That makes the crawl line up better with what a user can actually click, but it also means this method is bounded by what the category pages choose to render.
- The extraction path works best when Safco exposes item-level structure through `masterData` or a stable item table. Pages that do not expose that structure can only be partially recovered.
- Some valid Safco product pages are informational or unavailable-option pages rather than sellable item-table pages. For those, the crawler writes a page-level fallback record instead of item-level rows.
- `Unit` is heuristic. When packaging or size is not explicit in the item text, the crawler leaves it blank rather than guessing.
- `Alternative Product URL` and `Alternative Item Number` depend on a constrained Gemini ranking step over generated candidates. If the candidate set is weak, those fields may remain blank or conservative.
- The irregular-page LLM probe is still experimental. It is useful for investigating unsupported layouts, but it is not yet trusted enough to automatically overwrite the deterministic result set.

---

## Failure Handling

### Current Failure Handling

Current behaviors:
- detail page HTTP failures fall back to a smaller page-level record instead of dropping the URL silently
- placeholder images are filtered out so the export does not fill up with white placeholder assets
- item-level tier pricing prefers structured embedded data over brittle HTML-only parsing
- product pages are classified before final output is written
- `No options of this product are available.` pages are treated as a real page type, not just a generic parse failure
- crawl diagnostics are written to `output/crawl_report.csv`
- per-URL checkpoint state is written to `output/checkpoint.csv`

### Crawl Report

Current crawl report columns:
- `Category URL`
- `Product URL`
- `Page Number`
- `Page Type`
- `Classification Reason`
- `Status`
- `Error Stage`
- `Error Type`
- `Error Message`
- `HTTP Status`
- `Fallback Used`
- `Records Written`

### Current Failure Semantics

- `success`
  - page was classified and extracted through the primary item-table path
- `partial_fallback`
  - the page was reachable, but extraction fell back to a smaller page-level record
- `no_item_options_page`
  - represented in the report through `Page Type`, indicating a real page type rather than a broken fetch

The crawl report is there to answer practical questions:
- which URL failed
- at what stage it failed
- whether it was a true failure or just a different page type
- whether fallback logic was used

---

## How I Would Scale To Full-Site Crawling In Production

### Scaling Path

- split category discovery and detail extraction into separate jobs
- move the current checkpoint pattern from CSV to a stronger persistent crawl-state store
- move from CSV-only persistence to a DB or object storage backed pipeline
- add queue-based processing for detail extraction workers
- add retry policies, backoff, and domain-level rate limiting
- make writes idempotent based on stable natural keys such as `Product URL + Item Number + Qty`
- introduce distributed workers only after crawl state and observability are stable

### Production Hardening Areas

- logging and metrics
- timeout management
- selector drift monitoring
- schema evolution
- secret management
- deployment path

This submission stays in POC scope. I focused implementation on the parts that change output correctness:
- correct category discovery
- correct output grain
- page classification
- fallback handling

---

## How I Would Monitor Data Quality

### Data Quality Goals

- coverage of discovered URLs
- extraction success rate
- partial success rate
- field completeness by column
- placeholder / invalid image rate
- item count drift
- price-tier extraction accuracy

### Proposed Monitoring Signals

- crawl success/failure dashboard
- per-category row counts
- per-field null rates
- unexpected schema drift alerts
- sampled record QA
- source-to-output consistency checks

---

## Current Repository Structure

```text
.
├── README.md
├── scraper/
│   ├── __init__.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── category_navigation.py
│   │   ├── deduplication.py
│   │   ├── item_extraction.py
│   │   ├── llm_alternatives.py
│   │   ├── llm_irregular_extraction.py
│   │   ├── models.py
│   │   ├── page_classification.py
│   │   └── recovery.py
│   ├── discover_links.py
│   ├── extract_products.py
│   ├── run_llm_alternatives.py
│   └── run_llm_irregular.py
├── requirements.txt
└── output/
```

---

## Submission Notes

- The primary workflow is runnable today for the two target categories.
- The system already separates navigation, extraction, classification, deduplication, LLM enrichment, experimental irregular-page probing, and recovery concerns.
- The biggest design choice in this project was switching category discovery to visible rendered links instead of relying only on upstream listing/index data.
