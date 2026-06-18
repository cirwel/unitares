import js from '@eslint/js';
import globals from 'globals';

// The dashboard source is plain browser scripts (IIFEs attaching to `window.*`),
// not ES modules — so cross-file references resolve through the global object and
// `no-undef` would flood with false positives until the module migration lands.
// We keep the genuinely useful bug-catching rules on and let Prettier own
// formatting. Build tooling (vite/eslint/vitest configs) and the test files run
// in Node/ESM and get their own block.
export default [
    {
        ignores: ['node_modules/**', 'dist/**'],
    },
    js.configs.recommended,
    {
        // Browser source modules.
        files: ['*.js'],
        ignores: ['*.config.js', 'src/**'],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'script',
            // Cross-file dashboard globals resolve through `window` at runtime;
            // with `no-undef` off we don't enumerate them here (listing names the
            // files also `var`-declare just produces redeclare noise).
            globals: {
                ...globals.browser,
            },
        },
        rules: {
            // `no-undef` is impractical for the global-IIFE style; re-enable once
            // modules land. Keep the rules that catch real mistakes.
            'no-undef': 'off',
            'no-unused-vars': ['warn', { args: 'none', varsIgnorePattern: '^_' }],
            'no-redeclare': 'warn',
            eqeqeq: ['warn', 'smart'],
            'no-var': 'off', // legacy style; migrate incrementally, don't churn now
        },
    },
    {
        // Build/test tooling — Node + ESM.
        files: ['*.config.js', 'eslint.config.mjs', 'src/**/*.js', 'tests/**/*.js'],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'module',
            globals: { ...globals.node },
        },
        rules: {
            'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
        },
    },
    {
        // Vitest tests get vitest globals via imports; allow browser env too.
        files: ['tests/**/*.js'],
        languageOptions: {
            globals: { ...globals.browser, ...globals.node },
        },
    },
];
