// system-health.js — System Health panel.
//
// Consumes:
//   GET /health/deep — cached snapshot from deep_health_probe_task
//                      (src/services/runtime_queries.py::get_health_check_data)
//
// Surfaces the per-component check matrix that operators previously had to
// curl by hand: primary_db / audit_db / redis_cache / knowledge_graph /
// calibration / agent_metadata / data_directory / pi_connectivity /
// telemetry / identity_continuity / lease_plane (added 2026-05-08, Wave 2
// Phase C.5 #418). Each row shows status badge + a short detail line.
//
// No charts in this panel — Chart.js setup is intentionally NOT applied.
// Just authFetch + DOM rendering.
//
// Auth + fetch go through `authFetch` from utils.js.

(function () {
    'use strict';

    async function fetchDeepHealth() {
        try {
            var resp = await authFetch('/health/deep');
            if (resp.status === 503) {
                // Probe task hasn't populated the cache yet — surface that
                // explicitly rather than treating as a fetch error.
                return { _not_yet_populated: true };
            }
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return await resp.json();
        } catch (e) {
            console.warn('[SystemHealth] deep fetch failed:', e);
            return null;
        }
    }

    function setMetric(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function statusBadgeClass(status) {
        // Maps the runtime_queries status vocabulary to themed classes.
        // Matches the four status colors used in sentinel-stat-sev-* (kept
        // visually distinct from the severity scale).
        switch (status) {
            case 'healthy':     return 'system-health-status-healthy';
            case 'warning':     return 'system-health-status-warning';
            case 'unavailable': return 'system-health-status-unavailable';
            case 'deprecated':  return 'system-health-status-warning';
            case 'error':       return 'system-health-status-error';
            default:            return 'system-health-status-unknown';
        }
    }

    function shortDetailFor(name, check) {
        // Per-check detail line: human-readable, max ~80 chars. Pulls the
        // most informative single field per check rather than dumping JSON.
        if (!check || typeof check !== 'object') return '';
        if (check.error) return String(check.error);
        if (check.reason) return String(check.reason);
        switch (name) {
            case 'primary_db':
                if (check.info && check.info.pool_size != null) {
                    return 'pool=' + check.info.pool_size + ' ' +
                           (check.configured_backend || '');
                }
                return check.configured_backend || '';
            case 'redis_cache':
                if (check.present === false) return 'not configured';
                if (check.stats && check.stats.keyspace_hit_rate_percent != null) {
                    return 'hit ' + check.stats.keyspace_hit_rate_percent + '%';
                }
                return '';
            case 'lease_plane':
                return check.url || '';
            case 'calibration':
                if (check.pending_updates != null) {
                    return 'pending=' + check.pending_updates;
                }
                return '';
            case 'identity_continuity':
                return check.mode || '';
            case 'pi_connectivity':
                if (check.reachable === false) return 'unreachable';
                return '';
            case 'knowledge_graph':
                if (check.warning) return String(check.warning);
                return '';
            case 'telemetry':
                if (check.audit_log_exists === false) return 'audit log missing';
                return '';
            case 'data_directory':
                if (check.exists === false) return 'data dir missing';
                return '';
            default:
                return '';
        }
    }

    function renderOverall(snapshot) {
        var status = snapshot.status || 'unknown';
        var breakdown = snapshot.status_breakdown || {};
        var overallEl = document.getElementById('system-health-overall');
        if (overallEl) {
            overallEl.textContent = status;
            // Reset class list to avoid pile-up on repeated refreshes.
            overallEl.className = 'system-health-overall ' + statusBadgeClass(status);
        }
        setMetric('system-health-count-healthy', breakdown.healthy || 0);
        setMetric('system-health-count-warning', breakdown.warning || 0);
        setMetric('system-health-count-unavailable', breakdown.unavailable || 0);
        setMetric('system-health-count-error', breakdown.error || 0);
    }

    function renderChecks(snapshot) {
        var grid = document.getElementById('system-health-grid');
        if (!grid) return;
        grid.innerHTML = '';
        var checks = snapshot.checks || {};
        var names = Object.keys(checks).sort();
        if (names.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'system-health-empty';
            empty.textContent = 'No checks present in snapshot.';
            grid.appendChild(empty);
            return;
        }
        names.forEach(function (name) {
            var check = checks[name];
            var status = (check && check.status) || 'unknown';
            var row = document.createElement('div');
            row.className = 'system-health-row ' + statusBadgeClass(status);
            row.innerHTML =
                '<span class="system-health-row-name"></span>' +
                '<span class="system-health-row-status"></span>' +
                '<span class="system-health-row-detail"></span>';
            row.children[0].textContent = name;
            row.children[1].textContent = status;
            row.children[2].textContent = shortDetailFor(name, check);
            grid.appendChild(row);
        });
    }

    function renderMeta(snapshot) {
        var meta = document.getElementById('system-health-meta');
        if (!meta) return;
        var cache = snapshot._cache || {};
        var ageSec = cache.age_seconds;
        var stale = cache.stale;
        if (ageSec == null) {
            meta.textContent = 'snapshot age unknown';
            return;
        }
        var label = 'snapshot ' + ageSec.toFixed(0) + 's old';
        if (stale) label += ' · STALE';
        meta.textContent = label;
        meta.classList.toggle('system-health-meta-stale', !!stale);
    }

    function renderUnavailableState() {
        var grid = document.getElementById('system-health-grid');
        if (grid) {
            grid.innerHTML = '';
            var msg = document.createElement('div');
            msg.className = 'system-health-empty';
            msg.textContent = 'Deep-health probe has not run yet — retry in a few seconds.';
            grid.appendChild(msg);
        }
        var overall = document.getElementById('system-health-overall');
        if (overall) {
            overall.textContent = 'pending';
            overall.className = 'system-health-overall system-health-status-unknown';
        }
    }

    function renderErrorState() {
        var grid = document.getElementById('system-health-grid');
        if (grid) {
            grid.innerHTML = '';
            var msg = document.createElement('div');
            msg.className = 'system-health-empty';
            msg.textContent = 'Failed to fetch /health/deep — see browser console.';
            grid.appendChild(msg);
        }
    }

    async function refresh() {
        var snapshot = await fetchDeepHealth();
        if (!snapshot) {
            renderErrorState();
            return;
        }
        if (snapshot._not_yet_populated) {
            renderUnavailableState();
            return;
        }
        renderOverall(snapshot);
        renderChecks(snapshot);
        renderMeta(snapshot);
    }

    function wire() {
        var btn = document.getElementById('system-health-refresh');
        if (btn) btn.addEventListener('click', refresh);
        refresh();
        // Light auto-refresh — match the deep-health probe cadence (30s).
        setInterval(refresh, 30000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }

    window.SystemHealthPanel = { refresh: refresh };
})();
