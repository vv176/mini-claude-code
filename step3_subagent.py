"""
Step 3: Context isolation — delegate the search to a sub-agent.

Step 2 works, but look at what is left in the main conversation when it
finishes: every directory listing, every search result, every file it read on
the way to the answer. All of it is still there, and the model re-reads the
whole pile on every single turn that follows.

Most of it was scaffolding. The agent needed `cart.py` lines 26-32 — it did
not need the other nine files it opened to find them.

So: give the searching to a sub-agent with its own conversation. It explores,
it accumulates its own mess, and it returns a short report. The mess is thrown
away with it; only the conclusion crosses back.

    main agent          sub-agent (fresh context)
    ----------          -------------------------
    spawn_searcher  ->  list_files, search, outline, read_file ... 14 calls
         <-------------  "the bug is at pricing.py:31 and cart.py:28,30"
    read_file
    edit_file
    run_command

Run it both ways and compare what is sitting in context at the end:

    python step3_subagent.py --repo scratchrepo --mode inline   --task "..."
    python step3_subagent.py --repo scratchrepo --mode subagent --task "..."

Dependencies:
    pip install openai python-dotenv

Run:
    python step3_subagent.py --repo <path> --task "<what is wrong>" [--mode inline|subagent]
"""

import os
import re
import ast
import json
import time
import difflib
import hashlib
import fnmatch
import argparse
import subprocess
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("MODEL", "gpt-4o")

_client = None


def client() -> OpenAI:
    """Built on first use, not at import — so the tools stay importable and
    testable on a machine with no API key."""
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set (put it in .env)")
        _client = OpenAI(api_key=key)
    return _client

MAX_HOPS = 20
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             "dist", "build", ".pytest_cache", ".mypy_cache", ".idea"}

REPO = Path(".")  # set from --repo in main()

# ─── session state ────────────────────────────────────────────────────────
# READ is what makes G1/G2 possible: the sha of every file at the moment the
# agent looked at it. LAST_FAILURES is what makes G5 possible.
READ: dict[str, dict] = {}            # path -> {"sha": str, "lines": int, "at": float}
LAST_FAILURES: set | None = None      # set of failing test ids from the previous run
RAN_ANYTHING = False                  # has the agent run a command yet? (G6)
TEST_FILE_RE = re.compile(r"(^|/)(tests?)/|(^|/)test_[^/]+$|_test\.[a-z]+$")


def sha_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# ─────────────────────────────────────────────
# PATH SAFETY
#
# Every path the model supplies is resolved and checked against the repo
# root. Not a security boundary — a blast radius. The agent cannot name a
# file outside the repo it was pointed at.
# ─────────────────────────────────────────────

def safe_path(rel: str) -> tuple[Path | None, str | None]:
    rel = (rel or ".").strip()
    # Reject absolute paths outright. Do NOT strip the leading slash to make
    # them relative — that silently turns '/etc/passwd' into '<repo>/etc/passwd'
    # and hides the fact that the model asked for something it should not have.
    if rel.startswith("/") or Path(rel).is_absolute():
        return None, ("ERROR: PathOutsideRepo: absolute paths are not allowed. "
                      "Use a path relative to the repository root.")
    target = (REPO / rel).resolve()
    if not str(target).startswith(str(REPO.resolve())):
        return None, f"ERROR: PathOutsideRepo: '{rel}' resolves outside the repository."
    return target, None


def rel(p: Path) -> str:
    return str(p.relative_to(REPO.resolve()))


def walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.startswith("."):
                yield Path(dirpath) / name


# ─────────────────────────────────────────────
# TOOL 1 — list_files : what exists
# ─────────────────────────────────────────────

def list_files(subdir: str = ".", pattern: str = "*") -> str:
    target, err = safe_path(subdir)
    if err:
        return err
    if not target.is_dir():
        return f"ERROR: NotADirectory: '{subdir}' is not a directory in this repo."

    hits = [rel(p) for p in walk_files(target) if fnmatch.fnmatch(p.name, pattern)]
    hits.sort()
    if not hits:
        return f"(no files matching '{pattern}' under '{subdir}')"
    return "\n".join(hits)


# ─────────────────────────────────────────────
# TOOL 2 — search : where a token appears
#
# Returns coordinates: path, or path:line:text. ripgrep when available,
# a pure-Python walk when it is not, so this runs on any machine.
# ─────────────────────────────────────────────

