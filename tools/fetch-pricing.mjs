#!/usr/bin/env node
// Keep c4x/prices.json equal to what Anthropic publishes, and fail the build when it drifts.
//
// WHY A PARSER. There is no pricing API. The numbers live on two web pages and nowhere machine
// readable, so the choice is between a parser that runs on every push and a table somebody
// remembers to check. The second one is how a dashboard ends up quoting last year's prices with
// a current-looking date on them.
//
// TWO SOURCES, ON PURPOSE.
//
//   docs      platform.claude.com/docs/en/about-claude/pricing
//             A real <table> with EVERY model, including retired ones. This is what gets parsed
//             into prices.json, because it is the only complete list.
//
//   marketing claude.com/pricing#api
//             Four current models, as prose. The docs page itself says "for the most current
//             pricing information, visit claude.com/pricing", so this is the page that is meant
//             to be authoritative, and it is also the page that cannot supply a full table.
//
// So the docs page provides the numbers and the marketing page audits them. Where both list a
// model they must agree exactly; a disagreement is not reconciled and not averaged, it FAILS,
// because two prices for one model means this tool cannot tell which one is real.
//
// Modes:
//   --check      fetch, parse, compare against c4x/prices.json, exit 1 on any difference (CI)
//   --write      same, then rewrite c4x/prices.json
//   --self-test  parse fixture HTML with no network, including inputs that must be rejected
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TABLE = join(ROOT, 'c4x', 'prices.json');

const DOCS = 'https://platform.claude.com/docs/en/about-claude/pricing';
const MARKETING = 'https://claude.com/pricing';

// "Claude Opus 4.8 (retired, except on Bedrock)" -> "claude-opus-4-8"
//
// The parenthetical is dropped: it is availability, not identity, and a model that retires keeps
// the id its transcripts were written with. Dots become hyphens because that is how the model
// strings appear in the API and therefore in this store: "claude-opus-4-8", never "4.8".
export function modelId(label) {
  const bare = String(label).replace(/\([^)]*\)/g, ' ').trim();
  if (!bare) return null;
  const slug = bare
    .toLowerCase()
    .replace(/[.\s]+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  if (!slug) return null;
  // The marketing page writes "Opus 5" where the docs page writes "Claude Opus 5". One id for
  // both, or the cross-check would compare two disjoint sets and pass by finding no overlap.
  return slug.startsWith('claude-') ? slug : `claude-${slug}`;
}

// "$12.50 / MTok" -> 12.5, and anything that is not a price -> null.
//
// Null rather than 0. A cell this cannot read must stop the run, and 0 is a number that would
// flow into prices.json and out onto the page as "this model is free".
export function money(cell) {
  const text = String(cell).replace(/<[^>]*>/g, ' ').replace(/&[a-z]+;/gi, ' ').trim();
  const match = text.match(/^\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\/\s*MTok)?$/i);
  return match ? Number(match[1]) : null;
}

function cellsOf(rowHtml) {
  return [...rowHtml.matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)]
    .map((m) => m[1].replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim());
}

// The docs table: model, base input, 5m cache write, 1h cache write, cache hit, output.
export function parseDocs(html) {
  const models = {};
  const problems = [];
  for (const row of html.match(/<tr[\s\S]*?<\/tr>/gi) || []) {
    const cells = cellsOf(row);
    if (cells.length < 6) continue;
    if (!/^Claude\s/i.test(cells[0])) continue;
    const id = modelId(cells[0]);
    const input = money(cells[1]);
    const write5m = money(cells[2]);
    const write1h = money(cells[3]);
    const read = money(cells[4]);
    const output = money(cells[5]);
    if (!id || input === null || output === null) {
      problems.push(`unreadable row: ${cells.slice(0, 6).join(' | ')}`);
      continue;
    }
    // The cache columns are checked as RATIOS of the base input price, which is how they are
    // documented. A model that ever priced its cache off that ratio would break every figure on
    // the page silently, because the app derives cache rates from one shared multiplier.
    const ratio = (value) => (value === null || !input ? null : Number((value / input).toFixed(4)));
    models[id] = {
      input,
      output,
      label: String(cells[0]).replace(/\([^)]*\)/g, '').trim(),
      _ratios: { read: ratio(read), write_5m: ratio(write5m), write_1h: ratio(write1h) },
    };
  }
  return { models, problems };
}

