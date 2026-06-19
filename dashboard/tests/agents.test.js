import { describe, it, expect, beforeEach } from 'vitest';
import { loadDashboardScripts, AGENT_LIST_SCRIPTS } from './helpers/load-dashboard.js';

// Each test gets a fresh jsdom global (vitest isolates files but not `it`s by
// default for globals mutated via window.eval), so reload the chain and reset
// the DOM + shared state per test.
let Agents;

function freshDom() {
    document.body.innerHTML = `
        <div id="agents-filter-info"></div>
        <div id="agents-container"></div>
    `;
}

function agent(overrides = {}) {
    return {
        agent_id: overrides.agent_id || 'a-' + Math.random().toString(36).slice(2, 8),
        label: 'Agent',
        lifecycle_status: 'active',
        status: 'active',
        total_updates: 5,
        last_update: new Date().toISOString(),
        tags: [],
        metrics: null,
        ...overrides,
    };
}

beforeEach(() => {
    loadDashboardScripts(AGENT_LIST_SCRIPTS);
    Agents = window.AgentsModule;
    freshDom();
    window.state.set({ cachedAgents: [], agentPageSize: 20, prodOnlyActive: false });
});

describe('isTestAgent / getProductionAgents', () => {
    it('flags exp_/val_/test_ prefixes and pytest labels as test agents', () => {
        expect(Agents.isTestAgent(agent({ label: 'exp_alpha' }))).toBe(true);
        expect(Agents.isTestAgent(agent({ label: 'val_run' }))).toBe(true);
        expect(Agents.isTestAgent(agent({ label: 'test_throwaway' }))).toBe(true);
        expect(Agents.isTestAgent(agent({ label: 'cli-pytest-worker' }))).toBe(true);
    });

    it('does NOT catch server-only patterns like itest- (client heuristic is looser)', () => {
        // Documents a real divergence: the server-side _is_test_agent flags
        // "itest-*", but the client isTestAgent uses \btest\b which has no word
        // boundary inside "itest". Pinned so the gap is intentional, not a
        // silent regression.
        expect(
            Agents.isTestAgent(agent({ label: 'itest-plugin', lifecycle_status: 'active' }))
        ).toBe(false);
    });

    it('flags agents tagged test/experimental', () => {
        expect(Agents.isTestAgent(agent({ label: 'Real', tags: ['experimental'] }))).toBe(true);
        expect(Agents.isTestAgent(agent({ label: 'Real', tags: ['production'] }))).toBe(false);
    });

    it('does not flag an actively-running, plainly-named agent', () => {
        expect(Agents.isTestAgent(agent({ label: 'Sentinel', lifecycle_status: 'active' }))).toBe(
            false
        );
    });

    it('getProductionAgents strips test agents from a mixed list', () => {
        const list = [
            agent({ label: 'Sentinel' }),
            agent({ label: 'test_throwaway', lifecycle_status: 'archived', total_updates: 0 }),
            agent({ label: 'Watcher' }),
        ];
        const prod = Agents.getProductionAgents(list);
        expect(prod.map((a) => a.label).sort()).toEqual(['Sentinel', 'Watcher']);
    });
});

describe('renderAgentsList — participated vs never-checked-in partition (#826/#836)', () => {
    it('renders participated agents in the main list and folds never-checked-in into a <details> group', () => {
        const real = agent({ label: 'RealWorker', total_updates: 7 });
        const ghosts = Array.from({ length: 3 }, (_, i) =>
            agent({ label: 'claude-thread-' + i, total_updates: 0 })
        );
        const all = [real, ...ghosts];
        window.state.set({ cachedAgents: all });

        Agents.renderAgentsList(all, '');
        const html = document.getElementById('agents-container').innerHTML;

        // The real participant renders as a card in the main (non-folded) list.
        const foldIdx = html.indexOf('agent-ghost-group');
        expect(foldIdx).toBeGreaterThan(-1); // a fold exists
        expect(html.indexOf('RealWorker')).toBeGreaterThan(-1);
        // RealWorker appears BEFORE the folded ghost group, i.e. in the main list.
        expect(html.indexOf('RealWorker')).toBeLessThan(foldIdx);

        // The fold is labelled with the never-checked-in count.
        expect(html).toContain('Never checked in');
        expect(html).toMatch(/Never checked in[^<]*3/);
    });

    it('shows the empty-main note when every agent is never-checked-in', () => {
        const ghosts = Array.from({ length: 2 }, (_, i) =>
            agent({ label: 'ghost-' + i, total_updates: 0 })
        );
        window.state.set({ cachedAgents: ghosts });

        Agents.renderAgentsList(ghosts, '');
        const html = document.getElementById('agents-container').innerHTML;

        expect(html).toContain('No agents have checked in');
        expect(html).toContain('agent-ghost-group');
    });

    it('does not render a fold when all agents have participated', () => {
        const all = [
            agent({ label: 'W1', total_updates: 3 }),
            agent({ label: 'W2', total_updates: 9 }),
        ];
        window.state.set({ cachedAgents: all });

        Agents.renderAgentsList(all, '');
        const html = document.getElementById('agents-container').innerHTML;

        expect(html).not.toContain('agent-ghost-group');
        expect(html).toContain('W1');
        expect(html).toContain('W2');
    });

    it('renders the empty-state message when the filtered list is empty', () => {
        window.state.set({ cachedAgents: [agent({ label: 'W1' })] });
        Agents.renderAgentsList([], '');
        const html = document.getElementById('agents-container').innerHTML;
        expect(html).toContain('No agents match the current filters');
    });
});

describe('agent cards are keyboard-operable (WCAG 2.1.1 / 4.1.2)', () => {
    it('renders each card as a focusable button with an accessible name', () => {
        const all = [agent({ label: 'RealWorker', total_updates: 7 })];
        window.state.set({ cachedAgents: all });
        Agents.renderAgentsList(all, '');

        const card = document.querySelector('#agents-container .agent-item');
        expect(card).not.toBeNull();
        expect(card.getAttribute('role')).toBe('button');
        expect(card.getAttribute('tabindex')).toBe('0');
        expect(card.getAttribute('aria-label')).toContain('RealWorker');
    });
});

describe('health filter — clickable critical count (#fix-1)', () => {
    function critAndWell() {
        return [
            agent({ label: 'CritAgent', total_updates: 5, health_status: 'critical' }),
            agent({ label: 'WellAgent', total_updates: 5, health_status: 'healthy' }),
        ];
    }

    it('applyAgentFilters shows only agents matching agentHealthFilter', () => {
        const all = critAndWell();
        window.state.set({ cachedAgents: all, agentHealthFilter: 'critical' });
        Agents.applyAgentFilters();
        const html = document.getElementById('agents-container').innerHTML;
        expect(html).toContain('CritAgent');
        expect(html).not.toContain('WellAgent');
    });

    it('shows all agents when no health filter is set', () => {
        const all = critAndWell();
        window.state.set({ cachedAgents: all, agentHealthFilter: null });
        Agents.applyAgentFilters();
        const html = document.getElementById('agents-container').innerHTML;
        expect(html).toContain('CritAgent');
        expect(html).toContain('WellAgent');
    });

    it('clearAgentFilters drops the health filter', () => {
        window.state.set({ cachedAgents: critAndWell(), agentHealthFilter: 'critical' });
        Agents.clearAgentFilters();
        expect(window.state.get('agentHealthFilter')).toBeNull();
    });
});
