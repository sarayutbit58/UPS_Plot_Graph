"""Append manufacturer-verified source products to a new private snapshot.

This tool never publishes or modifies its inputs. Raw source rows are isolated
by product ID before projection, so historical alias offers are not merged.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

try:
    from .published_catalog import FORMAT, SCHEMA, project_model
    from .publish_turso import BASE_SNAPSHOT_KEY, snapshot_fingerprint
    from .recommendation_engine import _build_inventory
    from .stage_verified_catalog import (_read_snapshot, _reject_constant, _same_created_file,
                                         _validate_manifest_privacy, _validate_records)
    from .verified_evidence import _key, _product_key, _source, apply_verified_evidence
except ImportError:
    from published_catalog import FORMAT, SCHEMA, project_model
    from publish_turso import BASE_SNAPSHOT_KEY, snapshot_fingerprint
    from recommendation_engine import _build_inventory
    from stage_verified_catalog import (_read_snapshot, _reject_constant, _same_created_file,
                                        _validate_manifest_privacy, _validate_records)
    from verified_evidence import _key, _product_key, _source, apply_verified_evidence


MAX_MANIFEST_BYTES = 8_000_000


def _token(value):
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _qualified_token(manufacturer, value):
    brand, identifier = _token(manufacturer), _token(value)
    return (brand, identifier) if brand and identifier else None


def _sku_tokens(product):
    brand = product.get('manufacturer')
    return {item for item in (_qualified_token(brand, product.get('sku')),
                              _qualified_token(brand, product.get('manufacturer_sku'))) if item}


def _identity_tokens(product):
    identifiers = _sku_tokens(product)
    if not _token(product.get('sku')):
        model_only = _qualified_token(product.get('manufacturer'), product.get('model'))
        if model_only:
            identifiers.add(model_only)
    return identifiers


def _record_tokens(record):
    brand = record.get('manufacturer')
    return {item for item in (_qualified_token(brand, record.get('catalogSku')),
                              _qualified_token(brand, record.get('manufacturerSku'))) if item}


def _snapshot_rows(path, metadata, products, expected_fingerprint):
    """Retrieve existing raw rows exactly, verifying the validated snapshot is unchanged."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        current_meta = dict(db.execute('SELECT key,value FROM catalog_meta'))
        rows = db.execute('SELECT product_id,manufacturer,model,sku,rating_w,rating_va,'
                          'outlet_count,model_json FROM published_models ORDER BY product_id').fetchall()
        if (current_meta != metadata or len(rows) != len(products)
                or snapshot_fingerprint(list(current_meta.items()), rows) != expected_fingerprint):
            raise ValueError('Snapshot changed during validation')
        for row, product in zip(rows, products):
            columns = ('id', 'manufacturer', 'model', 'sku', 'rating_w', 'rating_va', 'outlet_count')
            if (any(field not in product or product[field] != row[index]
                    for index, field in enumerate(columns)) or
                    json.loads(row[-1], parse_constant=_reject_constant) != product):
                raise ValueError('Snapshot changed during validation')
        db.rollback()
        return rows


def _source_connection(path):
    """Expose one selected source product through views, with no copied raw rows."""
    db = sqlite3.connect(':memory:', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('ATTACH DATABASE ? AS raw', (path.as_uri() + '?mode=ro',))
        db.execute('CREATE TEMP TABLE selected_ids(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TEMP VIEW products AS SELECT p.* FROM raw.products p '
                   'JOIN selected_ids i ON i.id=p.id')
        for table in ('manufacturers', 'product_sources', 'source_files',
                      'product_specifications', 'price_offers'):
            db.execute('CREATE TEMP VIEW ' + table + ' AS SELECT * FROM raw.' + table)
        return db
    except BaseException:
        db.close()
        raise


def _source_index(db):
    index = defaultdict(list)
    for row in db.execute('SELECT p.id,p.model,p.sku,p.product_type,m.name AS manufacturer '
                          'FROM raw.products p LEFT JOIN raw.manufacturers m ON m.id=p.manufacturer_id'):
        product = dict(row)
        key = _product_key(product)
        if key is not None:
            index[key].append(product)
    return index


def _ready(product, record):
    if (record.get('category') != 'UPS' or product.get('product_type') != 'UPS'
            or product.get('product_category') != 'UPS' or product.get('non_ups_reason')):
        return False
    maker = product.get('manufacturer')
    return (product.get('identity_verified') is True
            and product.get('manufacturer_sku') == record.get('manufacturerSku')
            and bool(_source(product.get('identity_source'), maker))
            and all(product.get(field + '_verified') is True
                    and bool(_source(product.get(field + '_source'), maker))
                    for field in ('rating_w', 'rating_va')))


def _new_metadata(metadata, existing, appended):
    result = dict(metadata)
    for name in ('original_model_count', 'omitted_missing_capacity_count'):
        if name in result:
            result.setdefault('historical_' + name, result.pop(name))
    products = existing + appended
    result.update(format=FORMAT, generated_at=datetime.now(timezone.utc).isoformat(),
                  model_count=str(len(products)),
                  published_missing_capacity_count=str(sum(
                      not (item.get('rating_w') and item.get('rating_va')) for item in products)),
                  append_added_count=str(len(appended)))
    return result


def _create_output(output, rows, metadata):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(str(output), flags, 0o600)
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)
    try:
        with closing(sqlite3.connect(output)) as db:
            if not _same_created_file(output, identity):
                raise ValueError('Output was replaced during staging')
            db.executescript(SCHEMA)
            with db:
                db.executemany('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)', rows)
                db.executemany('INSERT INTO catalog_meta VALUES(?,?)', metadata.items())
        if not _same_created_file(output, identity):
            raise ValueError('Output was replaced during staging')
    except BaseException:
        if _same_created_file(output, identity):
            try:
                output.unlink()
            except OSError:
                pass
        raise


