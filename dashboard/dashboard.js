/**
 * Unitares Governance Dashboard
 *
 * Main application logic. Depends on:
 * - utils.js (DashboardAPI, DataProcessor)
 * - components.js (ThemeManager)
 * - Chart.js (visualizations)
 */

// ============================================================================
// CONFIGURATION & STATE
// ============================================================================

/**
 * Dashboard configuration constants.
 * All magic numbers should be defined here.
 */
const CONFIG = {
    // Timing (ms)
    REFRESH_INTERVAL_MS: 30000,
    COPY_FEEDBACK_MS: 1500,
    SCROLL_FEEDBACK_MS: 2000,
    DEBOUNCE_MS: 250,

    // Time ranges (ms)
    HOUR_MS: 60 * 60 * 1000,
    DAY_MS: 24 * 60 * 60 * 1000,
    WEEK_MS: 7 * 24 * 60 * 60 * 1000,
    MONTH_MS: 30 * 24 * 60 * 60 * 1000,

    // EISV
    EISV_WINDOW_MS: 30 * 60 * 1000,
    EISV_BUCKET_MS: 30000,
    EISV_MAX_POINTS: 60,

    // Limits
    MAX_REFRESH_FAILURES: 2,
    MAX_TIMELINE_ITEMS: 100,
    MAX_EVENTS_LOG: 20,

    // Coalescing
    COALESCE_WINDOW_MS: 30000,

    // Optional panel refresh cadence
    OPTIONAL_REFRESH_EVERY_N_CYCLES: 3,

    // Knowledge reads can be heavier and are not needed on every dashboard tick.
    KNOWLEDGE_READ_CACHE_MS: 2 * 60 * 1000
};

// Verify dependencies loaded
if (typeof DashboardAPI === 'undefined' || typeof DataProcessor === 'undefined' || typeof ThemeManager === 'undefined') {
    console.error('Dashboard utilities not loaded. Make sure utils.js and components.js are accessible.');
}

// Core instances
const api = typeof DashboardAPI !== 'undefined' ? new DashboardAPI(window.location.origin) : null;
const themeManager = typeof ThemeManager !== 'undefined' ? new ThemeManager() : null;
// State bridges — route globals through state.js for module access
// Each property getter/setter delegates to state.get()/state.set()
// so existing code like `cachedAgents = x` still works transparently.
if (typeof state !== 'undefined') {
    ['refreshFailures', 'autoRefreshPaused', 'previousStats',
        'cachedAgents', 'cachedDiscoveries', 'cachedStuckAgents',
        'cachedDialecticSessions', 'eisvChartUpper', 'eisvChartLower',
        'eisvWebSocket', 'agentEISVHistory', 'knownAgents',
        'selectedAgentView', 'lastVitalsTimestamp'
    ].forEach(function (key) {
        Object.defineProperty(window, key, {
            get: function () { return state.get(key); },
            set: function (v) { var u = {}; u[key] = v; state.set(u); },
            configurable: true
        });
    });
}

// ============================================================================
// INCIDENT HISTORY (fetches from server audit trail)
// ============================================================================
async function fetchIncidents(type, limit = 100) {
    try {
        const resp = await authFetch('/api/incidents?type=' + encodeURIComponent(type) + '&limit=' + limit);
        const data = await resp.json();
        return data.success ? (data.incidents || []) : [];
    } catch (e) { return []; }
}

// ============================================================================
// MODAL FUNCTIONS
// ============================================================================
let modalTriggerElement = null;

/**
 * Expand a panel into a modal view.
 * @param {'discoveries'|'dialectic'|'stuck-agents'} panelType - Panel to expand
 */
function expandPanel(panelType) {
    modalTriggerElement = document.activeElement;
    const modal = document.getElementById('panel-modal');
    const modalTitle = document.getElementById('modal-title');
    const modalBody = document.getElementById('modal-body');

    if (panelType === 'discoveries') {
        modalTitle.textContent = `Discoveries (${cachedDiscoveries.length})`;
        modalBody.innerHTML = renderDiscoveriesForModal(cachedDiscoveries);
    } else if (panelType === 'dialectic') {
        modalTitle.textContent = `Dialectic (${cachedDialecticSessions.length})`;
        modalBody.innerHTML = renderDialecticForModal(cachedDialecticSessions);
    } else if (panelType === 'stuck-agents') {
        modalTitle.textContent = `Stuck Agents (${cachedStuckAgents.length})`;
        modalBody.innerHTML = renderStuckAgentsForModal(cachedStuckAgents);
    }

    modal.classList.add('visible');
    document.body.style.overflow = 'hidden';

    // Focus first focusable element in modal
    const firstFocusable = modal.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if (firstFocusable) firstFocusable.focus();
}

/**
 * Close the modal and return focus to trigger element.
 */
function closeModal() {
    const modal = document.getElementById('panel-modal');
    if (!modal) return;
    modal.classList.remove('visible');
    document.body.style.overflow = '';

    // Return focus to trigger element
    if (modalTriggerElement) {
        modalTriggerElement.focus();
        modalTriggerElement = null;
    }
}

/**
 * Trap focus within modal when open.
 * @param {KeyboardEvent} e
 */
function trapFocus(e) {
    const modal = document.getElementById('panel-modal');
    if (!modal || !modal.classList.contains('visible')) return;
    if (e.key !== 'Tab') return;

    const focusableEls = modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if (focusableEls.length === 0) return;

    const firstEl = focusableEls[0];
    const lastEl = focusableEls[focusableEls.length - 1];

    if (e.shiftKey && document.activeElement === firstEl) {
        lastEl.focus();
        e.preventDefault();
    } else if (!e.shiftKey && document.activeElement === lastEl) {
        firstEl.focus();
        e.preventDefault();
    }
}

document.addEventListener('keydown', trapFocus);

// Close modal on escape or click outside
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
});
document.getElementById('panel-modal')?.addEventListener('click', (e) => {
    if (e.target.classList.contains('panel-modal-overlay')) closeModal();
});
// Close button handler
document.querySelector('.panel-modal-close')?.addEventListener('click', closeModal);

// ============================================================================
// KEYBOARD SHORTCUTS
// ============================================================================
(function initKeyboardShortcuts() {
    const SECTION_KEYS = {
        '1': 'stats-section',
        '2': 'governance-pulse-panel',
        '3': 'eisv-chart-panel',
        '4': 'agents-section',
        '5': 'discoveries-section',
        '6': 'dialectic-section'
    };

    function isInputFocused() {
        var tag = document.activeElement?.tagName;
        return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
    }

    function showShortcutsHelp() {
        var overlay = document.getElementById('shortcuts-overlay');
        if (overlay) {
            overlay.classList.toggle('visible');
            return;
        }
    }

    document.addEventListener('keydown', function (e) {
        // Don't capture when typing in inputs or when modifiers are held (except shift)
        if (e.ctrlKey || e.metaKey || e.altKey) return;

        var modal = document.getElementById('panel-modal');
        if (modal && modal.classList.contains('visible')) return;

        if (isInputFocused() && e.key !== 'Escape') return;

        switch (e.key) {
            case 'r':
                e.preventDefault();
                var refreshBtn = document.getElementById('refresh-now');
                if (refreshBtn) refreshBtn.click();
                break;
            case 't':
                e.preventDefault();
                var themeBtn = document.getElementById('theme-toggle');
                if (themeBtn) themeBtn.click();
                break;
            case 'f':
                e.preventDefault();
                var searchInput = document.getElementById('agent-search');
                if (searchInput) searchInput.focus();
                break;
            case '?':
                e.preventDefault();
                showShortcutsHelp();
                break;
            case 'Escape':
                if (isInputFocused()) document.activeElement.blur();
                var shortcutsOverlay = document.getElementById('shortcuts-overlay');
                if (shortcutsOverlay) shortcutsOverlay.classList.remove('visible');
                break;
            default:
                if (SECTION_KEYS[e.key]) {
                    e.preventDefault();
                    var nav = document.getElementById('section-nav');
                    var target = document.getElementById(SECTION_KEYS[e.key]);
                    if (target && nav) {
                        var navHeight = nav.offsetHeight + 12;
                        var targetPos = target.getBoundingClientRect().top + window.scrollY - navHeight;
                        window.scrollTo({ top: targetPos, behavior: 'smooth' });
                    }
                }
        }
    });

    // Build the shortcuts help overlay
    var overlay = document.createElement('div');
    overlay.id = 'shortcuts-overlay';
    overlay.className = 'shortcuts-overlay';
    overlay.innerHTML =
        '<div class="shortcuts-panel">' +
        '<div class="shortcuts-header"><h3>Keyboard Shortcuts</h3><button class="shortcuts-close" type="button" aria-label="Close">&times;</button></div>' +
        '<div class="shortcuts-grid">' +
        '<div class="shortcut-row"><kbd>r</kbd><span>Refresh data</span></div>' +
        '<div class="shortcut-row"><kbd>t</kbd><span>Toggle theme</span></div>' +
        '<div class="shortcut-row"><kbd>f</kbd><span>Focus agent search</span></div>' +
        '<div class="shortcut-row"><kbd>1</kbd>-<kbd>7</kbd><span>Jump to section</span></div>' +
        '<div class="shortcut-row"><kbd>Esc</kbd><span>Close / unfocus</span></div>' +
        '<div class="shortcut-row"><kbd>?</kbd><span>Toggle this help</span></div>' +
        '</div></div>';
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function (e) {
        if (e.target === overlay || e.target.classList.contains('shortcuts-close')) {
            overlay.classList.remove('visible');
        }
    });
})();

/**
 * Render discoveries list for modal view.
 * @param {Array<Object>} discoveries - Discovery objects from API
 * @returns {string} HTML string
 */
function renderDiscoveriesForModal(discoveries) {
    if (!discoveries || discoveries.length === 0) {
        return '<div class="loading">No discoveries found</div>';
    }

    return `<div class="discoveries-list">${discoveries.map(d => {
        const type = d.type || d.discovery_type || 'note';
        const summary = d.summary || d.title || 'Untitled';
        const content = d.content || d.details || '';
        const agent = d.agent_id || d.agent || 'Unknown';
        const time = d.timestamp || d.created_at || '';

        return `
            <div class="discovery-item">
                <div class="discovery-header">
                    <span class="discovery-type">${escapeHtml(type)}</span>
                    <span class="discovery-time">${escapeHtml(time)}</span>
                </div>
                <div class="discovery-summary">${escapeHtml(summary)}</div>
                ${content ? `<div class="discovery-content" style="margin-top: 8px; font-size: 0.9em; color: var(--text-secondary);">${escapeHtml(content)}</div>` : ''}
                <div class="discovery-meta" style="margin-top: 8px; font-size: 0.8em; color: var(--text-secondary);">
                    Agent: ${escapeHtml(agent.length > 20 ? agent.substring(0, 20) + '...' : agent)}
                </div>
            </div>
        `;
    }).join('')}</div>`;
}

function renderDialecticForModal(sessions) {
    if (!sessions || sessions.length === 0) {
        return '<div class="loading">No dialectic sessions found</div>';
    }

    return `<div class="dialectic-list">${sessions.map(session => {
        const phase = session.phase || session.status || 'unknown';
        const phaseColor = getPhaseColor(phase);
        const requestorId = session.paused_agent || session.requestor_id || 'Unknown';
        const reviewerId = session.reviewer || session.reviewer_id || 'None';
        const synthesizerId = session.synthesizer || '';
        const sessionType = session.session_type || session.type || 'verification';
        const topic = session.topic || session.reason || `${sessionType} session`;
        const created = session.created || session.created_at || '';
        const sessionId = session.session_id || 'unknown';

        return `
            <div class="dialectic-item ${phase}">
                <div class="dialectic-header">
                    <span class="dialectic-type" style="border-color: ${phaseColor}; color: ${phaseColor}">
                        ${escapeHtml(formatDialecticPhase(phase))}
                    </span>
                    <span class="dialectic-session-type">${escapeHtml(sessionType)}</span>
                    <span class="dialectic-session-id-copy" title="Copy session ID" data-session-id="${escapeHtml(sessionId)}">
                        <code class="code-tertiary">${escapeHtml(sessionId.substring(0, 16))}${sessionId.length > 16 ? '…' : ''}</code> 📋
                    </span>
                    <span class="dialectic-time">${escapeHtml(created)}</span>
                </div>
                <div class="dialectic-topic">${escapeHtml(topic)}</div>
                <div class="dialectic-agents dialectic-agents-three">
                    <span class="agent-pill"><span class="agent-label">Requestor</span> ${escapeHtml(requestorId.substring(0, 8))}</span>
                    ${reviewerId && reviewerId !== 'None' ? `<span class="agent-pill"><span class="agent-label">Reviewer</span> ${escapeHtml(reviewerId.substring(0, 8))}</span>` : ''}
                    ${synthesizerId ? `<span class="agent-pill agent-pill-synthesizer"><span class="agent-label">Synthesizer</span> ${escapeHtml(synthesizerId.substring(0, 8))}</span>` : ''}
                </div>
            </div>
        `;
    }).join('')}</div>`;
}