def _search_rg(pattern, target, regex, glob, mode, context):
    cmd = ["rg", "--line-number", "--with-filename", "--smart-case", "--color", "never"]
    if not regex:
        cmd.append("--fixed-strings")
    if glob:
        cmd += ["--glob", glob]
    if mode == "files":
        cmd.append("--files-with-matches")
    elif context:
        cmd += ["--context", str(context)]
    cmd += ["--", pattern, str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.strip())
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _search_python(pattern, target, regex, glob, mode):
    flags = re.IGNORECASE if pattern.islower() else 0
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    lines = []
    files = [target] if target.is_file() else list(walk_files(target))
    for path in files:
        if glob and not fnmatch.fnmatch(str(path), glob) and not fnmatch.fnmatch(path.name, glob):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if not rx.search(text):
            continue
        if mode == "files":
            lines.append(str(path))
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                lines.append(f"{path}:{i}:{line}")
    return lines


def search(pattern: str, path: str = ".", regex: bool = False, glob: str | None = None,
           mode: str = "files", context: int = 0) -> str:
    target, err = safe_path(path)
    if err:
        return err
    if not pattern.strip():
        return "ERROR: EmptyPattern: give a token to search for."

    try:
        raw = _search_rg(pattern, target, regex, glob, mode, context)
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired):
        raw = _search_python(pattern, target, regex, glob, mode)

    root = str(REPO.resolve()) + "/"
    out = [ln.replace(root, "") for ln in raw]
    if not out:
        note = ("[HARNESS] Nothing matched. Try a shorter token, a different spelling, "
                "or search the tests for the word the user used.")
        # Very common miss: the model writes a regex but leaves regex=false, so
        # the metacharacters are matched literally and nothing is ever found.
        if not regex and re.search(r"[|\[\](){}*+?^$\\]", pattern):
            note += ("\n[HARNESS] Your pattern contains regex characters "
                     f"({', '.join(sorted(set(c for c in pattern if c in '|[](){{}}*+?^$')))}) "
                     "but regex=false, so it was searched for LITERALLY. "
                     "Retry with regex=true, or search for one plain token.")
        return f"(no matches for '{pattern}')\n{note}"
    body = "\n".join(out)
    if mode == "files":
        body += "\n[HARNESS] Filenames only. Re-run with mode=\"content\" to see the lines."

    # Models fixate on the first file in a result list and report it as "the"
    # site. When matches span several files, say so explicitly — the evidence
    # being present in context is demonstrably not enough.
    files_hit = {}
    for line in out:
        name = line.split(":", 1)[0]
        if name.isdigit():          # a bare line number, not a path
            continue
        files_hit[name] = files_hit.get(name, 0) + 1
    if len(files_hit) > 1:
        listing = ", ".join(f"{f} ({c})" for f, c in sorted(files_hit.items()))
        body += (f"\n[HARNESS] These matches span {len(files_hit)} files: {listing}. "
                 f"If you are about to report a defect, account for EVERY file in this "
                 f"list — not just the first one.")
    return body


# ─────────────────────────────────────────────
# TOOL 3 — outline : the shape of a file, cheaply
#
# Python goes through ast, so the line ranges are exact rather than guessed.
# A SyntaxError is not a failure here — it is a finding, and often the bug,
# so it is reported instead of raised.
# ─────────────────────────────────────────────

DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:public|private|protected|static|async|final)?\s*"
    r"(?:def|class|function|func|fn|type|interface|struct|const\s+\w+\s*=\s*\(|\w+\s*\()"
)


def _outline_python(src: str) -> str | None:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return (f"SYNTAX ERROR at line {e.lineno}: {e.msg}\n"
                f"[HARNESS] This file does not parse. That is very likely the bug — "
                f"read around line {e.lineno}.")
    rows = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            rows.append((node.lineno, node.end_lineno, f"def {node.name}({args})"))
        elif isinstance(node, ast.ClassDef):
            rows.append((node.lineno, node.end_lineno, f"class {node.name}"))
    rows.sort()
    return "\n".join(f"{a:>5}-{b:<5} {label}" for a, b, label in rows)


def outline(path: str) -> str:
    target, err = safe_path(path)
    if err:
        return err
    if not target.is_file():
        return f"ERROR: FileNotFound: '{path}' is not a file in this repo."

    src = target.read_text(errors="replace")
    total = len(src.splitlines())

    if target.suffix == ".py":
        body = _outline_python(src)
    else:
        body = "\n".join(
            f"{i:>5}       {line.strip()}"
            for i, line in enumerate(src.splitlines(), 1)
            if DEF_RE.match(line)
        )

    if not body:
        return (f"(no definitions found in {path}; {total} lines)\n"
                "[HARNESS] Small or non-code file — just read_file it.")
    return (f"{path} — {total} lines\n{body}\n"
            f"[HARNESS] Signatures only. read_file with offset/limit to see a body.")


