import { describe, it, expect, beforeEach } from 'vitest';
import { loadDashboardScripts } from './helpers/load-dashboard.js';

// DashboardState is a standalone pub/sub container (state.js, no deps). We build
// fresh instances via the exported constructor so tests never touch the singleton.
let DashboardState;

beforeEach(() => {
    loadDashboardScripts(['state.js']);
    DashboardState = window.DashboardState;
});

describe('DashboardState get/set', () => {
    it('returns initial values and undefined for unknown keys', () => {
        const s = new DashboardState({ a: 1, b: 'two' });
        expect(s.get('a')).toBe(1);
        expect(s.get('b')).toBe('two');
        expect(s.get('missing')).toBeUndefined();
    });

    it('set updates values and a later get reflects them', () => {
        const s = new DashboardState({ count: 0 });
        s.set({ count: 5, added: true });
        expect(s.get('count')).toBe(5);
        expect(s.get('added')).toBe(true);
    });

    it('set ignores inherited (non-own) properties', () => {
        const s = new DashboardState({});
        // An object whose prototype carries `polluted` — for..in would surface it
        // without the own-property guard fixed in state.js.
        const proto = { polluted: 'nope' };
        const updates = Object.create(proto);
        updates.real = 'yes';
        s.set(updates);
        expect(s.get('real')).toBe('yes');
        expect(s.get('polluted')).toBeUndefined();
    });
});

describe('DashboardState subscriptions', () => {
    it('notifies a key listener with (newVal, oldVal, key)', () => {
        const s = new DashboardState({ x: 1 });
        const calls = [];
        s.on('x', (newVal, oldVal, key) => calls.push([newVal, oldVal, key]));
        s.set({ x: 2 });
        expect(calls).toEqual([[2, 1, 'x']]);
    });

    it('does not fire a key listener for other keys', () => {
        const s = new DashboardState({ x: 1, y: 1 });
        let fired = false;
        s.on('x', () => {
            fired = true;
        });
        s.set({ y: 9 });
        expect(fired).toBe(false);
    });

    it('notifies wildcard listeners with (key, newVal, oldVal)', () => {
        const s = new DashboardState({ x: 1 });
        const calls = [];
        s.on('*', (key, newVal, oldVal) => calls.push([key, newVal, oldVal]));
        s.set({ x: 7 });
        expect(calls).toEqual([['x', 7, 1]]);
    });

    it('the returned unsubscribe stops further key notifications', () => {
        const s = new DashboardState({ x: 1 });
        let count = 0;
        const off = s.on('x', () => {
            count += 1;
        });
        s.set({ x: 2 });
        off();
        s.set({ x: 3 });
        expect(count).toBe(1);
    });

    it('the returned unsubscribe stops further wildcard notifications', () => {
        const s = new DashboardState({ x: 1 });
        let count = 0;
        const off = s.on('*', () => {
            count += 1;
        });
        s.set({ x: 2 });
        off();
        s.set({ x: 3 });
        expect(count).toBe(1);
    });
});

describe('DashboardState snapshot', () => {
    it('returns a shallow copy that does not mutate internal state', () => {
        const s = new DashboardState({ a: 1 });
        const snap = s.snapshot();
        snap.a = 999;
        expect(s.get('a')).toBe(1);
    });
});