function renderStuckAgentsForModal(agents) {
    if (!agents || agents.length === 0) {
        return '<div class="stuck-agents-empty"><span class="stuck-agents-empty-icon">✓</span>No stuck agents detected</div>';
    }

    const getAgentData = (id) => cachedAgents.find(a => a.agent_id === id);

    const formatAge = (minutes) => {
        if (minutes < 60) return `${minutes.toFixed(0)}m`;
        if (minutes < 1440) return `${(minutes / 60).toFixed(1)}h`;
        return `${(minutes / 1440).toFixed(1)}d`;
    };

    const reasonConfig = {
        'activity_timeout': { label: 'Inactive', icon: '⏸', color: 'var(--text-muted)', severity: 'low' },
        'critical_margin_timeout': { label: 'Critical', icon: '⚠', color: 'var(--color-volatility)', severity: 'high' },
        'tight_margin_timeout': { label: 'Tight Margin', icon: '◐', color: 'var(--color-entropy)', severity: 'medium' }
    };

    return `<div class="stuck-agents-list">${agents.map(stuck => {
        const agentData = getAgentData(stuck.agent_id);
        const name = agentData?.name || agentData?.label || stuck.agent_id.substring(0, 10) + '...';
        const age = formatAge(stuck.age_minutes);
        const config = reasonConfig[stuck.reason] || { label: stuck.reason, icon: '?', color: 'var(--text-muted)', severity: 'low' };

        // Get additional details from cached agent data
        const metrics = agentData?.metrics || {};
        const coherence = metrics.coherence !== undefined ? (metrics.coherence * 100).toFixed(0) + '%' : '—';
        const risk = metrics.risk_score !== undefined ? (metrics.risk_score * 100).toFixed(0) + '%' : '—';
        const updates = agentData?.total_updates || '—';
        const trustTier = agentData?.trust_tier || 0;
        const tierNames = ['Unknown', 'Emerging', 'Established', 'Verified'];
        const tierName = tierNames[trustTier] || 'Unknown';
        const lastUpdate = agentData?.last_update ? formatTimestamp(agentData.last_update) : '—';
        const stuckSince = stuck.age_minutes ? formatTimestamp(Date.now() - stuck.age_minutes * 60000) : null;
        const purpose = agentData?.purpose || '—';

        return `
            <div class="stuck-agent-card stuck-agent-${config.severity}" data-agent-id="${escapeHtml(stuck.agent_id)}">
                <div class="stuck-agent-header">
                    <div class="stuck-agent-identity">
                        <span class="stuck-agent-icon" style="color: ${config.color}">${config.icon}</span>
                        <span class="stuck-agent-name">${escapeHtml(name)}</span>
                        <span class="stuck-agent-badge" style="background: ${config.color}20; color: ${config.color}; border: 1px solid ${config.color}40">${config.label}</span>
                    </div>
                    <div class="stuck-agent-actions">
                        <button class="stuck-agent-view-btn" data-agent-id="${escapeHtml(stuck.agent_id)}" title="View agent details">
                            <span>Details</span>
                        </button>
                        <button class="stuck-agent-dialectic-btn" data-agent-id="${escapeHtml(stuck.agent_id)}" title="Request dialectic review">
                            <span>Dialectic</span>
                        </button>
                        <button class="stuck-agent-resume-btn" data-agent-id="${escapeHtml(stuck.agent_id)}" title="Clear stuck status and resume this agent">
                            <span>Unstick</span>
                        </button>
                        <button class="stuck-agent-archive-btn" data-agent-id="${escapeHtml(stuck.agent_id)}" title="Archive this stuck agent">
                            <span>Archive</span>
                        </button>
                    </div>
                </div>
                <div class="stuck-agent-metrics">
                    <div class="stuck-metric">
                        <span class="stuck-metric-label">Stuck</span>
                        <span class="stuck-metric-value stuck-metric-time">${age}</span>
                    </div>
                    <div class="stuck-metric">
                        <span class="stuck-metric-label">Coherence</span>
                        <span class="stuck-metric-value">${coherence}</span>
                    </div>
                    <div class="stuck-metric">
                        <span class="stuck-metric-label">Risk</span>
                        <span class="stuck-metric-value">${risk}</span>
                    </div>
                    <div class="stuck-metric">
                        <span class="stuck-metric-label">Updates</span>
                        <span class="stuck-metric-value">${updates}</span>
                    </div>
                    <div class="stuck-metric">
                        <span class="stuck-metric-label">Trust</span>
                        <span class="stuck-metric-value">${tierName}</span>
                    </div>
                </div>
                ${purpose !== '—' ? `<div class="stuck-agent-purpose">${escapeHtml(purpose)}</div>` : ''}
                <div class="stuck-agent-details">
                    <span class="stuck-agent-detail-item" title="Details">${escapeHtml(stuck.details || '')}</span>
                </div>
                <div class="stuck-agent-footer">
                    <span class="stuck-agent-id" title="Click to copy">${escapeHtml(stuck.agent_id)}</span>
                    ${stuckSince ? `<span class="stuck-agent-last-update">Stuck since: ${stuckSince}</span>` : ''}
                    <span class="stuck-agent-last-update">Last update: ${lastUpdate}</span>
                </div>
            </div>`;
    }).join('')}</div>`;
}

// Archive stuck agent handler
async function archiveStuckAgent(agentId) {
    try {
        const result = await callTool('archive_agent', {
            agent_id: agentId,
            reason: 'Archived from dashboard - stuck agent'
        });
        if (result && result.success) {
            // Remove from cached stuck agents
            cachedStuckAgents = cachedStuckAgents.filter(a => a.agent_id !== agentId);
            // Refresh the modal
            const modalBody = document.getElementById('modal-body');
            const modalTitle = document.getElementById('modal-title');
            if (modalBody && modalTitle) {
                modalTitle.textContent = `Stuck Agents (${cachedStuckAgents.length})`;
                modalBody.innerHTML = renderStuckAgentsForModal(cachedStuckAgents);
            }
            // Refresh agents list
            loadAgents();
            loadStuckAgents();
            return true;
        }
        return false;
    } catch (error) {
        console.error('Failed to archive agent:', error);
        return false;
    }
}

// Resume/unstick stuck agent handler
async function resumeStuckAgent(agentId) {
    try {
        const agentData = cachedAgents.find(a => a.agent_id === agentId);
        const isActive = agentData && (agentData.lifecycle_status === 'active' || agentData.status === 'active');
        let result = await callTool('agent', {
            action: 'resume',
            agent_id: agentId,
            reason: isActive ? 'Unstuck from dashboard' : 'Resumed from dashboard',
            unstick: isActive ? true : undefined
        });
        if (result && result.success) {
            cachedStuckAgents = cachedStuckAgents.filter(a => a.agent_id !== agentId);
            const modalBody = document.getElementById('modal-body');
            const modalTitle = document.getElementById('modal-title');
            if (modalBody && modalTitle) {
                modalTitle.textContent = `Stuck Agents (${cachedStuckAgents.length})`;
                modalBody.innerHTML = renderStuckAgentsForModal(cachedStuckAgents);
            }
            loadAgents();
            loadStuckAgents();
            return true;
        }
        // Fallback: operator_resume_agent for hard limits
        result = await callTool('operator_resume_agent', {
            target_agent_id: agentId,
            reason: 'Operator resumed from dashboard'
        });
        if (result && result.success) {
            cachedStuckAgents = cachedStuckAgents.filter(a => a.agent_id !== agentId);
            const modalBody = document.getElementById('modal-body');
            const modalTitle = document.getElementById('modal-title');
            if (modalBody && modalTitle) {
                modalTitle.textContent = `Stuck Agents (${cachedStuckAgents.length})`;
                modalBody.innerHTML = renderStuckAgentsForModal(cachedStuckAgents);
            }
            loadAgents();
            loadStuckAgents();
            return true;
        }
        return false;
    } catch (error) {
        console.error('Failed to resume agent:', error);
        return false;
    }
}

// Event delegation for stuck agents modal
document.addEventListener('click', async (event) => {
    // Handle resume button
    const resumeBtn = event.target.closest('.stuck-agent-resume-btn');
    if (resumeBtn) {
        event.stopPropagation();
        const agentId = resumeBtn.getAttribute('data-agent-id');
        if (!agentId) return;

        resumeBtn.disabled = true;
        resumeBtn.innerHTML = '<span>...</span>';

        const success = await resumeStuckAgent(agentId);
        if (!success) {
            resumeBtn.disabled = false;
            resumeBtn.innerHTML = '<span>Failed</span>';
            setTimeout(() => {
                resumeBtn.innerHTML = '<span>Resume</span>';
            }, CONFIG.SCROLL_FEEDBACK_MS);
        }
        return;
    }

    // Handle archive button
    const archiveBtn = event.target.closest('.stuck-agent-archive-btn');
    if (archiveBtn) {
        event.stopPropagation();
        const agentId = archiveBtn.getAttribute('data-agent-id');
        if (!agentId) return;

        archiveBtn.disabled = true;
        archiveBtn.innerHTML = '<span>...</span>';

        const success = await archiveStuckAgent(agentId);
        if (!success) {
            archiveBtn.disabled = false;
            archiveBtn.innerHTML = '<span>Failed</span>';
            setTimeout(() => {
                archiveBtn.innerHTML = '<span>Archive</span>';
            }, CONFIG.SCROLL_FEEDBACK_MS);
        }
        return;
    }

    // Handle view details button
    const viewBtn = event.target.closest('.stuck-agent-view-btn');
    if (viewBtn) {
        event.stopPropagation();
        const agentId = viewBtn.getAttribute('data-agent-id');
        if (!agentId) return;

        const agent = cachedAgents.find(a => a.agent_id === agentId);
        if (agent) {
            closeModal();
            setTimeout(() => showAgentDetail(agent), 100);
        }
        return;
    }

    // Handle request dialectic button
    const dialecticBtn = event.target.closest('.stuck-agent-dialectic-btn');
    if (dialecticBtn) {
        event.stopPropagation();
        const agentId = dialecticBtn.getAttribute('data-agent-id');
        if (!agentId) return;
        dialecticBtn.disabled = true;
        dialecticBtn.innerHTML = '<span>...</span>';
        try {
            const result = await callTool('request_dialectic_review', {
                agent_id: agentId,
                reason: 'Requested from dashboard',
                reviewer_mode: 'llm'
            });
            if (result && result.success) {
                dialecticBtn.innerHTML = '<span>Created</span>';
                loadDialecticSessions();
                closeModal();
            } else {
                dialecticBtn.innerHTML = '<span>Failed</span>';
                dialecticBtn.disabled = false;
            }
        } catch (e) {
            console.error('Request dialectic failed:', e);
            dialecticBtn.innerHTML = '<span>Error</span>';
            dialecticBtn.disabled = false;
        }
        return;
    }

    // Handle ID copy
    const idEl = event.target.closest('.stuck-agent-id');
    if (idEl) {
        const id = idEl.textContent;
        try {
            await navigator.clipboard.writeText(id);
            const original = idEl.textContent;
            idEl.textContent = 'Copied!';
            setTimeout(() => { idEl.textContent = original; }, CONFIG.COPY_FEEDBACK_MS);
        } catch (e) {
            console.error('Copy failed:', e);
        }
        return;
    }

    // Discovery status update
    const discoveryStatusBtn = event.target.closest('.discovery-status-btn');
    if (discoveryStatusBtn) {
        event.stopPropagation();
        const discoveryId = discoveryStatusBtn.getAttribute('data-discovery-id');
        const status = discoveryStatusBtn.getAttribute('data-status');
        if (!discoveryId || !status) return;
        discoveryStatusBtn.disabled = true;
        discoveryStatusBtn.textContent = '...';
        try {
            const result = await callTool('update_discovery_status_graph', { discovery_id: discoveryId, status });
            if (result && result.success) {
                discoveryStatusBtn.textContent = 'Done';
                loadDiscoveries('', { force: true });
                closeModal();
            } else {
                discoveryStatusBtn.textContent = 'Failed';
                discoveryStatusBtn.disabled = false;
            }
        } catch (e) {
            console.error('Status update failed:', e);
            discoveryStatusBtn.textContent = 'Error';
            discoveryStatusBtn.disabled = false;
        }
        return;
    }

    // Handle agent detail Pin to Pulse button
    const detailPinBtn = event.target.closest('.agent-detail-pin-btn');
    if (detailPinBtn) {
        event.stopPropagation();
        const agentId = detailPinBtn.getAttribute('data-agent-id');
        const agentName = detailPinBtn.getAttribute('data-agent-name');
        if (!agentId) return;
        if (state.get('pinnedAgentId') === agentId) {
            state.set({ pinnedAgentId: null, pinnedAgentName: null });
            localStorage.removeItem('unitares_pinned_agent_id');
            localStorage.removeItem('unitares_pinned_agent_name');
            detailPinBtn.textContent = 'Pin to Pulse';
        } else {
            state.set({ pinnedAgentId: agentId, pinnedAgentName: agentName || agentId });
            localStorage.setItem('unitares_pinned_agent_id', agentId);
            localStorage.setItem('unitares_pinned_agent_name', agentName || agentId);
            detailPinBtn.textContent = 'Unpin from Pulse';
        }
        applyAgentFilters();
        return;
    }

    // Handle agent detail Resume button
    const detailResumeBtn = event.target.closest('.agent-detail-resume-btn');
    if (detailResumeBtn) {
        event.stopPropagation();
        const agentId = detailResumeBtn.getAttribute('data-agent-id');
        if (!agentId) return;
        detailResumeBtn.disabled = true;
        detailResumeBtn.textContent = 'Resuming...';
        try {
            const result = await callTool('agent', {
                action: 'resume',
                agent_id: agentId,
                reason: 'Resumed from dashboard agent detail'
            });
            if (result && result.success) {
                detailResumeBtn.textContent = 'Resumed';
                detailResumeBtn.style.borderColor = 'var(--color-success)';
                detailResumeBtn.style.color = 'var(--color-success)';
                loadAgents();
                loadStuckAgents();
            } else {
                detailResumeBtn.textContent = 'Failed';
                detailResumeBtn.disabled = false;
                setTimeout(() => { detailResumeBtn.textContent = 'Resume Agent'; }, 2000);
            }
        } catch (err) {
            console.error('Resume failed:', err);
            detailResumeBtn.textContent = 'Error';
            detailResumeBtn.disabled = false;
            setTimeout(() => { detailResumeBtn.textContent = 'Resume Agent'; }, 2000);
        }
        return;
    }

    // Handle agent detail Archive button
    const detailArchiveBtn = event.target.closest('.agent-detail-archive-btn');
    if (detailArchiveBtn) {
        event.stopPropagation();
        const agentId = detailArchiveBtn.getAttribute('data-agent-id');
        if (!agentId) return;
        detailArchiveBtn.disabled = true;
        detailArchiveBtn.textContent = 'Archiving...';
        try {
            const result = await callTool('archive_agent', {
                agent_id: agentId,
                reason: 'Archived from dashboard agent detail'
            });
            if (result && result.success) {
                detailArchiveBtn.textContent = 'Archived';
                detailArchiveBtn.style.borderColor = 'var(--color-success)';
                detailArchiveBtn.style.color = 'var(--color-success)';
                loadAgents();
                loadStuckAgents();
            } else {
                detailArchiveBtn.textContent = 'Failed';
                detailArchiveBtn.disabled = false;
                setTimeout(() => { detailArchiveBtn.textContent = 'Archive Agent'; }, 2000);
            }
        } catch (err) {
            console.error('Archive failed:', err);
            detailArchiveBtn.textContent = 'Error';
            detailArchiveBtn.disabled = false;
            setTimeout(() => { detailArchiveBtn.textContent = 'Archive Agent'; }, 2000);
        }
        return;
    }
});

// ============================================================================
// API & UTILITY WRAPPERS
// ============================================================================

async function callTool(toolName, toolArguments = {}, options = {}) {
    return api.callTool(toolName, toolArguments, options);
}

// Re-export DataProcessor utilities for convenience
const escapeHtml = DataProcessor.escapeHtml;
const highlightMatch = DataProcessor.highlightMatch;
const copyToClipboard = DataProcessor.copyToClipboard;
const formatRelativeTime = DataProcessor.formatRelativeTime;
const formatTimestamp = DataProcessor.formatTimestamp;

// ============================================================================
// UI HELPERS
// ============================================================================

/** Strip dashboard-internal keys (prefixed with _) from objects before display. */
function filterInternalKeys(obj) {
    if (!obj || typeof obj !== 'object') return obj;
    return Object.fromEntries(
        Object.entries(obj).filter(([k]) => !k.startsWith('_'))
    );
}

function showError(message) {
    const container = document.getElementById('error-container');
    container.innerHTML = `<div class="error">Error: ${escapeHtml(message)}</div>`;
}

function clearError() {
    document.getElementById('error-container').innerHTML = '';
}

