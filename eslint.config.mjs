// Flat config with NO imports, deliberately.
//
// Extending eslint's recommended set would mean a devDependency, a lockfile and a node_modules in a
// repo whose whole install story is that it pulls nothing. The rules below are written out instead,
// so `npx --yes eslint` works with nothing checked in and nothing installed.
//
// Run: npx --yes eslint .

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
