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

WHAT IS AND IS NOT IN THE TABLE BELOW. Only prices that could be stated with confidence are
entered. Every other model this store has seen is deliberately absent, so its cost renders blank
and the page says how many calls that covers. That is a real gap and the app names it on screen
rather than filling it with a plausible guess: a wrong price is worse than no price, because it
is indistinguishable from a right one on the page and it multiplies through billions of tokens.

Adding a model is one entry here. Nothing else in the app needs to change.
"""

# The date the entries below were last checked, and where they must be checked against. Printed on
# the page beside every figure derived from them, so a reader can see how stale the arithmetic is
# without opening this file.
PRICE_TABLE_DATE = "2026-08-30"
PRICE_SOURCE = "https://www.anthropic.com/pricing"

# Multipliers on the model's base INPUT price, rather than separate per-model numbers.
#
# They are published as ratios and they have held across models, so writing them per model would
# invite four numbers per entry where two plus a shared ratio is the same information. If a model
# ever prices its cache differently, it gets explicit cache_read / cache_write keys, which
# price_for() prefers over these.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

# USD per MILLION tokens. Keys are the model strings exactly as the transcripts record them.
PRICES = {
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "output": 5.00,
        "note": "Claude Haiku 4.5, standard tier",
    },
}

# Models this store has seen that are deliberately NOT priced above, and why. Listed rather than
# left implicit so the gap is a documented decision and not an oversight: the page counts these
# and says so, and a reader adding prices knows exactly which entries are missing.
UNPRICED_REASON = (
    "no published price was confirmed for this model when the table was last checked, so its cost "
    "is left blank rather than estimated"
)

# Not a model. The harvest writes this where a transcript recorded usage with no model string, so
# it is excluded from the "you could price this" count: there is nothing to look up.
SYNTHETIC = "<synthetic>"


def price_for(model):
    """The per-million-token prices for a model, or None when the table does not carry it.

    None, not a zeroed dict. Every caller then has to decide what to do about not knowing, which
    is the decision that must not be made by accident.
    """
    if not model:
        return None
    entry = PRICES.get(str(model))
    if not entry:
        return None
    base = float(entry["input"])
    return {
        "input": base,
        "output": float(entry["output"]),
        "cache_read": float(entry.get("cache_read", base * CACHE_READ_MULTIPLIER)),
        "cache_write": float(entry.get("cache_write", base * CACHE_WRITE_MULTIPLIER)),
        "note": entry.get("note", ""),
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
        return (f"Estimated from the price table of {PRICE_TABLE_DATE}, which covers every model "
                f"in this population.")
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
