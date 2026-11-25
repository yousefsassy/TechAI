"""An external-content SQLite FTS index, maintained in the product write transaction."""
from functools import lru_cache

from sqlalchemy import text

from backend.services.shopping_terms import EQUIVALENTS
from scraping.normalize import plain


def initialize(connection):
    connection.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS product_titles USING fts5(name, content='products', content_rowid='id')")
    if connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='product_titles_insert'").first():
        return
    for event, body in {
        'insert': "INSERT INTO product_titles(rowid,name) VALUES(new.id,new.name)",
        'delete': "INSERT INTO product_titles(product_titles,rowid,name) VALUES('delete',old.id,old.name)",
        'update': "INSERT INTO product_titles(product_titles,rowid,name) VALUES('delete',old.id,old.name); INSERT INTO product_titles(rowid,name) VALUES(new.id,new.name)",
    }.items():
        connection.exec_driver_sql(f'CREATE TRIGGER IF NOT EXISTS product_titles_{event} AFTER {event} ON products BEGIN {body}; END')
    connection.exec_driver_sql("INSERT INTO product_titles(product_titles) VALUES('rebuild')")


@lru_cache(maxsize=512)
def _title_pattern(terms):
    # Use prefixes for the same plural handling as the final product-type matcher.
    variants = set()
    arabic = False
    for term in terms:
        word = plain(term)
        equivalents = next((group for group in EQUIVALENTS if word in group), (word,))
        for alias in equivalents:
            words = alias.split()
            first = words[0] if words else ''
            if not first.isalnum():
                return '', False
            variants.add('"' + (first.rstrip('s') or first) + '"*')
            arabic |= any('\u0600' <= char <= '\u06ff' for char in first)
    return ' OR '.join(sorted(variants)), arabic


def title_candidates(query, terms):
    pattern, arabic = _title_pattern(tuple(terms))
    if not pattern:
        return query
    candidates = 'SELECT rowid FROM product_titles WHERE product_titles MATCH :titles'
    if arabic:
        # SQLite FTS removes Latin accents but can split Arabic titles on
        # vowel marks. The final type matcher applies Arabic normalization.
        candidates += " UNION SELECT id FROM products WHERE name GLOB '*[؀-ۿ]*'"
    return query.where(text(f'supplier_offers.product_id IN ({candidates})').bindparams(titles=pattern))
