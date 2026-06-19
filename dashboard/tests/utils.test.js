import { describe, it, expect, beforeEach } from 'vitest';
import { loadDashboardScripts } from './helpers/load-dashboard.js';

// DataProcessor is a Layer-0 module (utils.js, no deps). escapeHtml and
// highlightMatch are the dashboard's XSS boundary, so they get the most coverage.
let DataProcessor;

beforeEach(() => {
    loadDashboardScripts(['utils.js']);
    DataProcessor = window.DataProcessor;
});

describe('DataProcessor.escapeHtml', () => {
    it('returns empty string for null/undefined', () => {
        expect(DataProcessor.escapeHtml(null)).toBe('');
        expect(DataProcessor.escapeHtml(undefined)).toBe('');
    });

    it('escapes the five HTML-sensitive characters', () => {
        expect(DataProcessor.escapeHtml('&')).toBe('&amp;');
        expect(DataProcessor.escapeHtml('<')).toBe('&lt;');
        expect(DataProcessor.escapeHtml('>')).toBe('&gt;');
        expect(DataProcessor.escapeHtml('"')).toBe('&quot;');
        expect(DataProcessor.escapeHtml("'")).toBe('&#39;');
    });

    it('neutralizes a script-tag XSS payload', () => {
        expect(DataProcessor.escapeHtml("<script>alert('x')</script>")).toBe(
            '&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;'
        );
    });

    it('coerces non-string input to string', () => {
        expect(DataProcessor.escapeHtml(42)).toBe('42');
    });
});

describe('DataProcessor.highlightMatch', () => {
    it('escapes the text when no term is given', () => {
        expect(DataProcessor.highlightMatch('<b>', '')).toBe('&lt;b&gt;');
        expect(DataProcessor.highlightMatch('<b>', null)).toBe('&lt;b&gt;');
    });

    it('wraps a case-insensitive match in a <mark>', () => {
        expect(DataProcessor.highlightMatch('Hello World', 'world')).toBe(
            'Hello <mark class="highlight">World</mark>'
        );
    });

    it('escapes HTML in both the matched and unmatched segments', () => {
        // The matched "<world>" is HTML-escaped inside the <mark>; surrounding
        // text is escaped too. Confirms highlighting never opens an XSS hole.
        expect(DataProcessor.highlightMatch('a <world> b', '<world>')).toBe(
            'a <mark class="highlight">&lt;world&gt;</mark> b'
        );
    });

    it('treats regex metacharacters in the term as literals', () => {
        // "a.b" must match the literal "a.b", not "axb" — the term is regex-escaped.
        expect(DataProcessor.highlightMatch('axb a.b', 'a.b')).toBe(
            'axb <mark class="highlight">a.b</mark>'
        );
    });
});

describe('DataProcessor.formatRelativeTime', () => {
    const MIN = 60 * 1000;
    const HOUR = 60 * MIN;
    const DAY = 24 * HOUR;

    it('returns null for falsy input', () => {
        expect(DataProcessor.formatRelativeTime(0)).toBeNull();
        expect(DataProcessor.formatRelativeTime(null)).toBeNull();
    });

    it('returns "just now" for non-positive elapsed time', () => {
        expect(DataProcessor.formatRelativeTime(Date.now() + 5000)).toBe('just now');
    });

    it('buckets seconds / minutes / hours / days', () => {
        expect(DataProcessor.formatRelativeTime(Date.now() - 5000)).toBe('5s ago');
        expect(DataProcessor.formatRelativeTime(Date.now() - 3 * MIN)).toBe('3m ago');
        expect(DataProcessor.formatRelativeTime(Date.now() - 2 * HOUR)).toBe('2h ago');
        expect(DataProcessor.formatRelativeTime(Date.now() - 3 * DAY)).toBe('3d ago');
    });

    it('buckets weeks / months / years', () => {
        expect(DataProcessor.formatRelativeTime(Date.now() - 14 * DAY)).toBe('2w ago');
        expect(DataProcessor.formatRelativeTime(Date.now() - 60 * DAY)).toBe('2mo ago');
        expect(DataProcessor.formatRelativeTime(Date.now() - 800 * DAY)).toBe('2y ago');
    });
});

describe('DataProcessor.formatTimestamp', () => {
    it('returns null for falsy or unparseable input', () => {
        expect(DataProcessor.formatTimestamp(null)).toBeNull();
        expect(DataProcessor.formatTimestamp('not-a-date')).toBeNull();
    });

    it('produces a readable string for a valid timestamp', () => {
        // Exact time is locale/timezone dependent; assert structure, not the clock.
        const out = DataProcessor.formatTimestamp('2026-03-15T12:00:00Z');
        expect(out).toMatch(/Mar \d+/);
        expect(typeof out).toBe('string');
    });
});