# ─────────────────────────────────────────────
# TOOL 4 — read_file : the actual code
#
# Numbered like `cat -n`. The numbers are for orientation, not addressing.
# The footer always says how much of the file you did NOT see.
# ─────────────────────────────────────────────

def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    target, err = safe_path(path)
    if err:
        return err
    if not target.is_file():
        return f"ERROR: FileNotFound: '{path}' is not a file in this repo."

    lines = target.read_text(errors="replace").splitlines()
    total = len(lines)
    # Record what we saw and when. edit_file checks this.
    READ[rel(target)] = {"sha": sha_of(target), "lines": total, "at": time.time()}
    offset = max(1, int(offset))
    limit = max(1, int(limit))
    window = lines[offset - 1: offset - 1 + limit]
    if not window:
        return f"ERROR: OffsetPastEnd: '{path}' has {total} lines; offset {offset} is past the end."

    body = "\n".join(f"{offset + i:>5}\t{ln}" for i, ln in enumerate(window))
    last = offset + len(window) - 1
    if offset == 1 and last == total:
        footer = f"[HARNESS] Complete file — all {total} lines."
    else:
        footer = (f"[HARNESS] Showing lines {offset}-{last} of {total}. "
                  f"{total - len(window)} lines not shown — this is a partial view.")
        if total <= 200:
            footer += (f" This file is only {total} lines — read it whole in one call "
                       f"instead of paging through it.")
    return f"{body}\n{footer}"


# ─────────────────────────────────────────────
# TOOL 5 — edit_file : exact string replacement
#
# No line numbers anywhere in this signature. old_string is both the address
# and the checksum: if the file is not what the agent thinks it is, the match
# fails and nothing is written.
# ─────────────────────────────────────────────

def _occurrences(content: str, needle: str):
    """Byte offsets of every occurrence, with the 1-based line each starts on."""
    spots, start = [], 0
    while True:
        i = content.find(needle, start)
        if i == -1:
            return spots
        spots.append((i, content.count("\n", 0, i) + 1))
        start = i + 1


def _near_miss(content: str, needle: str) -> str:
    """When old_string is absent, point at the closest line in the file.

    The cause is nearly always indentation or a quote character, and the model
    cannot see that from 'not found'.
    """
    lines = content.splitlines()
    probe = needle.strip().splitlines()[0] if needle.strip() else needle
    best = difflib.get_close_matches(probe, [l.strip() for l in lines], n=1, cutoff=0.5)
    if not best:
        return ""
    idx = next(i for i, l in enumerate(lines) if l.strip() == best[0])
    return (f"\n[HARNESS] Closest line in the file is {idx + 1}: {lines[idx]!r}\n"
            f"Compare it with what you sent — leading whitespace and quote characters "
            f"must match exactly. Re-read the file if you are unsure.")


