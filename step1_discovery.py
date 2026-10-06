"""
Step 1: Discovery — the agent loop, and four read-only tools.

This is the whole of "read the repo" from the poster. No editing yet.

The question this file answers:
    Given a vague complaint and a repo it has never seen, how does an agent
    find the exact function that is wrong?

The answer is NOT a vector database. There is no index, no embedding, no
graph. There is a file system and a model that can search it:

    list_files   what exists           -> structure
    search       where a token appears -> coordinates (path:line)
    outline      what is in this file  -> signatures + line ranges, ~5% of it
    read_file    the actual code       -> one function, not 600 lines

The ladder matters. Each rung costs more context than the one above, so the
agent climbs only as far as it has to. A whole-file read is the last resort,
not the first move.

Try:
    python step1_discovery.py --repo scratchrepo \
        --task "something is wrong with discounts on big orders"

Dependencies:
    pip install openai python-dotenv

Run:
    python step1_discovery.py --repo <path> --task "<vague complaint>"
"""

import os
import re
import ast
import json
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
    return f"{body}\n{footer}"


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
]


SYSTEM_PROMPT = """You are a code-reading agent working inside a repository you have never seen.
Your job in this session is to LOCATE things and explain them. You cannot edit anything yet.

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

When you are done, answer in plain English: the file, the function, the exact line, what is
wrong with it, and what the fix would be. Mention every site you found — if the same mistake
appears in more than one place, say so."""


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
    return f"{name}({args})"


def log_result(i, n, result, cap=12):
    lines = result.split("\n")
    print(f"  │ [RESULT {i}/{n}]")
    for ln in lines[:cap]:
        print(f"  │   {ln}")
    if len(lines) > cap:
        print(f"  │   ... ({len(lines) - cap} more lines)")


# ─────────────────────────────────────────────
# THE LOOP
# ─────────────────────────────────────────────

def run_agent(task: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    hop = 0

    while True:
        if hop >= MAX_HOPS:
            return "(stopped: hop limit reached)"

        response = client().chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, max_tokens=2000)
        choice = response.choices[0]

        if not choice.message.tool_calls:
            messages.append(choice.message)
            print(f"\n  [STATS] {hop} hops")
            return choice.message.content

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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task", required=True)
    args = ap.parse_args()

    REPO = Path(args.repo).resolve()
    if not REPO.is_dir():
        raise SystemExit(f"not a directory: {REPO}")

    print("=" * 70)
    print("   STEP 1 — DISCOVERY (read-only)")
    print("=" * 70)
    print(f"\n  repo  : {REPO}\n  model : {MODEL}\n  task  : {args.task}\n")

    print(f"\n  Agent: {run_agent(args.task)}\n")