function formatChange(current, previous) {
    if (previous === undefined || previous === null) return '';
    const diff = current - previous;
    if (diff === 0) return '';
    const arrow = diff > 0 ? '▲' : '▼';
    const dir = diff > 0 ? 'up' : 'down';
    const sign = diff > 0 ? '+' : '';
    return `<span class="change-arrow ${dir}">${arrow}</span><span class="change-arrow ${dir}">${sign}${diff}</span>`;
}

function updateConnectionBanner(hasError) {
    const banner = document.getElementById('connection-banner');
    if (!banner) return;
    if (hasError) {
        refreshFailures += 1;
    } else {
        // Reset on success, but only if we had failures
        if (refreshFailures > 0) {
            refreshFailures = Math.max(0, refreshFailures - 1); // Decay failures gradually
        }
    }

    // Only show banner after multiple consecutive failures
    if (refreshFailures >= CONFIG.MAX_REFRESH_FAILURES) {
        banner.textContent = `Connection issues detected (${refreshFailures} failures). Check server status or network. Click "Refresh now" to retry.`;
        banner.classList.remove('hidden');
    } else {
        banner.classList.add('hidden');
    }
}

function updateRefreshStatus() {
    const status = document.getElementById('refresh-status');
    if (!status) return;
    status.textContent = autoRefreshPaused
        ? 'Auto-refresh paused'
        : `Auto-refresh every ${Math.round(CONFIG.REFRESH_INTERVAL_MS / 1000)} seconds`;
}

// Agent utilities, rendering, filtering, detail modal, and export
// are now in agents.js → AgentsModule
var getAgentStatus = AgentsModule.getAgentStatus;
var getAgentDisplayName = AgentsModule.getAgentDisplayName;
var agentHasMetrics = AgentsModule.agentHasMetrics;
var formatStatusLabel = AgentsModule.formatStatusLabel;
var updateStatusLegend = AgentsModule.updateStatusLegend;
var updateAgentFilterInfo = AgentsModule.updateAgentFilterInfo;
var applyAgentFilters = AgentsModule.applyAgentFilters;
var clearAgentFilters = AgentsModule.clearAgentFilters;
var showAgentDetail = AgentsModule.showAgentDetail;
var exportAgents = AgentsModule.exportAgents;

// Discovery utilities, rendering, filtering, detail modal, and export
// are now in discoveries.js → DiscoveriesModule
var normalizeDiscoveryType = DiscoveriesModule.normalizeDiscoveryType;
var formatDiscoveryType = DiscoveriesModule.formatDiscoveryType;
var updateDiscoveryFilterInfo = DiscoveriesModule.updateDiscoveryFilterInfo;
var updateDiscoveryLegend = DiscoveriesModule.updateDiscoveryLegend;
var applyDiscoveryFilters = DiscoveriesModule.applyDiscoveryFilters;
var clearDiscoveryFilters = DiscoveriesModule.clearDiscoveryFilters;
var showDiscoveryDetail = DiscoveriesModule.showDiscoveryDetail;
var exportDiscoveries = DiscoveriesModule.exportDiscoveries;

// ============================================================================
// DATA LOADING
// ============================================================================

/**
 * Load agents from API and render to panel.
 * Updates cachedAgents and stats.
 * @returns {Promise<void>}
 */
async function loadAgents() {
    try {
        console.log('Loading agents...');
        // Use unified agent() tool with action='list'
        let result = await callTool('agent', {
            action: 'list',
            include_metrics: true,
            recent_days: 30,
            limit: 200,
            min_updates: 0,
            status_filter: 'all'
        });

        console.log('Agents loaded:', result ? (result.summary?.total || 'ok') : 'null');

        // Handle null/undefined result
        if (!result) {
            throw new Error('No response from server');
        }

        // Handle rate limit errors gracefully - don't count as failure
        if (result.error && result.error.includes('rate limit')) {
            console.warn('Rate limit hit, will retry on next refresh');
            // Keep existing data, don't clear cache
            return true; // Return true to not trigger connection banner
        }

        // Check for error response
        if (result.error) {
            throw new Error(result.error);
        }

        // Handle case where result might be an array (unexpected format)
        if (Array.isArray(result)) {
            console.warn('Unexpected array response, converting to expected format');
            const agentsObj = {
                active: result.filter(a => (a.lifecycle_status || a.status) === 'active'),
                waiting_input: result.filter(a => (a.lifecycle_status || a.status) === 'waiting_input'),
                paused: result.filter(a => (a.lifecycle_status || a.status) === 'paused'),
                archived: result.filter(a => (a.lifecycle_status || a.status) === 'archived'),
                deleted: result.filter(a => (a.lifecycle_status || a.status) === 'deleted'),
                unknown: result.filter(a => !['active', 'waiting_input', 'paused', 'archived', 'deleted'].includes(a.lifecycle_status || a.status))
            };
            const summary = {
                total: result.length,
                by_status: {
                    active: agentsObj.active.length,
                    waiting_input: agentsObj.waiting_input.length,
                    paused: agentsObj.paused.length,
                    archived: agentsObj.archived.length,
                    deleted: agentsObj.deleted.length,
                    unknown: agentsObj.unknown.length
                }
            };
            result = { agents: agentsObj, summary: summary };
        }

        // Parse the actual API response format
        // list_agents returns: { agents: { active: [], waiting_input: [], ... }, summary: { total: N, ... } }
        const agentsObj = result.agents || {};
        const summary = result.summary || {};
        const byStatus = summary.by_status || {};

        // Use summary counts (accurate) not array lengths (limited by pagination)
        const total = summary.total || 0;
        // "active" headline = agents that have actually checked in at least once
        // (participated). Falls back to lifecycle-active if the backend predates
        // the participation split. Never-participated are still in `total`.
        const neverParticipated = summary.never_participated || 0;
        const active = (summary.participated != null)
            ? summary.participated
            : ((byStatus.active || 0) + (byStatus.waiting_input || 0));
        const paused = byStatus.paused || 0;
        const archived = byStatus.archived || 0;
        const deleted = byStatus.deleted || 0;
        const unknown = byStatus.unknown || 0;

        updateStatusLegend({
            active: byStatus.active || 0,
            waiting_input: byStatus.waiting_input || 0,
            paused,
            archived,
            deleted,
            unknown
        });

        // Flatten agents from all status categories (for display only)
        const allAgents = [
            ...(agentsObj.active || []),
            ...(agentsObj.waiting_input || []),
            ...(agentsObj.paused || []),
            ...(agentsObj.archived || []),
            ...(agentsObj.deleted || []),
            ...(agentsObj.unknown || [])
        ];

        // Update stats with animated counters (merged Agents card: active / total)
        const totalEl = document.getElementById('total-agents');
        const activeEl = document.getElementById('active-agents');
        const agentsChangeEl = document.getElementById('agents-change');
        if (totalEl) animateValue(totalEl, total);
        if (activeEl) animateValue(activeEl, active);

        const agentsChange = formatChange(total, previousStats.totalAgents);
        const breakdown = [];
        if (active > 0) breakdown.push(`${active} active`);
        if (neverParticipated > 0) breakdown.push(`${neverParticipated} never checked in`);
        if (paused > 0) breakdown.push(`${paused} paused`);
        if (archived > 0) breakdown.push(`${archived} archived`);
        if (deleted > 0) breakdown.push(`${deleted} deleted`);
        if (unknown > 0) breakdown.push(`${unknown} unknown`);
        if (agentsChangeEl) {
            agentsChangeEl.innerHTML = agentsChange || (total > 0 ? breakdown.join(', ') || 'All active' : 'No agents yet');
        }

        previousStats.totalAgents = total;
        previousStats.activeAgents = active;

        // Fleet health stat card — computed from loaded agent data
        const fleetCoherenceEl = document.getElementById('fleet-coherence');
        const fleetDetailEl = document.getElementById('fleet-health-detail');
        if (fleetCoherenceEl && fleetDetailEl) {
            // Always exclude test agents from fleet stats to prevent data corruption
            const fleetAgents = (typeof AgentsModule !== 'undefined' && AgentsModule.getProductionAgents)
                ? AgentsModule.getProductionAgents(allAgents)
                : allAgents;
            const agentsWithMetrics = fleetAgents.filter(a => {
                const m = a.metrics || {};
                return m.coherence !== undefined && m.coherence !== null && (a.total_updates || 0) > 0;
            });
            if (agentsWithMetrics.length > 0) {
                const totalUpdates = agentsWithMetrics.reduce((sum, a) => sum + (a.total_updates || 1), 0);
                const avgCoherence = agentsWithMetrics.reduce((sum, a) => sum + Number(a.metrics.coherence) * (a.total_updates || 1), 0) / totalUpdates;
                animateValue(fleetCoherenceEl, avgCoherence * 100, { decimals: 0, suffix: '%' });
                const criticalCount = fleetAgents.filter(a => a.health_status === 'critical').length;
                const highRiskCount = fleetAgents.filter(a => {
                    const rs = a.metrics && a.metrics.risk_score;
                    return rs !== undefined && rs !== null && Number(rs) > 0.6;
                }).length;
                const parts = [];
                // Make "N critical" a clickable filter so the agent(s) behind the
                // count are always reachable — no more phantom number with no row.
                if (criticalCount > 0) {
                    const activeHealth = state.get('agentHealthFilter') === 'critical';
                    parts.push(`<span class="fleet-critical-link${activeHealth ? ' active' : ''}" role="button" tabindex="0" data-health="critical" title="Show critical agents">${criticalCount} critical</span>`);
                }
                if (highRiskCount > 0) parts.push(`${highRiskCount} high-risk`);
                // Fleet coherence change indicator
                if (previousStats.fleetCoherence !== undefined && previousStats.fleetCoherence !== null) {
                    const cohDiff = avgCoherence - previousStats.fleetCoherence;
                    if (Math.abs(cohDiff) > 0.001) {
                        const arrow = cohDiff > 0 ? '▲' : '▼';
                        const dir = cohDiff > 0 ? 'up' : 'down';
                        const sign = cohDiff > 0 ? '+' : '';
                        parts.push(`<span class="change-arrow ${dir}">${arrow} ${sign}${(cohDiff * 100).toFixed(0)}%</span>`);
                    }
                }
                previousStats.fleetCoherence = avgCoherence;

                const agentCountLabel = agentsWithMetrics.length + ' agents';
                fleetDetailEl.innerHTML = parts.length > 0 ? agentCountLabel + ' · ' + parts.join(', ') : agentCountLabel;
            } else {
                fleetCoherenceEl.textContent = '-';
                fleetDetailEl.innerHTML = 'No metrics data';
            }
        }

        // Trust tier distribution
        const trustBarsEl = document.getElementById('trust-tier-bars');
        const trustDetailEl = document.getElementById('trust-tier-detail');
        if (trustBarsEl) {
            const tierNameToNum = { unknown: 0, emerging: 1, established: 2, verified: 3 };
            const tierCounts = [0, 0, 0, 0]; // T0-T3
            allAgents.forEach(a => {
                const raw = a.trust_tier;
                const num = raw !== undefined && raw !== null
                    ? (typeof raw === 'number' ? raw : (tierNameToNum[String(raw).toLowerCase()] || 0))
                    : 0;
                if (num >= 0 && num <= 3) tierCounts[num]++;
            });
            const maxCount = Math.max(...tierCounts, 1);
            const tierLabels = ['T0', 'T1', 'T2', 'T3'];
            const tierNames = ['Unknown', 'Emerging', 'Established', 'Verified'];
            const tierColors = ['var(--text-secondary)', 'var(--accent-orange)', 'var(--green)', 'var(--accent-cyan, #06b6d4)'];
            const activeTier = state.get('agentTierFilter');
            trustBarsEl.innerHTML = tierLabels.map((label, idx) => {
                const pct = (tierCounts[idx] / maxCount) * 100;
                const activeCls = activeTier === idx ? ' trust-bar-row-active' : '';
                return `<div class="trust-bar-row${activeCls}" role="button" tabindex="0" data-tier="${idx}" title="${tierNames[idx]}: ${tierCounts[idx]} agents — click to filter">
                    <span class="trust-bar-label">${label}</span>
                    <div class="trust-bar-track"><div class="trust-bar-fill" style="width:${pct}%;background:${tierColors[idx]}"></div></div>
                    <span class="trust-bar-count">${tierCounts[idx]}</span>
                </div>`;
            }).join('');
            if (trustDetailEl) {
                const verified = tierCounts[3];
                const total = allAgents.length;
                trustDetailEl.textContent = total > 0 ? `${verified} verified of ${total}` : 'No agents';
            }
        }

        // Sort by last update (most recent first)
        allAgents.sort((a, b) => {
            const aTime = new Date(a.last_update || a.created_at || 0);
            const bTime = new Date(b.last_update || b.created_at || 0);
            return bTime - aTime;
        });

        cachedAgents = allAgents;

        // Stale pinned agent validation
        var pinnedId = state.get('pinnedAgentId');
        if (pinnedId && !allAgents.find(function (a) { return a.agent_id === pinnedId; })) {
            state.set({ pinnedAgentId: null, pinnedAgentName: null });
            localStorage.removeItem('unitares_pinned_agent_id');
            localStorage.removeItem('unitares_pinned_agent_name');
            if (typeof showToast === 'function') {
                showToast('Pinned agent no longer exists — unpinned');
            }
        }

        applyAgentFilters();
        return true;

    } catch (error) {
        console.error('Error loading agents:', error);
        const errorMsg = error.message || 'Unknown error';
        showError(`Failed to load agents: ${errorMsg}`);
        // Preserve prior cachedAgents on transient failure. Wiping the list
        // on every caught error caused agents to "drop" whenever the
        // backend tool timed out (anyio deadlock): a successful refresh
        // would populate the list, and a concurrent / subsequent failing
        // refresh's catch block would clear it. Only render the error
        // placeholder if we never had data to begin with.
        if (!cachedAgents || cachedAgents.length === 0) {
            const container = document.getElementById('agents-container');
            if (container) {
                container.innerHTML = `<div class="loading">Error loading agents: ${escapeHtml(errorMsg)}</div>`;
            }
            updateAgentFilterInfo(0);
            updateStatusLegend(null);
        }
        return false;
    }
}

// Stuck agents monitoring
// cachedStuckAgents managed by state.js bridge

