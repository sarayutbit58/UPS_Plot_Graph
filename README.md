# UPS Picker — เครื่องคำนวณเลือก UPS

A Thai-language tool for sizing and selecting UPS (Uninterruptible Power Supply) units. It combines a static web calculator frontend with a Python database and decision-engine layer that handles product-catalog ETL, deduplication, validation, and recommendation/allocation/optimization solvers.

## Project Structure

```
.
├── index.html               # Single-page web app (Thai UI, Chart.js, Prompt font)
├── calculations.js          # Browser/Node UMD module: load sizing, power factor, battery Ah/runtime math
├── catalog.js               # UMD module: builds catalog search requests from filters + load (watts, VA, outlets)
├── optimization.js          # UMD module: client for the hosted same-origin optimization API
├── optimization.test.js     # Node test runner tests for the optimization client
├── audit_report.md          # Triple-depth audit report (static analysis, security, functional/logic)
├── styles/                  # Tailwind input and built CSS
├── test/                    # Node:test suites (calculations, catalog, export)
├── tailwind.config.cjs      # Tailwind configuration
├── render.yaml              # Render service definition (cloud_api.py + Turso env vars)
└── database/                # Database & decision-engine layer (see database/README.md)
    ├── schema.sql           # Canonical schema (mirrors migrations/001_initial.up.sql)
    ├── extract.py / normalize.py / import_catalog.py   # ETL pipeline
    ├── merge_duplicates.py / prune_unpriced.py         # Identity reconciliation & pruning
    ├── build_database.py    # Combined runner
    ├── recommendation_engine.py / allocation_engine.py / optimization_engine.py  # Solvers
    ├── recommendation_api.py / cloud_api.py            # Local loopback API / hosted cloud API
    ├── turso_store.py / publish_turso.py               # Optional compact Turso catalog
    ├── manage.py            # Migrate / rollback / seed
    ├── validate.py          # Data validation → validation.json / validation.md
    ├── docs/                # schema.md and editable ERD (ups-database.drawio)
    ├── migrations/ seeds/ tests/
    └── manufacturer_runtime_curves.json                # Exact manufacturer runtime source records
```

## Frontend (JavaScript)

- `index.html` is a self-contained page; core logic lives in three UMD modules (`calculations.js`, `catalog.js`, `optimization.js`) that work both in the browser and under Node for testing.
- `calculations.js` performs device-load aggregation (up to 500 devices), power-factor handling (default 0.8), and exact battery Amp-hour / runtime math with micro-Ah resolution, plus safe JSON import parsing with size limits.
- `catalog.js` validates filter/load inputs and builds paginated catalog search requests.
- `optimization.js` calls the hosted same-origin `/api/optimizations` endpoint (no localhost restriction).

### Build & Test (Node)

Requires Node.js (uses the built-in `node:test` runner).

```bash
npm install --yes            # installs tailwindcss (dev dependency)
npm run build:css            # tailwindcss -i ./styles/tailwind.input.css -o ./styles/tailwind.css --minify
npm test                     # node --test (runs optimization.test.js and test/*.cjs)
```

To view the app locally, serve the repo root with any static file server (e.g. `python3 -m http.server`) so relative asset paths and the same-origin API resolve correctly.

## Database & Decision Engines (Python)

Full details live in [`database/README.md`](database/README.md). Summary:

- Raw manufacturer/distributor source material stays local; outputs go to `database/local/` (gitignored — may contain confidential partner pricing).
- Pipeline: extract → normalize → import → deduplicate → prune unpriced → validate, all transactional.
- Engines: UPS recommendation, outlet allocation, and optimization, exposed via a loopback-only local API (`recommendation_api.py`) or the hosted cloud API (`cloud_api.py`).
- Requires Python 3.11+ with SQLite support; document readers need `openpyxl`, `pypdf`, `xlrd`, `pyxlsb`, `olefile` (optional Pillow).

```bash
# Build the local database from a pricelist folder
python -X utf8 database/build_database.py --source-root "<Pricelist folder>" --output-dir database/local

# Run the local API (loopback only)
python -X utf8 database/recommendation_api.py --db database/local/ups-products.sqlite3 --port 8000

# Python test suites
python -X utf8 -m unittest discover -s database/tests -v
python -X utf8 database/test_extract.py
```

## Deployment (Render + Turso)

`render.yaml` defines a free-tier web service (`ups-team-selector`) that:

- Build step syntax-checks every `database/*.py` file.
- Starts with `python database/cloud_api.py serve`.
- Health check at `/health`.
- Reads secrets from environment variables (do not commit values):
  - `TURSO_DATABASE_URL` — Turso database URL for the published catalog
  - `TURSO_AUTH_TOKEN` — Turso auth token
  - `UPS_USERS_JSON` — hashed user credentials for the API (PBKDF2, 600k iterations)

API routes served by `cloud_api.py`: `/api/recommendations`, `/api/allocations`, `/api/optimizations`, `/api/catalog/search`, plus `/health` and static assets. Requests are host-guarded, authenticated, and throttled; credentials never appear in service logs.

## Security Notes

- The local API binds to loopback and rejects non-loopback `Host`/`Origin` headers.
- Hosted API requires authentication and rate-limits failed attempts.
- `database/local/` is gitignored to keep partner pricing out of version control.
- See `audit_report.md` for the full static/security/logic audit findings.

## License

Private project (`"private": true` in `package.json`). No license file is present in the repository.
