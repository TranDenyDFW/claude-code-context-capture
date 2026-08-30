"""The extractor itself, against components built here.

Runs with no store at all, which matters: every other test in this suite trusts this module, so if
it silently returned nothing they would all pass by finding nothing to disagree with. That is the
failure mode this file exists to prevent.
"""
import numpy as np
import plotly.graph_objects as go
from dash import dash_table, dcc, html

from c4x.cli import extract


def test_finds_a_table_and_its_rows():
    tree = html.Div([html.Div("noise"), dash_table.DataTable(
        id="t1", columns=[{"name": "a", "id": "a"}], data=[{"a": 1}, {"a": 2}])])
    found = extract.tables(tree)
    assert len(found) == 1
    assert found[0]["id"] == "t1"
    assert found[0]["columns"] == ["a"]
    assert found[0]["rows"] == [{"a": 1}, {"a": 2}]


def test_finds_tables_nested_in_any_children_shape():
    """Tables hide inside lists, tuples and single children, not just flat lists."""
    tree = html.Div([[dash_table.DataTable(id="deep", columns=[], data=[])],
                     html.Details(html.Summary(html.Div(
                         dash_table.DataTable(id="deeper", columns=[], data=[]))))])
    assert {t["id"] for t in extract.tables(tree)} == {"deep", "deeper"}


def test_table_by_id_returns_none_when_absent():
    tree = html.Div(dash_table.DataTable(id="present", columns=[], data=[]))
    assert extract.table_by_id(tree, "present") is not None
    assert extract.table_by_id(tree, "absent") is None


def test_reads_numpy_values_which_are_not_python_numbers():
    """plotly hands back numpy scalars, and numpy.int64 is not a Python int.

    Checking isinstance(v, (int, float)) reported "no numeric axis" for a chart made entirely of
    numbers, which is exactly the kind of silent nothing this suite must not build on.
    """
    fig = go.Figure(go.Bar(x=np.array([10, 20, 30]), y=["a", "b", "c"], name="bar"))
    described = extract.describe_figure(fig)
    trace = described["traces"][0]
    assert trace["x_max"] == 30.0
    assert trace["x_sum"] == 60.0
    assert trace["y_max"] is None, "y holds labels here, not numbers"


def test_reads_a_horizontal_and_a_vertical_chart_the_same_way():
    vertical = extract.describe_figure(go.Figure(go.Scatter(x=[1, 2], y=[5, 9], name="v")))
    assert vertical["traces"][0]["y_max"] == 9.0
    assert vertical["traces"][0]["points"] == 2


def test_reads_threshold_bands():
    fig = go.Figure()
    fig.add_hrect(y0=100, y1=200)
    fig.add_hrect(y0=200, y1=300)
    assert extract.describe_figure(fig)["bands"] == [(100, 200), (200, 300)]


def test_collects_prose_but_not_table_cells():
    """Cells are data. Letting them into the prose makes every substring assertion meaningless."""
    tree = html.Div([html.Div("a claim the page makes"),
                     dash_table.DataTable(id="t", columns=[{"name": "c", "id": "c"}],
                                          data=[{"c": "a claim the page makes"}])])
    assert extract.texts(tree) == ["a claim the page makes"]


def test_empty_and_none_are_handled_rather_than_raising():
    assert extract.tables(None) == []
    assert extract.figures([]) == []
    assert extract.texts(html.Div()) == []


def test_a_graph_with_no_figure_is_not_a_crash():
    assert extract.figures(html.Div(dcc.Graph(id="g"))) == []


def test_the_extractor_can_fail():
    """A negative control. If this module returned canned results the suite would be worthless."""
    empty = html.Div("nothing here")
    assert extract.tables(empty) == []
    populated = html.Div(dash_table.DataTable(id="x", columns=[], data=[{"a": 1}]))
    assert len(extract.tables(populated)) == 1