async function loadStuckAgents() {
    try {
        const result = await callTool('detect_stuck_agents', {});
        const countEl = document.getElementById('stuck-agents-count');
        const detailEl = document.getElementById('stuck-agents-detail');
        const cardEl = document.getElementById('stuck-agents-card');

        if (!countEl || !detailEl || !cardEl) return;

        if (result && result.success) {
            // Soft signals (e.g. cadence_silence) are surfaced via the audit
            // trail, NOT as alarming "Stuck" badges/counts — they deliberately
            // include benign finished-and-idle agents the rule can't distinguish
            // from a genuine hang, so folding them into the stuck count/health
            // metric would mislead. Hard (actionable) stuck only here.
            const stuck = (result.stuck_agents || []).filter(s => !s.soft);
            cachedStuckAgents = stuck;
            const count = stuck.length;
            animateValue(countEl, count);

            // Show change from previous
            const stuckChange = formatChange(count, previousStats.stuckAgents);
            if (stuckChange) {
                // For stuck agents, an increase is bad (red), decrease is good (green)
                // formatChange shows up=green, down=red by default, so we invert
                const diff = count - (previousStats.stuckAgents || 0);
                if (diff > 0) {
                    detailEl.innerHTML = '<span class="change-arrow down">▲</span><span class="change-arrow down">+' + diff + '</span> ';
                } else if (diff < 0) {
                    detailEl.innerHTML = '<span class="change-arrow up">▼</span><span class="change-arrow up">' + diff + '</span> ';
                }
            }
            previousStats.stuckAgents = count;

            // Cross-reference: mark stuck agents in cachedAgents so agent cards show stuck badge
            const stuckIds = new Set(stuck.map(s => s.agent_id));
            const stuckMap = {};
            stuck.forEach(s => { stuckMap[s.agent_id] = s; });
            cachedAgents.forEach(a => {
                a._stuck = stuckIds.has(a.agent_id);
                a._stuckInfo = stuckMap[a.agent_id] || null;
            });
            // Re-render agent list to show stuck badges
            if (stuckIds.size > 0 && typeof applyAgentFilters === 'function') {
                applyAgentFilters();
            }

            // Style card based on count
            cardEl.classList.remove('stat-warning', 'stat-critical');
            if (count > 10) {
                cardEl.classList.add('stat-critical');
            } else if (count > 0) {
                cardEl.classList.add('stat-warning');
            }

            // Show breakdown by reason
            const byReason = result.summary?.by_reason || {};
            const parts = [];
            if (byReason.critical_margin_timeout > 0) parts.push(`${byReason.critical_margin_timeout} critical`);
            if (byReason.tight_margin_timeout > 0) parts.push(`${byReason.tight_margin_timeout} tight`);
            if (byReason.activity_timeout > 0) parts.push(`${byReason.activity_timeout} inactive`);
            detailEl.innerHTML = count > 0 ? parts.join(', ') : 'All agents healthy';
        } else {
            countEl.textContent = '-';
            detailEl.innerHTML = 'Could not check';
        }
    } catch (e) {
        console.debug('Could not load stuck agents:', e);
    }
}

// System health — fetches /health for DB pool, uptime, server status
async function loadSystemHealth() {
    try {
        const resp = await authFetch('/health');
        const data = await resp.json();
        const valueEl = document.getElementById('system-health-value');
        const detailEl = document.getElementById('system-health-detail');
        const cardEl = document.getElementById('system-health-card');
        if (!valueEl || !detailEl || !cardEl) return;

        const db = data.database || {};
        const uptime = data.uptime?.formatted || '?';

        cardEl.classList.remove('stat-warning', 'stat-critical');

        if (db.status === 'connected') {
            // Backend returns pool_idle (idle conns) and pool_size (total in pool); pool_max is max capacity
            const idle = db.pool_idle ?? db.pool_free ?? 0;
            const total = db.pool_size ?? 0;
            const max = Math.max(db.pool_max ?? total, 1);  // Avoid div by zero
            const active = Math.max(0, total - idle);
            const usage = max > 0 ? ((active / max) * 100).toFixed(0) : 0;

            if (usage > 90) {
                valueEl.textContent = '⚠ DB';
                cardEl.classList.add('stat-critical');
            } else if (usage > 70) {
                valueEl.textContent = 'OK';
                cardEl.classList.add('stat-warning');
            } else {
                valueEl.textContent = 'OK';
            }
            detailEl.innerHTML = `DB pool ${active}/${max} active · Up ${uptime}`;
        } else if (db.status === 'no_pool') {
            valueEl.textContent = '⚠';
            cardEl.classList.add('stat-warning');
            detailEl.innerHTML = `DB pool not initialized · Up ${uptime}`;
        } else {
            valueEl.textContent = '✗';
            cardEl.classList.add('stat-critical');
            detailEl.innerHTML = `DB ${db.status}: ${db.error || 'unknown'} · Up ${uptime}`;
        }
    } catch (e) {
        const valueEl = document.getElementById('system-health-value');
        const cardEl = document.getElementById('system-health-card');
        if (valueEl) valueEl.textContent = '✗';
        if (cardEl) cardEl.classList.add('stat-critical');
        const detailEl = document.getElementById('system-health-detail');
        if (detailEl) detailEl.innerHTML = 'Server unreachable';
    }
}

// S10.3: format the by_class breakdown as a chip strip for the card detail
// line. The envelope shape comes from sequential_calibration.compute_metrics_by_class:
//   { bootstrapped: bool, by_class: { class_tag: { eligible_samples, calibration_gap, ... } } }
// Returns a string like "substrate: 12 · ephemeral: 8" with the top
// buckets by sample count, or null when the envelope has no useful content.
function formatByClassChips(byClassEnvelope) {
    if (!byClassEnvelope || !byClassEnvelope.by_class) return null;
    var buckets = byClassEnvelope.by_class;
    var entries = Object.keys(buckets)
        .map(function (k) { return { name: k, samples: buckets[k].eligible_samples || 0 }; })
        .filter(function (e) { return e.samples > 0; })
        .sort(function (a, b) { return b.samples - a.samples; })
        .slice(0, 3);
    if (entries.length === 0) return null;
    return entries.map(function (e) { return e.name + ': ' + e.samples; }).join(' · ');
}

async function loadCalibration() {
    try {
        const result = await callTool('check_calibration', {});
        const valueEl = document.getElementById('calibration-value');
        const detailEl = document.getElementById('calibration-detail');
        if (!valueEl || !detailEl) return;
        if (result && result.success) {
            const samples = result.total_samples ?? 0;
            const th = result.trajectory_health ?? result.accuracy ?? 0;
            const byClass = result.by_class;
            const byClassBootstrapping = byClass && byClass.bootstrapped === false;
            if (samples === 0) {
                valueEl.textContent = '—';
                valueEl.style.color = '';
                detailEl.textContent = 'No samples \u00b7 Fleet-wide';
            } else if ((result.calibration_status || (result.calibrated ? 'calibrated' : 'miscalibrated')) === 'signal_stale') {
                var staleness = result.tactical_staleness_days;
                valueEl.textContent = 'Stale';
                valueEl.style.color = 'var(--color-warning, #eab308)';
                var staleStr = (staleness != null) ? staleness.toFixed(0) + 'd' : '?';
                detailEl.textContent = 'Tactical signal ' + staleStr + ' old · ' + samples + ' samples';
            } else {
                var calibrated = (result.calibration_status
                    ? result.calibration_status === 'calibrated'
                    : result.calibrated === true);
                valueEl.textContent = calibrated ? 'Yes' : 'No';
                valueEl.style.color = calibrated
                    ? 'var(--color-success, #22c55e)'
                    : 'var(--color-danger, #ef4444)';
                var trajectoryPct = (th * 100).toFixed(0);

                // S10.3: per-class breakdown leads the detail line when
                // available; per-channel chips (S22) follow. The bootstrap
                // window (pre-S10 state file pre-first-rebucket) renders
                // explicitly as "bootstrapping\u2026" so operators see the
                // gap honestly rather than reading partial buckets as
                // fleet-representative.
                var classChips = byClassBootstrapping
                    ? 'bootstrapping\u2026'
                    : formatByClassChips(byClass);
                var perChannel = result.per_channel_calibration;
                var channelChips = null;
                if (perChannel && Object.keys(perChannel).length > 0) {
                    channelChips = Object.keys(perChannel).map(function (name) {
                        var c = perChannel[name];
                        var icon = c.calibrated ? '\u2713' : '\u2715';
                        return name + ': ' + icon;
                    }).join(' \u00b7 ');
                }
                var detailParts = [samples + ' samples'];
                if (classChips) detailParts.push(classChips);
                if (channelChips) detailParts.push(channelChips);
                detailParts.push(trajectoryPct + '% trajectory');
                detailEl.textContent = detailParts.join(' \u00b7 ');
            }
        } else {
            valueEl.textContent = '-';
            valueEl.style.color = '';
            detailEl.textContent = '';
        }
    } catch (e) {
        const valueEl = document.getElementById('calibration-value');
        if (valueEl) {
            valueEl.textContent = '-';
            valueEl.style.color = '';
        }
    }
}

async function loadAnomalies() {
    try {
        const result = await callTool('detect_anomalies', {});
        const countEl = document.getElementById('anomalies-count');
        const detailEl = document.getElementById('anomalies-detail');
        if (!countEl || !detailEl) return;
        if (result && result.anomalies) {
            // stale=true marks an anomaly recomputed from a frozen (idle)
            // history window — already reported, not a current finding (#637).
            const fresh = result.anomalies.filter(a => a.stale !== true);
            const staleCount = result.anomalies.length - fresh.length;
            animateValue(countEl, fresh.length);
            detailEl.textContent = fresh.length > 0
                ? (fresh[0]?.type || 'Detected')
                : (staleCount > 0 ? staleCount + ' stale' : 'None');
        } else {
            countEl.textContent = '-';
            detailEl.textContent = '';
        }
    } catch (e) {
        const countEl = document.getElementById('anomalies-count');
        if (countEl) countEl.textContent = '-';
    }
}

async function loadServerInfo() {
    try {
        const resp = await authFetch('/health');
        const data = await resp.json();
        const el = document.getElementById('server-version');
        if (el && data) {
            const v = data.version || '';
            el.textContent = v ? 'v' + v : '-';
        }
    } catch (e) {
        const el = document.getElementById('server-version');
        if (el) el.textContent = '-';
    }
}

/**
 * Update the Quick Status hero section.
 * Shows fleet health at a glance: green/yellow/red dot + summary.
 */
/**
 * Render the "Needs attention" band under the hero from the fleet-severity
 * reasons. Each reason becomes a deep-link chip to its panel. The band is hidden
 * when there are no active exceptions (no empty-state noise).
 * @param {Array<{label:string, anchor:string, severity:string}>} reasons
 */
function renderNeedsAttention(reasons) {
    const band = document.getElementById('needs-attention');
    if (!band) return;
    reasons = reasons || [];
    if (reasons.length === 0) {
        band.innerHTML = '';
        band.hidden = true;
        return;
    }
    const esc = (typeof DataProcessor !== 'undefined' && DataProcessor.escapeHtml)
        ? DataProcessor.escapeHtml
        : (s => String(s));
    const chips = reasons.map(r => {
        const cls = 'needs-attention-chip sev-' + (r.severity === 'critical' ? 'critical' : 'caution');
        const label = esc(r.label) + ' →';
        return r.anchor
            ? '<a class="' + cls + '" href="' + esc(r.anchor) + '">' + label + '</a>'
            : '<span class="' + cls + '">' + label + '</span>';
    }).join('');
    band.innerHTML = '<span class="needs-attention-label">Needs attention</span>' + chips;
    band.hidden = false;
}

function updateQuickStatus(agents, stuckAgents) {
    const dot = document.getElementById('qs-dot');
    const label = document.getElementById('qs-label');
    const detail = document.getElementById('qs-detail');
    const lumen = document.getElementById('qs-lumen');
    if (!dot || !label) return;

    stuckAgents = stuckAgents || [];
    agents = agents || [];

    // Compute fleet health
    const criticalAgents = agents.filter(a => a.health_status === 'critical').length;
    const agentsWithMetrics = agents.filter(a => {
        const m = a.metrics || {};
        return m.coherence !== undefined && m.coherence !== null;
    });
    const avgCoherence = agentsWithMetrics.length > 0
        ? agentsWithMetrics.reduce((s, a) => s + Number(a.metrics.coherence), 0) / agentsWithMetrics.length
        : null;

    // Aggregate ALL severity sources (not just agents) so the hero can't read
    // "healthy" while the DB is down or Watcher has criticals. The panels publish
    // their summaries to state; computeFleetSeverity (fleet-severity.js) takes the
    // worst. Falls back to the legacy agent-only computation if the module is
    // absent. See docs/proposals/dashboard-hero-severity-rollup.md.
    let status = 'healthy';
    let statusText = 'All systems healthy';
    let attentionReasons = [];
    if (typeof computeFleetSeverity === 'function') {
        const severity = computeFleetSeverity({
            criticalAgents: criticalAgents,
            stuckCount: stuckAgents.length,
            avgCoherence: avgCoherence,
            systemHealth: state.get('systemHealthOverall'),
            watcher: state.get('watcherSummary'),
            sentinel: state.get('sentinelSummary'),
            silentResidents: state.get('silentResidents'),
        });
        // Map severity level → existing hero CSS class (caution → warning).
        status = severity.level === 'critical' ? 'critical'
            : severity.level === 'caution' ? 'warning' : 'healthy';
        statusText = severity.text;
        attentionReasons = severity.reasons;
    } else {
        if (stuckAgents.length > 2 || criticalAgents > 0) {
            status = 'critical';
            statusText = criticalAgents + ' critical' + (stuckAgents.length > 0 ? ', ' + stuckAgents.length + ' stuck' : '');
        } else if (stuckAgents.length > 0 || (avgCoherence !== null && avgCoherence < 0.4)) {
            status = 'warning';
            const parts = [];
            if (stuckAgents.length > 0) parts.push(stuckAgents.length + ' stuck');
            if (avgCoherence !== null && avgCoherence < 0.4) parts.push('coherence ' + (avgCoherence * 100).toFixed(0) + '%');
            statusText = parts.join(', ');
        }
    }

    dot.className = 'quick-status-dot ' + status;
    label.textContent = statusText;
    renderNeedsAttention(attentionReasons);

    // Detail: agent count
    if (detail) {
        detail.textContent = agents.length + ' agents' + (agentsWithMetrics.length > 0 ? ' · ' + agentsWithMetrics.length + ' reporting' : '');
    }

    // Lumen slot: show pinned agent or first active agent summary
    if (lumen) {
        const pinnedId = state.get('pinnedAgentId');
        const targetAgent = pinnedId
            ? agents.find(a => a.agent_id === pinnedId)
            : agents[0];
        if (targetAgent) {
            const m = targetAgent.metrics || {};
            const name = targetAgent.label || targetAgent.display_name || targetAgent.name || '';
            const v = m.verdict || '';
            const icon = v && typeof AgentsModule !== 'undefined' ? '' : '';
            lumen.textContent = name ? name + (v ? ' · ' + v : '') : '';
        } else {
            lumen.textContent = '';
        }
    }
}

/**
 * Load discoveries from API and render to panel.
 * Updates cachedDiscoveries and stats.
 * @returns {Promise<void>}
 */
