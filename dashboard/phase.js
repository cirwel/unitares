/**
 * Unitares Phase Space Visualization
 *
 * Renders agents as particles in the E-I phase plane.
 * Basin contours, flow field, and live updates via WebSocket.
 */
(function () {
    'use strict';

    // ========================================================================
    // Configuration
    // ========================================================================

    var CFG = {
        // Basin band boundaries along the I axis. These are an illustrative
        // 1-D PROJECTION of the engine's multi-dimensional classifier
        // (config/governance_config.py classify_basin, over I, coherence, |V|,
        // risk, plus the BASIN_HIGH box) — the background bands cannot show the
        // full classifier, so the authoritative per-agent label is the `basin`
        // field rendered in each dot's tooltip, not the band an agent sits in.
        // These values are SOURCED LIVE from the server at init via
        // config(action='get') -> thresholds.basin_low_i_ceil / basin_high_i_min
        // (the I-axis breakpoints of classify_basin), so the bands cannot drift
        // from the engine. The literals below are only a fallback if that fetch
        // fails; they mirror BASIN_LOW_I_CEIL (0.5) and BASIN_HIGH.I_min (0.7).
        basin: { low: 0.5, high: 0.7 },
        // Steady-state equilibrium (governance_config.py calibration note)
        equilibrium: { E: 0.70, I: 0.75 },
        // ODE params (governance_config.py)
        ode: {
            ALPHA: 0.5, K: 0.1, MU: 0.8, DELTA: 0.4,
            KAPPA: 0.3, GAMMA_I: 0.3, BETA_E: 0.1, BETA_I: 0.05
        },
        margin: { top: 30, right: 30, bottom: 55, left: 60 },
        dot: { min: 5, max: 20 },
        refreshMs: 30000,
        transition: 1200
    };

    // ========================================================================
    // State
    // ========================================================================

    var agentData = [];
    var svg, g, xScale, yScale, W, H;
    var tooltipEl = document.getElementById('tooltip');
    var statusEl = document.getElementById('status');
    var ws = null;
    var refreshTimer = null;
    var connectionState = 'disconnected'; // 'connected' | 'polling' | 'disconnected'

    // ========================================================================
    // API helpers
    // ========================================================================

    function getToken() {
        return localStorage.getItem('unitares_api_token') ||
            new URLSearchParams(window.location.search).get('token');
    }

    function callTool(name, args) {
        var headers = { 'Content-Type': 'application/json' };
        var token = getToken();
        if (token) headers['Authorization'] = 'Bearer ' + token;

        return fetch('/v1/tools/call', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ name: name, arguments: args || {} })
        })
        .then(function (resp) {
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.json();
        })
        .then(function (data) {
            if (data.error) throw new Error(data.error);
            var result = data.result;
            if (typeof result === 'string') {
                try { result = JSON.parse(result); } catch (e) { /* keep as string */ }
            }
            return result;
        });
    }

    // ========================================================================
    // Color / sizing helpers
    // ========================================================================

    function voidColor(v) {
        var clamped = Math.max(-0.5, Math.min(0.5, v || 0));
        var t = (clamped + 0.5); // 0=blue, 0.5=white, 1=amber
        var r, gg, b;
        if (t < 0.5) {
            var p = t / 0.5;
            r = Math.round(96 + (226 - 96) * p);
            gg = Math.round(165 + (232 - 165) * p);
            b = Math.round(250 + (240 - 250) * p);
        } else {
            var p2 = (t - 0.5) / 0.5;
            r = Math.round(226 + (245 - 226) * p2);
            gg = Math.round(232 + (158 - 232) * p2);
            b = Math.round(240 + (11 - 240) * p2);
        }
        return 'rgb(' + r + ',' + gg + ',' + b + ')';
    }

    function voidColorAlpha(v, alpha) {
        var clamped = Math.max(-0.5, Math.min(0.5, v || 0));
        var t = (clamped + 0.5);
        var r, gg, b;
        if (t < 0.5) {
            var p = t / 0.5;
            r = Math.round(96 + (226 - 96) * p);
            gg = Math.round(165 + (232 - 165) * p);
            b = Math.round(250 + (240 - 250) * p);
        } else {
            var p2 = (t - 0.5) / 0.5;
            r = Math.round(226 + (245 - 226) * p2);
            gg = Math.round(232 + (158 - 232) * p2);
            b = Math.round(240 + (11 - 240) * p2);
        }
        return 'rgba(' + r + ',' + gg + ',' + b + ',' + alpha + ')';
    }

    function dotRadius(s) {
        var t = Math.max(0, Math.min(1, s || 0));
        return CFG.dot.max - t * (CFG.dot.max - CFG.dot.min);
    }

    function dotOpacity(agent) {
        if (!agent.last_update) return 0.3;
        var age = Date.now() - new Date(agent.last_update).getTime();
        var hrs = age / 3600000;
        if (hrs < 1) return 1.0;
        if (hrs < 6) return 0.8;
        if (hrs < 24) return 0.6;
        return 0.35;
    }

    function agentLabel(a) {
        return a.label || a.display_name || a.name || (a.agent_id || '?').slice(0, 12);
    }

    // Authoritative per-agent basin (server-computed classify_basin over the full
    // 6-input classifier). This is the TRUE classification, drawn as the particle
    // ring — so a dot's basin no longer has to be inferred from its I-axis band
    // (which is only a 1-D projection and can disagree). No ring when basin absent.
    function basinStroke(basin) {
        if (basin === 'high') return 'rgba(52,211,153,0.9)';
        if (basin === 'boundary') return 'rgba(251,191,36,0.9)';
        if (basin === 'low') return 'rgba(96,165,250,0.95)';
        return 'rgba(255,255,255,0.15)';
    }

    // ========================================================================
    // SVG setup
    // ========================================================================

    function setupSVG() {
        var container = document.getElementById('phase-container');
        var rect = container.getBoundingClientRect();

        W = rect.width - CFG.margin.left - CFG.margin.right;
        H = rect.height - CFG.margin.top - CFG.margin.bottom;

        xScale = d3.scaleLinear().domain([0, 1]).range([0, W]);
        yScale = d3.scaleLinear().domain([0, 1]).range([H, 0]);

        svg = d3.select('#phase-container')
            .append('svg')
            .attr('width', rect.width)
            .attr('height', rect.height);

        g = svg.append('g')
            .attr('transform', 'translate(' + CFG.margin.left + ',' + CFG.margin.top + ')');

        // Glow filter
        var defs = svg.append('defs');
        var filter = defs.append('filter')
            .attr('id', 'glow')
            .attr('x', '-80%').attr('y', '-80%')
            .attr('width', '260%').attr('height', '260%');
        filter.append('feGaussianBlur')
            .attr('in', 'SourceGraphic')
            .attr('stdDeviation', '5')
            .attr('result', 'blur');
        var merge = filter.append('feMerge');
        merge.append('feMergeNode').attr('in', 'blur');
        merge.append('feMergeNode').attr('in', 'SourceGraphic');

        // Soft outer glow (larger, dimmer)
        var filter2 = defs.append('filter')
            .attr('id', 'glow-outer')
            .attr('x', '-100%').attr('y', '-100%')
            .attr('width', '300%').attr('height', '300%');
        filter2.append('feGaussianBlur')
            .attr('in', 'SourceGraphic')
            .attr('stdDeviation', '12');
    }

    // ========================================================================
    // Static layers
    // ========================================================================

    function drawBasins() {
        var basins = g.append('g').attr('class', 'basins');

        // Low basin fill
        basins.append('rect')
            .attr('x', 0).attr('y', yScale(CFG.basin.low))
            .attr('width', W).attr('height', yScale(0) - yScale(CFG.basin.low))
            .attr('fill', 'rgba(96, 165, 250, 0.03)');

        // Transitional fill
        basins.append('rect')
            .attr('x', 0).attr('y', yScale(CFG.basin.high))
            .attr('width', W).attr('height', yScale(CFG.basin.low) - yScale(CFG.basin.high))
            .attr('fill', 'rgba(251, 191, 36, 0.025)');

        // High basin fill
        basins.append('rect')
            .attr('x', 0).attr('y', 0)
            .attr('width', W).attr('height', yScale(CFG.basin.high))
            .attr('fill', 'rgba(52, 211, 153, 0.03)');

        // Boundary lines
        [CFG.basin.low, CFG.basin.high].forEach(function (b) {
            basins.append('line')
                .attr('x1', 0).attr('y1', yScale(b))
                .attr('x2', W).attr('y2', yScale(b))
                .attr('stroke', 'rgba(255,255,255,0.07)')
                .attr('stroke-dasharray', '8,5')
                .attr('stroke-width', 0.8);
        });

        // I-axis bands — a 1-D projection of the classifier (integrity breakpoints
        // only). The authoritative per-agent basin is the particle RING, not the
        // band a dot sits in; labels say "low/high I" to avoid over-claiming.
        var labels = [
            { text: 'low I', y: 0.25, color: '96,165,250' },
            { text: 'mid I', y: 0.6, color: '251,191,36' },
            { text: 'high I', y: 0.85, color: '52,211,153' }
        ];
        labels.forEach(function (l) {
            basins.append('text')
                .attr('x', W - 10).attr('y', yScale(l.y))
                .attr('text-anchor', 'end')
                .attr('fill', 'rgba(' + l.color + ',0.18)')
                .attr('font-size', '10px')
                .attr('letter-spacing', '1.5px')
                .text(l.text);
        });
    }

    function drawEquilibrium() {
        var cx = xScale(CFG.equilibrium.E);
        var cy = yScale(CFG.equilibrium.I);
        var eq = g.append('g').attr('class', 'equilibrium');

        // Soft radial glow rings
        [60, 35, 18].forEach(function (r, i) {
            eq.append('circle')
                .attr('cx', cx).attr('cy', cy).attr('r', r)
                .attr('fill', 'rgba(52,211,153,' + (0.015 + i * 0.01) + ')')
                .attr('stroke', 'none');
        });

        // Crosshair
        var sz = 10;
        eq.append('line')
            .attr('x1', cx - sz).attr('y1', cy)
            .attr('x2', cx + sz).attr('y2', cy)
            .attr('stroke', 'rgba(52,211,153,0.12)').attr('stroke-width', 0.5);
        eq.append('line')
            .attr('x1', cx).attr('y1', cy - sz)
            .attr('x2', cx).attr('y2', cy + sz)
            .attr('stroke', 'rgba(52,211,153,0.12)').attr('stroke-width', 0.5);

        // Label
        eq.append('text')
            .attr('x', cx + 14).attr('y', cy + 3)
            .attr('fill', 'rgba(52,211,153,0.2)')
            .attr('font-size', '9px')
            .attr('letter-spacing', '0.5px')
            .text('EQ');
    }

    function drawFlowField() {
        var field = g.append('g').attr('class', 'flow-field');
        var gridN = 14;
        var S_ss = 0.18;
        var C_ss = 0.50;

        for (var ei = 1; ei < gridN; ei++) {
            for (var ii = 1; ii < gridN; ii++) {
                var e = ei / gridN;
                var i_val = ii / gridN;

                // ODE-derived flow direction
                var dE = CFG.ode.ALPHA * (i_val - e) - CFG.ode.BETA_E * e * S_ss + CFG.ode.KAPPA * C_ss;
                var dI = CFG.ode.GAMMA_I * i_val * (1 - i_val) - CFG.ode.K * S_ss + CFG.ode.BETA_I * C_ss;

                var mag = Math.sqrt(dE * dE + dI * dI);
                if (mag < 0.005) continue;

                var arrowLen = Math.min(mag * 50, 12);
                var nx = dE / mag;
                var ny = dI / mag;

                var x1 = xScale(e);
                var y1 = yScale(i_val);
                var x2 = x1 + nx * arrowLen;
                var y2 = y1 - ny * arrowLen; // SVG y-inverted

                var opacity = Math.min(mag * 1.2, 0.10);

                field.append('line')
                    .attr('x1', x1).attr('y1', y1)
                    .attr('x2', x2).attr('y2', y2)
                    .attr('stroke', 'rgba(255,255,255,' + opacity + ')')
                    .attr('stroke-width', 0.6)
                    .attr('stroke-linecap', 'round');

                // Tiny arrowhead dot
                field.append('circle')
                    .attr('cx', x2).attr('cy', y2).attr('r', 0.8)
                    .attr('fill', 'rgba(255,255,255,' + opacity + ')');
            }
        }
    }

    function drawAxes() {
        // X axis
        g.append('g')
            .attr('transform', 'translate(0,' + H + ')')
            .call(d3.axisBottom(xScale).ticks(5).tickFormat(d3.format('.1f')))
            .call(function (ax) {
                ax.selectAll('line, path').attr('stroke', 'rgba(255,255,255,0.1)');
                ax.selectAll('text').attr('fill', 'rgba(255,255,255,0.3)').attr('font-size', '10px');
            });

        // Y axis
        g.append('g')
            .call(d3.axisLeft(yScale).ticks(5).tickFormat(d3.format('.1f')))
            .call(function (ax) {
                ax.selectAll('line, path').attr('stroke', 'rgba(255,255,255,0.1)');
                ax.selectAll('text').attr('fill', 'rgba(255,255,255,0.3)').attr('font-size', '10px');
            });

        // Axis labels
        g.append('text')
            .attr('x', W / 2).attr('y', H + 42)
            .attr('text-anchor', 'middle')
            .attr('fill', 'rgba(255,255,255,0.35)')
            .attr('font-size', '11px')
            .attr('letter-spacing', '2px')
            .text('ENERGY');

        g.append('text')
            .attr('transform', 'rotate(-90)')
            .attr('x', -H / 2).attr('y', -42)
            .attr('text-anchor', 'middle')
            .attr('fill', 'rgba(255,255,255,0.35)')
            .attr('font-size', '11px')
            .attr('letter-spacing', '2px')
            .text('INTEGRITY');
    }

    // ========================================================================
    // Agent rendering (D3 data join with transitions)
    // ========================================================================

    function renderAgents(agents) {
        agentData = agents;

        // Data join keyed by agent_id
        var dots = g.selectAll('.agent-group')
            .data(agents, function (d) { return d.agent_id; });

        // EXIT
        dots.exit()
            .transition().duration(CFG.transition / 2)
            .attr('opacity', 0)
            .remove();

        // ENTER
        var enter = dots.enter()
            .append('g')
            .attr('class', 'agent-group')
            .attr('transform', function (d) {
                var m = d.metrics || {};
                return 'translate(' + xScale(m.E || 0.5) + ',' + yScale(m.I || 0.5) + ')';
            })
            .attr('opacity', 0);

        // Outer glow circle
        enter.append('circle')
            .attr('class', 'agent-glow')
            .attr('r', function (d) { return dotRadius((d.metrics || {}).S) * 2; })
            .attr('fill', function (d) { return voidColorAlpha((d.metrics || {}).V, 0.08); })
            .attr('filter', 'url(#glow-outer)');

        // Core circle. Fill = V (valence), size = S (entropy), ring = authoritative basin.
        enter.append('circle')
            .attr('class', 'agent-core')
            .attr('r', function (d) { return dotRadius((d.metrics || {}).S); })
            .attr('fill', function (d) { return voidColor((d.metrics || {}).V); })
            .attr('stroke', function (d) { return basinStroke((d.metrics || {}).basin); })
            .attr('stroke-width', 2.5)
            .attr('filter', 'url(#glow)')
            .style('cursor', 'pointer');

        // Label
        enter.append('text')
            .attr('class', 'agent-label')
            .attr('y', function (d) { return dotRadius((d.metrics || {}).S) + 14; })
            .attr('text-anchor', 'middle')
            .attr('fill', 'rgba(255,255,255,0.35)')
            .attr('font-size', '9px')
            .attr('letter-spacing', '0.5px')
            .text(function (d) { return agentLabel(d); });

        // Hover events
        enter.on('mouseenter', function (event, d) { showTooltip(event, d); })
            .on('mousemove', function (event, d) { moveTooltip(event); })
            .on('mouseleave', hideTooltip);

        // ENTER transition
        enter.transition().duration(CFG.transition)
            .attr('opacity', function (d) { return dotOpacity(d); });

        // UPDATE (merge enter + update)
        var merged = enter.merge(dots);

        merged.transition().duration(CFG.transition)
            .attr('transform', function (d) {
                var m = d.metrics || {};
                return 'translate(' + xScale(m.E || 0.5) + ',' + yScale(m.I || 0.5) + ')';
            })
            .attr('opacity', function (d) { return dotOpacity(d); });

        merged.select('.agent-glow')
            .transition().duration(CFG.transition)
            .attr('r', function (d) { return dotRadius((d.metrics || {}).S) * 2; })
            .attr('fill', function (d) { return voidColorAlpha((d.metrics || {}).V, 0.08); });

        merged.select('.agent-core')
            .transition().duration(CFG.transition)
            .attr('r', function (d) { return dotRadius((d.metrics || {}).S); })
            .attr('fill', function (d) { return voidColor((d.metrics || {}).V); })
            .attr('stroke', function (d) { return basinStroke((d.metrics || {}).basin); });

        merged.select('.agent-label')
            .transition().duration(CFG.transition)
            .attr('y', function (d) { return dotRadius((d.metrics || {}).S) + 14; })
            .text(function (d) { return agentLabel(d); });
    }

    // ========================================================================
    // Tooltip
    // ========================================================================

    function showTooltip(event, d) {
        var m = d.metrics || {};
        var verdict = m.verdict || m.behavioral_verdict || '-';
        var basin = m.basin || '-';
        var coherence = m.coherence != null ? Number(m.coherence).toFixed(3) : '-';

        var html = '<div class="tt-name">' + escapeHtml(agentLabel(d)) + '</div>';
        html += '<div class="tt-row"><span class="tt-label">E</span><span class="tt-value">' + fmt(m.E) + '</span></div>';
        html += '<div class="tt-row"><span class="tt-label">I</span><span class="tt-value">' + fmt(m.I) + '</span></div>';
        html += '<div class="tt-row"><span class="tt-label">S</span><span class="tt-value">' + fmt(m.S) + '</span></div>';
        html += '<div class="tt-row"><span class="tt-label">V</span><span class="tt-value">' + fmt(m.V) + '</span></div>';
        html += '<div class="tt-row"><span class="tt-label">C</span><span class="tt-value">' + coherence + '</span></div>';
        html += '<div class="tt-verdict">';
        html += '<div class="tt-row"><span class="tt-label">basin</span><span class="tt-value">' + escapeHtml(basin) + '</span></div>';
        html += '<div class="tt-row"><span class="tt-label">verdict</span><span class="tt-value">' + escapeHtml(verdict) + '</span></div>';
        html += '</div>';

        tooltipEl.innerHTML = html;
        tooltipEl.classList.add('visible');
        moveTooltip(event);
    }

    function moveTooltip(event) {
        var x = event.clientX + 16;
        var y = event.clientY - 10;
        // Keep on screen
        var tw = tooltipEl.offsetWidth;
        var th = tooltipEl.offsetHeight;
        if (x + tw > window.innerWidth - 10) x = event.clientX - tw - 16;
        if (y + th > window.innerHeight - 10) y = window.innerHeight - th - 10;
        if (y < 10) y = 10;
        tooltipEl.style.left = x + 'px';
        tooltipEl.style.top = y + 'px';
    }

    function hideTooltip() {
        tooltipEl.classList.remove('visible');
    }

    function fmt(v) {
        return v != null ? Number(v).toFixed(3) : '-';
    }

    function escapeHtml(s) {
        var div = document.createElement('div');
        div.textContent = s || '';
        return div.innerHTML;
    }

    // ========================================================================
    // Data loading
    // ========================================================================

    function loadAgents() {
        return callTool('agent', {
            action: 'list',
            include_metrics: true,
            recent_days: 30,
            limit: 50,
            min_updates: 3
        }).then(function (result) {
            if (!result) return [];

            // Flatten all lifecycle categories into one array
            var all = [];
            var agentsByStatus = result.agents || result;
            if (Array.isArray(agentsByStatus)) {
                all = agentsByStatus;
            } else {
                var categories = ['active', 'waiting_input', 'paused', 'archived'];
                categories.forEach(function (cat) {
                    if (agentsByStatus[cat] && Array.isArray(agentsByStatus[cat])) {
                        agentsByStatus[cat].forEach(function (a) { all.push(a); });
                    }
                });
            }

            // Filter: must have EISV metrics
            return all.filter(function (a) {
                var m = a.metrics;
                return m && m.E != null && m.I != null;
            });
        });
    }

    function loadAndRender() {
        loadAgents()
            .then(function (agents) {
                renderAgents(agents);
                setStatus('polling');
            })
            .catch(function (err) {
                console.error('[Phase] Load error:', err);
                setStatus('disconnected');
            });
    }

    // ========================================================================
    // WebSocket for live updates
    // ========================================================================

    function connectWebSocket() {
        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var url = protocol + '//' + window.location.host + '/ws/eisv';
        var token = getToken();
        if (token) url += '?token=' + encodeURIComponent(token);

        try {
            ws = new WebSocket(url);
        } catch (e) {
            console.warn('[Phase] WebSocket not available');
            return;
        }

        ws.onopen = function () {
            console.log('[Phase] WebSocket connected');
            setStatus('connected');
        };

        ws.onmessage = function (event) {
            try {
                var msg = JSON.parse(event.data);
                if (msg.type === 'eisv_update' && msg.agent_id) {
                    updateAgentFromWS(msg);
                }
            } catch (e) { /* ignore parse errors */ }
        };

        ws.onclose = function () {
            console.log('[Phase] WebSocket closed, reconnecting in 5s');
            setStatus('polling');
            setTimeout(connectWebSocket, 5000);
        };

        ws.onerror = function () {
            setStatus('polling');
        };
    }

    function updateAgentFromWS(msg) {
        // Find and update the agent in our data, then re-render
        var found = false;
        for (var i = 0; i < agentData.length; i++) {
            if (agentData[i].agent_id === msg.agent_id) {
                if (msg.E != null) agentData[i].metrics.E = msg.E;
                if (msg.I != null) agentData[i].metrics.I = msg.I;
                if (msg.S != null) agentData[i].metrics.S = msg.S;
                if (msg.V != null) agentData[i].metrics.V = msg.V;
                if (msg.coherence != null) agentData[i].metrics.coherence = msg.coherence;
                agentData[i].last_update = new Date().toISOString();
                found = true;
                break;
            }
        }
        if (found) renderAgents(agentData);
    }

    // ========================================================================
    // Polling fallback
    // ========================================================================

    function startRefresh() {
        if (refreshTimer) clearInterval(refreshTimer);
        refreshTimer = setInterval(loadAndRender, CFG.refreshMs);
    }

    // ========================================================================
    // Status indicator
    // ========================================================================

    function setStatus(state) {
        connectionState = state;
        var dot = statusEl.querySelector('.dot');
        dot.className = 'dot ' + state;
        var labels = { connected: 'live', polling: 'polling', disconnected: 'offline' };
        var count = agentData.length;
        statusEl.innerHTML = '<span class="dot ' + state + '"></span>' +
            labels[state] + (count ? ' &middot; ' + count + ' agents' : '');
    }

    // ========================================================================
    // Resize handler
    // ========================================================================

    var resizeTimeout;
    window.addEventListener('resize', function () {
        clearTimeout(resizeTimeout);
        resizeTimeout = setTimeout(function () {
            // Rebuild everything
            d3.select('#phase-container svg').remove();
            setupSVG();
            drawBasins();
            drawEquilibrium();
            drawFlowField();
            drawAxes();
            renderAgents(agentData);
        }, 200);
    });

    // ========================================================================
    // Init
    // ========================================================================

    // Source basin band breakpoints from the engine's own constants so the
    // bands cannot drift from classify_basin. Falls back to CFG.basin literals
    // on any failure (unauthenticated read, older server without the field).
    function loadBasinThresholds() {
        return callTool('config', { action: 'get' })
            .then(function (result) {
                var t = (result && result.thresholds) || {};
                if (typeof t.basin_low_i_ceil === 'number') CFG.basin.low = t.basin_low_i_ceil;
                if (typeof t.basin_high_i_min === 'number') CFG.basin.high = t.basin_high_i_min;
            })
            .catch(function (err) {
                console.warn('[Phase] basin thresholds fetch failed, using fallback:', err);
            });
    }

    function init() {
        setupSVG();
        // Fetch engine basin breakpoints before drawing the static bands so
        // they reflect live config; fall through to fallback literals on error.
        loadBasinThresholds().then(function () {
            drawBasins();
            drawEquilibrium();
            drawFlowField();
            drawAxes();
            loadAndRender();
            startRefresh();
            connectWebSocket();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
