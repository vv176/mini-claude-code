"""Deterministic tests for step2's guards. No API calls."""
import sys, shutil, tempfile
from pathlib import Path
import step2_fix_loop as s

fails = []
def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"\n        -> {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

def fresh():
    """Disposable copy of the fixture so tests never corrupt the original."""
    tmp = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree("scratchrepo.pristine", tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    s.REPO = tmp.resolve(); s.READ.clear(); s.LAST_FAILURES = None
    s.RAN_ANYTHING = True      # most tests exercise the edit guards, not G6
    return tmp

PRICING = "src/orders/pricing.py"

print("\n=== G6 ReproduceFirst ===")
fresh(); s.RAN_ANYTHING = False
s.read_file(PRICING)
r = s.edit_file(PRICING, "subtotal > TIER_1_THRESHOLD", "subtotal >= TIER_1_THRESHOLD")
check("edit before any run refused", r.startswith("ERROR: ReproduceFirst"), r[:90])
s.run_command("python3 -m pytest -q")
check("unlocked after running", s.edit_file(PRICING, "subtotal > TIER_1_THRESHOLD",
      "subtotal >= TIER_1_THRESHOLD").startswith("Edited"))

print("\n=== partial-run warning ===")
fresh()
r = s.run_command("python3 -m pytest -q -x")
check("-x flagged", "STOPPED at the first failure" in r, r.splitlines()[-1][:110])
r = s.run_command("python3 -m pytest -q")
check("full run not flagged", "STOPPED at the first failure" not in r)

print("\n=== small-file read nudge ===")
fresh()
r = s.read_file("src/utils/money.py", offset=1, limit=3)
check("nudges to read whole", "read it whole in one call" in r, r.splitlines()[-1][:110])

print("\n=== G1 ReadBeforeEdit ===")
fresh()
r = s.edit_file(PRICING, "subtotal > TIER_1_THRESHOLD", "subtotal >= TIER_1_THRESHOLD")
check("edit before read refused", r.startswith("ERROR: ReadBeforeEdit"), r[:90])
s.read_file(PRICING)
r = s.edit_file(PRICING, "subtotal > TIER_1_THRESHOLD", "subtotal >= TIER_1_THRESHOLD")
check("edit after read applies", r.startswith("Edited"), r[:90])
check("returns a diff", "---" in r and "+++" in r)
check("change on disk", ">= TIER_1_THRESHOLD" in (s.REPO / PRICING).read_text())

print("\n=== G2 StaleRead ===")
repo = fresh()
s.read_file(PRICING)
(repo / PRICING).write_text((repo / PRICING).read_text() + "\n# touched externally\n")
r = s.edit_file(PRICING, "subtotal > TIER_1_THRESHOLD", "subtotal >= TIER_1_THRESHOLD")
check("stale edit refused", r.startswith("ERROR: StaleRead"), r[:90])
s.read_file(PRICING)
check("re-read clears staleness",
      s.edit_file(PRICING, "subtotal > TIER_1_THRESHOLD", "subtotal >= TIER_1_THRESHOLD").startswith("Edited"))

print("\n=== G3 UniqueAnchor ===")
fresh(); s.read_file(PRICING)
r = s.edit_file(PRICING, "    return round_currency(total)", "    return total")   # 4 spaces, file has 8
check("under-indented anchor refused", r.startswith("ERROR: IndentationMismatch"), r[:120])
check("shows the real line", "actually reads" in r, r[:200])
fresh(); s.read_file(PRICING)
r2 = s.edit_file(PRICING, "zzz_no_such_text", "x")
check("absent anchor refused", r2.startswith("ERROR: AnchorNotFound"), r2[:90])
r3 = s.edit_file(PRICING, "if subtotal > TIER_1_THRESHOLD and extra:", "x")
check("near-miss hint given", "Closest line in the file is" in r3, r3[:200])

fresh(); s.read_file("src/orders/cart.py")
r = s.edit_file("src/orders/cart.py", "        if subtotal > TIER_2_THRESHOLD:", "        if subtotal >= TIER_2_THRESHOLD:")
check("unique anchor applies", r.startswith("Edited"), r[:90])

fresh(); s.read_file(PRICING)
r = s.edit_file(PRICING, "        return Decimal", "        return D")
check("ambiguous refused w/ count" if "Ambiguous" in r else "ambiguous refused w/ count",
      r.startswith("ERROR: AmbiguousAnchor") or r.startswith("Edited"), r[:120])

# a genuinely duplicated string
fresh(); s.read_file("src/orders/cart.py")
dup = "        if subtotal > TIER_"
r = s.edit_file("src/orders/cart.py", dup, dup.replace(">", ">="))
check("duplicate anchor refused", r.startswith("ERROR: AmbiguousAnchor"), r[:140])
check("names the lines", "lines 28" in r or "lines" in r, r[:140])
r2 = s.edit_file("src/orders/cart.py", dup, dup.replace(">", ">="), line_hint=28)
check("line_hint disambiguates", r2.startswith("Edited"), r2[:90])

fresh(); s.read_file("src/orders/cart.py")
r = s.edit_file("src/orders/cart.py", dup, dup.replace(">", ">="), replace_all=True)
check("replace_all changes both", r.startswith("Edited") and "2 replacements" in r, r[:90])

print("\n=== G4 DontEditTheTest ===")
fresh(); s.read_file("tests/test_pricing.py")
r = s.edit_file("tests/test_pricing.py", 'Decimal("1000")', 'Decimal("1001")')
check("test edit refused", r.startswith("ERROR: DontEditTheTest"), r[:90])
r = s.edit_file("tests/test_pricing.py", 'Decimal("1000")', 'Decimal("1001")', reason="spec changed")
check("reason unlocks it", r.startswith("Edited"), r[:90])

print("\n=== run_command + G5 ProgressSignal ===")
fresh()
r = s.run_command("python3 -m pytest -q")
check("runs tests", "exit 1" in r, r[:80])
check("counts failures", "[HARNESS] 4 test(s) failing" in r, r.splitlines()[-1][:120])
first = set(s.LAST_FAILURES)

r = s.run_command("python3 -m pytest -q")          # nothing changed
check("identical run detected", "IDENTICAL to the previous run" in r, r.splitlines()[-1][:140])

s.read_file(PRICING)
s.edit_file(PRICING, "        if subtotal > TIER_2_THRESHOLD:", "        if subtotal >= TIER_2_THRESHOLD:")
s.edit_file(PRICING, "        if subtotal > TIER_1_THRESHOLD:", "        if subtotal >= TIER_1_THRESHOLD:")
r = s.run_command("python3 -m pytest -q")
check("progress detected", "Fixed since last run" in r, r.splitlines()[-1][:160])

# regression: break something that passed
s.read_file(PRICING)
e = s.edit_file(PRICING, 'TIER_2_RATE = Decimal("0.15")', 'TIER_2_RATE = Decimal("0.99")')
check("regression setup edit applied", e.startswith("Edited"), e[:160])
r = s.run_command("python3 -m pytest -q")
check("regression flagged", "REGRESSION" in r, r.splitlines()[-1][:160])

check("exit 127 lists markers", "Build/test markers present" in s.run_command("definitely_not_a_command"))
check("timeout honoured", s.run_command("sleep 5", timeout=1).startswith("ERROR: Timeout"))

print("\n=== output truncation keeps the tail ===")
long = "\n".join(f"line{i}" for i in range(400))
t = s._truncate(long)
check("tail preserved", "line399" in t)
check("head preserved", "line0" in t)
check("middle omitted", "lines omitted from the middle" in t)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}\n")
sys.exit(1 if fails else 0)
