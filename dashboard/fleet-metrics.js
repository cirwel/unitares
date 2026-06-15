// fleet-metrics.js — render `metrics.series` time-series on the dashboard.
//
// Consumes:
//   GET /v1/metrics/catalog       — available series (name, description, unit)
//   GET /v1/metrics/series?name=X — points for the selected series
//
// Theme handling mirrors dashboard/eisv-charts.js::makeChartOptions so
// axis ticks, grid, tooltip, and fonts are legible on the dark theme —
// Chart.js defaults are dark-grey-on-dark and render invisible.
// Auth + fetch go through the shared `authFetch` helper from utils.js so
// this module stays aligned with the rest of the dashboard.

(function () {
    'use strict';

    var chart = null;
    var currentName = null;
    var catalogCache = [];

    // Default x-axis window. Chronicler scrapes daily, so 14 days = ~14
    // points per series — matches the 14-day rolling window the GitHub
    // traffic series advertise and gives all other series enough context
    // to read trend without dragging in months of cold history.
    var WINDOW_DAYS = 14;

    // Set once so subsequent `new Chart()` calls pick up the theme without
    // repeating the block. Applied at wire() time because the dashboard body
    // CSS vars must be resolvable.
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

    async function fetchCatalog() {
        try {
            var resp = await authFetch('/v1/metrics/catalog');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) {
                throw new Error((data && data.error) || 'unknown');
            }
            return data.metrics || [];
        } catch (e) {
            console.warn('[FleetMetrics] catalog fetch failed:', e);
            return [];
        }
    }

    async function fetchSeries(name) {
        try {
            var since = new Date(Date.now() - WINDOW_DAYS * 86400 * 1000).toISOString();
            var resp = await authFetch('/v1/metrics/series?name=' + encodeURIComponent(name)
                + '&since=' + encodeURIComponent(since));
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) {
                throw new Error((data && data.error) || 'unknown');
            }
            return data.points || [];
        } catch (e) {
            console.warn('[FleetMetrics] series fetch failed:', e);
            return [];
        }
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

    function setDescription(text) {
        var el = document.getElementById('fleet-metrics-description');
        if (el) el.textContent = text || '';
    }

    function setScrapeStatus(points) {
        var el = document.getElementById('fleet-metrics-scrape-status');
        if (!el) return;
        if (!points || points.length === 0) {
            el.textContent = 'no data in last ' + WINDOW_DAYS + 'd — awaiting scrape';
            el.title = '';
            return;
        }
        var newest = points[points.length - 1];
        el.textContent = 'last scrape: ' + formatRelative(newest.ts)
            + ' · ' + points.length + ' pt' + (points.length === 1 ? '' : 's')
            + ' · ' + WINDOW_DAYS + 'd window';
        el.title = newest.ts;
    }

    function showChart(show) {
        var canvas = document.getElementById('fleet-metrics-chart');
        var empty = document.getElementById('fleet-metrics-empty');
        if (canvas) canvas.style.display = show ? '' : 'none';
        if (empty) empty.style.display = show ? 'none' : '';
    }

    function renderEmpty(message) {
        var empty = document.getElementById('fleet-metrics-empty');
        if (empty) empty.textContent = message;
        showChart(false);
        if (chart) { chart.destroy(); chart = null; }
    }

    // Theme-aware chart options, patterned on eisv-charts.js:makeChartOptions.
    function chartOptionsFor(metric) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
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
                    time: { tooltipFormat: 'yyyy-MM-dd HH:mm' },
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#a0a0b0', font: { size: 11 }, maxRotation: 0 },
                },
                y: {
                    beginAtZero: false,
                    title: { display: !!metric.unit, text: metric.unit || '', color: '#a0a0b0' },
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: {
                        color: '#a0a0b0',
                        font: { family: "'JetBrains Mono', monospace", size: 11 },
                    },
                },
            },
        };
    }

    function renderChart(metric, points) {
        var canvas = document.getElementById('fleet-metrics-chart');
        if (!canvas) return;

        if (points.length === 0) {
            renderEmpty('No data for "' + metric.name + '" in the last '
                + WINDOW_DAYS + ' days. Chronicler runs daily — Refresh after the next cycle.');
            return;
        }

        showChart(true);
        var data = points.map(function (p) {
            return { x: new Date(p.ts), y: p.value };
        });

        var lineColor = (typeof MetricColors !== 'undefined'
            && MetricColors.HEX
            && MetricColors.HEX.chartCoherence)
            ? MetricColors.HEX.chartCoherence
            : '#06b6d4';

        if (chart) chart.destroy();
        // eslint-disable-next-line no-undef
        chart = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: {
                datasets: [{
                    label: metric.name + (metric.unit ? ' (' + metric.unit + ')' : ''),
                    data: data,
                    borderColor: lineColor,
                    backgroundColor: lineColor + '22',   // 0x22 alpha — subtle fill
                    fill: true,
                    tension: 0.2,
                    pointRadius: 3,
                }],
            },
            options: chartOptionsFor(metric),
        });
    }

    // Chronicler emits three unrelated metric families into one catalog:
    // fleet/governance state, project/codebase stats, and runtime-infra
    // latency. A flat dropdown made the panel "mean three things at once".
    // Classify by series-name prefix so the picker reads as Fleet · Project ·
    // Infra optgroups; anything unmatched falls through to Other so a new
    // series never silently disappears. `.error` twins classify by their base
    // name so a failing scraper sorts next to the series it shadows.
    var METRIC_GROUPS = [
        { label: 'Fleet', test: /^(agents|checkins|kg)\./ },
        { label: 'Project', test: /^(github|tests|tokei)\./ },
        { label: 'Infra', test: /^(lease_plane|ode)\./ },
    ];
    var METRIC_GROUP_ORDER = ['Fleet', 'Project', 'Infra', 'Other'];

    function metricGroupLabel(name) {
        var base = name.replace(/\.error$/, '');
        for (var i = 0; i < METRIC_GROUPS.length; i++) {
            if (METRIC_GROUPS[i].test.test(base)) return METRIC_GROUPS[i].label;
        }
        return 'Other';
    }

    function populateDropdown(metrics) {
        var select = document.getElementById('fleet-metrics-select');
        if (!select) return;
        select.innerHTML = '';
        if (metrics.length === 0) {
            var opt = document.createElement('option');
            opt.value = '';
            opt.textContent = '(no metrics registered)';
            select.appendChild(opt);
            return;
        }

        // Bucket by family, preserving catalog order within each bucket, then
        // emit optgroups in fixed family order. Empty buckets are skipped so a
        // family with no registered series doesn't clutter the picker.
        var buckets = {};
        metrics.forEach(function (m) {
            var label = metricGroupLabel(m.name);
            (buckets[label] = buckets[label] || []).push(m);
        });
        METRIC_GROUP_ORDER.forEach(function (groupLabel) {
            var items = buckets[groupLabel];
            if (!items || items.length === 0) return;
            var group = document.createElement('optgroup');
            group.label = groupLabel;
            items.forEach(function (m) {
                var o = document.createElement('option');
                o.value = m.name;
                o.textContent = m.name;
                if (m.description) o.title = m.description;
                group.appendChild(o);
            });
            select.appendChild(group);
        });

        if (!currentName || !metrics.some(function (m) { return m.name === currentName; })) {
            currentName = metrics[0].name;
        }
        select.value = currentName;
    }

    function setErrorBadge(activeErrorNames) {
        var badge = document.getElementById('fleet-metrics-error-badge');
        if (!badge) return;
        if (!activeErrorNames || activeErrorNames.length === 0) {
            badge.hidden = true;
            badge.textContent = '';
            badge.title = '';
            return;
        }
        badge.hidden = false;
        badge.textContent = '⚠ ' + activeErrorNames.length
            + ' error' + (activeErrorNames.length === 1 ? '' : 's');
        badge.title = 'Failing scrapers: ' + activeErrorNames.join(', ');
    }

    async function refresh() {
        // Chronicler registers `<name>.error` twins upfront (catalog.py
        // auto-twins) so failures have a stable slot. They only matter
        // when a scraper has actually failed — hide empty twins from the
        // dropdown, surface active ones inline, and light a header badge.
        // The catalog endpoint returns `last_point_ts` per metric (null
        // if empty), so presence is one round-trip, not N+1.
        var raw = await fetchCatalog();
        var baseMetrics = raw.filter(function (m) { return !m.name.endsWith('.error'); });
        var activeErrorTwins = raw.filter(function (m) {
            return m.name.endsWith('.error') && m.last_point_ts;
        });
        setErrorBadge(activeErrorTwins.map(function (m) { return m.name; }));

        catalogCache = baseMetrics.concat(activeErrorTwins);
        populateDropdown(catalogCache);
        if (catalogCache.length === 0) {
            setDescription('');
            setScrapeStatus(null);
            renderEmpty('No metrics registered yet.');
            return;
        }
        var metric = catalogCache.find(function (m) { return m.name === currentName; }) || catalogCache[0];
        currentName = metric.name;
        setDescription(metric.description || '');
        var points = await fetchSeries(metric.name);
        setScrapeStatus(points);
        renderChart(metric, points);
    }

    function wire() {
        applyChartDefaults();
        var select = document.getElementById('fleet-metrics-select');
        var refreshBtn = document.getElementById('fleet-metrics-refresh');
        if (select) {
            select.addEventListener('change', function () {
                currentName = select.value;
                refresh();
            });
        }
        if (refreshBtn) {
            refreshBtn.addEventListener('click', refresh);
        }
        refresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }

    window.FleetMetricsPanel = {
        refresh: refresh,
        _fetchCatalog: fetchCatalog,
        _fetchSeries: fetchSeries,
    };
})();
