"""Explicit constraints that can be grounded without a model call."""
import re
from collections import OrderedDict
from copy import deepcopy
from functools import lru_cache
from threading import Lock

from backend.services.shopping_criteria import apply_price_boundary
from backend.services.shopping_terms import literal_terms, product_phrase
from scraping.normalize import plain

MOUSE = r'\b(?:mouse|mice|mouses|souris|فارة|ماوس)\b'
GAMING = r'\b(?:gaming|gamer|للالعاب|العاب)\b'
RECOMMEND = r'\b(?:recommend|best|meilleur\w*|افضل)\b'
CHEAPEST = r'\b(?:cheapest|least expensive|moins cher\w*|الارخص|ارخص)\b'
GPU = r'\b(rtx|gtx)\s*(\d{4})(?:\s*(ti|super))?\b'
RAM = r'\b(\d+)\s*(?:gb|go|جيجا)\s*(?:de\s+)?(?:ram|ذاكرة)\b'
PHONE = r'\b(?:iphone|ايفون)\s*(\d{1,2})\b'
STOCK = r'(?:(?:only )?(?:show (?:me )?)?(?:products |items |ones )?(?:that are )?(?:in stock)(?: only)?|(?:seulement )?(?:les produits )?(?:en stock)(?: seulement)?|(?:فقط )?(?:المنتجات )?المتوفرة(?: فقط)?)'


def explicit_features(message, criteria):
    text = plain(message)
    apply_price_boundary(message, criteria)
    if re.search(CHEAPEST, text):
        criteria['sort'] = 'price'
    elif re.search(RECOMMEND, text):
        criteria['sort'] = 'recommend'
    if re.search(MOUSE, text) and re.search(GAMING, text):
        criteria['search_terms'] = list(dict.fromkeys([*criteria.get('search_terms', []), 'gaming']))
    if ram := re.search(RAM, text):
        criteria['min_ram_gb'] = int(ram[1])
        if re.fullmatch(r'\d+ gb', criteria.get('preference', '')):
            criteria.pop('preference')
    if (gpu := re.search(GPU, text)) and not criteria.get('forbid_dedicated_gpu'):
        criteria['gpu_model'] = ''.join(gpu.groups(''))
        criteria['dedicated_gpu'] = True
    return criteria


def simple_search(message, previous, language, extract):
    text = plain(message).strip(' !?.؟')
    if previous and re.fullmatch(STOCK, text):
        return None, dict(previous), None  # Catalog searches already require confirmed stock.
    mouse = re.search(MOUSE, text)
    phone = re.search(PHONE, text)
    ram_gpu = re.search(RAM, text) and re.search(GPU, text)
    if not (mouse or phone or ram_gpu) or re.search(r'\b(?:compare|why|how|except|not|sauf|pourquoi)\b', text):
        return None
    # Only bypass the planner when the remaining content is understood.
    rest = re.sub('|'.join((MOUSE, GAMING, GPU, RAM, PHONE)), ' ', text)
    rest = re.sub(r'\b(?:where|can|i|find|the|are|that|is|for|my|a|an|and|budget|with|only|in|stock|wireless|wired|sans fil|filaire|bluetooth|pc portable|laptop|rtx|at|to|see|gaming|gamer|ou|trouver|le|la|les|des|moi|montre|cherche|une?|اقل|من|ارني|وريني|اظهر|اعرض|لي|لاسلكية|لاسلكي|اريد|اين|اجد|دينار|باقل|بميزانية)\b', ' ', rest)
    explicit, issue = extract(message, {}, language)
    if explicit.get('forbidden_terms') or explicit.get('forbid_dedicated_gpu'):
        rest = _SEARCH_FEATURES.sub(' ', rest)
        rest = re.sub(r'\b(?:without|no|sans|pas|بدون|غير)\b', ' ', rest)
    if any(word not in (explicit.get('brand'), 'pro', 'max', 'plus', 'mini') for word in literal_terms(rest)):
        return None
    if mouse:
        explicit['type_terms'] = ['souris']
    elif phone:
        explicit['type_terms'] = ['iphone ' + phone[1]]
        variant = re.match(r'\s+(pro max|pro|plus|mini)\b', text[phone.end():])
        edition = variant[1] if variant else ''
        explicit['title_terms'] = [phone[1], *edition.split()]
        explicit['phone_model'] = ('iphone ' + phone[1] + ' ' + edition).strip()
    elif not explicit.get('category'):
        return None
    if re.search(r'\bmy budget\b', text) and previous.get('budget'):
        explicit.setdefault('budget', previous['budget'])
    return None, explicit_features(message, explicit), issue



