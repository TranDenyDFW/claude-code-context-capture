# frontend

The React presentation layer. It reads the API and draws it; it contains no SQL, no window
arithmetic and no pricing, because all of that already exists in `c4x/` and having a second copy in
TypeScript would mean two answers to every question.

## Running it

Two processes. The API serves the data, this serves the page.

```bash
python -m c4x.api
```

```bash
npm install --prefix frontend
npm run dev --prefix frontend
```

Then <http://localhost:5173>. Vite proxies everything under `/api` to `127.0.0.1:8059`, so the
browser sees one origin and there is no CORS preflight in development.

## Checks

```bash
npm run typecheck --prefix frontend
npm run test --prefix frontend
```

Both also run as part of `node tools/run_tests.mjs` at the repo root, which is the single command
that runs everything. They are skipped there, loudly, if `frontend/node_modules` is absent.

## How it is put together

The server does not send a page, it sends a description of one:
`{tables: [...], figures: [...], plotly: [...], text: [...]}`, which is exactly what
`c4x/cli/extract.py::describe()` has always returned. That is why `src/components/Pane.tsx` renders
every tab with no per-tab code, and why `tools/parity.py` can prove the API and the old dashboard
say the same thing. A page built by hand per tab would have neither property.

Charts are drawn by **Plotly**, not by a React charting library, because the API already sends
finished Plotly figures. Rebuilding them elsewhere would mean re-deriving anomaly bands,
per-segment zones, compaction markers, budget lines, treemaps and heat-shaded cells by hand, and
every one of those is a chance for the new chart to say something slightly different from the old
one. Plotly is loaded on demand: it is 4 MB of the bundle, and a tab with no charts never pays for
it.

`src/components/Pane.test.tsx` is the last link in the verification chain. `tools/parity.py` proves
the API agrees with the dashboard; that file proves the DOM agrees with the payload. Without the
second link, "the backends agree" would say nothing about what a reader actually sees, which is not
hypothetical: the sweep that prompted it caught the Cost tab rendering one table where the payload
had six.