def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False, line_hint: int | None = None,
              reason: str | None = None) -> str:
    target, err = safe_path(path)
    if err:
        return err
    if not target.is_file():
        return f"ERROR: FileNotFound: '{path}' is not a file in this repo."
    key = rel(target)

    # G6 — reproduce before you repair. An agent that edits before it has run
    # anything is fixing a bug it has only imagined, and it has no baseline to
    # tell later whether it broke something that used to pass.
    if not RAN_ANYTHING:
        return ("ERROR: ReproduceFirst: you have not run anything yet. Run the test suite "
                "before editing — you need to see the real failure, and you need a baseline "
                "of what already passes so you can tell afterwards whether you broke it.")

    # G4 — editing the test to make the suite green is the classic cheat.
    if TEST_FILE_RE.search(key) and not reason:
        return ("ERROR: DontEditTheTest: '" + key + "' is a test file. A failing test is "
                "usually correct and the source is usually wrong — fix the source. If you "
                "genuinely believe the TEST is wrong, call edit_file again with a `reason` "
                "explaining why, and state it in your reply to the user as well.")

    # G1 — never edit a file you have not looked at.
    if key not in READ:
        return (f"ERROR: ReadBeforeEdit: you have not read '{key}' in this session. "
                f"read_file it first — editing a file you have not seen is how agents "
                f"destroy code.")

    # G2 — the file must still be what you read.
    current = sha_of(target)
    if current != READ[key]["sha"]:
        READ.pop(key, None)
        return (f"ERROR: StaleRead: '{key}' has changed since you read it (an earlier edit, "
                f"a test run, or someone else). Your line numbers and context are out of "
                f"date. read_file it again, then redo this edit.")

    content = target.read_text()
    if old_string == new_string:
        return "ERROR: NoOp: old_string and new_string are identical."

    spots = _occurrences(content, old_string)

    # An under-indented anchor matches as a SUBSTRING inside a more-indented
    # line: "    return x" is found inside "        return x". The replacement
    # then applies mid-line and silently corrupts the indentation. So when the
    # anchor starts with whitespace the model meant whole lines — require the
    # match to begin at a line boundary.
    if old_string[:1] in (" ", "\t"):
        aligned = [sp for sp in spots if sp[0] == 0 or content[sp[0] - 1] == "\n"]
        if spots and not aligned:
            line_no = spots[0][1]
            actual = content.splitlines()[line_no - 1]
            return (f"ERROR: IndentationMismatch: that text occurs in '{key}' but only "
                    f"INSIDE a more-indented line, so replacing it would corrupt the "
                    f"indentation. Line {line_no} actually reads:\n{actual!r}\n"
                    f"Copy it exactly, including the leading whitespace.")
        spots = aligned

    # G3 — the anchor must identify exactly one place.
    if not spots:
        return (f"ERROR: AnchorNotFound: that exact text does not appear in '{key}'."
                + _near_miss(content, old_string))

    if len(spots) > 1 and not replace_all and line_hint is None:
        at = ", ".join(str(ln) for _, ln in spots)
        return (f"ERROR: AmbiguousAnchor: that text appears {len(spots)} times in '{key}' "
                f"(lines {at}). Include the surrounding lines so the anchor is unique, or "
                f"pass line_hint=<line> to pick one, or replace_all=true if every "
                f"occurrence really should change.")

    before = content
    if replace_all:
        content = content.replace(old_string, new_string)
        changed = len(spots)
    else:
        offset = (min(spots, key=lambda s: abs(s[1] - line_hint))[0]
                  if line_hint is not None else spots[0][0])
        content = content[:offset] + new_string + content[offset + len(old_string):]
        changed = 1

    target.write_text(content)
    READ[key] = {"sha": sha_of(target), "lines": len(content.splitlines()), "at": time.time()}

    diff = list(difflib.unified_diff(before.splitlines(), content.splitlines(),
                                     fromfile=key, tofile=key, lineterm="", n=2))
    body = "\n".join(diff[:40])
    if len(diff) > 40:
        body += f"\n... ({len(diff) - 40} more diff lines)"
    return f"Edited {key} ({changed} replacement{'s' if changed != 1 else ''}).\n{body}"


# ─────────────────────────────────────────────
# TOOL 6 — run_command : verification
#
# Deliberately generic. Making this `run_tests` would hide the interesting
# problem — working out HOW this repo runs its tests is part of the job.
# ─────────────────────────────────────────────

RUNNER_MARKERS = ["pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
                  "package.json", "Makefile", "go.mod", "Cargo.toml", "build.gradle"]

FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def _truncate(text: str, head: int = 40, tail: int = 80) -> str:
    """Keep the END of the output. A traceback's summary is at the bottom,
    so naive head-truncation throws away the only lines that matter."""
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    omitted = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"... ({omitted} lines omitted from the middle) ..."] + lines[-tail:])


def _progress_note(output: str, returncode: int) -> str:
    """G5 — tell the model whether its last edit actually moved anything."""
    global LAST_FAILURES
    failures = set(FAILED_RE.findall(output))

    if returncode == 0 and not failures:
        note = "\n[HARNESS] Command succeeded (exit 0)."
        if LAST_FAILURES:
            note += f" {len(LAST_FAILURES)} previously failing test(s) now pass."
        LAST_FAILURES = set()
        return note

    if not failures:                       # non-zero, but not a parseable test report
        LAST_FAILURES = None
        return (f"\n[HARNESS] Command failed (exit {returncode}). Read the output above "
                f"before changing anything.")

    note = f"\n[HARNESS] {len(failures)} test(s) failing."
    if LAST_FAILURES is not None:
        fixed = LAST_FAILURES - failures
        broke = failures - LAST_FAILURES
        if failures == LAST_FAILURES:
            note += (" IDENTICAL to the previous run — your last edit changed nothing that "
                     "matters. Do not tweak the same line again; your hypothesis about WHERE "
                     "the bug lives is probably wrong. Go back to searching.")
        else:
            if fixed:
                note += f" Fixed since last run: {', '.join(sorted(fixed))}."
            if broke:
                note += (f" REGRESSION — these passed before your edit and fail now: "
                         f"{', '.join(sorted(broke))}. Check what else depends on what you changed.")
    LAST_FAILURES = failures
    return note


