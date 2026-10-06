from decimal import Decimal

from src.orders.cart import CartSummary


def test_free_shipping_at_exactly_1000():
    cart = CartSummary()
    assert cart.shipping_charge(Decimal("1000")) == Decimal("0")


def test_discount_label_at_exactly_1000():
    cart = CartSummary()
    assert cart.discount_label(Decimal("1000")) == "10% off"
