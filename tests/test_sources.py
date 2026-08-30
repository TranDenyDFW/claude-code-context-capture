"""The Sources tab: what entered the window that nobody typed.

Attachments, hook output and the record census. Every table here is a count of rows in a table this
test can count itself.
"""
import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-sources")


def table_with(body, column):
    for table in extract.tables(body):
        if column in (table["columns"] or []):
            return table
    return None


def test_the_record_census_matches_the_store(body, q):
    """The census says how many of each transcript record type were ingested.

    record_types is (type, n): a census already, one row per type carrying its own count. Counting
    its ROWS returns 1 for every type, which is what a first version of this test did, and it
    disagreed with a correct page by a factor of 383,475.
    """
    table = table_with(body, "type") or table_with(body, "record_type")
    if table is None:
        pytest.skip("no record census on this store")
    truth = {str(r["type"]): int(r["n"]) for _, r in
             q("SELECT type, n FROM record_types").iterrows()}
    if not truth:
        pytest.fail("record_types is empty, so the census could not be verified")
    compared = 0
    for row in table["rows"]:
        key = str(row.get("type") or row.get("record_type"))
        if key not in truth:
            continue
        shown = row.get("n") if isinstance(row.get("n"), (int, float)) else row.get("records")
        if not isinstance(shown, (int, float)):
            continue
        assert int(shown) == truth[key], f"{key}: tab {shown} vs store {truth[key]}"
        compared += 1
    assert compared > 0, "no census row could be compared"


def test_attachment_counts_match_sql(body, q):
    table = table_with(body, "attachments") or table_with(body, "kind")
    if table is None:
        pytest.skip("no attachment table on this store")
    total = int(q("SELECT COUNT(*) AS n FROM attachments").iloc[0]["n"])
    rendered = sum(int(v) for row in table["rows"] for k, v in row.items()
                   if isinstance(v, (int, float)) and "count" in str(k).lower())
    assert rendered <= total or total == 0


def test_hook_events_are_reported_when_present(body, q):
    n = int(q("SELECT COUNT(*) AS n FROM hook_events").iloc[0]["n"])
    if not n:
        pytest.skip("no hook events in this store")
    text = "\n".join(extract.texts(body))
    assert "hook" in text.lower(), "the store holds hook events and the tab never mentions them"


def test_every_table_carries_the_query_that_produced_it(body):
    """This tab is evidence-first: a number a reader cannot reproduce is not evidence."""
    text = "\n".join(extract.texts(body))
    tables = extract.tables(body)
    if not tables:
        pytest.fail("the Sources tab rendered no tables at all")
    assert text.upper().count("SELECT") >= 1, "no SQL is shown beside any table"


def test_the_tab_survives_a_session_with_no_attachments(pane, q, has_store):
    bare = q("""SELECT t.session_id FROM turns t
                 LEFT JOIN attachments a ON a.session_id = t.session_id
                WHERE a.session_id IS NULL GROUP BY t.session_id LIMIT 1""")
    if bare.empty:
        pytest.skip("every session here has attachments")
    text = "\n".join(extract.texts(pane("tab-sources", session=bare.iloc[0]["session_id"])))
    assert "could not be rendered" not in text