// The marketing page is prose, not a table: "Opus 5 <blurb> Input $ 5 / MTok Output $ 25 / MTok".
//
// Tags flatten to SPACES rather than newlines because the page puts the dollar sign, the number
// and the unit in three separate elements. A line-based reading sees "$", "5" and "/ MTok" as
// three unrelated lines and matches nothing at all, which reads on the console as "this page has
// no prices on it" rather than as a broken parser.
//
// The gap between a name and its Input row forbids "MTok", which stops one model's name being
// paired with the next model's prices. Without it the capture runs backwards across the previous
// block and every model after the first is off by one, silently and plausibly.
export function parseMarketing(text) {
  const models = {};
  const flat = String(text).replace(/<[^>]*>/g, ' ').replace(/&[a-z]+;/gi, ' ')
    .replace(/\s+/g, ' ');
  const pattern = /([A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)? [0-9](?:\.[0-9])?) (?:(?!MTok).){0,300}?Input \$ ?([0-9.]+) \/ MTok Output \$ ?([0-9.]+) \/ MTok/g;
  for (const match of flat.matchAll(pattern)) {
    // The name pattern accepts two capitalised words and "MTok" is one, so a match that begins at
    // the tail of the previous block carries it. Trimmed here rather than by tightening the
    // pattern, which would also reject a genuine two-word model name.
    const id = modelId(String(match[1]).replace(/^MTok /, ''));
    if (!id || id === 'claude-mtok') continue;
    models[id] = { input: Number(match[2]), output: Number(match[3]) };
  }
  return models;
}

// Where both pages list a model they must agree. Returns the disagreements, never a resolution.
export function crossCheck(docs, marketing) {
  const clashes = [];
  for (const [id, seen] of Object.entries(marketing)) {
    const known = docs[id];
    if (!known) continue;
    if (known.input !== seen.input || known.output !== seen.output) {
      clashes.push(`${id}: docs say $${known.input}/$${known.output}, ` +
                   `claude.com says $${seen.input}/$${seen.output}`);
    }
  }
  return clashes;
}

export function ratioProblems(models, multipliers) {
  const out = [];
  for (const [id, entry] of Object.entries(models)) {
    for (const [key, expected] of [['read', multipliers.read], ['write_5m', multipliers.write_5m],
                                   ['write_1h', multipliers.write_1h]]) {
      const seen = entry._ratios?.[key];
      if (seen === null || seen === undefined) continue;
      if (Math.abs(seen - expected) > 0.001) {
        out.push(`${id}: ${key} is ${seen}x the input price, not the ${expected}x this app assumes`);
      }
    }
  }
  return out;
}

export function differences(current, fetched) {
  const out = [];
  const ids = new Set([...Object.keys(current), ...Object.keys(fetched)]);
  for (const id of [...ids].sort()) {
    const a = current[id];
    const b = fetched[id];
    if (!a) out.push(`+ ${id}: new, $${b.input}/$${b.output}`);
    else if (!b) out.push(`- ${id}: no longer published (kept: old transcripts still name it)`);
    else if (a.input !== b.input || a.output !== b.output) {
      out.push(`~ ${id}: $${a.input}/$${a.output} -> $${b.input}/$${b.output}`);
    }
  }
  return out;
}

async function grab(url) {
  const response = await fetch(url, { headers: { 'user-agent': 'c4x-pricing-check' } });
  if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
  return response.text();
}

