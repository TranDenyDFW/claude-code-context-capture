"""Check the three new Session-tab features against the store, not against each other.

The audit proves the tables are numeric and reachable. It says nothing about whether the bands sit
at the right heights, whether the budget headroom is the real headroom, or whether the diff totals
match SQL written independently of the code that produced them.
"""
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import app as m  # noqa: E402
import c4x.panels as panels  # noqa: E402
import c4x.store as store  # noqa: E402

failures = []


def check(name, ok, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if not ok:
        failures.append(name)


session_id = m.q("""SELECT session_id FROM compactions GROUP BY 1
                    ORDER BY COUNT(*) DESC LIMIT 1""").iloc[0]["session_id"]
turns = m.session_turns(session_id)
n = len(turns)

# 1. Bands sit at the thresholds the mirror publishes, per segment, and nowhere else.
fig, _ = m.session_view(session_id, "main", 80, (1, n), with_cards=False)
rects = [sh for sh in fig.layout.shapes if sh.type == "rect"]
segs = store.segments_for(session_id).get("segments", [])
windows = [s["window"] for s in segs if s.get("window")]
expected = []
for w in windows:
    t = m.THRESHOLDS[w]
    expected += [(t["warn"], t["compact"]), (t["compact"], t["blocked"]), (t["blocked"], w)]
drawn = sorted((r.y0, r.y1) for r in rects)
verdict = "match" if drawn == sorted(expected) else f"{drawn[:3]} vs {sorted(expected)[:3]}"
check("bands are the published thresholds",
      drawn == sorted(expected),
      f"{len(rects)} rects over {len(windows)} resolved segment(s); {verdict}")

# 2. The budget line is that share of the window, and the headroom it states is the real one.
budget = 80
latest = int(turns["total_resident"].iloc[-1])
# HORIZONTAL dotted lines in the budget colour. The A/B marks are dotted too, and being vertical
# they carry y0=0, which the first version of this check read as a budget target of zero.
dotted = [sh for sh in fig.layout.shapes
          if sh.type == "line" and getattr(sh.line, "dash", None) == "dot"
          and sh.y0 == sh.y1 and getattr(sh.line, "color", None) == m.GOOD]
targets = sorted({round(sh.y0) for sh in dotted})
want = sorted({round(w * budget / 100.0) for w in windows})
note = next((a.text for a in fig.layout.annotations if "budget" in str(a.text)), "")
real_headroom = (windows[-1] * budget / 100.0 - latest) if windows else None
check("budget line is that share of the window", targets == want, f"{targets} vs {want}")
check("stated headroom is the real headroom",
      real_headroom is not None and m.fmt_tokens(abs(real_headroom)) in note,
      f"{note!r} against {m.fmt_tokens(abs(real_headroom))} left at {m.fmt_tokens(latest)}")

# 3. A and B are marked where asked.
fig2, _ = m.session_view(session_id, "main", None, (3, n - 2), with_cards=False)
labels = {a.text: a.x for a in fig2.layout.annotations if a.text in ("A", "B")}
check("A and B are marked at the chosen turns",
      labels.get("A") == 3 and labels.get("B") == n - 2,
      f"{labels} for turns 3 and {n - 2}")

# 4. The diff totals equal SQL written here rather than reused from the app.
a, b = 2, min(n, 40)
ts_a = str(turns["ts"].iloc[a - 1])
ts_b = str(turns["ts"].iloc[b - 1])
spend, tools, targets_df, said = panels.turn_diff(session_id, "main", ts_a, ts_b)
mine = m.q("""SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens),0) AS output,
                     COALESCE(SUM(cache_read_input_tokens),0) AS cache_read
                FROM api_calls
               WHERE session_id = ? AND ts > ? AND ts <= ? AND is_sidechain = 0""",
           (session_id, ts_a, ts_b)).iloc[0]
row = spend.iloc[0]
check("diff spend matches independent SQL",
      int(row["calls"]) == int(mine["calls"]) and int(row["output"]) == int(mine["output"])
      and int(row["cache_read"]) == int(mine["cache_read"]),
      f"calls {int(row['calls'])} vs {int(mine['calls'])}, "
      f"output {int(row['output'])} vs {int(mine['output'])}, "
      f"cache_read {int(row['cache_read'])} vs {int(mine['cache_read'])}")

mine_tools = m.q("""SELECT COUNT(*) AS n, COALESCE(SUM(result_bytes),0) AS b FROM tool_calls
                     WHERE session_id = ? AND ts > ? AND ts <= ? AND is_sidechain = 0""",
                 (session_id, ts_a, ts_b)).iloc[0]
check("diff tool totals match independent SQL",
      int(tools["calls"].sum()) == int(mine_tools["n"])
      and int(tools["result_bytes"].sum()) == int(mine_tools["b"]),
      f"{int(tools['calls'].sum())} calls / {int(tools['result_bytes'].sum())} bytes vs "
      f"{int(mine_tools['n'])} / {int(mine_tools['b'])}")

# 5. The range is exclusive of A and inclusive of B, which is what makes the parts add up.
one = panels.turn_diff(session_id, "main", ts_a, ts_a)[0].iloc[0]
check("an empty range reports nothing", int(one["calls"]) == 0, f"{int(one['calls'])} calls")

# 6. A degenerate range explains itself instead of rendering an empty panel.
panel = m.turn_diff_panel(session_id, "main", turns, 5, 5)
text = str(panel)
check("a collapsed range says so", "Move the two handles" in text, text[:60])

# 7. Direction is in the TEXT. On the live page a fall of 43.4k rendered as "43.4k", the same as a
# rise of 43.4k, with only the colour telling them apart. The range is CHOSEN to fall: starting at
# the high-water mark guarantees it, where a fixed pair of turns gave a rise and never tested this.
peak_at = int(turns["total_resident"].astype(float).idxmax()) + 1
lo, hi = peak_at, len(turns)
drop = (int(turns["total_resident"].iloc[hi - 1] or 0)
        - int(turns["total_resident"].iloc[lo - 1] or 0))
if lo >= hi or drop >= 0:
    check("the delta carries its sign in the text", False,
          "no falling range exists in this session, so this check could not run, "
          "which is a failure rather than a pass")
else:
    text = str(m.turn_diff_panel(session_id, "main", turns, lo, hi))
    signed = "-" + m.fmt_tokens(abs(drop))
    unsigned = m.fmt_tokens(abs(drop))
    check("the delta carries its sign in the text",
          signed in text,
          f"turns {lo} to {hi} fall by {unsigned}; panel shows {signed!r}: {signed in text}")

print()
print("FEATURE CHECKS PASS" if not failures else f"FEATURE CHECKS FAIL: {failures}")
sys.exit(0 if not failures else 1)
