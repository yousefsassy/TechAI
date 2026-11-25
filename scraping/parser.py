import json,hashlib
from datetime import datetime
from urllib.parse import urlsplit,urlunsplit
from pydantic import BaseModel,Field,ConfigDict
from decimal import Decimal
from typing import Literal
from scraping.normalize import specs,money,stock,category_name_valid
from backend.services.catalog import safe_url
from backend.models import now

class ScrapedOffer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=1000)
    category:str = Field(min_length=1, max_length=100)
    supplier:str
    source_id:str
    url:str
    image:str|None=None
    price:Decimal=Field(gt=0,le=10000000,decimal_places=3)
    stock_status:Literal['in_stock','out_of_stock','unknown']
    available_quantity:int|None=Field(default=None,ge=0)
    specifications:dict
    raw:dict
    model:str|None=None
    signature:str
    observed_at:datetime=Field(default_factory=now)

def objects(value):
    if isinstance(value,list):
        for v in value: yield from objects(v)
    elif isinstance(value,dict):
        yield value
        if '@graph' in value: yield from objects(value['@graph'])

def availability(page, offer):
    structured = stock(offer.get('availability'))
    # Only observe the product purchase widget. Other availability labels on
    # the page can belong to related products or individual branches.
    labels = [node.get_all_text().strip() for node in page.css(
        '#product-availability, .product-info-main .stock, .product-add-to-cart .stock')]
    statuses = {stock(label) for label in labels if label}
    if not statuses or statuses == {structured}:
        return structured
    if not offer.get('availability') and len(statuses) == 1:
        return statuses.pop()
    return 'unknown'

def parse_product(page,url,category,supplier,*,observed_at=None):
    product=None
    for script in page.css('script[type="application/ld+json"]::text').getall():
        try:
            parsed = json.loads(script)
        except (ValueError,TypeError): continue
        for obj in objects(parsed):
            kind = obj.get('@type')
            if kind != 'Product' and not (isinstance(kind, list) and 'Product' in kind):
                continue
            if product is not None and product != obj:
                raise ValueError('ambiguous_product_metadata')
            product = obj
    if not product: raise ValueError('structured_product_metadata_missing')
    offer=product.get('offers',{})
    if isinstance(offer,list):
        if len(offer)!=1: raise ValueError('ambiguous_offers')
        offer=offer[0]
    if not isinstance(offer, dict): raise ValueError('malformed_offer')
    if offer.get('priceCurrency')!='TND': raise ValueError('unexpected_currency')
    raw={}
    names=page.css('.data-sheet dt'); values=page.css('.data-sheet dd')
    for key,value in zip(names,values): raw[key.get_all_text().strip()]=value.get_all_text().strip()
    for row in page.css('#product-attribute-specs-table tr'):
        keys=row.css('th'); vals=row.css('td')
        if keys and vals: raw[keys[0].get_all_text().strip()]=vals[0].get_all_text().strip()
    name=product.get('name'); description=product.get('description')
    if not isinstance(name, str) or not name.strip(): raise ValueError('missing_product_name')
    name = name.strip()
    if not isinstance(description, str): description = ''
    if not category_name_valid(category,name):
        raise ValueError('wrong_product_category')
    normalized=specs(name+' '+description,raw)
    brand=product.get('brand')
    if isinstance(brand,dict): brand=brand.get('name')
    if isinstance(brand,str): normalized['brand']=brand
    model=product.get('mpn') or product.get('model')
    source_id=str(product.get('productID') or product.get('sku') or url)
    p=urlsplit(url); canonical=urlunsplit((p.scheme,p.netloc,p.path,'',''))
    if not safe_url(canonical): raise ValueError('unsafe_source_url')
    image=product.get('image')
    if isinstance(image,list): image=image[0] if image else None
    if isinstance(image,dict): image=image.get('url')
    # Each retailer owns its evidence; group matching observations at read time.
    signature = hashlib.sha256(canonical.encode()).hexdigest()
    return ScrapedOffer(name=name,category=category,supplier=supplier,source_id=source_id,url=canonical,
        image=safe_url(image) if isinstance(image, str) else None,price=money(offer.get('price')),
        stock_status=availability(page, offer),specifications=normalized,
        raw={'attributes':raw,'description':description,'metadata':product},
        model=model,signature=signature,observed_at=observed_at or now())
