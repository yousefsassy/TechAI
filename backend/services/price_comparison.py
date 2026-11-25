"""Group retailer observations without treating a retailer SKU as a global identifier."""
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal

from scraping.normalize import plain


COLORS = {'black': 'black', 'noir': 'black', 'noire': 'black', 'white': 'white', 'blanc': 'white',
          'blanche': 'white', 'rouge': 'red', 'red': 'red', 'blue': 'blue', 'bleu': 'blue',
          'rose': 'pink', 'pink': 'pink', 'gris': 'gray', 'gray': 'gray', 'grey': 'gray'}


RULE_VERSION = 2
GTIN_FIELDS = ('gtin14', 'gtin13', 'gtin12', 'gtin8', 'gtin')
OFFER_FIELDS = ('id', 'supplier', 'price_tnd', 'stock_status', 'product_url', 'scraped_at')
VARIANT_FIELDS = ('brand', 'color', 'ram', 'storage', 'gpu', 'pack_size', 'edition')
PACK = re.compile(r'\b(?:packs?|lots?|bundles?|kits?)\s+(?:of |de )?(\d+)\b|\b(\d+)\s*(?:pieces?|unites?|units?)\b')


def _gtin(metadata):
    for key in GTIN_FIELDS:
        digits = str(metadata.get(key) or '').strip()
        if len(digits) not in (8, 12, 13, 14) or not digits.isascii() or not digits.isdigit() or len(set(digits)) == 1:
            continue
        checksum = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(reversed(digits)))
        if checksum % 10 == 0:
            return digits.zfill(14)
    return None


def _normalized(value):
    return re.sub(r'[^\w]+', '', plain(str(value or '')))


def _identifier(value):
    return 3 <= len(value) <= 80 and any(c.isalpha() for c in value) and any(c.isdigit() for c in value)


def identity(product, metadata, product_id):
    """Collect independent identifiers and evidence; every match checks configuration."""
    metadata = metadata if isinstance(metadata, dict) else {}
    gtin = _gtin(metadata)
    brand = _normalized(product.get('brand'))
    mpn = _normalized(metadata.get('mpn'))
    model = _normalized(product.get('model'))
    anchors = []
    if gtin:
        anchors.append(('gtin', gtin))
    if brand and 4 <= len(mpn) <= 80 and any(c.isdigit() for c in mpn) and len(set(mpn)) > 1:
        anchors.append(('mpn', brand, mpn))
    if not anchors and (not brand or not _identifier(model)):
        return (('catalog', product_id),), None, {}
    name = plain(product['name'])
    pack = PACK.search(name)
    edition = re.search(r'\b(?:ipad|iphone|airpods)\b.{0,24}\b(pro max|pro|ultra|plus|mini|air|max)\b', name)
    color_words = re.findall(r'\w+', str(metadata.get('color') or '').lower())
    if not any(word in COLORS for word in color_words):
        color_words = re.findall(r'\w+', name) if any(color in name for color in COLORS) else []
    colors = tuple(sorted({COLORS[w] for w in color_words if w in COLORS}))
    storage = product.get('storage_gb')
    if not storage and product.get('category') in ('laptop', 'desktop', 'tablet', 'discovered'):
        capacities = re.findall(r'\b(\d+)\s*(gb|go|tb|to)\b', name)
        sizable = {int(size) * (1024 if unit in ('tb', 'to') else 1) for size, unit in capacities
                   if unit in ('tb', 'to') or int(size) >= 64}
        storage = next(iter(sizable)) if len(sizable) == 1 else None
    graphics = _normalized(product.get('graphics')).removeprefix('geforce').removeprefix('radeon')
    pack_size = int(pack[1] or pack[2]) if pack else (
        'bundle' if re.search(r'\b(?:packs?|bundles?|kits?|lots?)\b', name) else 1
    )
    variant = dict(zip(VARIANT_FIELDS, (brand or None, colors or None, product.get('ram_gb'), storage,
                                       graphics or None, pack_size,
                                       edition[1] if edition else None)))
    if brand and _identifier(model) and any(variant[key] for key in ('color', 'ram', 'storage', 'gpu', 'edition')):
        anchors.append(('model-title', brand, model, ' '.join(re.findall(r'\w+', name))))
    anchors.append(('catalog', product_id))
    return tuple(anchors), gtin, variant