def run_command(command: str, timeout: int = 120) -> str:
    if not command.strip():
        return "ERROR: EmptyCommand"
    try:
        # PYTHONDONTWRITEBYTECODE: Python records the source mtime in the .pyc
        # header as WHOLE SECONDS. An agent that edits a file and re-runs the
        # tests in the same second gets the stale bytecode and sees its own fix
        # do nothing — which then sends it off chasing a bug it already fixed.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(command, shell=True, cwd=str(REPO), capture_output=True,
                              text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return f"ERROR: Timeout: '{command}' exceeded {timeout}s and was killed."

    global RAN_ANYTHING
    RAN_ANYTHING = True
    output = _truncate((proc.stdout + proc.stderr).strip()) or "(no output)"

    # -x / --maxfail stops at the first failure, so the result says nothing
    # about the tests that never ran. Agents reach for it to save time and then
    # read "exit 0" as "everything is fine".
    partial = ""
    if re.search(r"(^|\s)(-x|--exitfirst|--maxfail[= ]\d+)", command):
        partial = ("\n[HARNESS] This run used -x/--maxfail, so it STOPPED at the first "
                   "failure and the remaining tests never executed. This result does not "
                   "tell you the state of the suite. Re-run the full suite before you "
                   "conclude anything.")

    if proc.returncode == 127:
        present = [m for m in RUNNER_MARKERS if (REPO / m).exists()]
        hint = ", ".join(present) if present else "none of the usual ones"
        return (f"exit 127 — command not found.\n{output}\n"
                f"[HARNESS] Build/test markers present in this repo: {hint}. "
                f"Use one of those to work out how this project runs its tests.")

    return f"exit {proc.returncode}\n{output}{_progress_note(output, proc.returncode)}{partial}"



# ─────────────────────────────────────────────
# TOOL 7 — spawn_searcher : a second agent, with its own context
#
# The sub-agent gets the read-only tools and a fresh message list. Whatever it
# reads stays inside its own conversation and is discarded when it returns;
# the main agent only ever sees the final report.
# ─────────────────────────────────────────────

SEARCHER_PROMPT = """You are a code search specialist. Another agent is fixing a bug and
has asked you a question. You can only look at code — you cannot edit or run anything.

Investigate using list_files, search, outline and read_file, then return a SHORT report:

- the file paths and exact line numbers that matter
- what the code at those lines actually does
- anything you checked and ruled out

Your report is going into another agent's limited context, so it must be compact. Do not
paste large blocks of code — cite path:line and describe. Ten lines is a good report;
fifty is a failure. If you looked at nine files and only two are relevant, mention the
two and say the rest were irrelevant."""

SEARCHER_MAX_HOPS = 10


def searcher_tools():
    """Read-only subset. Resolved at call time because TOOLS is defined below."""
    return [t for t in TOOLS if t["function"]["name"] in
            ("list_files", "search", "outline", "read_file")]


def spawn_searcher(question: str) -> str:
    """Run a read-only agent in its own context. Returns only its report."""
    messages = [{"role": "system", "content": SEARCHER_PROMPT},
                {"role": "user", "content": question}]
    calls = 0

    for _ in range(SEARCHER_MAX_HOPS):
        resp = client().chat.completions.create(
            model=MODEL, messages=messages, tools=searcher_tools(), max_tokens=1500)
        choice = resp.choices[0]

        if not choice.message.tool_calls:
            report = choice.message.content or "(no findings)"
            print(f"  │   └─ sub-agent finished: {calls} tool calls, "
                  f"report is {len(report.splitlines())} lines")
            return f"[report from sub-agent, after {calls} tool calls]\n{report}"

        messages.append(choice.message)
        for call in choice.message.tool_calls:
            calls += 1
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": f"ERROR: bad JSON ({e})"})
                continue
            print(f"  │   ├─ [sub] {fmt_call(call.function.name, args)}")
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": execute_tool(call.function.name, args)})

    return "[sub-agent hit its hop limit without reporting]"


