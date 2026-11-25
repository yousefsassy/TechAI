"""Shared shopping predicates for cached offers and freshly verified product pages."""
import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache

from backend.services.shopping_terms import matches_features, term_matches, type_matches
from scraping.normalize import category_name_valid, plain


CATEGORIES = {
    'headset': ('headset', 'headphone', 'casque', 'سماعة راس', 'سماعات راس', 'سماعة الراس', 'سماعات الراس', 'هيدست'),
    'keyboard': ('keyboard', 'keyboards', 'clavier', 'claviers', 'لوحة مفاتيح', 'لوحات مفاتيح', 'كيبورد'),
    'earphones': ('airpods', 'earbuds', 'earphones', 'ecouteurs', 'سماعات', 'ايربودز', 'إيربودز', 'ايربودس'),
    'laptop': ('laptop', 'notebook', 'pc portable', 'ordinateur portable', 'لابتوب', 'حاسوب محمول', 'كمبيوتر محمول'),
    'desktop': ('desktop', 'pc bureau', 'ordinateur de bureau', 'tower', 'حاسوب مكتبي', 'كمبيوتر مكتبي'),
    'tablet': ('tablet', 'tablette', 'ipad', 'تابلت', 'جهاز لوحي', 'ايباد', 'آيباد'),
}

DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬', '01234567890123456789. ')
STRICT = r'under|below|less th[ae]n|moins de|sous|اقل من|تحت'
INCLUSIVE = (r'up to|at most|max(?:imum)?(?: budget)?|budget|jusqu.a|حتى|ميزانيتي|الميزانية|'
             r'بحد اقصى|لا\s*تتجاوز|لا\s*يتجاوز|لا\s*تزيد\s*عن|اقصى\s*ميزانية')
CURRENCY = (r'usd|dollars?|eur|euros?|gbp|pounds?|aed|sar|cad|mad|dzd|jpy|egp|'
            r'تونس[يي]|دينار(?: تونسي)?|دولار|يورو|جنيه|tnd|dt|dinars?|د\.ت|[$€£¥]')
AMOUNT = r'-?\d+(?:[., ]\d+)*'
BUDGET = re.compile(
    rf'(?<!\w)(?P<operator>{STRICT}|{INCLUSIVE})(?!\w)\s*(?:(?:of|de|is|est|هي)\s+)?'
    rf'(?P<before>{CURRENCY})?\s*(?P<amount>{AMOUNT})\s*(?P<after>{CURRENCY})?(?!\w)'
)
CURRENCY_AMOUNT = re.compile(
    rf'(?<![\w.,])(?:(?P<leading>{CURRENCY})(?!\w)\s*(?P<first>{AMOUNT})(?!\w)|'
    rf'(?P<last>{AMOUNT})\s*(?P<trailing>{CURRENCY})(?!\w))'
)
_catalog_type_matches = lru_cache(maxsize=1024)(type_matches)

LOCAL_CURRENCY = frozenset({'tnd', 'dt', 'dinars', 'dinar', 'دينار', 'دينار تونسي', 'د.ت'})


def query_text(message):
    return plain(message).translate(DIGITS)


def _budget_match(message):
    text = query_text(message)
    # Standalone Arabic operators also appear with attached prepositions (بأقل).
    text = re.sub(r'(?<!\w)ب(?=اقل من|ميزانيتي|الميزانية|حد اقصى)', ' ', text)
    return BUDGET.search(text), CURRENCY_AMOUNT.search(text)


def _amount(text, language):
    if '.' in text and ',' in text:
        decimal = '.' if text.rfind('.') > text.rfind(',') else ','
    elif ',' in text:
        decimal = '.' if language == 'en' and re.fullmatch(r'\d{1,3}(?:,\d{3})+', text) else ','
    elif re.fullmatch(r'\d{1,3}(?:\.\d{3}){2,}', text) or (
        language == 'fr' and re.fullmatch(r'\d{1,3}\.\d{3}', text)
    ):
        decimal = ','
    else:
        decimal = '.'
    integer, delimiter, fraction = text.partition(decimal)
    grouping = ',' if decimal == '.' else '.'
    separators = {char for char in integer if char in ' ,.'}
    if separators:
        if len(separators) != 1 or not re.fullmatch(
            rf'\d{{1,3}}(?:[{re.escape(grouping)} ]\d{{3}})+', integer
        ):
            raise ValueError('ambiguous_budget')
    digits = integer.replace(' ', '').replace(grouping, '')
    if not digits.isdecimal() or (delimiter and not re.fullmatch(r'\d{1,3}', fraction)):
        raise ValueError('ambiguous_budget')
    value = Decimal(digits + ('.' + fraction if delimiter else ''))
    if value <= 0 or value > 10_000_000:
        raise ValueError('ambiguous_budget')
    return value