ANCHOR_PRIORITY = {'gtin': 0, 'mpn': 1, 'model-title': 2, 'catalog': 3}


@dataclass(eq=False)
class _Group:
    anchor: tuple
    gtin: str | None
    variant: dict
    rows: list = field(default_factory=list)

    def compatible(self, gtin, variant):
        return not (gtin and self.gtin and self.gtin != gtin) and all(
            value is None or self.variant.get(key) in (None, value) for key, value in variant.items()
        )

    def add(self, anchor, gtin, variant, row):
        if ANCHOR_PRIORITY[anchor[0]] < ANCHOR_PRIORITY[self.anchor[0]]:
            self.anchor = anchor
        self.gtin = self.gtin or gtin
        self.variant.update((key, value) for key, value in variant.items() if value is not None)
        self.rows.append(row)

    def best(self):
        return min(self.rows, key=lambda row: (row['price_tnd'], row['id']))

    def card(self):
        self.rows.sort(key=lambda row: (row['price_tnd'], row['id']))
        cheapest = self.rows[0]
        offers = []
        best_price = Decimal(cheapest['price_tnd'])
        for row in self.rows:
            offer = {field: row[field] for field in OFFER_FIELDS}
            offer['price_tnd'] = str(row['price_tnd'])
            offer['scraped_at'] = row['scraped_at'].isoformat()
            offer['difference_tnd'] = str(Decimal(row['price_tnd']) - best_price)
            offers.append(offer)
        public = {key: value for key, value in cheapest.items() if key != '_identity'}
        fingerprint = repr((RULE_VERSION, self.anchor, tuple(self.variant.get(key) for key in VARIANT_FIELDS) if self.anchor[0] != 'catalog' else ()))
        return dict(public, offers=offers, group_id=hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
                    match_reason=self.anchor[0], match_rule_version=RULE_VERSION)


def comparison_groups(rows):
    """Group compatible rows in deterministic order, withholding ambiguous matches."""
    indices = {}
    groups = []
    ordered = sorted(rows, key=lambda row: (
        ANCHOR_PRIORITY[row['_identity'][0][0][0]],
        -sum(value is not None for value in row['_identity'][2].values()), row['id']
    ))
    for row in ordered:
        anchors, gtin, variant = row['_identity']
        group = None
        index_anchors = anchors
        for anchor in anchors:
            matches = [g for g in indices.get(anchor, ()) if g.compatible(gtin, variant)]
            if matches:
                if len(matches) == 1:
                    group = matches[0]
                    group.add(anchors[0], gtin, variant, row)
                else:
                    index_anchors = anchors[-1:]
                break
        if group is None:
            group = _Group(index_anchors[0], gtin, variant, [row])
            groups.append(group)
        for anchor in index_anchors:
            bucket = indices.setdefault(anchor, set())
            bucket.add(group)
    return groups


def mark_verified(cards, urls):
    for card in cards:
        for row in [card, *card.get('offers', [])]:
            row['verified_this_turn'] = row['product_url'] in urls
    return cards


def recommendation_evidence(product, detail=None):
    if detail is None:
        detail = plain(' '.join([product['name'], product['description'], str(product['specifications'])]))
    facts = []
    if re.search(r'\b(?:mouse|souris)\b', detail):
        for pattern in (r'\b\d{3,5}\s*(?:dpi|ppp)\b', r'\b\d{3,4}\s*hz\b',
                        r'\b(?:boutons? programmables?|programmable buttons?)\b', r'capteur optique|optical sensor'):
            if match := re.search(pattern, detail):
                facts.append(match[0])
    return facts
