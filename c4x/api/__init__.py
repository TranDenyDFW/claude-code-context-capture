"""HTTP access to what the dashboard renders.

The store has been reachable two ways: through the page, and through `python -m c4x.cli`. This adds
a third, so a browser that is not Dash can read the same thing. It exists for the React frontend
and it is useful on its own: `curl` can now ask what the Cost tab says.

WHAT IT SERVES, and why that is not a new implementation. Every tab payload is produced by calling
`app._render_tab`, the same callback the browser dispatches and the CLI already goes through, and
reducing the result with `c4x.cli.extract.describe`. Nothing here re-queries the store or restates
a chart. That is deliberate and temporary: while two frontends exist, they must not be able to
disagree, and the cheapest way to guarantee that is to have one of them be the other's source. When
Dash retires, this is where the queries move to.

The consequence, stated rather than discovered later: this API is exactly as fast as the Dash render
it wraps, plus serialisation. Phase 3 caches these payloads; nothing here is fast yet.
"""
