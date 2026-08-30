"""What the recorded tokens would have cost, and the price table that says so.

THE FIRST DERIVED-MONEY FIGURE IN THIS APP. Everything else here reports something the transcripts
actually recorded. A cost is not recorded anywhere: `turns` carries every token component and the
model name, and no column of it contains a price. So a cost figure is this app doing arithmetic on
a number it went and got from somewhere else, and the whole design of this module is about that
gap being visible rather than papered over.

Three rules, all of them load-bearing:

1. A model with NO ENTRY renders BLANK, never zero. Zero is a claim - it says these calls were
   free - and it is the wrong claim by exactly the amount nobody can see. Blank says "this app
   does not know", which is true. The same discipline as `not loaded` on the Window tab, which
   renders blank rather than 0 for a kind that carries no residency flag.
2. Every price carries the date it was read and where from, and the page prints that date beside
   every figure derived from it. A price table with no date is a number with no shelf life.
3. The table is COMMITTED, in source, in one place. Not fetched at runtime, not configurable
   through the UI, not cached from an API. Anyone reading a cost on this page can open this file
   and see the exact numbers it came from, which is the same standard the SQL accordions hold
   every table on the page to.

WHERE THE NUMBERS CAME FROM. Every row below was read off the published pricing table at the URL
in PRICE_SOURCE on the date in PRICE_TABLE_DATE, and nothing was inferred. A model the page does
not list stays out of this table, so its cost renders blank and the app says how many calls that
covers: a wrong price is worse than no price, because it is indistinguishable from a right one on
the page and it multiplies through billions of tokens.

Adding a model is one entry here. Nothing else in the app needs to change.

TWO REASONS EVERY FIGURE HERE IS A LOWER BOUND, both because the store records less than the
price list distinguishes:

1. Cache writes are billed by TTL: 1.25x the base input rate for a five-minute cache and 2x for a
   one-hour one. `cache_creation_input_tokens` records the tokens and not which TTL bought them,
   so the cheaper rate is used. A session running a one-hour cache is undercharged on that
   component by a factor of 1.6.
2. Claude 4.6 and later charge 1.1x under a non-global inference geography, and regional endpoints
   carry a 10% premium. Nothing in the transcripts records either, so the global rate is used.

Both are stated on the page rather than corrected for, which is the same rule the rest of this
app follows: report what was recorded, and name what was not.
"""

# The date the entries below were last checked, and where they must be checked against. Printed on
# the page beside every figure derived from them, so a reader can see how stale the arithmetic is
# without opening this file.
import json
from pathlib import Path

# The committed table, beside this file. JSON rather than a Python literal because a MACHINE keeps
# it current: tools/fetch-pricing.mjs reads the published pricing pages on every push and rewrites
# this file when they move. A dict in source would mean the updater had to edit Python, and an
# updater that edits code is one bad regex away from breaking the app it is keeping honest.
TABLE_PATH = Path(__file__).with_name("prices.json")


def _load():
    """The table, or an empty one that makes every cost render blank.

    A missing or unparseable file must NOT raise. The dashboard would fail to import, which turns
    "this app does not know what these tokens cost" into "this app does not start", and the first
    is the honest consequence of a missing price table.
    """
    try:
        data = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001 - a bad table must not stop the app
        return {"checked": "never", "source": "", "models": {},
                "cache_multipliers": {"read": 0.10, "write_5m": 1.25, "write_1h": 2.00}}
    return data


_TABLE = _load()

PRICE_TABLE_DATE = _TABLE.get("checked") or "never"
PRICE_SOURCE = _TABLE.get("source") or ""
_MULTIPLIERS = _TABLE.get("cache_multipliers") or {}
CACHE_READ_MULTIPLIER = float(_MULTIPLIERS.get("read", 0.10))
CACHE_WRITE_MULTIPLIER = float(_MULTIPLIERS.get("write_5m", 1.25))
CACHE_WRITE_1H_MULTIPLIER = float(_MULTIPLIERS.get("write_1h", 2.00))

# USD per MILLION tokens, keyed by the model id the published table names.
PRICES = _TABLE.get("models") or {}

UNPRICED_REASON = (
    "the published pricing table carried no row for it when this table was last read, so its cost "
    "is left blank rather than estimated"
)

SYNTHETIC = "<synthetic>"


