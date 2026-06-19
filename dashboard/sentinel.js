// sentinel.js — Sentinel findings stream panel.
//
// Consumes:
//   GET /v1/sentinel/summary — counts by severity + violation class, recent stream
//                              (live in-memory ring buffer, wiped on mcp restart)
//   GET /v1/sentinel/backlog — durable HIGH/critical findings from audit.events
//                              (survives restarts; the "did I miss one?" view)
//
// Unlike Watcher, Sentinel findings are transient fleet-state signals with no
// open/closed lifecycle. The counts + class breakdown are always the live 24h
// summary; the STREAM toggles between the live recent log and the durable HIGH
// backlog so a finding that fired before a deploy is still reviewable.
//
// Auth + fetch go through `authFetch` from utils.js.

(function () {
    'use strict';

    // 'live' = summary.recent (ring buffer) · 'durable' = backlog (audit.events)
    var streamMode = 'live';

    async function fetchSummary() {
        try {
            var resp = await authFetch('/v1/sentinel/summary');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) {
                throw new Error((data && data.error) || 'unknown');
            }
            return data;
        } catch (e) {
            console.warn('[Sentinel] summary fetch failed:', e);
            return null;
        }
    }

    async function fetchBacklog() {
        try {
            var resp = await authFetch('/v1/sentinel/backlog?severity=high&window_hours=168');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) {
                throw new Error((data && data.error) || 'unknown');
            }
            return data;
        } catch (e) {
            console.warn('[Sentinel] backlog fetch failed:', e);
            return null;
        }
    }

    function setMetric(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function renderCounts(summary) {
        var bySev = summary.by_severity || {};
        setMetric('sentinel-count-total', summary.total || 0);
        setMetric('sentinel-count-critical', bySev.critical || 0);
        setMetric('sentinel-count-high', bySev.high || 0);
        setMetric('sentinel-count-medium', bySev.medium || 0);
    }

    function renderClassBreakdown(summary) {
        var container = document.getElementById('sentinel-class-breakdown');
        if (!container) return;
        container.innerHTML = '';
        var classes = summary.by_violation_class || [];
        if (classes.length === 0) {
            var empty = document.createElement('span');
            empty.className = 'sentinel-class-empty';
            empty.textContent = 'no findings in window';
            container.appendChild(empty);
            return;
        }
        for (var i = 0; i < classes.length; i++) {
            var c = classes[i];
            var pill = document.createElement('span');
            pill.className = 'sentinel-class-pill';
            var name = document.createElement('span');
            name.className = 'sentinel-class-name';
            name.textContent = c.violation_class;
            var count = document.createElement('span');
            count.className = 'sentinel-class-count';
            count.textContent = String(c.count);
            // Tooltip shows severity breakdown so hovering tells the full story
            var sevParts = [];
            var sev = c.by_severity || {};
            for (var k in sev) {
                if (Object.prototype.hasOwnProperty.call(sev, k)) {
                    sevParts.push(k + ':' + sev[k]);
                }
            }
            pill.title = sevParts.join(' · ');
            pill.appendChild(name);
            pill.appendChild(count);
            container.appendChild(pill);
        }
    }

    function formatRelative(isoStr) {
        if (!isoStr) return '';
        var then = new Date(isoStr);
        if (isNaN(then.getTime())) return isoStr;
        var secs = Math.floor((Date.now() - then.getTime()) / 1000);
        if (secs < 60) return 'just now';
        if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
        if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
        return Math.floor(secs / 86400) + 'd ago';
    }

    function renderStreamRows(rows, emptyText) {
        var container = document.getElementById('sentinel-stream');
        if (!container) return;
        container.innerHTML = '';
        if (!rows || rows.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'sentinel-stream-empty';
            empty.textContent = emptyText;
            container.appendChild(empty);
            return;
        }
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var row = document.createElement('div');
            row.className = 'sentinel-row';

            var ts = document.createElement('span');
            ts.className = 'sentinel-row-time';
            ts.textContent = formatRelative(r.timestamp);
            ts.title = r.timestamp || '';

            var sev = document.createElement('span');
            var sevValue = (r.severity || '?').toLowerCase();
            sev.className = 'sentinel-row-sev sentinel-row-sev-' + sevValue;
            sev.textContent = sevValue;

            var vc = document.createElement('span');
            vc.className = 'sentinel-row-class';
            vc.textContent = r.violation_class || '?';

            var msg = document.createElement('span');
            msg.className = 'sentinel-row-msg';
            msg.textContent = r.message || '';

            row.appendChild(ts);
            row.appendChild(sev);
            row.appendChild(vc);
            row.appendChild(msg);
            container.appendChild(row);
        }
    }

    function setFooter(text) {
        var el = document.getElementById('sentinel-meta');
        if (el) el.textContent = text;
    }

    // The counts + class breakdown are always the live 24h summary. Only the
    // stream switches source.
    async function refreshStream() {
        if (streamMode === 'durable') {
            var backlog = await fetchBacklog();
            if (!backlog) {
                renderStreamRows([], 'Durable backlog unavailable.');
                return;
            }
            renderStreamRows(
                backlog.findings,
                'No durable HIGH findings in the last '
                    + Math.round((backlog.window_hours || 168) / 24) + 'd.'
            );
            setFooter('stream: durable HIGH · ' + (backlog.count || 0)
                + ' findings · ' + Math.round((backlog.window_hours || 168) / 24)
                + 'd · survives restarts');
        } else {
            var summary = await fetchSummary();
            if (!summary) {
                renderStreamRows([], 'Live stream unavailable.');
                return;
            }
            renderStreamRows(
                summary.recent,
                'No Sentinel findings in the last ' + (summary.window_hours || 24) + ' hours.'
            );
            setFooter('stream: live · ' + (summary.total || 0) + ' findings · '
                + (summary.window_hours || 24) + 'h · refreshed '
                + formatRelative(summary.generated_at));
        }
    }

    async function refresh() {
        var summary = await fetchSummary();
        if (summary) {
            renderCounts(summary);
            renderClassBreakdown(summary);
            // Publish severity counts for the fleet-severity hero rollup.
            var sev = summary.by_severity || {};
            if (typeof state !== 'undefined') {
                state.set({ sentinelSummary: { critical: sev.critical || 0, high: sev.high || 0 } });
            }
        } else {
            setMetric('sentinel-count-total', '—');
            setMetric('sentinel-count-critical', '—');
            setMetric('sentinel-count-high', '—');
            setMetric('sentinel-count-medium', '—');
            // Feed-error marker so a dead endpoint isn't read as "0 findings".
            if (typeof state !== 'undefined') state.set({ sentinelSummary: { error: true } });
        }
        await refreshStream();
    }

    function wire() {
        var btn = document.getElementById('sentinel-refresh');
        if (btn) btn.addEventListener('click', refresh);
        var toggle = document.getElementById('sentinel-toggle-durable');
        if (toggle) {
            toggle.addEventListener('click', function () {
                streamMode = (streamMode === 'durable') ? 'live' : 'durable';
                toggle.textContent = (streamMode === 'durable')
                    ? 'Stream: Durable HIGH' : 'Stream: Live';
                refreshStream();
            });
        }
        refresh();
        // Auto-refresh on the dashboard's 30s cadence, honoring the global
        // "Pause auto-refresh" toggle. Without this the panel loaded once at
        // init and then showed indefinitely-stale findings with no cue.
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

    window.SentinelPanel = { refresh: refresh };
})();
