"""Run the database-only scan, migration, seed, import and validation pipeline."""
import argparse
import json
from pathlib import Path

from extract import extract
from import_catalog import import_catalog
from validate import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--output-dir', default=str(Path(__file__).parent / 'local'))
    args = parser.parse_args()
    source = Path(args.source_root).resolve()
    output = Path(args.output_dir).resolve()
    if source == output or source in output.parents:
        parser.error('Output must be outside the source tree')
    output.mkdir(parents=True, exist_ok=True)
    scan = extract(source, output)
    catalog = output / 'ups-products.sqlite3'
    imported = import_catalog(catalog, output / 'inventory.jsonl', output / 'records.jsonl')
    report = validate(catalog, output / 'validation.json')
    print(json.dumps({'database': str(catalog), 'scan': scan,
                      'import': imported, 'validation_passed': report['passed']}, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
