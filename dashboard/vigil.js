// vigil.js — Vigil (janitor resident) dedicated panel.
//
// Vigil runs every 30 min via launchd. Its KG writes are mostly low-severity
// groundskeeper deltas ("N stale, M archived") which previously crowded the
// main Discoveries feed. This panel segregates Vigil's cycle history and
// recent writes so the main feed can hide them by default.
//
// Backed by /v1/vigil/summary. authFetch from utils.js handles the bearer
// token; don't reach into localStorage directly here.

(function () {
    'use strict';

    async function fetchSummary() {
        try {
            var resp = await authFetch('/v1/vigil/summary');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) {
                throw new Error((data && data.error) || 'unknown');
            }
            return data;
        } catch (e) {
            console.warn('[Vigil] summary fetch failed:', e);
            return null;
        }
    }

    function setMetric(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function formatRelative(isoStr) {
        if (!isoStr) return '—';
        var then = new Date(isoStr);
        if (isNaN(then.getTime())) return isoStr;
        var secs = Math.floor((Date.now() - then.getTime()) / 1000);
        if (secs < 60) return 'just now';
        if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
        if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
        return Math.floor(secs / 86400) + 'd ago';
    }

    function fmtNum(v, digits) {
        if (v == null || typeof v !== 'number' || isNaN(v)) return '—';
        return v.toFixed(digits == null ? 2 : digits);
    }

    function renderStats(summary) {
        var s = summary.stats || {};
        setMetric('vigil-last-cycle', formatRelative(s.last_cycle_at));
        setMetric('vigil-cycles-24h', s.cycles_24h != null ? s.cycles_24h : '—');
        setMetric('vigil-writes-24h', s.writes_24h != null ? s.writes_24h : '—');
        setMetric('vigil-avg-coherence', fmtNum(s.avg_coherence_window));
        var verdictEl = document.getElementById('vigil-last-verdict');
        if (verdictEl) {
            var verdict = s.last_verdict || '—';
            verdictEl.textContent = verdict;
            verdictEl.className = 'vigil-stat-value vigil-verdict-' + String(verdict).toLowerCase();
        }
    }

    function renderCycles(cycles) {
        var container = document.getElementById('vigil-cycles');
        if (!container) return;
        container.innerHTML = '';
        if (!cycles || cycles.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'vigil-stream-empty';
            empty.textContent = 'No Vigil cycles in the window (has the launchd job been loaded?).';
            container.appendChild(empty);
            return;
        }
        for (var i = 0; i < cycles.length; i++) {
            var c = cycles[i];
            var row = document.createElement('div');
            row.className = 'vigil-row vigil-row-cycle';

            var ts = document.createElement('span');
            ts.className = 'vigil-row-time';
            ts.textContent = formatRelative(c.timestamp);
            ts.title = c.timestamp || '';

            var verdict = document.createElement('span');
            var vLower = String(c.verdict || '?').toLowerCase();
            verdict.className = 'vigil-row-verdict vigil-verdict-' + vLower;
            verdict.textContent = vLower;

            var coh = document.createElement('span');
            coh.className = 'vigil-row-metric';
            coh.textContent = 'C ' + fmtNum(c.coherence);
            coh.title = 'coherence';

            var risk = document.createElement('span');
            risk.className = 'vigil-row-metric';
            risk.textContent = 'R ' + fmtNum(c.risk);
            risk.title = 'risk score';

            var eisv = document.createElement('span');
            eisv.className = 'vigil-row-eisv';
            eisv.textContent = 'E' + fmtNum(c.E, 2)
                + ' I' + fmtNum(c.I, 2)
                + ' S' + fmtNum(c.S, 2)
                + ' V' + fmtNum(c.V, 2);

            row.appendChild(ts);
            row.appendChild(verdict);
            row.appendChild(coh);
            row.appendChild(risk);
            row.appendChild(eisv);
            container.appendChild(row);
        }
    }

    function renderWrites(writes) {
        var container = document.getElementById('vigil-writes');
        if (!container) return;
        container.innerHTML = '';
        if (!writes || writes.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'vigil-stream-empty';
            empty.textContent = 'Vigil has not written to the knowledge graph in the window.';
            container.appendChild(empty);
            return;
        }
        for (var i = 0; i < writes.length; i++) {
            var w = writes[i];
            var row = document.createElement('div');
            row.className = 'vigil-row vigil-row-write';

            var ts = document.createElement('span');
            ts.className = 'vigil-row-time';
            ts.textContent = formatRelative(w.timestamp);
            ts.title = w.timestamp || '';

            var sev = document.createElement('span');
            var sevVal = String(w.severity || 'low').toLowerCase();
            sev.className = 'vigil-row-sev vigil-row-sev-' + sevVal;
            sev.textContent = sevVal;

            var type = document.createElement('span');
            type.className = 'vigil-row-type';
            type.textContent = w.type || 'note';

            var summary = document.createElement('span');
            summary.className = 'vigil-row-msg';
            summary.textContent = w.summary || '';

            row.appendChild(ts);
            row.appendChild(sev);
            row.appendChild(type);
            row.appendChild(summary);
            container.appendChild(row);
        }
    }

    function setFooter(summary) {
        var el = document.getElementById('vigil-meta');
        if (!el) return;
        var stats = summary.stats || {};
        var parts = [
            'window: ' + (summary.window_hours || 72) + 'h',
            (stats.total_cycles_in_window || 0) + ' cycles',
            (stats.total_writes_in_window || 0) + ' writes',
            'refreshed ' + formatRelative(summary.generated_at),
        ];
        if (!summary.agent_id) parts.push('(no Vigil agent registered)');
        el.textContent = parts.join(' · ');
    }

    async function refresh() {
        var summary = await fetchSummary();
        if (!summary) {
            setMetric('vigil-last-cycle', '—');
            setMetric('vigil-cycles-24h', '—');
            setMetric('vigil-writes-24h', '—');
            setMetric('vigil-avg-coherence', '—');
            setMetric('vigil-last-verdict', '—');
            return;
        }
        renderStats(summary);
        renderCycles(summary.cycles || []);
        renderWrites(summary.recent_writes || []);
        setFooter(summary);
    }

    function wire() {
        var btn = document.getElementById('vigil-refresh');
        if (btn) btn.addEventListener('click', refresh);
        refresh();
        // Auto-refresh on the dashboard's 30s cadence, honoring the global
        // "Pause auto-refresh" toggle. Without this the panel loaded once at
        // init and then showed indefinitely-stale cycles with no cue.
        setInterval(function () {
            if (typeof state !== 'undefined' && state.get('autoRefreshPaused')) return;
            refresh();
        }, 30000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }

    window.VigilPanel = { refresh: refresh };
})();