async function loadDiscoveries(searchQuery = '', options = {}) {
    try {
        console.log('Loading discoveries...', searchQuery ? `(search: ${searchQuery})` : '');
        const force = options.force === true;
        const readOptions = searchQuery
            ? { useCache: !force }
            : { useCache: !force, cacheTimeout: CONFIG.KNOWLEDGE_READ_CACHE_MS };

        // Single API call — get discoveries and derive count from results
        // Vigil is hidden by default: the Vigil panel (see vigil.js) shows
        // its groundskeeper stream separately. Searching explicitly still
        // returns its entries since the user intent is clear.
        const toolArgs = {
            limit: 50,
            include_details: true,
        };

        if (searchQuery) {
            toolArgs.query = searchQuery;
        } else {
            toolArgs.exclude_agent_labels = ['Vigil'];
        }

        const searchResult = await callTool('search_knowledge_graph', toolArgs, readOptions);

        // Handle null/undefined result
        if (!searchResult) {
            throw new Error('No response from server');
        }

        // Check for error in response
        if (searchResult.error || searchResult.success === false) {
            const errorMsg = searchResult.error || searchResult.message || 'Unknown error';
            // Don't throw for empty results - that's valid
            if (errorMsg.includes('too many clients') || errorMsg.includes('connection')) {
                throw new Error(`Database connection issue: ${errorMsg}. The server may have too many connections open.`);
            }
            if (errorMsg.includes('fetch failed')) {
                throw new Error(`Database query failed: ${errorMsg}. This may indicate connection pool exhaustion.`);
            }
            // For other errors, log but continue with empty results
            console.warn('Knowledge graph search error:', errorMsg);
            cachedDiscoveries = [];
            updateDiscoveryFilterInfo(0);
            updateDiscoveryLegend([]);
            return false;
        }

        // Handle both array and object response formats
        let discoveries = [];
        if (Array.isArray(searchResult)) {
            discoveries = searchResult;
            console.log('Got array response with', discoveries.length, 'discoveries');
        } else if (searchResult.discoveries) {
            discoveries = searchResult.discoveries;
            console.log('Got discoveries array with', discoveries.length, 'items');
        } else if (searchResult.results) {
            discoveries = searchResult.results;
            console.log('Got results array with', discoveries.length, 'items');
        } else {
            // Unexpected format - log and try to continue
            console.warn('Unexpected response format:', searchResult);
            discoveries = [];
        }

        // Sort by ID (which contains ISO timestamp) descending to get most recent first
        // ID format: "2025-12-29T08:34:42.201273" - lexicographically sortable
        discoveries.sort((a, b) => {
            const aId = (a.id || '').trim();
            const bId = (b.id || '').trim();
            if (!aId && !bId) return 0;
            if (!aId) return 1;  // No ID goes to end
            if (!bId) return -1; // No ID goes to end
            return bId.localeCompare(aId);
        });

        const enrichedDiscoveries = discoveries.map(d => {
            // Parse date from id (format: "2025-12-29T08:34:42.201273" or "2026-01-01T23:40:53.202482")
            // IDs use ISO timestamps - parse correctly
            let dateStr = 'Unknown';
            let dateObj = null;
            if (d.id) {
                try {
                    const isoStr = d.id.substring(0, 19);
                    const [datePart, timePart] = isoStr.split('T');
                    const [year, month, day] = datePart.split('-').map(Number);
                    const [hour, minute, second] = timePart.split(':').map(Number);
                    dateObj = new Date(year, month - 1, day, hour, minute, second || 0);

                    if (!isNaN(dateObj.getTime())) {
                        dateStr = formatTimestamp(dateObj);
                    } else {
                        dateStr = isoStr.replace('T', ' ');
                    }
                } catch (e) {
                    dateStr = d.id.substring(0, 19).replace('T', ' ');
                }
            } else if (d.created_at) {
                dateObj = new Date(d.created_at);
                dateStr = !isNaN(dateObj.getTime()) ? formatTimestamp(d.created_at) : d.created_at;
            } else if (d.timestamp) {
                dateObj = new Date(d.timestamp);
                dateStr = !isNaN(dateObj.getTime()) ? formatTimestamp(d.timestamp) : d.timestamp;
            }

            const timestampMs = dateObj ? dateObj.getTime() : null;
            return {
                ...d,
                _displayDate: dateStr,
                _timestampMs: timestampMs,
                _relativeTime: timestampMs ? formatRelativeTime(timestampMs) : null
            };
        });

        cachedDiscoveries = enrichedDiscoveries;

        // Update stat card — fetch real total from knowledge stats
        const loadedCount = enrichedDiscoveries.length;
        let totalDiscoveries = loadedCount;
        try {
            const stats = await callTool('knowledge', { action: 'stats' }, readOptions);
            if (stats && stats.stats && stats.stats.total_discoveries !== undefined) {
                totalDiscoveries = stats.stats.total_discoveries;
            }
            // Render lifecycle bar
            const lifecycleBar = document.getElementById('knowledge-lifecycle-bar');
            if (lifecycleBar && stats?.stats?.by_status) {
                const bs = stats.stats.by_status;
                const bp = stats.stats.by_policy || {};
                const total = stats.stats.total_discoveries || 1;
                const segments = [
                    { key: 'open', count: bs.open || 0, color: 'var(--green, #2ecc71)', label: 'Open' },
                    { key: 'resolved', count: bs.resolved || 0, color: 'var(--accent-cyan, #06b6d4)', label: 'Resolved' },
                    { key: 'archived', count: bs.archived || 0, color: 'var(--text-secondary)', label: 'Archived' },
                    { key: 'cold', count: bs.cold || 0, color: 'var(--surface-2, #333)', label: 'Cold' },
                ];
                const barSegments = segments
                    .filter(s => s.count > 0)
                    .map(s => `<div class="lifecycle-segment" style="flex:${s.count};background:${s.color}" title="${s.label}: ${s.count}"></div>`)
                    .join('');
                const labels = segments
                    .filter(s => s.count > 0)
                    .map(s => `<span class="lifecycle-label"><span class="lifecycle-dot" style="background:${s.color}"></span>${s.label} ${s.count}</span>`)
                    .join('');
                const policyInfo = bp.permanent ? `${bp.permanent} permanent` : '';
                lifecycleBar.innerHTML = `<div class="lifecycle-track">${barSegments}</div><div class="lifecycle-labels">${labels}${policyInfo ? '<span class="lifecycle-label lifecycle-label-dim">' + policyInfo + '</span>' : ''}</div>`;
            }
        } catch (e) {
            console.debug('Could not fetch knowledge stats, using loaded count');
        }
        const countEl = document.getElementById('discoveries-count');
        if (countEl) animateValue(countEl, totalDiscoveries);
        const discoveriesChange = formatChange(totalDiscoveries, previousStats.discoveries);
        const changeEl = document.getElementById('discoveries-change');
        if (changeEl) {
            const parts = [];
            if (totalDiscoveries > loadedCount) parts.push(`Showing ${loadedCount}`);
            if (discoveriesChange) parts.push(discoveriesChange);
            changeEl.innerHTML = parts.join(' · ') || (totalDiscoveries > 0 ? 'Recent discoveries' : 'No discoveries yet');
        }
        previousStats.discoveries = totalDiscoveries;

        updateDiscoveryLegend(cachedDiscoveries);
        // Discoveries have their own panel — don't cross-seed into the activity
        // timeline. Live discovery_write events still arrive via WebSocket.
        // Re-apply local filters (type/time) to the new search results
        applyDiscoveryFilters();
        return true;

    } catch (error) {
        const errorMsg = error.message || 'Unknown error';
        console.error('Failed to load discoveries:', error);

        // Show helpful error message
        let userMessage = `Failed to load discoveries: ${errorMsg}`;
        let isRetryable = false;

        if (errorMsg.includes('too many clients') || errorMsg.includes('connection pool') || errorMsg.includes('connection issue')) {
            userMessage = 'Database connection pool exhausted. The server has too many open connections. Try refreshing in a moment or restart the server.';
            isRetryable = true;
        } else if (errorMsg.includes('fetch failed') || errorMsg.includes('timeout')) {
            userMessage = 'Database query timed out or failed. This may indicate connection issues. Try refreshing.';
            isRetryable = true;
        } else if (errorMsg.includes('401') || errorMsg.includes('Authentication')) {
            userMessage = 'Authentication required. Check if the server needs an API token.';
        } else if (errorMsg.includes('PostgreSQL') || errorMsg.includes('database')) {
            userMessage = 'Database error. Check server logs for details.';
            isRetryable = true;
        }

        // Show error banner if retryable
        if (isRetryable) {
            updateConnectionBanner(true);
        }

        showError(userMessage);
        cachedDiscoveries = [];
        state.set({ filteredDiscoveries: [] });
        updateDiscoveryFilterInfo(0);
        updateDiscoveryLegend([]);
        const container = document.getElementById('discoveries-container');
        if (container) {
            container.innerHTML = `<div class="loading">${escapeHtml(userMessage)}<br><small>Try Refresh or check server.</small></div>`;
        }
        const countEl = document.getElementById('discoveries-count');
        const changeEl = document.getElementById('discoveries-change');
        if (countEl) countEl.textContent = '?';
        if (changeEl) changeEl.innerHTML = 'Error loading';

        return false;
    }
}

// ============================================================================
// DIALECTIC SESSIONS
// ============================================================================

// cachedDialecticSessions managed by state.js bridge

async function loadDialecticSessions() {
    try {
        console.log('Loading dialectic sessions...');
        const result = await callTool('dialectic', {
            action: 'list',
            limit: 50,
            include_transcript: false
        });

        console.log('Dialectic sessions result:', result);

        // Handle null/undefined result
        if (!result) {
            throw new Error('No response from server');
        }

        // Check for error
        if (result.error || result.success === false) {
            console.warn('Dialectic sessions error:', result.error || result.message);
            cachedDialecticSessions = [];
            updateDialecticDisplay([], 'Error loading', { error: true });
            return false;
        }

        // Extract sessions - minimal filtering, sort by date
        const rawSessions = result.sessions || [];
        const sessions = rawSessions
            .sort((a, b) => {
                // Sort by date descending (most recent first)
                const dateA = new Date(a.created || 0);
                const dateB = new Date(b.created || 0);
                return dateB - dateA;
            });
        cachedDialecticSessions = sessions;

        // Update stat card
        const sessionsEl = document.getElementById('dialectic-sessions');
        const changeEl = document.getElementById('dialectic-change');
        if (sessionsEl) {
            animateValue(sessionsEl, sessions.length);
        }
        if (changeEl) {
            const resolved = sessions.filter(s => s.phase === 'resolved' || s.status === 'resolved').length;
            const active = sessions.filter(s => !['resolved', 'failed'].includes(s.phase || s.status)).length;
            const dialecticChange = formatChange(sessions.length, previousStats.dialecticSessions);
            changeEl.innerHTML = (dialecticChange ? dialecticChange + ' ' : '') + `${resolved} resolved, ${active} active`;
            previousStats.dialecticSessions = sessions.length;
        }

        // Dialectic sessions have their own panel — don't cross-seed into the
        // activity timeline. Stale sessions were resurfacing as "activity".

        // Apply current filter (respects user's active filter selection)
        applyDialecticFilters();

        return true;
    } catch (error) {
        console.error('Error loading dialectic sessions:', error);
        cachedDialecticSessions = [];
        updateDialecticDisplay([], 'Error loading', { error: true });
        return false;
    }
}

// Dialectic utilities, rendering, filtering, detail modal
// are now in dialectic.js → DialecticModule
var getPhaseColor = DialecticModule.getPhaseColor;
var formatDialecticPhase = DialecticModule.formatDialecticPhase;
var updateDialecticDisplay = DialecticModule.updateDialecticDisplay;
var updateDialecticFilterInfo = DialecticModule.updateDialecticFilterInfo;
var renderDialecticList = DialecticModule.renderDialecticList;
var applyDialecticFilters = DialecticModule.applyDialecticFilters;
var showDialecticDetail = DialecticModule.showDialecticDetail;
var renderDialecticDetailContent = DialecticModule.renderDialecticDetailContent;

// ============================================================================
// MAIN REFRESH & INITIALIZATION
// ============================================================================

/**
 * Main refresh function - loads all dashboard data.
 * @param {Object} [options]
 * @param {boolean} [options.force=false] - Force refresh even if paused
 * @returns {Promise<void>}
 */
async function refresh(options = {}) {
    const force = options.force === true;
    if (autoRefreshPaused && !force) {
        return;
    }

    console.log('Refreshing dashboard...', { force, paused: autoRefreshPaused });

    // When search is active, still refresh discoveries (search filters the cache)
    // but skip only if we're doing a non-forced refresh and user is mid-search.
    // Always do full refresh on force or when search is empty.

    clearError();
    // NOTE: do not stamp "last updated" here — that runs before any data loads
    // and unconditionally, so the clock advanced even when every request failed,
    // showing a fresh time over stale data. It is set only on success below.
    const lastUpdateEl = document.getElementById('last-update');

    try {
        console.log('Starting parallel load...');
        const results = await Promise.allSettled([
            loadAgents(),
            loadDiscoveries('', { force }),
            loadDialecticSessions(),
            loadStuckAgents(),
            loadSystemHealth(),
            loadCalibration(),
            loadAnomalies(),
            loadServerInfo()
        ]);
        console.log('Load results:', results);

        // Check if critical operations failed (agents and discoveries)
        const agentsResult = results[0];
        const discoveriesResult = results[1];
        const dialecticResult = results[2];
        // results[3] is loadStuckAgents - non-critical

        const criticalFailures = [
            agentsResult.status === 'rejected' || (agentsResult.status === 'fulfilled' && agentsResult.value === false),
            discoveriesResult.status === 'rejected' || (discoveriesResult.status === 'fulfilled' && discoveriesResult.value === false)
        ].filter(Boolean).length;

        // Only show connection banner if BOTH critical operations failed
        // This prevents false positives from transient errors
        if (criticalFailures >= 2) {
            updateConnectionBanner(true);
            console.warn('Critical operations failed:', {
                agents: agentsResult.status,
                discoveries: discoveriesResult.status
            });
        } else {
            updateConnectionBanner(false);
        }

        // Stamp "last updated" only on a successful refresh, so the clock can't
        // advance over stale data. On failure, mark the existing time stale
        // instead of refreshing it.
        if (lastUpdateEl) {
            if (criticalFailures < 2) {
                lastUpdateEl.textContent = new Date().toLocaleTimeString();
                lastUpdateEl.classList.remove('stale');
                lastUpdateEl.removeAttribute('title');
            } else {
                lastUpdateEl.classList.add('stale');
                lastUpdateEl.title = 'Last refresh failed — showing the last successful update time';
                if (!lastUpdateEl.textContent || lastUpdateEl.textContent === '-') {
                    lastUpdateEl.textContent = 'unavailable';
                }
            }
        }

        // Log any individual failures
        results.forEach((result, index) => {
            if (result.status === 'rejected') {
                console.error(`Load operation ${index} failed:`, result.reason);
            }
        });

        // Update quick status after all loads complete
        updateQuickStatus(cachedAgents, cachedStuckAgents);
    } catch (error) {
        // This should rarely happen since we're using Promise.allSettled
        updateConnectionBanner(true);
        if (lastUpdateEl) {
            lastUpdateEl.classList.add('stale');
            lastUpdateEl.title = 'Last refresh failed';
        }
        console.error('Refresh error:', error);
        showError(`Refresh failed: ${error.message}`);
    }
}

