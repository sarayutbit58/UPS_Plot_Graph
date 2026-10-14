# UPS evidence database schema

Updated 2026-10-14, implementation pass 1. The canonical business DDL is `../schema.sql`; migration metadata is managed separately by the migration runner.

Pass 2 correction, 2026-10-14: source paths and locators preserve exact whitespace (including Thai directory/sheet names and repeated spaces), with NFC normalization and path slash normalization only. Product/manufacturer normalization is unchanged.

Final pass 3 correction, 2026-10-14: canonical SKU requires `manufacturer_sku`, or `sku` explicitly typed with `sku_type=manufacturer`. Untyped/distributor article codes remain only in raw evidence; identity falls back to manufacturer+model. Explicit `non_ups_category` flags/notes and `model_conflict` flags are quarantined. Schema and ERD stay unchanged.

## Evidence model

`products` stores identity, while `product_sources` stores each source assertion. Every specification and offer references a `product_source_id`, which resolves to an exact source file and nonempty locator. Conflicting specifications and prices across files or locators remain separate rows. No source is promoted to a single authoritative rating or current price.

| Parent | Child | Cardinality |
|---|---|---|
| manufacturers | products | Manufacturer 1 to products 0..many; product manufacturer 0..1 |
| products | product_sources | Product 1 to observations 0..many; observation product exactly 1 |
| source_files | product_sources | File 1 to observations 0..many; observation file exactly 1 |
| product_sources | product_specifications | Observation 1 to specification 0..1 |
| product_sources | price_offers | Observation 1 to offer 0..1 |
| source_files | import_rejections | File 1 to rejections 0..many |
| topology_types | product_specifications | Type 1 to specifications 0..many; specification topology 0..1 |
| form_factors | product_specifications | Form 1 to specifications 0..many; specification form 0..1 |
| currencies | price_offers | Currency 1 to offers 0..many; offer currency 0..1 |

Foreign keys use restrictive default deletion. Callers enable `PRAGMA foreign_keys = ON` on every connection. `raw_json` stores original fields including conflicting or unmapped attributes; `raw_text` preserves source text. Confidence and extraction notes remain in `raw_json` rather than become commercial facts.

## Normalization API and deduplication

`normalize_record(raw)` returns a dictionary. Accepted records have `accepted=True`, the original record serialized in `raw_json`, source path/locator, and flattened normalized fields. Rejections have `accepted=False`, `reason`, source path/locator and `raw_json`. The importer must persist rejections against the already registered source file.

Manufacturer keys normalize Unicode NFC, whitespace and case. Product keys are JSON arrays of manufacturer key plus `sku` and exact normalized manufacturer SKU, falling back to `model` and exact normalized model. Canonical SKU is accepted from `manufacturer_sku`, or from `sku` only when `sku_type=manufacturer`; generic/distributor codes stay in raw evidence. Model/SKU punctuation and case are preserved. Unknown manufacturer/model or explicit identity conflict is quarantined; unknown brands are never merged. Identity conflict can be indicated by `identity_ambiguous=True`, `model_conflict=True`, or `identity_conflict`, `model_conflict`, `brand_conflict` in extraction notes. Explicit `non_ups_category` flags/notes are also quarantined. The importer must also quarantine differing models for an already known manufacturer+SKU key. Brand aliases are not guessed.

The source-observation unique key is `(product_id, source_file_id, source_locator)`. Distinct source price tiers must have distinct column/field locators. Price amounts use exact nonnegative decimal strings, stored as TEXT without float conversion. Currency remains NULL unless explicit and supported. VAT defaults to `unknown`; price type is `source-marked` only when an explicit source price type exists. The precise tier stays in `raw_json`. Dates must be valid ISO calendar dates; no date is inferred from scan time.

Ratings must be finite and positive, otherwise the record is rejected. Unknown topology/form values remain in `raw_json` with NULL normalized references. Seed codes are topology `offline`, `line-interactive`, `online-double-conversion`; form `tower`, `rackmount`, `rack-tower`; currency `THB`, `USD`, `EUR`. Seeds only define vocabulary and never supply a missing source assertion.

`source_files` has a unique relative path, nullable SHA-256, extension, size, status, error, scan timestamp and metadata. A hash is NULL when file bytes could not be read; no dummy digest is invented. Allowed states are `discovered`, `imported`, `partial`, `rejected`, `error`, `unsupported`. Changed-file replacement and transactional import behavior belong to the importer; schema does not overwrite observations automatically.

## Diagram

`ups-database.drawio` is native editable XML showing all business columns and relationships. It contains no customer records or prices. The migration runner's `schema_migrations` metadata table is outside this business diagram.