def parse_budget(message, language):
    """Return a TND limit and its boundary, or signal why clarification is needed."""
    explicit, currency_only = _budget_match(message)
    if explicit:
        amount = explicit['amount']
        currencies = (explicit['before'], explicit['after'])
    elif currency_only:
        amount = currency_only['first'] or currency_only['last']
        currencies = (currency_only['leading'], currency_only['trailing'])
    else:
        return None, False
    if any(code and code not in LOCAL_CURRENCY for code in currencies):
        raise ValueError('currency')
    return _amount(amount, language), bool(explicit and re.fullmatch(STRICT, explicit['operator']))


def apply_price_boundary(message, criteria):
    explicit, currency_only = _budget_match(message)
    if criteria.get('budget') and (explicit or currency_only):
        if explicit and re.fullmatch(STRICT, explicit['operator']):
            criteria['exclusive_budget'] = True
        else:
            criteria.pop('exclusive_budget', None)
    return criteria


@dataclass(frozen=True)
class Criteria:
    """Evaluate the same resolved criteria against ORM and newly parsed observations."""

    fields: dict
    category: str | None = field(init=False)
    budget: Decimal | None = field(init=False)
    brand: str = field(init=False)
    types: tuple[str, ...] = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, 'category', self.fields.get('category'))
        limit = self.fields.get('budget')
        object.__setattr__(self, 'budget', Decimal(str(limit)) if limit else None)
        object.__setattr__(self, 'brand', self.fields.get('brand', ''))
        object.__setattr__(self, 'types', tuple(self.fields.get('type_terms') or ()))

    def price_matches(self, price):
        if price <= 0:
            return False
        if self.budget is None:
            return True
        return price < self.budget if self.fields.get('exclusive_budget') else price <= self.budget

    def matches_product(self, *, name, category, brand, model, raw, ram, storage, graphics):
        if self.category and not self.types and category != self.category:
            if category != 'accessories' or self.category not in ('headset', 'keyboard'):
                return False
        if not category_name_valid(self.category, name):
            return False
        title = plain(name) if self.brand or self.fields.get('title_terms') else ''
        if self.brand:
            subject = re.split(r'\b(?:pour|compatible avec|compatible with|for)\b', title, maxsplit=1)[0]
            if not term_matches(self.brand, plain(brand or '')):
                # Older rows can carry an inferred brand. Use a leading product
                # title only when it does not also name the conflicting brand.
                if not re.match(rf'^(?:\w+\s+){{0,3}}{re.escape(self.brand)}\b', subject):
                    return False
                if brand and term_matches(brand, subject):
                    return False
        if self.types and not _catalog_type_matches(self.types, name):
            return False
        if not self.types and self.category in ('keyboard', 'headset') and not _catalog_type_matches(CATEGORIES[self.category], name):
            return False
        if any(not term_matches(term, f'{title} {plain(str(model or ""))}')
               for term in self.fields.get('title_terms', ())):
            return False
        if self.brand == 'apple' and self.category in ('tablet', 'earphones'):
            apple_type = 'ipad' if self.category == 'tablet' else 'airpods'
            if not _catalog_type_matches((apple_type,), name):
                return False
            if self.fields.get('airpods_edition') or self.fields.get('airpods_generation'):
                published = re.search(r'\bairpods\s*(pro|max)?\s*(?:gen(?:eration)?\s*)?(\d)?', title)
                edition = self.fields.get('airpods_edition')
                generation = self.fields.get('airpods_generation')
                if not published or (edition and published[1] != edition) or (
                    generation and int(published[2] or 0) != generation
                ):
                    return False
        return matches_features(self.fields, name, raw, ram, storage, graphics)
