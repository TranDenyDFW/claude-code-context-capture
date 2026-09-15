"""The Summary tab: the findings it asserts, and the totals it reports.

Every row here is a claim about the store made in prose, which is the easiest kind of number to get
wrong and the hardest to notice. Each finding names a session and quotes figures, so the session
must exist and the figures must be recomputable.
"""
import re

import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-summary")


def findings_table(body):
    for table in extract.tables(body):
        if "finding" in (table["columns"] or []):
            return table
    return None


def test_every_finding_carries_evidence_and_an_action(body):
    """A finding with no action is an observation, and this tab promises actions."""
    table = findings_table(body)
    assert table is not None, "no findings table on the Summary tab"
    assert table["rows"], "the tab claims findings and rendered none"
    for row in table["rows"]:
        assert str(row.get("finding", "")).strip(), row
        assert str(row.get("evidence", "")).strip(), f"no evidence for {row.get('finding')}"
        assert str(row.get("do this", "")).strip(), f"no action for {row.get('finding')}"


def test_the_count_it_states_is_the_count_it_renders(body):
    table = findings_table(body)
    text = "\n".join(extract.texts(body))
    assert f"{len(table['rows'])} finding" in text, (
        f"{len(table['rows'])} rows rendered, and the tab states something else")


def test_every_identifier_a_finding_quotes_resolves_to_something(body, q):
    """A finding that cites an id nothing can be looked up by is a stale claim.

    Not every hex token is a session: the MCP finding quotes SERVER uuids, which live in
    tool_calls.server_name and are not in `sessions` at all. Treating them as session prefixes was
    a wrong test, not a wrong page, so both namespaces are accepted and an id belonging to neither
    is the failure.
    """
    table = findings_table(body)
    sessions = {sid[:8] for sid in q("SELECT session_id FROM sessions")["session_id"]}
    servers = {str(s)[:8] for s in
               q("SELECT DISTINCT server_name FROM tool_calls WHERE server_name IS NOT NULL")
               ["server_name"]}
    known = sessions | servers
    quoted = set()
    for row in table["rows"]:
        quoted |= set(re.findall(r"\b[0-9a-f]{8}\b", str(row.get("evidence", ""))))
    unknown = {p for p in quoted if p not in known}
    assert not unknown, (
        f"findings cite ids that are neither a session nor an MCP server: {sorted(unknown)[:4]}")


def test_the_tab_says_it_describes_the_whole_store(body):
    """Every other tab answers to the header selection. This one does not, and must say so, or its
    numbers get read as belonging to whatever is selected."""
    text = "\n".join(extract.texts(body))
    assert "WHOLE store" in text or "whole store" in text


def test_the_selection_does_not_change_this_tab(pane, session_id, cohort, has_store):
    """The claim above, tested rather than trusted."""
    plain = findings_table(pane("tab-summary"))
    selected = findings_table(pane("tab-summary", session=session_id, coh=cohort))
    assert plain["rows"] == selected["rows"], (
        "the Summary tab changed with the selection, which contradicts what it states")


def test_the_project_chart_sums_to_the_stores_resident_tokens(body, q):
    """The bar chart is a top-15, so it cannot exceed the total and must not be empty."""
    figures = extract.figures(body)
    assert figures, "no chart on the Summary tab"
    charted = max((t["x_sum"] or t["y_sum"] or 0) for f in figures for t in f["traces"])
    total = float(q("SELECT COALESCE(SUM(total_resident),0) AS n FROM api_calls").iloc[0]["n"])
    assert charted > 0, "the chart plots nothing"
    assert charted <= total * 1.01, (
        f"the top-15 chart plots {charted:,.0f} against a store total of {total:,.0f}")



# THE CACHE COST OF AN ACCOUNT SWITCH, on a store built for it: harvest's account_log dates the
# switch, and the first call of a chat after it that reads no cache and writes its whole context
# again is the cost. An expired cache, a compaction, and a machine that never switched are not.
import sqlite3

from tests.test_projects import build_store, forget_cached_rows

SWITCH = "2026-09-15T12:00:00.000Z"