async function main(mode) {
  const table = JSON.parse(readFileSync(TABLE, 'utf8'));
  const [docsHtml, marketingHtml] = await Promise.all([grab(DOCS), grab(MARKETING)]);
  const { models, problems } = parseDocs(docsHtml);
  const count = Object.keys(models).length;
  console.log(`  docs page:      ${count} models parsed`);
  if (problems.length) problems.forEach((p) => console.log(`  UNREADABLE  ${p}`));
  // A page that reformats its table parses to nothing, and nothing compared against a committed
  // table looks exactly like "every model was removed". Refused rather than reported.
  if (count < 4) {
    console.log(`  PRICING FAIL  only ${count} models parsed; the page shape has changed`);
    return 1;
  }
  const marketing = parseMarketing(marketingHtml);
  console.log(`  claude.com:     ${Object.keys(marketing).length} models parsed for cross-check`);
  const clashes = crossCheck(models, marketing);
  const ratios = ratioProblems(models, table.cache_multipliers);
  const drift = differences(table.models, Object.fromEntries(
    Object.entries(models).map(([id, m]) => [id, { input: m.input, output: m.output }])));

  clashes.forEach((c) => console.log(`  DISAGREEMENT  ${c}`));
  ratios.forEach((r) => console.log(`  RATIO CHANGED ${r}`));
  drift.forEach((d) => console.log(`  DRIFT         ${d}`));

  if (clashes.length || ratios.length) {
    console.log('  PRICING FAIL  the two sources disagree, or a cache rate is no longer a ' +
                'multiple this app assumes. Not reconciled here on purpose.');
    return 1;
  }
  if (!drift.length) {
    console.log(`  PRICING PASS  c4x/prices.json matches both sources (${count} models)`);
    return 0;
  }
  if (mode !== '--write') {
    console.log(`  PRICING FAIL  ${drift.length} difference(s). Run: node tools/fetch-pricing.mjs --write`);
    return 1;
  }
  // Models are kept even when the page stops listing one: transcripts already in the store name
  // it, and dropping the entry would turn a priced session into an unpriced one on the next push.
  const merged = { ...table.models };
  for (const [id, entry] of Object.entries(models)) {
    merged[id] = { input: entry.input, output: entry.output, label: entry.label };
  }
  const next = {
    ...table,
    checked: new Date().toISOString().slice(0, 10),
    models: Object.fromEntries(Object.entries(merged).sort(([a], [b]) => a.localeCompare(b))),
  };
  writeFileSync(TABLE, `${JSON.stringify(next, null, 2)}\n`);
  console.log(`  PRICING WRITTEN  ${drift.length} change(s), checked ${next.checked}`);
  return 0;
}

