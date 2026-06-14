/**
 * Dashboard Utilities
 *
 * Core utilities for API calls, error handling, caching, and data processing.
 * Designed for quality, maintainability, and performance.
 */

/**
 * Creates a debounced function that delays invoking fn until after delay ms
 * have elapsed since the last time the debounced function was invoked.
 * @param {Function} fn - Function to debounce
 * @param {number} delay - Delay in milliseconds
 * @returns {Function} Debounced function
 */
function debounce(fn, delay) {
    let timeoutId;
    return function(...args) {
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => fn.apply(this, args), delay);
    };
}

/**
 * Fetch wrapper that adds Authorization header when a token is configured.
 * Use this instead of bare fetch() for authenticated endpoints.
 */
function authFetch(url, options = {}) {
    const token = localStorage.getItem('unitares_api_token') ||
        new URLSearchParams(window.location.search).get('token');
    if (token) {
        options.headers = Object.assign({}, options.headers, {
            'Authorization': `Bearer ${token}`
        });
    }
    const operatorToken = getOperatorToken();
    if (operatorToken) {
        options.headers = Object.assign({}, options.headers, {
            'X-Unitares-Operator': operatorToken
        });
    }
    return fetch(url, options);
}

/**
 * Operator token (X-Unitares-Operator) from localStorage or URL params.
 * Under STRICT_IDENTITY_REQUIRED the server resolves this to a stable
 * operator identity, which is what authorizes the dashboard's write
 * buttons (archive/resume/config-set/dialectic-request). Reads work
 * without it.
 */
function getOperatorToken() {
    return localStorage.getItem('unitares_operator_token') ||
        new URLSearchParams(window.location.search).get('operator_token');
}

/**
 * One-time ?operator_token=... handoff: persist it to localStorage and
 * scrub it from the address bar, so the credential survives reloads
 * without re-entering browser history on every navigation. Private
 * browsing (localStorage throws) keeps the URL fallback untouched.
 */
(function persistOperatorTokenFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get('operator_token');
    if (!fromUrl) return;
    try {
        localStorage.setItem('unitares_operator_token', fromUrl);
    } catch (e) {
        return;
    }
    params.delete('operator_token');
    const qs = params.toString();
    window.history.replaceState(
        null, '',
        window.location.pathname + (qs ? '?' + qs : '') + window.location.hash
    );
})();

class DashboardAPI {
    /**
     * Centralized API client with retry logic, error handling, and caching.
     */
    constructor(baseURL = window.location.origin) {
        this.baseURL = baseURL;
        this.cache = new Map();
        this.cacheTimeout = 25000; // 25 seconds default cache (just under 30s refresh interval)
        this.retryConfig = {
            maxRetries: 3,
            baseDelay: 500,
            maxDelay: 5000
        };
    }

    /**
     * Get API token from localStorage or URL params
     */
    getAuthToken() {
        return localStorage.getItem('unitares_api_token') ||
            new URLSearchParams(window.location.search).get('token');
    }

    /**
     * Get cache key for a tool call
     */
    getCacheKey(toolName, toolArguments) {
        return `${toolName}:${JSON.stringify(toolArguments)}`;
    }

    /**
     * Check if cached data is still valid
     */
    isCacheValid(cacheEntry, cacheTimeout = this.cacheTimeout) {
        if (!cacheEntry) return false;
        return Date.now() - cacheEntry.timestamp < cacheTimeout;
    }

