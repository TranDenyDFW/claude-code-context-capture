"""The compactions tab.

Every compaction, and how far past its own threshold each one fired.
"""
import plotly.graph_objects as go
from dash import dash_table, dcc, html

from c4x.store import COMPACTION_WINDOWS, THRESHOLDS, all_compactions, fit_window
from c4x.theme import (
    BORDER,
    DANGER,
    GOOD,
    MUTED,
    TABLE_STYLE,
    TEXT,
    dark_fig,
    header_help,
    numeric_columns,
)


# ---- Compactions ----------------------------------------------------------
def compactions_layout(session_id=None, scope="main", cohort=None):
    df = all_compactions(session_id, cohort)
    # Window comes from the model segment the compaction sits in. Where segmentation cannot
    # resolve one (no non-sidechain turns recorded around the event), fall back to fitting from
    # the token count alone and SAY SO in the confidence column rather than hiding the weaker
    # basis behind an identical-looking number.
    windows, confidences = [], []
    for uuid, pre in zip(df["uuid"], df["pre_tokens"], strict=True):
        res = COMPACTION_WINDOWS.get(uuid) or {}
        if res.get("window"):
            windows.append(res["window"])
            confidences.append(res.get("confidence", "?"))
        else:
            windows.append(fit_window(int(pre))[0])
            confidences.append("token-fit")
    df["fitted_window"] = windows
    df["confidence"] = confidences
    df["threshold"] = [THRESHOLDS[w]["compact"] for w in df["fitted_window"]]
    df["overshoot"] = df["pre_tokens"] - df["threshold"]

    neg = int((df["overshoot"] < 0).sum())
    fig = go.Figure(go.Scatter(
        x=df["pre_tokens"], y=df["overshoot"], mode="markers",
        marker=dict(size=8, color=[DANGER if o < 0 else GOOD for o in df["overshoot"]],
                    line=dict(color=BORDER, width=1)),
        text=[f"{p} | v{v} | {m}" for p, v, m
              in zip(df["project"], df["version"], df["model"], strict=True)],
        hovertemplate="%{text}<br>pre %{x:,.0f}<br>overshoot %{y:,.0f}<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dash"))
    fig.update_layout(title=f"Overshoot past the predicted trigger ({len(df)} compactions, "
                            f"{neg} below threshold)",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="tokens at compaction", yaxis_title="tokens past threshold")

    show = df.copy()
    show["ts"] = show["ts"].astype(str).str.slice(0, 19).str.replace("T", " ", regex=False)
    cols = ["ts", "project", "model", "version", "trigger", "pre_tokens", "post_tokens",
            "dropped", "survivors", "fitted_window", "confidence", "threshold", "overshoot"]

    return html.Div([
        dcc.Graph(figure=dark_fig(fig, 400), config={"displayModeBar": False}),
        html.Div(
            "Overshoot must be non-negative: a compaction cannot fire below its own threshold, "
            "so red points are the falsifying observations and are worth reading one by one. The "
            "window is resolved from the model segment the compaction sits in; the confidence "
            "column says on what evidence, and token-fit means segmentation could not resolve it.",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "8px 0 14px 0"},
        ),
        dash_table.DataTable(
            id="tbl-compactions",
            columns=(_cols :=
                numeric_columns(cols, {"pre_tokens", "post_tokens", "dropped", "survivors",
                                           "fitted_window", "threshold", "overshoot"})),
            tooltip_header=header_help(_cols),
            # uuid rides along in the data but is not a displayed column, so a click can be traced
            # back to the right compaction even after the user sorts or filters the table.
            data=show[cols + ["uuid"]].to_dict("records"),
            page_size=15, filter_action="native",  # sort comes from TABLE_STYLE
            style_table={"overflowX": "auto"},
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            **TABLE_STYLE,
        ),
        html.Div(
            "Click a row to read the summary it produced, and what it dropped.",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "10px 0 0 0"},
        ),
        html.Div(id="compaction-detail", style={"marginTop": "12px"}),
    ])