# The vocabulary below only consumes wording the local parser can fully resolve.
_SEARCH_WORDS = re.compile(
    r"\b(?:find|where|can|i|buy|look|am|looking|show|me|need|want|please|give|list|see|"
    r"the|a|an|some|any|these|that|are|my|for|and|to|of|with|has|only|under|below|"
    r"less than|up to|at most|max(?:imum)?|budget|dinars?|tnd|dt|in stock|"
    r"cherche|recherche|trouver|montre[z]?|moi|le|la|les|des|de|du|je|une?|"
    r"veux|avec|pour|et|moins de|sous|jusqu[. ]a|en stock|"
    r"ارني|وريني|اظهر|اعرض|اريد|ابحث|عن|لي|من|دينار|تونسي|تحت|اقل|ميزانيتي|حتى|فقط)\b"
)
_SEARCH_FEATURES = re.compile(
    r"\b(?:bluetooth|wireless|sans fil|wired|filaire|rgb|mechanical|mecanique|"
    r"dedicated gpu|discrete graphics|dedicated graphics|graphics card|gpu|carte graphique|"
    r"لاسلكي(?:ة)?|سلكي(?:ة)?|بطاقة رسوميات)\b"
)
_SEARCH_BUDGET = re.compile(r"(?<!\w)[\d.,٬٫ ]+(?:tnd|dt|dinars?|دينار(?: تونسي)?)\b")
_SOFT_USE = re.compile(r"\b(?:for programming|pour (?:la )?programmation|للبرمجة)\b")
_PRICE_COMPARISON = re.compile(
    r"(?:(?:which(?: one| of (?:these|them))?|what) is (?:the )?|(?:show me )?(?:the )?)"
    r"(?:cheapest|least expensive)(?: one)?|"
    r"(?:lequel|laquelle|quel|quelle) (?:est )?(?:le |la )?moins cher(?:e)?|"
    r"(?:ايها|ايهم|ايهما|ما هو) الارخص"
)


def price_comparison(text):
    return bool(_PRICE_COMPARISON.fullmatch(text))


@lru_cache(maxsize=16)
def _category_pattern(terms):
    aliases = [re.escape(word) + (r's?' if len(word) > 3 and word.isascii() and not word.endswith('s') else '')
               for word in terms]
    return re.compile(r'(?<!\w)(?:' + '|'.join(aliases) + r')(?!\w)')


