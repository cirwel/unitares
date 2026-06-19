// watcher.js — Watcher findings pipeline panel.
//
// Consumes:
//   GET /v1/watcher/summary — counts by status/severity, pattern table, 30d timeline
//
// Theme handling mirrors dashboard/eisv-charts.js::makeChartOptions so axis
// ticks, grid, tooltip, and fonts are legible on the dark theme — Chart.js
// defaults are dark-grey-on-dark and render invisible.
// Auth + fetch go through `authFetch` from utils.js.

(function () {
    'use strict';

    var chart = null;

    function applyChartDefaults() {
        if (typeof Chart === 'undefined' || !Chart.defaults) return;
        var bodyStyle = getComputedStyle(document.body);
        var textSecondary = (bodyStyle.getPropertyValue('--text-secondary') || '').trim() || '#a0a0b0';
        var fontFamily = (bodyStyle.getPropertyValue('--font-family') || '').trim() || "'Outfit', sans-serif";
        Chart.defaults.color = textSecondary;
        if (Chart.defaults.font) {
            Chart.defaults.font.family = fontFamily;
        }
        Chart.defaults.borderColor = 'rgba(255,255,255,0.08)';
    }

    async function fetchSummary() {
        try {
            var resp = await authFetch('/v1/watcher/summary');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) {
                throw new Error((data && data.error) || 'unknown');
            }
            return data;
        } catch (e) {
            console.warn('[Watcher] summary fetch failed:', e);
            return null;
        }
    }

    function setMetric(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function renderCounts(summary) {
        var byStatus = summary.by_status || {};
        var bySev = summary.by_severity_open || {};
        setMetric('watcher-count-open', (byStatus.surfaced || 0) + (byStatus.open || 0));
        // Resolved-as-bug status is "confirmed" in the watcher data model —
        // see agents/watcher/findings.py VALID_FINDING_STATUSES.
        setMetric('watcher-count-confirmed', byStatus.confirmed || 0);
        setMetric('watcher-count-dismissed', byStatus.dismissed || 0);
        setMetric('watcher-count-critical', bySev.critical || 0);
        setMetric('watcher-count-high', bySev.high || 0);
    }

    function renderPatterns(summary) {
        var tbody = document.getElementById('watcher-patterns-body');
        if (!tbody) return;
        tbody.innerHTML = '';
        var patterns = summary.patterns || [];
        if (patterns.length === 0) {
            var tr = document.createElement('tr');
            var td = document.createElement('td');
            td.colSpan = 5;
            td.className = 'watcher-patterns-empty';
            td.textContent = 'No findings yet.';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }
        for (var i = 0; i < patterns.length && i < 12; i++) {
            var p = patterns[i];
            var tr = document.createElement('tr');
            var cells = [
                p.pattern,
                String(p.surfaced),
                String(p.confirmed),
                String(p.dismissed),
                p.dismiss_ratio == null ? '—' : (Math.round(p.dismiss_ratio * 100) + '%'),
            ];
            for (var c = 0; c < cells.length; c++) {
                var td = document.createElement('td');
                td.textContent = cells[c];
                if (c === 4 && p.dismiss_ratio != null && p.dismiss_ratio >= 0.75) {
                    // A high dismiss ratio is only a "noisy rule" signal when the
                    // rule has never confirmed a real bug. When confirmed > 0, the
                    // dismissals are the FP-filter pipeline catching known-benign
                    // matches while the rule still earns its keep — don't paint it
                    // as noise (the conflation that put healthy P003/P005/P016 on
                    // the 2026-06-12 retirement triage; see PR #659).
                    if (Number(p.confirmed) > 0) {
                        td.className = 'watcher-dismiss-ratio-ok';
                        td.title = 'High dismiss ratio, but this rule still confirms real bugs ('
                            + p.confirmed + ' confirmed) — false-positive filters working, not noise'
                            + (p.dismissed_fp ? ' · ' + p.dismissed_fp + ' dismissed as confirmed FPs' : '');
                    } else {
                        td.className = 'watcher-dismiss-ratio-high';  // genuine retirement candidate
                        td.title = 'Mostly dismissed with zero confirmed bugs — candidate for review/retirement';
                    }
                }
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
    }

    function chartOptions() {
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    labels: { boxWidth: 12, font: { size: 11 } },
                },
                tooltip: {
                    backgroundColor: 'rgba(13,13,18,0.9)',
                    titleFont: { family: "'Inter', sans-serif" },
                    bodyFont: { family: "'JetBrains Mono', monospace", size: 12 },
                    padding: 10,
                    borderColor: '#333',
                    borderWidth: 1,
                },
            },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'day', tooltipFormat: 'yyyy-MM-dd' },
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#a0a0b0', font: { size: 11 }, maxRotation: 0 },
                },
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: {
                        color: '#a0a0b0',
                        font: { family: "'JetBrains Mono', monospace", size: 11 },
                        precision: 0,
                    },
                },
            },
        };
    }

    function renderChart(summary) {
        var canvas = document.getElementById('watcher-timeline-chart');
        if (!canvas) return;

        var timeline = summary.timeline || [];
        var hex = (typeof MetricColors !== 'undefined' && MetricColors.HEX) ? MetricColors.HEX : {};
        var detectedColor = hex.chartSurprise || '#f59e0b';
        var confirmedColor = hex.chartIntegrity || '#10b981';
        var dismissedColor = hex.chartDrift || '#6b7280';

        function series(key, color, label) {
            var data = timeline.map(function (d) {
                return { x: new Date(d.day + 'T00:00:00Z'), y: d[key] || 0 };
            });
            return {
                label: label,
                data: data,
                borderColor: color,
                backgroundColor: color + '33',
                fill: false,
                tension: 0.2,
                pointRadius: 2,
            };
        }

        if (chart) chart.destroy();
        // eslint-disable-next-line no-undef
        chart = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: {
                datasets: [
                    series('detected', detectedColor, 'new findings'),
                    series('confirmed', confirmedColor, 'confirmed'),
                    series('dismissed', dismissedColor, 'dismissed'),
                ],
            },
            options: chartOptions(),
        });
    }

    function formatRelative(isoStr) {
        if (!isoStr) return '';
        var then = new Date(isoStr);
        var secs = Math.floor((Date.now() - then.getTime()) / 1000);
        if (secs < 60) return 'just now';
        if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
        if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
        return Math.floor(secs / 86400) + 'd ago';
    }

    function setFooter(summary) {
        var el = document.getElementById('watcher-meta');
        if (!el) return;
        el.textContent = 'total findings ever: ' + (summary.total || 0)
            + ' · window: ' + (summary.window_days || 30) + ' days'
            + ' · refreshed ' + formatRelative(summary.generated_at);
    }

    async function refresh() {
        var summary = await fetchSummary();
        if (!summary) {
            setMetric('watcher-count-open', '—');
            setMetric('watcher-count-resolved', '—');
            setMetric('watcher-count-dismissed', '—');
            setMetric('watcher-count-critical', '—');
            setMetric('watcher-count-high', '—');
            // Publish a feed-error marker for the fleet-severity hero rollup so a
            // dead endpoint surfaces as "unavailable", not a silent "0 findings".
            if (typeof state !== 'undefined') state.set({ watcherSummary: { error: true } });
            return;
        }
        renderCounts(summary);
        renderPatterns(summary);
        renderChart(summary);
        setFooter(summary);
        // Publish severity counts for the fleet-severity hero rollup.
        var sev = summary.by_severity_open || {};
        if (typeof state !== 'undefined') {
            state.set({ watcherSummary: { critical: sev.critical || 0, high: sev.high || 0 } });
        }
    }

    function wire() {
        applyChartDefaults();
        var btn = document.getElementById('watcher-refresh');
        if (btn) btn.addEventListener('click', refresh);
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

    window.WatcherPanel = { refresh: refresh };
})();
