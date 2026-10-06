"""Plain data holders used across the order pipeline."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class LineItem:
    sku: str
    name: str
    unit_price: Decimal
    quantity: int = 1


@dataclass
class Customer:
    customer_id: str
    email: str
    is_premium: bool = False
