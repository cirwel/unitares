/**
 * Unitares Dashboard — EISV Charts Module
 *
 * EISV time-series charts, WebSocket handler, governance pulse,
 * events log, drift gauges, and animated value updates.
 * Extracted from dashboard.js to reduce monolith size.
 */
(function () {
    'use strict';

    if (typeof DashboardState === 'undefined') {
        console.warn('[EISVChartsModule] state.js not loaded, module disabled');
        return;
    }

    var escapeHtml = DataProcessor.escapeHtml;

    // Module constants (mirrors CONFIG values for independence)
    var EISV_WINDOW_MS = 30 * 60 * 1000;
    var EISV_BUCKET_MS = 30000;
    var EISV_MAX_POINTS = 60;
    var SCROLL_FEEDBACK_MS = 2000;
    var MAX_LOG_ENTRIES = 8;

    var DRIFT_AXES = ['emotional', 'epistemic', 'behavioral'];
    var TREND_ICONS = {
        stable: '',
        oscillating: '~',
        drifting_up: '\u2197',
        drifting_down: '\u2198'
    };
    var TREND_COLORS = {
        stable: '#6b7280',
        oscillating: '#06b6d4',
        drifting_up: '#ef4444',
        drifting_down: '#3b82f6'
    };
    var EVENT_ICONS = {
        verdict_change: '\u26A1',
        risk_threshold: '\uD83D\uDCCA',
        trajectory_adjustment: '\uD83C\uDFAF',
        drift_alert: '\uD83C\uDF0A',
        agent_new: '\u2728',
        agent_idle: '\uD83D\uDCA4',
        checkin: '\u2713'
    };
    var SEVERITY_CLASSES = {
        info: 'event-info',
        warning: 'event-warning',
        critical: 'event-critical'
    };

    // ========================================================================
    // Agent dropdown
    // ========================================================================

    function updateAgentDropdown() {
        var select = document.getElementById('eisv-agent-select');
        if (!select) return;
        var currentValue = select.value;
        var knownAgents = state.get('knownAgents');
        var agentEISVHistory = state.get('agentEISVHistory');

        var specialOpts = ['__fleet__', '__all__'];
        var agentOpts = Array.from(knownAgents).sort();

        var existingAgents = Array.from(select.options)
            .filter(function (o) { return specialOpts.indexOf(o.value) === -1; })
            .map(function (o) { return o.value; });

        if (JSON.stringify(existingAgents) === JSON.stringify(agentOpts)) return;

        select.innerHTML = '';
        select.add(new Option('Fleet Average', '__fleet__'));
        select.add(new Option('All agents', '__all__'));

        if (agentOpts.length > 0) {
            var sep = new Option('\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500', '');
            sep.disabled = true;
            select.add(sep);
            agentOpts.forEach(function (agentId) {
                var history = agentEISVHistory[agentId];
                var name = (history && history[0] && history[0].name) || agentId;
                var shortName = name.length > 20 ? name.substring(0, 17) + '...' : name;
                select.add(new Option(shortName, agentId));
            });
        }

        if (Array.from(select.options).some(function (o) { return o.value === currentValue; })) {
            select.value = currentValue;
        }
    }

    // ========================================================================
    // Fleet average computation
    // ========================================================================

    function computeFleetAverage() {
        var now = Date.now();
        var cutoff = now - EISV_WINDOW_MS;
        var agentEISVHistory = state.get('agentEISVHistory');

        var allPoints = [];
        var agentIds = Object.keys(agentEISVHistory);
        for (var i = 0; i < agentIds.length; i++) {
            var history = agentEISVHistory[agentIds[i]];
            for (var j = 0; j < history.length; j++) {
                if (history[j].ts >= cutoff) {
                    allPoints.push(history[j]);
                }
            }
        }

        if (allPoints.length === 0) return null;

        var buckets = {};
        for (var k = 0; k < allPoints.length; k++) {
            var pt = allPoints[k];
            var bucket = Math.floor(pt.ts / EISV_BUCKET_MS) * EISV_BUCKET_MS;
            if (!buckets[bucket]) {
                buckets[bucket] = { E: [], I: [], S: [], V: [], coherence: [], ode_E: [], ode_I: [], ode_S: [], ode_V: [] };
            }
            buckets[bucket].E.push(pt.E);
            buckets[bucket].I.push(pt.I);
            buckets[bucket].S.push(pt.S);
            buckets[bucket].V.push(pt.V);
            buckets[bucket].coherence.push(pt.coherence);
            if (pt.ode_E != null) buckets[bucket].ode_E.push(pt.ode_E);
            if (pt.ode_I != null) buckets[bucket].ode_I.push(pt.ode_I);
            if (pt.ode_S != null) buckets[bucket].ode_S.push(pt.ode_S);
            if (pt.ode_V != null) buckets[bucket].ode_V.push(pt.ode_V);
        }

        function avg(arr) {
            if (!arr.length) return null;
            var sum = 0;
            for (var n = 0; n < arr.length; n++) sum += arr[n];
            return sum / arr.length;
        }

        var result = [];
        var sortedKeys = Object.keys(buckets).sort();
        for (var m = 0; m < sortedKeys.length; m++) {
            var ts = sortedKeys[m];
            var vals = buckets[ts];
            result.push({
                x: new Date(parseInt(ts)),
                E: avg(vals.E),
                I: avg(vals.I),
                S: avg(vals.S),
                V: avg(vals.V),
                coherence: avg(vals.coherence),
                ode_E: avg(vals.ode_E),
                ode_I: avg(vals.ode_I),
                ode_S: avg(vals.ode_S),
                ode_V: avg(vals.ode_V),
                agentCount: vals.E.length
            });
        }
        return result;
    }

    // ========================================================================
    // Chart rebuild from selection
    // ========================================================================

    function rebuildChartFromSelection() {
        var eisvChartUpper = state.get('eisvChartUpper');
        var eisvChartLower = state.get('eisvChartLower');
        if (!eisvChartUpper || !eisvChartLower) return;

        eisvChartUpper.data.datasets.forEach(function (ds) { ds.data = []; });
        eisvChartLower.data.datasets.forEach(function (ds) { ds.data = []; });

        var now = Date.now();
        var cutoff = now - EISV_WINDOW_MS;
        var selectedAgentView = state.get('selectedAgentView');
        var agentEISVHistory = state.get('agentEISVHistory');

        if (selectedAgentView === '__fleet__') {
            var avgData = computeFleetAverage();
            if (avgData) {
                avgData.forEach(function (pt) {
                    var ac = pt.agentCount || 0;
                    var fa = 'fleet avg (' + ac + ' agents)';
                    eisvChartUpper.data.datasets[0].data.push({ x: pt.x, y: pt.E, agent: fa });
                    eisvChartUpper.data.datasets[1].data.push({ x: pt.x, y: pt.I, agent: fa });
                    eisvChartUpper.data.datasets[2].data.push({ x: pt.x, y: pt.coherence, agent: fa });
                    if (pt.ode_E != null) eisvChartUpper.data.datasets[3].data.push({ x: pt.x, y: pt.ode_E, agent: fa });
                    if (pt.ode_I != null) eisvChartUpper.data.datasets[4].data.push({ x: pt.x, y: pt.ode_I, agent: fa });
                    eisvChartLower.data.datasets[0].data.push({ x: pt.x, y: pt.S, agent: fa });
                    eisvChartLower.data.datasets[1].data.push({ x: pt.x, y: pt.V, agent: fa });
                    if (pt.ode_S != null) eisvChartLower.data.datasets[2].data.push({ x: pt.x, y: pt.ode_S, agent: fa });
                    if (pt.ode_V != null) eisvChartLower.data.datasets[3].data.push({ x: pt.x, y: pt.ode_V, agent: fa });
                });
            }
        } else if (selectedAgentView === '__all__') {
            var histories = Object.values(agentEISVHistory);
            for (var h = 0; h < histories.length; h++) {
                for (var p = 0; p < histories[h].length; p++) {
                    var pt = histories[h][p];
                    if (pt.ts >= cutoff) {
                        var x = new Date(pt.ts);
                        var n = pt.name || '';
                        eisvChartUpper.data.datasets[0].data.push({ x: x, y: pt.E, agent: n });
                        eisvChartUpper.data.datasets[1].data.push({ x: x, y: pt.I, agent: n });
                        eisvChartUpper.data.datasets[2].data.push({ x: x, y: pt.coherence, agent: n });
                        if (pt.ode_E != null) eisvChartUpper.data.datasets[3].data.push({ x: x, y: pt.ode_E, agent: n });
                        if (pt.ode_I != null) eisvChartUpper.data.datasets[4].data.push({ x: x, y: pt.ode_I, agent: n });
                        eisvChartLower.data.datasets[0].data.push({ x: x, y: pt.S, agent: n });
                        eisvChartLower.data.datasets[1].data.push({ x: x, y: pt.V, agent: n });
                        if (pt.ode_S != null) eisvChartLower.data.datasets[2].data.push({ x: x, y: pt.ode_S, agent: n });
                        if (pt.ode_V != null) eisvChartLower.data.datasets[3].data.push({ x: x, y: pt.ode_V, agent: n });
                    }
                }
            }
            [eisvChartUpper, eisvChartLower].forEach(function (chart) {
                chart.data.datasets.forEach(function (ds) {
                    ds.data.sort(function (a, b) { return a.x - b.x; });
                });
            });
        } else {
            var hist = agentEISVHistory[selectedAgentView] || [];
            for (var q = 0; q < hist.length; q++) {
                if (hist[q].ts >= cutoff) {
                    var xd = new Date(hist[q].ts);
                    var nm = hist[q].name || '';
                    eisvChartUpper.data.datasets[0].data.push({ x: xd, y: hist[q].E, agent: nm });
                    eisvChartUpper.data.datasets[1].data.push({ x: xd, y: hist[q].I, agent: nm });
                    eisvChartUpper.data.datasets[2].data.push({ x: xd, y: hist[q].coherence, agent: nm });
                    if (hist[q].ode_E != null) eisvChartUpper.data.datasets[3].data.push({ x: xd, y: hist[q].ode_E, agent: nm });
                    if (hist[q].ode_I != null) eisvChartUpper.data.datasets[4].data.push({ x: xd, y: hist[q].ode_I, agent: nm });
                    eisvChartLower.data.datasets[0].data.push({ x: xd, y: hist[q].S, agent: nm });
                    eisvChartLower.data.datasets[1].data.push({ x: xd, y: hist[q].V, agent: nm });
                    if (hist[q].ode_S != null) eisvChartLower.data.datasets[2].data.push({ x: xd, y: hist[q].ode_S, agent: nm });
                    if (hist[q].ode_V != null) eisvChartLower.data.datasets[3].data.push({ x: xd, y: hist[q].ode_V, agent: nm });
                }
            }
        }

        // Limit chart data to prevent unbounded memory growth
        [eisvChartUpper, eisvChartLower].forEach(function (chart) {
            chart.data.datasets.forEach(function (ds) {
                while (ds.data.length > EISV_MAX_POINTS) {
                    ds.data.shift();
                }
            });
        });

        requestAnimationFrame(function () {
            eisvChartUpper.update('none');
            eisvChartLower.update('none');
        });
    }

    // ========================================================================
    // Chart.js plugin: equilibrium reference lines
    // ========================================================================

    var equilibriumPlugin = {
        id: 'equilibriumLines',
        afterDraw: function (chart) {
            var lines = chart.options.plugins && chart.options.plugins.equilibriumLines;
            if (!lines || !lines.length) return;
            var ctx = chart.ctx;
            var yScale = chart.scales.y;
            var xStart = chart.chartArea.left;
            var xEnd = chart.chartArea.right;

            lines.forEach(function (line) {
                var y = yScale.getPixelForValue(line.value);
                if (y < chart.chartArea.top || y > chart.chartArea.bottom) return;

                ctx.save();
                ctx.beginPath();
                ctx.strokeStyle = line.color || 'rgba(255,255,255,0.15)';
                ctx.lineWidth = 1;
                ctx.setLineDash(line.dash || [4, 4]);
                ctx.moveTo(xStart, y);
                ctx.lineTo(xEnd, y);
                ctx.stroke();

                if (line.label) {
                    ctx.font = '10px Inter, sans-serif';
                    ctx.fillStyle = line.color || 'rgba(255,255,255,0.3)';
                    ctx.textAlign = 'right';
                    ctx.fillText(line.label, xEnd - 4, y - 4);
                }
                ctx.restore();
            });
        }
    };

    if (typeof Chart !== 'undefined') {
        Chart.register(equilibriumPlugin);
    }

    // ========================================================================
    // Chart options factory
    // ========================================================================

    function makeChartOptions(extraYOpts) {
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
                    callbacks: {
                        label: function (ctx) {
                            var pt = ctx.raw || {};
                            var agent = pt.agent ? ' [' + pt.agent + ']' : '';
                            return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(4) + agent;
                        }
                    }
                }
            },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'minute', displayFormats: { minute: 'HH:mm' }, tooltipFormat: 'HH:mm:ss' },
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#a0a0b0', font: { size: 11 }, maxRotation: 0 }
                },
                y: Object.assign({
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: {
                        color: '#a0a0b0',
                        font: { family: "'JetBrains Mono', monospace", size: 11 },
                        callback: function (v) { return v.toFixed(3); }
                    }
                }, extraYOpts)
            }
        };
    }

    // ========================================================================
    // Chart initialization
    // ========================================================================

    function initEISVChart() {
        var upperCtx = document.getElementById('eisv-chart-upper');
        var lowerCtx = document.getElementById('eisv-chart-lower');
        if (!upperCtx || !lowerCtx) return;

        var CE = MetricColors.HEX.chartEnergy;
        var CI = MetricColors.HEX.chartIntegrity;
        var CC = MetricColors.HEX.chartCoherence;
        var CS = MetricColors.HEX.chartEntropy;
        var CV = MetricColors.HEX.chartVoid;

        // Upper chart: E, I, Coherence + ODE overlay
        var upperOpts = makeChartOptions({ grace: '5%' });
        upperOpts.plugins.equilibriumLines = [
            { value: 0.593, label: 'E eq \u22480.593', color: 'rgba(124,58,237,0.3)', dash: [3, 6] },
            { value: 0.595, label: 'I eq \u22480.595', color: 'rgba(16,185,129,0.3)', dash: [3, 6] },
            { value: 0.499, label: 'Coh eq \u22480.50', color: 'rgba(6,182,212,0.3)', dash: [3, 6] }
        ];

        var chartUpper = new Chart(upperCtx, {
            type: 'line',
            data: {
                datasets: [
                    // [0-2] Behavioral (primary)
                    { label: 'Energy (E)', borderColor: CE, backgroundColor: 'rgba(124,58,237,0.08)', fill: true, data: [], tension: 0.3, pointRadius: 3, pointHoverRadius: 5, borderWidth: 2 },
                    { label: 'Integrity (I)', borderColor: CI, backgroundColor: 'rgba(16,185,129,0.08)', fill: true, data: [], tension: 0.3, pointRadius: 3, pointHoverRadius: 5, borderWidth: 2 },
                    { label: 'Coherence', borderColor: CC, backgroundColor: 'transparent', data: [], tension: 0.3, pointRadius: 0, borderWidth: 2, borderDash: [6, 3] },
                    // [3-4] ODE overlay (dashed, dimmer)
                    { label: 'E (ODE)', borderColor: 'rgba(124,58,237,0.35)', backgroundColor: 'transparent', data: [], tension: 0.3, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 4], hidden: !state.get('showODE') },
                    { label: 'I (ODE)', borderColor: 'rgba(16,185,129,0.35)', backgroundColor: 'transparent', data: [], tension: 0.3, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 4], hidden: !state.get('showODE') }
                ]
            },
            options: upperOpts
        });

        // Lower chart: S, V + ODE overlay
        var lowerOpts = makeChartOptions({ grace: '10%' });
        lowerOpts.plugins.equilibriumLines = [
            { value: 0.012, label: 'S eq \u22480.012', color: 'rgba(245,158,11,0.3)', dash: [3, 6] },
            { value: 0.0, label: 'zero', color: 'rgba(255,255,255,0.1)', dash: [2, 4] }
        ];

        var chartLower = new Chart(lowerCtx, {
            type: 'line',
            data: {
                datasets: [
                    // [0-1] Behavioral (primary)
                    { label: 'Entropy (S)', borderColor: CS, backgroundColor: 'rgba(245,158,11,0.08)', fill: true, data: [], tension: 0.3, pointRadius: 3, pointHoverRadius: 5, borderWidth: 2 },
                    { label: 'Void (V)', borderColor: CV, backgroundColor: 'rgba(239,68,68,0.08)', fill: true, data: [], tension: 0.3, pointRadius: 3, pointHoverRadius: 5, borderWidth: 2 },
                    // [2-3] ODE overlay (dashed, dimmer)
                    { label: 'S (ODE)', borderColor: 'rgba(245,158,11,0.35)', backgroundColor: 'transparent', data: [], tension: 0.3, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 4], hidden: !state.get('showODE') },
                    { label: 'V (ODE)', borderColor: 'rgba(239,68,68,0.35)', backgroundColor: 'transparent', data: [], tension: 0.3, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 4], hidden: !state.get('showODE') }
                ]
            },
            options: lowerOpts
        });

        state.set({ eisvChartUpper: chartUpper, eisvChartLower: chartLower });
    }

    // ========================================================================
    // Add EISV data point (from WebSocket)
    // ========================================================================

    function addEISVDataPoint(data) {
        var eisvChartUpper = state.get('eisvChartUpper');
        var eisvChartLower = state.get('eisvChartLower');
        if (!eisvChartUpper || !eisvChartLower) return;

        var ts = new Date(data.timestamp);
        var tsMs = ts.getTime();
        var eisv = data.eisv || {};
        var agentId = data.agent_id || 'unknown';
        var agentName = data.agent_name || agentId;
        var agentEISVHistory = state.get('agentEISVHistory');
        var knownAgents = state.get('knownAgents');
        var selectedAgentView = state.get('selectedAgentView');

        // Extract ODE state from metrics (separate from behavioral EISV)
        var ode = (data.metrics && data.metrics.ode) || {};

        // Store in per-agent history
        if (!agentEISVHistory[agentId]) {
            agentEISVHistory[agentId] = [];
        }
        agentEISVHistory[agentId].push({
            ts: tsMs,
            name: agentName,
            E: eisv.E || 0,
            I: eisv.I || 0,
            S: eisv.S || 0,
            V: eisv.V || 0,
            coherence: data.coherence || 0,
            ode_E: ode.E != null ? ode.E : null,
            ode_I: ode.I != null ? ode.I : null,
            ode_S: ode.S != null ? ode.S : null,
            ode_V: ode.V != null ? ode.V : null
        });

        // Track known agents for dropdown
        if (!knownAgents.has(agentId)) {
            knownAgents.add(agentId);
            updateAgentDropdown();
        }

        // Trim old data from all agent histories
        // Keep at least MIN_SPARKLINE_POINTS per agent so slow check-in agents
        // (e.g. Vigil every 30min) still get sparklines
        var MIN_SPARKLINE_POINTS = 20;
        var cutoff = tsMs - EISV_WINDOW_MS;
        var aids = Object.keys(agentEISVHistory);
        for (var i = 0; i < aids.length; i++) {
            var hist = agentEISVHistory[aids[i]];
            if (hist.length > MIN_SPARKLINE_POINTS) {
                agentEISVHistory[aids[i]] = hist.filter(function (pt) { return pt.ts >= cutoff; });
                // Ensure we never trim below minimum
                if (agentEISVHistory[aids[i]].length < MIN_SPARKLINE_POINTS) {
                    agentEISVHistory[aids[i]] = hist.slice(-MIN_SPARKLINE_POINTS);
                }
            }
            if (agentEISVHistory[aids[i]].length === 0) {
                delete agentEISVHistory[aids[i]];
                knownAgents.delete(aids[i]);
            }
        }

        // Update chart based on selected view
        if (selectedAgentView === '__fleet__') {
            rebuildChartFromSelection();
        } else if (selectedAgentView === '__all__' || selectedAgentView === agentId) {
            var _a = agentName;
            eisvChartUpper.data.datasets[0].data.push({ x: ts, y: eisv.E || 0, agent: _a });
            eisvChartUpper.data.datasets[1].data.push({ x: ts, y: eisv.I || 0, agent: _a });
            eisvChartUpper.data.datasets[2].data.push({ x: ts, y: data.coherence || 0, agent: _a });
            if (ode.E != null) eisvChartUpper.data.datasets[3].data.push({ x: ts, y: ode.E, agent: _a });
            if (ode.I != null) eisvChartUpper.data.datasets[4].data.push({ x: ts, y: ode.I, agent: _a });
            eisvChartLower.data.datasets[0].data.push({ x: ts, y: eisv.S || 0, agent: _a });
            eisvChartLower.data.datasets[1].data.push({ x: ts, y: eisv.V || 0, agent: _a });
            if (ode.S != null) eisvChartLower.data.datasets[2].data.push({ x: ts, y: ode.S, agent: _a });
            if (ode.V != null) eisvChartLower.data.datasets[3].data.push({ x: ts, y: ode.V, agent: _a });

            var cutoffDate = new Date(cutoff);
            [eisvChartUpper, eisvChartLower].forEach(function (chart) {
                chart.data.datasets.forEach(function (ds) {
                    while (ds.data.length > 0 && ds.data[0].x < cutoffDate) {
                        ds.data.shift();
                    }
                    while (ds.data.length > EISV_MAX_POINTS) {
                        ds.data.shift();
                    }
                });
            });
            requestAnimationFrame(function () {
                eisvChartUpper.update('none');
                eisvChartLower.update('none');
            });
        }

        // Update info label
        var info = document.getElementById('eisv-chart-info');
        if (info) {
            var shortName = agentName.length > 16 ? agentName.substring(0, 16) + '...' : agentName;
            var viewLabel = selectedAgentView === '__fleet__' ? '(fleet avg)' :
                selectedAgentView === '__all__' ? '(all)' : '';
            info.innerHTML = '<span class="eisv-agent-label">' + escapeHtml(shortName) + ' ' + viewLabel + '</span>' +
                ' <span class="eisv-value" style="color:' + MetricColors.HEX.chartEnergy + '">E ' + (eisv.E || 0).toFixed(3) + '</span>' +
                ' <span class="eisv-value" style="color:' + MetricColors.HEX.chartIntegrity + '">I ' + (eisv.I || 0).toFixed(3) + '</span>' +
                ' <span class="eisv-value" style="color:' + MetricColors.HEX.chartEntropy + '">S ' + (eisv.S || 0).toFixed(4) + '</span>' +
                ' <span class="eisv-value" style="color:' + MetricColors.HEX.chartVoid + '">V ' + (eisv.V || 0).toFixed(5) + '</span>';
        }

        // Hide empty message
        var emptyMsg = document.getElementById('eisv-chart-empty');
        if (emptyMsg) emptyMsg.style.display = 'none';

        // Update Governance Pulse panel (filtered by pinned agent)
        var pinned = state.get('pinnedAgentId');
        if (!pinned || data.agent_id === pinned) {
            updateGovernancePulse(data);
        }
    }

    // ========================================================================
    // Drift gauges
    // ========================================================================

    function updateDriftGauge(index, value, trendInfo) {
        var fill = document.getElementById('drift-g-' + index);
        var valEl = document.getElementById('drift-v-' + index);
        var trendEl = document.getElementById('drift-trend-' + index);
        if (!fill) return;

        var clamped = Math.max(-0.5, Math.min(0.5, value));
        var pct = Math.abs(clamped) / 0.5 * 50;

        if (clamped >= 0) {
            fill.style.left = '50%';
            fill.style.width = pct + '%';
        } else {
            fill.style.left = (50 - pct) + '%';
            fill.style.width = pct + '%';
        }
        fill.style.background = MetricColors.forDrift(clamped, pct, 'hex');

        if (valEl) {
            valEl.textContent = (clamped >= 0 ? '+' : '') + clamped.toFixed(3);
            valEl.style.color = Math.abs(clamped) < 0.005 ? '' :
                clamped > 0 ? MetricColors.STATUS.danger.hex : MetricColors.STATUS.info.hex;
        }

        if (trendEl && trendInfo) {
            var trend = trendInfo.trend || 'stable';
            var strength = trendInfo.strength || 0;
            trendEl.textContent = TREND_ICONS[trend] || '';
            trendEl.style.color = TREND_COLORS[trend] || '#6b7280';
            trendEl.style.opacity = 0.5 + (strength * 0.5);
            trendEl.title = trend.replace('_', ' ') + (strength > 0.5 ? ' (strong)' : '');
        } else if (trendEl) {
            trendEl.textContent = '';
        }
    }

    // ========================================================================
    // Governance verdict
    // ========================================================================

    function updateGovernanceVerdict(data) {
        var verdict = document.getElementById('gov-verdict');
        if (!verdict) return;
        var label = verdict.querySelector('.verdict-label');
        var risk = data.risk;
        if (risk == null) return;

        var text, cls;
        if (risk < 0.35) { text = 'Approve'; cls = ''; }
        else if (risk < 0.60) { text = 'Proceed'; cls = 'risk-elevated'; }
        else if (risk < 0.70) { text = 'Pause'; cls = 'risk-high'; }
        else { text = 'Critical'; cls = 'risk-high'; }

        if (label) label.textContent = text;
        verdict.className = 'governance-verdict' + (cls ? ' ' + cls : '');
    }

    // ========================================================================
    // Data freshness
    // ========================================================================

    function updateDataFreshness(timestamp) {
        var el = document.getElementById('data-freshness');
        if (!el || !timestamp) return;
        state.set({ lastVitalsTimestamp: new Date(timestamp) });
        updateFreshnessDisplay();
    }

    function updateFreshnessDisplay() {
        var el = document.getElementById('data-freshness');
        var lastVitalsTimestamp = state.get('lastVitalsTimestamp');
        if (!el || !lastVitalsTimestamp) return;

        var ago = Math.floor((Date.now() - lastVitalsTimestamp.getTime()) / 1000);
        if (ago < 5) {
            el.textContent = 'just now';
            el.className = 'data-freshness fresh';
        } else if (ago < 60) {
            el.textContent = ago + 's ago';
            el.className = 'data-freshness fresh';
        } else if (ago < 300) {
            el.textContent = Math.floor(ago / 60) + 'm ago';
            el.className = 'data-freshness';
        } else {
            el.textContent = Math.floor(ago / 60) + 'm ago';
            el.className = 'data-freshness stale';
        }
    }

    // ========================================================================
    // Verdict badge + formatting helpers
    // ========================================================================

    function getVerdictBadge(verdict, risk) {
        var v = verdict || 'safe';
        if (!verdict && risk != null) {
            if (risk < 0.35) v = 'approve';
            else if (risk < 0.60) v = 'proceed';
            else if (risk < 0.70) v = 'pause';
            else v = 'critical';
        }
        var badges = {
            'safe': { text: 'A', cls: 'verdict-approve', title: 'Approve' },
            'approve': { text: 'A', cls: 'verdict-approve', title: 'Approve' },
            'caution': { text: 'C', cls: 'caution', title: 'Caution' },
            'proceed': { text: 'P', cls: 'verdict-proceed', title: 'Proceed' },
            'elevated': { text: 'P', cls: 'verdict-proceed', title: 'Proceed with caution' },
            'pause': { text: '!', cls: 'verdict-pause', title: 'Pause' },
            'high-risk': { text: '!', cls: 'verdict-pause', title: 'High Risk' },
            'critical': { text: 'X', cls: 'verdict-critical', title: 'Critical' }
        };
        return badges[v] || badges['safe'];
    }

    // ========================================================================
    // Events log
    // ========================================================================

    function addEventEntry(event) {
        var container = document.getElementById('events-log-entries');
        if (!container) return;

        var empty = container.querySelector('.pulse-log-empty');
        if (empty) empty.remove();

        var ts = event.timestamp ? new Date(event.timestamp) : new Date();
        var icon = EVENT_ICONS[event.type] || '\uD83D\uDCCC';
        var severityClass = SEVERITY_CLASSES[event.severity] || 'event-info';

        var entry = document.createElement('div');
        entry.className = 'pulse-log-entry ' + severityClass;
        entry.innerHTML =
            '<span class="event-icon">' + icon + '</span>' +
            '<span class="log-time">' + DataProcessor.formatTimestamp(ts) + '</span>' +
            '<span class="event-message">' + escapeHtml(event.message || event.type) + '</span>';

        if (event.reason) {
            entry.title = event.reason;
        }

        container.insertBefore(entry, container.firstChild);

        while (container.children.length > MAX_LOG_ENTRIES) {
            container.removeChild(container.lastChild);
        }

        var section = document.getElementById('pulse-log-section');
        if (section) section.classList.remove('pulse-log-empty');
    }

    function fetchInitialEvents() {
        authFetch('/api/events?limit=20')
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (data.success && data.events && data.events.length > 0) {
                    data.events.slice().reverse().forEach(function (event) {
                        addEventEntry(event);
                    });
                }
            })
            .catch(function (e) {
                console.debug('Could not fetch initial events:', e);
            });
    }

    // ========================================================================
    // Value animation
    // ========================================================================

    function updateValueWithGlow(element, newValue) {
        if (!element) return;
        var oldValue = element.textContent;
        element.textContent = newValue;
        if (oldValue !== newValue) {
            element.classList.add('live-update');
            setTimeout(function () { element.classList.remove('live-update'); }, 800);
        }
    }

    function animateValue(element, newValue, options) {
        if (!element) return;
        options = options || {};
        var duration = options.duration || 600;
        var decimals = options.decimals || 0;
        var prefix = options.prefix || '';
        var suffix = options.suffix || '';
        var oldText = element.textContent.replace(/[^0-9.-]/g, '');
        var oldValue = parseFloat(oldText);

        if (isNaN(oldValue) || isNaN(newValue) || oldValue === newValue) {
            element.textContent = prefix + (isNaN(newValue) ? '-' : newValue.toFixed(decimals)) + suffix;
            return;
        }

        element.classList.add('value-updating');
        var startTime = performance.now();
        var diff = newValue - oldValue;

        function step(currentTime) {
            var elapsed = currentTime - startTime;
            var progress = Math.min(elapsed / duration, 1);
            var eased = 1 - Math.pow(1 - progress, 3);
            var current = oldValue + diff * eased;
            element.textContent = prefix + current.toFixed(decimals) + suffix;

            if (progress < 1) {
                requestAnimationFrame(step);
            } else {
                element.textContent = prefix + newValue.toFixed(decimals) + suffix;
                setTimeout(function () { element.classList.remove('value-updating'); }, 300);
            }
        }
        requestAnimationFrame(step);
    }

    // ========================================================================
    // Governance Pulse panel
    // ========================================================================

    function updateGovernancePulse(data) {
        var agentNameEl = document.getElementById('pulse-agent-name');
        var agentName = data.agent_name || data.agent_id || 'unknown';
        if (agentNameEl) {
            var displayName = agentName.length > 20 ? agentName.substring(0, 17) + '...' : agentName;
            var isLocalDevice = agentName.toLowerCase().includes('lumen') || agentName.toLowerCase().includes('anima') || data.agent_id === 'mac-orchestrator';
            var localBadge = isLocalDevice ? ' <span class="local-device-badge" title="Physical Device">LUMEN</span>' : '';
            agentNameEl.innerHTML = escapeHtml(displayName) + localBadge;
            agentNameEl.title = agentName;
        }

        // Risk bar
        var risk = data.risk;
        if (risk != null) {
            var rBar = document.getElementById('v-risk');
            var rVal = document.getElementById('vv-risk');
            var rDetail = document.getElementById('vv-risk-detail');
            if (rBar) {
                rBar.style.width = (risk * 100).toFixed(0) + '%';
                rBar.style.background = MetricColors.forRisk(risk, 'hex');
            }
            updateValueWithGlow(rVal, risk.toFixed(3));

            if (rDetail) {
                var adjustment = data.risk_adjustment || 0;
                var rawRisk = data.risk_raw || risk;
                var reason = data.risk_reason || '';
                if (adjustment !== 0) {
                    var sign = adjustment > 0 ? '+' : '';
                    rDetail.textContent = '(' + sign + (adjustment * 100).toFixed(0) + '%)';
                    rDetail.title = reason || 'Base: ' + (rawRisk * 100).toFixed(1) + '%, Adjusted: ' + (risk * 100).toFixed(1) + '%';
                    rDetail.style.color = adjustment > 0 ? 'var(--risk-high, #ef4444)' : 'var(--risk-low, #22c55e)';
                } else {
                    rDetail.textContent = '';
                    rDetail.title = '';
                }
            }
        }

        // Governance input signals
        var inputs = data.inputs;
        if (inputs) {
            var cxBar = document.getElementById('v-complexity');
            var cxVal = document.getElementById('vv-complexity');
            if (cxBar && inputs.complexity != null) {
                cxBar.style.width = (inputs.complexity * 100).toFixed(0) + '%';
                var cx = inputs.complexity;
                cxBar.style.background = cx < 0.4 ? '#00f0ff' : cx < 0.7 ? '#eab308' : '#f97316';
            }
            if (inputs.complexity != null) updateValueWithGlow(cxVal, inputs.complexity.toFixed(2));

            var cfBar = document.getElementById('v-confidence');
            var cfVal = document.getElementById('vv-confidence');
            if (cfBar && inputs.confidence != null) {
                cfBar.style.width = (inputs.confidence * 100).toFixed(0) + '%';
                var cf = inputs.confidence;
                cfBar.style.background = cf < 0.5 ? '#f97316' : cf < 0.75 ? '#eab308' : '#22c55e';
            }
            if (inputs.confidence != null) updateValueWithGlow(cfVal, inputs.confidence.toFixed(2));

            var drift = inputs.ethical_drift;
            var driftTrends = data.drift_trends || {};
            if (drift && drift.length === 3) {
                for (var i = 0; i < 3; i++) {
                    var axis = DRIFT_AXES[i];
                    updateDriftGauge(i, drift[i], driftTrends[axis] || null);
                }
            }
        }

        // Verdict badge
        updateGovernanceVerdict(data);

        // Events log
        if (data.events && data.events.length > 0) {
            data.events.forEach(function (event) { addEventEntry(event); });
        }

        // Data freshness
        updateDataFreshness(data.timestamp);
    }

    // ========================================================================
    // WebSocket initialization
    // ========================================================================

    function initWebSocket() {
        if (typeof EISVWebSocket === 'undefined') {
            console.warn('EISVWebSocket not available');
            return;
        }

        var ws = new EISVWebSocket(
            function (data) {
                if (data.type === 'eisv_update') {
                    addEISVDataPoint(data);
                    updateAgentCardFromWS(data);
                    addActivityDataPoint(data);
                    // Feed global activity timeline
                    if (typeof TimelineModule !== 'undefined' && TimelineModule.onEISVUpdate) {
                        TimelineModule.onEISVUpdate(data);
                    }
                    // Feed residents panel — only acts if agent is a resident.
                    if (typeof ResidentsModule !== 'undefined' && ResidentsModule.onEISVUpdate) {
                        ResidentsModule.onEISVUpdate(data);
                    }
                } else if (data.type) {
                    // lifecycle_*, identity_*, knowledge_*, circuit_breaker_*
                    // were silently dropped before this branch existed.
                    if (typeof TimelineModule !== 'undefined' && TimelineModule.onGovernanceEvent) {
                        TimelineModule.onGovernanceEvent(data);
                    }
                    if (typeof ResidentsModule !== 'undefined' && ResidentsModule.onGovernanceEvent) {
                        ResidentsModule.onGovernanceEvent(data);
                    }
                    if (typeof AgentsModule !== 'undefined' && AgentsModule.onGovernanceEvent) {
                        AgentsModule.onGovernanceEvent(data);
                    }
                }
            },
            function (status) {
                var wsStatus = document.getElementById('ws-status');
                var wsDot = wsStatus ? wsStatus.querySelector('.ws-dot') : null;
                var wsLabel = wsStatus ? wsStatus.querySelector('.ws-label') : null;
                if (wsDot) {
                    wsDot.className = 'ws-dot ' + status;
                }
                if (wsStatus) {
                    var titles = {
                        connected: 'Live via WebSocket',
                        disconnected: 'WebSocket disconnected',
                        reconnecting: 'WebSocket reconnecting...',
                        polling: 'Live via HTTP polling (WebSocket unavailable)'
                    };
                    wsStatus.title = titles[status] || status;
                }
                if (wsLabel) {
                    wsLabel.textContent = status === 'polling' ? 'Polling' : 'Live';
                }
                console.log('[WS] Status:', status);
            }
        );

        state.set({ eisvWebSocket: ws });
        ws.connect();
    }

    // ========================================================================
    // Activity sparkline
    // ========================================================================

    var SPARKLINE_BUCKET_MS = 5 * 60 * 1000;  // 5-minute buckets
    var SPARKLINE_WINDOW_MS = 60 * 60 * 1000; // 1-hour window
    var SPARKLINE_BUCKETS = 12;

    // Client-side activity tracking (supplements server data)
    var activityBuckets = null; // initialized on first data

    function initActivitySparkline() {
        var ctx = document.getElementById('activity-sparkline');
        if (!ctx) return;

        var chart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [
                    { label: 'proceed', data: [], backgroundColor: '#22c55e', barPercentage: 0.9, categoryPercentage: 0.9 },
                    { label: 'guide', data: [], backgroundColor: '#eab308', barPercentage: 0.9, categoryPercentage: 0.9 },
                    { label: 'pause', data: [], backgroundColor: '#ef4444', barPercentage: 0.9, categoryPercentage: 0.9 }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 200 },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(13,13,18,0.9)',
                        bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
                        callbacks: {
                            title: function (items) {
                                return items[0] ? items[0].label : '';
                            }
                        }
                    }
                },
                scales: {
                    x: { stacked: true, display: false },
                    y: {
                        stacked: true,
                        display: false,
                        beginAtZero: true
                    }
                }
            }
        });

        state.set({ activitySparkline: chart });

        // Fetch initial data from server
        fetchActivityData(chart);
    }

    function fetchActivityData(chart) {
        if (!chart) chart = state.get('activitySparkline');
        if (!chart) return;

        authFetch('/api/activity?window=60&bucket=5')
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (data.success && data.buckets) {
                    // Store buckets and render
                    activityBuckets = data.buckets;
                    renderActivitySparkline(chart);
                }
            })
            .catch(function (e) {
                console.debug('Could not fetch activity data:', e);
            });
    }

    function renderActivitySparkline(chart) {
        if (!chart || !activityBuckets) return;

        var labels = [];
        var proceedData = [];
        var guideData = [];
        var pauseData = [];

        for (var i = 0; i < activityBuckets.length; i++) {
            var b = activityBuckets[i];
            var d = new Date(b.ts * 1000);
            labels.push(d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
            proceedData.push(b.proceed || 0);
            guideData.push(b.guide || 0);
            pauseData.push(b.pause || 0);
        }

        chart.data.labels = labels;
        chart.data.datasets[0].data = proceedData;
        chart.data.datasets[1].data = guideData;
        chart.data.datasets[2].data = pauseData;
        chart.update('none');
    }

    function addActivityDataPoint(data) {
        var chart = state.get('activitySparkline');
        if (!chart) return;

        // Determine verdict action from the eisv_update data
        var decision = data.decision || {};
        var action = (typeof decision === 'object' ? decision.action : decision) || 'proceed';

        var now = Date.now() / 1000; // epoch seconds
        var bucketSize = 300; // 5 minutes in seconds
        var currentBucketStart = Math.floor(now / bucketSize) * bucketSize;

        // Initialize buckets if needed
        if (!activityBuckets) {
            activityBuckets = [];
            for (var i = SPARKLINE_BUCKETS - 1; i >= 0; i--) {
                activityBuckets.push({
                    ts: currentBucketStart - (i * bucketSize),
                    proceed: 0,
                    guide: 0,
                    pause: 0
                });
            }
        }

        // Check if we need to shift buckets (new 5-min window)
        var lastBucket = activityBuckets[activityBuckets.length - 1];
        if (currentBucketStart > lastBucket.ts) {
            // Add new bucket(s) and trim old ones
            var nextTs = lastBucket.ts + bucketSize;
            while (nextTs <= currentBucketStart) {
                activityBuckets.push({ ts: nextTs, proceed: 0, guide: 0, pause: 0 });
                nextTs += bucketSize;
            }
            // Keep only the last SPARKLINE_BUCKETS
            while (activityBuckets.length > SPARKLINE_BUCKETS) {
                activityBuckets.shift();
            }
        }

        // Increment the current bucket
        var last = activityBuckets[activityBuckets.length - 1];
        if (action === 'guide') {
            last.guide += 1;
        } else if (action === 'pause' || action === 'reject') {
            last.pause += 1;
        } else {
            last.proceed += 1;
        }

        renderActivitySparkline(chart);
    }

    // ========================================================================
    // Agent card flash on WS update
    // ========================================================================

    function updateAgentCardFromWS(data) {
        var agentCards = document.querySelectorAll('.agent-item');
        for (var i = 0; i < agentCards.length; i++) {
            var card = agentCards[i];
            var nameEl = card.querySelector('.agent-name');
            if (nameEl && (nameEl.textContent.indexOf(data.agent_name) !== -1 || nameEl.textContent.indexOf(data.agent_id) !== -1)) {
                card.classList.add('just-updated');
                (function (c) {
                    setTimeout(function () { c.classList.remove('just-updated'); }, SCROLL_FEEDBACK_MS);
                })(card);
                break;
            }
        }
    }

    // ========================================================================
    // Self-initialization
    // ========================================================================

    function onDOMReady() {
        // Wire up agent dropdown
        var select = document.getElementById('eisv-agent-select');
        if (select) {
            select.addEventListener('change', function (e) {
                state.set({ selectedAgentView: e.target.value });
                rebuildChartFromSelection();
            });
        }

        // Wire up clear chart button
        var clearBtn = document.getElementById('eisv-chart-clear');
        if (clearBtn) {
            clearBtn.addEventListener('click', function () {
                var upper = state.get('eisvChartUpper');
                var lower = state.get('eisvChartLower');
                [upper, lower].forEach(function (chart) {
                    if (chart) {
                        chart.data.datasets.forEach(function (ds) { ds.data = []; });
                    }
                });
                requestAnimationFrame(function () {
                    if (upper) upper.update();
                    if (lower) lower.update();
                });
                var emptyMsg = document.getElementById('eisv-chart-empty');
                if (emptyMsg) emptyMsg.style.display = '';
                var info = document.getElementById('eisv-chart-info');
                if (info) info.innerHTML = '';
            });
        }

        // Wire up ODE overlay toggle
        var odeBtn = document.getElementById('ode-toggle');
        if (odeBtn) {
            // Set initial visual state
            if (state.get('showODE')) odeBtn.classList.add('active');

            odeBtn.addEventListener('click', function () {
                var show = !state.get('showODE');
                state.set({ showODE: show });
                localStorage.setItem('unitares_show_ode', show ? 'true' : 'false');
                odeBtn.classList.toggle('active', show);

                var upper = state.get('eisvChartUpper');
                var lower = state.get('eisvChartLower');
                if (upper) {
                    upper.data.datasets[3].hidden = !show;  // E (ODE)
                    upper.data.datasets[4].hidden = !show;  // I (ODE)
                    upper.update('none');
                }
                if (lower) {
                    lower.data.datasets[2].hidden = !show;  // S (ODE)
                    lower.data.datasets[3].hidden = !show;  // V (ODE)
                    lower.update('none');
                }
            });
        }

        // Pin state reactivity
        var scopeLabel = document.getElementById('pulse-scope-label');
        var unpinBtn = document.getElementById('pulse-unpin-btn');
        var pulseAgentName = document.getElementById('pulse-agent-name');
        var freshnessEl = document.getElementById('data-freshness');

        function applyPinState(pinnedId) {
            var pinnedName = state.get('pinnedAgentName');
            if (pinnedId) {
                if (scopeLabel) scopeLabel.textContent = 'Pinned';
                if (unpinBtn) unpinBtn.classList.remove('hidden');
                if (pulseAgentName) {
                    pulseAgentName.textContent = pinnedName || pinnedId;
                    pulseAgentName.title = pinnedName || pinnedId;
                }
                if (freshnessEl) freshnessEl.textContent = 'Waiting for data from ' + (pinnedName || pinnedId) + '...';
            } else {
                if (scopeLabel) scopeLabel.textContent = 'Last check-in';
                if (unpinBtn) unpinBtn.classList.add('hidden');
            }
        }

        state.on('pinnedAgentId', function (newVal) {
            applyPinState(newVal);
        });

        // Apply initial pin state (restored from localStorage)
        applyPinState(state.get('pinnedAgentId'));

        if (unpinBtn) {
            unpinBtn.addEventListener('click', function () {
                state.set({ pinnedAgentId: null, pinnedAgentName: null });
                localStorage.removeItem('unitares_pinned_agent_id');
                localStorage.removeItem('unitares_pinned_agent_name');
            });
        }

        // Initialize charts and WebSocket (deferred for canvas dimensions)
        requestAnimationFrame(function () {
            initEISVChart();
            initActivitySparkline();
            initWebSocket();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onDOMReady);
    } else {
        onDOMReady();
    }

    // Periodic updates
    setInterval(updateFreshnessDisplay, 5000);

    // ========================================================================
    // Public API
    // ========================================================================

    window.EISVChartsModule = {
        initEISVChart: initEISVChart,
        initWebSocket: initWebSocket,
        addEISVDataPoint: addEISVDataPoint,
        updateGovernancePulse: updateGovernancePulse,
        updateAgentCardFromWS: updateAgentCardFromWS,
        updateValueWithGlow: updateValueWithGlow,
        animateValue: animateValue,
        getVerdictBadge: getVerdictBadge,
        addEventEntry: addEventEntry,
        fetchInitialEvents: fetchInitialEvents,
        rebuildChartFromSelection: rebuildChartFromSelection,
        updateAgentDropdown: updateAgentDropdown,
        initActivitySparkline: initActivitySparkline,
        addActivityDataPoint: addActivityDataPoint
    };
})();