def append_catalog(snapshot_path, source_path, output_path, records):
    """Create a distinct v2 snapshot with only eligible, unambiguous additions."""
    try:
        snapshot = Path(snapshot_path).resolve(strict=True)
        source = Path(source_path).resolve(strict=True)
        requested = Path(output_path)
        if requested.name in {'', '.', '..'}:
            raise ValueError('Output must name a new file')
        output = requested.parent.resolve(strict=True) / requested.name
        if snapshot == source or output in (snapshot, source):
            raise ValueError('Input and output must be distinct files')
        if os.path.lexists(output):
            raise FileExistsError('Output already exists')
        if not isinstance(records, list):
            raise ValueError('Evidence records must be a list')
        _validate_manifest_privacy(records)
        metadata, existing, fingerprint = _read_snapshot(snapshot, with_fingerprint=True)
        if metadata.get('format') != FORMAT or metadata.get('includes_prices') != 'true':
            raise ValueError('A private v2 snapshot containing projected prices is required')
        old_rows = _snapshot_rows(snapshot, metadata, existing, fingerprint)
        base = metadata.get(BASE_SNAPSHOT_KEY, fingerprint)
        if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{64}', base):
            raise ValueError('Invalid catalog base fingerprint')
        metadata[BASE_SNAPSHOT_KEY] = base

        existing_ids = {product['id'] for product in existing}
        existing_tokens = set().union(*(_identity_tokens(product) for product in existing)) if existing else set()
        existing_skus = set().union(*(_sku_tokens(product) for product in existing)) if existing else set()
        token_counts = Counter(token for record in records if isinstance(record, dict)
                               for token in _record_tokens(record))
        key_counts = Counter(key for record in records if (key := _key(record)) is not None)
        skipped = Counter()
        additions = []
        additions_rows = []
        with closing(_source_connection(source)) as db:
            db.execute('BEGIN')
            source_index = _source_index(db)
            for record in records:
                key = _key(record)
                if key is None:
                    skipped['invalid_evidence'] += 1
                    continue
                tokens = _record_tokens(record)
                if key_counts[key] > 1 or any(token_counts[token] > 1 for token in tokens):
                    skipped['duplicate_request_identity'] += 1
                    continue
                matches = source_index.get(key, [])
                if len(matches) != 1:
                    skipped['ambiguous_source_identity' if matches else 'source_identity_missing'] += 1
                    continue
                match = matches[0]
                model_alias = _qualified_token(match.get('manufacturer'), match.get('model'))
                if (match['id'] in existing_ids or tokens.intersection(existing_tokens)
                        or model_alias in existing_skus):
                    skipped['already_published_or_alias'] += 1
                    continue
                if match.get('product_type') != 'UPS' or record.get('category') != 'UPS':
                    skipped['not_ups'] += 1
                    continue
                db.execute('DELETE FROM selected_ids')
                db.execute('INSERT INTO selected_ids VALUES(?)', (match['id'],))
                products = _build_inventory(db)
                if len(products) != 1 or products[0].get('id') != match['id']:
                    skipped['ambiguous_source_identity'] += 1
                    continue
                original = products[0]
                try:
                    _validate_records([original], [record])
                except ValueError:
                    skipped['invalid_evidence'] += 1
                    continue
                product = apply_verified_evidence([original], [record])[0]
                if not _ready(product, record):
                    skipped['not_verified_ups'] += 1
                    continue
                projected = project_model(product, include_prices=True)
                if not any(isinstance(offer, dict) and offer.get('kind') in ('Dealer', 'NDP')
                           for offer in projected.get('prices', [])):
                    skipped['no_projected_price'] += 1
                    continue
                payload = json.dumps(projected, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                additions.append(projected)
                additions_rows.append((projected['id'], projected.get('manufacturer'), projected.get('model'),
                                       projected.get('sku'), projected.get('rating_w'), projected.get('rating_va'),
                                       projected.get('outlet_count'), payload))
                existing_ids.add(projected['id'])
                existing_tokens.update(_identity_tokens(projected))
                existing_skus.update(_sku_tokens(projected))

        new_metadata = _new_metadata(metadata, existing, additions)
        _create_output(output, old_rows + additions_rows, new_metadata)
        return {'models': len(old_rows) + len(additions_rows), 'added': len(additions_rows),
                'skipped': dict(sorted(skipped.items()))}
    except FileExistsError:
        raise FileExistsError('Output already exists') from None
    except (OSError, sqlite3.Error, RecursionError, UnicodeError):
        raise ValueError('Catalog append failed; check private inputs and output') from None


def _read_manifest(path):
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Evidence manifest exceeds size limit')
    content = path.read_text(encoding='utf-8')
    try:
        manifest = json.loads(content, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        manifest = [json.loads(line, parse_constant=_reject_constant)
                    for line in content.splitlines() if line.strip()]
    if isinstance(manifest, dict) and manifest.get('version') == 1:
        manifest = manifest.get('records')
    elif isinstance(manifest, dict):
        manifest = [manifest]
    if not isinstance(manifest, list):
        raise ValueError('Evidence records must be a list')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = append_catalog(args.snapshot, args.source_db, args.output, _read_manifest(args.manifest))
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        parser.exit(1, 'Catalog append failed; check the private inputs and output.\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
