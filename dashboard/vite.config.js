import { defineConfig } from 'vitest/config';

// Single source of truth for both the Vite build and the vitest run.
//
// Build: bundles src/main.js (which side-effect-imports the browser modules in
// load order and vendors Chart.js off the CDN) into dist/. Production serving is
// NOT switched to this output yet — see dashboard/README.md for the migration
// plan. The bundle proves the toolchain end-to-end and is the seam the module
// migration will build on.
//
// Test: the dashboard modules are plain browser IIFEs, so vitest loads them into
// a jsdom global via tests/helpers/load-dashboard.js.
export default defineConfig({
    base: '/dashboard/dist/',
    build: {
        outDir: 'dist',
        emptyOutDir: true,
        rollupOptions: {
            input: 'src/main.js',
            output: {
                entryFileNames: 'assets/[name]-[hash].js',
                chunkFileNames: 'assets/[name]-[hash].js',
                assetFileNames: 'assets/[name]-[hash][extname]',
            },
        },
    },
    test: {
        environment: 'jsdom',
        include: ['tests/**/*.test.js'],
    },
});
