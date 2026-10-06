"""Cart-level summaries shown to the customer before checkout."""

from decimal import Decimal

from src.orders.pricing import PricingEngine, TIER_1_THRESHOLD, TIER_2_THRESHOLD
from src.utils.money import round_currency

FREE_SHIPPING_THRESHOLD = Decimal("1000")
STANDARD_SHIPPING = Decimal("49")


class CartSummary:
    """Builds the summary block rendered on the checkout page."""

    def __init__(self, engine: PricingEngine | None = None) -> None:
        self.engine = engine or PricingEngine()

    def qualifies_for_free_shipping(self, subtotal: Decimal) -> bool:
        return subtotal > FREE_SHIPPING_THRESHOLD

    def shipping_charge(self, subtotal: Decimal) -> Decimal:
        if self.qualifies_for_free_shipping(subtotal):
            return Decimal("0")
        return STANDARD_SHIPPING

    def discount_label(self, subtotal: Decimal) -> str:
        """Human-readable badge for the discount the customer earned."""
        if subtotal > TIER_2_THRESHOLD:
            return "15% off"
        if subtotal > TIER_1_THRESHOLD:
            return "10% off"
        return "No discount"

    def build(self, items) -> dict:
        sub = self.engine.subtotal(items)
        return {
            "subtotal": sub,
            "discount_label": self.discount_label(sub),
            "shipping": self.shipping_charge(sub),
            "total": round_currency(self.engine.total_payable(items) + self.shipping_charge(sub)),
        }
