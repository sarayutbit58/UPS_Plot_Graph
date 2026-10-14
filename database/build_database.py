"""Run the database-only scan, migration, seed, import and validation pipeline."""
import argparse
import json
from pathlib import Path

from extract import extract
from import_catalog import import_catalog
from merge_duplicates import reconcile, render_markdown
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
    merged = reconcile(catalog, minimum_score=0.80, apply=True)
    (output / 'deduplication.json').write_text(json.dumps(merged, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'deduplication.md').write_text(render_markdown(merged), encoding='utf-8')
    report = validate(catalog, output / 'validation.json')
    print(json.dumps({'database': str(catalog), 'scan': scan,
                      'import': imported, 'deduplication': merged['result'],
                      'validation_passed': report['passed']}, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
