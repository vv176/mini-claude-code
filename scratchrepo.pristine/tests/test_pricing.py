from decimal import Decimal

from src.orders.models import LineItem
from src.orders.pricing import PricingEngine


def item(price, qty=1):
    return LineItem(sku="SKU1", name="Widget", unit_price=Decimal(str(price)), quantity=qty)


def test_no_discount_below_threshold():
    engine = PricingEngine()
    assert engine.tier_discount_rate(Decimal("999")) == Decimal("0")


def test_tier_1_applies_at_exactly_1000():
    engine = PricingEngine()
    assert engine.tier_discount_rate(Decimal("1000")) == Decimal("0.10")


def test_tier_2_applies_at_exactly_5000():
    engine = PricingEngine()
    assert engine.tier_discount_rate(Decimal("5000")) == Decimal("0.15")