def known_search(message, previous, language, extract, categories):
    text = plain(message).strip(' !?.؟')
    explicit, issue = extract(message, {}, language)
    category = explicit.get('category')
    if category not in categories:
        return None
    # A compatibility target in another category still needs interpretation.
    type_pattern = _category_pattern(categories[category])
    if not type_pattern.search(product_phrase(text)):
        return None

    # Question, evaluation and contrast words must retain the open planner path.
    if re.search(r'^(?:what|why|how|comment|pourquoi|explique|ما هو|اشرح)\b', text) or re.search(
        r'\b(?:compare|comparer|versus|vs|except|sauf|instead of)\b', text
    ):
        return None
    remaining = type_pattern.sub(' ', text)
    remaining = _SEARCH_BUDGET.sub(' ', remaining)
    remaining = _SEARCH_FEATURES.sub(' ', remaining)
    remaining = _SOFT_USE.sub(' ', remaining)
    remaining = _SEARCH_WORDS.sub(' ', remaining)
    remaining = re.sub('|'.join((RECOMMEND, GAMING, GPU, RAM)), ' ', remaining)
    # Consume negative feature words only when the shared parser found an
    # explicit negative constraint. Foreign currencies are handled by extract.
    if explicit.get('forbidden_terms') or explicit.get('forbid_dedicated_gpu'):
        remaining = re.sub(r'\b(?:without|no|sans|pas|بدون|غير)\b', ' ', remaining)
    if issue:
        remaining = re.sub(r'\b(?:usd|eur|dollars?|euros?|دولار|يورو)\b', ' ', remaining)
    consumed = {explicit.get('brand'), explicit.get('preference'), 'لوجيتك', 'لوجيتش', 'لوجيتيك', 'سامسونج', 'لينوفو', 'ابل'}
    if any(word not in consumed for word in literal_terms(remaining)):
        return None

    if previous.get('category') == category and re.match(r'^(?:only|seulement|فقط)\b', text):
        explicit, issue = extract(message, previous, language)
    if re.search(GAMING, text) and category == 'laptop':
        explicit['search_terms'] = list(dict.fromkeys([*explicit.get('search_terms', []), 'gaming']))
    if _SOFT_USE.search(text):
        explicit['sort'] = 'recommend'
        explicit['intended_use'] = 'programming'
    return None, explicit_features(message, explicit), issue


_ORDINALS = (
    ('first', 'premier', 'premiere', 'الاول', 'الاولى'),
    ('second', 'deuxieme', 'الثاني', 'الثانية'),
    ('third', 'troisieme', 'الثالث', 'الثالثة'),
    ('fourth', 'quatrieme', 'الرابع', 'الرابعة'),
    ('fifth', 'cinquieme', 'الخامس', 'الخامسة'),
    ('sixth', 'sixieme', 'السادس', 'السادسة'),
    ('seventh', 'septieme', 'السابع', 'السابعة'),
    ('eighth', 'huitieme', 'الثامن', 'الثامنة'),
    ('ninth', 'neuvieme', 'التاسع', 'التاسعة'),
    ('tenth', 'dixieme', 'العاشر', 'العاشرة'),
    ('eleventh', 'onzieme', 'الحادي عشر', 'الحادية عشرة'),
    ('twelfth', 'douzieme', 'الثاني عشر', 'الثانية عشرة'),
)
_ORDINAL_POSITIONS = {word: position for position, words in enumerate(_ORDINALS, start=1) for word in words}
_ORDINAL = re.compile(r'\b(?:' + '|'.join(sorted(_ORDINAL_POSITIONS, key=len, reverse=True)) + r')\b')
_CART = r'\b(?:cart|panier|السلة|السله|سلة|سله)\b'
_CART_ADD = re.compile(r'^(?:(?:please|can you|could you|stp|s il te plait)\s+)?(?:add|ajoute[zr]?|اضف|ضيف|حط)\b')
_CART_REMOVE = re.compile(r'^(?:(?:please|can you|could you|stp)\s+)?(?:remove|delete|retire[zr]?|supprime[zr]?|احذف|ازل|شيل)\b')
_CART_SUMMARY = re.compile(
    r"(?:(?:please )?(?:show(?: me)?|view|see|montre(?: moi)?|affiche|voir|اعرض|اظهر) )?"
    r"(?:(?:my|the|mon|le) )?(?:cart|panier|سلتي|السلة)|"
    r"(?:(?:what (?:is|s)(?: in)?|how much is) )?(?:(?:my|the) )?cart(?: (?:total|subtotal))?|"
    r"(?:(?:what (?:is|s)|how much is) )?(?:the )?total (?:of|for) (?:my|the) cart|"
    r"(?:(?:quel est|combien coute) )?(?:le )?total (?:du|de mon) panier|"
    r"(?:كم )?(?:اجمالي|مجموع) (?:السلة|سلتي)"
)


