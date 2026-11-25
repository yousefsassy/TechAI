import re, unicodedata
from decimal import Decimal, InvalidOperation

def plain(text):
    if text.isascii():
        return text.lower()
    return ''.join(c for c in unicodedata.normalize('NFKD',text) if not unicodedata.combining(c)).lower()

def category_name_valid(category, name):
    if category == 'headset':
        title = plain(name)
        return bool(re.search(r'\b(casques?|headsets?|headphones?)\b', title)) and not re.search(
            r'\b(support|stand|coussinets?|etui|sacoche|housse|adaptateur|adapter)\b', title)
    if category == 'keyboard':
        t=plain(name)
        return bool(re.search(r'\b(clavier|keyboard)\b',t)) and not bool(re.search(r'\b(repose poignet|support|tapis|souris|keycaps?)\b',t))
    if category != 'laptop': return True
    return not bool(re.search(r'\b(refroidisseur|cooler|ventilateur|chargeur|batterie|sacoche|support|cooling)\b', plain(name)))

def money(value):
    text = format(value, 'f') if isinstance(value, Decimal) else str(value)
    text = unicodedata.normalize('NFKC', text).strip().replace('٬', ' ').replace('٫', '.')
    text = ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    if not re.fullmatch(r'[0-9][0-9., ]*(?:TND|DT)?', text, re.IGNORECASE):
        raise ValueError('malformed_price')
    text = re.sub(r'(?:TND|DT)$', '', text, flags=re.IGNORECASE).strip()
    # The fractional separator in a retailer amount is the rightmost
    # punctuation. A preceding separator must group exactly three digits.
    if '.' in text and ',' in text:
        decimal = '.' if text.rfind('.') > text.rfind(',') else ','
        grouping = ',' if decimal == '.' else '.'
        integer, fraction = text.rsplit(decimal, 1)
        if not re.fullmatch(r'\d{1,3}(?:' + re.escape(grouping) + r'\d{3})+', integer):
            raise ValueError('malformed_price')
        text = integer.replace(grouping, '') + '.' + fraction
    else:
        text = text.replace(',', '.')
        if text.count('.') > 1:
            if not re.fullmatch(r'\d{1,3}(?:\.\d{3})+', text):
                raise ValueError('malformed_price')
            text = text.replace('.', '')
        if ' ' in text:
            if not re.fullmatch(r'\d{1,3}(?: \d{3})+(?:\.\d+)?', text):
                raise ValueError('malformed_price')
            text = text.replace(' ', '')
    if not re.fullmatch(r'\d+(?:\.\d{1,3})?', text):
        raise ValueError('malformed_price')
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError('malformed_price') from exc
    if result <= 0 or result > 10000000:
        raise ValueError('invalid_price')
    return result.quantize(Decimal('.001'))

def capacity(text):
    m=re.search(r'(\d+(?:[.,]\d+)?)\s*(go|gb|to|tb)\b',plain(text))
    if not m: return None
    return int(Decimal(m[1].replace(',','.'))*(1024 if m[2] in ('to','tb') else 1))

def specs(text,attributes=None):
    t=plain(text); attrs={plain(k):v for k,v in (attributes or {}).items()}
    ram=None; storage=None; storage_type=None; processor=None; screen=None; os_name=None
    for key,value in attrs.items():
        if key == 'memoire' or any(k in key for k in ('memoire ram','ram','memoire vive')): ram=capacity(value)
        if any(k in key for k in ('disque dur','stockage','storage')):
            storage=capacity(value)
            storage_type=next((x for x in ('SSD','HDD','eMMC') if x.lower() in value.lower()),None)
        if any(k in key for k in ('processeur','processor')): processor=value.strip()
        if 'systeme' in key or 'operating' in key: os_name=value.strip()
        if ('taille' in key and 'ecran' in key) or 'screen size' in key:
            m=re.search(r'\d+(?:[.,]\d+)?',value)
            if m: screen=float(m[0].replace(',','.'))
    if ram is None:
        m=re.search(r'(?:ram|memoire(?: vive)?)\s*[:\-]?\s*(\d+\s*(?:go|gb))',t) or re.search(r'(\d+\s*(?:go|gb))\s*(?:ram|ddr[345])',t)
        if m: ram=capacity(m[1])
    if storage is None:
        m=re.search(r'(\d+(?:[.,]\d+)?\s*(?:go|gb|to|tb))\s*(ssd|hdd|emmc)',t) or re.search(r'(?:ssd|hdd|stockage|disque dur)\s*[:\-]?\s*(\d+(?:[.,]\d+)?\s*(?:go|gb|to|tb))',t)
        if m:
            storage=capacity(m[1]); storage_type=next((x for x in ('SSD','HDD','eMMC') if x.lower() in m[0]),None)
    if processor is None:
        m=re.search(r'(?:intel\s+)?(?:core\s+)?i[3579](?:[\s-]+\d{4,5}[a-z]*)?|(?:amd\s+)?ryzen\s+[3579](?:\s+\d{4}[a-z]*)?',t)
        if m: processor=m[0]
    brand=next((b for b in ('Logitech','Lenovo','Dell','HP','Asus','Acer','Apple','MSI','Samsung','Huawei','Honor','Chuwi','Blackview','Bmax') if re.search(r'\b'+b.lower()+r'\b',t)),None)
    return dict(brand=brand,processor=processor,ram_gb=ram,storage_gb=storage,storage_type=storage_type,screen_size=screen,operating_system=os_name)

def stock(text):
    value = plain(str(text or ''))
    if re.search(r'outofstock|out of stock|soldout|rupture|epuise|hors stock|indisponible|'
                 r'\b(?:non|pas)\s+(?:en stock|disponible)\b', value):
        return 'out_of_stock'
    if re.search(r'backorder|preorder|presale|sur commande|pre[ -]?commande|'
                 r'\b(?:en|a la) commande\b|\b(?:magasin|agence|boutique|branche|point de vente)\b', value):
        return 'unknown'
    if re.search(r'instock|\bin stock\b|\ben stock\b|\bdisponibles?\b|limitedavailability', value):
        return 'in_stock'
    return 'unknown'
