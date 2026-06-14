/**
 * Unitares Dashboard — Activity Timeline & Skeletons
 *
 * Global activity feed: check-ins, verdicts, discoveries, dialectic events.
 * Also handles skeleton loaders and WS status label.
 */
(function () {
    'use strict';

    if (typeof DashboardState === 'undefined') {
        console.warn('[TimelineModule] state.js not loaded, module disabled');
        return;
    }

    var escapeHtml = typeof DataProcessor !== 'undefined' ? DataProcessor.escapeHtml : function (s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); };
    var formatRelativeTime = typeof DataProcessor !== 'undefined' ? DataProcessor.formatRelativeTime : function () { return ''; };

    var MAX_TIMELINE_ITEMS = 100;
    var timelineEntries = []; // {ts, type, agent, message, verdict, className, violationClass, attentionLevel}
    var currentFilter = 'important';

    // Violation taxonomy reverse-lookup index — populated from /v1/taxonomy.
    // Maps surface id (Watcher pattern, Sentinel finding type, broadcast event
    // type) → class id (CON / INT / ENT / REC / BEH / VOI). Empty until the
    // first fetch resolves; entries without a mapping just don't show a badge.
    var taxonomyReverse = {
        watcher_patterns: {},
        sentinel_findings: {},
        broadcast_events: {}
    };
    var taxonomyClassMeta = {}; // id -> { id, name, description }
    var taxonomyLoaded = false;

    function classFor(kind, surfaceId) {
        if (!surfaceId) return null;
        var bucket = taxonomyReverse[kind];
        return (bucket && bucket[surfaceId]) || null;
    }

    function classBadgeHtml(classId) {
        if (!classId) return '';
        var meta = taxonomyClassMeta[classId];
        var title = meta ? (meta.name + ' — ' + (meta.description || '').replace(/\s+/g, ' ').trim()) : classId;
        return '<span class="tl-class tl-class-' + classId + '" title="' + escapeHtml(title) + '">' + escapeHtml(classId) + '</span>';
    }

    function getAuthToken() {
        try {
            return localStorage.getItem('unitares_api_token') ||
                new URLSearchParams(window.location.search).get('token');
        } catch (e) { return null; }
    }

    async function loadTaxonomy() {
        try {
            var token = getAuthToken();
            var headers = {};
            if (token) headers['Authorization'] = 'Bearer ' + token;
            var resp = await fetch('/v1/taxonomy', {
                credentials: 'same-origin',
                headers: headers
            });
            if (!resp.ok) return;
            var data = await resp.json();
            if (!data || data.success === false) return;
            taxonomyReverse = data.reverse || taxonomyReverse;
            (data.classes || []).forEach(function (c) {
                taxonomyClassMeta[c.id] = c;
            });
            taxonomyLoaded = true;
        } catch (e) {
            // non-fatal — class badges just won't render until next attempt
        }
    }
    loadTaxonomy();

    // ========================================================================
    // Skeleton loader initialization
    // ========================================================================

    function initSkeletons() {
        if (typeof LoadingSkeleton === 'undefined') return;
        var targets = {
            'agents-skeleton': { type: 'listItem', count: 3 },
            'discoveries-skeleton': { type: 'card', count: 3 },
            'dialectic-skeleton': { type: 'card', count: 2 }
        };
        var ids = Object.keys(targets);
        for (var i = 0; i < ids.length; i++) {
            var el = document.getElementById(ids[i]);
            if (el) {
                el.innerHTML = LoadingSkeleton.create(targets[ids[i]].type, targets[ids[i]].count);
            }
        }
    }

    initSkeletons();

    // ========================================================================
    // WebSocket status label
    // ========================================================================

    function updateWSStatusLabel(status) {
        var dot = document.querySelector('#ws-status .ws-dot');
        var label = document.querySelector('#ws-status .ws-label');
        var container = document.getElementById('ws-status');
        if (!dot || !label || !container) return;

        dot.className = 'ws-dot ' + status;
        var labels = { connected: 'Live', polling: 'Polling (~30s)', reconnecting: 'Reconnecting', disconnected: 'Offline', poll_error: 'Stale Data' };
        label.textContent = labels[status] || 'Offline';
        var titles = { connected: 'Connected via WebSocket', polling: 'Polling every ~30 seconds (WebSocket unavailable)', reconnecting: 'Reconnecting...', disconnected: 'Offline', poll_error: 'Polling failed — data may be stale' };
        container.title = titles[status] || 'Offline';
    }

    // ========================================================================
    // Activity timeline
    // ========================================================================

    var VERDICT_CLASSES = {
        approve: 'tl-good', proceed: 'tl-good',
        caution: 'tl-caution', guide: 'tl-caution',
        pause: 'tl-bad', reject: 'tl-bad'
    };

    var ROUTINE_EVENT_TYPES = {
        knowledge_read: true,
        lifecycle_created: true,
        lifecycle_resumed: true,
        circuit_breaker_reset: true
    };

    function normalizeSeverity(severity) {
        return String(severity || '').toLowerCase();
    }

    function verdictForSeverity(severity) {
        var s = normalizeSeverity(severity);
        if (s === 'critical') return 'pause';
        if (s === 'high' || s === 'medium' || s === 'moderate' || s === 'warning') return 'caution';
        return null;
    }

    function attentionLevelForSeverity(severity) {
        return verdictForSeverity(severity) ? 'attention' : 'noise';
    }

    function isGreenVerdict(verdict) {
        return verdict === 'proceed' || verdict === 'approve';
    }

    function entryKey(e) {
        return e.type + '|' + (+e.ts) + '|' + (e.agent || '');
    }

    var seenKeys = {};

    function addTimelineEntry(entry) {
        // entry: {ts: Date, type: string, agent: string, message: string, verdict?: string}
        entry.ts = entry.ts || new Date();
        entry.className = entry.verdict ? (VERDICT_CLASSES[entry.verdict] || '') : '';

        // Deduplicate seeded entries (discoveries/dialectic re-seed every refresh)
        var key = entryKey(entry);
        if (seenKeys[key]) return;
        seenKeys[key] = true;

        timelineEntries.unshift(entry);
        if (timelineEntries.length > MAX_TIMELINE_ITEMS) {
            timelineEntries.length = MAX_TIMELINE_ITEMS;
            // Rebuild seenKeys from surviving entries so it doesn't grow forever
            seenKeys = {};
            for (var i = 0; i < timelineEntries.length; i++) {
                seenKeys[entryKey(timelineEntries[i])] = true;
            }
        }

        renderTimeline();
    }

    // VERDICT_ICONS is defined in utils.js and exported on window

    function isImportantEntry(e) {
        if (e.attentionLevel === 'noise') return false;
        if (e.attentionLevel === 'attention') return true;
        if (e.verdict) return !isGreenVerdict(e.verdict);
        return e.type !== 'checkin';
    }

    function renderTimeline() {
        var container = document.getElementById('timeline-container');
        if (!container) return;

        var filtered;
        if (currentFilter === 'all') {
            filtered = timelineEntries;
        } else if (currentFilter === 'important') {
            filtered = timelineEntries.filter(isImportantEntry);
        } else if (currentFilter.indexOf('class:') === 0) {
            var wantedClass = currentFilter.slice('class:'.length);
            filtered = timelineEntries.filter(function (e) { return e.violationClass === wantedClass; });
        } else {
            filtered = timelineEntries.filter(function (e) { return e.type === currentFilter; });
        }

        // Sort by timestamp descending — entries arrive from multiple sources
        // (WS, discovery seeding, dialectic seeding) in arbitrary order.
        filtered.sort(function (a, b) { return b.ts - a.ts; });

        if (filtered.length === 0) {
            container.innerHTML = '<div class="timeline-empty">No events' + (currentFilter !== 'all' ? ' matching filter' : '') + '</div>';
            return;
        }

        var html = filtered.slice(0, 50).map(function (e) {
            var timeStr = DataProcessor.formatTimestamp(e.ts);
            var relative = formatRelativeTime(e.ts.getTime());
            var relStr = relative ? ' (' + relative + ')' : '';
            var typeIcon = {
                checkin: '\u25CF',        // ●
                verdict: '\u25A0',        // ■
                discovery: '\u2605',      // ★
                dialectic: '\u25B6',      // ▶
                lifecycle: '\u2691',      // ⚑  — agent status changes
                identity: '\u25C6',       // ◆  — identity/continuity events
                knowledge: '\u270E',      // ✎  — KG writes, confidence clamps
                circuit_breaker: '\u26A1',// ⚡ — circuit-breaker trip/reset
                event: '\u25CB'           // ○ — unknown fallback
            }[e.type] || '\u25CB';
            var agentStr = e.agent ? '<span class="tl-agent">' + escapeHtml(e.agent) + '</span>' : '';
            var verdictIcon = e.verdict && VERDICT_ICONS[e.verdict] ? VERDICT_ICONS[e.verdict] + ' ' : '';
            var verdictBadge = e.verdict ? '<span class="tl-verdict ' + (VERDICT_CLASSES[e.verdict] || '') + '">' + verdictIcon + escapeHtml(e.verdict) + '</span>' : '';

            var classBadge = classBadgeHtml(e.violationClass);
            return '<div class="tl-entry ' + (e.className || '') + '" data-type="' + (e.type || '') + '" data-class="' + (e.violationClass || '') + '" data-attention="' + (e.attentionLevel || '') + '">' +
                '<span class="tl-icon">' + typeIcon + '</span>' +
                '<span class="tl-time" title="' + escapeHtml(timeStr + relStr) + '">' + timeStr + '</span>' +
                agentStr + verdictBadge + classBadge +
                '<span class="tl-message">' + escapeHtml(e.message || '') + '</span>' +
            '</div>';
        }).join('');

        container.innerHTML = html;
    }

    // Called from WebSocket handler for each EISV update
    function onEISVUpdate(data) {
        if (!data || data.type !== 'eisv_update') return;

        var agentLabel = data.agent_label || data.agent_name || (data.agent_id ? data.agent_id.substring(0, 12) : 'unknown');
        var verdict = data.verdict;
        var risk = data.risk_score != null ? (data.risk_score * 100).toFixed(0) + '%' : null;
        var coherence = data.coherence != null ? data.coherence.toFixed(3) : null;

        // Check-in entry
        var parts = [];
        if (risk) parts.push('risk ' + risk);
        if (coherence) parts.push('C ' + coherence);
        var metricsStr = parts.length ? ' (' + parts.join(', ') + ')' : '';

        addTimelineEntry({
            ts: data.timestamp ? new Date(data.timestamp) : new Date(),
            type: 'checkin',
            agent: agentLabel,
            message: 'checked in' + metricsStr,
            verdict: verdict,
            attentionLevel: verdict && !isGreenVerdict(verdict) ? 'attention' : 'noise'
        });

        // Events within the update
        if (data.events && data.events.length > 0) {
            data.events.forEach(function (event) {
                var eventVerdict = verdictForSeverity(event.severity);
                addTimelineEntry({
                    ts: event.timestamp ? new Date(event.timestamp) : new Date(),
                    type: 'verdict',
                    agent: agentLabel,
                    message: event.message || event.type,
                    verdict: eventVerdict,
                    severity: normalizeSeverity(event.severity),
                    attentionLevel: eventVerdict ? 'attention' : 'noise'
                });
            });
        }
    }

    // Called from WebSocket handler for every non-eisv_update broadcaster event.
    // Before this was added, lifecycle_*, identity_*, knowledge_*, and
    // circuit_breaker_* events arrived over the wire and were silently dropped
    // by the WS handler. This function classifies them into a timeline entry
    // type + human-readable message + optional verdict so they render in the
    // activity feed alongside check-ins.
    function onGovernanceEvent(data) {
        if (!data || !data.type) return;
        if (data.type === 'eisv_update') return; // handled by onEISVUpdate
        if (data.type === 'cross_device_call') return; // internal plumbing, not a governance event

        var agent = data.agent_label || data.agent_name ||
            (data.agent_id ? String(data.agent_id).substring(0, 12) : 'system');
        var ts = data.timestamp ? new Date(data.timestamp) : new Date();
        var t = data.type;

        var category = 'event';
        var message = t.replace(/_/g, ' ');
        var verdict = null;
        var severity = normalizeSeverity(data.severity);
        var attentionLevel = ROUTINE_EVENT_TYPES[t] ? 'noise' : null;

        var violationClass = classFor('broadcast_events', t);

        if (t.indexOf('lifecycle_') === 0) {
            category = 'lifecycle';
            var phase = t.slice('lifecycle_'.length);
            message = phase.replace(/_/g, ' ');
            if (phase === 'paused' || phase === 'stuck_detected' ||
                phase === 'silent_critical') {
                verdict = 'pause';
            } else if (phase === 'loop_detected') {
                verdict = 'caution';
            } else if (phase === 'resumed') {
                verdict = 'proceed';
            }
            if (verdict && !isGreenVerdict(verdict)) attentionLevel = 'attention';
            if (data.reason) message += ' — ' + data.reason;
        } else if (t.indexOf('identity_') === 0) {
            category = 'identity';
            message = t.slice('identity_'.length).replace(/_/g, ' ');
            if (t === 'identity_drift') verdict = 'caution';
            if (verdict || verdictForSeverity(severity)) attentionLevel = 'attention';
            if (data.detail) message += ' — ' + data.detail;
        } else if (t.indexOf('knowledge_') === 0) {
            category = 'knowledge';
            if (t === 'knowledge_write') {
                var summary = data.summary || '';
                if (summary.length > 80) summary = summary.substring(0, 77) + '...';
                message = 'wrote ' + (data.discovery_type || 'discovery') +
                    (summary ? ': ' + summary : '');
                if (data.tags && data.tags.length) {
                    message += ' [' + data.tags.slice(0, 3).join(', ') + ']';
                }
                verdict = verdictForSeverity(severity);
                attentionLevel = attentionLevelForSeverity(severity);
            } else if (t === 'knowledge_confidence_clamped') {
                message = 'confidence clamped' +
                    (data.summary ? ': ' + data.summary : '');
                verdict = 'caution';
                attentionLevel = 'attention';
            }
        } else if (t.indexOf('circuit_breaker_') === 0) {
            category = 'circuit_breaker';
            var action = t === 'circuit_breaker_trip' ? 'tripped' : 'reset';
            message = 'breaker ' + action +
                (data.reason ? ' — ' + data.reason : '');
            verdict = action === 'tripped' ? 'pause' : 'proceed';
            if (action === 'tripped') attentionLevel = 'attention';
        } else if (severity) {
            verdict = verdictForSeverity(severity);
            attentionLevel = attentionLevelForSeverity(severity);
        }

        // For knowledge_write events, prefer the explicit violation_class on
        // the payload (Watcher emits it now per agents/common/taxonomy.py).
        // Falls back to event-type lookup for other classes.
        if (t === 'knowledge_write' && data.violation_class) {
            violationClass = data.violation_class;
        }

        addTimelineEntry({
            ts: ts,
            type: category,
            agent: agent,
            message: message,
            verdict: verdict,
            severity: severity,
            sourceType: t,
            attentionLevel: attentionLevel,
            violationClass: violationClass
        });
    }

    // Discovery and dialectic seeding removed — those sections have their own
    // panels. Live events (knowledge_write, etc.) still arrive via WebSocket
    // through onGovernanceEvent.

    function clearTimeline() {
        timelineEntries.length = 0;
        seenKeys = {};
        renderTimeline();
    }

    // ========================================================================
    // Event listeners
    // Deferred — timeline.js loads in <head> before body elements exist.
    // ========================================================================

    function _bindTimelineEvents() {
        var filterSelect = document.getElementById('timeline-filter');
        if (filterSelect) {
            filterSelect.addEventListener('change', function () {
                currentFilter = this.value;
                renderTimeline();
            });
        }

        var clearBtn = document.getElementById('timeline-clear');
        if (clearBtn) {
            clearBtn.addEventListener('click', clearTimeline);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _bindTimelineEvents);
    } else {
        _bindTimelineEvents();
    }

    // ========================================================================
    // Public API
    // ========================================================================

    window.TimelineModule = {
        initSkeletons: initSkeletons,
        updateWSStatusLabel: updateWSStatusLabel,
        addTimelineEntry: addTimelineEntry,
        onEISVUpdate: onEISVUpdate,
        onGovernanceEvent: onGovernanceEvent,
        clearTimeline: clearTimeline,
        renderTimeline: renderTimeline
    };
})();
