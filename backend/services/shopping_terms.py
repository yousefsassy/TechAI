"""Catalog vocabulary used for offline search and to ground product switches."""
import re
from functools import lru_cache

from scraping.normalize import plain


# Retailer titles are mostly French. Each group represents interchangeable search terms.
EQUIVALENTS = (
    ('headset', 'headsets', 'headphone', 'headphones', 'casque', 'casques'),
    ('keyboard', 'keyboards', 'clavier', 'claviers'),
    ('laptop', 'laptops', 'notebook', 'pc portable', 'ordinateur portable'),
    ('mouse', 'mice', 'mouses', 'souris', 'فارة', 'ماوس'),
    ('ipad', 'ipads', 'ايباد'),
    ('monitor', 'monitors', 'moniteur', 'ecran', 'شاشة'),
    ('printer', 'printers', 'imprimante', 'طابعة'),
    ('webcam', 'webcams', 'كاميرا ويب'),
    ('chair', 'chairs', 'chaise', 'كرسي'),
    ('router', 'routers', 'routeur', 'راوتر'),
    ('speaker', 'speakers', 'enceinte', 'مكبر صوت'),
    ('stand', 'stands', 'support', 'حامل'),
    ('charger', 'chargers', 'chargeur', 'شاحن'),
    ('case', 'cases', 'coque', 'housse', 'غطاء'),
    ('lamp', 'lamps', 'lampe', 'مصباح'),
)
BRAND_ALIASES = {
    'لوجيتك': 'logitech', 'لوجيتش': 'logitech', 'لوجيتيك': 'logitech',
    'سامسونج': 'samsung', 'لينوفو': 'lenovo', 'ابل': 'apple',
}
FEATURES = (
    ('wireless', 'sans fil', 'bluetooth', 'لاسلكي', 'لاسلكية'),
    ('wired', 'filaire', 'سلكي', 'سلكية'),
    ('rgb', 'r g b', 'argb', 'rvb', 'eclairage multicolore', 'multicolor lighting', 'rgb lighting', 'eclairage rgb', 'retro eclairage rgb', 'led rgb', 'rgb leds'),
    ('gaming', 'gamer', 'للالعاب', 'العاب'),
    ('mechanical', 'mecanique', 'ميكانيكية'),
)
IGNORED = set("""
    now next instead actually also please no yes without not sans sauf and or the a an i im m d would like
    want need get can could you your my make any some looking for to find recommend recommendations
    give show me something that has in them good great best available buy buying shopping
    what about how do have is there another different suggest switch lets let s see under below up less than then with of at most maximum max budget
    tnd dt dinar dinars range price prices within around options products product listings
    ones one cheaper cheapest those these it they more only same expensive powerful
    je j voudrais veux cherche recherche des du de un une le la les moi montre montrez
    propose proposez bon bonne meilleur meilleure en et pour avec sous moins cher chere
    maintenant plutot tu peux avez vous est ce que disponible jusqu a prix
    اريد ابحث عن اعرض لي اظهر الان هل عندك من جيد جيدة ممتاز ممتازة افضل بميزانية
    بسعر تحت اقل دينار تونسي فقط حدود حتى
""".split())
REFINEMENT = re.compile(
    r'^(?:with\b|without\b|under\b|below\b|cheaper\b|same\b|among\b|which (?:of|one)\b|'
    r'avec\b|sans\b|moins de\b|parmi\b|بميزانية|بارخص|باقل|بنفس)')


def literal_terms(message):
    text = plain(message)
    text = re.sub(r'\b\d+(?:[.,]\d+)?\s*(?:tnd|dt|dinars?|دينار)\b', ' ', text)
    for group in (*EQUIVALENTS, *FEATURES):
        for term in group:
            if ' ' in term:
                text = re.sub(r'(?<!\w)' + term + r'(?!\w)', group[0], text)
    return list(dict.fromkeys(term for term in re.findall(r'[^\W_]+', text)
                              if term not in IGNORED and not term.isdigit()))[:8]


def product_phrase(message):
    """Ignore a compatibility target when a shopper has already named a product."""
    text = plain(message)
    parts = re.split(r'\s+(?:for|pour)\s+(?:(?:my|your|mon|ma|le|un|a|the)\s+)?'
                     r'(?=laptops?\b|pc\b|tablets?\b|ipads?\b|ordinateur\b)', text, maxsplit=1)
    return parts[0] if len(parts) > 1 and literal_terms(parts[0]) else text


@lru_cache(maxsize=512)
def _term_pattern(word):
    variants = next((group for group in (*EQUIVALENTS, *FEATURES) if word in group), (word,))
    if word == 'bluetooth':
        variants = (word,)
    return re.compile(r'(?<!\w)(?:' + '|'.join(map(re.escape, variants)) + r')(?!\w)')


def term_matches(term, normalized_text):
    word = plain(term).strip()
    return bool(word and _term_pattern(word).search(normalized_text))


def new_title_terms(message, explicit):
    """Unknown nouns start a title search. A brand, feature or budget alone refines."""
    if explicit.get('category') or REFINEMENT.search(plain(message)):
        return []
    terms = literal_terms(product_phrase(message))
    features = {word for group in FEATURES for word in group}
    features.update(('rgb', 'anc', 'gpu', 'graphics', 'card', 'dedicated', 'ram', 'gb', 'go', 'ssd'))
    features.update((explicit.get('brand'), explicit.get('preference')))
    return [term for term in terms if term not in features
            and not (term in BRAND_ALIASES and BRAND_ALIASES[term] == explicit.get('brand'))]