# ─────────────────────────────────────────────
# TOOL SCHEMAS
# ─────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": (
            "List files in the repository, recursively. Start here when you do not yet know "
            "the layout. Use `pattern` to narrow by filename (e.g. 'test_*.py', '*.json'). "
            "Build/vendor directories are skipped automatically."),
        "parameters": {"type": "object", "properties": {
            "subdir": {"type": "string", "description": "Directory relative to repo root. Default '.'"},
            "pattern": {"type": "string", "description": "Filename glob, e.g. '*.py'. Default '*'"}},
            "required": []}}},

    {"type": "function", "function": {
        "name": "search",
        "description": (
            "Find where a token appears in the repo. Returns COORDINATES — file paths, or "
            "path:line:text — so you can read only the part that matters. This is your primary "
            "navigation tool: search before you read. "
            "mode='files' (default) returns filenames only and is cheap; mode='content' returns "
            "the matching lines and costs far more context, so use it once you have narrowed down. "
            "Search for identifiers that would appear IN THE CODE (a function or constant name), "
            "not for the user's phrasing — users say 'big orders', code says 'THRESHOLD'."),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Literal text, or a regex if regex=true."},
            "path": {"type": "string", "description": "Directory or file to search. Default '.'"},
            "regex": {"type": "boolean", "description": "Treat pattern as a regex. Default false."},
            "glob": {"type": "string", "description": "Restrict to matching files, e.g. '*.py'."},
            "mode": {"type": "string", "enum": ["files", "content"],
                     "description": "'files' = names only (cheap). 'content' = matching lines."},
            "context": {"type": "integer", "description": "Lines of context around each match."}},
            "required": ["pattern"]}}},

    {"type": "function", "function": {
        "name": "outline",
        "description": (
            "Show the structure of ONE file — every class and function with its exact line range — "
            "without reading the bodies. A 600-line file comes back as about 15 lines. Use this on "
            "any file over ~200 lines before reading it, then read_file the single range you need. "
            "If the file has a syntax error, this reports the line, which is usually the bug."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to repo root."}},
            "required": ["path"]}}},

    {"type": "function", "function": {
        "name": "read_file",
        "description": (
            "Read the contents of a file with line numbers. `offset` is the LINE NUMBER to start "
            "at and `limit` is how many lines to return — use the range that outline gave you "
            "rather than reading everything. For a file under ~200 lines just read it whole. "
            "The footer tells you how much of the file you did not see; never assume you have "
            "seen all of it."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "offset": {"type": "integer", "description": "First line to return (1-based). Default 1."},
            "limit": {"type": "integer", "description": "How many lines. Default 200."}},
            "required": ["path"]}}},

    {"type": "function", "function": {
        "name": "edit_file",
        "description": (
            "Replace an exact piece of text in a file. There is no line-number parameter and "
            "that is deliberate: `old_string` is both the address AND the proof that the file "
            "is still what you think it is. "
            "RULES: you must have read_file the file in this session; `old_string` must match "
            "byte for byte INCLUDING indentation (strip the line-number prefix that read_file "
            "adds); and it must appear exactly once. If it appears several times, quote the "
            "surrounding lines until it is unique — that is almost always the right fix — or "
            "pass line_hint to choose one, or replace_all=true if every occurrence should "
            "change. Make the smallest edit that could work; do not reformat or refactor."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "old_string": {"type": "string", "description": "Exact text to replace, including indentation."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
            "line_hint": {"type": "integer", "description": "If the text appears more than once, pick the occurrence nearest this line."},
            "reason": {"type": "string", "description": "Required ONLY when editing a test file: why the test itself is wrong."}},
            "required": ["path", "old_string", "new_string"]}}},

    {"type": "function", "function": {
        "name": "run_command",
        "description": (
            "Run a shell command in the repository root and get back the exit code and output. "
            "This is how you verify — an edit you have not run is a guess. Use it to run the "
            "test suite, a single test, a linter, or anything else. "
            "Work out how THIS repo runs its tests by looking at what is in the root "
            "(pytest.ini, package.json, Makefile, go.mod...) rather than assuming. "
            "After each run the harness tells you whether the set of failing tests changed, "
            "so read that line before deciding what to do next."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command, e.g. 'python -m pytest -q'."},
            "timeout": {"type": "integer", "description": "Seconds before it is killed. Default 120."}},
            "required": ["command"]}}},

    {"type": "function", "function": {
        "name": "spawn_searcher",
        "description": (
            "Hand an investigation to a second agent that has its own fresh context and the "
            "read-only tools. Ask it a full question in plain English — 'which function decides "
            "the discount tier, and are there other places that do the same comparison?' — and "
            "it comes back with a short report of paths and line numbers. "
            "Use this for any open-ended hunt. Everything it reads stays in ITS context, not "
            "yours, so you get the conclusion without the forty files it opened to reach it. "
            "You still read_file anything you intend to edit — the report is a pointer, not a "
            "substitute for looking."),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "A complete question, with enough background for a fresh agent."}},
            "required": ["question"]}}},
]


