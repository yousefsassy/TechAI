# Conversational price comparison

## Toprix investigation, 24 November 2026

Toprix offers model search and category browsing. The Informatique category exposes subcategories and filters for store, brand, minimum and maximum price, promotion and availability. Its public LG D24TIH page shows a retailer-hosted image, SKU, technical description and two retailer offers. Each offer has a stock label, price and purchase link. [Home](https://toprix.tn/) · [Category](https://toprix.tn/categories/informatique) · [Example comparison](https://toprix.tn/produit/69993598fa64919072dd2424)

Toprix describes automatic, regular collection from Mytek, Tunisianet and Spacenet. The public research did not establish the matching algorithm, internal index, refresh interval, cache expiry, a documented read API or a catalogue reuse licence. The robots file could not be verified. The store submission page accepts catalogue additions. TechStock uses independently collected retailer evidence. Toprix ingestion would require a documented, permitted source. [About Toprix](https://www.toprix.tn/a-propos) · [Store submission](https://toprix.tn/ajouter)

## Request path

1. The authenticated React client posts a message and conversation ID to the NDJSON shopping endpoint. The backend resolves structured constraints against recent session-owned results. Common model, GPU/RAM and mouse searches and short follow-ups run without a provider request. Other turns use the existing tool-calling model and offline fallback. Explicit gaming intent for mice, numeric budgets and GPU models are grounded in code.
2. SQLite filters stock, budget, collection age and category. An FTS5 title index retrieves candidates for type phrases. English, French and Arabic aliases expand concepts into retailer vocabulary. A second matcher checks titles, compatibility phrases, attributes and hard specs. This semantic layer is vocabulary based. Unknown synonyms involve the planner and can still miss listings.
3. An ordinary recent match completes from cache. On a miss, stale cache or insufficient comparison coverage, the existing discovery workers consult source robots rules and their category pages or cached public sitemaps. Coverage checks can skip recently represented sources. Explicit refreshes try known URLs first. Per-host pacing, Retry-After pauses, page limits and database leases apply across requests. NDJSON previews update as verified observations arrive.
4. Scraped product facts are stored per retailer page. Eligible offers are grouped before the 12-card limit using a checksum-valid GTIN, a brand and manufacturer part number with configuration guards, an exact normalized branded model/title, or an existing shared catalogue product ID. Retailer-local SKU alone is insufficient. Missing or differently stated identifiers can leave separate cards. Recollection updates existing offer IDs, keeping cart references valid.
5. The representative offer is the lowest eligible price for its product. All table deltas use decimal TND arithmetic. Out-of-stock, unconfirmed, stale, demo and over-budget offers are excluded from comparisons. Each offer carries its own observation timestamp and current-request verification flag. Unknown stock never qualifies as available. The cheapest result describes the stores and observations covered.
6. Recommendation ranking considers requested features, published capabilities, price relative to the budget, freshness and recent HTTP success as a small source-accessibility signal. This cannot measure fulfilment reliability, sensor quality or real-world performance. Explicit cheapest searches sort by price. Known-model comparisons show the first matching listing for each named subject and identify the variant actually found. The original LangGraph approval and purchase-order workflow remains a separate API path.

## Operation and measurement

| Setting or endpoint | Purpose |
|---|---|
| SHOPPING_FRESH_HOURS | Recent-cache threshold, default 6 hours |
| CATALOG_MAX_AGE_HOURS | Maximum eligible observation age, default 48 hours |
| SHOPPING_LIVE_DISCOVERY | Enable on-demand retailer requests |
| CATALOG_AUTO_REFRESH / CATALOG_REFRESH_HOURS | Enable and schedule the existing background catalogue job |
| INPUT_COST_PER_MILLION / OUTPUT_COST_PER_MILLION | Provider rates in USD per million tokens. Missing price or usage yields an unknown estimate |
| GET /shopping/metrics | Authenticated process-level totals for requests, failures, latencies, usage, cache hits and retailer coverage |
| GET /shopping/catalog-status | Background collection status |

Startup requires SQLite compiled with FTS5. It builds the title index for existing rows and installs transactional insert, rename and delete triggers. Product search reads current offer rows directly. No embedding service or external search server is required.

Each chat result includes server-side duration, time to the first nonempty result, initial catalogue hits, retailer lookup time, returned stores, tokens and estimated cost. A cache hit means the initial retrieval returned eligible cards, even if live verification followed. Process totals reset on restart. HTTP success means the source responded successfully. Offline constraint precision is measured separately with `python -m eval.shopping_eval`. Human relevance, recall and ground-truth market coverage require labelled data.

## Local benchmark

On 24 November 2026, the baseline and modified service searched the same SQLite snapshot containing 108 offers. Each case used one warm-up plus 30 timed calls with tracing disabled. Baseline measurements preceded edits. The mouse search now excludes mouse pads and accessory listings.

| Cached retrieval | Before median | After median | Before p95 | After p95 |
|---|---:|---:|---:|---:|
| Laptops, 4,000 TND ceiling | 4.14 ms | 3.31 ms | 4.66 ms | 3.75 ms |
| Mice, 400 TND ceiling | 4.64 ms | 1.34 ms | 5.04 ms | 1.51 ms |

A separate 30-turn cached service benchmark for “Show me mice under 400 TND” measured 3.72 ms median and 4.30 ms p95 including planning and conversation persistence. Browser transport, production concurrency, external model calls and live retailer latency were outside this measurement. Run `python -m eval.shopping_benchmark` against a current catalogue to repeat the retrieval benchmark.

The snapshot constraint audit returned three matching tablet cards, all within the evaluated filters. The other two queries returned no cards and have undefined precision. Fixture tests exercise cross-store identity, variants, strict budgets, gaming relevance, multilingual search, named comparisons, cache provenance and conversational refinements. Recorded-page and simulated-transport tests cover extraction and progressive results. Live retailer DNS and local SMTP socket binding were unavailable in the implementation environment.
