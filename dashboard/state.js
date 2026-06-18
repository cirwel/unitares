/**
 * Unitares Dashboard — State Manager
 *
 * Simple pub/sub state container wrapping dashboard globals.
 * Modules read/write via state.get()/state.set() instead of bare globals.
 */
(function () {
    'use strict';

    function DashboardState(initial) {
        this._state = Object.assign({}, initial);
        this._listeners = {};   // key → [fn, ...]
        this._anyListeners = []; // fn(key, newVal, oldVal)
    }

    DashboardState.prototype.get = function (key) {
        return this._state[key];
    };

    DashboardState.prototype.set = function (updates) {
        for (var key in updates) {
            if (!Object.prototype.hasOwnProperty.call(updates, key)) continue;
            var oldVal = this._state[key];
            var newVal = updates[key];
            this._state[key] = newVal;
            var kl = this._listeners[key];
            if (kl) for (var i = 0; i < kl.length; i++) kl[i](newVal, oldVal, key);
            for (var j = 0; j < this._anyListeners.length; j++) this._anyListeners[j](key, newVal, oldVal);
        }
    };

    /** Subscribe to changes. key='*' for all. Returns unsubscribe function. */
    DashboardState.prototype.on = function (key, fn) {
        if (key === '*') {
            this._anyListeners.push(fn);
        } else {
            if (!this._listeners[key]) this._listeners[key] = [];
            this._listeners[key].push(fn);
        }
        var self = this;
        return function () {
            if (key === '*') {
                var idx = self._anyListeners.indexOf(fn);
                if (idx !== -1) self._anyListeners.splice(idx, 1);
            } else if (self._listeners[key]) {
                var idx2 = self._listeners[key].indexOf(fn);
                if (idx2 !== -1) self._listeners[key].splice(idx2, 1);
            }
        };
    };

    DashboardState.prototype.snapshot = function () {
        return Object.assign({}, this._state);
    };

    // Singleton with initial values matching the old globals
    var dashboardState = new DashboardState({
        refreshFailures: 0,
        autoRefreshPaused: false,
        previousStats: {},
        cachedAgents: [],
        cachedDiscoveries: [],
        filteredDiscoveries: [],
        cachedStuckAgents: [],
        cachedDialecticSessions: [],
        eisvChartUpper: null,
        eisvChartLower: null,
        eisvWebSocket: null,
        agentEISVHistory: {},
        knownAgents: new Set(),
        selectedAgentView: '__fleet__',
        lastVitalsTimestamp: null,
        pinnedAgentId: null,
        pinnedAgentName: null,
        prodOnlyActive: typeof localStorage !== 'undefined' && localStorage.getItem('unitares_prod_only') === 'true',
        showODE: typeof localStorage !== 'undefined' && localStorage.getItem('unitares_show_ode') !== 'false',
        agentPageSize: 20,
        agentTierFilter: null
    });

    // Restore pinned agent from localStorage
    if (typeof window !== 'undefined' && window.localStorage) {
        var savedId = localStorage.getItem('unitares_pinned_agent_id');
        var savedName = localStorage.getItem('unitares_pinned_agent_name');
        if (savedId) {
            dashboardState.set({ pinnedAgentId: savedId, pinnedAgentName: savedName || savedId });
        }
    }

    if (typeof window !== 'undefined') {
        window.DashboardState = DashboardState;
        window.state = dashboardState;
    }
})();
