"""Conservatively merge product rows that differ only in model presentation."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from manage import connect, transaction
from normalize import model_identity_key


MODEL_ONLY_SCORE = 0.85
UNIQUE_SKU_SCORE = 0.97
SPEC_FIELDS = ('rating_va', 'rating_w', 'topology', 'form_factor')


def _product_rows(connection):
    return connection.execute('''SELECT p.id,p.manufacturer_id,m.canonical_key AS manufacturer_key,
        m.name AS manufacturer,p.model,p.sku,p.product_type
        FROM products p JOIN manufacturers m ON m.id=p.manufacturer_id ORDER BY p.id''').fetchall()


def _specs(connection, product_id):
    rows = connection.execute('''SELECT s.rating_va,s.rating_w,s.topology,s.form_factor
        FROM product_sources ps JOIN product_specifications s ON s.product_source_id=ps.id
        WHERE ps.product_id=?''', (product_id,)).fetchall()
    return {field: {row[field] for row in rows if row[field] is not None} for field in SPEC_FIELDS}


def _spec_conflicts(connection, first_id, second_id):
    return _cluster_spec_conflicts(connection, [first_id], [second_id])


def _cluster_spec_conflicts(connection, first_ids, second_ids):
    def combined(product_ids):
        result = {field: set() for field in SPEC_FIELDS}
        for product_id in product_ids:
            values = _specs(connection, product_id)
            for field in SPEC_FIELDS:
                result[field].update(values[field])
        return result

    first, second = combined(first_ids), combined(second_ids)
    return [field for field in SPEC_FIELDS if len(first[field] | second[field]) > 1]


def _source_locator_collision(connection, first_id, second_id):
    return connection.execute('''SELECT 1 FROM product_sources a JOIN product_sources b
        ON a.source_file_id=b.source_file_id AND a.source_locator=b.source_locator
        WHERE a.product_id=? AND b.product_id=? LIMIT 1''', (first_id, second_id)).fetchone() is not None


def _cluster_locator_collision(connection, cluster_ids, candidate_id):
    return any(_source_locator_collision(connection, product_id, candidate_id) for product_id in cluster_ids)


def plan_merges(connection, minimum_score=0.80):
    """Return safe, explainable merges and skipped candidate groups without writing."""
    products = _product_rows(connection)
    groups = {}
    for product in products:
        key = (product['manufacturer_id'], product['product_type'], model_identity_key(product['model']))
        groups.setdefault(key, []).append(product)

    merges, skipped = [], []
    for (manufacturer_id, product_type, normalized_model), group in groups.items():
        if not normalized_model or len(group) < 2:
            continue
        sku_rows = [product for product in group if product['sku']]
        # Preserve the source SKU spelling; case-only differences may be meaningful.
        sku_keys = {product['sku'].strip() for product in sku_rows}
        no_sku_rows = [product for product in group if not product['sku']]
        candidates = []
        if len(sku_keys) == 1 and sku_rows:
            target = min(sku_rows, key=lambda product: product['id'])
            candidates.extend((product, target, UNIQUE_SKU_SCORE,
                               'same manufacturer, type and alphanumeric model; one explicit manufacturer SKU')
                              for product in no_sku_rows)
        else:
            if len(sku_keys) > 1 and no_sku_rows:
                skipped.append({'product_ids': [product['id'] for product in group],
                                'reason': 'model maps to multiple explicit manufacturer SKUs'})
            # Do not merge any member of an ambiguous multi-SKU group.
            if not sku_rows and len(no_sku_rows) > 1:
                target = min(no_sku_rows, key=lambda product: product['id'])
                candidates.extend((product, target, MODEL_ONLY_SCORE,
                                   'same manufacturer, type and alphanumeric model; neither row has an explicit SKU')
                                  for product in no_sku_rows if product['id'] != target['id'])

        accepted_clusters = {}
        for source, target, rule_score, reason in candidates:
            if rule_score < minimum_score:
                skipped.append({'product_ids': [source['id'], target['id']], 'reason': 'below score threshold'})
                continue
            cluster_ids = accepted_clusters.setdefault(target['id'], [target['id']])
            conflicts = _cluster_spec_conflicts(connection, cluster_ids, [source['id']])
            if conflicts:
                skipped.append({'product_ids': [source['id'], target['id']],
                                'reason': 'conflicting specifications: ' + ', '.join(conflicts)})
                continue
            if _cluster_locator_collision(connection, cluster_ids, source['id']):
                skipped.append({'product_ids': [source['id'], target['id']],
                                'reason': 'same source file and locator on both products'})
                continue
            merges.append({'source_id': source['id'], 'target_id': target['id'],
                           'manufacturer': source['manufacturer'], 'product_type': product_type,
                           'source_model': source['model'], 'target_model': target['model'],
                           'target_sku': target['sku'], 'normalized_model': normalized_model,
                           'rule_score': rule_score, 'reason': reason})
            cluster_ids.append(source['id'])

    return {'minimum_score': minimum_score, 'merges': merges, 'skipped': skipped}


def apply_merges(connection, plan):
    """Move source assertions intact, then delete merged identity rows transactionally."""
    merged_sources = set()
    products_removed = 0
    with transaction(connection):
        fresh_plan = plan_merges(connection, plan['minimum_score'])
        if fresh_plan['merges'] != plan['merges']:
            raise ValueError('Catalog changed since planning; create a fresh merge plan')
        for item in plan['merges']:
            source_id, target_id = item['source_id'], item['target_id']
            if source_id in merged_sources:
                raise ValueError(f'Product {source_id} is scheduled more than once as a source')
            source = connection.execute('SELECT * FROM products WHERE id=?', (source_id,)).fetchone()
            target = connection.execute('SELECT * FROM products WHERE id=?', (target_id,)).fetchone()
            if not source or not target:
                raise ValueError(f'Merge input changed: {source_id} -> {target_id}')
            if (source['manufacturer_id'] != target['manufacturer_id'] or
                    source['product_type'] != target['product_type'] or
                    model_identity_key(source['model']) != model_identity_key(target['model'])):
                raise ValueError(f'Merge identity changed: {source_id} -> {target_id}')
            if _spec_conflicts(connection, source_id, target_id):
                raise ValueError(f'Specifications changed since planning: {source_id} -> {target_id}')
            if _source_locator_collision(connection, source_id, target_id):
                raise ValueError(f'Source locator collision: {source_id} -> {target_id}')
            connection.execute('UPDATE product_sources SET product_id=? WHERE product_id=?', (target_id, source_id))
            connection.execute('DELETE FROM products WHERE id=?', (source_id,))
            merged_sources.add(source_id)
            products_removed += 1
    return {'products_removed': products_removed, 'products_remaining': connection.execute('SELECT count(*) FROM products').fetchone()[0]}


def reconcile(db_path, minimum_score=0.80, apply=False):
    connection = connect(db_path)
    try:
        plan = plan_merges(connection, minimum_score)
        result = apply_merges(connection, plan) if apply else None
        return {'plan': plan, 'result': result}
    finally:
        connection.close()


def render_markdown(output):
    plan = output['plan']
    merges = plan['merges']
    skipped_reasons = {}
    for item in plan['skipped']:
        skipped_reasons[item['reason']] = skipped_reasons.get(item['reason'], 0) + 1
    lines = [
        '# Product model deduplication', '',
        f"Minimum rule score: {plan['minimum_score']:.2f}",
        f"Planned or applied merges: {len(merges)}",
        f"Products removed: {output['result']['products_removed'] if output['result'] else 'not applied'}",
        f"Products remaining: {output['result']['products_remaining'] if output['result'] else 'not applied'}", '',
        '## Matching rule', '',
        'Candidate identity requires the same manufacturer, product type, and the same model after Unicode compatibility normalization, case-folding, and removal of spaces/punctuation. Letters and digits are never changed or removed. A model-only record joins a SKU-backed product only when exactly one explicit manufacturer SKU exists in that group. Non-null specifications must not contradict, and source file/locator pairs must not collide.', '',
        'The score is a deterministic rule score, not a statistically calibrated probability. Model-only duplicates score 0.85; a unique matching manufacturer SKU scores 0.97. Both exceed the requested 0.80 cutoff. Different letters or digits, multiple explicit SKUs, conflicting specifications, or colliding source locators disqualify a merge.', '',
        '## Skipped candidates', '',
    ]
    lines.extend(f'- {count}: {reason}' for reason, count in sorted(skipped_reasons.items()))
    lines.extend(['', 'Similar strings with different letters or digits are not merged based on text similarity alone. This preserves model suffixes, capacities, and voltage variants.', '',
                  '## Approved candidate list', '',
                  '| Source model | Canonical model | Manufacturer SKU | Rule score |',
                  '|---|---|---|---:|'])
    for item in merges:
        lines.append(f"| {item['source_model'].replace('|', '\\|')} | {item['target_model'].replace('|', '\\|')} | {item['target_sku'] or '—'} | {item['rule_score']:.2f} |")
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--minimum-score', type=float, default=0.80)
    parser.add_argument('--apply', action='store_true', help='apply the reviewed merge plan transactionally')
    parser.add_argument('--report', type=Path, help='write the full plan and result as UTF-8 JSON')
    args = parser.parse_args(argv)
    if not 0 <= args.minimum_score <= 1:
        parser.error('--minimum-score must be between 0 and 1')
    try:
        output = reconcile(args.db, args.minimum_score, args.apply)
        encoded = json.dumps(output, ensure_ascii=False, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(encoded + '\n', encoding='utf-8')
            args.report.with_suffix('.md').write_text(render_markdown(output), encoding='utf-8')
        print(json.dumps({'candidate_merges': len(output['plan']['merges']),
                          'skipped_groups': len(output['plan']['skipped']),
                          'applied': args.apply, 'result': output['result']}, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f'merge failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
