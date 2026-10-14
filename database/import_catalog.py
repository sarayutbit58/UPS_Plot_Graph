"""Import confidential scanner JSONL into the normalized local UPS catalog."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from manage import connect, migrate, seed, transaction
from normalize import normalize_record


def json_lines(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f'{path}: line {number} must be an object')
            yield value


def source_status(value):
    text = str(value or 'discovered').lower()
    if text in {'discovered', 'imported', 'partial', 'rejected', 'error', 'unsupported'}:
        return text
    if 'error' in text or 'unreadable' in text:
        return 'error'
    if 'unsupported' in text or 'no_ocr' in text or 'skipped' in text:
        return 'unsupported'
    if 'partial' in text or 'limit' in text:
        return 'partial'
    if text in {'scanned', 'ok', 'processed', 'no_ups', 'extracted', 'success'}:
        return 'imported'
    return 'partial'


def upsert_source(connection, item):
    relative = str(item.get('relative_path') or item.get('source_path') or '').replace('\\', '/')
    if not relative or relative.startswith('/') or '..' in Path(relative).parts or re.match(r'^[A-Za-z]:', relative):
        raise ValueError('Inventory contains a non-relative source path')
    digest = item.get('sha256') or None
    if digest and not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
        raise ValueError(f'Invalid SHA-256 for {relative}')
    previous = connection.execute('SELECT id, sha256 FROM source_files WHERE relative_path=?', (relative,)).fetchone()
    if previous and previous['sha256'] and digest != previous['sha256']:
        raise ValueError(f'Source changed; import into a fresh database to preserve provenance: {relative}')
    metadata = dict(item)
    connection.execute('''INSERT INTO source_files
        (relative_path,sha256,extension,size_bytes,status,error,scanned_at,metadata_json)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(relative_path) DO UPDATE SET
        sha256=excluded.sha256, status=excluded.status, error=excluded.error,
        scanned_at=excluded.scanned_at, metadata_json=excluded.metadata_json''', (
        relative, digest, item.get('extension') or Path(relative).suffix.lower(),
        int(item.get('size_bytes', item.get('size', 0))), source_status(item.get('status')),
        str(item['error']) if item.get('error') else None,
        item.get('scanned_at') or datetime.now(timezone.utc).isoformat(),
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
    ))
    return connection.execute('SELECT id FROM source_files WHERE relative_path=?', (relative,)).fetchone()[0]


def reject(connection, source_id, record, reason):
    raw_json = record.get('raw_json') or json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    connection.execute('''INSERT OR IGNORE INTO import_rejections
        (source_file_id,source_locator,reason,raw_json) VALUES (?,?,?,?)''',
        (source_id, str(record.get('source_locator') or 'unknown'), reason, raw_json))


def import_catalog(db_path, inventory_path, records_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(db_path)
    try:
        migrate(connection)
        seed(connection)
        totals = {'inventory': 0, 'records': 0, 'accepted': 0, 'duplicates': 0, 'rejected': 0}
        with transaction(connection):
            source_ids = {}
            for item in json_lines(inventory_path):
                relative = str(item.get('relative_path') or item.get('source_path') or '').replace('\\', '/')
                source_ids[relative] = upsert_source(connection, item)
                totals['inventory'] += 1
            for raw in json_lines(records_path):
                totals['records'] += 1
                record = normalize_record(raw)
                source_id = source_ids.get(record['source_path'])
                if source_id is None:
                    raise ValueError(f'Record source missing from inventory: {record["source_path"]}')
                if not record['accepted']:
                    reject(connection, source_id, record, record['reason'])
                    totals['rejected'] += 1
                    continue
                connection.execute('INSERT OR IGNORE INTO manufacturers(name,canonical_key) VALUES (?,?)',
                                   (record['manufacturer'], record['manufacturer_key']))
                manufacturer_id = connection.execute('SELECT id FROM manufacturers WHERE canonical_key=?',
                                                      (record['manufacturer_key'],)).fetchone()[0]
                previous = connection.execute('SELECT * FROM products WHERE identity_key=?',
                                               (record['identity_key'],)).fetchone()
                if previous and (previous['model'] != record['model'] or previous['product_type'] != record['product_type']):
                    reject(connection, source_id, record, 'identity conflict: same key has a different model or product type')
                    totals['rejected'] += 1
                    continue
                connection.execute('''INSERT OR IGNORE INTO products
                    (manufacturer_id,identity_key,model,sku,description,product_type) VALUES (?,?,?,?,?,?)''',
                    (manufacturer_id, record['identity_key'], record['model'], record['sku'],
                     record['description'], record['product_type']))
                product_id = connection.execute('SELECT id FROM products WHERE identity_key=?',
                                                 (record['identity_key'],)).fetchone()[0]
                previous_source = connection.execute('''SELECT id,raw_json FROM product_sources
                    WHERE product_id=? AND source_file_id=? AND source_locator=?''',
                    (product_id, source_id, record['source_locator'])).fetchone()
                if previous_source:
                    if previous_source['raw_json'] == record['raw_json']:
                        totals['duplicates'] += 1
                    else:
                        reject(connection, source_id, record, 'source locator conflict: differing assertions at the same location')
                        totals['rejected'] += 1
                    continue
                source_record_id = connection.execute('''INSERT INTO product_sources
                    (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
                    (product_id, source_id, record['source_locator'], record['raw_text'], record['raw_json'])).lastrowid
                connection.execute('''INSERT INTO product_specifications
                    (product_source_id,rating_va,rating_w,topology,form_factor) VALUES (?,?,?,?,?)''',
                    (source_record_id, record['rating_va'], record['rating_w'], record['topology'], record['form_factor']))
                if record['price_amount'] is not None:
                    connection.execute('''INSERT INTO price_offers
                        (product_source_id,amount_decimal,currency,vat_treatment,price_type,source_date)
                        VALUES (?,?,?,?,?,?)''', (source_record_id, record['price_amount'], record['currency'],
                                              record['vat_treatment'], record['price_type'], record['source_date']))
                totals['accepted'] += 1
        totals.update({table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                       for table in ('products', 'product_sources', 'price_offers', 'import_rejections')})
        return totals
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--records', required=True)
    args = parser.parse_args()
    print(json.dumps(import_catalog(args.db, args.inventory, args.records), ensure_ascii=False))


if __name__ == '__main__':
    main()
