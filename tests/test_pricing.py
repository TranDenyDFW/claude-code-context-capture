"""The first derived-money figure in an app that otherwise reports only what was recorded.

Nothing in this store holds a cost. `turns` carries every token component and the model name, and
no column of it contains a price, so every dollar on the page is this app doing arithmetic on a
number it got from a file. These checks are about that gap staying visible: an unpriced model must
render BLANK rather than zero, and no cost may appear anywhere without the date of the table it
came from.

Zero is the dangerous value. It is a claim - these calls were free - and it is wrong by exactly
the amount nobody can see, because a column of $0.00 looks like a measurement.
"""
import pytest

from c4x import pricing
from c4x.cli import extract
from c4x.theme import fmt_cost

NOT_IN_THE_TABLE = "claude-imaginary-9-20990101"


# --- the table itself ------------------------------------------------------
def test_the_table_is_dated_and_says_where_it_came_from():
    """A price has a shelf life. A table with no date is a number nobody can age."""
    assert pricing.PRICE_TABLE_DATE
    assert len(pricing.PRICE_TABLE_DATE) == 10 and pricing.PRICE_TABLE_DATE.count("-") == 2
    assert pricing.PRICE_SOURCE.startswith("http")


def test_every_entry_carries_both_prices_and_they_are_positive():
    assert pricing.PRICES, "the price table is empty"
    for model, entry in pricing.PRICES.items():
        assert entry["input"] > 0, f"{model} has no input price"
        assert entry["output"] > 0, f"{model} has no output price"
        assert entry["output"] >= entry["input"], f"{model} prices output below input"


def test_cache_rates_are_derived_from_the_input_price_unless_stated():
    """Published as ratios on the base input price, so they are written once rather than four
    numbers per entry that could drift apart."""
    model = next(iter(pricing.PRICES))
    prices = pricing.price_for(model)
    assert prices["cache_read"] == pytest.approx(
        prices["input"] * pricing.CACHE_READ_MULTIPLIER)
    assert prices["cache_write"] == pytest.approx(
        prices["input"] * pricing.CACHE_WRITE_MULTIPLIER)
    assert prices["cache_read"] < prices["input"] < prices["cache_write"]


# --- the missing-price rule ------------------------------------------------
def test_an_unpriced_model_returns_none_and_never_zero():
    """None, not 0.0. Every caller then has to decide what to do about not knowing, which is the
    decision that must not be made by accident."""
    assert pricing.price_for(NOT_IN_THE_TABLE) is None
    assert pricing.price_for(None) is None
    assert pricing.price_for("") is None
    value = pricing.cost_of(NOT_IN_THE_TABLE, input_tokens=10 ** 9, output_tokens=10 ** 8,
                            cache_read=10 ** 10)
    assert value is None, f"an unpriced model produced {value!r} instead of None"


def test_a_priced_model_costs_what_the_table_says():
    """Recomputed here from the entry, so a change to the arithmetic is caught rather than the
    change to the table being caught by a hard-coded expectation."""
    model = next(iter(pricing.PRICES))
    p = pricing.price_for(model)
    value = pricing.cost_of(model, input_tokens=1_000_000, output_tokens=2_000_000,
                            cache_read=10_000_000, cache_write=500_000)
    expected = (p["input"] + 2 * p["output"] + 10 * p["cache_read"] + 0.5 * p["cache_write"])
    assert value == pytest.approx(expected)


def test_cache_reads_are_not_charged_at_the_input_rate():
    """In this store cache reads are 31.4 BILLION tokens against 53 million of fresh input, so
    charging them as input would overstate the total across almost the whole figure. That is the
    dominant term, not a rounding difference."""
    model = next(iter(pricing.PRICES))
    as_cache = pricing.cost_of(model, cache_read=1_000_000)
    as_input = pricing.cost_of(model, input_tokens=1_000_000)
    assert as_cache < as_input
    assert as_cache == pytest.approx(as_input * pricing.CACHE_READ_MULTIPLIER)


