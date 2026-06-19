import { describe, it, expect, beforeEach } from 'vitest';
import { loadDashboardScripts } from './helpers/load-dashboard.js';

// computeFleetSeverity is a pure function (fleet-severity.js, no deps).
let compute;

beforeEach(() => {
    loadDashboardScripts(['fleet-severity.js']);
    compute = window.computeFleetSeverity;
});

describe('computeFleetSeverity — level', () => {
    it('is healthy with no signals', () => {
        const r = compute({});
        expect(r.level).toBe('healthy');
        expect(r.reasons).toEqual([]);
        expect(r.text).toBe('All systems healthy');
    });

    it('is healthy when counts are zero/falsy', () => {
        expect(
            compute({
                criticalAgents: 0,
                stuckCount: 0,
                watcher: { critical: 0, high: 0 },
                sentinel: { critical: 0, high: 0 },
                silentResidents: [],
            }).level
        ).toBe('healthy');
    });

    it('escalates to critical when the database (system health) is unavailable', () => {
        // The proposal's canonical failure: hero must NOT read healthy here.
        const r = compute({ systemHealth: 'unavailable' });
        expect(r.level).toBe('critical');
        expect(r.text).toContain('System health');
    });

    it('treats system health error and critical as critical too', () => {
        expect(compute({ systemHealth: 'error' }).level).toBe('critical');
        expect(compute({ systemHealth: 'critical' }).level).toBe('critical');
        expect(compute({ systemHealth: 'healthy' }).level).toBe('healthy');
        expect(compute({ systemHealth: 'warning' }).level).toBe('healthy'); // warning isn't critical
    });

    it('critical agents, Watcher critical, and Sentinel critical each escalate to red', () => {
        expect(compute({ criticalAgents: 1 }).level).toBe('critical');
        expect(compute({ watcher: { critical: 2 } }).level).toBe('critical');
        expect(compute({ sentinel: { critical: 1 } }).level).toBe('critical');
    });

    it('stuck agents, Sentinel-high, and silent residents are caution (amber), not red', () => {
        expect(compute({ stuckCount: 3 }).level).toBe('caution');
        expect(compute({ sentinel: { high: 4 } }).level).toBe('caution');
        expect(compute({ silentResidents: ['Vigil'] }).level).toBe('caution');
        expect(compute({ avgCoherence: 0.3 }).level).toBe('caution');
    });

    it('a failed monitoring feed is caution, never silently "all clear"', () => {
        expect(compute({ sentinel: { error: true } }).level).toBe('caution');
        expect(compute({ watcher: { error: true } }).level).toBe('caution');
    });

    it('critical wins over caution when both are present', () => {
        const r = compute({ criticalAgents: 1, stuckCount: 5 });
        expect(r.level).toBe('critical');
    });
});

describe('computeFleetSeverity — reasons & text', () => {
    it('orders critical reasons ahead of caution reasons', () => {
        const r = compute({ stuckCount: 2, criticalAgents: 1 });
        expect(r.reasons[0].severity).toBe('critical');
        expect(r.reasons[r.reasons.length - 1].severity).toBe('caution');
    });

    it('attaches a deep-link anchor to each reason', () => {
        const r = compute({ watcher: { critical: 1 }, silentResidents: 2 });
        const byAnchor = r.reasons.map((x) => x.anchor);
        expect(byAnchor).toContain('#watcher-section');
        expect(byAnchor).toContain('#residents-section');
    });

    it('summarizes the hero text as the top reason plus an overflow count', () => {
        const r = compute({ criticalAgents: 1, stuckCount: 2, silentResidents: 1 });
        expect(r.text).toMatch(/^1 agent critical \(\+2 more\)$/);
    });

    it('singularizes/pluralizes agent and resident labels', () => {
        expect(compute({ criticalAgents: 1 }).reasons[0].label).toBe('1 agent critical');
        expect(compute({ criticalAgents: 2 }).reasons[0].label).toBe('2 agents critical');
        expect(compute({ silentResidents: 1 }).reasons[0].label).toBe('1 resident silent');
        expect(compute({ silentResidents: ['a', 'b'] }).reasons[0].label).toBe(
            '2 residents silent'
        );
    });
});