    /**
     * Call a tool via the MCP API with retry logic and caching.
     * 
     * @param {string} toolName - Name of the tool to call
     * @param {object} toolArguments - Tool arguments
     * @param {object} options - Options: {useCache, cacheTimeout, retry}
     * @returns {Promise<object>} Tool result
     */
    async callTool(toolName, toolArguments = {}, options = {}) {
        const {
            useCache = true,
            cacheTimeout = this.cacheTimeout,
            retry = true
        } = options;

        // Ensure toolArguments is an object
        if (!toolArguments || typeof toolArguments !== 'object' || Array.isArray(toolArguments)) {
            toolArguments = {};
        }

        // Check cache
        if (useCache) {
            const cacheKey = this.getCacheKey(toolName, toolArguments);
            const cached = this.cache.get(cacheKey);
            if (this.isCacheValid(cached, cacheTimeout)) {
                return cached.data;
            }
        }

        // Make request with retry logic
        let lastError;
        const maxRetries = retry ? this.retryConfig.maxRetries : 1;

        for (let attempt = 0; attempt < maxRetries; attempt++) {
            try {
                const result = await this._makeRequest(toolName, toolArguments);

                // Cache successful result
                if (useCache) {
                    const cacheKey = this.getCacheKey(toolName, toolArguments);
                    this.cache.set(cacheKey, {
                        data: result,
                        timestamp: Date.now()
                    });
                }

                return result;
            } catch (error) {
                lastError = error;

                // Don't retry on certain errors
                if (error.status === 401 || error.status === 403 || error.status === 404) {
                    throw error;
                }

                // Exponential backoff
                if (attempt < maxRetries - 1) {
                    const delay = Math.min(
                        this.retryConfig.baseDelay * Math.pow(2, attempt),
                        this.retryConfig.maxDelay
                    );
                    await this._sleep(delay);
                }
            }
        }

        throw lastError;
    }

    /**
     * Make HTTP request to tool endpoint
     */
    async _makeRequest(toolName, toolArguments) {
        const headers = {
            'Content-Type': 'application/json',
        };

        const token = this.getAuthToken();
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const operatorToken = getOperatorToken();
        if (operatorToken) {
            headers['X-Unitares-Operator'] = operatorToken;
        }

        // Ensure toolArguments is a plain object
        if (!toolArguments || typeof toolArguments !== 'object' || Array.isArray(toolArguments)) {
            toolArguments = {};
        }

        const requestBody = {
            name: String(toolName || ''),
            arguments: toolArguments
        };

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 30000); // 30s timeout

        try {
            const requestBodyStr = JSON.stringify(requestBody);
            console.log(`[API] Calling ${toolName}:`, {
                url: `${this.baseURL}/v1/tools/call`,
                body: requestBodyStr.substring(0, 200) + (requestBodyStr.length > 200 ? '...' : '')
            });

            const response = await fetch(`${this.baseURL}/v1/tools/call`, {
                method: 'POST',
                headers: headers,
                body: requestBodyStr,
                signal: controller.signal
            });

            clearTimeout(timeoutId);

            const responseText = await response.text();
            console.log(`[API] Response status: ${response.status}`, responseText.substring(0, 200));

            // Detect HTML responses (e.g. proxy error pages, challenge pages)
            const isHTML = responseText.trimStart().startsWith('<!') || responseText.trimStart().startsWith('<html');
            if (isHTML) {
                console.error('[API] Got HTML instead of JSON — likely a proxy interstitial page');
                throw new Error('Server returned an HTML page instead of JSON. If using a tunnel, visit the dashboard URL in your browser first to pass any challenge page.');
            }

            if (!response.ok) {
                let errorMessage = `HTTP ${response.status}: ${response.statusText}`;

                try {
                    const errorData = JSON.parse(responseText);
                    if (errorData.error) {
                        errorMessage = errorData.error;
                    }
                } catch {
                    if (responseText) {
                        errorMessage = responseText.substring(0, 200);
                    }
                }

                const error = new Error(errorMessage);
                error.status = response.status;
                throw error;
            }

            let data;
            try {
                data = JSON.parse(responseText);
            } catch (parseError) {
                console.error('[API] Failed to parse response as JSON:', responseText.substring(0, 200));
                throw new Error(`Invalid JSON response from server: ${parseError.message}`);
            }

            // Check for error in response even if HTTP 200
            if (data.success === false || data.error) {
                const errorMsg = data.error || data.message || 'Tool call failed';
                const error = new Error(errorMsg);
                error.status = data.status || 500;
                throw error;
            }

            // Handle result - could be string JSON or already parsed
            if (typeof data.result === 'string') {
                try {
                    return JSON.parse(data.result);
                } catch (parseError) {
                    console.warn('[API] Result is string but not valid JSON, returning as-is');
                    return data.result;
                }
            }
            return data.result;
        } catch (error) {
            clearTimeout(timeoutId);

            if (error.name === 'AbortError' || error.name === 'TimeoutError') {
                const timeoutError = new Error(`Request timeout after 30s. The server may be overloaded.`);
                timeoutError.status = 504;
                throw timeoutError;
            }

            if (error.message.includes('Failed to fetch') || error.message.includes('NetworkError')) {
                const networkError = new Error(`Network error: Cannot reach server at ${this.baseURL}. Is the server running?`);
                networkError.status = 0;
                throw networkError;
            }

            throw error;
        }
    }

    /**
     * Sleep utility for delays
     */
    _sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    /**
     * Clear cache
     */
    clearCache() {
        this.cache.clear();
    }

    /**
     * Clear cache for specific tool
     */
    clearCacheFor(toolName, toolArguments = {}) {
        const cacheKey = this.getCacheKey(toolName, toolArguments);
        this.cache.delete(cacheKey);
    }
}

