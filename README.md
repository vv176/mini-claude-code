# Mini Claude Code

A coding agent in three files, built live for the *Build your own Claude Code* webinar.

You give it a vague complaint — *"some tests are failing around discounts"* — and it
finds the bug in a repo it has never seen, fixes it, runs the tests, reads the failure,
and goes again until the suite is green.

There is **no vector database and no index** anywhere in this project. Discovery is
`grep`, `ls` and `head`, driven by a model that can read the results and write a better
query. The file system is the index; the LLM is the query planner.

---

## Setup

```bash
pip install openai python-dotenv
cp .env.example .env          # then put your OpenAI key in it
```

Ripgrep is used if you have it and a pure-Python fallback runs if you don't, so
`brew install ripgrep` is optional.

## Run it

```bash
python3 plant.py boundary     # creates scratchrepo/ with a bug in it

python3 step1_discovery.py --repo scratchrepo \
  --task "customers are complaining that discounts aren't applying correctly on big orders. find what's wrong."

python3 step2_fix_loop.py --repo scratchrepo \
  --task "several tests are failing. find the bug and fix it."
```

`plant.py` always rebuilds `scratchrepo/` from `scratchrepo.pristine/`, so you can
re-run a demo as many times as you like. Step 2 and 3 **write to disk** — re-plant
before each run. Step 1 is read-only and safe to repeat.

---

## The three steps

| File | Adds | The idea |
|---|---|---|
| `step1_discovery.py` | `list_files`, `search`, `outline`, `read_file` | Finding things without an index |
| `step2_fix_loop.py` | `edit_file`, `run_command` | Closing the loop: edit, verify, retry |
| `step3_subagent.py` | `spawn_searcher` | Keeping the main context clean |

Each file is standalone and cumulative — step 2 is step 1 plus 326 lines. Read any one
of them top to bottom without the others.

Step 3 takes `--mode inline|subagent`. Run the same task both ways and compare the
context summary printed at the end.

### The reading ladder

Every rung costs more context than the one above, so the agent climbs only as far as it
must. A whole-file read is the last resort, not the first move.

```
search      where a token appears  ->  path:line     ~10 lines
outline     the shape of one file  ->  signatures    ~5% of the file
read_file   the actual code        ->  one function
```

`outline` uses Python's `ast`, so line ranges are exact rather than guessed. When a file
has a syntax error it reports the line instead of raising — which is usually the bug.

### Editing is compare-and-swap, not line numbers

`edit_file` has no line-number parameter. `old_string` must match exactly and appear
exactly once. A line number is a pointer; a string is a pointer **plus a checksum** — if
the file moved underneath the agent, the edit fails loudly instead of writing to the
wrong place.

### The guards

Each one exists because the agent actually failed that way during testing, and each
refuses with an instruction rather than just "no".

| Guard | Stops |
|---|---|
| `ReproduceFirst` | editing before you have run anything |
| `ReadBeforeEdit` | editing a file you have not read |
| `StaleRead` | editing a file that changed since you read it |
| `AnchorNotFound` / `AmbiguousAnchor` | an anchor that matches zero or several places |
| `IndentationMismatch` | an under-indented anchor matching mid-line |
| `DontEditTheTest` | making the suite green by changing the test |
| `ProgressSignal` | not noticing that the failures are identical to last run |

`ProgressSignal` is the interesting one: after every run the harness diffs the set of
failing tests and tells the model whether it fixed something, broke something, or
changed nothing at all.

---

## The demo bugs

```bash
python3 plant.py              # list them
python3 plant.py ordering     # plant one
```

| Bug | Why it is interesting |
|---|---|
| `boundary` | One mistake copied into 5 comparisons across 2 files. Fix one and it is still broken. |
| `typeerror` | The traceback points at `cart.py:40`; the cause is 20 lines higher. Two symptoms, one cause. |
| `ordering` | Tax applied before the discount, so tax pushes a 900 order over the 1000 tier. **No keyword to grep for.** |
| `syntax` | A missing colon. The suite will not even collect. |
| `cascade` | A syntax error hiding a second bug. The agent cannot see problem two until problem one is fixed. |

`ordering` is the one to study. `total_payable`, `tax` and `apply_tier_discount` are all
innocent names — search cannot find this. The agent has to read the test docstring,
work out the intended order of operations, and realise that taxing first lets tax carry
an order across a threshold it never earned.

## Tests

```bash
python3 test_step1.py     # tools
python3 test_step2.py     # guards
```

No API key needed — they exercise the tool layer directly.

---

Built for the Agentic AI cohort · [Programming Pathshala](https://programmingpathshala.com)
