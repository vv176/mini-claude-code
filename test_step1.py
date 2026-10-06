"""Deterministic tests for step1's tools. No API calls."""
import sys, shutil
from pathlib import Path
import step1_discovery as s

s.REPO = Path("scratchrepo.pristine").resolve()
fails = []

def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("\n=== path safety ===")
check("../ rejected", s.read_file("../../etc/passwd").startswith("ERROR: PathOutsideRepo"))
check("absolute rejected", s.read_file("/etc/passwd").startswith("ERROR: PathOutsideRepo"))
check("missing file", s.read_file("nope.py").startswith("ERROR: FileNotFound"))

print("\n=== list_files ===")
out = s.list_files()
check("finds sources", "src/orders/pricing.py" in out)
check("skips __pycache__", "__pycache__" not in out)
check("pattern filter", set(s.list_files(".", "test_*.py").split("\n")) ==
      {"tests/test_cart.py", "tests/test_pricing.py"}, s.list_files(".", "test_*.py"))

print("\n=== search ===")
files = s.search("THRESHOLD")
check("files mode -> names", "src/orders/pricing.py" in files and ":" not in files.split("\n")[0])
content = s.search("TIER_1_THRESHOLD", mode="content")
check("content mode -> path:line:text", any(l.count(":") >= 2 for l in content.split("\n")[:3]))
check("glob filter", "tests/" not in s.search("Decimal", glob="*.py", path="src"))
check("no-match guidance", "[HARNESS] Nothing matched" in s.search("zzzz_not_here"))

# rg vs python fallback must agree
rg_hits = set(s._search_rg("THRESHOLD", s.REPO, False, None, "files", 0))
py_hits = set(s._search_python("THRESHOLD", s.REPO, False, None, "files"))
check("rg == python fallback", rg_hits == py_hits, f"{len(rg_hits)} vs {len(py_hits)}")

print("\n=== outline ===")
o = s.outline("src/orders/pricing.py")
check("has class", "class PricingEngine" in o)
check("has line ranges", "def tier_discount_rate" in o and "-" in o)
check("signature args", "def apply_tier_discount(self, subtotal)" in o)
check("footer", "[HARNESS] Signatures only" in o)

broken = s.REPO / "_broken_tmp.py"; broken.write_text("def f(x):\n    if x > 1\n        return x\n")
ob = s.outline("_broken_tmp.py")
check("syntax error reported", "SYNTAX ERROR at line 2" in ob, ob[:80])
check("syntax error flagged as bug", "very likely the bug" in ob)
broken.unlink()

print("\n=== read_file ===")
r = s.read_file("src/utils/money.py")
check("complete-file footer", "Complete file — all" in r)
check("line numbers present", "\t" in r and r.strip().split("\n")[0].strip().startswith("1"))
w = s.read_file("src/orders/pricing.py", offset=30, limit=10)
check("window footer honest", "Showing lines 30-39 of" in w, w.split("\n")[-1])
check("window starts at offset", w.strip().startswith("30\t") or w.split("\n")[0].strip().startswith("30"))
check("offset past end", s.read_file("src/utils/money.py", offset=9999).startswith("ERROR: OffsetPastEnd"))

print("\n=== dispatch never raises ===")
check("unknown tool", s.execute_tool("nope", {}).startswith("ERROR: UnknownTool"))
check("missing required arg", s.execute_tool("outline", {}).startswith("ERROR: KeyError"))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}\n")
sys.exit(1 if fails else 0)