class DataProcessor {
    /**
     * Utilities for processing and formatting dashboard data
     */

    /**
     * Calculate trend from current and previous values
     */
    static calculateTrend(current, previous) {
        if (previous === undefined || previous === null || previous === 0) {
            return null;
        }
        const diff = current - previous;
        const percentChange = ((diff / previous) * 100).toFixed(1);
        return {
            diff,
            percentChange: Math.abs(percentChange),
            direction: diff > 0 ? 'up' : diff < 0 ? 'down' : 'neutral',
            isSignificant: Math.abs(percentChange) > 5 // 5% threshold
        };
    }

    /**
     * Categorize drift significance for UI styling
     * @param {number} val - Drift value
     * @returns {string} - 'normal', 'warning', or 'critical'
     */
    static getDriftStatus(val) {
        const abs = Math.abs(val);
        if (abs > 0.15) return 'critical';
        if (abs > 0.05) return 'warning';
        return 'normal';
    }

    /**
     * Format EISV metric with context
     */
    static formatEISVMetric(value, metricName) {
        if (value === null || value === undefined || isNaN(value)) {
            return { display: '-', interpretation: 'No data', color: 'var(--text-secondary)' };
        }

        // V can be negative (I > E imbalance); other metrics are [0, 1]
        const clamped = metricName === 'V'
            ? Math.max(-1, Math.min(1, value))
            : Math.max(0, Math.min(1, value));
        // Bar uses |V| scaled from its effective range (dynamics keep V in ~[-0.1, 0.1])
        const visualValue = metricName === 'V' ? Math.min(1, Math.abs(clamped) / 0.3) : clamped;
        const percent = (visualValue * 100).toFixed(0);

        const interpretations = {
            E: {
                low: { text: 'Low energy - limited productive capacity', color: 'var(--accent-orange)' },
                medium: { text: 'Moderate energy - steady productivity', color: 'var(--accent-yellow)' },
                high: { text: 'High energy - strong productive capacity', color: 'var(--accent-green)' }
            },
            I: {
                low: { text: 'Low integrity - information quality concerns', color: 'var(--accent-orange)' },
                medium: { text: 'Moderate integrity - acceptable quality', color: 'var(--accent-yellow)' },
                high: { text: 'High integrity - excellent information quality', color: 'var(--accent-green)' }
            },
            S: {
                low: { text: 'Low entropy - highly ordered', color: 'var(--accent-green)' },
                medium: { text: 'Moderate entropy - balanced', color: 'var(--accent-yellow)' },
                high: { text: 'High entropy - high disorder/uncertainty', color: 'var(--accent-orange)' }
            },
            V: {
                low: { text: 'Low void - good E-I balance', color: 'var(--accent-green)' },
                medium: { text: 'Moderate void - some E-I imbalance', color: 'var(--accent-yellow)' },
                high: { text: 'High void - significant E-I imbalance', color: 'var(--accent-orange)' }
            },
            C: {
                low: { text: 'Low coherence - fragmented state', color: 'var(--accent-orange)' },
                medium: { text: 'Moderate coherence - some alignment', color: 'var(--accent-yellow)' },
                high: { text: 'High coherence - well-aligned state', color: 'var(--accent-green)' }
            }
        };

        // Use rescaled value for interpretation thresholds so V's 0-0.3 range
        // maps to the same low/medium/high bands as other metrics
        let interpretation;
        if (visualValue < 0.33) {
            interpretation = interpretations[metricName]?.low || { text: 'Low', color: 'var(--text-secondary)' };
        } else if (visualValue < 0.67) {
            interpretation = interpretations[metricName]?.medium || { text: 'Moderate', color: 'var(--text-secondary)' };
        } else {
            interpretation = interpretations[metricName]?.high || { text: 'High', color: 'var(--text-secondary)' };
        }

        return {
            display: clamped.toFixed(3),
            percent: percent,
            interpretation: interpretation.text,
            color: interpretation.color,
            value: clamped
        };
    }

