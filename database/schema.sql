PRAGMA foreign_keys = ON;

CREATE TABLE manufacturers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    canonical_key TEXT NOT NULL UNIQUE
);
CREATE TABLE source_files (
    id INTEGER PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE CHECK (length(trim(relative_path)) > 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    status TEXT NOT NULL CHECK (status IN ('discovered','imported','partial','rejected','error','unsupported')),
    error TEXT,
    scanned_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE topology_types (code TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE form_factors (code TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE currencies (code TEXT PRIMARY KEY CHECK (length(code) = 3), label TEXT NOT NULL);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    manufacturer_id INTEGER REFERENCES manufacturers(id),
    identity_key TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    sku TEXT,
    description TEXT,
    product_type TEXT NOT NULL CHECK (product_type IN ('UPS','battery','accessory'))
);
CREATE TABLE product_sources (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    source_locator TEXT NOT NULL CHECK (length(trim(source_locator)) > 0),
    raw_text TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(product_id, source_file_id, source_locator)
);
CREATE TABLE product_specifications (
    product_source_id INTEGER PRIMARY KEY REFERENCES product_sources(id),
    rating_va REAL CHECK (rating_va IS NULL OR rating_va > 0),
    rating_w REAL CHECK (rating_w IS NULL OR rating_w > 0),
    topology TEXT REFERENCES topology_types(code),
    form_factor TEXT REFERENCES form_factors(code)
);
CREATE TABLE price_offers (
    id INTEGER PRIMARY KEY,
    product_source_id INTEGER NOT NULL UNIQUE REFERENCES product_sources(id),
    amount_decimal TEXT NOT NULL CHECK (
        typeof(amount_decimal) = 'text' AND length(amount_decimal) > 0
        AND amount_decimal NOT GLOB '*[^0-9.]*'
        AND amount_decimal GLOB '*[0-9]*'
        AND length(amount_decimal) - length(replace(amount_decimal, '.', '')) <= 1
    ),
    currency TEXT REFERENCES currencies(code),
    vat_treatment TEXT NOT NULL DEFAULT 'unknown' CHECK (vat_treatment IN ('unknown','exclusive','inclusive')),
    price_type TEXT NOT NULL DEFAULT 'unknown' CHECK (price_type IN ('unknown','source-marked')),
    source_date TEXT
);
CREATE TABLE import_rejections (
    id INTEGER PRIMARY KEY,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    source_locator TEXT NOT NULL,
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(source_file_id, source_locator, reason)
);
CREATE INDEX product_sources_file_idx ON product_sources(source_file_id);
CREATE INDEX products_manufacturer_idx ON products(manufacturer_id);
CREATE INDEX import_rejections_file_idx ON import_rejections(source_file_id);