def shown_position(text):
    if ordinal := _ORDINAL.search(text):
        return _ORDINAL_POSITIONS[ordinal[0]]
    if ordinal := re.search(r'\b(\d+)(?:st|nd|rd|th|e|eme|er)?\b', text):
        return int(ordinal[1])
    return None


_DETAIL = re.compile(
    r"^(?:show(?: me)?(?: the)? details (?:of|for)|details (?:of|for)|"
    r"montre(?: moi)? les details (?:de|du)|اعرض تفاصيل) (.+)$"
)
_ORDERED_COMPARISON = re.compile(r'^(?:compare|comparez|comparer|قارن) (.+)$')
_POSITION_LABEL = re.compile(
    r'\b(?:the|le|la|du|of|un|no|n|numero|number|item|product|produit|article|'
    r'offre|items|products|produits|articles|offres|one|ones|المنتج|العرض|رقم)\b'
)


def displayed_positions(text):
    detail = _DETAIL.fullmatch(text)
    compare = _ORDERED_COMPARISON.fullmatch(text)
    if detail:
        fragments = [detail[1]]
    elif compare:
        fragments = re.split(r'\s+(?:and|with|vs\.?|et|avec|و)\s+|\s+و(?=ال|\d)', compare[1])
        if len(fragments) != 2:
            return []
    else:
        return []
    positions = []
    for fragment in fragments:
        ordinal = _POSITION_LABEL.sub(' ', fragment).strip()
        if not (_ORDINAL.fullmatch(ordinal) or re.fullmatch(r'\d{1,2}(?:st|nd|rd|th|e|eme|er)?', ordinal)):
            return []
        positions.append(shown_position(ordinal))
    return positions


def feature_refinement(text):
    match = re.fullmatch(r'(avec|with|only|فقط|without|sans|بدون)\s+(.+)', text)
    return match if match and _SEARCH_FEATURES.fullmatch(match[2]) else None


def cart_action(message):
    text = plain(message).replace('-', ' ').replace("'", ' ').strip()
    has_cart = bool(re.search(_CART, text))
    if has_cart and _CART_ADD.match(text):
        return 'add'
    if _CART_REMOVE.match(text) and (has_cart or shown_position(text) is not None):
        return 'remove'
    if _CART_SUMMARY.fullmatch(text.strip(' ?!.؟')):
        return 'total'
    return None


_PUBLIC_PLANS = OrderedDict()
_PUBLIC_PLANS_LOCK = Lock()
_PUBLIC_PLAN_LIMIT = 256


def get_public_plan(key):
    with _PUBLIC_PLANS_LOCK:
        if key not in _PUBLIC_PLANS:
            return None
        _PUBLIC_PLANS.move_to_end(key)
        return deepcopy(_PUBLIC_PLANS[key])


def remember_public_plan(key, criteria):
    # Only caller-confirmed context-free catalog criteria reach this cache.
    with _PUBLIC_PLANS_LOCK:
        _PUBLIC_PLANS[key] = deepcopy(criteria)
        _PUBLIC_PLANS.move_to_end(key)
        if len(_PUBLIC_PLANS) > _PUBLIC_PLAN_LIMIT:
            _PUBLIC_PLANS.popitem(last=False)


def named_pair(message, brands):
    match = re.fullmatch(r'(?:compare|comparez|comparer|قارن)\s+(.+?)\s+(?:and|vs\.?|versus|et|و)\s*(.+?)[?.!]*', plain(message))
    if not match:
        return None
    subjects = []
    for part in match.groups():
        terms = re.findall(r'[\w-]+', part)
        brand = next((word for word in terms if word in brands), None)
        model = [word for word in terms if word not in (brand, 'the', 'le', 'la')]
        if not brand or not model or len(model) > 4:
            return None
        subjects.append(dict(brand=brand, title_terms=model))
    return subjects