const agentSearchInput = document.getElementById('agent-search');
const agentStatusFilterInput = document.getElementById('agent-status-filter');
const agentMetricsOnlyInput = document.getElementById('agent-metrics-only');
if (agentSearchInput) {
    agentSearchInput.addEventListener('input', debounce(applyAgentFilters, CONFIG.DEBOUNCE_MS));
}
if (agentStatusFilterInput) {
    agentStatusFilterInput.addEventListener('change', applyAgentFilters);
}
if (agentMetricsOnlyInput) {
    agentMetricsOnlyInput.addEventListener('change', applyAgentFilters);
}
const agentSortInput = document.getElementById('agent-sort');
if (agentSortInput) {
    agentSortInput.addEventListener('change', applyAgentFilters);
}
const agentClearFiltersButton = document.getElementById('agent-clear-filters');
if (agentClearFiltersButton) {
    agentClearFiltersButton.addEventListener('click', clearAgentFilters);
}

// Agent filters toggle
const agentFiltersToggle = document.getElementById('agent-filters-toggle');
const agentFiltersRow = document.getElementById('agent-filters-row');
if (agentFiltersToggle && agentFiltersRow) {
    agentFiltersToggle.addEventListener('click', () => {
        const isCollapsed = agentFiltersRow.classList.toggle('collapsed');
        agentFiltersToggle.classList.toggle('active', !isCollapsed);
    });
}

// Compare agents
const agentCompareBtn = document.getElementById('agent-compare-btn');
if (agentCompareBtn) {
    agentCompareBtn.addEventListener('click', async () => {
        const agents = cachedAgents.filter(a => (a.metrics?.E ?? a.metrics?.coherence) != null);
        if (agents.length < 2) {
            showError('Need at least 2 agents with metrics to compare');
            return;
        }
        const modal = document.getElementById('panel-modal');
        const modalTitle = document.getElementById('modal-title');
        const modalBody = document.getElementById('modal-body');
        if (!modal || !modalTitle || !modalBody) return;
        modalTitle.textContent = 'Compare Agents';
        const opts = agents.map(a => `<option value="${escapeHtml(a.agent_id)}">${escapeHtml(a.label || a.name || a.agent_id)}</option>`).join('');
        modalBody.innerHTML = '<div class="grid-2col mb-md">' +
            '<div><label>Agent 1:</label><select id="compare-agent1">' + opts + '</select></div>' +
            '<div><label>Agent 2:</label><select id="compare-agent2">' + opts + '</select></div>' +
            '</div><button class="panel-button" id="compare-run">Compare</button><div id="compare-result" class="mt-md"></div>';
        const runBtn = modalBody.querySelector('#compare-run');
        const resultDiv = modalBody.querySelector('#compare-result');
        const select2 = modalBody.querySelector('#compare-agent2');
        if (select2 && agents.length > 1) select2.selectedIndex = 1;
        runBtn.addEventListener('click', async () => {
            const id1 = modalBody.querySelector('#compare-agent1').value;
            const id2 = modalBody.querySelector('#compare-agent2').value;
            if (id1 === id2) { resultDiv.innerHTML = 'Select different agents'; return; }
            runBtn.disabled = true;
            resultDiv.innerHTML = 'Loading...';
            try {
                const r = await callTool('compare_agents', { agent_ids: [id1, id2] });
                if (r?.success === false || r?.error) {
                    resultDiv.innerHTML = '<div class="compare-error">Error: ' + escapeHtml(r.error || 'Unknown error') + '</div>';
                    runBtn.disabled = false;
                    return;
                }
                const c = r?.comparison || r;
                if (!c || !c.agents || c.agents.length < 2) {
                    resultDiv.innerHTML = '<div class="compare-error">No comparison data returned</div>';
                    runBtn.disabled = false;
                    return;
                }
                resultDiv.innerHTML = renderCompareResult(c, agents);
            } catch (e) {
                resultDiv.innerHTML = '<div class="compare-error">Error: ' + escapeHtml(e.message) + '</div>';
            }
            runBtn.disabled = false;
        });
        modal.classList.add('visible');
        document.body.style.overflow = 'hidden';
    });
}

function renderCompareResult(comparison, cachedList) {
    const a1 = comparison.agents[0];
    const a2 = comparison.agents[1];
    const label = (id) => {
        const found = cachedList.find(a => a.agent_id === id);
        return escapeHtml(found?.label || found?.name || id.substring(0, 12));
    };
    const name1 = label(a1.agent_id);
    const name2 = label(a2.agent_id);

    const healthColor = (s) => s === 'healthy' ? 'var(--green)' : s === 'moderate' ? 'var(--orange)' : 'var(--red, #e55)';
    const verdictColor = (v) => v === 'proceed' || v === 'approve' ? 'var(--green)' : v === 'caution' || v === 'guide' ? 'var(--orange)' : 'var(--red, #e55)';

    // EISV metrics with ranges for bar scaling
    const metrics = [
        { key: 'E', label: 'Energy', max: 1, color: 'var(--energy, #4ecdc4)' },
        { key: 'I', label: 'Integrity', max: 1, color: 'var(--integrity, #45b7d1)' },
        { key: 'S', label: 'Entropy', max: 1, color: 'var(--entropy, #f7dc6f)', invert: true },
        { key: 'V', label: 'Void', max: 0.5, color: 'var(--volatility, #bb8fce)', signed: true },
        { key: 'coherence', label: 'Coherence', max: 1, color: 'var(--coherence, #85c1e9)' },
        { key: 'risk_score', label: 'Risk', max: 1, color: 'var(--red, #e55)', invert: true },
    ];

    const barWidth = (val, max) => Math.min(Math.abs(val) / max * 100, 100);

    let barsHtml = metrics.map(m => {
        const v1 = a1[m.key] ?? 0;
        const v2 = a2[m.key] ?? 0;
        const diff = v2 - v1;
        const diffStr = (diff >= 0 ? '+' : '') + diff.toFixed(3);
        const diffClass = m.invert ? (diff > 0.02 ? 'worse' : diff < -0.02 ? 'better' : '') : (diff > 0.02 ? 'better' : diff < -0.02 ? 'worse' : '');
        return `<div class="compare-metric-row">
            <div class="compare-metric-label">${m.label}</div>
            <div class="compare-bars">
                <div class="compare-bar-cell">
                    <div class="compare-bar-track"><div class="compare-bar" style="width:${barWidth(v1, m.max)}%;background:${m.color};opacity:0.8"></div></div>
                    <span class="compare-bar-val">${v1.toFixed(3)}</span>
                </div>
                <div class="compare-bar-cell">
                    <div class="compare-bar-track"><div class="compare-bar" style="width:${barWidth(v2, m.max)}%;background:${m.color};opacity:0.8"></div></div>
                    <span class="compare-bar-val">${v2.toFixed(3)}</span>
                </div>
            </div>
            <div class="compare-diff ${diffClass}">${diffStr}</div>
        </div>`;
    }).join('');

    // Similarities & outliers
    let extrasHtml = '';
    if (comparison.similarities?.length > 0) {
        extrasHtml += '<div class="compare-section"><h4>Similarities</h4><ul class="compare-findings">' +
            comparison.similarities.map(s => `<li>${escapeHtml(s.description)} <span class="compare-sim-score">(${(s.similarity * 100).toFixed(0)}%)</span></li>`).join('') +
            '</ul></div>';
    }
    if (comparison.outliers?.length > 0) {
        extrasHtml += '<div class="compare-section"><h4>Outliers</h4><ul class="compare-findings">' +
            comparison.outliers.map(o => `<li><strong>${label(o.agent_id)}</strong>: ${escapeHtml(o.reason)} (${o.value.toFixed(3)} vs mean ${o.mean.toFixed(3)})</li>`).join('') +
            '</ul></div>';
    }

    return `<div class="compare-result">
        <div class="compare-header">
            <div class="compare-agent-col">
                <span class="compare-agent-name">${name1}</span>
                <span class="compare-badge" style="color:${verdictColor(a1.verdict)}">${escapeHtml(a1.verdict || '-')}</span>
                <span class="compare-badge" style="color:${healthColor(a1.health_status)}">${escapeHtml(a1.health_status || '-')}</span>
            </div>
            <div class="compare-agent-col">
                <span class="compare-agent-name">${name2}</span>
                <span class="compare-badge" style="color:${verdictColor(a2.verdict)}">${escapeHtml(a2.verdict || '-')}</span>
                <span class="compare-badge" style="color:${healthColor(a2.health_status)}">${escapeHtml(a2.health_status || '-')}</span>
            </div>
            <div class="compare-diff-header">Diff</div>
        </div>
        ${barsHtml}
        ${extrasHtml}
    </div>`;
}

// Debounce helper
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

const discoverySearchInput = document.getElementById('discovery-search');
const discoveryTypeFilterInput = document.getElementById('discovery-type-filter');
const discoveryTimeFilterInput = document.getElementById('discovery-time-filter');

if (discoverySearchInput) {
    discoverySearchInput.addEventListener('input', debounce(applyDiscoveryFilters, CONFIG.DEBOUNCE_MS));
}
if (discoveryTypeFilterInput) {
    discoveryTypeFilterInput.addEventListener('change', applyDiscoveryFilters);
}
if (discoveryTimeFilterInput) {
    discoveryTimeFilterInput.addEventListener('change', applyDiscoveryFilters);
}
const discoveryClearFiltersButton = document.getElementById('discovery-clear-filters');
if (discoveryClearFiltersButton) {
    discoveryClearFiltersButton.addEventListener('click', () => {
        clearDiscoveryFilters();
        // Reset to full list from server
        loadDiscoveries('', { force: true });
    });
}
const discoveryLegend = document.getElementById('discoveries-type-legend');
if (discoveryLegend && discoveryTypeFilterInput) {
    discoveryLegend.addEventListener('click', event => {
        const chip = event.target.closest('.discovery-type');
        if (!chip) return;
        const type = chip.getAttribute('data-type');
        if (!type) return;
        discoveryTypeFilterInput.value = type;
        applyDiscoveryFilters();
    });
}

const refreshNowButton = document.getElementById('refresh-now');
const pauseRefreshInput = document.getElementById('pause-refresh');
if (refreshNowButton) {
    refreshNowButton.addEventListener('click', () => refresh({ force: true }));
}
if (pauseRefreshInput) {
    pauseRefreshInput.addEventListener('change', event => {
        autoRefreshPaused = event.target.checked;
        updateRefreshStatus();
    });
}
updateRefreshStatus();

const agentsContainer = document.getElementById('agents-container');
if (agentsContainer) {
    agentsContainer.addEventListener('click', event => {
        // Show more pagination (must be checked before .agent-item fallthrough)
        const showMore = event.target.closest('.show-more-btn');
        if (showMore) {
            event.stopPropagation();
            state.set({ agentPageSize: state.get('agentPageSize') + 20 });
            applyAgentFilters();
            return;
        }

        // Metrics toggle
        const metricsToggle = event.target.closest('.agent-metrics-toggle');
        if (metricsToggle) {
            event.stopPropagation();
            const card = metricsToggle.closest('.agent-item');
            if (card) card.classList.toggle('metrics-expanded');
            return;
        }

        // Handle pin button click
        const pinBtn = event.target.closest('button[data-action="pin"]');
        if (pinBtn) {
            event.stopPropagation();
            const agentId = pinBtn.getAttribute('data-agent-id');
            const agentName = pinBtn.getAttribute('data-agent-name');
            if (!agentId) return;
            if (state.get('pinnedAgentId') === agentId) {
                state.set({ pinnedAgentId: null, pinnedAgentName: null });
                localStorage.removeItem('unitares_pinned_agent_id');
                localStorage.removeItem('unitares_pinned_agent_name');
            } else {
                state.set({ pinnedAgentId: agentId, pinnedAgentName: agentName || agentId });
                localStorage.setItem('unitares_pinned_agent_id', agentId);
                localStorage.setItem('unitares_pinned_agent_name', agentName || agentId);
            }
            applyAgentFilters();
            return;
        }

        // Handle copy-id button click (don't bubble to agent detail)
        const button = event.target.closest('button[data-action="copy-id"]');
        if (button) {
            event.stopPropagation();
            const agentId = button.getAttribute('data-agent-id');
            if (!agentId) return;
            copyToClipboard(agentId)
                .then(() => {
                    const originalLabel = button.textContent;
                    button.textContent = 'Copied';
                    setTimeout(() => {
                        button.textContent = originalLabel;
                    }, CONFIG.COPY_FEEDBACK_MS);
                })
                .catch(() => {
                    const originalLabel = button.textContent;
                    button.textContent = 'Copy failed';
                    setTimeout(() => {
                        button.textContent = originalLabel;
                    }, CONFIG.COPY_FEEDBACK_MS);
                });
            return;
        }

        // Handle agent card click → open detail modal
        const agentItem = event.target.closest('.agent-item');
        if (!agentItem) return;
        const agentUuid = agentItem.getAttribute('data-agent-uuid');
        if (!agentUuid) return;
        const agent = cachedAgents.find(a => (a.agent_id || '') === agentUuid);
        if (agent) showAgentDetail(agent);
    });

    // Keyboard: agent cards are role="button" tabindex="0" — Enter/Space opens
    // detail, matching the click path above so the primary interaction is not
    // mouse-only (WCAG 2.1.1). Only act when the card itself is focused, so
    // Enter/Space inside nested controls (copy/pin/metrics toggle) is untouched.
    agentsContainer.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const agentItem = event.target.closest('.agent-item');
        if (!agentItem || event.target !== agentItem) return;
        const agentUuid = agentItem.getAttribute('data-agent-uuid');
        if (!agentUuid) return;
        const agent = cachedAgents.find(a => (a.agent_id || '') === agentUuid);
        if (agent) {
            event.preventDefault(); // stop Space from scrolling the page
            showAgentDetail(agent);
        }
    });
}

const agentsLegend = document.getElementById('agents-status-legend');
if (agentsLegend && agentStatusFilterInput) {
    agentsLegend.addEventListener('click', event => {
        const chip = event.target.closest('.status-chip');
        if (!chip) return;
        const status = chip.getAttribute('data-status');
        if (!status) return;
        agentStatusFilterInput.value = status;
        applyAgentFilters();
    });
}

// Dialectic sessions event listeners
const dialecticStatusFilter = document.getElementById('dialectic-status-filter');
const dialecticRefreshButton = document.getElementById('dialectic-refresh');
if (dialecticStatusFilter) {
    dialecticStatusFilter.addEventListener('change', applyDialecticFilters);
}
if (dialecticRefreshButton) {
    dialecticRefreshButton.addEventListener('click', async () => {
        dialecticRefreshButton.disabled = true;
        dialecticRefreshButton.textContent = 'Loading...';
        try {
            await loadDialecticSessions();
        } finally {
            dialecticRefreshButton.disabled = false;
            dialecticRefreshButton.textContent = 'Refresh';
        }
    });
}

// Click handler for dialectic items to show full details
const dialecticContainer = document.getElementById('dialectic-container');
if (dialecticContainer) {
    dialecticContainer.addEventListener('click', (event) => {
        if (event.target.closest('.dialectic-session-id-copy')) return; // Copy button, don't open detail
        const item = event.target.closest('.dialectic-item');
        if (!item) return;
        const sessionId = item.getAttribute('data-session-id');
        if (!sessionId) return;

        const session = cachedDialecticSessions.find(s => s.session_id === sessionId);
        if (!session) return;
        showDialecticDetail(session);
    });
}

