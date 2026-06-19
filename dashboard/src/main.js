// Single ES-module entry for the Vite build.
//
// The dashboard's runtime is still a chain of browser IIFEs that attach to
// `window.*` and read each other through the global object. Bundling them as
// ordered side-effect imports preserves that load order exactly (matching the
// <script> tags in index.html) while letting Vite vendor third-party deps off
// the CDN, fingerprint assets, and give us an HMR dev loop. Converting the
// modules to real ESM imports/exports is a follow-up — this entry is the seam
// that makes that migration incremental rather than big-bang.

// --- Vendored third-party deps (previously loaded from jsdelivr CDN) ---
import Chart from 'chart.js/auto';
import 'chartjs-adapter-date-fns';

// The modules reference a bare global `Chart`; expose the bundled one.
window.Chart = Chart;

// --- Dashboard modules, in the same order index.html declares them ---
import '../utils.js';
import '../state.js';
import '../colors.js';
import '../components.js';
import '../fleet-severity.js';
import '../visualizations.js';
import '../agents.js';
import '../discoveries.js';
import '../dialectic.js';
import '../eisv-charts.js';
import '../timeline.js';
import '../residents.js';
import '../fleet-metrics.js';
import '../watcher.js';
import '../sentinel.js';
import '../vigil.js';
import '../system-health.js';
// Loaded at end-of-body in index.html (after the DOM exists).
import '../dashboard.js';
import '../resident-progress.js';
