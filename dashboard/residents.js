/**
 * Unitares Dashboard — Resident Fleet Panel
 *
 * Renders a card per "always-on" agent (residents) at the top of the dashboard.
 * Residents are operator-configurable via UNITARES_RESIDENT_AGENTS (env) or
 * agent_metadata.resident=True; this module is user-agnostic and renders
 * whatever /v1/residents returns.
 *
 * Each card shows:
 *   - Status dot (healthy / silent / paused / unknown) — pulses when live
 *   - Verdict pill
 *   - Coherence sparkline (SVG, last hour) with risk shading
 *   - Current EISV vector
 *   - Silence indicator (elapsed since last check-in)
 *   - Last 3 KG writes the agent authored
 *
 * Live update: subscribes to broadcaster events. eisv_update events for the
 * agent push a new sparkline point; knowledge_write events refresh the
 * recent-writes strip; lifecycle_paused/resumed flip the status.
 */

(function () {
    'use strict';

    var REFRESH_INTERVAL_MS = 60 * 1000; // periodic re-fetch fallback (covers broadcaster restarts)

    // residents indexed by agent_id (filled by /v1/residents response).
    // Agents without an agent_id (haven't checked in yet) are keyed by label.
    var residentsByAgentId = {};
    var residentsByLabel = {};
    var orderedLabels = [];
    var sourceLabel = '';

    // ---------------------------------------------------------------------
    // Pure helpers
    // ---------------------------------------------------------------------

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function fmtSilence(seconds) {
        if (seconds == null) return '—';
        if (seconds < 60) return Math.round(seconds) + 's';
        if (seconds < 3600) return Math.round(seconds / 60) + 'm';
        if (seconds < 86400) return (seconds / 3600).toFixed(1) + 'h';
        return Math.round(seconds / 86400) + 'd';
    }

    function fmtCheckinSource(source) {
        if (source === 'agent_metadata') return 'agent metadata';
        if (source === 'broadcaster_eisv') return 'live EISV event';
        return source || 'unknown source';
    }

    function statusForCard(resident, nowMs) {
        // Recompute status client-side so the "silent" flip happens between
        // server fetches without waiting for a re-poll.
        if (resident.status === 'paused' || resident.status === 'archived') {
            return resident.status;
        }
        if (!resident.last_checkin_at) return 'unknown';
        try {
            var lastMs = new Date(resident.last_checkin_at).getTime();
            var elapsed = (nowMs - lastMs) / 1000;
            if (elapsed > resident.silence_threshold_seconds) return 'silent';
            return 'healthy';
        } catch (e) {
            return 'unknown';
        }
    }

    function verdictColour(v) {
        if (v === 'proceed' || v === 'approve') return 'verdict-good';
        if (v === 'guide' || v === 'caution') return 'verdict-caution';
        if (v === 'pause' || v === 'reject') return 'verdict-bad';
        return 'verdict-neutral';
    }

    // ---------------------------------------------------------------------
    // Pill rendering (compact status strip — see renderCard)
    // ---------------------------------------------------------------------

    // Server marks event-driven residents authoritatively from the registry
    // (expected_cadence_s=None ⇒ event_driven=true). The previous heuristic
    // (>=12h threshold AND no last_checkin_at) misclassified Watcher: Watcher
    // does check in per fire, so last_checkin_at is set and the heuristic
    // returned false — leaving the pill to render as silent/healthy depending
    // on time-since-fire. Trust the server flag; fall back to the old heuristic
    // only for older servers that don't return it.
    function isEventDriven(resident) {
        if (resident.event_driven === true) return true;
        if (resident.event_driven === false) return false;
        if (resident.last_checkin_at) return false;
        var th = resident.silence_threshold_seconds;
        return th != null && th >= 12 * 3600;
    }

    function renderCard(resident, nowMs) {
        var eventDriven = isEventDriven(resident);
        var status = eventDriven ? 'event-driven' : statusForCard(resident, nowMs);

        // Compute live silence (since items re-render between server polls).
        var liveSilence = resident.silence_seconds;
        if (resident.last_checkin_at) {
            try {
                liveSilence = (nowMs - new Date(resident.last_checkin_at).getTime()) / 1000;
            } catch (e) { /* keep server value */ }
        }

        // The pill is the unique value-add: name + status dot + live silence
        // + optional recent-alerts badge + warning when past threshold. EISV,
        // verdict, full write detail live in the Agents + Activity sections.
        var rightHtml;
        if (eventDriven) {
            rightHtml = '<span class="resident-pill-eventdriven" title="Event-driven — does not check in to governance on a schedule. See Activity for findings.">event-driven</span>';
        } else {
            var silenceTxt = fmtSilence(liveSilence);
            var checkinSource = fmtCheckinSource(resident.last_checkin_source);
            var overThreshold = liveSilence != null &&
                resident.silence_threshold_seconds != null &&
                liveSilence > resident.silence_threshold_seconds;
            rightHtml = '<span class="resident-pill-silence' +
                (overThreshold ? ' over-threshold' : '') +
                '" title="time since last check-in (threshold: ' +
                escapeHtml(fmtSilence(resident.silence_threshold_seconds)) +
                ', source: ' + escapeHtml(checkinSource) + ')">' +
                escapeHtml(silenceTxt) + '</span>';
        }

        // Recent-alerts count — the residents endpoint returns up to 5 recent
        // writes per agent. Surface the count so operators can see at a glance
        // whether a resident is actively writing to the KG.
        var writes = resident.recent_writes || [];
        var alertsHtml = '';
        if (writes.length > 0) {
            // Find the most severe among recent writes to pick a colour.
            var hasCritical = writes.some(function (w) { return w.severity === 'critical'; });
            var hasHigh = writes.some(function (w) { return w.severity === 'high'; });
            var sevClass = hasCritical ? 'sev-critical' : hasHigh ? 'sev-high' : 'sev-normal';
            var preview = writes.slice(0, 3).map(function (w) {
                return (w.type || 'note') + ': ' + (w.summary || '').slice(0, 60);
            }).join('\n');
            alertsHtml = '<span class="resident-pill-alerts ' + sevClass + '"' +
                ' title="' + escapeHtml('recent KG writes:\n' + preview) + '">' +
                writes.length + '</span>';
        }

        var title = resident.label + ' · ' + (eventDriven ? 'event-driven' : status) +
            (!eventDriven && liveSilence != null ? ' · silent ' + fmtSilence(liveSilence) : '') +
            (!eventDriven && resident.last_checkin_source ? ' · source ' + fmtCheckinSource(resident.last_checkin_source) : '') +
            (resident.total_updates ? ' · ' + resident.total_updates + ' check-ins' : '') +
            (writes.length ? ' · ' + writes.length + ' recent writes' : '');

        return '<span class="resident-pill status-' + status + '"' +
            ' data-agent="' + escapeHtml(resident.label) + '"' +
            ' title="' + escapeHtml(title) + '">' +
            '<span class="resident-pill-dot status-' + status + '"></span>' +
            '<span class="resident-pill-name">' + escapeHtml(resident.label) + '</span>' +
            alertsHtml +
            rightHtml +
        '</span>';
    }

    function renderAll() {
        var container = document.getElementById('residents-grid');
        if (!container) return;
        var src = document.getElementById('residents-source-label');
        if (src) {
            src.textContent = sourceLabel ? sourceLabel : '';
        }
        if (orderedLabels.length === 0) {
            container.innerHTML = '<span class="residents-strip-empty">' +
                'No residents detected — known residents auto-surface once they check in, or set ' +
                '<code>UNITARES_RESIDENT_AGENTS</code> to override' +
                '</span>';
            return;
        }
        var nowMs = Date.now();
        var pills = orderedLabels.map(function (label) {
            var r = residentsByLabel[label];
            return r ? renderCard(r, nowMs) : '';
        });
        container.innerHTML = pills.join('');
    }

    // ---------------------------------------------------------------------
    // Data fetch
    // ---------------------------------------------------------------------

    function getAuthToken() {
        try {
            return localStorage.getItem('unitares_api_token') ||
                new URLSearchParams(window.location.search).get('token');
        } catch (e) {
            return null;
        }
    }

    async function fetchResidents() {
        try {
            // Attach the bearer token if present — bare fetch() returns 401
            // when the dashboard server is configured with UNITARES_HTTP_API_TOKEN.
            // Trusted-network bypass kicks in for curl from localhost but not
            // for browser fetches because the request reaches the server with
            // a hostname/origin that doesn't match the trusted set.
            var token = getAuthToken();
            var headers = {};
            if (token) headers['Authorization'] = 'Bearer ' + token;
            var resp = await fetch('/v1/residents', {
                credentials: 'same-origin',
                headers: headers,
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            if (!data || data.success === false) throw new Error(data && data.error || 'unknown');
            sourceLabel = data.source || '';
            orderedLabels = (data.configured || []).slice();
            residentsByAgentId = {};
            residentsByLabel = {};
            for (var i = 0; i < (data.residents || []).length; i++) {
                var r = data.residents[i];
                residentsByLabel[r.label] = r;
                if (r.agent_id) residentsByAgentId[r.agent_id] = r;
            }
            renderAll();
        } catch (e) {
            console.warn('[Residents] fetch failed:', e);
            var grid = document.getElementById('residents-grid');
            if (grid) grid.innerHTML = '<div class="residents-empty">Residents unavailable: ' + escapeHtml(String(e.message || e)) + '</div>';
        }
    }

    // ---------------------------------------------------------------------
    // Live WS update — feed eisv_update / knowledge_write to the right card
    // ---------------------------------------------------------------------

    function onEISVUpdate(data) {
        if (!data || data.type !== 'eisv_update') return;
        var aid = data.agent_id;
        if (!aid) return;
        var resident = residentsByAgentId[aid];
        if (!resident) {
            // Maybe a resident's first check-in since the last fetch — refetch.
            fetchResidents();
            return;
        }
        // Only thing the strip cares about on a check-in: reset silence and
        // bump status back to healthy. EISV / verdict / writes are shown in
        // the Agents + Activity panels below — don't duplicate them here.
        resident.last_checkin_at = data.timestamp || new Date().toISOString();
        resident.total_updates = (resident.total_updates || 0) + 1;
        renderAll();
    }

    function onGovernanceEvent(data) {
        if (!data || !data.type) return;
        if (data.type === 'eisv_update') return; // handled separately
        var aid = data.agent_id;
        if (!aid) return;
        var resident = residentsByAgentId[aid];
        if (!resident) return;

        if (data.type === 'knowledge_write') {
            var write = {
                id: data.discovery_id,
                type: data.discovery_type || 'note',
                severity: data.severity || 'low',
                summary: data.summary || '',
                tags: data.tags || [],
                timestamp: data.timestamp,
            };
            resident.recent_writes = [write].concat(resident.recent_writes || []).slice(0, 5);
            renderAll();
        } else if (data.type === 'lifecycle_paused' || data.type === 'lifecycle_archived') {
            resident.status = data.type.replace('lifecycle_', '');
            renderAll();
        } else if (data.type === 'lifecycle_resumed') {
            resident.status = 'healthy';
            renderAll();
        }
    }

    // ---------------------------------------------------------------------
    // Tick: re-render once per second so silence indicators stay live.
    // ---------------------------------------------------------------------
    function startSilenceTicker() {
        setInterval(function () {
            if (orderedLabels.length > 0) renderAll();
        }, 1000);
    }

    // ---------------------------------------------------------------------
    // Click-to-scroll: pill → resident's own panel/widget.
    //
    // Vigil/Sentinel/Watcher/Chronicler have dedicated section panels.
    // Other residents (Steward, Lumen, …) surface as rows in the Resident
    // Progress panel. Falls back to the agent card in the Agents section
    // when no resident-specific surface is found.
    // ---------------------------------------------------------------------
    var DEDICATED_PANELS = {
        vigil: 'vigil-section',
        sentinel: 'sentinel-section',
        watcher: 'watcher-section',
        chronicler: 'fleet-metrics-section',
    };

    function bindPillClicks() {
        var grid = document.getElementById('residents-grid');
        if (!grid) return;
        grid.addEventListener('click', function (e) {
            var pill = e.target.closest('.resident-pill');
            if (!pill) return;
            var label = pill.getAttribute('data-agent');
            if (!label) return;
            scrollToResidentSurface(label);
        });
    }

    function scrollToResidentSurface(label) {
        var target = findDedicatedPanel(label) ||
            findResidentProgressRow(label) ||
            findAgentCard(label);
        if (!target) return;
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.classList.add('resident-highlight');
        setTimeout(function () {
            target.classList.remove('resident-highlight');
        }, 1500);
    }

    function findDedicatedPanel(label) {
        var id = DEDICATED_PANELS[(label || '').toLowerCase()];
        if (!id) return null;
        return document.getElementById(id);
    }

    function findResidentProgressRow(label) {
        if (!label) return null;
        var safe = label.replace(/"/g, '\\"');
        var row = document.querySelector('.rp-row[data-label="' + safe + '"]');
        if (row) return row;
        // Fall back to the panel itself if the row hasn't rendered yet.
        return document.getElementById('resident-progress-panel');
    }

    function findAgentCard(label) {
        var container = document.getElementById('agents-container');
        if (!container) return null;
        var items = container.querySelectorAll('.agent-item');
        var needle = label.toLowerCase();
        for (var i = 0; i < items.length; i++) {
            var nameEl = items[i].querySelector('.agent-name');
            if (nameEl && nameEl.textContent.toLowerCase().indexOf(needle) !== -1) {
                return items[i];
            }
        }
        return null;
    }

    // ---------------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------------

    function init() {
        fetchResidents();
        setInterval(fetchResidents, REFRESH_INTERVAL_MS);
        startSilenceTicker();
        bindPillClicks();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.ResidentsModule = {
        fetchResidents: fetchResidents,
        onEISVUpdate: onEISVUpdate,
        onGovernanceEvent: onGovernanceEvent,
    };
})();