// Click handler for dialectic session ID copy (list and modal)
document.addEventListener('click', (event) => {
    const copyEl = event.target.closest('.dialectic-session-id-copy');
    if (!copyEl) return;
    event.preventDefault();
    event.stopPropagation();
    const sid = copyEl.getAttribute('data-session-id');
    if (sid && typeof copyToClipboard === 'function') {
        copyToClipboard(sid).then(() => {
            const hint = copyEl.querySelector('.copy-hint');
            if (hint) { const t = hint.textContent; hint.textContent = 'Copied!'; setTimeout(() => { hint.textContent = t; }, 1500); }
        }).catch(() => {});
    }
});

// Click handler for tags — sets them as the search term in the relevant panel
document.addEventListener('click', (event) => {
    const tag = event.target.closest('.clickable-tag');
    if (!tag) return;
    event.stopPropagation(); // Don't trigger parent click (e.g. discovery detail modal)
    const tagText = tag.getAttribute('data-tag') || tag.textContent.trim();
    if (!tagText) return;

    // Figure out which search to populate: discovery or agent
    const inDiscovery = tag.closest('.discoveries-list, .discovery-detail');
    const inAgent = tag.closest('.agents-panel, .agent-detail');

    if (inDiscovery || (!inAgent)) {
        // Default: search discoveries
        const searchInput = document.getElementById('discovery-search');
        if (searchInput) {
            searchInput.value = tagText;
            applyDiscoveryFilters();
            searchInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
            searchInput.focus();
        }
    } else {
        // Agent panel
        const searchInput = document.getElementById('agent-search');
        if (searchInput) {
            searchInput.value = tagText;
            applyAgentFilters();
            searchInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
            searchInput.focus();
        }
    }

    // Close modal if open (tag was clicked inside a detail view)
    const modal = document.getElementById('panel-modal');
    if (modal && modal.classList.contains('visible')) {
        modal.classList.remove('visible');
        document.body.style.overflow = '';
    }
});

// Click handler for discovery items to show full details
const discoveriesContainer = document.getElementById('discoveries-container');
if (discoveriesContainer) {
    discoveriesContainer.addEventListener('click', (event) => {
        const item = event.target.closest('.discovery-item');
        if (!item) return;
        const index = parseInt(item.getAttribute('data-discovery-index'), 10);
        if (isNaN(index) || index < 0) return;

        const filtered = state.get('filteredDiscoveries') || cachedDiscoveries;
        if (index >= filtered.length) return;
        const discovery = filtered[index];
        showDiscoveryDetail(discovery);
    });
}

// Click handler for stuck agents card — shows current + server history
const stuckAgentsCard = document.getElementById('stuck-agents-card');
if (stuckAgentsCard) {
    stuckAgentsCard.addEventListener('click', async () => {
        const modal = document.getElementById('panel-modal');
        const modalTitle = document.getElementById('modal-title');
        const modalBody = document.getElementById('modal-body');
        if (!modal || !modalTitle || !modalBody) return;

        const stuck = cachedStuckAgents || [];
        modalTitle.textContent = 'Stuck Agents (' + stuck.length + ')';
        modal.classList.add('visible');
        document.body.style.overflow = 'hidden';

        // Current
        let html = '<div style="margin-bottom:16px;">';
        if (stuck.length > 0) {
            html += renderStuckAgentsForModal(stuck);
        } else {
            html += '<div style="padding:8px; opacity:0.6;">No agents currently stuck</div>';
        }
        html += '</div>';

        // Server history
        modalBody.innerHTML = html + '<div class="loading" style="font-size:12px;">Loading history...</div>';
        const incidents = await fetchIncidents('stuck_detected');
        html += '<h3 style="font-size:13px; opacity:0.5; margin:16px 0 8px; border-top:1px solid rgba(255,255,255,0.06); padding-top:12px;">History (' + incidents.length + ' incidents)</h3>';
        if (incidents.length === 0) {
            html += '<div style="opacity:0.5; padding:8px; font-size:12px;">No incidents recorded yet</div>';
        } else {
            html += incidents.map(function (inc) {
                const d = inc.details || {};
                const tsMs = new Date(inc.timestamp).getTime();
                const relative = !isNaN(tsMs) && formatRelativeTime ? formatRelativeTime(tsMs) : null;
                const absolute = formatTimestamp(inc.timestamp);
                const when = relative ? relative + ' — ' + absolute : (absolute || inc.timestamp);
                const names = (d.agents || []).map(a => escapeHtml(a.agent_name || a.agent_id || '?')).join(', ');
                return '<div style="padding:6px 8px; border-left:2px solid var(--accent-orange); margin-bottom:4px; font-size:12px;">' +
                    '<span style="opacity:0.5;">' + when + '</span> &mdash; ' +
                    '<strong>' + (d.count || '?') + ' stuck</strong>' +
                    (names ? ' <span style="opacity:0.6;">(' + names + ')</span>' : '') +
                    '</div>';
            }).join('');
        }
        modalBody.innerHTML = html;
    });
}

// Calibration card - expand to modal
const calibrationCard = document.getElementById('calibration-card');
if (calibrationCard) {
    calibrationCard.addEventListener('click', async () => {
        const modal = document.getElementById('panel-modal');
        const modalTitle = document.getElementById('modal-title');
        const modalBody = document.getElementById('modal-body');
        if (!modal || !modalTitle || !modalBody) return;
        modalTitle.textContent = 'Calibration';
        modalBody.innerHTML = '<div class="loading">Loading...</div>';
        modal.classList.add('visible');
        document.body.style.overflow = 'hidden';
        try {
            const r = await callTool('check_calibration', {});
            const th = r?.trajectory_health ?? r?.accuracy ?? 0;
            const dist = r?.confidence_distribution || {};
            const samples = r?.total_samples ?? 0;
            const issues = r?.issues || [];
            const status = r?.calibration_status
                || (samples === 0 ? 'no_data' : (r?.calibrated ? 'calibrated' : 'miscalibrated'));
            const staleness = r?.tactical_staleness_days;
            const statusLabels = {
                calibrated: 'Yes',
                miscalibrated: 'No',
                signal_stale: 'Unknown (signal stale)',
                no_data: 'Unknown (no samples yet)',
            };
            var calHtml = '<div class="detail-section">' +
                '<div><strong>Calibrated:</strong> ' + (statusLabels[status] || status) + '</div>' +
                (staleness != null
                    ? '<div><strong>Tactical signal age:</strong> ' + staleness.toFixed(1) + ' days</div>'
                    : '') +
                '<div><strong>Trajectory health:</strong> ' + (th * 100).toFixed(1) + '%</div>' +
                '<div><strong>Samples:</strong> ' + samples + '</div>' +
                (dist.mean != null ? '<div><strong>Confidence mean:</strong> ' + (dist.mean * 100).toFixed(1) + '%</div>' : '');

            // S10.3: per-class breakdown panel. Renders bootstrap banner when
            // the tracker has agent history that hasn't been rebucketed yet
            // (first 30min after a pre-S10 state file load). Class envelopes
            // carry only descriptive statistics — log_evidence / capped_alarm
            // are intentionally absent (see plan.md §3.4 anytime-validity scope).
            var byClassData = r?.by_class;
            if (byClassData) {
                calHtml += '<div style="margin-top:12px;border-top:1px solid rgba(255,255,255,0.06);padding-top:8px"><strong>By class:</strong>';
                if (byClassData.bootstrapped === false) {
                    calHtml += ' <span style="opacity:0.6;font-style:italic">(bootstrapping — class data sparse until next 30-min sweeper tick)</span>';
                }
                var buckets = byClassData.by_class || {};
                var bucketNames = Object.keys(buckets).filter(function (n) {
                    return (buckets[n].eligible_samples || 0) > 0;
                });
                if (bucketNames.length === 0) {
                    calHtml += '<div style="opacity:0.5;padding:4px 0;font-size:12px">No per-class samples yet</div>';
                } else {
                    bucketNames.sort(function (a, b) {
                        return (buckets[b].eligible_samples || 0) - (buckets[a].eligible_samples || 0);
                    });
                    calHtml += '<ul style="margin:4px 0;padding-left:18px">';
                    bucketNames.forEach(function (name) {
                        var b = buckets[name];
                        var accPct = (b.empirical_accuracy != null) ? (b.empirical_accuracy * 100).toFixed(1) + '%' : '—';
                        var gap = (b.calibration_gap != null) ? b.calibration_gap.toFixed(3) : '—';
                        calHtml += '<li>' + escapeHtml(name) + ': '
                            + b.eligible_samples + ' samples · '
                            + accPct + ' accuracy · '
                            + 'gap ' + gap + '</li>';
                    });
                    calHtml += '</ul>';
                }
                calHtml += '</div>';
            }

            if (issues.length > 0) {
                calHtml += '<div style="margin-top:8px"><strong>Issues:</strong><ul style="margin:4px 0;padding-left:18px">';
                issues.forEach(function (iss) { calHtml += '<li>' + escapeHtml(iss) + '</li>'; });
                calHtml += '</ul></div>';
            }
            calHtml += '</div>';
            modalBody.innerHTML = calHtml;
        } catch (e) {
            modalBody.innerHTML = '<div class="loading">Error: ' + escapeHtml(e.message) + '</div>';
        }
    });
}

// Anomalies card — shows current + server history
const anomaliesCard = document.getElementById('anomalies-card');
if (anomaliesCard) {
    anomaliesCard.addEventListener('click', async () => {
        const modal = document.getElementById('panel-modal');
        const modalTitle = document.getElementById('modal-title');
        const modalBody = document.getElementById('modal-body');
        if (!modal || !modalTitle || !modalBody) return;
        modalTitle.textContent = 'Anomalies';
        modalBody.innerHTML = '<div class="loading">Loading...</div>';
        modal.classList.add('visible');
        document.body.style.overflow = 'hidden';
        try {
            const r = await callTool('detect_anomalies', {});
            const anomalies = r?.anomalies || [];

            // Current — fresh findings first; stale ones (recomputed from a
            // frozen idle window, #637) are demoted and labeled, not hidden.
            const ordered = anomalies.slice().sort((a, b) =>
                (a.stale === true ? 1 : 0) - (b.stale === true ? 1 : 0));
            let html = '<div style="margin-bottom:16px;">';
            if (ordered.length === 0) {
                html += '<div style="padding:8px; opacity:0.6;">No anomalies currently detected</div>';
            } else {
                html += ordered.map(a =>
                    '<div style="margin-bottom:10px; padding:8px; border-left:3px solid var(--accent-orange);' +
                    (a.stale === true ? ' opacity:0.5;' : '') + '">' +
                    '<strong>' + escapeHtml(a.type || 'anomaly') + '</strong> ' + escapeHtml(a.severity || '') +
                    (a.stale === true ? ' <span style="font-size:11px; opacity:0.7;">(stale — idle window, already reported)</span>' : '') + '<br>' +
                    escapeHtml(a.description || '') + ' ' + (a.agent_id ? '<code>' + escapeHtml(a.agent_id) + '</code>' : '') +
                    '</div>'
                ).join('');
            }
            html += '</div>';

            // Server history
            const incidents = await fetchIncidents('anomaly_detected');
            html += '<h3 style="font-size:13px; opacity:0.5; margin:16px 0 8px; border-top:1px solid rgba(255,255,255,0.06); padding-top:12px;">History (' + incidents.length + ' incidents)</h3>';
            if (incidents.length === 0) {
                html += '<div style="opacity:0.5; padding:8px; font-size:12px;">No incidents recorded yet</div>';
            } else {
                html += incidents.map(function (inc) {
                    const d = inc.details || {};
                    const tsMs = new Date(inc.timestamp).getTime();
                    const relative = !isNaN(tsMs) && formatRelativeTime ? formatRelativeTime(tsMs) : null;
                    const absolute = formatTimestamp(inc.timestamp);
                    const when = relative ? relative + ' — ' + absolute : (absolute || inc.timestamp);
                    // Two shapes coexist: legacy batch entries (agent_id='system',
                    // details.anomalies is an array) and per-agent entries
                    // (inc.agent_id is the affected agent, details has scalar
                    // type/severity/description).
                    let count;
                    let anomalyDetails;
                    if (Array.isArray(d.anomalies)) {
                        count = d.count || d.anomalies.length || 0;
                        anomalyDetails = d.anomalies.map(function (a) {
                            const agentName = a.agent_name || (a.agent_id ? a.agent_id.substring(0, 8) + '...' : '');
                            const desc = a.description || '';
                            return escapeHtml(a.type || '?') + ' (' + escapeHtml(a.severity || '') + ')' +
                                (agentName ? ' <code>' + escapeHtml(agentName) + '</code>' : '') +
                                (desc ? ' — ' + escapeHtml(desc) : '');
                        }).join('; ');
                    } else {
                        count = 1;
                        const agentName = inc.agent_id && inc.agent_id !== 'system'
                            ? inc.agent_id.substring(0, 8) + '...'
                            : '';
                        const desc = d.description || '';
                        anomalyDetails = escapeHtml(d.type || '?') + ' (' + escapeHtml(d.severity || '') + ')' +
                            (agentName ? ' <code>' + escapeHtml(agentName) + '</code>' : '') +
                            (desc ? ' — ' + escapeHtml(desc) : '');
                    }
                    return '<div style="padding:6px 8px; border-left:2px solid var(--accent-orange); margin-bottom:4px; font-size:12px;">' +
                        '<span style="opacity:0.5;">' + when + '</span> &mdash; ' +
                        '<strong>' + count + ' anomal' + (count === 1 ? 'y' : 'ies') + '</strong>' +
                        (anomalyDetails ? '<br><span style="opacity:0.7;">' + anomalyDetails + '</span>' : '') +
                        '</div>';
                }).join('');
            }
            modalBody.innerHTML = html;
        } catch (e) {
            modalBody.innerHTML = '<div class="loading">Error: ' + escapeHtml(e.message) + '</div>';
        }
    });
}