SYSTEM_PROMPT = """You are a debugging agent working inside a repository you have never seen. You find the
cause of a failure, fix it, and prove the fix by running the tests.

THE PROTOCOL — follow it in this order:

1. REPRODUCE FIRST. Run the whole test suite before touching anything. An unverified bug
   report is a rumour, and you need to know what already passes so you can tell later
   whether you broke something.
2. LOCALISE. Read the traceback bottom-up, then search for the symbol it names. Do not
   start editing because a file name looks relevant.
3. SMALLEST EDIT THAT COULD WORK. No refactors, no cleanups, no renames along the way.
4. RE-RUN, and read the harness line about whether the failures changed. If the set of
   failures is IDENTICAL, your edit was in the wrong place — go back to step 2 instead of
   adjusting the same line again.
5. SWEEP FOR SIBLINGS. The same mistake is usually copied elsewhere. Once you know the
   faulty pattern, search the repo for it and fix every site.
6. FULL SUITE BEFORE YOU STOP. A fix that breaks a different test is not a fix.
7. DO NOT STOP WHILE THE SUITE IS RED. Your turn ends when the full suite passes. If a
   remaining failure is genuinely unrelated to what you were asked, you may leave it — but
   then say explicitly which test, why it is unrelated, and that you chose not to fix it.
   "I recommend investigating X" is not an acceptable ending if you could have investigated
   X yourself.
8. NEVER MAKE A TEST PASS BY EDITING THE TEST. If you truly believe a test is wrong, say
   so to the user in plain words and explain why.

How you work:

- SEARCH BEFORE YOU READ. You do not know the layout. Never guess a filename — find it.
  Climb the ladder: list_files for structure, search for coordinates, outline for the shape
  of one file, read_file for the few lines that actually matter.
- TRANSLATE THE USER'S WORDS INTO THE CODE'S WORDS. People describe symptoms ("big orders",
  "checkout is wrong"); code contains identifiers (THRESHOLD, apply_discount). Your first
  search should guess the identifier, and if it misses, try the user's word in the TESTS —
  test names are written in human language and they point at the code.
- READ AS LITTLE AS POSSIBLE. A whole-file read is a last resort. Outline first on anything
  substantial, then read one range.
- WORK IN PARALLEL. Independent lookups belong in the SAME response, not one per turn.
  Your first move especially: you can list the repo and fire two or three candidate
  searches simultaneously instead of waiting to see each result. Only go sequential when
  the next step genuinely depends on what came back.
- SWEEP FOR SIBLINGS BEFORE YOU CONCLUDE. A mistake in one place is usually copied
  somewhere else. Once you know the faulty pattern, search the repo for that pattern —
  the comparison, the constant, the helper — and check every other place it appears.
  Reporting one site when there are three is a wrong answer, not a partial one.
- NEVER ASSERT WHAT YOU HAVE NOT READ. If you name a line as the cause, you must have read
  that line in this session. No guessing from a filename.
- A SEARCH RESULT IS A POINTER, NOT A READING. Matching lines tell you where to look; they
  do not tell you what the code is supposed to do. Before concluding about a file, read it.
  The thing that settles the question — a docstring, a comment stating the rule, a constant,
  a test name — is almost never on the line your search matched.
- FINISH THE INVESTIGATION. Never end your turn with "next I should check X" — if there is a
  next step, take it now; you have the tools. You are done only when you can name the file,
  the function, the line, and precisely what is wrong, without hedging. If you genuinely
  cannot determine it, say that plainly and list what you ruled out and how.
- A PARTIAL VIEW IS NOT THE FILE. The harness tells you what you did not see. Believe it.

When you are done, answer in plain English: what was broken, every file and line you
changed, and the final state of the test suite. If anything is still failing, say so
plainly rather than implying success."""


# ─────────────────────────────────────────────
# DISPATCH + LOGGING
# ─────────────────────────────────────────────

def execute_tool(name, args):
    try:
        if name == "list_files":
            return list_files(args.get("subdir", "."), args.get("pattern", "*"))
        if name == "search":
            return search(args["pattern"], args.get("path", "."), args.get("regex", False),
                          args.get("glob"), args.get("mode", "files"), args.get("context", 0))
        if name == "outline":
            return outline(args["path"])
        if name == "read_file":
            return read_file(args["path"], args.get("offset", 1), args.get("limit", 200))
        if name == "edit_file":
            return edit_file(args["path"], args["old_string"], args["new_string"],
                             args.get("replace_all", False), args.get("line_hint"),
                             args.get("reason"))
        if name == "run_command":
            return run_command(args["command"], args.get("timeout", 120))
        if name == "spawn_searcher":
            return spawn_searcher(args["question"])
        return f"ERROR: UnknownTool: {name}"
    except Exception as exc:                      # a tool must never kill the loop
        return f"ERROR: {type(exc).__name__}: {exc}"


def fmt_call(name, args):
    if name == "list_files":
        return f"list_files(subdir={args.get('subdir', '.')!r}, pattern={args.get('pattern', '*')!r})"
    if name == "search":
        extra = f", path={args['path']!r}" if args.get("path", ".") != "." else ""
        extra += f", glob={args['glob']!r}" if args.get("glob") else ""
        return f"search({args['pattern']!r}, mode={args.get('mode', 'files')!r}{extra})"
    if name == "outline":
        return f"outline({args['path']!r})"
    if name == "read_file":
        return f"read_file({args['path']!r}, offset={args.get('offset', 1)}, limit={args.get('limit', 200)})"
    if name == "edit_file":
        extra = ", replace_all=True" if args.get("replace_all") else ""
        extra += f", line_hint={args['line_hint']}" if args.get("line_hint") else ""
        return f"edit_file({args['path']!r}{extra})"
    if name == "run_command":
        return f"run_command({args['command']!r})"
    if name == "spawn_searcher":
        return f"spawn_searcher({args['question'][:70]!r}...)"
    return f"{name}({args})"


