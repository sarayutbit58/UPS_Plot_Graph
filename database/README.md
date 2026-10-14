# UPS product database foundation

This is a standalone database layer for UPS Picker. It does not change the browser application or implement selection, ranking, optimization, allocation or application reports.

## Deliverables

- Schema: `schema.sql` (matches `migrations/001_initial.up.sql`).
- Editable ERD: `docs/ups-database.drawio`; table contracts: `docs/schema.md`.
- Migrate/rollback: `manage.py`, `migrations/001_initial.up.sql`, `migrations/001_initial.down.sql`.
- Reference seed: `seeds/001_reference.sql` (classification vocabulary only).
- ETL: `extract.py`, `normalize.py`, `import_catalog.py`; combined runner: `build_database.py`.
- Data validation: `validate.py`; local `validation.json` and `validation.md`.
- Acceptance tests: `tests/`; extraction checks: `test_extract.py`.

## Run

Use Python 3.11+ with SQLite support. On Windows, pass the actual interpreter and source folder as quoted paths:

```powershell
& '<python.exe>' -X utf8 database/build_database.py --source-root '<Pricelist folder>' --output-dir database/local
```

The scan reads sources sequentially and inventories every encountered file, including archive members. It records failures and coverage limits. Outputs are outside the original source tree. The database is `database/local/ups-products.sqlite3`.

Document readers require `openpyxl`, `pypdf`, `xlrd`, `pyxlsb` and `olefile`; optional image inspection uses Pillow. The extractor also discovers reader packages in `database/local/deps`. Windows image OCR uses the installed Windows English OCR engine, with no model downloads. Other platforms/languages record their coverage limitations. Check `extraction_summary.json` and inventory status rather than assuming full text coverage.

Individual operations (replace `python` with the available interpreter path):

```text
python -X utf8 database/extract.py --source-root "<source folder>" --output-dir database/local
python -X utf8 database/manage.py migrate --db database/local/ups-products.sqlite3
python -X utf8 database/manage.py seed --db database/local/ups-products.sqlite3
python -X utf8 database/import_catalog.py --db database/local/ups-products.sqlite3 --inventory database/local/inventory.jsonl --records database/local/records.jsonl
python -X utf8 database/validate.py --db database/local/ups-products.sqlite3 --report database/local/validation.json
python -X utf8 database/manage.py rollback --db "<disposable acceptance-test database>"
python -X utf8 -m unittest discover -s database/tests -v
python -X utf8 database/test_extract.py
```

Rollback drops the initial catalog tables and their rows. Run it only against a disposable database or after taking a backup. Migration, rollback, seed and import are transactional. Repeating an identical import preserves row counts. A changed source hash requires a fresh output/database, so historical provenance cannot silently point to replaced content.

## Data rules

Canonical products deduplicate by manufacturer and explicit manufacturer SKU, otherwise by manufacturer/model. Distributor article codes remain source evidence. Ambiguous identity and explicit non-UPS categories are quarantined in `import_rejections`. Specs/prices from separate sources remain separate assertions; their conflicts are reported rather than overwritten.

Prices are exact decimal strings. Currency, VAT, dates and ratings stay unknown when not explicit in the source. Multiple price tiers have separate locators. Product assertions resolve through `product_sources` to source file, hash, and sheet/row/page/line/member locator.

`database/local/` is ignored by Git: databases, inventories, extracted records, caches, validation reports and reader packages may contain confidential partner pricing. Only generic code, schema, reference seeds, tests and ERD belong in version control. The data validation artifact records structural acceptance separately from content coverage and commercial uncertainty. Do not use historical source prices as guaranteed current quotations.