// ============================================================================
// Stat-card navigation — display-only cards jump to their owning section
// and apply a sensible default filter. Cards with their own modal/expand
// behavior (Stuck, Calibration, Anomalies) are untouched.
// ============================================================================
(function initStatCardNav() {
    const nav = document.getElementById('section-nav');
    const navOffset = () => (nav ? nav.offsetHeight + 12 : 0);

    function scrollToSection(id) {
        const target = document.getElementById(id);
        if (!target) return;
        const pos = target.getBoundingClientRect().top + window.scrollY - navOffset();
        window.scrollTo({ top: pos, behavior: 'smooth' });
    }

    function wire(cardId, sectionId, applyPreset) {
        const card = document.getElementById(cardId);
        if (!card) return;
        card.classList.add('stat-card-nav');
        card.setAttribute('role', 'button');
        card.setAttribute('tabindex', '0');
        const existingTitle = card.getAttribute('title');
        if (!existingTitle) card.setAttribute('title', 'Click to open section');
        const go = (e) => {
            // Don't hijack clicks on the help-icon popover
            if (e && e.target && e.target.closest('.help-icon-placeholder')) return;
            scrollToSection(sectionId);
            if (applyPreset) { try { applyPreset(); } catch (err) { /* best-effort */ } }
        };
        card.addEventListener('click', go);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(e); }
        });
    }

    function setSelectValue(id, value) {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.value === value) return;
        el.value = value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    wire('fleet-coherence-card', 'eisv-chart-panel');

    wire('agents-count-card', 'agents-section', () => {
        setSelectValue('agent-status-filter', 'active');
        // Expand filters row so the user sees what's applied
        const row = document.getElementById('agent-filters-row');
        if (row && row.classList.contains('collapsed')) row.classList.remove('collapsed');
    });

    wire('discoveries-count-card', 'discoveries-section', () => {
        setSelectValue('discovery-time-filter', '24h');
    });

    wire('dialectic-count-card', 'dialectic-section', () => {
        setSelectValue('dialectic-status-filter', 'substantive');
    });

    // Trust tier bars — click a tier to filter Agents by that tier.
    // Click the already-active tier to clear.
    const trustCard = document.getElementById('trust-tier-card');
    if (trustCard) {
        const toggleTier = (tierNum) => {
            const current = state.get('agentTierFilter');
            const next = current === tierNum ? null : tierNum;
            state.set({ agentTierFilter: next });
            if (typeof applyAgentFilters === 'function') applyAgentFilters();
            scrollToSection('agents-section');
        };
        trustCard.addEventListener('click', (e) => {
            const row = e.target.closest('.trust-bar-row');
            if (!row) return;
            const tierNum = parseInt(row.dataset.tier, 10);
            if (isNaN(tierNum)) return;
            e.stopPropagation();
            toggleTier(tierNum);
        });
        trustCard.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const row = e.target.closest('.trust-bar-row');
            if (!row) return;
            const tierNum = parseInt(row.dataset.tier, 10);
            if (isNaN(tierNum)) return;
            e.preventDefault();
            toggleTier(tierNum);
        });
    }

    // Fleet Health card — click the "N critical" count to filter the Agents list
    // to critical agents (so the count is never a dead, unclickable number).
    // Click again (or Reset) to clear.
    const fleetCard = document.getElementById('fleet-coherence-card');
    if (fleetCard) {
        const toggleHealth = (health) => {
            const current = state.get('agentHealthFilter');
            const next = current === health ? null : health;
            state.set({ agentHealthFilter: next });
            if (typeof applyAgentFilters === 'function') applyAgentFilters();
            scrollToSection('agents-section');
        };
        fleetCard.addEventListener('click', (e) => {
            const link = e.target.closest('.fleet-critical-link');
            if (!link) return;
            e.stopPropagation();
            toggleHealth(link.dataset.health || 'critical');
        });
        fleetCard.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const link = e.target.closest('.fleet-critical-link');
            if (!link) return;
            e.preventDefault();
            toggleHealth(link.dataset.health || 'critical');
        });
    }
})();

// Thresholds button
const thresholdsBtn = document.getElementById('thresholds-btn');
if (thresholdsBtn) {
    thresholdsBtn.addEventListener('click', async () => {
        const modal = document.getElementById('panel-modal');
        const modalTitle = document.getElementById('modal-title');
        const modalBody = document.getElementById('modal-body');
        if (!modal || !modalTitle || !modalBody) return;
        modalTitle.textContent = 'Thresholds';
        modalBody.innerHTML = '<div class="loading">Loading...</div>';
        modal.classList.add('visible');
        document.body.style.overflow = 'hidden';
        try {
            const r = await callTool('config', { action: 'get' });
            const t = r?.thresholds || {};
            const keys = ['risk_approve_threshold', 'risk_revise_threshold', 'coherence_critical_threshold', 'void_threshold_initial'];
            modalBody.innerHTML = '<div class="thresholds-form">' +
                keys.map(k => `<div><label>${k.replace(/_/g, ' ')}:</label> <input type="number" step="0.01" data-key="${k}" value="${t[k] ?? ''}" style="width:80px"></div>`).join('') +
                '<button class="panel-button mt-md" id="thresholds-save">Save</button></div>';
            modalBody.querySelector('#thresholds-save').addEventListener('click', async () => {
                const inputs = modalBody.querySelectorAll('input[data-key]');
                const thresholds = {};
                inputs.forEach(inp => { const v = parseFloat(inp.value); if (!isNaN(v)) thresholds[inp.dataset.key] = v; });
                if (Object.keys(thresholds).length === 0) return;
                const result = await callTool('config', { action: 'set', thresholds });
                if (result?.success) {
                    modalBody.innerHTML = '<div class="loading">Saved.</div>';
                    setTimeout(closeModal, 800);
                } else {
                    modalBody.innerHTML += '<div class="text-secondary-sm mb-md">Error: ' + (result?.errors?.join(', ') || '') + '</div>';
                }
            });
        } catch (e) {
            modalBody.innerHTML = '<div class="loading">Error: ' + escapeHtml(e.message) + '</div>';
        }
    });
}

// showAgentDetail is now in agents.js → AgentsModule.showAgentDetail

// showDiscoveryDetail is now in discoveries.js → DiscoveriesModule.showDiscoveryDetail

// showDialecticDetail is now in dialectic.js → DialecticModule.showDialecticDetail

// renderDialecticDetailContent is now in dialectic.js → DialecticModule.renderDialecticDetailContent

// Theme toggle
const themeToggle = document.getElementById('theme-toggle');
const themeIcon = document.getElementById('theme-icon');
const themeLabel = document.getElementById('theme-label');
if (themeToggle && themeManager) {
    themeToggle.addEventListener('click', () => {
        const newTheme = themeManager.toggle();
        if (themeIcon) themeIcon.textContent = newTheme === 'dark' ? '🌙' : '☀️';
        if (themeLabel) themeLabel.textContent = newTheme === 'dark' ? 'Dark' : 'Light';
    });
    // Set initial icon
    const currentTheme = themeManager.getTheme();
    if (themeIcon) themeIcon.textContent = currentTheme === 'dark' ? '🌙' : '☀️';
    if (themeLabel) themeLabel.textContent = currentTheme === 'dark' ? 'Dark' : 'Light';
} else if (themeToggle) {
    // Hide theme toggle if themeManager not available
    themeToggle.style.display = 'none';
}


// exportAgents is now in agents.js → AgentsModule.exportAgents

// exportDiscoveries is now in discoveries.js → DiscoveriesModule.exportDiscoveries

const exportAgentsCsv = document.getElementById('export-agents-csv');
const exportAgentsJson = document.getElementById('export-agents-json');
const exportDiscoveriesCsv = document.getElementById('export-discoveries-csv');
const exportDiscoveriesJson = document.getElementById('export-discoveries-json');

if (exportAgentsCsv) exportAgentsCsv.addEventListener('click', () => exportAgents('csv'));
if (exportAgentsJson) exportAgentsJson.addEventListener('click', () => exportAgents('json'));
if (exportDiscoveriesCsv) exportDiscoveriesCsv.addEventListener('click', () => exportDiscoveries('csv'));
if (exportDiscoveriesJson) exportDiscoveriesJson.addEventListener('click', () => exportDiscoveries('json'));

// ========================================
// EISV Charts Module (eisv-charts.js)
// ========================================
// Chart init, WebSocket, governance pulse, decisions log, drift gauges,
// and value animations are now in EISVChartsModule.
var animateValue = EISVChartsModule.animateValue;
var updateValueWithGlow = EISVChartsModule.updateValueWithGlow;
var addEISVDataPoint = EISVChartsModule.addEISVDataPoint;
var addEventEntry = EISVChartsModule.addEventEntry;
var fetchInitialEvents = EISVChartsModule.fetchInitialEvents;
var getVerdictBadge = EISVChartsModule.getVerdictBadge;
var rebuildChartFromSelection = EISVChartsModule.rebuildChartFromSelection;
var updateAgentDropdown = EISVChartsModule.updateAgentDropdown;
var initEISVChart = EISVChartsModule.initEISVChart;
var initWebSocket = EISVChartsModule.initWebSocket;
var updateGovernancePulse = EISVChartsModule.updateGovernancePulse;
var updateAgentCardFromWS = EISVChartsModule.updateAgentCardFromWS;

// EISV functions removed — see eisv-charts.js
// (computeFleetAverage, rebuildChartFromSelection, makeChartOptions, equilibriumPlugin,
//  initEISVChart, addEISVDataPoint, drift gauges, governance verdict/pulse,
//  decisions log, events log, value animations, WebSocket init)
// Module self-initializes chart + WebSocket on DOMContentLoaded.
// ============================================
// Timeline Module (timeline.js)
// ============================================
// Skeletons and WS status label in TimelineModule.
// Module self-initializes skeletons, range filter, and click handlers.
var updateWSStatusLabel = TimelineModule.updateWSStatusLabel;

// Patch EISV WebSocket to update status label
if (typeof EISVWebSocket !== 'undefined') {
    const origInitWS = initWebSocket;
    initWebSocket = function () {
        origInitWS();
        const checkInterval = setInterval(() => {
            const wsEl = document.querySelector('#ws-status .ws-dot');
            if (!wsEl) return;
            const currentClass = wsEl.className;
            if (currentClass.includes('connected')) updateWSStatusLabel('connected');
            else if (currentClass.includes('poll_error')) updateWSStatusLabel('poll_error');
            else if (currentClass.includes('polling')) updateWSStatusLabel('polling');
            else if (currentClass.includes('reconnecting')) updateWSStatusLabel('reconnecting');
            else updateWSStatusLabel('disconnected');
        }, CONFIG.SCROLL_FEEDBACK_MS);
    };
}

// ============================================
// Fleet Heatmap Toggle
// ============================================
(function initHeatmap() {
    const toggleBtn = document.getElementById('heatmap-toggle');
    const closeBtn = document.getElementById('heatmap-close');
    const panel = document.getElementById('heatmap-panel');
    if (!toggleBtn || !panel) return;

    function renderHeatmap() {
        if (typeof FleetHeatmap === 'undefined') return;
        const agentsWithMetrics = cachedAgents.filter(a => agentHasMetrics(a)).slice(0, 30);
        if (agentsWithMetrics.length === 0) return;
        const heatmap = new FleetHeatmap('fleet-heatmap');
        heatmap.render(agentsWithMetrics);
    }

    function toggleHeatmap() {
        const isVisible = panel.style.display !== 'none';
        panel.style.display = isVisible ? 'none' : '';
        if (!isVisible) renderHeatmap();
    }

    toggleBtn.addEventListener('click', toggleHeatmap);
    if (closeBtn) closeBtn.addEventListener('click', () => { panel.style.display = 'none'; });

    // Re-render on each refresh if visible. Intentional monkeypatch: wrap the
    // existing top-level `refresh` so the heatmap re-renders on every refresh
    // cycle without the refresh loop needing to know about this panel.
    const origRefresh = refresh;
    // eslint-disable-next-line no-func-assign -- deliberate wrap of the refresh fn
    refresh = async function () {
        const result = await origRefresh();
        if (panel.style.display !== 'none') renderHeatmap();
        return result;
    };
})();

// Initial load
// ============================================================================
// SECTION NAVIGATION (scroll-spy)
// ============================================================================
(function initSectionNav() {
    const nav = document.getElementById('section-nav');
    if (!nav) return;

    const navItems = nav.querySelectorAll('.section-nav-item');
    const sectionIds = Array.from(navItems).map(item => item.dataset.section);
    const sections = sectionIds.map(id => document.getElementById(id)).filter(Boolean);

    // Smooth scroll on click
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            if (!item.dataset.section) return; // External links (e.g. Phase Space)
            e.preventDefault();
            const target = document.getElementById(item.dataset.section);
            if (target) {
                const navHeight = nav.offsetHeight + 12;
                const targetPos = target.getBoundingClientRect().top + window.scrollY - navHeight;
                window.scrollTo({ top: targetPos, behavior: 'smooth' });
            }
        });
    });

    // Scroll-spy: highlight active section
    let ticking = false;
    function updateActiveSection() {
        const navHeight = nav.offsetHeight + 20;
        let activeId = sectionIds[0];

        for (let i = sections.length - 1; i >= 0; i--) {
            const rect = sections[i].getBoundingClientRect();
            if (rect.top <= navHeight + 40) {
                activeId = sectionIds[i];
                break;
            }
        }

        navItems.forEach(item => {
            item.classList.toggle('active', item.dataset.section === activeId);
        });
        ticking = false;
    }

    window.addEventListener('scroll', () => {
        if (!ticking) {
            requestAnimationFrame(updateActiveSection);
            ticking = true;
        }
    }, { passive: true });

    updateActiveSection();
})();

// ============================================================================
// SCROLL-TO-TOP BUTTON
// ============================================================================
(function initScrollToTop() {
    var btn = document.getElementById('scroll-top');
    if (!btn) return;

    var ticking = false;
    window.addEventListener('scroll', function () {
        if (!ticking) {
            requestAnimationFrame(function () {
                btn.classList.toggle('visible', window.scrollY > 400);
                ticking = false;
            });
            ticking = true;
        }
    }, { passive: true });

    btn.addEventListener('click', function () {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
})();

console.log('Dashboard initializing...');
console.log('API available:', typeof api !== 'undefined' && api !== null);
console.log('DataProcessor available:', typeof DataProcessor !== 'undefined');
console.log('ThemeManager available:', typeof themeManager !== 'undefined' && themeManager !== null);

// Hydrate help icon placeholders and show shortcuts hint
function hydrateHelpIcons() {
    if (typeof createHelpIcon !== 'function') return;
    var placeholders = document.querySelectorAll('.help-icon-placeholder');
    placeholders.forEach(function (el) {
        var term = el.getAttribute('data-term');
        if (term) {
            var icon = createHelpIcon(term);
            el.parentNode.replaceChild(icon, el);
        }
    });
}

function showShortcutsHintOnce() {
    if (localStorage.getItem('unitares_shortcuts_seen')) return;
    var hint = document.createElement('div');
    hint.className = 'shortcuts-hint';
    hint.innerHTML = 'Press <kbd>?</kbd> for keyboard shortcuts';
    document.body.appendChild(hint);
    requestAnimationFrame(function () { hint.classList.add('visible'); });

    function dismiss() {
        hint.classList.remove('visible');
        setTimeout(function () { hint.remove(); }, 400);
        localStorage.setItem('unitares_shortcuts_seen', 'true');
        document.removeEventListener('keydown', dismiss);
    }

    setTimeout(dismiss, 8000);
    document.addEventListener('keydown', dismiss);
}

function dashboardInit() {
    hydrateHelpIcons();
    showShortcutsHintOnce();
    setTimeout(function () { refresh(); }, 100);
    fetchInitialEvents();
}

// Wait for DOM to be ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        console.log('DOM ready, starting initial load');
        dashboardInit();
    });
} else {
    console.log('DOM already ready, starting initial load');
    dashboardInit();
}

// Auto-refresh every 30 seconds
setInterval(() => {
    if (!autoRefreshPaused) {
        refresh();
    }
}, CONFIG.REFRESH_INTERVAL_MS);
