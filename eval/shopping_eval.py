"""Audit currently collected offers against explicit shopping constraints."""
import json
from decimal import Decimal
from backend.database import init_db
from backend.services.shopping import extract, search

SCENARIOS = [
    'Find Apple AirPods under 500 TND',
    'Find Lenovo laptops under 2500 TND',
    'Find tablets under 1500 TND',
]

def evaluate():
    init_db()
    result = []
    for request in SCENARIOS:
        criteria, issue = extract(request, {})
        offers = search(criteria) if not issue else []
        relevant = [o for o in offers if o['category'] == criteria['category']
                    and Decimal(o['price_tnd']) <= Decimal(criteria['budget'])
                    and (not criteria.get('brand') or criteria['brand'] in (o['brand'] or o['name']).lower())]
        result.append(dict(query=request, offers=len(offers), relevant=len(relevant),
                           precision=(len(relevant) / len(offers)) if offers else None))
    return result

if __name__ == '__main__':
    print(json.dumps(evaluate(), indent=2))