def _turn(con, sid, n, ts, resident, cread, ccreate, eph_1h=0):
    con.execute(
        """INSERT INTO turns (uuid,session_id,ts,model,request_id,input_tokens,
             cache_creation_input_tokens,cache_read_input_tokens,output_tokens,thinking_tokens,
             eph_1h,eph_5m,service_tier,total_resident,is_sidechain,file_path,line_no,parent_uuid)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"{sid}-x{n}", sid, ts, "claude-opus-5", f"req-{sid}-x{n}", 1, ccreate, cread, 4, 0,
         eph_1h, 0 if eph_1h else 1, "standard", resident, 0, rf"C:\t\{sid}.jsonl", 100 + n, None))


def _switch_store(tmp_path, monkeypatch, log_rows=2):
    from c4x import store
    path = build_store(tmp_path / "switch.db")
    con = sqlite3.connect(str(path))
    rows = [("2026-09-15T10:00:00.000Z", "2026-09-15T10:00:00.000Z", "acct-1"),
            ("2026-09-15T12:03:00.000Z", SWITCH, "acct-2")][:log_rows]
    for seen, switched, account in rows:
        con.execute("INSERT INTO account_log (seen_at, switched_at, account, org, source) "
                    "VALUES (?, ?, ?, 'org', 'config.json')", (seen, switched, account))
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    return path


class TestTheCacheCostOfASwitch:
    def test_a_rewrite_right_after_a_switch_is_counted(self, tmp_path, monkeypatch):
        from c4x.tabs.summary import decisions, switch_rewrites
        path = _switch_store(tmp_path, monkeypatch)
        con = sqlite3.connect(str(path))
        _turn(con, "s0-0", 1, "2026-09-15T11:58:00.000Z", 300_000, 290_000, 5_000)
        _turn(con, "s0-0", 2, "2026-09-15T12:01:00.000Z", 300_000, 0, 295_000)
        _turn(con, "s0-0", 3, "2026-09-15T12:02:00.000Z", 300_000, 295_000, 1_000)
        con.commit()
        con.close()
        forget_cached_rows()
        got = switch_rewrites()
        assert got["calls"] == 1 and got["tokens"] == 295_000 and got["worst"] == "s0-0"
        assert got["sessions"] == ["s0-0"] and got["switches"] == 1
        finding = [d for d in decisions() if "account switch" in d["finding"]]
        assert len(finding) == 1 and finding[0]["session_id"] == "s0-0"
        assert "295.0k" in finding[0]["evidence"] or "295k" in finding[0]["evidence"]
        assert finding[0]["goes to"] == "tab-session"

    def test_an_expired_cache_is_not_a_switch(self, tmp_path, monkeypatch):
        """The previous call was ten minutes before, past the five-minute lifetime it asked for:
        the rewrite would have happened with no switch at all."""
        from c4x.tabs.summary import switch_rewrites
        path = _switch_store(tmp_path, monkeypatch)
        con = sqlite3.connect(str(path))
        _turn(con, "s0-0", 1, "2026-09-15T11:50:00.000Z", 300_000, 290_000, 5_000)
        _turn(con, "s0-0", 2, "2026-09-15T12:01:00.000Z", 300_000, 0, 295_000)
        con.commit()
        con.close()
        forget_cached_rows()
        assert switch_rewrites()["calls"] == 0

    def test_a_one_hour_cache_still_alive_is_counted(self, tmp_path, monkeypatch):
        from c4x.tabs.summary import switch_rewrites
        path = _switch_store(tmp_path, monkeypatch)
        con = sqlite3.connect(str(path))
        _turn(con, "s0-0", 1, "2026-09-15T11:20:00.000Z", 300_000, 290_000, 5_000, eph_1h=1)
        _turn(con, "s0-0", 2, "2026-09-15T12:01:00.000Z", 300_000, 0, 295_000)
        con.commit()
        con.close()
        forget_cached_rows()
        assert switch_rewrites()["calls"] == 1

    def test_a_compaction_is_not_a_rewrite(self, tmp_path, monkeypatch):
        """The new context is a fraction of the old: what a compaction writes."""
        from c4x.tabs.summary import switch_rewrites
        path = _switch_store(tmp_path, monkeypatch)
        con = sqlite3.connect(str(path))
        _turn(con, "s0-0", 1, "2026-09-15T11:58:00.000Z", 300_000, 290_000, 5_000)
        _turn(con, "s0-0", 2, "2026-09-15T12:01:00.000Z", 70_000, 0, 70_000)
        con.commit()
        con.close()
        forget_cached_rows()
        assert switch_rewrites()["calls"] == 0

    def test_a_machine_that_never_switched_has_no_finding(self, tmp_path, monkeypatch):
        from c4x.tabs.summary import decisions, switch_rewrites
        path = _switch_store(tmp_path, monkeypatch, log_rows=1)
        con = sqlite3.connect(str(path))
        _turn(con, "s0-0", 1, "2026-09-15T11:58:00.000Z", 300_000, 290_000, 5_000)
        _turn(con, "s0-0", 2, "2026-09-15T12:01:00.000Z", 300_000, 0, 295_000)
        con.commit()
        con.close()
        forget_cached_rows()
        assert switch_rewrites()["calls"] == 0
        assert not [d for d in decisions() if "account switch" in d["finding"]]
