import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dashboardDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

/**
 * Load real dashboard browser scripts into the current jsdom global scope.
 *
 * The modules are IIFEs that read/write bare globals (`DataProcessor`,
 * `DashboardState`, `state`, …) which the browser exposes as `window.*`
 * properties. Running each file through `window.eval` executes it in the jsdom
 * window's global scope, so top-level `var`/`function` declarations and explicit
 * `window.X = …` assignments both land as real globals — matching the browser's
 * script-tag load semantics. We load in dependency order (utils → state →
 * colors → visualizations → components → agents), the same order index.html
 * declares them.
 *
 * @param {string[]} files - script filenames relative to dashboard/, in load order.
 */
export function loadDashboardScripts(files) {
    if (!window.localStorage || typeof window.localStorage.getItem !== 'function') {
        const store = new Map();
        const storage = {
            getItem: (key) => (store.has(String(key)) ? store.get(String(key)) : null),
            setItem: (key, value) => {
                store.set(String(key), String(value));
            },
            removeItem: (key) => {
                store.delete(String(key));
            },
            clear: () => {
                store.clear();
            },
        };
        Object.defineProperty(window, 'localStorage', { value: storage, configurable: true });
    }
    for (const file of files) {
        const src = fs.readFileSync(path.join(dashboardDir, file), 'utf8');
        // Indirect eval via window → runs in global scope, not this function's.
        window.eval(src);
    }
}

/** Default chain needed to render the agent list. */
export const AGENT_LIST_SCRIPTS = [
    'utils.js',
    'state.js',
    'colors.js',
    'visualizations.js',
    'components.js',
    'agents.js',
];
