import { defineConfig } from 'vitest/config';

// The dashboard ships as plain browser scripts (IIFEs attaching to `window.*`),
// not ES/CommonJS modules, so tests run them inside a jsdom global scope via the
// loader in tests/helpers/load-dashboard.js. Keep this config minimal.
export default defineConfig({
    test: {
        environment: 'jsdom',
        include: ['tests/**/*.test.js'],
    },
});
