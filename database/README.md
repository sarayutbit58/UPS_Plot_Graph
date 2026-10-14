# UPS product database foundation

This is a standalone database layer for UPS Picker. It does not change the browser application or implement selection, ranking, optimization, allocation or application reports.

## Deliverables

- Schema: `schema.sql` (matches `migrations/001_initial.up.sql`).
- Editable ERD: `docs/ups-database.drawio`; table contracts: `docs/schema.md`.
- Migrate/rollback: `manage.py`, `migrations/001_initial.up.sql`, `migrations/001_initial.down.sql`.
- Reference seed: `seeds/001_reference.sql` (classification vocabulary only).
- ETL: `extract.py`, `normalize.py`, `import_catalog.py`; conservative identity reconciliation: `merge_duplicates.py`; unpriced-product pruning: `prune_unpriced.py`; combined runner: `build_database.py`.
- UPS recommendation engine and loopback API: `recommendation_engine.py`, `recommendation_api.py`; scoring and API contract: `recommendation.md`; exact manufacturer runtime source records: `manufacturer_runtime_curves.json`.
- Data validation: `validate.py`; local `validation.json` and `validation.md`.
- Acceptance tests: `tests/`; extraction checks: `test_extract.py`.

## Run

Use Python 3.11+ with SQLite support. On Windows, pass the actual interpreter and source folder as quoted paths:

```powershell
& '<python.exe>' -X utf8 database/build_database.py --source-root '<Pricelist folder>' --output-dir database/local
```

The scan reads sources sequentially and inventories every encountered file, including archive members. It records failures and coverage limits. Outputs are outside the original source tree. The database is `database/local/ups-products.sqlite3`; the build also writes deduplication, unpriced-pruning, and validation reports.

Document readers require `openpyxl`, `pypdf`, `xlrd`, `pyxlsb` and `olefile`; optional image inspection uses Pillow. The extractor also discovers reader packages in `database/local/deps`. Windows image OCR uses the installed Windows English OCR engine, with no model downloads. Other platforms/languages record their coverage limitations. Check `extraction_summary.json` and inventory status rather than assuming full text coverage.

Individual operations (replace `python` with the available interpreter path):

```text
python -X utf8 database/extract.py --source-root "<source folder>" --output-dir database/local
python -X utf8 database/manage.py migrate --db database/local/ups-products.sqlite3
python -X utf8 database/manage.py seed --db database/local/ups-products.sqlite3
python -X utf8 database/import_catalog.py --db database/local/ups-products.sqlite3 --inventory database/local/inventory.jsonl --records database/local/records.jsonl
python -X utf8 database/merge_duplicates.py --db database/local/ups-products.sqlite3 --minimum-score 0.80 --report database/local/deduplication.json --apply
python -X utf8 database/prune_unpriced.py --db database/local/ups-products.sqlite3 --report database/local/unpriced-pruning.json
python -X utf8 database/recommendation_api.py --db database/local/ups-products.sqlite3 --port 8000
python -X utf8 -m unittest database.tests.test_recommendation_engine database.tests.test_recommendation_api -v
python -X utf8 database/validate.py --db database/local/ups-products.sqlite3 --report database/local/validation.json
python -X utf8 database/manage.py rollback --db "<disposable acceptance-test database>"
python -X utf8 -m unittest discover -s database/tests -v
python -X utf8 database/test_extract.py
```

Rollback drops the initial catalog tables and their rows. Run it only against a disposable database or after taking a backup. Migration, rollback, seed and import are transactional. Repeating an identical import preserves row counts. A changed source hash requires a fresh output/database, so historical provenance cannot silently point to replaced content.

## Data rules

Canonical products deduplicate by manufacturer and explicit manufacturer SKU, otherwise by manufacturer, product type, and model after Unicode compatibility normalization, case-folding, and removal of spaces/punctuation. Letters and digits remain intact, so numeric/model suffix changes remain distinct. Manufacturer SKU comparisons preserve exact case and punctuation. After import, the reconciler joins a model-only row to a SKU-backed product only when that normalized identity has exactly one explicit manufacturer SKU, the entire combined evidence cluster has no conflicting non-null specifications, and source locators do not collide. A model-only duplicate can merge with another only when its group has no explicit SKU rows. Any group mapped to multiple SKUs or with conflicting specifications remains separate. `deduplication.md` reports a deterministic eligibility score, not a statistically calibrated probability. Distributor article codes remain source evidence. Ambiguous identity and explicit non-UPS categories are quarantined in `import_rejections`. Specs/prices from separate sources remain separate assertions; their conflicts are reported rather than overwritten.

After deduplication, `prune_unpriced.py` removes each product with no price offer anywhere and its source/specification rows. It retains all source observations for any product that has at least one price offer, even when some of those observations have no price. Source-file inventory and quarantined import rejections remain unchanged.

Prices are exact decimal strings. Currency, VAT, dates and ratings stay unknown when not explicit in the source. Multiple price tiers have separate locators. Product assertions resolve through `product_sources` to source file, hash, and sheet/row/page/line/member locator.

`database/local/` is ignored by Git: databases, inventories, extracted records, caches, validation reports and reader packages may contain confidential partner pricing. Only generic code, schema, reference seeds, tests and ERD belong in version control. The data validation artifact records structural acceptance separately from content coverage and commercial uncertainty. Do not use historical source prices as guaranteed current quotations.

## Latest reconciliation (2026-10-14)

The catalog first reconciled 136 cosmetic model variants, then removed 454 products with no price offers. The resulting database has 1,094 products (817 UPS), 7,506 source assertions/specifications, and all 7,084 price offers, 1,612 source files, and 7,386 rejections. No products without at least one price offer remain. Structural validation passed with SQLite integrity `ok` and no foreign-key violations. See `database/local/deduplication.md`, `database/local/unpriced-pruning.md`, and `database/local/validation.md` for the separate reports.

## UPS recommendations (2026-10-14)

`recommendation_api.py` serves the database-backed Top 5 engine on loopback only. The `/api/recommendations` endpoint ranks using the formula in `recommendation.md`, returns capacity/runtime/outlet/price/expansion scores with source evidence, and marks missing values unknown. It excludes product identities explicitly described as battery accessories before combining source rows, while retaining complete UPS systems sold with external battery packs. It rejects numeric inputs outside the finite JSON range. Reseller cost is the explicit NDP/Dealer source amount with VAT/source metadata; the current catalog has no currency or offer date, so the API does not convert the amount or score price efficiency until comparable currency and VAT data exist. APC runtime overlay entries are exact SKU/model matches to the linked Schneider technical specification.

`allocation_engine.py` adds count-based device, UPS-unit and outlet assignment through `POST /api/allocations`. It supports fixed quantities and auto-added units from a selected mixed-model pool, enforces the existing 20% W/VA margin and a required in-range runtime curve, and returns per-unit utilization, unassigned devices, capacity validation and engineering recommendations. Outlet slots are generic because the catalog does not record receptacle types. See `allocation.md` for the request contract and limits.