def type_matches(terms, title):
    """A product type must describe the item itself, not a compatibility note."""
    name = re.split(r'\b(?:pour|compatible avec|compatible with|for)\b', plain(title), maxsplit=1)[0]
    name = re.sub(r'[^\w]+', ' ', name)
    if any(term_matches(term, 'mouse') for term in terms):
        mouse = re.search(r'\b(?:souris|mouse|mice|mouses)\b', name)
        if mouse and re.search(r'\b(?:claviers?|keyboards?|pads?|tapis)\b', name):
            return False
        if re.search(r'\b(?:mousepad|packs?|ensembles?|bundles?|combos?|kits?)\b', name):
            return False
    for phrase in terms:
        phrase = plain(phrase).strip()
        equivalents = next((group for group in EQUIVALENTS if phrase in group), ())
        if equivalents:
            if any(_type_matches_literal(alias, name) for alias in equivalents):
                return True
            continue
        if _type_matches_literal(phrase, name):
            return True
    return False


@lru_cache(maxsize=256)
def _type_pattern(phrase):
    words = re.findall(r'\w+', plain(phrase))
    if not words:
        return None
    pattern = r'\s+'.join(re.escape(w.rstrip('s') if len(w) > 3 and not w.endswith('ss') else w)
                           + (r's?' if len(w) > 3 and not w.endswith('ss') else '') for w in words)
    return re.compile(r'\b' + pattern + r'\b')


def _type_matches_literal(phrase, name):
    pattern = _type_pattern(phrase)
    match = pattern.search(name) if pattern else None
    if match and not re.search(r'\b(?:tapis|coque|film|verre|pochette|tapis de souris|mouse pad|mousepad|sac|sacoche|housse|etui|filtre|support|chargeur|cable|adaptateur|batterie|pieces?|kit de nettoyage)\b', name[:match.start()]):
        return True
    return False

NEGATION = r'(?<!\w)(?:without|no|non|not|sans|aucun(?:e)?|بدون|بلا)\s+'
FEATURE_ALIASES = {
    'rgb': FEATURES[2],
    'dedicated_gpu': ('dedicated gpu', 'dedicated graphics', 'discrete gpu', 'discrete graphics',
                      'carte graphique dediee', 'carte graphique', 'gpu', 'بطاقة رسوميات', 'كرت شاشة'),
    'wireless': FEATURES[0],
    'bluetooth': ('bluetooth', 'بلوتوث'),
    'wired': FEATURES[1],
    'mechanical': FEATURES[4],
    'gaming': FEATURES[3],
}
INTEGRATED_GPU = re.compile(r'\b(?:integrated graphics|(?:gpu|graphics) integrated|'
                            r'graphique(?:s)? integree(?:s)?|intel (?:iris xe|uhd(?: graphics)?)|'
                            r'radeon (?:[67]80m|[78]90m|[78]60m))\b')


@lru_cache(maxsize=512)
def _feature_absence_pattern(term):
    options = FEATURE_ALIASES.get(term, (term,))
    pattern = '|'.join(re.escape(value) for value in sorted(options, key=len, reverse=True))
    return re.compile(NEGATION + r'(?:a |an |de |un |une |أي )?(?:' + pattern + r')(?!\w)')


def explicit_absences(text):
    return [term for term in FEATURE_ALIASES if _feature_absence_pattern(term).search(text)]


def _negative_evidence(term, raw, detail):
    if _feature_absence_pattern(term).search(detail):
        return True
    if term == 'dedicated_gpu':
        return bool(INTEGRATED_GPU.search(detail))
    for key, value in (raw.get('attributes') or {}).items():
        if term_matches(term, plain(str(key))) and plain(str(value)).strip() in ('no', 'non', 'false', '0', 'لا'):
            return True
    return False


def matches_features(criteria, name, raw, ram, storage, graphics):
    if criteria.get('phone_model'):
        model = re.search(r'\biphone\s*(\d{1,2})\b(?:\s+(pro max|pro|plus|mini)\b)?', plain(name))
        if not model or ('iphone ' + model[1] + ' ' + (model[2] or '')).strip() != criteria['phone_model']:
            return False
    required = list(criteria.get('search_terms', []))
    forbidden = criteria.get('forbidden_terms', [])
    if criteria.get('preference') and not criteria.get('soft_preference'):
        required.append(criteria['preference'])
    needs_gpu = criteria.get('dedicated_gpu') or criteria.get('gpu_priority')
    if needs_gpu:
        required = [term for term in required if not re.fullmatch(
            r'(?:good |powerful |dedicated |discrete )?(?:gpu|graphics card)|carte graphique(?: dediee)?',
            plain(term))]
    if criteria.get('gpu_model') and criteria['gpu_model'] != re.sub(r'\s+|geforce', '', plain(graphics or '')):
        return False
    if ((ram or 0) < (criteria.get('min_ram_gb') or 0)
            or (storage or 0) < (criteria.get('min_storage_gb') or 0)
            or (needs_gpu and not graphics)
            or (criteria.get('forbid_dedicated_gpu') and graphics)):
        return False
    if not (required or forbidden or criteria.get('forbid_dedicated_gpu')):
        return True
    detail = plain(' '.join((name, str(raw.get('description') or ''),
                             ' '.join(str(v) for v in (raw.get('attributes') or {}).values()))))
    if any(not term_matches(term, detail) or _negative_evidence(term, raw, detail) for term in required):
        return False
    for term in forbidden:
        positive = _feature_absence_pattern(term).sub(' ', detail)
        if term_matches(term, positive) or not _negative_evidence(term, raw, detail):
            return False
    return not criteria.get('forbid_dedicated_gpu') or _negative_evidence('dedicated_gpu', raw, detail)