    /**
     * Format relative time
     */
    static formatRelativeTime(timestampMs) {
        if (!timestampMs) return null;
        const diffMs = Date.now() - timestampMs;
        if (diffMs <= 0) return 'just now';

        const seconds = Math.floor(diffMs / 1000);
        if (seconds < 60) return `${seconds}s ago`;

        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;

        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ago`;

        const days = Math.floor(hours / 24);
        if (days < 7) return `${days}d ago`;

        const weeks = Math.floor(days / 7);
        if (weeks < 5) return `${weeks}w ago`;

        const months = Math.floor(days / 30);
        if (months < 12) return `${months}mo ago`;

        const years = Math.floor(days / 365);
        return `${years}y ago`;
    }

    /**
     * Format timestamp for display
     */
    static formatTimestamp(timestamp) {
        if (!timestamp) return null;
        const date = new Date(timestamp);
        if (isNaN(date.getTime())) return null;
        const now = new Date();
        const sameYear = date.getFullYear() === now.getFullYear();
        const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const month = monthNames[date.getMonth()];
        const day = date.getDate();
        const time = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        if (sameYear) return `${month} ${day}, ${time}`;
        return `${month} ${day} ${date.getFullYear()}, ${time}`;
    }

    /**
     * Escape HTML to prevent XSS
     */
    static escapeHtml(text) {
        if (text === null || text === undefined) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Highlight search terms in text
     */
    static highlightMatch(text, term) {
        if (!term) return DataProcessor.escapeHtml(text);

        const safeText = String(text || '');
        const safeTerm = String(term || '').trim();
        if (!safeTerm) return DataProcessor.escapeHtml(safeText);

        const escapedTerm = safeTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regex = new RegExp(escapedTerm, 'ig');
        let result = '';
        let lastIndex = 0;
        let match;

        while ((match = regex.exec(safeText)) !== null) {
            result += DataProcessor.escapeHtml(safeText.slice(lastIndex, match.index));
            result += `<mark class="highlight">${DataProcessor.escapeHtml(match[0])}</mark>`;
            lastIndex = match.index + match[0].length;
        }
        result += DataProcessor.escapeHtml(safeText.slice(lastIndex));
        return result;
    }

    /**
     * Export data as CSV
     */
    static exportToCSV(data, filename) {
        if (!data || data.length === 0) return;

        const headers = Object.keys(data[0]);
        const csv = [
            headers.join(','),
            ...data.map(row =>
                headers.map(header => {
                    const value = row[header];
                    if (value === null || value === undefined) return '';
                    const stringValue = String(value);
                    // Escape quotes and wrap in quotes if contains comma, quote, or newline
                    if (stringValue.includes(',') || stringValue.includes('"') || stringValue.includes('\n')) {
                        return `"${stringValue.replace(/"/g, '""')}"`;
                    }
                    return stringValue;
                }).join(',')
            )
        ].join('\n');

        const blob = new Blob([csv], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'export.csv';
        a.click();
        URL.revokeObjectURL(url);
    }

    /**
     * Export data as JSON
     */
    static exportToJSON(data, filename) {
        const json = JSON.stringify(data, null, 2);
        const blob = new Blob([json], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'export.json';
        a.click();
        URL.revokeObjectURL(url);
    }

    /**
     * Copy text to clipboard
     */
    static async copyToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }

        // Fallback for older browsers
        return new Promise((resolve, reject) => {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            try {
                const success = document.execCommand('copy');
                document.body.removeChild(textarea);
                if (success) {
                    resolve();
                } else {
                    reject(new Error('Copy failed'));
                }
            } catch (error) {
                document.body.removeChild(textarea);
                reject(error);
            }
        });
    }

    /**
     * Format environmental data from shared memory if available
     */
    static processEnvData(raw) {
        if (!raw) return { temp: 0, humidity: 0, lux: 0, status: 'unknown' };
        return {
            temp: raw.temp || raw.temperature || 0,
            humidity: raw.humidity || 0,
            lux: raw.lux || raw.light || 0,
            status: (raw.temp > 30 || raw.temp < 10) ? 'warning' : 'stable'
        };
    }
}

