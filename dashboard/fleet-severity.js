/**
 * Unitares Dashboard — Fleet Severity Rollup
 *
 * Aggregates every "something is wrong" signal in the dashboard into a single
 * worst-case fleet severity for the Quick Status hero, plus a list of exception
 * "reasons" for the "Needs attention" band. See
 * docs/proposals/dashboard-hero-severity-rollup.md.
 *
 * Policy (operator-chosen, 2026-06-19): CRITICAL → red, everything else →
 * amber ("caution"). All exceptions appear in the band regardless of level.
 *
 * computeFleetSeverity() is a PURE function (no DOM, no globals) so the severity
 * math is unit-tested; the gather/render wiring lives in dashboard.js.
 */
(function () {
    'use strict';

    // Anchors map a reason to the panel that explains it (for band deep-links).
    var ANCHORS = {
        agents: '#agents-section',
        systemHealth: '#system-health-section',
        watcher: '#watcher-section',
        sentinel: '#sentinel-section',
        residents: '#residents-section',
    };

    function num(value) {
        var n = Number(value);
        return isFinite(n) && n > 0 ? n : 0;
    }

    /**
     * @param {object} inputs
     * @param {number} [inputs.criticalAgents]  count of agents at health_status 'critical'
     * @param {number} [inputs.stuckCount]      count of stuck agents
     * @param {number|null} [inputs.avgCoherence] fleet average coherence (0..1) or null
     * @param {string|null} [inputs.systemHealth] overall system-health status string
     * @param {object|null} [inputs.watcher]    { critical, high } or { error:true }
     * @param {object|null} [inputs.sentinel]   { critical, high } or { error:true }
     * @param {Array|number} [inputs.silentResidents] silent resident labels or a count
     * @returns {{level:'healthy'|'caution'|'critical', reasons:Array, text:string}}
     */
    function computeFleetSeverity(inputs) {
        inputs = inputs || {};
        var critical = [];
        var caution = [];

        function add(bucket, label, anchorKey) {
            bucket.push({
                label: label,
                anchor: ANCHORS[anchorKey] || null,
                severity: bucket === critical ? 'critical' : 'caution',
            });
        }

        // ── Critical contributors (escalate the hero to red) ──
        var sysHealth = inputs.systemHealth;
        if (sysHealth === 'critical' || sysHealth === 'unavailable' || sysHealth === 'error') {
            add(critical, 'System health: ' + sysHealth, 'systemHealth');
        }
        var critAgents = num(inputs.criticalAgents);
        if (critAgents > 0) {
            add(critical, critAgents + ' agent' + (critAgents === 1 ? '' : 's') + ' critical', 'agents');
        }
        if (inputs.watcher && num(inputs.watcher.critical) > 0) {
            add(critical, 'Watcher: ' + num(inputs.watcher.critical) + ' critical', 'watcher');
        }
        if (inputs.sentinel && num(inputs.sentinel.critical) > 0) {
            add(critical, 'Sentinel: ' + num(inputs.sentinel.critical) + ' critical', 'sentinel');
        }

        // ── Caution contributors (amber; or band-only when hero is already red) ──
        var stuck = num(inputs.stuckCount);
        if (stuck > 0) {
            add(caution, stuck + ' stuck', 'agents');
        }
        if (inputs.sentinel && num(inputs.sentinel.high) > 0) {
            add(caution, 'Sentinel: ' + num(inputs.sentinel.high) + ' high', 'sentinel');
        }
        var silent = Array.isArray(inputs.silentResidents)
            ? inputs.silentResidents.length
            : num(inputs.silentResidents);
        if (silent > 0) {
            add(caution, silent + ' resident' + (silent === 1 ? '' : 's') + ' silent', 'residents');
        }
        // A monitoring feed that failed to load is concerning but not fleet-critical
        // — surface it as caution so a dead endpoint can't masquerade as "0 findings".
        if (inputs.watcher && inputs.watcher.error) {
            add(caution, 'Watcher feed unavailable', 'watcher');
        }
        if (inputs.sentinel && inputs.sentinel.error) {
            add(caution, 'Sentinel feed unavailable', 'sentinel');
        }
        if (inputs.avgCoherence !== null && inputs.avgCoherence !== undefined && inputs.avgCoherence < 0.4) {
            add(caution, 'Coherence ' + Math.round(inputs.avgCoherence * 100) + '%', 'agents');
        }

        var reasons = critical.concat(caution);
        var level = critical.length > 0 ? 'critical' : caution.length > 0 ? 'caution' : 'healthy';

        var text;
        if (level === 'healthy') {
            text = 'All systems healthy';
        } else {
            text = reasons[0].label + (reasons.length > 1 ? ' (+' + (reasons.length - 1) + ' more)' : '');
        }

        return { level: level, reasons: reasons, text: text };
    }

    if (typeof window !== 'undefined') {
        window.computeFleetSeverity = computeFleetSeverity;
    }
})();
