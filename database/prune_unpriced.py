"""Remove products that have no price offer anywhere in the catalog."""

import json
import sqlite3
import sys
from pathlib import Path

from manage import connect, transaction


def prune_unpriced(connection):
    """Delete unpriced product identities and their specs/source links atomically."""
    with transaction(connection):
        product_ids = [row[0] for row in connection.execute('''SELECT p.id FROM products p
            WHERE NOT EXISTS (
                SELECT 1 FROM product_sources ps JOIN price_offers po
                    ON po.product_source_id=ps.id
                WHERE ps.product_id=p.id
            ) ORDER BY p.id''')]
        before = connection.execute('SELECT count(*) FROM products').fetchone()[0]
        product_sources = 0
        specifications = 0
        offers = 0
        if product_ids:
            placeholders = ','.join('?' for _ in product_ids)
            source_filter = f'product_id IN ({placeholders})'
            product_sources = connection.execute(
                f'SELECT count(*) FROM product_sources WHERE {source_filter}', product_ids).fetchone()[0]
            offers = connection.execute(f'''SELECT count(*) FROM price_offers po
                JOIN product_sources ps ON ps.id=po.product_source_id WHERE {source_filter}''', product_ids).fetchone()[0]
            if offers:
                raise ValueError('Refusing to remove a product source that has a price offer')
            specifications = connection.execute(f'''DELETE FROM product_specifications
                WHERE product_source_id IN (SELECT id FROM product_sources WHERE {source_filter})''', product_ids).rowcount
            connection.execute(f'DELETE FROM product_sources WHERE {source_filter}', product_ids)
            connection.execute(f'DELETE FROM products WHERE id IN ({placeholders})', product_ids)
        after = connection.execute('SELECT count(*) FROM products').fetchone()[0]
        return {
            'products_before': before,
            'products_removed': len(product_ids),
            'products_remaining': after,
            'product_sources_removed': product_sources,
            'specifications_removed': specifications,
            'price_offers_removed': offers,
        }


def prune_unpriced_database(db_path):
    connection = connect(db_path)
    try:
        return prune_unpriced(connection)
    finally:
        connection.close()


def render_markdown(result):
    return '\n'.join([
        '# Unpriced product removal', '',
        f"Products before: {result['products_before']}",
        f"Products removed (no price offers): {result['products_removed']}",
        f"Products remaining: {result['products_remaining']}",
        f"Product source assertions removed: {result['product_sources_removed']}",
        f"Specification rows removed: {result['specifications_removed']}",
        f"Price offers removed: {result['price_offers_removed']}", '',
        'A product is retained when any source assertion for it has at least one price offer. All source assertions and specifications for retained products remain, including observations that do not themselves contain a price. Source-file inventory and quarantined import rejections are not product rows and remain available for provenance and coverage reporting.', '',
    ])


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    connection = connect(args.db)
    try:
        result = prune_unpriced(connection)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            args.report.with_suffix('.md').write_text(render_markdown(result), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f'prune failed: {error}', file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
