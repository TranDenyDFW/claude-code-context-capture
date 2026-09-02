// Flat config with NO imports, still deliberately, but no longer for the reason it once was.
//
// The original reason was that extending eslint's recommended set would mean a devDependency, a
// lockfile and a node_modules in a repo whose install story is that it pulls nothing. Half of that
// is now spent: eslint IS a pinned devDependency, because `npx --yes eslint` resolves whatever is
// newest at the moment it runs, so a release could change what lints between two runs of the same
// commit and CI would have no way to tell you.
//
// Writing the rules out is still the right call, for the half that remains. Every plugin added
// here is another package in the lockfile whose own releases decide what this repo considers an
// error, and the rules below are few enough to read in one sitting.
//
// Run: npm run lint  (or `npx eslint .` once `npm ci` has run)

export default [
  {
    // tmp/ holds throwaway sandbox copies of this same tree, so linting it counts every finding
    // several times over and attributes them to files nobody edits.
    ignores: ['tmp/**', 'data/**', 'node_modules/**', '.md/**'],
  },
  {
    files: ['**/*.mjs'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: {
        console: 'readonly',
        process: 'readonly',
        URL: 'readonly',
        TextDecoder: 'readonly',
        TextEncoder: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        structuredClone: 'readonly',
        setInterval: 'readonly',
        clearInterval: 'readonly',
        setImmediate: 'readonly',
        Buffer: 'readonly',
        URLSearchParams: 'readonly',
        AbortController: 'readonly',
        fetch: 'readonly',
      },
    },
    linterOptions: {
      reportUnusedDisableDirectives: true,
    },
    rules: {
      // The ones that catch real mistakes rather than taste.
      'no-undef': 'error',
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-const-assign': 'error',
      'no-dupe-keys': 'error',
      'no-dupe-args': 'error',
      'no-duplicate-case': 'error',
      'no-unreachable': 'error',
      'no-fallthrough': 'error',
      'no-self-compare': 'error',
      'no-unsafe-negation': 'error',
      'use-isnan': 'error',
      'valid-typeof': 'error',
      'no-await-in-loop': 'off',        // sequential I/O is the point in the harvest walk
      'require-atomic-updates': 'error',

      // Shadowing bit this codebase once already, where an inner `db` hid an outer one.
      'no-shadow': 'error',

      eqeqeq: ['error', 'smart'],
      'prefer-const': 'error',
      'no-var': 'error',
    },
  },
  {
    // The audit's fixtures deliberately contain odd shapes.
    files: ['tools/make_fixture.mjs'],
    rules: { 'no-shadow': 'off' },
  },
];
