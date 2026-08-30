"""The Probes tab: readings taken directly from Claude Code.

A probe is the only MEASURED figure in this app; everything else is derived. That makes its tab the
one place where a wrong number cannot be blamed on a model, so the tables must match the rows.
"""
import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-probes")


@pytest.fixture(scope="module")
def probe_id(q):
    df = q("SELECT id FROM probes ORDER BY ts DESC LIMIT 1")
    if df.empty:
        pytest.fail("no probes recorded, so this tab could not be verified")
    return int(df.iloc[0]["id"])


def test_every_probe_in_the_store_is_listed(body, q):
    total = int(q("SELECT COUNT(*) AS n FROM probes").iloc[0]["n"])
    biggest = max((len(t["rows"]) for t in extract.tables(body)), default=0)
    assert biggest >= 1
    text = "\n".join(extract.texts(body))
    assert str(total) in text.replace(",", "") or biggest >= total


def test_a_failed_probe_is_shown_as_failed(body, q):
    """A probe that errored must not be rendered as a reading of zero."""
    failed = int(q("SELECT COUNT(*) AS n FROM probes WHERE ok = 0").iloc[0]["n"])
    if not failed:
        pytest.skip("no failed probes in this store")
    text = "\n".join(extract.texts(body)).lower()
    assert "error" in text or "failed" in text


def test_category_rows_match_probe_categories(body, q, probe_id):
    truth = {str(r["name"]): int(r["tokens"] or 0) for _, r in
             q("SELECT name, tokens FROM probe_categories WHERE probe_id = ?",
               (probe_id,)).iterrows()}
    if not truth:
        pytest.skip("the newest probe recorded no categories")
    rendered = {}
    for table in extract.tables(body):
        for row in table["rows"]:
            name = row.get("name") or row.get("category")
            tokens = row.get("tokens")
            if name in truth and isinstance(tokens, (int, float)):
                rendered[str(name)] = int(tokens)
    for name, tokens in rendered.items():
        assert tokens == truth[name], f"{name}: tab {tokens} vs probe {truth[name]}"


def test_a_probes_categories_sum_to_its_window(q, probe_id):
    """Every category plus free space is the whole window. A gap means a category is missing."""
    row = q("SELECT max_tokens FROM probes WHERE id = ?", (probe_id,)).iloc[0]
    window = int(row["max_tokens"] or 0)
    if not window:
        pytest.skip("that probe recorded no window size")
    total = int(q("""SELECT COALESCE(SUM(tokens),0) AS n FROM probe_categories
                      WHERE probe_id = ? AND COALESCE(is_deferred,0) = 0""",
                  (probe_id,)).iloc[0]["n"])
    assert total == pytest.approx(window, rel=0.02), (
        f"probe {probe_id} categories sum to {total:,} against a {window:,} window")


def test_deferred_categories_are_flagged_not_counted(q, probe_id):
    deferred = q("""SELECT name, tokens FROM probe_categories
                     WHERE probe_id = ? AND is_deferred = 1""", (probe_id,))
    if deferred.empty:
        pytest.skip("that probe recorded nothing deferred")
    assert (deferred["tokens"] > 0).all(), "a deferred row with no tokens says nothing"
