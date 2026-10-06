#!/usr/bin/env python3
"""
Plant a bug in scratchrepo for a live demo.

    python3 plant.py              list the bugs
    python3 plant.py ordering     reset the repo and plant that one

Always resets from scratchrepo.pristine first, so bugs never stack up by
accident. Prints the exact task phrasing to paste into the agent.
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE / "scratchrepo"
PRISTINE = HERE / "scratchrepo.pristine"
CART = "src/orders/cart.py"
PRICING = "src/orders/pricing.py"


def sub(path, old, new):
    p = REPO / path
    t = p.read_text()
    assert old in t, f"anchor missing in {path}: {old[:50]!r}"
    p.write_text(t.replace(old, new))


def write(path, text):
    (REPO / path).write_text(text)


def fix_boundaries():
    """Start from a correct repo (the pristine copy ships WITH the boundary bug)."""
    for f in (PRICING, CART):
        p = REPO / f
        t = p.read_text()
        for a in ("subtotal > TIER_2_THRESHOLD", "subtotal > TIER_1_THRESHOLD",
                  "subtotal > FREE_SHIPPING_THRESHOLD"):
            t = t.replace(a, a.replace(">", ">="))
        p.write_text(t)


SUMMARY_TEST = '''from decimal import Decimal

from src.orders.cart import CartSummary
from src.orders.models import LineItem


def basket(price, qty=1):
    return [LineItem(sku="S1", name="Widget", unit_price=Decimal(str(price)), quantity=qty)]


def test_summary_for_a_large_order():
    summary = CartSummary().build(basket(2000))
    assert summary["shipping"] == Decimal("0")
    assert summary["discount_label"] == "10% off"


def test_summary_for_a_small_order():
    assert CartSummary().build(basket(100))["shipping"] == Decimal("49")
'''

TOTAL_TEST = '''from decimal import Decimal

from src.orders.models import LineItem
from src.orders.pricing import PricingEngine


def basket(price, qty=1):
    return [LineItem(sku="S1", name="Widget", unit_price=Decimal(str(price)), quantity=qty)]


def test_order_just_below_tier_1_gets_no_discount():
    """A 900 basket is below the 1000 discount tier.

    Tax is charged on what the customer actually owes after any discount, so a
    900 order pays 900 + 18% = 1062.00 and earns no discount at all.
    """
    assert PricingEngine().total_payable(basket(900)) == Decimal("1062.00")


def test_order_above_tier_1_gets_the_discount():
    """2000 -> 10% off -> 1800, then 18% tax -> 2124.00"""
    assert PricingEngine().total_payable(basket(2000)) == Decimal("2124.00")
'''


def boundary():
    pass            # already in the pristine copy


def typeerror():
    fix_boundaries()
    sub(CART, '        if self.qualifies_for_free_shipping(subtotal):\n            return Decimal("0")',
              '        if self.qualifies_for_free_shipping(subtotal):\n            return "0"')
    write("tests/test_summary.py", SUMMARY_TEST)


def ordering():
    fix_boundaries()
    sub(PRICING, """        sub = self.subtotal(items)
        discounted = self.apply_tier_discount(sub)
        return round_currency(discounted + self.tax(discounted))""",
                 """        sub = self.subtotal(items)
        taxed = round_currency(sub + self.tax(sub))
        return self.apply_tier_discount(taxed)""")
    write("tests/test_total.py", TOTAL_TEST)


def syntax():
    fix_boundaries()
    sub(CART, "    def discount_label(self, subtotal: Decimal) -> str:",
              "    def discount_label(self, subtotal: Decimal) -> str")


def cascade():
    # Two phases, deliberately small. A syntax error stops the suite from
    # running at all, and hiding behind it is ONE boundary bug. The agent
    # cannot see the second problem until it has fixed the first, so the
    # "run the tests, read the error, go again" arrow is forced rather than
    # hoped for. Keep the second phase to a single site: a long phase two
    # runs the agent out of hops and the demo ends red.
    fix_boundaries()
    sub(CART, "        return subtotal >= FREE_SHIPPING_THRESHOLD",
              "        return subtotal > FREE_SHIPPING_THRESHOLD")
    sub(CART, "    def discount_label(self, subtotal: Decimal) -> str:",
              "    def discount_label(self, subtotal: Decimal) -> str")


BUGS = {
    "boundary":  (boundary,  "several tests are failing. find the bug and fix it.",
                  "3 sites across 2 files, same mistake. Watch the sibling sweep."),
    "typeerror": (typeerror, "the checkout summary is blowing up for large orders. fix it.",
                  "Traceback points at cart.py:40; the cause is 20 lines higher. Two symptoms, one cause."),
    "ordering":  (ordering,  "a test is failing. find the bug and fix it.",
                  "No greppable keyword. It must read the test docstring to learn the intended order."),
    "syntax":    (syntax,    "the test suite won't even run. fix it.",
                  "Collection error, exit 2. Nothing runs at all."),
    "cascade":   (cascade,   "the test suite won't even run. fix it.",
                  "Syntax error hiding ONE boundary failure. Two phases -> the retry arrow, guaranteed."),
}


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in BUGS:
        print(__doc__)
        for name, (_, task, why) in BUGS.items():
            print(f"  {name:<10} {why}")
        sys.exit(0)

    name = sys.argv[1]
    fn, task, why = BUGS[name]
    shutil.rmtree(REPO, ignore_errors=True)
    shutil.copytree(PRISTINE, REPO, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    fn()

    out = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=REPO,
                         capture_output=True, text=True)
    print(f"\n  planted: {name}\n  {why}\n")
    print("  " + (out.stdout + out.stderr).strip().splitlines()[-1])
    print(f'\n  python3 step2_fix_loop.py --repo scratchrepo --task "{task}"\n')
