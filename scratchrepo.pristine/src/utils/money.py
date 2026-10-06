"""Currency helpers."""

from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")


def round_currency(value: Decimal) -> Decimal:
    """Round to 2 decimal places, half-up, the way finance expects."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