def log_result(i, n, result, cap=16):
    lines = result.split("\n")
    print(f"  │ [RESULT {i}/{n}]")
    for ln in lines[:cap]:
        print(f"  │   {ln}")
    if len(lines) > cap:
        print(f"  │   ... ({len(lines) - cap} more lines)")


# ─────────────────────────────────────────────
# THE LOOP
# ─────────────────────────────────────────────

def run_agent(task: str, tools: list) -> tuple:
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    hop = 0

    while True:
        if hop >= MAX_HOPS:
            return "(stopped: hop limit reached)", messages

        response = client().chat.completions.create(
            model=MODEL, messages=messages, tools=tools, max_tokens=2000)
        choice = response.choices[0]

        if not choice.message.tool_calls:
            messages.append(choice.message)
            print(f"\n  [STATS] {hop} hops")
            return choice.message.content, messages

        messages.append(choice.message)
        hop += 1
        calls = choice.message.tool_calls
        n = len(calls)
        print(f"\n  ┌─ HOP {hop} ─ " +
              (f"⇉ PARALLEL: {n} tool calls in ONE response" if n > 1 else "→ 1 tool call"))

        seen = {}                      # (name, args) -> result, within this hop
        for i, call in enumerate(calls, 1):
            name = call.function.name
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": f"ERROR: arguments were not valid JSON ({e}). Resend."})
                print(f"  │ [TOOL {i}/{n}] {name} — MALFORMED ARGUMENTS")
                continue

            print(f"  │ [TOOL {i}/{n}] {fmt_call(name, args)}")
            key = (name, json.dumps(args, sort_keys=True))
            if key in seen:
                result = seen[key]
                print(f"  │   (identical to an earlier call this hop — reusing the result)")
            else:
                result = execute_tool(name, args)
                seen[key] = result
                log_result(i, n, result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        print(f"  └─ HOP {hop} done")



# ─────────────────────────────────────────────
# WHAT IS LEFT IN THE MAIN CONTEXT
#
# Not a token counter — just an honest list of what the model re-reads on
# every subsequent turn. The shape of the list is the lesson.
# ─────────────────────────────────────────────

def context_summary(messages: list) -> None:
    print("\n" + "=" * 70)
    print("   WHAT IS SITTING IN THE MAIN CONTEXT NOW")
    print("=" * 70)
    tool_lines = 0
    for m in messages:
        role = m["role"] if isinstance(m, dict) else m.role
        if role == "tool":
            body = m["content"] if isinstance(m, dict) else m.content
            n = len(body.splitlines())
            tool_lines += n
            head = body.splitlines()[0][:58] if body.splitlines() else ""
            print(f"   tool result  {n:>4} lines   {head}")
        elif role == "assistant":
            calls = (m.get("tool_calls") if isinstance(m, dict) else m.tool_calls) or []
            if calls:
                names = ", ".join(c.function.name for c in calls)
                print(f"   assistant               -> {names}")
    print("-" * 70)
    print(f"   {tool_lines} lines of tool output are now permanent context,")
    print(f"   re-sent to the model on every turn from here on.")
    print("=" * 70)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--mode", choices=["inline", "subagent"], default="subagent",
                    help="inline = the main agent searches for itself (step 2 behaviour); "
                         "subagent = searching is delegated to a fresh context")
    args = ap.parse_args()

    REPO = Path(args.repo).resolve()
    if not REPO.is_dir():
        raise SystemExit(f"not a directory: {REPO}")

    print("=" * 70)
    print("   STEP 3 — CONTEXT ISOLATION")
    print("=" * 70)
    print(f"\n  repo  : {REPO}\n  model : {MODEL}\n  mode  : {args.mode}\n  task  : {args.task}\n")

    if args.mode == "inline":
        # Everything in one context — exactly step 2.
        active = [t for t in TOOLS if t["function"]["name"] != "spawn_searcher"]
    else:
        # The main agent cannot search at all. It must delegate, then read the
        # one file it was pointed at, edit, and verify.
        active = [t for t in TOOLS if t["function"]["name"] in
                  ("spawn_searcher", "read_file", "edit_file", "run_command")]

    answer, messages = run_agent(args.task, active)
    print(f"\n  Agent: {answer}\n")
    context_summary(messages)