function selfTest() {
  const rows = `
    <table><tr><th>Model</th><th>Base Input Tokens</th><th>5m Cache Writes</th>
    <th>1h Cache Writes</th><th>Cache Hits &amp; Refreshes</th><th>Output Tokens</th></tr>
    <tr><td>Claude Opus 4.8</td><td>$5 / MTok</td><td>$6.25 / MTok</td><td>$10 / MTok</td>
        <td>$0.50 / MTok</td><td>$25 / MTok</td></tr>
    <tr><td>Claude Haiku 4.5</td><td>$1 / MTok</td><td>$1.25 / MTok</td><td>$2 / MTok</td>
        <td>$0.10 / MTok</td><td>$5 / MTok</td></tr>
    <tr><td>Claude Sonnet 4 (retired, except on Bedrock)</td><td>$3 / MTok</td><td>$3.75 / MTok</td>
        <td>$6 / MTok</td><td>$0.30 / MTok</td><td>$15 / MTok</td></tr>
    <tr><td>Claude Broken 9</td><td>see sales</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>
    <tr><td>Some other row</td><td>not a price</td></tr></table>`;
  const parsed = parseDocs(rows);
  const multipliers = { read: 0.1, write_5m: 1.25, write_1h: 2.0 };
  // Shaped like the real page rather than like prose: the dollar sign and the number arrive as
  // separate elements, and the second block follows the first with nothing between them but the
  // trailing "/ MTok" that the name pattern must not swallow.
  const marketingText = '<p>Opus 4.8</p> blurb <span>Input</span> <span>$</span> <span>5</span> ' +
                        '<span>/ MTok</span> <span>Output</span> <span>$</span> <span>25</span> ' +
                        '<span>/ MTok</span> <p>Haiku 4.5</p> blurb <span>Input</span> ' +
                        '<span>$</span> <span>1</span> <span>/ MTok</span> <span>Output</span> ' +
                        '<span>$</span> <span>5</span> <span>/ MTok</span>';
  const wrongRatio = parseDocs(rows.replace('<td>$0.50 / MTok</td>', '<td>$2.50 / MTok</td>'));

  const cases = [
    ['a dotted model name becomes the id the store records',
     modelId('Claude Opus 4.8') === 'claude-opus-4-8'],
    ['the marketing page name maps to the same id',
     modelId('Opus 4.8') === 'claude-opus-4-8'],
    ['a retirement note is not part of the identity',
     modelId('Claude Sonnet 4 (retired, except on Bedrock)') === 'claude-sonnet-4'],
    ['a price parses to a number', money('$12.50 / MTok') === 12.5],
    ['a bare dollar amount parses', money('$5') === 5],
    ['prose does not parse as a price', money('not a price') === null],
    ['an empty cell does not parse as zero', money('') === null],
    ['a range does not parse', money('$5 - $10 / MTok') === null],
    ['three model rows parse and the junk row does not',
     Object.keys(parsed.models).length === 3],
    ['a retired model keeps its own price',
     parsed.models['claude-sonnet-4'].input === 3 && parsed.models['claude-sonnet-4'].output === 15],
    ['a model row whose price will not parse is REPORTED, not dropped silently',
     parsed.problems.length === 1 && parsed.problems[0].includes('Claude Broken 9')],
    ['a row that is not a model row at all is simply skipped, not reported',
     !Object.keys(parsed.models).some((id) => id.includes('some-other'))],
    ['cache ratios matching the assumption raise nothing',
     ratioProblems(parsed.models, multipliers).length === 0],
    ['a cache rate that stops being the assumed multiple is caught',
     ratioProblems(wrongRatio.models, multipliers).length === 1],
    ['the marketing page parses into the same ids',
     Object.keys(parseMarketing(marketingText)).sort().join() ===
       'claude-haiku-4-5,claude-opus-4-8'],
    ['two sources that agree produce no clash',
     crossCheck(parsed.models, parseMarketing(marketingText)).length === 0],
    ['two sources that disagree are NOT reconciled',
     crossCheck(parsed.models, { 'claude-opus-4-8': { input: 9, output: 25 } }).length === 1],
    ['a model only one source lists is not a clash',
     crossCheck(parsed.models, { 'claude-unknown-1': { input: 1, output: 2 } }).length === 0],
    ['an unchanged table reports no drift',
     differences({ 'claude-opus-4-8': { input: 5, output: 25 } },
                 { 'claude-opus-4-8': { input: 5, output: 25 } }).length === 0],
    ['a changed price is drift',
     differences({ 'claude-opus-4-8': { input: 5, output: 25 } },
                 { 'claude-opus-4-8': { input: 6, output: 25 } }).length === 1],
    ['a new model is drift',
     differences({}, { 'claude-opus-4-8': { input: 5, output: 25 } })[0].startsWith('+')],
    ['a model the page drops is reported, not deleted',
     differences({ 'claude-opus-4-8': { input: 5, output: 25 } }, {})[0].startsWith('-')],
    ['the committed table parses and carries its date and source',
     (() => { const t = JSON.parse(readFileSync(TABLE, 'utf8'));
              return /^\d{4}-\d{2}-\d{2}$/.test(t.checked) && t.source.startsWith('http')
                     && Object.keys(t.models).length > 0; })()],
  ];
  let bad = 0;
  for (const [what, ok] of cases) {
    if (!ok) { bad++; console.log(`  FAIL  ${what}`); }
  }
  console.log(bad ? `SELF-TEST FAIL (${bad} of ${cases.length})`
                  : `SELF-TEST PASS (${cases.length} checks)`);
  return bad ? 1 : 0;
}

const mode = process.argv[2] || '--check';
process.exit(mode === '--self-test' ? selfTest() : await main(mode));
