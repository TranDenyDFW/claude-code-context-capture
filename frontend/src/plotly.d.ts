/**
 * `plotly.js-dist-min` is a prebuilt bundle and ships no types.
 *
 * Declared as `unknown` rather than pulled from DefinitelyTyped on purpose. The published
 * `@types/plotly.js` describes a different major version, so it would assert a shape this bundle
 * does not necessarily have, and a type that is confidently wrong is worse than one that is
 * honestly absent. The two calls actually used are narrowed at the import site in `Plot.tsx`; the
 * shape of a FIGURE, which is the part that matters, is `PlotlyFigure` in `api.ts` and comes from
 * the server.
 */
declare module 'plotly.js-dist-min' {
  const Plotly: unknown
  export default Plotly
}
