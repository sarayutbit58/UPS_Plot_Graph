"""Generate the required local data validation evidence (not an application reporting layer)."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from manage import connect, applied_version


def validate(db_path, report_path=None):
    connection = connect(db_path)
    try:
        integrity = [row[0] for row in connection.execute('PRAGMA integrity_check')]
        foreign_keys = [list(row) for row in connection.execute('PRAGMA foreign_key_check')]
        counts = {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ('manufacturers', 'products', 'product_sources', 'product_specifications',
                                'price_offers', 'source_files', 'import_rejections', 'topology_types', 'form_factors', 'currencies')}
        source_statuses = dict(connection.execute('SELECT status,count(*) FROM source_files GROUP BY status'))
        source_issues = [dict(row) for row in connection.execute('''SELECT relative_path,status,error
            FROM source_files WHERE status IN ('partial','error','unsupported','rejected','discovered') ORDER BY relative_path''')]
        invalid_prices = []
        for row in connection.execute('SELECT id,amount_decimal FROM price_offers'):
            try:
                number = Decimal(row['amount_decimal'])
                if not number.is_finite() or number < 0:
                    invalid_prices.append(row['id'])
            except InvalidOperation:
                invalid_prices.append(row['id'])
        capacity_conflicts = [dict(row) for row in connection.execute('''SELECT p.id,p.model,
            count(DISTINCT s.rating_va) AS distinct_va,count(DISTINCT s.rating_w) AS distinct_w
            FROM products p JOIN product_sources ps ON ps.product_id=p.id
            JOIN product_specifications s ON s.product_source_id=ps.id GROUP BY p.id
            HAVING count(DISTINCT s.rating_va)>1 OR count(DISTINCT s.rating_w)>1''')]
        missing_specs = connection.execute('''SELECT count(*) FROM product_specifications
            WHERE rating_va IS NULL AND rating_w IS NULL''').fetchone()[0]
        unknown_currency = connection.execute('SELECT count(*) FROM price_offers WHERE currency IS NULL').fetchone()[0]
        unknown_vat = connection.execute("SELECT count(*) FROM price_offers WHERE vat_treatment='unknown'").fetchone()[0]
        ups_count = connection.execute("SELECT count(*) FROM products WHERE product_type='UPS'").fetchone()[0]
        errors = []
        if integrity != ['ok']: errors.append('SQLite integrity check failed')
        if foreign_keys: errors.append('Foreign-key violations')
        if applied_version(connection) != 1: errors.append('Initial migration missing')
        if not all(counts[table] > 0 for table in ('topology_types', 'form_factors', 'currencies')):
            errors.append('Reference seed missing')
        if ups_count == 0: errors.append('No identifiable UPS products imported')
        if invalid_prices: errors.append('Invalid decimal prices')
        if counts['product_sources'] != counts['product_specifications']: errors.append('Source/spec row mismatch')
        rejections = dict(connection.execute('SELECT reason,count(*) FROM import_rejections GROUP BY reason'))
        report = dict(generated_at=datetime.now(timezone.utc).isoformat(), passed=not errors,
                      errors=errors, integrity=integrity, foreign_key_violations=foreign_keys,
                      counts=counts, ups_products=ups_count, source_statuses=source_statuses,
                      content_coverage_complete=not source_issues, source_issues=source_issues,
                      rejected_reasons=rejections, missing_capacity_observations=missing_specs,
                      unknown_currency_offers=unknown_currency, unknown_vat_offers=unknown_vat,
                      capacity_conflicts=capacity_conflicts, invalid_price_ids=invalid_prices,
                      limitations=['Source prices are historical observations, not guaranteed current quotations.',
                                   'Unknown fields remain null; conflicting assertions are retained for review.',
                                   'Coverage completeness is separate from structural database acceptance.'])
        if report_path:
            target = Path(report_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            lines = ['# UPS Database Data Validation', '', f'Generated: {report["generated_at"]}', '',
                     f'Structural acceptance: {"PASS" if report["passed"] else "FAIL"}',
                     f'Content coverage complete: {report["content_coverage_complete"]}', '',
                     '| Table | Rows |', '|---|---:|']
            lines.extend(f'| {name} | {count} |' for name, count in counts.items())
            lines.extend(['', f'UPS products: {ups_count}', f'Unknown-currency offers: {unknown_currency}',
                          f'Unknown-VAT offers: {unknown_vat}', f'Capacity conflicts: {len(capacity_conflicts)}',
                          '', '## Source coverage issues', ''])
            lines.extend(f'- {row["relative_path"]}: {row["status"]}; {row["error"] or "see inventory metadata"}' for row in source_issues)
            lines.extend(['', '## Rejected records', ''])
            lines.extend(f'- {reason}: {count}' for reason, count in rejections.items())
            lines.extend(['', '## Limits', ''] + [f'- {value}' for value in report['limitations']])
            target.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return report
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report = validate(args.db, args.report)
    print(json.dumps({key: report[key] for key in ('passed','counts','ups_products','source_statuses','errors')}, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