def price_for(model):
    """The per-million-token prices for a model, or None when the table does not carry it.

    None, not a zeroed dict. Every caller then has to decide what to do about not knowing, which
    is the decision that must not be made by accident.
    """
    if not model:
        return None
    name = str(model)
    entry = PRICES.get(name)
    if entry is None:
        # The pricing page names a FAMILY, "Claude Haiku 4.5", and the transcripts record a
        # SNAPSHOT, "claude-haiku-4-5-20251001". Without this the released model that actually ran
        # is unpriced while its family sits in the table one suffix away, and the page reports a
        # gap it does not have.
        #
        # Longest match wins, and only on a hyphen boundary. Matching a bare prefix would let
        # "claude-opus-4" price "claude-opus-45" if such a name ever appeared, which is a wrong
        # price rather than a missing one, and this module treats those very differently.
        candidates = [key for key in PRICES if name.startswith(f"{key}-")]
        if not candidates:
            return None
        entry = PRICES[max(candidates, key=len)]
    base = float(entry["input"])
    return {
        "input": base,
        "output": float(entry["output"]),
        "cache_read": float(entry.get("cache_read", base * CACHE_READ_MULTIPLIER)),
        "cache_write": float(entry.get("cache_write", base * CACHE_WRITE_MULTIPLIER)),
        "note": entry.get("label") or entry.get("note", ""),
    }


def cost_of(model, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0):
    """USD for one model's recorded tokens, or None when the model is not priced.

    Cache reads are counted at their own rate rather than at the input rate. In this store they
    are 31.4 BILLION tokens against 53 million of fresh input, so charging them as input would
    overstate the total by roughly the cache discount across almost the entire figure. That is not
    a rounding difference; it is the dominant term.
    """
    prices = price_for(model)
    if prices is None:
        return None
    return (
        (float(input_tokens or 0) * prices["input"]
         + float(output_tokens or 0) * prices["output"]
         + float(cache_read or 0) * prices["cache_read"]
         + float(cache_write or 0) * prices["cache_write"]) / 1_000_000.0
    )


def cost_of_rows(rows):
    """(total, priced_rows, unpriced_models) over an iterable of per-model token dicts.

    Returns the total for the rows it COULD price and the set of models it could not, so a caller
    can render a figure and, beside it, say what the figure leaves out. A total that silently
    skipped its unpriced rows would be a smaller number wearing the same label.
    """
    total, priced, missing = 0.0, 0, {}
    for row in rows:
        model = row.get("model")
        value = cost_of(model,
                        row.get("input_tokens"), row.get("output_tokens"),
                        row.get("cache_read_input_tokens"),
                        row.get("cache_creation_input_tokens"))
        if value is None:
            missing[model] = missing.get(model, 0) + int(row.get("calls") or 0)
            continue
        total += value
        priced += int(row.get("calls") or 0)
    return total, priced, missing


def coverage_note(missing, priced_calls):
    """One sentence naming what the cost figures above it do not cover.

    Written here rather than at each call site so the three places that show a cost cannot drift
    into describing the same gap three different ways.
    """
    real = {model: calls for model, calls in missing.items() if model != SYNTHETIC}
    if not real:
        # The SOURCE belongs in both branches. It used to appear only when coverage was
        # incomplete, so completing the table removed the one link that said where the money came
        # from: the better the coverage got, the less the page explained itself.
        return (f"ESTIMATE. Priced from the table in c4x/prices.json, read from {PRICE_SOURCE} on "
                f"{PRICE_TABLE_DATE}, which covers every model in this population. Cache writes "
                f"are charged at the five-minute rate and no inference-geography premium is "
                f"applied, because the store records neither, so this is a LOWER BOUND.")
    names = ", ".join(f"{model} ({calls:,} calls)"
                      for model, calls in sorted(real.items(), key=lambda kv: -kv[1])[:4])
    more = len(real) - 4
    return (f"ESTIMATE, and an INCOMPLETE one. Priced from the table of {PRICE_TABLE_DATE} "
            f"({PRICE_SOURCE}), which carries {len(PRICES)} of the models in this population. "
            f"{sum(real.values()):,} calls are NOT included in any figure here, against "
            f"{priced_calls:,} that are: {names}"
            + (f", and {more} more" if more > 0 else "")
            + f". They render blank rather than zero, because {UNPRICED_REASON}. Add them in "
              f"c4x/pricing.py and every cost on this page follows.")