def test_a_total_over_mixed_rows_reports_what_it_left_out():
    """A total that silently skipped its unpriced rows is a smaller number wearing the same
    label. cost_of_rows returns the missing models so the caller can say so beside the figure."""
    model = next(iter(pricing.PRICES))
    rows = [
        {"model": model, "calls": 10, "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"model": NOT_IN_THE_TABLE, "calls": 7, "input_tokens": 10 ** 9, "output_tokens": 10 ** 9,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ]
    total, priced, missing = pricing.cost_of_rows(rows)
    assert priced == 10, "the unpriced row was counted as covered"
    assert total == pytest.approx(pricing.price_for(model)["input"])
    assert missing == {NOT_IN_THE_TABLE: 7}


def test_the_coverage_note_names_the_gap_and_where_to_close_it():
    note = pricing.coverage_note({NOT_IN_THE_TABLE: 4_000}, priced_calls=10)
    assert "INCOMPLETE" in note
    assert "4,000 calls are NOT included" in note
    assert NOT_IN_THE_TABLE in note
    assert "c4x/pricing.py" in note
    assert pricing.PRICE_TABLE_DATE in note


def test_the_synthetic_model_is_not_counted_as_a_missing_price():
    """It is not a model: the harvest writes it where a transcript recorded usage with no model
    string, so there is nothing to look up and listing it as unpriced is noise."""
    note = pricing.coverage_note({pricing.SYNTHETIC: 900}, priced_calls=10)
    assert "INCOMPLETE" not in note
    assert pricing.SYNTHETIC not in note


# --- formatting ------------------------------------------------------------
def test_a_missing_cost_formats_as_nothing_at_all():
    """Not "-", which fmt_tokens uses for a missing MEASUREMENT. A missing cost is a gap in this
    app's knowledge of a price, and "-" reads as "measured, and it was nothing"."""
    assert fmt_cost(None) == ""
    assert fmt_cost(float("nan")) == ""


def test_a_real_zero_is_still_a_zero():
    """Distinguished from missing: a priced model with no tokens really did cost nothing."""
    assert fmt_cost(0) == "$0.00"
    assert fmt_cost(0.0) == "$0.00"


def test_sub_cent_amounts_do_not_pretend_to_six_decimal_places():
    assert fmt_cost(0.0004) == "<$0.01"
    assert fmt_cost(0.01) == "$0.01"
    assert fmt_cost(12.345) == "$12.35"
    # Four figures drop the cents: they are noise beside a number that size, and a column mixing
    # "$1,234.56" with "$0.02" cannot be scanned. 1234.6 rather than 1234.5 on purpose, because
    # Python rounds a tie to even and the expectation would be about that rather than about this.
    assert fmt_cost(1234.6) == "$1,235"
    assert fmt_cost(999.99) == "$999.99"


# --- what reaches the page -------------------------------------------------
@pytest.fixture(scope="module")
def cost_pane(pane, has_store):
    return pane("tab-cost")


def test_the_cost_tab_prints_the_price_table_date(cost_pane):
    """A cost figure with no visible price-table date is a failure. The reader cannot otherwise
    tell a figure derived from today's prices from one derived from last year's."""
    said = extract.all_words(cost_pane)
    assert pricing.PRICE_TABLE_DATE in said
    assert pricing.PRICE_SOURCE in said or "pricing.py" in said


def test_an_unpriced_row_carries_no_number_at_all(cost_pane):
    """The plan's rule, checked on the rendered rows: blank, never zero."""
    tables = [t for t in extract.tables(cost_pane) if "est_usd" in t["columns"]]
    assert tables, "the Cost tab renders no per-model cost table"
    rows = tables[0]["rows"]
    assert rows, "the cost table is empty"
    unpriced = [r for r in rows if not r.get("priced")]
    if not unpriced:
        pytest.skip("every model in this store is priced")
    for row in unpriced:
        value = row.get("est_usd")
        assert value is None or (isinstance(value, float) and value != value), (
            f"{row['model']} has no price and rendered {value!r}, which reads as a measurement")


def test_a_priced_row_carries_a_number_that_matches_the_table(cost_pane):
    tables = [t for t in extract.tables(cost_pane) if "est_usd" in t["columns"]]
    priced = [r for r in tables[0]["rows"] if r.get("priced")]
    if not priced:
        pytest.skip("no model in this store is priced")
    for row in priced:
        expected = pricing.cost_of(row["model"], row["input_tokens"], row["output_tokens"],
                                   row["cache_read_input_tokens"],
                                   row["cache_creation_input_tokens"])
        assert row["est_usd"] == pytest.approx(round(expected, 2))


def test_the_session_card_says_not_priced_rather_than_zero(pane, q, has_store):
    """A card reading $0.00 says the session was free. An empty one says this app does not know."""
    from c4x.tabs.session import session_view
    unpriced = q("""SELECT session_id FROM api_calls
                     WHERE model IS NOT NULL AND model NOT IN ({})
                     GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1""".format(
                         ",".join("?" * len(pricing.PRICES))), tuple(pricing.PRICES))
    if unpriced.empty:
        pytest.skip("every session in this store runs a priced model")
    _figure, cards = session_view(unpriced.iloc[0]["session_id"], "main")
    texts = extract.texts(cards)
    assert "estimated cost" in texts
    value = texts[texts.index("estimated cost") + 1]
    assert value == "not priced", f"an unpriced session's cost card reads {value!r}"
    assert "0.00" not in value and "$0" not in value


def test_a_priced_session_card_carries_the_table_date(q, has_store):
    from c4x.tabs.session import session_view
    priced = q("""SELECT session_id FROM api_calls WHERE model IN ({})
                   GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1""".format(
                       ",".join("?" * len(pricing.PRICES))), tuple(pricing.PRICES))
    if priced.empty:
        pytest.skip("no session in this store runs a priced model")
    _figure, cards = session_view(priced.iloc[0]["session_id"], "all")
    texts = extract.texts(cards)
    at = texts.index("estimated cost")
    assert texts[at + 1].startswith("$"), f"a priced session shows {texts[at + 1]!r}"
    assert pricing.PRICE_TABLE_DATE in texts[at + 2], "the card omits the price table date"


def test_the_compare_table_labels_cost_as_an_estimate(has_store):
    """It is the one row there that is not a reading, and it sits among twelve that are."""
    from c4x.panels import COMPARE_ROWS
    row = [r for r in COMPARE_ROWS if r[0] == "cost_usd"]
    assert row, "the compare table carries no cost row"
    assert "estimate" in row[0][2], "the cost row's unit does not say it is an estimate"
    assert pricing.PRICE_TABLE_DATE in row[0][2], "the cost row's unit omits the table date"