class ThemeManager {
    /**
     * Manages dark/light theme switching
     */
    constructor() {
        this.currentTheme = localStorage.getItem('dashboard_theme') || 'dark';
        this.applyTheme(this.currentTheme);
    }

    /**
     * Apply theme
     */
    applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('dashboard_theme', theme);
        this.currentTheme = theme;
    }

    /**
     * Toggle between dark and light themes
     */
    toggle() {
        const newTheme = this.currentTheme === 'dark' ? 'light' : 'dark';
        this.applyTheme(newTheme);
        return newTheme;
    }

    /**
     * Get current theme
     */
    getTheme() {
        return this.currentTheme;
    }
}

class EISVWebSocket {
    /**
     * WebSocket client for real-time EISV streaming from governance server.
     * Auto-reconnects with exponential backoff.
     */
    constructor(onUpdate, onStatusChange) {
        this.onUpdate = onUpdate;
        this.onStatusChange = onStatusChange || (() => { });
        this.ws = null;
        this.reconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.maxReconnectAttempts = 3;
        this.reconnectAttempts = 0;
        this.connected = false;
        this._intentionalClose = false;
        this._pollFallback = false;
        this._pollInterval = null;
    }

    connect() {
        this._intentionalClose = false;
        this.reconnectAttempts = 0;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/eisv`;

        try {
            this.ws = new WebSocket(wsUrl);
        } catch (e) {
            console.warn('[WS] WebSocket unavailable, falling back to polling');
            this._startPolling();
            return;
        }

        this.ws.onopen = () => {
            this.connected = true;
            this.reconnectDelay = 1000;
            this.reconnectAttempts = 0;
            this._pollFallback = false;
            this.onStatusChange('connected');
            console.log('[WS] Connected to /ws/eisv');
            this._backfillOnce();
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.onUpdate(data);
            } catch (e) {
                console.warn('[WS] Failed to parse message:', e);
            }
        };

        this.ws.onclose = () => {
            this.connected = false;
            this.onStatusChange('disconnected');
            if (!this._intentionalClose) {
                this._scheduleReconnect();
            }
        };

        this.ws.onerror = () => {
            // onclose will fire after this, which handles reconnect
        };
    }

    _scheduleReconnect() {
        this.reconnectAttempts++;
        if (this.reconnectAttempts > this.maxReconnectAttempts) {
            console.warn('[WS] Max reconnect attempts reached, falling back to HTTP polling');
            this._startPolling();
            return;
        }
        this.onStatusChange('reconnecting');
        setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    }

    _startPolling() {
        if (this._pollFallback) return;
        this._pollFallback = true;
        this._pollFailures = 0;
        this.onStatusChange('polling');
        console.log('[WS] Polling /v1/eisv/latest every 30s');
        // Backfill recent history, then poll immediately, then every 30s
        this._backfillOnce();
        this._pollOnce();
        this._pollInterval = setInterval(() => this._pollOnce(), 30000);
    }

    async _backfillOnce() {
        if (this._backfilled) return;
        this._backfilled = true;
        try {
            const resp = await fetch(`${window.location.origin}/v1/eisv/recent?limit=120`);
            if (!resp.ok) return;
            const data = await resp.json();
            const events = (data && Array.isArray(data.events)) ? data.events : [];
            for (const evt of events) {
                try { this.onUpdate(evt); } catch (_) { /* ignore per-event render errors */ }
            }
            console.log('[WS] Backfilled', events.length, 'EISV events');
        } catch (e) {
            console.debug('[WS] Backfill failed:', e);
        }
    }

    async _pollOnce() {
        try {
            const resp = await fetch(`${window.location.origin}/v1/eisv/latest`);
            if (resp.ok) {
                const data = await resp.json();
                if (data && data.type === 'eisv_update') {
                    this.onUpdate(data);
                }
                this._pollFailures = 0;
                if (this._pollErrorReported) {
                    this._pollErrorReported = false;
                    this.onStatusChange('polling');
                }
            } else {
                this._pollFailures = (this._pollFailures || 0) + 1;
                console.warn('[WS] Poll returned status', resp.status, '(' + this._pollFailures + ' consecutive failures)');
            }
        } catch (e) {
            this._pollFailures = (this._pollFailures || 0) + 1;
            console.warn('[WS] Poll failed:', e.message, '(' + this._pollFailures + ' consecutive failures)');
        }
        if (this._pollFailures >= 3 && !this._pollErrorReported) {
            this._pollErrorReported = true;
            this.onStatusChange('poll_error');
        }
    }

    disconnect() {
        this._intentionalClose = true;
        if (this.ws) {
            this.ws.close();
        }
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
    }
}

// ============================================================================
// GLOSSARY & UI HELPERS
// ============================================================================

const GLOSSARY = {
    'Coherence': 'How well-aligned an agent\'s state vector is. Derived from E-I balance and void — structural health, not semantic quality.',
    'Energy': 'Productive capacity. Rises when integrity exceeds energy, dragged down by high entropy.',
    'Integrity': 'Signal fidelity and information quality. Boosted by coherence, reduced by entropy.',
    'Entropy': 'Semantic uncertainty and disorder. Lower is better. Naturally decays over time, rises with complexity and ethical drift.',
    'Void': 'Accumulated E-I imbalance. Positive = running hot (E > I), negative = running careful (I > E). Decays toward zero.',
    'Risk': 'Combined score from EISV state, drift, and trajectory. Higher = more governance concern.',
    'Verdict': 'Governance decision after check-in: approve/proceed (healthy), guide (adjust), pause/reject (needs attention).',
    'Drift': 'Deviation from behavioral baseline across emotional, epistemic, and behavioral axes.',
    'Trust Tier': 'Agent reliability level. T0 = unknown (+5% risk), T1 = emerging (+5%), T2 = established (0%), T3 = verified (-5%).',
    'Basin': 'Region of EISV state space. High basin = healthy operation, low basin = degraded, boundary = transitioning.',
    'Phi': 'Integrated information measure. Higher values indicate more complex internal state integration.',
    'Calibration': 'Whether an agent\'s stated confidence matches observed outcomes. Overconfidence penalizes integrity.'
};

/**
 * Create a help icon with tooltip for a glossary term.
 * @param {string} term - Glossary term to look up
 * @returns {HTMLSpanElement}
 */
function createHelpIcon(term) {
    var span = document.createElement('span');
    span.className = 'help-icon';
    span.textContent = '?';
    span.title = GLOSSARY[term] || term;
    span.setAttribute('aria-label', 'Help: ' + term);
    return span;
}

/**
 * Show a temporary toast notification.
 * @param {string} message - Message to display
 * @param {number} [durationMs=3000] - Auto-dismiss duration in ms
 */
function showToast(message, durationMs) {
    durationMs = durationMs || 3000;
    var toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);
    // Trigger reflow then add visible class for animation
    toast.offsetHeight; // eslint-disable-line no-unused-expressions
    toast.classList.add('visible');
    setTimeout(function () {
        toast.classList.remove('visible');
        setTimeout(function () { toast.remove(); }, 300);
    }, durationMs);
}

// Verdict icons for color-blind accessibility (single source of truth)
const VERDICT_ICONS = {
    approve: '\u2713', proceed: '\u2713', safe: '\u2713',
    caution: '\u26A0', guide: '\u26A0',
    pause: '\u2715', reject: '\u2715'
};

// Export for use in dashboard
if (typeof window !== 'undefined') {
    window.DashboardAPI = DashboardAPI;
    window.DataProcessor = DataProcessor;
    window.ThemeManager = ThemeManager;
    window.EISVWebSocket = EISVWebSocket;
    window.debounce = debounce;
    window.GLOSSARY = GLOSSARY;
    window.VERDICT_ICONS = VERDICT_ICONS;
    window.createHelpIcon = createHelpIcon;
    window.showToast = showToast;
}
