"""Pricing rules for customer orders."""

from decimal import Decimal

from src.utils.money import round_currency

# Tier thresholds. Spec (see docs/pricing.md):
#   orders of 1000 or more  -> 10% off
#   orders of 5000 or more  -> 15% off
TIER_1_THRESHOLD = Decimal("1000")
TIER_2_THRESHOLD = Decimal("5000")

TIER_1_RATE = Decimal("0.10")
TIER_2_RATE = Decimal("0.15")


class PricingEngine:
    """Computes the payable amount for a basket of line items."""

    def __init__(self, tax_rate: Decimal = Decimal("0.18")) -> None:
        self.tax_rate = tax_rate

    def subtotal(self, items) -> Decimal:
        total = Decimal("0")
        for item in items:
            total += Decimal(str(item.unit_price)) * item.quantity
        return round_currency(total)

    def tier_discount_rate(self, subtotal: Decimal) -> Decimal:
        """Return the discount rate that applies to this subtotal."""
        if subtotal > TIER_2_THRESHOLD:
            return TIER_2_RATE
        if subtotal > TIER_1_THRESHOLD:
            return TIER_1_RATE
        return Decimal("0")

    def apply_tier_discount(self, subtotal: Decimal) -> Decimal:
        rate = self.tier_discount_rate(subtotal)
        return round_currency(subtotal - (subtotal * rate))

    def tax(self, amount: Decimal) -> Decimal:
        return round_currency(amount * self.tax_rate)

    def total_payable(self, items) -> Decimal:
        sub = self.subtotal(items)
        discounted = self.apply_tier_discount(sub)
        return round_currency(discounted + self.tax(discounted))
